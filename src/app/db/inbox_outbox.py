from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InboxRequest, OutboxEvent


async def record_inbox_request(
    session: AsyncSession, channel: str, external_event_id: str, payload: dict
) -> tuple[bool, InboxRequest]:
    """記錄一筆對外通道請求；若 (channel, external_event_id) 已存在，回傳既有記錄且 is_new=False，
    呼叫端不應重新處理該事件。"""
    result = await session.execute(
        select(InboxRequest).where(
            InboxRequest.channel == channel, InboxRequest.external_event_id == external_event_id
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return False, existing

    request = InboxRequest(channel=channel, external_event_id=external_event_id, payload=payload)
    session.add(request)
    await session.commit()
    return True, request


async def enqueue_outbox_event(session: AsyncSession, event_type: str, target: str, payload: dict) -> OutboxEvent:
    event = OutboxEvent(event_type=event_type, target=target, payload=payload, status="pending")
    session.add(event)
    await session.commit()
    return event


async def mark_outbox_event_sent(session: AsyncSession, event: OutboxEvent) -> None:
    event.status = "sent"
    event.sent_at = datetime.utcnow()
    await session.commit()


async def mark_outbox_event_failed(session: AsyncSession, event: OutboxEvent) -> None:
    event.status = "failed"
    event.attempt_count += 1
    await session.commit()
