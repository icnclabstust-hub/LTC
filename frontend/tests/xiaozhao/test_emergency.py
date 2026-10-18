"""緊急關鍵字對照測試題庫 eval/golden_queries.csv（v0.4）；免喚醒詞（2026-10-19 需求決議）。"""

import csv
from pathlib import Path

import pytest

from xiaozhao import emergency
from xiaozhao.wake_word import strip_wake_word

GOLDEN = Path(__file__).resolve().parents[3] / "eval" / "golden_queries.csv"


def _rows(predicate) -> list[dict]:
    with GOLDEN.open(encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if predicate(r)]


# 「緊急關鍵字」分類 18 題，加上台語分類的「阿伯跋倒矣」
EMERGENCY_ROWS = _rows(lambda r: r["category"] == "緊急關鍵字" or r["question_id"] == "Q148")
NOT_EMERGENCY_ROWS = _rows(lambda r: r["category"] == "緊急關鍵字（不該觸發）")
# 其他所有文字題（排除語音、多人場景的系統題）都不該觸發
TEXT_ROWS = _rows(lambda r: r not in EMERGENCY_ROWS and not r["query"].startswith("（"))


def _ids(rows):
    return [r["question_id"] for r in rows]


@pytest.mark.parametrize("row", EMERGENCY_ROWS, ids=_ids(EMERGENCY_ROWS))
def test_emergency_rows_trigger(row):
    assert emergency.detect(row["query"]) is not None


@pytest.mark.parametrize("row", NOT_EMERGENCY_ROWS, ids=_ids(NOT_EMERGENCY_ROWS))
def test_general_questions_about_emergencies_do_not_trigger(row):
    assert emergency.detect(row["query"]) is None


def test_no_other_text_row_triggers():
    triggered = [(r["question_id"], r["query"]) for r in TEXT_ROWS
                 if r not in NOT_EMERGENCY_ROWS and emergency.detect(r["query"]) is not None]
    assert triggered == []


def test_levels_and_upgrades():
    assert emergency.detect("個案叫不醒，好像沒有呼吸了").level == 1
    assert emergency.detect("個案跌倒了，站不起來").level == 2
    # 頭部撞擊加嗜睡升一級
    head = emergency.detect("個案跌倒撞到頭，現在一直想睡覺")
    assert (head.level, head.category) == (1, "跌倒撞到頭")


def test_low_blood_sugar_threshold():
    assert emergency.detect("個案量血糖只有 50，人在發抖冒汗").category == "疑似低血糖"
    assert emergency.detect("個案早上血糖 120 正常嗎") is None


def test_voice_is_never_downgraded():
    text = "預防跌倒有哪些方法"
    assert emergency.detect(text) is None
    assert emergency.detect(text, is_voice=True) is not None


def test_level1_not_downgraded_by_weak_time_words():
    # 「之前」只讓二級降級；一級寧可多回一次急救指引
    assert emergency.detect("個案噎到了，之前也噎過").level == 1
    assert emergency.detect("上週個案跌倒的事情") is None


def test_reply_only_claims_notified_when_true():
    match = emergency.detect("一進門就聞到很重的瓦斯味")
    assert "已通知督導" in emergency.reply_text(match, "05-2778785", True)
    text = emergency.reply_text(match, "05-2778785", False)
    assert "已通知督導" not in text and "05-2778785" in text and "119" in text


@pytest.mark.parametrize("raw,expected", [
    ("小照你好", ""),
    ("小照你好，喘息服務怎麼申請？", "喘息服務怎麼申請？"),
    ("小照 你好 請問輔具補助", "請問輔具補助"),
    ("小昭你好，我要問法規", "我要問法規"),
    ("小照妳好，跌倒怎麼辦", "跌倒怎麼辦"),
    ("你好小照，喘息服務是什麼？", "喘息服務是什麼？"),
])
def test_strip_wake_word_variants(raw, expected):
    assert strip_wake_word(raw) == (True, expected)


@pytest.mark.parametrize("text", ["請問喘息服務怎麼申請？", "小照是誰", "小照今天好可愛喔"])
def test_text_without_wake_word_is_kept(text):
    assert strip_wake_word(text) == (False, text)
