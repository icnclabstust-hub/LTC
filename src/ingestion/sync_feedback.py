"""從 Open WebUI 的 feedback 匯出 API 拉取照護員回饋，寫入 eval.feedback_sync
（見回饋迴圈設計文件第 5 節）。該匯出端點僅限管理員權限，需在 .env 設定
OPEN_WEBUI_API_KEY（Open WebUI 後台 Settings > Account > API Keys 產生，
須用管理員帳號）。"""

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.models import FeedbackSync
from app.db.session import SessionLocal
from ingestion.common import get_logger

logger = get_logger("sync_feedback")


async def fetch_feedbacks() -> list[dict]:
    if not settings.open_webui_api_key:
        raise RuntimeError("尚未設定 OPEN_WEBUI_API_KEY，無法呼叫 Open WebUI 匯出 API")

    async with httpx.AsyncClient(base_url=settings.open_webui_base_url, timeout=30.0) as client:
        response = await client.get(
            "/api/v1/evaluations/feedbacks/all/export",
            headers={"Authorization": f"Bearer {settings.open_webui_api_key}"},
        )
        response.raise_for_status()
        return response.json()


async def run() -> int:
    feedbacks = await fetch_feedbacks()
    logger.info(f"抓到 {len(feedbacks)} 筆 Open WebUI 回饋")

    async with SessionLocal() as session:
        for item in feedbacks:
            data = item.get("data") or {}
            meta = item.get("meta") or {}
            stmt = pg_insert(FeedbackSync).values(
                feedback_id=item["id"],
                rating=str(data.get("rating")) if data.get("rating") is not None else None,
                reason=data.get("reason"),
                comment=data.get("comment"),
                chat_id=meta.get("chat_id"),
                message_id=meta.get("message_id"),
                raw=item,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[FeedbackSync.feedback_id],
                set_={
                    "rating": stmt.excluded.rating,
                    "reason": stmt.excluded.reason,
                    "comment": stmt.excluded.comment,
                    "raw": stmt.excluded.raw,
                    "synced_at": stmt.excluded.synced_at,
                },
            )
            await session.execute(stmt)
        await session.commit()

    logger.info("回饋資料已同步至 PostgreSQL")
    return len(feedbacks)


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
