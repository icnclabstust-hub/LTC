"""長照 ABC 據點：CSV 全量下載後整批 upsert 到 catalog.ltc_points，供查詢直接讀
PostgreSQL（不經 ChromaDB/RAG，見資料擷取層設計文件範圍）。"""

import csv
import io

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.models import LtcPoint
from app.db.session import SessionLocal
from ingestion.common import get_logger

logger = get_logger("ltc_points_crawler")


async def run() -> bool:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(settings.ltc_points_csv_url)
        response.raise_for_status()
        text = response.content.decode("utf-8-sig")

    rows = list(csv.DictReader(io.StringIO(text)))
    logger.info(f"抓到 {len(rows)} 筆長照據點資料")

    async with SessionLocal() as session:
        for row in rows:
            stmt = pg_insert(LtcPoint).values(
                institution_code=row.get("機構代碼", ""),
                name=row.get("機構名稱", ""),
                category=row.get("機構種類", ""),
                address=row.get("地址全址", ""),
                phone=row.get("機構電話"),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[LtcPoint.institution_code],
                set_={
                    "name": stmt.excluded.name,
                    "category": stmt.excluded.category,
                    "address": stmt.excluded.address,
                    "phone": stmt.excluded.phone,
                    "synced_at": stmt.excluded.synced_at,
                },
            )
            await session.execute(stmt)
        await session.commit()

    logger.info("長照據點資料已同步至 PostgreSQL")
    return True


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
