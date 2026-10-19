"""長照結構化資料直接回答（2026-10-19 ltc-kb-graph 第 5.3 節）。

補助額度、部分負擔、交通分區、服務價格、申請資格、據點查詢：答案就是資料本身，不經 LLM，附出處。
給付規則（L1～L3，約兩百筆）整份讀成快照再以純函式組答案；據點（L4）每次查詢。
資料來源優先 Neo4j，連不上時改查 PostgreSQL catalog（真相來源），服務不中斷。
解析不出明確問法時回傳 None，交回原本的 RAG 路徑。
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date

from neo4j import AsyncGraphDatabase
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.models import Chunk
from app.db.models import (
    LtcBenefitCategory,
    LtcCopayRate,
    LtcEligibleGroup,
    LtcIdentityClass,
    LtcPoint,
    LtcQuota,
    LtcServiceItem,
    LtcTransportZone,
)
from app.db.queries import SERVICE_CITIES, extract_cities

logger = logging.getLogger(__name__)

_SNAPSHOT_TTL_SECONDS = 600
_MAX_PROVIDERS_LISTED = 8
_PERIOD = {"month": "每月", "3year": "每三年", "year": "每年"}
_CLASS_SHORT = {1: "第一類（低收入戶、中低收入戶）", 2: "第二類", 3: "第三類（一般戶）"}

# ---------- 問句解析 ----------

_CATEGORY_PATTERNS = (
    ("respite", re.compile(r"喘息|喘吸")),
    ("transport", re.compile(r"交通接送|接送|交通")),
    ("assistive", re.compile(r"輔具|輔劇|無障礙|居家環境改善")),
    ("care_pro", re.compile(r"照顧及專業|照顧服務|專業服務|居家服務|日照|日間照顧")),
)
_FOUR_PACKS = re.compile(r"四包|四大給付")
_COPAY = re.compile(r"自付|部分負擔|自己要付|自己付|付幾成|自己出|家己出|要付多少比例|要自己付錢")
_QUOTA = re.compile(r"額度|補助多少|補多少|補偌濟|最多多少|可以用多少|用幾天|多少錢|補助多少錢|會當補")
_PER_TRIP = re.compile(r"一趟|每趟|一改")
_ZONE = re.compile(r"第幾區|分區|哪一區")
_ITEMS = re.compile(r"哪幾種|有哪些|哪些方式|方式|項目")
_ITEM_PRICE = re.compile(r"價格|多少錢")
# 問「多少錢」但帶這些字眼時是在問額度，不是問單項服務價格
_QUOTA_HINT = re.compile(r"補助|額度|每個月|每月|一年|三年|最多|可以用|補")
_ELIGIBILITY = re.compile(r"幾歲|誰可以申請|哪些人可以|申請資格|服務對象|一定要\s*\d+\s*歲|\d+\s*歲以上才")
_LEVEL = re.compile(r"第?\s*([2-8二三四五六七八])\s*級")
_LEVEL_DIGITS = {"二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
_ITEM_CODE = re.compile(r"\b([A-Z]{2}\d{2}[a-z]?)\b")
# 只認「找地方」的問法；「可以申請喘息服務嗎」是資格問題，不能列出單位
_PROVIDER_ASK = re.compile(r"據點|哪裡|附近|有沒有|有哪些|單位|哪家")
_SERVICE_TYPES = (
    (re.compile(r"日照|日間照顧"), ("日間照顧服務",)),
    (re.compile(r"沐浴車"), ("到宅沐浴車服務",)),
    (re.compile(r"巷弄長照站|C\s*據點"), ("巷弄長照站",)),
    (re.compile(r"家庭托顧"), ("家庭托顧服務",)),
    (re.compile(r"小規模多機能"), ("小規模多機能服務",)),
    (re.compile(r"團體家屋"), ("團體家屋服務",)),
    (re.compile(r"送餐|營養餐飲"), ("營養餐飲服務",)),
    (re.compile(r"交通接送|接送"), ("交通接送服務", "社區式交通接送服務")),
    (re.compile(r"輔具"), ("輔具及居家無障礙環境改善服務",)),
    (re.compile(r"居家服務"), ("居家服務",)),
    (re.compile(r"喘息"), ("喘息服務",)),
    (re.compile(r"個案管理|A\s*單位"), ("個案管理服務",)),
)


def detect_category(text: str) -> str | None:
    for code, pattern in _CATEGORY_PATTERNS:
        if pattern.search(text):
            return code
    return None


def detect_level(text: str) -> int | None:
    m = _LEVEL.search(text)
    if m is None:
        return None
    return int(m.group(1)) if m.group(1).isdigit() else _LEVEL_DIGITS[m.group(1)]


def detect_identity_class(text: str) -> int | None:
    # 「中低收入老人生活津貼」依第一款／第二款分屬第一、二類，無法從問句判斷，不猜
    if "津貼" in text:
        return None
    if re.search(r"低收入戶|中低收入戶|低收|中低收", text):
        return 1
    if re.search(r"身心障礙者生活補助|身障生活補助", text):
        return 2
    if re.search(r"一般戶", text):
        return 3
    return None


def detect_service_types(text: str) -> tuple[str, ...]:
    for pattern, types in _SERVICE_TYPES:
        if pattern.search(text):
            return types
    return ()


# ---------- 資料快照 ----------


@dataclass
class BenefitSnapshot:
    categories: dict[str, dict] = field(default_factory=dict)
    quotas: list[dict] = field(default_factory=list)
    copays: list[dict] = field(default_factory=list)
    classes: dict[int, dict] = field(default_factory=dict)
    groups: list[dict] = field(default_factory=list)
    items: dict[str, dict] = field(default_factory=dict)
    zones: dict[tuple[str, str | None], dict] = field(default_factory=dict)
    towns: dict[str, list[str]] = field(default_factory=dict)
    source: str = ""


@dataclass
class StructuredAnswer:
    answer: str
    sources: list[Chunk]


def _cite(ref: str, url: str, lines: list[str], updated: date | None) -> Chunk:
    return Chunk(chunk_id=f"ltc:{ref}", doc_id=f"ltc:{ref}", content="\n".join(lines), law_name=ref,
                 article=None, source_url=url, updated_at=updated or date.today(), similarity=1.0)


def _money(amount: int) -> str:
    return f"{amount:,} 元"


def _today() -> date:
    return date.today()


def _effective(rows: list[dict]) -> list[dict]:
    """尚未施行的列（如 115-07-01 起的輔具第二組）保留，但標示施行日。"""
    return sorted(rows, key=lambda r: (r.get("need_level") or 0, r.get("zone") or 0, r.get("item_group") or 0))


def _from_note(row: dict) -> str:
    starts = row.get("valid_from")
    if isinstance(starts, date) and starts > _today():
        return f"（自 {starts.year - 1911} 年 {starts.month} 月 {starts.day} 日起實施）"
    return ""


def answer_categories(snap: BenefitSnapshot) -> StructuredAnswer:
    cats = list(snap.categories.values())
    lines = [f"{i + 1}. {c['name']}（{_PERIOD[c['period']]}給付，適用 {c['service_code_prefixes']} 碼）"
             for i, c in enumerate(cats)]
    text = "長照「四包錢」是長照服務的四大給付：\n" + "\n".join(lines) + \
           "\n四項額度依長照需要等級核定，彼此不得互相流用。"
    ref = cats[0]
    return StructuredAnswer(text, [_cite(ref["source_ref"], ref["source_url"], lines, ref.get("valid_from"))])


def answer_quota(snap: BenefitSnapshot, category: str, level: int | None, text: str) -> StructuredAnswer:
    cat = snap.categories[category]
    rows = _effective([q for q in snap.quotas if q["category"] == category])
    period = _PERIOD[cat["period"]]
    if category == "transport":
        by_zone = {q["zone"]: q["amount"] for q in rows}
        lines = [f"第{z}區：{period} {_money(a)}" for z, a in sorted(by_zone.items())]
        cities = extract_cities(text)
        local = [f"{c}屬第{snap.zones[(c, None)]['zone']}區" for c in cities if (c, None) in snap.zones]
        remote = [t for t in snap.towns.get("嘉義縣", []) if snap.zones.get(("嘉義縣", t), {}).get("zone") == 4]
        note = "交通接送額度依居住地分區（各等級相同）：" + "；".join(lines) + "。"
        if local:
            note += "\n" + "、".join(local) + (f"（嘉義縣{'、'.join(remote)}屬第四區）" if remote and "嘉義縣" in cities else "") + "。"
        if _PER_TRIP.search(text):
            item = snap.items.get("DA01")
            if item and item.get("note"):
                note += f"\n每趟補助金額：{item['note']}。"
        body = note
    elif category == "assistive":
        by_group = {q["item_group"]: q for q in rows}
        lines = [f"第{g}組：{period} {_money(q['amount'])}{_from_note(q)}" for g, q in sorted(by_group.items())]
        body = "輔具及居家無障礙環境改善服務額度（各等級相同），第一組與第二組擇一申請：" + "；".join(lines) + "。"
    else:
        if level is not None:
            row = next(q for q in rows if q["need_level"] == level)
            lines = [f"第{level}級：{period} {_money(row['amount'])}"]
            body = f"長照需要等級第{level}級的{cat['name']}額度為{period} {_money(row['amount'])}。"
        else:
            lines, grouped = [], {}
            for q in rows:
                grouped.setdefault(q["amount"], []).append(q["need_level"])
            for amount, levels in grouped.items():
                span = f"第{levels[0]}級" if len(levels) == 1 else f"第{levels[0]}～{levels[-1]}級"
                lines.append(f"{span}：{period} {_money(amount)}")
            body = f"{cat['name']}額度依長照需要等級：" + "；".join(lines) + "。"
    body += "\n實際等級與額度由照管中心評估核定；超過額度的部分須自行負擔。"
    ref = rows[0]
    return StructuredAnswer(body, [_cite(ref["source_ref"], ref["source_url"], lines, ref.get("valid_from"))])


def answer_copay(snap: BenefitSnapshot, category: str | None, identity: int | None) -> StructuredAnswer:
    cats = [category] if category else list(snap.categories)
    lines = []
    for code in cats:
        name = snap.categories[code]["name"]
        rows = [r for r in snap.copays if r["category"] == code and (identity is None or r["class_no"] == identity)]
        if code == "transport":
            for cls in sorted({r["class_no"] for r in rows}):
                parts = "、".join(f"第{r['zone']}區 {r['rate_pct']}%" for r in sorted(rows, key=lambda r: r["zone"])
                                 if r["class_no"] == cls)
                lines.append(f"{name}｜{_CLASS_SHORT[cls]}：{parts}")
        else:
            for r in sorted(rows, key=lambda r: r["class_no"]):
                lines.append(f"{name}｜{_CLASS_SHORT[r['class_no']]}：{r['rate_pct']}%")
    head = f"{_CLASS_SHORT[identity]}的部分負擔比率" if identity else "長照服務的部分負擔比率"
    body = head + "（在給付額度內，依長照身分別自付）：\n" + "\n".join(lines)
    if identity == 1:
        body += "\n第一類在給付額度內不必自付；"
    else:
        body += "\n"
    body += "超過核定額度的部分須全額自行負擔。"
    if identity is None:
        body += "\n長照身分別：" + "；".join(f"{c['name']}：{c['definition']}" for c in snap.classes.values())
    ref = next(r for r in snap.copays if r["category"] == cats[0])
    sources = [_cite(ref["source_ref"], ref["source_url"], lines, ref.get("valid_from"))]
    if identity is None and snap.classes:
        c = next(iter(snap.classes.values()))
        sources.append(_cite(c["source_ref"], c["source_url"],
                             [f"{x['name']}：{x['definition']}" for x in snap.classes.values()], c.get("valid_from")))
    return StructuredAnswer(body, sources)


def answer_items(snap: BenefitSnapshot, category: str) -> StructuredAnswer | None:
    items = [i for i in snap.items.values() if i.get("category") == category and i.get("price")]
    if category == "care_pro":
        items = [i for i in items if i["code"].startswith("C")]
    if not items:
        return None
    lines = [f"{i['code']} {i['name']}：{_money(i['price'])}／{i['unit']}" for i in items]
    notes = sorted({i["note"] for i in items if i.get("note")})
    title = "喘息服務的服務方式與支付價格" if category == "respite" else "專業服務項目與給付標準"
    body = f"{title}：\n" + "\n".join(lines) + ("\n" + "；".join(notes) if notes else "") + \
           "\n實際使用由個管員依照顧計畫安排，費用自給付額度扣除並依身分別部分負擔。"
    ref = items[0]
    return StructuredAnswer(body, [_cite(ref["source_ref"], ref["source_url"], lines, ref.get("valid_from"))])


def answer_item_code(snap: BenefitSnapshot, code: str) -> StructuredAnswer | None:
    item = snap.items.get(code)
    if item is None:
        return None
    price = f"{_money(item['price'])}／{item['unit']}" if item.get("price") else (item.get("note") or "")
    line = f"{item['code']} {item['name']}：{price}"
    body = line + (f"（{item['note']}）" if item.get("note") and item.get("price") else "")
    return StructuredAnswer(body, [_cite(item["source_ref"], item["source_url"], [line], item.get("valid_from"))])


def answer_eligibility(snap: BenefitSnapshot) -> StructuredAnswer:
    lines = [g["name"] + _from_note(g) for g in snap.groups]
    body = "經縣市長期照顧管理中心評估，長照需要等級 2 級以上，並符合下列之一，即可申請長照服務：\n" + \
           "\n".join(f"・{l}" for l in lines) + "\n申請方式：撥打 1966 長照專線、聯絡當地照管中心，或住院期間洽出院準備服務小組。"
    ref = snap.groups[0]
    return StructuredAnswer(body, [_cite(ref["source_ref"], ref["source_url"], lines, ref.get("valid_from"))])


def answer_benefit_question(text: str, snap: BenefitSnapshot) -> StructuredAnswer | None:
    """給付規則類問題（純函式，可單元測試）；無法確定問法時回傳 None。"""
    if not snap.categories:
        return None
    if _FOUR_PACKS.search(text):
        return answer_categories(snap)
    if _ELIGIBILITY.search(text) and snap.groups:
        return answer_eligibility(snap)
    code = _ITEM_CODE.search(text.upper())
    if code and code.group(1) in {c.upper() for c in snap.items}:
        return answer_item_code(snap, next(c for c in snap.items if c.upper() == code.group(1)))
    category = detect_category(text)
    if _COPAY.search(text):
        return answer_copay(snap, category, detect_identity_class(text))
    if category is None:
        return None
    if category == "transport" and _ZONE.search(text):
        return answer_quota(snap, "transport", None, text)
    asks_items = _ITEMS.search(text) or (_ITEM_PRICE.search(text) and not _QUOTA_HINT.search(text))
    if category in ("respite", "care_pro") and asks_items:
        items = answer_items(snap, category)
        if items:
            return items
    if _QUOTA.search(text):
        return answer_quota(snap, category, detect_level(text), text)
    return None


# ---------- 資料讀取（Neo4j 優先、PostgreSQL 降級） ----------

_snapshot: BenefitSnapshot | None = None
_snapshot_at = 0.0
_driver = None
_warned = False


def _neo4j():
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    return _driver


def _as_date(value) -> date | None:
    return value.to_native() if hasattr(value, "to_native") else value


async def _snapshot_from_neo4j() -> BenefitSnapshot:
    snap = BenefitSnapshot(source="neo4j")
    async with _neo4j().session() as s:
        async def rows(query: str) -> list[dict]:
            return [r.data() for r in await (await s.run(query)).fetch(10_000)]

        for r in await rows("MATCH (c:BenefitCategory) WHERE c.valid_to IS NULL RETURN c {.*} AS c"):
            c = r["c"]
            snap.categories[c["key"]] = {**c, "valid_from": _as_date(c.get("valid_from"))}
        for r in await rows("MATCH (l:NeedLevel)-[q:QUOTA]->(c:BenefitCategory) WHERE q.valid_to IS NULL "
                            "RETURN c.key AS category, l.level AS need_level, q {.*} AS q"):
            snap.quotas.append({**r["q"], "category": r["category"], "need_level": r["need_level"],
                                "valid_from": _as_date(r["q"].get("valid_from"))})
        for r in await rows("MATCH (i:IdentityClass)-[e:COPAY]->(c:BenefitCategory) WHERE e.valid_to IS NULL "
                            "RETURN c.key AS category, i.class_no AS class_no, e {.*} AS e"):
            snap.copays.append({**r["e"], "category": r["category"], "class_no": r["class_no"],
                                "valid_from": _as_date(r["e"].get("valid_from"))})
        for r in await rows("MATCH (i:IdentityClass) WHERE i.valid_to IS NULL RETURN i {.*} AS i ORDER BY i.class_no"):
            snap.classes[r["i"]["class_no"]] = {**r["i"], "valid_from": _as_date(r["i"].get("valid_from"))}
        for r in await rows("MATCH (g:EligibleGroup)-[e:ELIGIBLE_FOR]->(:Program) WHERE e.valid_to IS NULL "
                            "RETURN g {.*} AS g ORDER BY g.valid_from, g.key"):
            snap.groups.append({**r["g"], "valid_from": _as_date(r["g"].get("valid_from"))})
        for r in await rows("MATCH (i:ServiceItem) WHERE i.valid_to IS NULL "
                            "OPTIONAL MATCH (c:BenefitCategory)-[:HAS_ITEM]->(i) RETURN i {.*} AS i, c.key AS category"):
            snap.items[r["i"]["code"]] = {**r["i"], "category": r["category"],
                                          "valid_from": _as_date(r["i"].get("valid_from"))}
        for r in await rows("MATCH (n)-[e:TRANSPORT_ZONE]->(z:TransportZone) WHERE e.valid_to IS NULL "
                            "RETURN coalesce(n.county, n.name) AS county, CASE WHEN n:Town THEN n.name END AS town, "
                            "z.zone AS zone, e.source_url AS source_url, e.source_ref AS source_ref"):
            snap.zones[(r["county"], r["town"])] = r
        for r in await rows("MATCH (t:Town)-[:IN_COUNTY]->(c:County) WHERE c.key IN ['嘉義市', '嘉義縣'] "
                            "RETURN c.key AS county, collect(DISTINCT t.name) AS towns"):
            snap.towns[r["county"]] = sorted(r["towns"])
    return snap


async def _snapshot_from_pg(session: AsyncSession) -> BenefitSnapshot:
    snap = BenefitSnapshot(source="postgresql")

    async def current(model):
        return list((await session.execute(select(model).where(model.valid_to.is_(None)))).scalars())

    def sourced(row) -> dict:
        return {"source_url": row.source_url, "source_ref": row.source_ref, "valid_from": row.valid_from}

    for c in await current(LtcBenefitCategory):
        snap.categories[c.code] = {"key": c.code, "name": c.name, "period": c.period,
                                   "service_code_prefixes": c.service_code_prefixes, **sourced(c)}
    snap.quotas = [{"category": q.category, "need_level": q.need_level, "zone": q.zone, "item_group": q.item_group,
                    "amount": q.amount, "period": q.period, **sourced(q)} for q in await current(LtcQuota)]
    snap.copays = [{"category": r.category, "class_no": r.identity_class, "zone": r.zone, "rate_pct": r.rate_pct,
                    **sourced(r)} for r in await current(LtcCopayRate)]
    snap.classes = {c.class_no: {"class_no": c.class_no, "name": c.name, "definition": c.definition, **sourced(c)}
                    for c in sorted(await current(LtcIdentityClass), key=lambda c: c.class_no)}
    snap.groups = [{"key": g.group_key, "name": g.name, **sourced(g)}
                   for g in sorted(await current(LtcEligibleGroup), key=lambda g: (g.valid_from, g.group_key))]
    snap.items = {i.code: {"code": i.code, "name": i.name, "category": i.category, "price": i.price, "unit": i.unit,
                           "note": i.note, **sourced(i)} for i in await current(LtcServiceItem)}
    snap.zones = {(z.county, z.town): {"zone": z.zone, **sourced(z)} for z in await current(LtcTransportZone)}
    towns = await session.execute(select(LtcPoint.address, LtcPoint.town_name).where(
        LtcPoint.town_name.is_not(None), or_(*(LtcPoint.address.contains(c) for c in SERVICE_CITIES))).distinct())
    for address, town in towns:
        snap.towns.setdefault(address.replace("台", "臺")[:3], [])
        if town not in snap.towns[address.replace("台", "臺")[:3]]:
            snap.towns[address.replace("台", "臺")[:3]].append(town)
    return snap


def _warn_once(exc: Exception) -> None:
    global _warned
    if not _warned:
        logger.warning("Neo4j 無法使用，結構化查詢改查 PostgreSQL：%s", exc)
        _warned = True


async def get_snapshot(session: AsyncSession) -> BenefitSnapshot:
    global _snapshot, _snapshot_at
    if _snapshot is not None and time.monotonic() - _snapshot_at < _SNAPSHOT_TTL_SECONDS:
        return _snapshot
    try:
        snap = await _snapshot_from_neo4j()
        if not snap.categories:
            raise RuntimeError("Neo4j 尚未同步長照資料")
    except Exception as exc:  # noqa: BLE001 — Neo4j 任何錯誤都降級，不影響回答
        _warn_once(exc)
        snap = await _snapshot_from_pg(session)
    _snapshot, _snapshot_at = snap, time.monotonic()
    return snap


# ---------- 據點（L4） ----------


def detect_town(text: str, snap: BenefitSnapshot, cities: tuple[str, ...]) -> tuple[str, str] | None:
    normalized = text.replace("台", "臺")
    for county in cities:
        for town in snap.towns.get(county, []):
            stem = town[:-1] if len(town) > 2 else town
            if stem in normalized:
                return county, town
    return None


async def _providers_neo4j(types, counties, town) -> tuple[int, list[dict]]:
    query = (
        "MATCH (p:Provider)-[:PROVIDES]->(s:ServiceType) WHERE s.name IN $types AND p.valid_to IS NULL "
        + ("MATCH (p)-[r:LOCATED_IN|SERVES_AREA]->(t:Town {key: $town}) " if town else
           "MATCH (p)-[r:LOCATED_IN]->(t:Town)-[:IN_COUNTY]->(c:County) WHERE c.key IN $counties ")
        # 位於該鄉鎮的單位排前面，其次才是服務範圍涵蓋該鄉鎮的外地單位
        + "WITH p, max(CASE type(r) WHEN 'LOCATED_IN' THEN 1 ELSE 0 END) AS local "
          "RETURN p.name AS name, p.phone AS phone, p.address AS address ORDER BY local DESC, p.address"
    )
    async with _neo4j().session() as s:
        rows = [r.data() for r in await (await s.run(query, types=list(types), counties=list(counties),
                                                      town=f"{town[0]}{town[1]}" if town else None)).fetch(10_000)]
    unique = list({r["name"]: r for r in rows}.values())
    return len(unique), unique[:_MAX_PROVIDERS_LISTED]


async def _providers_pg(session, types, counties, town) -> tuple[int, list[dict]]:
    stmt = select(LtcPoint).where(LtcPoint.service_names.overlap(list(types)),
                                  or_(*(LtcPoint.address.contains(c) for c in counties)))
    points = list((await session.execute(stmt)).scalars())
    if town:
        codes = {p.town_code for p in points if p.town_name == town[1] and p.address.startswith(town[0])}
        points = [p for p in points if (p.town_name == town[1] and p.address.replace("台", "臺").startswith(town[0]))
                  or codes & set(p.service_area_codes or ())]
    # 位於該鄉鎮的單位排前面，其次才是服務範圍涵蓋該鄉鎮的外地單位
    points.sort(key=lambda p: (not (town and p.town_name == town[1]), p.address))
    return len(points), [{"name": p.name, "phone": p.phone, "address": p.address}
                         for p in points[:_MAX_PROVIDERS_LISTED]]


async def answer_providers(text: str, session: AsyncSession, snap: BenefitSnapshot) -> StructuredAnswer | None:
    types = detect_service_types(text)
    if not types or not _PROVIDER_ASK.search(text):
        return None
    cities = extract_cities(text)
    if not all(c in SERVICE_CITIES for c in cities):
        return None  # 服務範圍外交回原流程（回覆僅服務嘉義縣市）
    town = detect_town(text, snap, cities)
    try:
        total, rows = await _providers_neo4j(types, cities, town)
    except Exception as exc:  # noqa: BLE001
        _warn_once(exc)
        total, rows = await _providers_pg(session, types, cities, town)
    where = f"{town[0]}{town[1]}（含服務範圍涵蓋該地區的單位）" if town else "、".join(cities)
    if total == 0:
        body = f"查無{where}提供「{types[0]}」的特約單位，建議洽 1966 長照專線或個管員。"
        lines = [body]
    else:
        lines = [f"{p['name']}｜{p['phone'] or '—'}｜{p['address']}" for p in rows]
        body = f"{where}提供「{'、'.join(types)}」的長照特約單位共 {total} 家" + \
               (f"，列出前 {len(rows)} 家" if total > len(rows) else "") + "：\n" + \
               "\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines)) + "\n實際能否接案請先電話確認，或洽個管員安排。"
    return StructuredAnswer(body, [_cite("衛福部長照ABC據點資料（每日同步）", settings.ltc_points_csv_url, lines, None)])


async def answer(text: str, session: AsyncSession) -> StructuredAnswer | None:
    """pipeline 的結構化分支入口；回傳 None 表示交回 RAG。"""
    if not settings.ltc_graph_enabled:
        return None
    snap = await get_snapshot(session)
    return answer_benefit_question(text, snap) or await answer_providers(text, session, snap)
