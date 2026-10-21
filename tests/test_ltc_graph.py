"""長照結構化資料：附表數字完整性、問句解析與答案組裝（2026-10-19 ltc-kb-graph 第 7 節）。"""

from datetime import date
from types import SimpleNamespace

import pytest

from app.core import ltc_graph
from app.core.ltc_graph import BenefitSnapshot, answer_benefit_question
from ingestion import ltc_benefit_data as data
from ingestion.graph_sync import build_graph
from ingestion.ltc_points_crawler import aggregate, service_name, split_town


def _snapshot() -> BenefitSnapshot:
    snap = BenefitSnapshot(source="test")
    snap.categories = {c["code"]: {**c, "key": c["code"]} for c in data.BENEFIT_CATEGORIES}
    snap.quotas = [dict(q) for q in data.QUOTAS]
    snap.copays = [{**r, "class_no": r["identity_class"]} for r in data.COPAY_RATES]
    snap.classes = {c["class_no"]: dict(c) for c in data.IDENTITY_CLASSES}
    snap.groups = [dict(g) for g in data.ELIGIBLE_GROUPS]
    snap.items = {i["code"]: dict(i) for i in data.SERVICE_ITEMS}
    snap.zones = {(z["county"], z["town"]): dict(z) for z in data.TRANSPORT_ZONES}
    snap.towns = {"嘉義縣": ["民雄鄉", "阿里山鄉", "番路鄉", "大埔鄉", "新港鄉"], "嘉義市": ["東區", "西區"]}
    return snap


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(ltc_graph, "_today", lambda: date(2026, 3, 1))


# ---------- 附表數字（與全國法規資料庫附表二／三／五原文逐項比對） ----------

def test_annex2_quota_values():
    quota = {q["quota_key"]: q["amount"] for q in data.QUOTAS}
    assert [quota[f"care_pro:L{lv}"] for lv in range(2, 9)] == [10020, 15460, 18580, 24100, 28070, 32090, 36180]
    assert [quota[f"transport:L5:Z{z}"] for z in range(1, 5)] == [1680, 1840, 2000, 2400]
    assert quota["assistive:L2:G1"] == 40000 and quota["assistive:L8:G2"] == 60000
    assert [quota[f"respite:L{lv}"] for lv in range(2, 9)] == [32340] * 5 + [48510] * 2


def test_annex5_copay_values():
    rate = {r["rate_key"]: r["rate_pct"] for r in data.COPAY_RATES}
    assert [rate[f"care_pro:C{c}"] for c in (1, 2, 3)] == [0, 5, 16]
    assert [rate[f"respite:C{c}"] for c in (1, 2, 3)] == [0, 5, 16]
    assert [rate[f"assistive:C{c}"] for c in (1, 2, 3)] == [0, 10, 30]
    assert [rate[f"transport:C3:Z{z}"] for z in (1, 2, 3, 4)] == [30, 27, 25, 21]
    assert [rate[f"transport:C2:Z{z}"] for z in (1, 2, 3, 4)] == [10, 9, 8, 7]


def test_annex3_has_48_remote_towns_and_chiayi_zones():
    assert sum(len(t) for t in data.REMOTE_TOWNS.values()) == 48
    zones = {(z["county"], z["town"]): z["zone"] for z in data.TRANSPORT_ZONES}
    assert zones[("嘉義市", None)] == 1 and zones[("嘉義縣", None)] == 2 and zones[("嘉義縣", "阿里山鄉")] == 4


def test_every_row_has_source():
    for rows in (data.QUOTAS, data.COPAY_RATES, data.TRANSPORT_ZONES, data.SERVICE_ITEMS, data.ELIGIBLE_GROUPS,
                 data.IDENTITY_CLASSES, data.BENEFIT_CATEGORIES):
        for row in rows:
            assert row["source_url"].startswith("https://") and row["source_ref"] and row["valid_from"]


# ---------- 問句 → 答案 ----------

@pytest.mark.parametrize("question, expected", [
    ("長照四包錢是哪四包？", ["照顧及專業服務", "交通接送服務", "輔具及居家無障礙環境改善服務", "喘息服務"]),
    ("四包錢是啥", ["喘息服務"]),
    ("照顧及專業服務每個月可以補助多少錢？", ["第2級：每月 10,020 元", "第8級：每月 36,180 元"]),
    ("第3級的照顧服務額度是多少？", ["每月 15,460 元"]),
    ("喘息服務一年可以用多少額度？", ["第2～6級：每年 32,340 元", "第7～8級：每年 48,510 元"]),
    ("喘吸服務可以用幾天", ["32,340 元"]),
    ("輔具補助三年最多多少錢？", ["第1組：每三年 40,000 元", "第2組：每三年 60,000 元"]),
    ("輔具三冬會當補偌濟？", ["40,000 元"]),
    ("交通接送一趟補助多少錢？", ["地方主管機關訂定公告", "第1區：每月 1,680 元"]),
    ("嘉義縣的交通接送是第幾區？", ["嘉義縣屬第2區", "阿里山鄉"]),
    ("一般戶使用長照服務自己要付多少比例？", ["第三類（一般戶）", "16%", "30%"]),
    ("一般戶愛家己出偌濟？", ["第三類（一般戶）"]),
    ("中低收入戶的部分負擔是多少？", ["第一類", "不必自付"]),
    ("買輔具自己要付幾成？", ["輔具及居家無障礙環境改善服務｜第三類（一般戶）：30%"]),
    ("喘息服務有哪幾種方式？", ["GA09 居家喘息服務：770 元／2小時", "GA05 機構住宿式喘息服務"]),
    ("GA03 多少錢", ["GA03 日間照顧中心喘息服務-全日：1,250 元／全日"]),
    ("失智症的人要幾歲才能申請長照？", ["50歲以上失智症者", "49歲以下失智症者"]),
    ("一定要 65 歲以上才能申請長照對不對？", ["55歲以上原住民", "失能身心障礙者"]),
])
def test_structured_answers(question, expected):
    result = answer_benefit_question(question, _snapshot())
    assert result is not None, question
    for text in expected:
        assert text in result.answer, (question, result.answer)
    assert result.sources and all(s.source_url.startswith("https://") for s in result.sources)


