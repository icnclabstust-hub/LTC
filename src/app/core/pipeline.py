from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cached_result, set_cached_result
from app.core.evidence import judge_evidence
from app.core.faithfulness import check_faithfulness
from app.core.generation import generate_answer
from app.core.intent import classify_intent
from app.core.models import Chunk, EvidenceVerdict, Intent
from app.core.rerank import Reranker
from app.core.retrieval import ChunkRetriever
from app.db.queries import extract_cities, is_in_service_area, query_ltc_points

_NO_EVIDENCE_MESSAGE = "查無具法源效力之資料，請洽 1966 長照專線或查詢原始公告，避免以本回覆做為依據。"
_FAITHFULNESS_FAILED_MESSAGE = "系統無法確認生成內容與來源一致，僅提供檢索到的原文出處，請自行核對。"
_AMBIGUOUS_MESSAGE = "檢索到的資料相關性較低，以下回覆僅供參考，請務必自行核對原始法規／SOP 公告。"
_OUT_OF_SERVICE_AREA_MESSAGE = "您查詢的地區不在本服務範圍（目前僅服務嘉義市、嘉義縣），請洽 1966 長照專線。"
_IDENTITY_ANSWER ="我是小照，長照法規與 SOP 問答助理，可以協助您查詢長照相關法規、照護操作流程與長照據點。"

_retriever = ChunkRetriever()
_reranker = Reranker()


@dataclass
class QueryResult:
    answer: str
    sources: list[Chunk] = field(default_factory=list)
    confidence: str = "low"
    disclaimer: str = ""


async def run_query_pipeline(query_text: str, session: AsyncSession) -> QueryResult:
    """/api/query 與 OpenAI 相容端點共用的 RAG 主流程，避免兩支端點各自維護一份邏輯。"""
    intent = classify_intent(query_text)

    if intent == Intent.IDENTITY:
        return QueryResult(answer=_IDENTITY_ANSWER, sources=[], confidence="high", disclaimer="")

    if intent == Intent.LTC_POINTS:
        cities = extract_cities(query_text)
        if not is_in_service_area(cities):
            return QueryResult(answer=_OUT_OF_SERVICE_AREA_MESSAGE, sources=[], confidence="high", disclaimer="")
        points = await query_ltc_points(session, city=None, district=None, cities=cities)
        summary = "、".join(p.name for p in points[:5]) or "查無符合條件之長照據點"
        return QueryResult(
            answer=summary,
            sources=[],
            confidence="high",
            disclaimer="資料來源：衛福部長照 ABC 據點資料集，每日排程同步。",
        )

    cached = await get_cached_result(query_text)
    if cached is not None:
        return cached

    chunks = _retriever.search(query_text)
    top_chunks = _reranker.rerank(query_text, chunks)
    verdict = judge_evidence(top_chunks)

    if verdict == EvidenceVerdict.INSUFFICIENT:
        result = QueryResult(answer=_NO_EVIDENCE_MESSAGE, sources=[], confidence="low", disclaimer=_NO_EVIDENCE_MESSAGE)
        await set_cached_result(query_text, result)
        return result

    draft_answer = await generate_answer(query_text, top_chunks)
    faithfulness = check_faithfulness(draft_answer, top_chunks)

    if not faithfulness.passed:
        excerpt = "\n\n".join(chunk.content for chunk in top_chunks)
        result = QueryResult(
            answer=excerpt,
            sources=top_chunks,
            confidence="medium",
            disclaimer=_FAITHFULNESS_FAILED_MESSAGE,
        )
        await set_cached_result(query_text, result)
        return result

    if verdict == EvidenceVerdict.AMBIGUOUS:
        result = QueryResult(answer=draft_answer, sources=top_chunks, confidence="medium", disclaimer=_AMBIGUOUS_MESSAGE)
    else:
        result = QueryResult(
            answer=draft_answer,
            sources=top_chunks,
            confidence="high",
            disclaimer="請以引用來源之政府公告原文為準，本回覆僅供參考。",
        )
    await set_cached_result(query_text, result)
    return result
