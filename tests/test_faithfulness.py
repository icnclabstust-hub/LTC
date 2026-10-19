from datetime import date

from app.core.faithfulness import check_faithfulness
from app.core.models import Chunk

_CHUNK = Chunk(
    chunk_id="c1",
    doc_id="d1",
    content="長照服務申請人須年滿六十五歲，且經評估有照顧需求者，得依規定申請居家服務。",
    law_name="長期照顧服務法",
    article="8",
    source_url="https://law.moj.gov.tw/example",
    updated_at=date(2026, 1, 1),
)


def test_passes_when_sentence_closely_matches_source():
    result = check_faithfulness("長照服務申請人須年滿六十五歲，經評估有照顧需求者得申請居家服務。", [_CHUNK])
    assert result.passed


def test_fails_when_sentence_unrelated_to_source():
    result = check_faithfulness("今天天氣晴朗適合出遊。", [_CHUNK])
    assert not result.passed


def test_fails_when_no_cited_chunks():
    result = check_faithfulness("長照服務申請人須年滿六十五歲。", [])
    assert not result.passed


_CHUNK_QUOTA = Chunk(
    chunk_id="c2",
    doc_id="d2",
    content="照顧及專業服務額度及交通接送服務額度，按月給付；未滿一個月，按比率計算。喘息服務額度，每年給付一次。",
    law_name="長期照顧服務申請及給付辦法",
    article="12",
    source_url="https://law.moj.gov.tw/example2",
    updated_at=date(2026, 1, 1),
)


def test_passes_when_answer_combines_several_chunks():
    """一段回答綜合多個 chunk：逐句比對聯集，不再因為整段跟單一 chunk 重疊不足而被擋（2026-10-19 實測 28 題）。"""
    answer = (
        "長照服務申請人須年滿六十五歲，且經評估有照顧需求者，得申請居家服務。\n"
        "照顧及專業服務額度按月給付，未滿一個月按比率計算。\n"
        "喘息服務額度每年給付一次。"
    )
    assert check_faithfulness(answer, [_CHUNK, _CHUNK_QUOTA]).passed


def test_ignores_citation_and_referral_sentences():
    answer = (
        "照顧及專業服務額度按月給付，未滿一個月按比率計算。\n"
        "（長期照顧服務申請及給付辦法第12條，更新日期：2026-01-01）\n"
        "如有疑問，建議洽詢1966長照專線。"
    )
    result = check_faithfulness(answer, [_CHUNK_QUOTA])
    assert result.passed
    assert result.rule_score == 1.0


def test_fails_when_most_sentences_are_unsupported():
    answer = (
        "照顧及專業服務額度按月給付，未滿一個月按比率計算。\n"
        "使用者每次申請都可以獲得一萬元現金補貼。\n"
        "家屬可以直接到便利商店領取補助款項。"
    )
    assert not check_faithfulness(answer, [_CHUNK_QUOTA]).passed


def test_fails_when_answer_has_no_checkable_sentence():
    assert not check_faithfulness("請洽1966。", [_CHUNK_QUOTA]).passed
