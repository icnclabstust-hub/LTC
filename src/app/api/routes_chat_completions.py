import time
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

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
    """OpenAI Chat Completions 格式沒有標準的來源引用欄位，改把來源附註在回答文字末尾
    （見回饋迴圈設計文件第 9 節未解決問題：之後若確認 Open WebUI 支援 citations
    附加欄位，可改用該格式）。"""
    if not result.sources:
        return result.answer

    lines = [result.answer, "", "---", "來源："]
    seen: set[str] = set()
    for chunk in result.sources:
        key = f"{chunk.law_name}-{chunk.article}"
        if key in seen:
            continue
        seen.add(key)
        article_part = f" 第{chunk.article}條" if chunk.article else ""
        lines.append(f"- {chunk.law_name}{article_part}（更新於 {chunk.updated_at.isoformat()}）：{chunk.source_url}")
    return "\n".join(lines)


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
    query_text = user_messages[-1].content if user_messages else ""

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
