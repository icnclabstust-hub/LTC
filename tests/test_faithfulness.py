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
