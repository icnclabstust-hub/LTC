import time
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.citations import build_citations, format_answer_with_quotes
from app.core.pii import mask_pii
from app.core.pipeline import QueryResult, run_query_pipeline
from app.db.queries import log_conversation
from app.db.session import get_session

router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    stream: bool = False


def _format_answer_with_sources(result: QueryResult) -> str:
    """OpenAI Chat Completions 格式沒有標準的來源引用欄位，改把原文引用附在回答文字末尾
    （與 LINE 路徑共用 citations 模組，督導在 Open WebUI 看到的引用與照護員一致）。"""
    return format_answer_with_quotes(result.answer, build_citations(result.sources), result.disclaimer)


_AGENT_MODEL_ID = "小照"


@router.get("/v1/models")
async def list_models() -> dict:
    return {
        "object": "list",
        "data": [{"id": _AGENT_MODEL_ID, "object": "model", "owned_by": "ltc-be"}],
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, session: AsyncSession = Depends(get_session)) -> dict:
    user_messages = [m for m in request.messages if m.role == "user"]
    # 寫入 biz.messages 前遮罩，與 /v1/query 一致
    query_text = mask_pii(user_messages[-1].content if user_messages else "")

    result: QueryResult = await run_query_pipeline(query_text, session)

    await log_conversation(
        session,
        query_text=query_text,
        answer_text=result.answer,
        cited_chunk_ids=[chunk.chunk_id for chunk in result.sources],
    )

    content = _format_answer_with_sources(result)

    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model or _AGENT_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