@pytest.mark.parametrize("question", [
    "長照費用可以在報稅時扣除嗎？",
    "這個月補助沒用完可以留到下個月嗎？",
    "外籍看護放假的時候可以申請喘息服務嗎？",
    "居家無障礙改善可以做哪些項目？",
    "喘息服務一年只能用一次對嗎？",
    "長照服務法的主管機關是誰？",
])
def test_falls_back_to_rag_when_not_structured(question):
    assert answer_benefit_question(question, _snapshot()) is None


def test_future_rows_are_marked_with_start_date():
    result = answer_benefit_question("輔具補助三年最多多少錢？", _snapshot())
    assert "自 115 年 7 月 1 日起實施" in result.answer


def test_detect_town_and_service_types():
    snap = _snapshot()
    assert ltc_graph.detect_town("民雄附近有日照中心嗎", snap, ("嘉義市", "嘉義縣")) == ("嘉義縣", "民雄鄉")
    assert ltc_graph.detect_service_types("嘉義縣有沒有到宅沐浴車") == ("到宅沐浴車服務",)
    assert ltc_graph.detect_service_types("可以申請喘息服務嗎") == ("喘息服務",)
    assert not ltc_graph._PROVIDER_ASK.search("外籍看護放假的時候可以申請喘息服務嗎")


# ---------- 據點彙整與圖譜建構 ----------

def test_split_town_and_service_name():
    assert split_town("嘉義縣太保市新埤里新埤214號之12") == "太保市"
    assert split_town("台南市新市區中興街1號") == "新市區"
    assert split_town("嘉義市東區民族路1號") == "東區"
    assert service_name("日間照顧服務(BB)") == "日間照顧服務"


def test_aggregate_merges_duplicate_rows_per_institution():
    rows = [
        {"機構代碼": "X1", "機構名稱": "甲", "機構種類": "2", "地址全址": "嘉義縣民雄鄉中樂村1號", "機構電話": "05-1",
         "縣市": "10010", "區": "10010110", "O_ABC": "B", "特約服務項目": "日間照顧服務(BB)", "特約區域": "10010110;10010070"},
        {"機構代碼": "X1", "機構名稱": "甲", "機構種類": "2", "地址全址": "嘉義縣民雄鄉中樂村1號", "機構電話": "05-1",
         "縣市": "10010", "區": "10010110", "O_ABC": "B", "特約服務項目": "喘息服務", "特約區域": "10010110"},
    ]
    point = aggregate(rows)["X1"]
    assert point["service_names"] == ["喘息服務", "日間照顧服務"]
    assert point["service_area_codes"] == ["10010070", "10010110"]
    assert point["town_name"] == "民雄鄉"


def test_build_graph_layers():
    def ns(rows):
        return [SimpleNamespace(**{"valid_to": None, **r}) for r in rows]

    point = SimpleNamespace(institution_code="X1", name="甲", phone="05-1", address="嘉義縣民雄鄉中樂村1號",
                            abc_level="B", category="2", county_code="10010", town_code="10010110",
                            town_name="民雄鄉", service_names=["日間照顧服務"], service_area_codes=["10010110"])
    nodes, rels = build_graph(ns(data.BENEFIT_CATEGORIES), ns(data.QUOTAS), ns(data.IDENTITY_CLASSES),
                              ns(data.COPAY_RATES), ns(data.TRANSPORT_ZONES), ns(data.SERVICE_ITEMS),
                              ns(data.ELIGIBLE_GROUPS), [point])
    counts = {label: len(rows) for label, rows in nodes.items()}
    assert counts["BenefitCategory"] == 4 and counts["NeedLevel"] == 7 and counts["IdentityClass"] == 3
    assert counts["TransportZone"] == 4 and counts["Provider"] == 1 and counts["ServiceType"] == 1
    types = {r[0] for r in rels}
    assert {"HAS_CATEGORY", "HAS_ITEM", "QUOTA", "COPAY", "ELIGIBLE_FOR", "IN_COUNTY", "TRANSPORT_ZONE",
            "LOCATED_IN", "SERVES_AREA", "PROVIDES"} <= types
    assert sum(r[0] == "QUOTA" for r in rels) == len(data.QUOTAS)
