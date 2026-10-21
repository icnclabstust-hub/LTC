"""PostgreSQL catalog 長照結構化資料 → Neo4j 分層圖譜（2026-10-19 ltc-kb-graph 第 5.2 節）。

Neo4j 只是訂閱者：每次全量比對（資料量小），以架構圖的五種原子操作落地——
ADD_ENTITY／UPDATE_ENTITY（MERGE＋SET）、ADD_RELATIONSHIP、EXPIRE_RELATIONSHIP、SOFT_DELETE；
本次沒出現的節點與關係設 valid_to，超過 30 天才實際刪除。不存法規條文，也不存使用者資料。
"""

from collections import Counter
from datetime import datetime

from neo4j import AsyncGraphDatabase
from sqlalchemy import select

from app.config import settings
from app.db.models import (
    LtcBenefitCategory,
    LtcCopayRate,
    LtcEligibleGroup,
    LtcIdentityClass,
    LtcPoint,
    LtcQuota,
    LtcServiceItem,
    LtcTransportZone,
    SyncCursor,
)
from app.db.session import SessionLocal
from ingestion.common import get_logger

logger = get_logger("graph_sync")

PROGRAM_KEY = "ltc_benefit"
PURGE_AFTER = "P30D"
_BATCH = 2000
LABELS = ("Program", "BenefitCategory", "ServiceItem", "NeedLevel", "IdentityClass", "EligibleGroup",
          "County", "Town", "TransportZone", "Provider", "ServiceType")


def _sourced(row) -> dict:
    return {"source_url": row.source_url, "source_ref": row.source_ref, "valid_from": row.valid_from}


def build_graph(categories, quotas, classes, copays, zones, items, groups, points) -> tuple[dict, list]:
    """回傳（{label: [ {key, props} ]}, [ (type, from_label, from_key, to_label, to_key, key, props) ]）。"""
    nodes: dict[str, dict[str, dict]] = {label: {} for label in LABELS}
    rels: list[tuple] = []

    def node(label: str, key: str, **props) -> str:
        nodes[label].setdefault(key, {}).update({k: v for k, v in props.items() if v is not None})
        return key

    # L1 制度層
    node("Program", PROGRAM_KEY, name="長照服務給付", law="長期照顧服務申請及給付辦法")
    for c in categories:
        node("BenefitCategory", c.code, name=c.name, service_code_prefixes=c.service_code_prefixes, period=c.period,
             **_sourced(c))
        rels.append(("HAS_CATEGORY", "Program", PROGRAM_KEY, "BenefitCategory", c.code, c.code, {}))
    for g in groups:
        node("EligibleGroup", g.group_key, name=g.name, **_sourced(g))
        rels.append(("ELIGIBLE_FOR", "EligibleGroup", g.group_key, "Program", PROGRAM_KEY, g.group_key, _sourced(g)))
    # L2 服務層
    for i in items:
        node("ServiceItem", i.code, code=i.code, name=i.name, price=i.price, unit=i.unit, note=i.note, **_sourced(i))
        if i.category:
            rels.append(("HAS_ITEM", "BenefitCategory", i.category, "ServiceItem", i.code, i.code, {}))
    # L3 資格給付層
    for q in quotas:
        level = node("NeedLevel", f"L{q.need_level}", level=q.need_level, name=f"長照需要等級第{q.need_level}級")
        rels.append(("QUOTA", "NeedLevel", level, "BenefitCategory", q.category, q.quota_key,
                     {"amount": q.amount, "period": q.period, "zone": q.zone, "item_group": q.item_group,
                      **_sourced(q)}))
    for ic in classes:
        node("IdentityClass", f"C{ic.class_no}", class_no=ic.class_no, name=ic.name, definition=ic.definition,
             **_sourced(ic))
    for r in copays:
        rels.append(("COPAY", "IdentityClass", f"C{r.identity_class}", "BenefitCategory", r.category, r.rate_key,
                     {"rate_pct": r.rate_pct, "zone": r.zone, **_sourced(r)}))
    # L4 資源地理層
    for z in zones:
        node("TransportZone", f"Z{z.zone}", zone=z.zone, name=f"交通接送第{z.zone}區")
        node("County", z.county, name=z.county)
        if z.town:
            town = node("Town", f"{z.county}{z.town}", name=z.town, county=z.county)
            rels.append(("IN_COUNTY", "Town", town, "County", z.county, town, {}))
            rels.append(("TRANSPORT_ZONE", "Town", town, "TransportZone", f"Z{z.zone}", z.zone_key, _sourced(z)))
        else:
            rels.append(("TRANSPORT_ZONE", "County", z.county, "TransportZone", f"Z{z.zone}", z.zone_key, _sourced(z)))

    town_by_code: dict[str, str] = {}
    votes: dict[str, Counter] = {}
    for p in points:
        county = p.address.replace("台", "臺")[:3]
        if p.town_code and p.town_name:
            votes.setdefault(p.town_code, Counter())[(county, p.town_name)] += 1
    for code, counter in votes.items():
        (county, town), _ = counter.most_common(1)[0]
        key = node("Town", f"{county}{town}", name=town, county=county, code=code)
        node("County", county, name=county, code=code[:5])
        rels.append(("IN_COUNTY", "Town", key, "County", county, key, {}))
        town_by_code[code] = key

    for p in points:
        node("Provider", p.institution_code, code=p.institution_code, name=p.name, phone=p.phone, address=p.address,
             abc=p.abc_level, category_code=p.category)
        if p.town_code in town_by_code:
            rels.append(("LOCATED_IN", "Provider", p.institution_code, "Town", town_by_code[p.town_code],
                         p.institution_code, {}))
        for area in p.service_area_codes or ():
            if area in town_by_code:
                rels.append(("SERVES_AREA", "Provider", p.institution_code, "Town", town_by_code[area],
                             f"{p.institution_code}:{area}", {}))
        for name in p.service_names or ():
            node("ServiceType", name, name=name)
            rels.append(("PROVIDES", "Provider", p.institution_code, "ServiceType", name,
                         f"{p.institution_code}:{name}", {}))

    return {label: [{"key": k, "props": v} for k, v in items_.items()] for label, items_ in nodes.items()}, rels


