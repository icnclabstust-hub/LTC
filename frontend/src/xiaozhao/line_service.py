"""LINE Messaging API：webhook（路線 2 LINE 案件、加好友綁定）、回覆與推送、依身分切換選單。

設計文件 2.1 節路線 2、5.1 節、5.5 節：驗證 X-Line-Signature、以 webhookEventId 去重；
回覆權杖失敗時改用 push；push 失敗在 LINE 案件標示「通知失敗」。
"""

import json
import logging
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
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TemplateMessage,
    TextMessage,
    URIAction,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, PostbackEvent, TextMessageContent

from . import services
from .backend import Backend, BackgroundRunner
from .config import Settings
from .db import Database, now

logger = logging.getLogger(__name__)

PREFIX_PATTERN = re.compile(r"^\s*小照你好[\s，,、。:：!！]*")
LINE_TEXT_LIMIT = 5000
# 超過回覆目標（3 秒）還沒拿到後端回覆時，先告知照護員（2026-10-14 async-backend 第 7 節）
STILL_QUERYING_TEXT = "小照還在查詢，回答好了會再通知你。"

# 加好友時的歡迎訊息（2026-10-12 提案學生指定文字）
WELCOME_TEXT = (
    "我是小照，請輸入您的問題，例如「小照你好，我可以幫長輩代購藥物嗎?」。"
    "我只回答規範查詢並附上出處；醫療、用藥、補助金額相關問題會轉給督導。"
)

HELP_TEXT = (
    "小照使用說明\n"
    "・聊天室提問：輸入「小照你好」開頭的文字，例如「小照你好，家屬要我幫忙代購，機構規定可以嗎？」；其他訊息小照不會回應。\n"
    "・回答範圍：只回答規範查詢並附上出處；醫療、用藥、補助金額，以及知識庫沒有依據的問題，會轉給督導回覆。\n"
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
    ) -> None:
        self._settings = settings
        self._db = db
        self._backend = backend
        self._classify = classify
        self._runner = runner or BackgroundRunner(inline=True)
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
        account = services.active_account(self._db, user_id)
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
        text = event.message.text
        account = services.active_account(self._db, user_id)
        is_admin = user_id in self._settings.admin_user_ids
        if account is None and not is_admin:
            self._reply_messages(event.reply_token, [self._bind_message()])
            return
        match = PREFIX_PATTERN.match(text)
        if match is None:
            return  # 只處理「小照你好」開頭的文字
        if account and account["role"] != "caregiver" and not is_admin:
            self.reply(event.reply_token, user_id, "LINE 案件只開放照護員使用；督導請到督導頁面回覆。")
            return
        question = text[match.end():].strip()
        if not question:
            self.reply(event.reply_token, user_id, "請在「小照你好」後面接著輸入你的問題。")
            return
        self._create_line_case(event, user_id, question)

    def _create_line_case(self, event, user_id: str, question: str) -> None:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO line_cases (line_message_id, caregiver_line_user_id, question, status, created_at)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (event.message.id, user_id, question, now()),
        )
        if cur.rowcount == 0:
            return  # 同一則訊息已處理過
        case_id = cur.lastrowid
        intent, conf = self._classify(question) if self._classify else (None, None)
        future = self._runner.submit(self._backend.query, {
            "line_case_id": case_id, "caregiver_name": services.caregiver_name(self._db, user_id),
            "text": question, "intent": intent, "intent_conf": conf,
            "keywords": [], "provisional": False,
        })
        # 回覆目標 3 秒：還沒好就先用回覆權杖告知，回答到了再 push（async-backend 第 7 節）
        try:
            result = future.result(timeout=self._settings.reply_target_seconds)
        except FutureTimeout:
            self.reply(event.reply_token, user_id, STILL_QUERYING_TEXT)
            future.add_done_callback(
                lambda f: f.exception() is None and self._finish_case(case_id, user_id, question, f.result(), None))
            return
        self._finish_case(case_id, user_id, question, result, event.reply_token)

    def _finish_case(self, case_id: int, user_id: str, question: str, result, reply_token: str | None) -> None:
        if result.status == "answered":
            answer, message = result.answer, result.answer
        else:
            answer = None
            services.create_supervisor_request(
                self._db, user_id, "line_case", case_id, question, result.reason, result.request_id
            )
            message = "這個問題已轉給督導，督導回覆後會通知你。"
        self._db.execute("UPDATE line_cases SET answer = ?, status = ?, reply_ms = ? WHERE id = ?",
                         (answer, result.status, result.elapsed_ms, case_id))
        sent = self.reply(reply_token, user_id, message) if reply_token else self.push(user_id, message)
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

    def reply(self, reply_token: str, user_id: str, text: str) -> bool:
        """先用回覆權杖；失敗（例如逾 1 分鐘）改用 push。"""
        if self._reply_messages(reply_token, [TextMessage(text=text[:LINE_TEXT_LIMIT])]):
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
