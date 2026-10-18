"""LINE Messaging API：webhook（路線 2 LINE 案件、加好友綁定）、回覆與推送、依身分切換選單。

設計文件 2.1 節路線 2、5.1 節、5.5 節：驗證 X-Line-Signature、以 webhookEventId 去重；
回覆權杖失敗時改用 push；push 失敗在 LINE 案件標示「通知失敗」。

2026-10-19 起（fe-be-integration 設計文件）：這是官方帳號唯一的 webhook 入口（後端不再有自己的 webhook）；
文字訊息不需要喚醒詞；緊急關鍵字在呼叫後端之前處理；可輸入「轉交給督導」「直接問督導：…」。
"""

import json
import logging
import math
import re
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    ButtonsTemplate,
    Configuration,
    MessageAction,
    MessagingApi,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TemplateMessage,
    TextMessage,
    URIAction,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, PostbackEvent, TextMessageContent

from . import emergency, services
from .backend import Backend, BackgroundRunner
from .config import Settings
from .db import Database, now
from .wake_word import strip_wake_word

logger = logging.getLogger(__name__)

LINE_TEXT_LIMIT = 5000
# 等不到 line_reply_wait_seconds 內的回覆時，先告知照護員（2026-10-14 async-backend 第 7 節）
STILL_QUERYING_TEXT = "小照還在查詢，回答好了會再通知你。"
ESCALATED_TEXT = "這個問題已轉給督導，督導回覆後會通知你。"
# 「轉交給督導」：把上一題轉給督導；「直接問督導：…」：不經 AI 直接建立督導請求（需求決議「與長照無關的問題」）
HANDOFF_TEXT = "轉交給督導"
HANDOFF_PATTERN = re.compile(r"^(?:我想|我要|請)?(?:轉交給督導|轉給督導|直接問督導|轉督導)[\s，,、。:：!！]*(.*)$", re.S)
# LINE loading animation 只接受 5～60 秒、5 的倍數
LOADING_MIN_SECONDS, LOADING_MAX_SECONDS = 5, 60

# 加好友時的歡迎訊息（2026-10-12 提案學生指定文字；2026-10-19 依需求決議改為免喚醒詞、補助金額可回答）
WELCOME_TEXT = (
    "我是小照，請直接輸入您的問題，例如「我可以幫長輩代購藥物嗎？」。"
    "我只回答長照規範查詢並附上原文出處；醫療、用藥，以及知識庫沒有依據的問題會轉給督導。"
    "遇到緊急狀況請先撥打 119。"
)

HELP_TEXT = (
    "小照使用說明\n"
    "・聊天室提問：直接輸入文字即可（開頭加不加「小照你好」都可以），例如「家屬要我幫忙代購，機構規定可以嗎？」。\n"
    "・回答範圍：只回答規範查詢並附上原文出處；醫療、用藥，以及知識庫沒有依據的問題，會轉給督導回覆。\n"
    "・找督導：輸入「直接問督導：你的問題」不經小照直接轉督導；輸入「轉交給督導」把上一個問題轉給督導。\n"
    "・緊急狀況（沒有呼吸、噎到、跌倒、走失…）：小照會立即回覆急救指引並通知督導，請同時撥打 119。\n"
    "・錄音模式：說「小照你好」開始一輪，說「小照請回答」、安靜 3 秒或說下一個「小照你好」結束。會在外部瀏覽器開啟，錄音中請保持螢幕亮著。\n"
    "・今日狀況回報：口述當天狀況，說完安靜 3 秒自動送出給督導。\n"
    "・住民、家屬請一律用代稱，不要輸入姓名、房號。"
)


class InvalidSignature(services.ServiceError):
    status_code = 400


