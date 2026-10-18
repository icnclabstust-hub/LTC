import base64
import hashlib
import hmac
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_line_webhook as webhook
from app.config import settings
from app.core.pipeline import QueryResult
from app.core.rate_limit import is_rate_limited
from app.db.binding import generate_code

_SECRET = "test-channel-secret"


class _FakeLineClient:
    def __init__(self, reply_ok: bool) -> None:
        self.reply_ok = reply_ok
        self.replies: list[str] = []
        self.pushes: list[tuple[str, str]] = []

    async def reply(self, reply_token: str, text: str) -> bool:
        self.replies.append(text)
        return self.reply_ok

    async def push(self, to: str, text: str) -> bool:
        self.pushes.append((to, text))
        return True


def _sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha256).digest()).decode("utf-8")


def _text_event(user_id: str, text: str) -> dict:
    return {
        "type": "message",
        "webhookEventId": f"test-{uuid.uuid4().hex}",
        "replyToken": "reply-token",
        "source": {"type": "user", "userId": user_id},
        "message": {"type": "text", "text": text},
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "line_channel_secret", _SECRET)
    app = FastAPI()
    app.include_router(webhook.router)
    return TestClient(app)


def test_webhook_returns_200_and_defers_processing(client, monkeypatch):
    handled: list[list[dict]] = []

    async def fake_handle_events(events):
        handled.append(events)

    monkeypatch.setattr(webhook, "_handle_events", fake_handle_events)
    body = json.dumps({"events": [_text_event("U1", "小照你好 測試")]}).encode("utf-8")

    response = client.post("/v1/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})

    assert response.status_code == 200
    assert len(handled) == 1 and len(handled[0]) == 1


def test_webhook_rejects_bad_signature(client):
    body = b'{"events":[]}'
    response = client.post("/v1/line/webhook", content=body, headers={"X-Line-Signature": "bad"})
    assert response.status_code == 401


def test_generated_binding_code_is_six_digits():
    code = generate_code()
    assert len(code) == 6 and code.isdigit()


@pytest.mark.asyncio(loop_scope="session")
async def test_reply_failure_falls_back_to_push(monkeypatch):
    fake = _FakeLineClient(reply_ok=False)
    monkeypatch.setattr(webhook, "_line_client", fake)

    async def fake_find_identity(session, channel, external_user_id):
        return object()

    async def fake_pipeline(query_text, session):
        return QueryResult(answer="測試答案", sources=[], confidence="high")

    async def fake_log(session, **kwargs):
        return None

    monkeypatch.setattr(webhook, "find_identity", fake_find_identity)
    monkeypatch.setattr(webhook, "run_query_pipeline", fake_pipeline)
    monkeypatch.setattr(webhook, "log_conversation", fake_log)

    await webhook._handle_events([_text_event("U_fallback", "小照你好 長照評鑑")])

    assert fake.replies == ["測試答案"]
    assert fake.pushes == [("U_fallback", "測試答案")]


@pytest.mark.asyncio(loop_scope="session")
async def test_processing_error_notifies_user(monkeypatch):
    fake = _FakeLineClient(reply_ok=True)
    monkeypatch.setattr(webhook, "_line_client", fake)

    async def fake_find_identity(session, channel, external_user_id):
        return object()

    async def failing_pipeline(query_text, session):
        raise RuntimeError("Ollama 逾時")

    monkeypatch.setattr(webhook, "find_identity", fake_find_identity)
    monkeypatch.setattr(webhook, "run_query_pipeline", failing_pipeline)

    await webhook._handle_events([_text_event("U_error", "小照你好 長照評鑑")])

    assert fake.pushes == [("U_error", webhook._PROCESSING_FAILED_MESSAGE)]


@pytest.mark.asyncio(loop_scope="session")
async def test_bind_attempts_are_rate_limited(monkeypatch):
    fake = _FakeLineClient(reply_ok=True)
    monkeypatch.setattr(webhook, "_line_client", fake)

    async def fake_redeem(session, code_str, channel, external_user_id):
        return None

    monkeypatch.setattr(webhook, "redeem_binding_code", fake_redeem)
    user_id = f"U_brute_{uuid.uuid4().hex}"

    events = [_text_event(user_id, "綁定 000000") for _ in range(webhook._BIND_ATTEMPT_LIMIT + 1)]
    await webhook._handle_events(events)

    assert fake.replies[-1] == webhook._BIND_RATE_LIMITED_MESSAGE
    assert fake.replies.count(webhook._BIND_RATE_LIMITED_MESSAGE) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_rate_limit_counts_within_window():
    key = f"test:{uuid.uuid4().hex}"
    results = [await is_rate_limited(key, limit=2, window_seconds=60) for _ in range(3)]
    assert results == [False, False, True]
