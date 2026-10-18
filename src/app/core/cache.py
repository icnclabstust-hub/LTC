import hashlib
import json
from datetime import date

import redis.asyncio as redis

from app.config import settings
from app.core.models import Chunk

_CACHE_TTL_SECONDS = 3600
# v2：2026-10-19 起只快取「回答」；改前綴讓舊版快取的拒答結果直接失效
_KEY_PREFIX = "query_cache:v2:"

_client = redis.from_url(settings.redis_url, decode_responses=True)


def _cache_key(query_text: str) -> str:
    digest = hashlib.sha256(query_text.strip().encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


def _serialize(result) -> str:
    return json.dumps(
        {
            "answer": result.answer,
            "confidence": result.confidence,
            "disclaimer": result.disclaimer,
            "sources": [
                {
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "content": c.content,
                    "law_name": c.law_name,
                    "article": c.article,
                    "source_url": c.source_url,
                    "updated_at": c.updated_at.isoformat(),
                    "similarity": c.similarity,
                }
                for c in result.sources
            ],
        }
    )


def _deserialize(data: str):
    from app.core.pipeline import QueryResult

    obj = json.loads(data)
    sources = [
        Chunk(
            chunk_id=s["chunk_id"],
            doc_id=s["doc_id"],
            content=s["content"],
            law_name=s["law_name"],
            article=s["article"],
            source_url=s["source_url"],
            updated_at=date.fromisoformat(s["updated_at"]),
            similarity=s["similarity"],
        )
        for s in obj["sources"]
    ]
    return QueryResult(answer=obj["answer"], sources=sources, confidence=obj["confidence"], disclaimer=obj["disclaimer"])


async def get_cached_result(query_text: str):
    try:
        data = await _client.get(_cache_key(query_text))
    except Exception:
        return None
    return _deserialize(data) if data else None


async def set_cached_result(query_text: str, result) -> None:
    try:
        await _client.set(_cache_key(query_text), _serialize(result), ex=_CACHE_TTL_SECONDS)
    except Exception:
        pass
