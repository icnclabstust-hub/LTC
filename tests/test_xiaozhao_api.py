"""/v1/query、/v1/scene、/v1/requests/{id}：依 FE HttpBackend 的合約（202＋輪詢）。

需要本機 PostgreSQL（與 test_binding.py 相同）；RAG pipeline 換成假函式。
"""

import uuid
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api import routes_xiaozhao
from app.config import settings
from app.core.models import Chunk, RefusalReason
from app.core.pipeline import QueryResult
from app.db.models import XiaozhaoRequest
from app.db.session import SessionLocal

pytestmark = pytest.mark.asyncio(loop_scope="session")

_TOKEN = "test-xiaozhao-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

_CHUNK = Chunk(
    chunk_id=str(uuid.uuid4()),
    doc_id="d1",
    content="長期照顧服務法第三條原文",
    law_name="長期照顧服務法",
    article="3",
    source_url="https://law.moj.gov.tw/",
    updated_at=date(2026, 1, 1),
    similarity=0.9,
)


@pytest.fixture
def pipeline_calls(monkeypatch):
    monkeypatch.setattr(settings, "xiaozhao_api_token", _TOKEN)
    calls: list[str] = []
    results: dict[str, QueryResult] = {}

    async def fake_pipeline(query_text, session):
        calls.append(query_text)
        if query_text in results:
            return results[query_text]
        if query_text == "boom":
            raise RuntimeError("ollama down")
        return QueryResult(answer="摘要回答", sources=[_CHUNK], confidence="high", disclaimer="僅供參考")

    monkeypatch.setattr(routes_xiaozhao, "run_query_pipeline", fake_pipeline)
    return SimpleNamespace(calls=calls, results=results)


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(routes_xiaozhao.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://be")


def _body(text: str = "家庭照顧者的定義是什麼？", **extra) -> dict:
    return {"request_id": uuid.uuid4().hex, "text": text, "role": "caregiver", "user_id": None, **extra}


async def test_rejects_missing_or_wrong_token(pipeline_calls):
    async with _client() as client:
        assert (await client.post("/v1/query", json=_body())).status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert (await client.get("/v1/requests/abc", headers=bad)).status_code == 401


async def test_rejects_everything_when_token_not_configured(pipeline_calls, monkeypatch):
    monkeypatch.setattr(settings, "xiaozhao_api_token", "")
    async with _client() as client:
        assert (await client.post("/v1/query", json=_body(), headers={"Authorization": "Bearer "})).status_code == 401


async def test_query_returns_202_then_answer_with_verbatim_quote(pipeline_calls):
    body = _body(line_case_id=7, caregiver_name="小吳")
    async with _client() as client:
        response = await client.post("/v1/query", json=body, headers=_AUTH)
        assert response.status_code == 202
        assert response.json()["request_id"] == body["request_id"]

        polled = (await client.get(f"/v1/requests/{body['request_id']}", headers=_AUTH)).json()

    assert polled["status"] == "answered"
    assert polled["answer_spoken"] == "摘要回答"
    assert "長期照顧服務法第三條原文" in polled["answer_full"]
    assert polled["citations"][0]["quote"] == "長期照顧服務法第三條原文"
    assert polled["refusal_reason"] is None
    assert polled["escalate"] is False

    async with SessionLocal() as session:
        row = (await session.execute(select(XiaozhaoRequest).where(XiaozhaoRequest.request_id == body["request_id"]))).scalar_one()
    assert row.channel == "line"
    assert "caregiver_name" not in row.payload
    assert "text" not in row.payload


async def test_resent_request_does_not_rerun_pipeline(pipeline_calls):
    body = _body("重送測試的問題")
    async with _client() as client:
        first = await client.post("/v1/query", json=body, headers=_AUTH)
        second = await client.post("/v1/query", json=body, headers=_AUTH)
    assert first.status_code == second.status_code == 202
    assert second.json()["status"] == "answered"
    assert pipeline_calls.calls.count("重送測試的問題") == 1


async def test_text_is_masked_before_storage_and_pipeline(pipeline_calls):
    body = _body("家屬電話 0912-345-678 要怎麼登記？")
    async with _client() as client:
        await client.post("/v1/query", json=body, headers=_AUTH)
    assert pipeline_calls.calls[-1] == "家屬電話 [電話] 要怎麼登記？"


async def test_refusal_is_structured(pipeline_calls):
    pipeline_calls.results["可以補吃藥嗎"] = QueryResult(
        answer="須由督導判斷", status="refused", refusal_reason=RefusalReason.SENSITIVE, escalate=True
    )
    body = _body("可以補吃藥嗎")
    async with _client() as client:
        await client.post("/v1/query", json=body, headers=_AUTH)
        polled = (await client.get(f"/v1/requests/{body['request_id']}", headers=_AUTH)).json()
    assert polled["status"] == "refused"
    assert polled["refusal_reason"] == "sensitive"
    assert polled["escalate"] is True


async def test_pipeline_error_marks_request_as_error(pipeline_calls):
    body = _body("boom")
    async with _client() as client:
        await client.post("/v1/query", json=body, headers=_AUTH)
        polled = (await client.get(f"/v1/requests/{body['request_id']}", headers=_AUTH)).json()
    assert polled["status"] == "error"


async def test_unknown_request_is_404(pipeline_calls):
    async with _client() as client:
        assert (await client.get(f"/v1/requests/{uuid.uuid4().hex}", headers=_AUTH)).status_code == 404


async def test_scene_is_recorded_without_transcript(pipeline_calls):
    body = {
        "request_id": uuid.uuid4().hex,
        "turn_id": 12,
        "intent_counts": {"regulation_inquiry": 1},
        "utterances": [{"idx": 0, "text": "阿嬤今天不想洗澡", "intent": "服務請求"}],
    }
    async with _client() as client:
        response = await client.post("/v1/scene", json=body, headers=_AUTH)
    assert response.status_code == 202
    assert pipeline_calls.calls == []
    async with SessionLocal() as session:
        row = (await session.execute(select(XiaozhaoRequest).where(XiaozhaoRequest.request_id == body["request_id"]))).scalar_one()
    assert row.status == "recorded"
    assert row.payload["intent_counts"] == {"regulation_inquiry": 1}
    assert "utterances" not in row.payload


async def test_rejects_malformed_request_id(pipeline_calls):
    async with _client() as client:
        response = await client.post("/v1/query", json=_body() | {"request_id": "../../etc"}, headers=_AUTH)
    assert response.status_code == 422
