from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import XiaozhaoRequest


async def create_request(session: AsyncSession, **fields) -> tuple[bool, XiaozhaoRequest]:
    """以 request_id 冪等建立；FE 重送同一請求（網路重試）時回傳既有紀錄且 is_new=False，呼叫端不應重跑 RAG。

    用 INSERT ... ON CONFLICT DO NOTHING，兩個重送同時到達時也只會有一個建立成功。
    """
    now = datetime.utcnow()
    stmt = (
        insert(XiaozhaoRequest)
        .values(citations=[], escalate=False, created_at=now, updated_at=now, **fields)
        .on_conflict_do_nothing(index_elements=["request_id"])
        .returning(XiaozhaoRequest.request_id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()
    request = await session.get(XiaozhaoRequest, fields["request_id"], populate_existing=True)
    return inserted is not None, request


async def get_request(session: AsyncSession, request_id: str) -> XiaozhaoRequest | None:
    return await session.get(XiaozhaoRequest, request_id, populate_existing=True)


async def complete_request(session: AsyncSession, request: XiaozhaoRequest, **fields) -> None:
    now = datetime.utcnow()
    for key, value in fields.items():
        setattr(request, key, value)
    request.updated_at = now
    request.completed_at = now
    await session.commit()
