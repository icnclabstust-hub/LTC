"""拒答分流規則對照測試題庫 eval/golden_queries.csv（v0.4，2026-10-19）。

緊急關鍵字由 FE 在呼叫 BE 前處理（frontend/tests/xiaozhao/test_emergency.py），這裡不測。
"""

import csv
from pathlib import Path

import pytest

from app.core.pii import mask_pii
from app.core.scope import has_domain_term, is_ambiguous, is_sensitive

_GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden_queries.csv"
_FE_HANDLED_CATEGORIES = {"緊急關鍵字", "緊急關鍵字（不該觸發）", "喚醒詞與指令", "督導使用"}
# 其他分類中屬於緊急關鍵字的題目（台語「跋倒」），同樣由 FE 攔下，不會送到 BE
_FE_EMERGENCY_IDS = {"Q148"}


def _rows() -> list[dict]:
    with _GOLDEN.open(encoding="utf-8-sig") as f:
        return [
            r
            for r in csv.DictReader(f)
            if r["category"] not in _FE_HANDLED_CATEGORIES and r["question_id"] not in _FE_EMERGENCY_IDS
        ]


def _ids(rows: list[dict]) -> list[str]:
    return [r["question_id"] for r in rows]


_SENSITIVE_ROWS = [r for r in _rows() if r["expected_refusal_reason"] == "sensitive"]
_ANSWER_ROWS = [r for r in _rows() if r["expected_behavior"] == "answer"]
_CLARIFY_ROWS = [r for r in _rows() if r["expected_behavior"] == "clarify"]
_OUT_OF_SCOPE_ROWS = [r for r in _rows() if r["expected_refusal_reason"] == "out_of_scope"]
_KB_MISSING_ROWS = [r for r in _rows() if r["expected_refusal_reason"] == "kb_missing"]

# 規則式判斷的已知漏判：題庫保留，等 P3 sufficient-context judge 再處理
_KNOWN_MISSES = {
    "Q177": "多意圖（交通接送＋喘息＋金額）要追問，規則只看短句",
    "Q129": "「翻成英文：謝謝你的照顧」引號內的「照顧」被當成領域詞",
}


def _known_miss(row: dict):
    reason = _KNOWN_MISSES.get(row["question_id"])
    return pytest.param(row, marks=pytest.mark.xfail(reason=reason, strict=True)) if reason else row


@pytest.mark.parametrize("row", _SENSITIVE_ROWS, ids=_ids(_SENSITIVE_ROWS))
def test_sensitive_rows_are_flagged(row):
    assert is_sensitive(row["query"])


@pytest.mark.parametrize("row", _ANSWER_ROWS, ids=_ids(_ANSWER_ROWS))
def test_answerable_rows_are_not_refused_by_rules(row):
    assert not is_sensitive(row["query"])
    assert not is_ambiguous(row["query"])


@pytest.mark.parametrize("row", [_known_miss(r) for r in _CLARIFY_ROWS], ids=_ids(_CLARIFY_ROWS))
def test_clarify_rows_are_ambiguous(row):
    assert is_ambiguous(row["query"])


@pytest.mark.parametrize("row", [_known_miss(r) for r in _OUT_OF_SCOPE_ROWS], ids=_ids(_OUT_OF_SCOPE_ROWS))
def test_out_of_scope_rows_have_no_domain_term(row):
    assert not has_domain_term(row["query"])


@pytest.mark.parametrize("row", _KB_MISSING_ROWS, ids=_ids(_KB_MISSING_ROWS))
def test_kb_missing_rows_have_domain_term(row):
    assert has_domain_term(row["query"])


def test_errand_for_medicine_is_not_sensitive():
    assert not is_sensitive("可以幫個案代購東西或代領藥嗎？")
    assert not is_sensitive("我可以幫長輩代購藥物嗎?")
    assert is_sensitive("我好像給個案吃錯藥了")


def test_mask_pii_replaces_national_id_and_phones():
    masked = mask_pii("個案 A123456789 的家屬電話 0912-345-678，機構 05-2778785，請撥 1966")
    assert masked == "個案 [身分證] 的家屬電話 [電話]，機構 [電話]，請撥 1966"
    assert mask_pii(masked) == masked
