"""小照 FE（frontend/）的 API 合約 ①②③：/v1/query、/v1/scene、/v1/requests/{id}。

欄位依 FE `HttpBackend`（frontend/src/xiaozhao/backend.py）；見 2026-10-19 fe-be-integration 設計文件第 5.2 節。
RAG 實測 9–33 秒，所以 ① 先回 202，pipeline 在回應送出後執行，FE 以 ③ 輪詢。
"""

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_auth import require_xiaozhao_token
from app.core.citations import build_citations, format_answer_with_quotes
from app.core.pii import mask_pii
from app.core.pipeline import run_query_pipeline
from app.db.queries import log_conversation
from app.db.session import SessionLocal, get_session
from app.db.users import get_user
from app.db.xiaozhao_requests import complete_request, create_request, get_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", dependencies=[Depends(require_xiaozhao_token)])

_REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
# 不落地的欄位：照護員姓名改以 user_id／代號識別；逐字稿在 /v1/scene 是否收存尚未定案（串接說明第 4 節第 4 題），從嚴不存
_DROPPED_FIELDS = ("text", "caregiver_name", "utterances")


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str = Field(min_length=8, max_length=64, pattern=_REQUEST_ID_PATTERN)
    text: str = Field(min_length=1, max_length=2000)
    role: str = "caregiver"
    user_id: str | None = None


class SceneIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str = Field(min_length=8, max_length=64, pattern=_REQUEST_ID_PATTERN)
    role: str = "caregiver"
    user_id: str | None = None


def _channel(payload: dict) -> str:
    if payload.get("line_case_id") is not None:
        return "line"
    if payload.get("text_question_id") is not None:
        return "liff_text"
    if payload.get("turn_id") is not None:
        return "recording"
    return "api"


def _stored_payload(body: BaseModel) -> dict:
    payload = body.model_dump()
    return {k: v for k, v in payload.items() if k not in _DROPPED_FIELDS}


async def _resolve_user_id(session: AsyncSession, raw: str | None) -> uuid.UUID | None:
    """FE 送來的 user_id 須是 biz.users 成員；格式錯或查無此人時不擋請求（照樣回答），只記錄。"""
    if not raw:
        return None
    try:
        user_id = uuid.UUID(raw)
    except ValueError:
        logger.warning(f"小照請求的 user_id 格式錯誤：{raw!r}")
        return None
    if await get_user(session, user_id) is None:
        logger.warning(f"小照請求的 user_id 不存在：{user_id}")
        return None
    return user_id


async def process_query(request_id: str) -> None:
    """背景執行 RAG，結果寫回 biz.xiaozhao_requests；例外時標成 error，FE 會轉督導。"""
    async with SessionLocal() as session:
        request = await get_request(session, request_id)
        if request is None or request.status != "pending":
            return
        try:
            result = await run_query_pipeline(request.query_text or "", session)
            citations = build_citations(result.sources)
            disclaimer = result.disclaimer if result.status == "answered" else ""
            await complete_request(
                session,
                request,
                status=result.status,
                answer_full=format_answer_with_quotes(result.answer, citations, disclaimer),
                answer_spoken=result.answer,
                citations=citations,
                refusal_reason=result.refusal_reason.value if result.refusal_reason else None,
                escalate=result.escalate,
                confidence=result.confidence,
            )
            await log_conversation(
                session,
                query_text=request.query_text or "",
                answer_text=result.answer,
                cited_chunk_ids=[chunk.chunk_id for chunk in result.sources],
            )
        except Exception as exc:
            logger.exception(f"小照請求 {request_id} 處理失敗")
            await session.rollback()
            await complete_request(session, request, status="error", error=type(exc).__name__)


@router.post("/query", status_code=202)
async def submit_query(
    body: QueryIn, background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)
) -> dict:
    is_new, request = await create_request(
        session,
        request_id=body.request_id,
        kind="query",
        status="pending",
        channel=_channel(body.model_dump()),
        user_id=await _resolve_user_id(session, body.user_id),
        role=body.role,
        query_text=mask_pii(body.text),
        payload=_stored_payload(body),
    )
    if is_new:
        background_tasks.add_task(process_query, request.request_id)
    return {"request_id": request.request_id, "status": request.status}


@router.post("/scene", status_code=202)
async def submit_scene(body: SceneIn, session: AsyncSession = Depends(get_session)) -> dict:
    """一輪的統計紀錄；回應不當作回答（FE multi-intent-turn 第 4.3 節），FE 不輪詢。"""
    _, request = await create_request(
        session,
        request_id=body.request_id,
        kind="scene",
        status="recorded",
        channel="recording",
        user_id=await _resolve_user_id(session, body.user_id),
        role=body.role,
        query_text=None,
        payload=_stored_payload(body),
    )
    return {"request_id": request.request_id, "status": request.status}


@router.get("/requests/{request_id}")
async def read_request(request_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    request = await get_request(session, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="request not found")
    return {
        "request_id": request.request_id,
        "status": request.status,
        "answer_full": request.answer_full,
        "answer_spoken": request.answer_spoken,
        "citations": request.citations,
        "refusal_reason": request.refusal_reason,
        "escalate": request.escalate,
        "confidence": request.confidence,
        "help_request_id": request.help_request_id,
        "human_answer": request.human_answer,
    }
