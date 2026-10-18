import sys
import uuid
from datetime import date

import pytest

sys.path.insert(0, "src")

from app.core.cache import get_cached_result, set_cached_result
from app.core.models import Chunk
from app.core.pipeline import QueryResult

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _unique_query() -> str:
    return f"cache-test-query-{uuid.uuid4().hex}"


async def test_miss_then_hit_roundtrip():
    query = _unique_query()

    assert await get_cached_result(query) is None

    result = QueryResult(
        answer="測試答案",
        sources=[
            Chunk(
                chunk_id="c1",
                doc_id="d1",
                content="測試內容",
                law_name="測試法",
                article="1",
                source_url="https://example.com",
                updated_at=date(2026, 1, 1),
                similarity=0.9,
            )
        ],
        confidence="high",
        disclaimer="測試免責聲明",
    )
    await set_cached_result(query, result)

    cached = await get_cached_result(query)
    assert cached is not None
    assert cached.answer == "測試答案"
    assert cached.confidence == "high"
    assert len(cached.sources) == 1
    assert cached.sources[0].law_name == "測試法"
    assert cached.sources[0].updated_at == date(2026, 1, 1)


async def test_miss_for_unknown_query():
    assert await get_cached_result(_unique_query()) is None