async def _load_catalog():
    async with SessionLocal() as session:
        async def current(model):
            return list((await session.execute(select(model).where(model.valid_to.is_(None)))).scalars())

        return (
            await current(LtcBenefitCategory), await current(LtcQuota), await current(LtcIdentityClass),
            await current(LtcCopayRate), await current(LtcTransportZone), await current(LtcServiceItem),
            await current(LtcEligibleGroup), list((await session.execute(select(LtcPoint))).scalars()),
        )


async def _batched(session, query: str, rows: list, **params) -> None:
    for start in range(0, len(rows), _BATCH):
        await session.run(query, rows=rows[start:start + _BATCH], **params)


async def run() -> bool:
    nodes, rels = build_graph(*await _load_catalog())
    version = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    stats: dict[str, int] = {}
    driver = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password),
                                       notifications_disabled_categories=["UNRECOGNIZED"])
    try:
        async with driver.session() as session:
            for label in LABELS:
                await session.run(f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS "
                                  f"FOR (n:{label}) REQUIRE n.key IS UNIQUE")
            # ADD_ENTITY／UPDATE_ENTITY
            for label, rows in nodes.items():
                await _batched(session, f"UNWIND $rows AS r MERGE (n:{label} {{key: r.key}}) "
                                        "SET n += r.props, n.synced_version = $v, n.valid_to = null", rows, v=version)
                stats[f"node:{label}"] = len(rows)
            # ADD_RELATIONSHIP（依類型與兩端標籤分批）
            groups: dict[tuple, list] = {}
            for rel_type, from_label, from_key, to_label, to_key, key, props in rels:
                groups.setdefault((rel_type, from_label, to_label), []).append(
                    {"from": from_key, "to": to_key, "key": key, "props": {k: v for k, v in props.items() if v is not None}})
            for (rel_type, from_label, to_label), rows in groups.items():
                await _batched(session, f"UNWIND $rows AS r MATCH (a:{from_label} {{key: r.from}}) "
                                        f"MATCH (b:{to_label} {{key: r.to}}) MERGE (a)-[e:{rel_type} {{key: r.key}}]->(b) "
                                        "SET e += r.props, e.synced_version = $v, e.valid_to = null", rows, v=version)
                stats[f"rel:{rel_type}"] = stats.get(f"rel:{rel_type}", 0) + len(rows)
            # EXPIRE_RELATIONSHIP、SOFT_DELETE：本次沒出現的設 valid_to
            expired = await (await session.run(
                "MATCH ()-[e]->() WHERE e.synced_version IS NOT NULL AND e.synced_version <> $v AND e.valid_to IS NULL "
                "SET e.valid_to = datetime() RETURN count(e) AS n", v=version)).single()
            deleted = await (await session.run(
                "MATCH (n) WHERE n.synced_version IS NOT NULL AND n.synced_version <> $v AND n.valid_to IS NULL "
                "SET n.valid_to = datetime() RETURN count(n) AS n", v=version)).single()
            # 軟到期超過 30 天才實際刪除
            await session.run(f"MATCH ()-[e]->() WHERE e.valid_to < datetime() - duration('{PURGE_AFTER}') DELETE e")
            await session.run(f"MATCH (n) WHERE n.valid_to < datetime() - duration('{PURGE_AFTER}') DETACH DELETE n")
            stats["expired_relationships"], stats["soft_deleted_nodes"] = expired["n"], deleted["n"]
    finally:
        await driver.close()

    async with SessionLocal() as db:
        cursor = await db.get(SyncCursor, "neo4j")
        if cursor is None:
            db.add(SyncCursor(subscriber="neo4j", last_synced_at=datetime.utcnow(), last_version=version, detail=stats))
        else:
            cursor.last_synced_at, cursor.last_version, cursor.detail = datetime.utcnow(), version, stats
        await db.commit()
    logger.info(f"Neo4j 同步完成（版本 {version}）：{stats}")
    return True


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
