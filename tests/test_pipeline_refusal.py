"""pipeline 拒答分流（2026-10-19 fe-be-integration 設計文件第 5.1 節的判斷表）。

檢索、生成、快取都換成假物件，不需要 ChromaDB／Ollama／Redis。
"""

from datetime import date

import pytest

from app.core import pipeline
from app.core.models import Chunk, RefusalReason

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ARTICLE = "長期照顧服務法第三條：家庭照顧者指於家庭中對失能者提供規律性照顧之主要親屬或家人。"


def _chunk(similarity: float, content: str = _ARTICLE) -> Chunk:
    return Chunk(
        chunk_id="00000000-0000-0000-0000-000000000001",
        doc_id="d1",
        content=content,
        law_name="長期照顧服務法",
        article="3",
        source_url="https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0070040",
        updated_at=date(2026, 1, 1),
        similarity=similarity,
    )


class _FakeRetriever:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.queries: list[str] = []

    def search(self, query: str) -> list[Chunk]:
        self.queries.append(query)
        return self.chunks


class _PassThroughReranker:
    def rerank(self, query: str, chunks: list[Chunk]) -> list[Chunk]:
        return chunks


@pytest.fixture
def fake_rag(monkeypatch):
    cache: dict[str, object] = {}
    generated: list[str] = []
    state = {"retriever": _FakeRetriever([]), "answer": _ARTICLE}

    async def fake_get(query_text):
        return cache.get(query_text)

    async def fake_set(query_text, result):
        cache[query_text] = result

    async def fake_generate(query, chunks):
        generated.append(query)
        return state["answer"]

    def set_chunks(chunks):
        state["retriever"] = _FakeRetriever(chunks)
        monkeypatch.setattr(pipeline, "_retriever", state["retriever"])

    monkeypatch.setattr(pipeline, "get_cached_result", fake_get)
    monkeypatch.setattr(pipeline, "set_cached_result", fake_set)
    monkeypatch.setattr(pipeline, "generate_answer", fake_generate)
    monkeypatch.setattr(pipeline, "_reranker", _PassThroughReranker())
    set_chunks([])
    return {"cache": cache, "generated": generated, "set_chunks": set_chunks, "state": state}


async def test_sufficient_and_faithful_answer_is_answered_and_cached(fake_rag):
    fake_rag["set_chunks"]([_chunk(0.9)])
    result = await pipeline.run_query_pipeline("家庭照顧者的定義是什麼？", None)
    assert result.status == "answered"
    assert result.refusal_reason is None
    assert not result.escalate
    assert result.sources
    assert "家庭照顧者的定義是什麼？" in fake_rag["cache"]


async def test_sensitive_question_is_refused_without_retrieval(fake_rag):
    fake_rag["set_chunks"]([_chunk(0.9)])
    result = await pipeline.run_query_pipeline("個案昨天漏吃一次安眠藥，今天可以補吃嗎？", None)
    assert result.status == "refused"
    assert result.refusal_reason == RefusalReason.SENSITIVE
    assert result.escalate
    assert "119" in result.answer
    assert fake_rag["state"]["retriever"].queries == []
    assert fake_rag["cache"] == {}


async def test_ambiguous_question_asks_back_without_escalating(fake_rag):
    result = await pipeline.run_query_pipeline("他可以用嗎？", None)
    assert result.refusal_reason == RefusalReason.AMBIGUOUS
    assert not result.escalate


async def test_insufficient_evidence_with_domain_term_is_kb_missing(fake_rag):
    fake_rag["set_chunks"]([_chunk(0.2)])
    result = await pipeline.run_query_pipeline("居服員的制服要去哪裡領？", None)
    assert result.refusal_reason == RefusalReason.KB_MISSING
    assert result.escalate
    assert fake_rag["cache"] == {}


async def test_insufficient_evidence_without_domain_term_is_out_of_scope(fake_rag):
    fake_rag["set_chunks"]([_chunk(0.2)])
    result = await pipeline.run_query_pipeline("台積電股票現在可以買嗎？", None)
    assert result.refusal_reason == RefusalReason.OUT_OF_SCOPE
    assert not result.escalate
    assert "轉交給督導" in result.answer


async def test_borderline_evidence_is_low_confidence_with_sources_and_no_generation(fake_rag):
    fake_rag["set_chunks"]([_chunk(0.55)])
    result = await pipeline.run_query_pipeline("家庭照顧者的定義是什麼？", None)
    assert result.refusal_reason == RefusalReason.LOW_CONFIDENCE
    assert result.escalate
    assert result.sources
    assert fake_rag["generated"] == []
    assert fake_rag["cache"] == {}


async def test_unfaithful_answer_is_refused_instead_of_dumping_chunks(fake_rag):
    fake_rag["set_chunks"]([_chunk(0.9)])
    fake_rag["state"]["answer"] = "完全無關的內容，與任何條文都沒有重疊"
    result = await pipeline.run_query_pipeline("家庭照顧者的定義是什麼？", None)
    assert result.refusal_reason == RefusalReason.LOW_CONFIDENCE
    assert result.answer != _ARTICLE
    assert fake_rag["cache"] == {}


async def test_pii_is_masked_before_retrieval_and_generation(fake_rag):
    fake_rag["set_chunks"]([_chunk(0.9)])
    await pipeline.run_query_pipeline("家屬 0912345678 問家庭照顧者的定義是什麼？", None)
    assert fake_rag["state"]["retriever"].queries == ["家屬 [電話] 問家庭照顧者的定義是什麼？"]
    assert fake_rag["generated"] == ["家屬 [電話] 問家庭照顧者的定義是什麼？"]
