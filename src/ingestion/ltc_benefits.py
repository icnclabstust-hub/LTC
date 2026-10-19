"""把 ltc_benefit_data 的給付結構化資料寫入 PostgreSQL catalog（真相來源，2026-10-19 ltc-kb-graph 5.1 節）。

以主鍵 upsert；資料檔中已不存在的列設 valid_to（不刪除），Neo4j 同步時對應為 EXPIRE／SOFT_DELETE。
"""

from datetime import date

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import (
    LtcBenefitCategory,
    LtcCopayRate,
    LtcEligibleGroup,
    LtcIdentityClass,
    LtcQuota,
    LtcServiceItem,
    LtcTransportZone,
)
from app.db.session import SessionLocal
from ingestion import ltc_benefit_data as data
from ingestion.common import get_logger

logger = get_logger("ltc_benefits")

# 依外鍵相依順序寫入
_TABLES = (
    (LtcBenefitCategory, "code", data.BENEFIT_CATEGORIES),
    (LtcIdentityClass, "class_no", data.IDENTITY_CLASSES),
    (LtcQuota, "quota_key", data.QUOTAS),
    (LtcCopayRate, "rate_key", data.COPAY_RATES),
    (LtcTransportZone, "zone_key", data.TRANSPORT_ZONES),
    (LtcServiceItem, "code", data.SERVICE_ITEMS),
    (LtcEligibleGroup, "group_key", data.ELIGIBLE_GROUPS),
)


async def run() -> bool:
    today = date.today()
    async with SessionLocal() as session:
        for model, key, rows in _TABLES:
            for row in rows:
                stmt = pg_insert(model).values(**row, valid_to=None)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[getattr(model, key)],
                    set_={**{c: getattr(stmt.excluded, c) for c in row if c != key},
                          "valid_to": None, "updated_at": stmt.excluded.updated_at},
                )
                await session.execute(stmt)
            current = [row[key] for row in rows]
            expired = await session.execute(
                update(model)
                .where(getattr(model, key).not_in(current), model.valid_to.is_(None))
                .values(valid_to=today)
            )
            logger.info(f"{model.__tablename__}：{len(rows)} 筆現行，{expired.rowcount} 筆設為到期")
        await session.commit()
    return True


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