class LineService:
    def __init__(
        self, settings: Settings, db: Database, backend: Backend,
        classify: Callable[[str], tuple[str, float]] | None = None, runner: BackgroundRunner | None = None,
        identity=None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._backend = backend
        self._classify = classify
        self._runner = runner or BackgroundRunner(inline=True)
        # 後端查詢另用一組執行緒：webhook 事件本身就在 runner 裡執行並等待查詢結果，
        # 共用同一組會在多則訊息同時進來時把執行緒占滿、查詢排不進去
        self._query_runner = BackgroundRunner(settings.backend_workers, inline=self._runner.inline)
        self._identity = identity
        self._parser = WebhookParser(settings.channel_secret)
        self._configuration = Configuration(access_token=settings.channel_access_token)
        self._richmenu_file = settings.data_dir / "richmenus.json"

    # ---------- webhook ----------

    def handle_webhook(self, body: str, signature: str) -> int:
        return self.process_events(self.parse_events(body, signature))

    def parse_events(self, body: str, signature: str) -> list:
        try:
            return self._parser.parse(body, signature)
        except InvalidSignatureError as exc:
            raise InvalidSignature("簽章錯誤") from exc

    def process_events(self, events: list) -> int:
        handled = 0
        for event in events:
            event_id = getattr(event, "webhook_event_id", None)
            if event_id:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO webhook_events (event_id, received_at) VALUES (?, ?)", (event_id, now())
                )
                if cur.rowcount == 0:
                    continue
            try:
                self._dispatch(event)
                handled += 1
            except Exception:
                logger.exception("處理 webhook 事件失敗（%s）", type(event).__name__)
        return handled

    def _dispatch(self, event) -> None:
        user_id = getattr(event.source, "user_id", None)
        if user_id is None:
            return  # 不處理群組聊天（非目標）
        if isinstance(event, FollowEvent):
            self._on_follow(event, user_id)
        elif isinstance(event, PostbackEvent):
            if event.postback.data == "help":
                self.reply(event.reply_token, user_id,
                           f"{HELP_TEXT}\n\n完整說明與知識庫法規涵蓋範圍：{self._settings.liff_url}?page=help")
        elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            self._on_text(event, user_id)

    def _on_follow(self, event, user_id: str) -> None:
        account = services.active_account(self._db, user_id, self._identity)
        is_admin = user_id in self._settings.admin_user_ids
        welcome = TextMessage(text=WELCOME_TEXT)
        if account or is_admin:
            role = "admin" if is_admin else account["role"]
            self.link_menu(user_id, role)
            self._reply_messages(event.reply_token, [welcome])
            return
        # 尚未綁定：歡迎訊息後接綁定碼按鈕
        self._reply_messages(event.reply_token, [welcome, self._bind_message()])

    def _bind_message(self) -> TemplateMessage:
        return TemplateMessage(
            alt_text="請輸入綁定碼",
            template=ButtonsTemplate(
                title="歡迎使用小照",
                text="請輸入管理員給你的綁定碼，完成後即可使用。",
                actions=[URIAction(label="輸入綁定碼", uri=f"{self._settings.liff_url}?page=bind")],
            ),
        )

    def _on_text(self, event, user_id: str) -> None:
        # 文字訊息不需要喚醒詞；開頭有「小照你好」（或說反）就去掉（2026-10-19 需求決議）
        _, question = strip_wake_word(event.message.text)
        is_admin = user_id in self._settings.admin_user_ids
        match = emergency.detect(question) if question else None
        if match is not None:
            # 緊急狀況先於綁定與身分檢查：查不到身分（或後端掛掉）也要先給急救指引
            try:
                account = services.active_account(self._db, user_id, self._identity)
            except services.ServiceError:
                logger.exception("緊急訊息查詢身分失敗，仍回覆急救指引")
                account = None
            self._on_emergency(event, user_id, question, match, bound=account is not None or is_admin)
            return
        account = services.active_account(self._db, user_id, self._identity)
        if account is None and not is_admin:
            self._reply_messages(event.reply_token, [self._bind_message()])
            return
        if account and account["role"] != "caregiver" and not is_admin:
            self.reply(event.reply_token, user_id, "LINE 案件只開放照護員使用；督導請到督導頁面回覆。")
            return
        if not question:
            self.reply(event.reply_token, user_id, "請直接輸入你的問題，例如「喘息服務怎麼申請？」。")
            return
        handoff = HANDOFF_PATTERN.match(question)
        if handoff is not None:
            self._on_handoff(event, user_id, handoff.group(1).strip())
            return
        self._create_line_case(event, user_id, question)

    def _insert_line_case(self, event, user_id: str, question: str, status: str) -> int | None:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO line_cases (line_message_id, caregiver_line_user_id, question, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (event.message.id, user_id, question, status, now()),
        )
        return cur.lastrowid if cur.rowcount else None  # 同一則訊息已處理過

    def _on_emergency(self, event, user_id: str, question: str, match: emergency.EmergencyMatch,
                      bound: bool) -> None:
        """固定回覆＋緊急督導請求＋通知督導；不呼叫後端、不經 LLM。未綁定的人只給急救指引，不建請求。"""
        case_id = self._insert_line_case(event, user_id, question, "emergency")
        if case_id is None:
            return
        notified = False
        if bound:
            request_id = services.create_emergency_request(self._db, user_id, "line_case", case_id, question, match)
            notified = self.notify_emergency(request_id, user_id, match)
        text = emergency.reply_text(match, self._settings.agency_phone, notified)
        self._db.execute("UPDATE line_cases SET answer = ? WHERE id = ?", (text, case_id))
        if not self.reply(event.reply_token, user_id, text):
            self._db.execute("UPDATE line_cases SET notify_failed = 1 WHERE id = ?", (case_id,))

    def notify_emergency(self, request_id: int, user_id: str, match: emergency.EmergencyMatch) -> bool:
        """push 給所有已綁定的督導（免費方案只有緊急才 push）；只放代號、類別與請求編號，不放問題原文。"""
        who = services.caller_fields(self._db, user_id)["caregiver_name"]
        level = "一級" if match.level == 1 else "二級"
        text = f"【緊急・{level}】{match.category}\n照護員：{who}\n督導請求 #{request_id}，請盡快到督導頁面查看並聯絡照護員。"
        results = [self.push(sid, text) for sid in services.active_supervisor_line_ids(self._db)]
        return any(results)

    def _on_handoff(self, event, user_id: str, question: str) -> None:
        if question:
            case_id = self._insert_line_case(event, user_id, question, "awaiting_human")
            if case_id is None:
                return
        else:
            last = self._db.one(
                "SELECT * FROM line_cases WHERE caregiver_line_user_id = ? AND status != 'emergency'"
                " ORDER BY id DESC LIMIT 1", (user_id,))
            if last is None:
                self.reply(event.reply_token, user_id, "請在「直接問督導：」後面寫下你的問題。")
                return
            if self._db.one("SELECT 1 FROM supervisor_requests WHERE source = 'line_case' AND source_id = ?",
                            (last["id"],)):
                self.reply(event.reply_token, user_id, "上一個問題已經轉給督導了，督導回覆後會通知你。")
                return
            case_id, question = last["id"], last["question"]
            self._db.execute("UPDATE line_cases SET status = 'awaiting_human' WHERE id = ?", (case_id,))
        services.create_supervisor_request(self._db, user_id, "line_case", case_id, question, "照護員要求轉督導",
                                           None, category="user_requested")
        self.reply(event.reply_token, user_id, ESCALATED_TEXT)

    def _create_line_case(self, event, user_id: str, question: str) -> None:
        case_id = self._insert_line_case(event, user_id, question, "pending")
        if case_id is None:
            return
        intent, conf = self._classify(question) if self._classify else (None, None)
        future = self._query_runner.submit(self._backend.query, {
            "line_case_id": case_id, **services.caller_fields(self._db, user_id),
            "text": question, "intent": intent, "intent_conf": conf,
            "keywords": [], "provisional": False,
        })
        # 後端實測 9–33 秒：先顯示「輸入中」動畫（不計費），在 reply token 有效期內用 reply 回答，
        # 不占免費方案每月 200 則的 push 額度（2026-10-19 fe-be-integration 第 4.4 節）
        wait = self._settings.line_reply_wait_seconds
        if not future.done():
            self.show_loading(user_id, wait)
        try:
            result = future.result(timeout=wait)
        except FutureTimeout:
            self.reply(event.reply_token, user_id, STILL_QUERYING_TEXT)
            future.add_done_callback(
                lambda f: f.exception() is None and self._finish_case(case_id, user_id, question, f.result(), None))
            return
        self._finish_case(case_id, user_id, question, result, event.reply_token)

    def _finish_case(self, case_id: int, user_id: str, question: str, result, reply_token: str | None) -> None:
        quick_replies = None
        if result.status == "answered":
            answer, message = result.answer, result.answer
            if result.refusal_reason == "out_of_scope":
                quick_replies = [HANDOFF_TEXT]
        else:
            answer = None
            services.create_supervisor_request(
                self._db, user_id, "line_case", case_id, question, result.reason, result.request_id,
                category=result.refusal_reason,
            )
            # 例如醫療問題：後端附的安全提示一併給照護員
            message = f"{result.answer}\n\n{ESCALATED_TEXT}" if result.answer else ESCALATED_TEXT
        self._db.execute("UPDATE line_cases SET answer = ?, status = ?, reply_ms = ? WHERE id = ?",
                         (answer, result.status, result.elapsed_ms, case_id))
        sent = self.reply(reply_token, user_id, message, quick_replies) if reply_token else self.push(user_id, message)
        if not sent:
            self._db.execute("UPDATE line_cases SET notify_failed = 1 WHERE id = ?", (case_id,))

    # ---------- 傳送 ----------

    def _reply_messages(self, reply_token: str, messages: list) -> bool:
        try:
            with ApiClient(self._configuration) as client:
                MessagingApi(client).reply_message(ReplyMessageRequest(reply_token=reply_token, messages=messages))
            return True
        except Exception:
            logger.exception("reply 失敗")
            return False

    def reply(self, reply_token: str, user_id: str, text: str, quick_replies: list[str] | None = None) -> bool:
        """先用回覆權杖；失敗（例如權杖逾時）改用 push。quick_replies 為快速回覆按鈕文字（按下即送出該文字）。"""
        quick_reply = QuickReply(items=[
            QuickReplyItem(action=MessageAction(label=label, text=label)) for label in quick_replies
        ]) if quick_replies else None
        if self._reply_messages(reply_token, [TextMessage(text=text[:LINE_TEXT_LIMIT], quick_reply=quick_reply)]):
            return True
        return self.push(user_id, text)

    def push(self, user_id: str, text: str) -> bool:
        try:
            with ApiClient(self._configuration) as client:
                MessagingApi(client).push_message(
                    PushMessageRequest(to=user_id, messages=[TextMessage(text=text[:LINE_TEXT_LIMIT])])
                )
            return True
        except Exception:
            logger.exception("push 失敗")
            return False

    def show_loading(self, user_id: str, seconds: float) -> None:
        """聊天室顯示「輸入中」動畫；收到回覆訊息時 LINE 會自動結束。失敗只記錄，不影響回答。"""
        loading = min(LOADING_MAX_SECONDS, max(LOADING_MIN_SECONDS, math.ceil(seconds / 5) * 5))
        try:
            with ApiClient(self._configuration) as client:
                MessagingApi(client).show_loading_animation(
                    ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=loading))
        except Exception:
            logger.exception("顯示輸入中動畫失敗")

    # ---------- 選單 ----------

    def link_menu(self, user_id: str, role: str) -> bool:
        """把該使用者的聊天室選單切到對應身分；選單由 richmenu.py setup 建立。"""
        menus = self._load_menus()
        menu_id = menus.get(role)
        if menu_id is None:
            logger.warning("尚未建立 %s 的選單，請先執行 python -m xiaozhao.richmenu setup", role)
            return False
        try:
            with ApiClient(self._configuration) as client:
                MessagingApi(client).link_rich_menu_id_to_user(user_id, menu_id)
            return True
        except Exception:
            logger.exception("切換選單失敗")
            return False

    def unlink_menu(self, user_id: str) -> None:
        try:
            with ApiClient(self._configuration) as client:
                MessagingApi(client).unlink_rich_menu_id_from_user(user_id)
        except Exception:
            logger.exception("移除選單失敗")

    def _load_menus(self) -> dict[str, str]:
        if not self._richmenu_file.exists():
            return {}
        return json.loads(Path(self._richmenu_file).read_text(encoding="utf-8"))
