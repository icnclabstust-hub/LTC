"""長照 ABC 據點：CSV 全量下載後整批 upsert 到 catalog.ltc_points，供查詢直接讀
PostgreSQL（不經 ChromaDB/RAG，見資料擷取層設計文件範圍）。

CSV 同一機構會依特約服務項目重複出現多列，先依機構代碼彙整服務項目與特約區域，
再寫入（2026-10-19 ltc-kb-graph：Neo4j 資源地理層用）。"""

import csv
import io
import re

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.models import LtcPoint
from app.db.session import SessionLocal
from ingestion.common import get_logger

logger = get_logger("ltc_points_crawler")

_COUNTY = re.compile(r"^(.{2}[縣市])")
_TOWN = re.compile(r"^(.{1,3}?[鄉鎮市區])")
_SERVICE_CODE = re.compile(r"\s*[（(][^）)]*[）)]\s*")


def split_town(address: str) -> str | None:
    """地址拆出鄉鎮市區（「嘉義縣太保市新埤里…」→「太保市」）。「新市區」這類名稱含「市」的，
    遇到緊接的「區」時往後延一字。"""
    address = address.replace("台", "臺").strip()
    county = _COUNTY.match(address)
    if county is None:
        return None
    rest = address[county.end():]
    town = _TOWN.match(rest)
    if town is None:
        return None
    name = town.group(1)
    if not name.endswith("區") and rest[len(name):len(name) + 1] == "區":
        name += "區"
    return name


def service_name(raw: str) -> str:
    """「日間照顧服務(BB)」→「日間照顧服務」：同一服務 CSV 有時帶碼別、有時不帶，統一成名稱。"""
    return _SERVICE_CODE.sub("", raw).strip()


def aggregate(rows: list[dict]) -> dict[str, dict]:
    points: dict[str, dict] = {}
    for row in rows:
        code = row.get("機構代碼", "").strip()
        if not code:
            continue
        point = points.setdefault(code, {
            "institution_code": code, "name": row.get("機構名稱", ""), "category": row.get("機構種類", ""),
            "address": row.get("地址全址", ""), "phone": row.get("機構電話"),
            "county_code": row.get("縣市") or None, "town_code": row.get("區") or None,
            "town_name": split_town(row.get("地址全址", "")), "abc_level": row.get("O_ABC") or None,
            "service_names": set(), "service_area_codes": set(),
        })
        for item in re.split(r"[;；,，、]", row.get("特約服務項目", "")):
            if item.strip():
                point["service_names"].add(service_name(item))
        for area in row.get("特約區域", "").split(";"):
            if area.strip():
                point["service_area_codes"].add(area.strip())
    for point in points.values():
        point["service_names"] = sorted(point["service_names"])
        point["service_area_codes"] = sorted(point["service_area_codes"])
    return points


async def run() -> bool:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(settings.ltc_points_csv_url)
        response.raise_for_status()
        text = response.content.decode("utf-8-sig")

    rows = list(csv.DictReader(io.StringIO(text)))
    points = aggregate(rows)
    logger.info(f"抓到 {len(rows)} 列、{len(points)} 個長照據點")

    async with SessionLocal() as session:
        for point in points.values():
            stmt = pg_insert(LtcPoint).values(**point)
            stmt = stmt.on_conflict_do_update(
                index_elements=[LtcPoint.institution_code],
                set_={**{c: getattr(stmt.excluded, c) for c in point if c != "institution_code"},
                      "synced_at": stmt.excluded.synced_at},
            )
            await session.execute(stmt)
        await session.commit()

    logger.info("長照據點資料已同步至 PostgreSQL")
    return True


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
