from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cached_result, set_cached_result
from app.core.evidence import judge_evidence
from app.core.faithfulness import check_faithfulness
from app.core.generation import generate_answer
from app.core.intent import classify_intent
from app.core.models import ESCALATE_REASONS, Chunk, EvidenceVerdict, Intent, RefusalReason
from app.core.pii import mask_pii
from app.core.rerank import Reranker
from app.core.retrieval import ChunkRetriever
from app.core.scope import has_domain_term, is_ambiguous, is_sensitive
from app.db.queries import extract_cities, is_in_service_area, query_ltc_points

_OUT_OF_SERVICE_AREA_MESSAGE = "您查詢的地區不在本服務範圍（目前僅服務嘉義市、嘉義縣），請洽 1966 長照專線。"
_IDENTITY_ANSWER ="我是小照，長照法規與 SOP 問答助理，可以協助您查詢長照相關法規、照護操作流程與長照據點。"
_ANSWERED_DISCLAIMER = "請以引用來源之政府公告原文為準，本回覆僅供參考。"

# 拒答文字不寫「已轉督導」：轉不轉由呼叫端依 escalate 決定（Open WebUI 的使用者本身就是督導）
_REFUSAL_MESSAGES = {
    RefusalReason.KB_MISSING: "查無具法源效力之資料，請洽 1966 長照專線或查詢原始公告，避免以本回覆做為依據。",
    RefusalReason.OUT_OF_SCOPE: "這個問題不在長照服務範圍內；如果需要督導協助，請輸入「轉交給督導」。",
    RefusalReason.LOW_CONFIDENCE: "檢索到的資料無法確認能回答這個問題，小照不提供推測的答案，請以原文出處自行核對。",
    RefusalReason.AMBIGUOUS: (
        "想再確認一下：您問的是哪一項服務（例如喘息、交通接送、輔具、居家服務）？"
        "若和補助或自付額有關，也請告訴我個案的身分別（一般戶、中低收入戶、低收入戶）。"
    ),
    RefusalReason.SENSITIVE: (
        "這個問題涉及醫療、用藥或個案安全，須由督導或醫護人員判斷，小照不提供建議。若個案有立即危險，請撥 119。"
    ),
}

_retriever = ChunkRetriever()
_reranker = Reranker()


@dataclass
class QueryResult:
    answer: str
    sources: list[Chunk] = field(default_factory=list)
    confidence: str = "low"
    disclaimer: str = ""
    status: str = "answered"  # answered | refused
    refusal_reason: RefusalReason | None = None
    escalate: bool = False


def _refused(reason: RefusalReason, sources: list[Chunk] | None = None) -> QueryResult:
    return QueryResult(
        answer=_REFUSAL_MESSAGES[reason],
        sources=sources or [],
        confidence="low",
        status="refused",
        refusal_reason=reason,
        escalate=reason in ESCALATE_REASONS,
    )


async def run_query_pipeline(query_text: str, session: AsyncSession) -> QueryResult:
    """/v1/query 與 OpenAI 相容端點共用的 RAG 主流程，避免兩支端點各自維護一份邏輯。

    判斷由便宜到昂貴（設計文件第 5.1 節）；只有「回答」會寫入快取，拒答不寫，
    否則督導補上知識後，同樣的問題在快取期限內仍會被拒答。
    """
    query_text = mask_pii(query_text)
    intent = classify_intent(query_text)

    if intent == Intent.IDENTITY:
        return QueryResult(answer=_IDENTITY_ANSWER, sources=[], confidence="high", disclaimer="")

    if is_sensitive(query_text):
        return _refused(RefusalReason.SENSITIVE)

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

    if is_ambiguous(query_text):
        return _refused(RefusalReason.AMBIGUOUS)

    cached = await get_cached_result(query_text)
    if cached is not None:
        return cached

    chunks = _retriever.search(query_text)
    top_chunks = _reranker.rerank(query_text, chunks)
    verdict = judge_evidence(top_chunks)

    if verdict == EvidenceVerdict.INSUFFICIENT:
        reason = RefusalReason.KB_MISSING if has_domain_term(query_text) else RefusalReason.OUT_OF_SCOPE
        return _refused(reason)

    # 模糊區依需求決議第 7 題「不確定一律轉督導定案」，不再生成「僅供參考」的答案；
    # 附上檢索到的原文，讓督導判斷
    if verdict == EvidenceVerdict.AMBIGUOUS:
        return _refused(RefusalReason.LOW_CONFIDENCE, top_chunks)

    draft_answer = await generate_answer(query_text, top_chunks)
    faithfulness = check_faithfulness(draft_answer, top_chunks)

    # 原本未通過時把整段 chunk（最長數千字）當答案丟回；改為拒答，原文改由 citations 提供
    if not faithfulness.passed:
        return _refused(RefusalReason.LOW_CONFIDENCE, top_chunks)

    result = QueryResult(answer=draft_answer, sources=top_chunks, confidence="high", disclaimer=_ANSWERED_DISCLAIMER)
    await set_cached_result(query_text, result)
    return result
