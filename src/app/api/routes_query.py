from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Chunk
from app.core.pipeline import QueryResult, run_query_pipeline
from app.db.queries import log_conversation
from app.db.session import get_session

router = APIRouter(prefix="/api")


class QueryRequest(BaseModel):
    query: str
    session_id: str


class SourceRef(BaseModel):
    law_name: str
    article: str | None
    updated_at: str
    source_url: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceRef]
    confidence: str
    disclaimer: str


@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest, session: AsyncSession = Depends(get_session)) -> QueryResponse:
    result: QueryResult = await run_query_pipeline(request.query, session)

    await log_conversation(
        session,
        query_text=request.query,
        answer_text=result.answer,
        cited_chunk_ids=[chunk.chunk_id for chunk in result.sources],
    )

    return QueryResponse(
        answer=result.answer,
        sources=_to_source_refs(result.sources),
        confidence=result.confidence,
        disclaimer=result.disclaimer,
    )


def _to_source_refs(chunks: list[Chunk]) -> list[SourceRef]:
    return [
        SourceRef(
            law_name=chunk.law_name,
            article=chunk.article,
            updated_at=chunk.updated_at.isoformat(),
            source_url=chunk.source_url,
        )
        for chunk in chunks
    ]
