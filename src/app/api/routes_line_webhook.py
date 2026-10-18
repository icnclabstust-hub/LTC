import json
import logging
import re

from fastapi import APIRouter, BackgroundTasks, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.line_client import LineClient
from app.core.line_security import verify_line_signature
from app.core.pipeline import run_query_pipeline
from app.core.rate_limit import is_rate_limited
from app.core.wake_word import match_wake_word
from app.db.binding import find_identity, redeem_binding_code
from app.db.inbox_outbox import record_inbox_request
from app.db.queries import log_conversation
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()

_line_client = LineClient()
_BIND_PATTERN = re.compile(r"^綁定\s*(\d{6})$")
_UNBOUND_MESSAGE = "請向主管索取綁定碼，並傳送「綁定 123456」完成綁定。"
_NO_WAKE_WORD_REMINDER = "請以「小照你好」開頭，我才會回應您的問題喔。"
_PROCESSING_FAILED_MESSAGE = "系統暫時無法處理您的問題，請稍後再試一次。"
_BIND_RATE_LIMITED_MESSAGE = "綁定嘗試次數過多，請 10 分鐘後再試。"

# 綁定碼僅 6 位數，須限制每個 LINE 帳號的核銷嘗試次數，避免被暴力猜中
_BIND_ATTEMPT_LIMIT = 5
_BIND_ATTEMPT_WINDOW_SECONDS = 600


@router.post("/v1/line/webhook")
async def line_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not settings.line_channel_secret or not verify_line_signature(settings.line_channel_secret, body, signature):
        return Response(status_code=401)

    # LINE 建議非同步處理 webhook：先回 200，RAG 處理（實測 9–33 秒）改在回應送出後執行
    payload = json.loads(body)
    background_tasks.add_task(_handle_events, payload.get("events", []))
    return Response(status_code=200)


async def _handle_events(events: list[dict]) -> None:
    async with SessionLocal() as session:
        for event in events:
            try:
                await _handle_event(session, event)
            except Exception:
                logger.exception(f"LINE 事件處理失敗：webhookEventId={event.get('webhookEventId')}")
                await session.rollback()
                # 事件已記入 inbox，LINE 重送時會被去重略過；至少通知使用者重問，避免訊息無聲遺失
                target = _push_target(event.get("source", {}))
                if target:
                    await _line_client.push(target, _PROCESSING_FAILED_MESSAGE)


def _push_target(source: dict) -> str | None:
    """push 的對象：群組訊息回到群組，一對一回到使用者本人。"""
    return source.get("groupId") or source.get("roomId") or source.get("userId")


async def _send(reply_token: str, source: dict, text: str) -> None:
    """優先用 reply（不占推播額度）；reply token 逾時或失效時改用 push。"""
    if await _line_client.reply(reply_token, text):
        return
    target = _push_target(source)
    if target:
        await _line_client.push(target, text)


async def _handle_event(session: AsyncSession, event: dict) -> None:
    event_id = event.get("webhookEventId")
    if not event_id:
        return

    is_new, _ = await record_inbox_request(session, channel="line", external_event_id=event_id, payload=event)
    if not is_new:
        return

    if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
        return

    text = event["message"]["text"]
    reply_token = event.get("replyToken", "")
    source = event.get("source", {})
    source_type = source.get("type")
    external_user_id = source.get("userId")
    is_group = source_type in ("group", "room")

    if not external_user_id:
        return

    identity = await find_identity(session, channel="line", external_user_id=external_user_id)

    if identity is None:
        if is_group:
            return
        bind_match = _BIND_PATTERN.match(text.strip())
        if bind_match:
            if await is_rate_limited(
                f"line_bind:{external_user_id}", _BIND_ATTEMPT_LIMIT, _BIND_ATTEMPT_WINDOW_SECONDS
            ):
                await _send(reply_token, source, _BIND_RATE_LIMITED_MESSAGE)
                return
            result = await redeem_binding_code(session, bind_match.group(1), "line", external_user_id)
            message = "綁定成功！歡迎使用小照。" if result else "綁定碼無效或已過期，請向主管重新索取。"
            await _send(reply_token, source, message)
        else:
            await _send(reply_token, source, _UNBOUND_MESSAGE)
        return

    wake_matched, remainder = match_wake_word(text)
    if not wake_matched:
        if not is_group:
            await _send(reply_token, source, _NO_WAKE_WORD_REMINDER)
        return

    result = await run_query_pipeline(remainder, session)
    await log_conversation(
        session,
        query_text=remainder,
        answer_text=result.answer,
        cited_chunk_ids=[chunk.chunk_id for chunk in result.sources],
    )
    await _send(reply_token, source, result.answer)
