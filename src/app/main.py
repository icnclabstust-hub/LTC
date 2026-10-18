import httpx
from fastapi import FastAPI
from sqlalchemy import text

from app.api.routes_chat_completions import router as chat_completions_router
from app.api.routes_line_webhook import router as line_webhook_router
from app.api.routes_ltc_points import router as ltc_points_router
from app.api.routes_query import router as query_router
from app.config import settings
from app.core.retrieval import ChunkRetriever
from app.db.session import SessionLocal

app = FastAPI(title="長照法規／SOP 問答 RAG 系統", version="0.1.0")
app.include_router(query_router)
app.include_router(ltc_points_router)
app.include_router(chat_completions_router)
app.include_router(line_webhook_router)


async def _check_postgres() -> bool:
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _check_chromadb() -> bool:
    try:
        ChunkRetriever()
        return True
    except Exception:
        return False


async def _check_ollama() -> bool:
    try:
        async with httpx.AsyncClient(base_url=settings.ollama_base_url, timeout=5.0) as client:
            response = await client.get("/api/tags")
            return response.status_code == 200
    except Exception:
        return False


@app.get("/health")
async def health() -> dict:
    dependencies = {
        "postgres": await _check_postgres(),
        "chromadb": _check_chromadb(),
        "ollama": await _check_ollama(),
    }
    return {"status": "ok" if all(dependencies.values()) else "degraded", "dependencies": dependencies}
