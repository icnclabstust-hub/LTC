"""依後端組 API 合約串接（2026-10-14 http-backend 設計文件第 5 節）。不連網：以假的 HTTP 傳輸模擬後端。"""

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from xiaozhao.backend import HttpBackend, TrackedBackend, build_backend

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from fake_backend import create_app  # noqa: E402

QUERY = {"client_request_id": "r1", "turn_id": 3, "question_utterance_id": 1, "text": "香蕉可以吃嗎？",
         "intent": "規範詢問", "intent_conf": 0.95, "keywords": [], "provisional": False}


class FakeServer:
    """依合約回應；pending 幾次後回 status。"""

    def __init__(self, final=None, pending=1, post_status=202, get_status=200):
        self.final = final or {"status": "answered", "answer_full": "可以少量吃", "answer_spoken": "可以"}
        self.pending, self.post_status, self.get_status = pending, post_status, get_status
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, request.headers.get("authorization")))
        if request.method == "POST":
            return httpx.Response(self.post_status, json={"request_id": body["request_id"]})
        if self.get_status != 200:
            return httpx.Response(self.get_status)
        if self.pending > 0:
            self.pending -= 1
            return httpx.Response(200, json={"status": "pending"})
        return httpx.Response(200, json=self.final)


def backend(server, **kwargs):
    clock = iter(range(1000))
    return HttpBackend("http://backend", token="t0k", transport=httpx.MockTransport(server),
                       sleep=lambda s: None, clock=lambda: next(clock), **kwargs)


def test_query_posts_contract_fields_and_polls_answer():
    server = FakeServer(pending=2)
    result = backend(server).query(QUERY)
    assert (result.status, result.answer, result.request_id) == ("answered", "可以少量吃", "r1")
    method, path, body, auth = server.calls[0]
    assert (method, path, auth) == ("POST", "/v1/query", "Bearer t0k")
    assert body["request_id"] == "r1" and "client_request_id" not in body
    assert body["client_intent"] == {"code": "regulation_inquiry", "confidence": 0.95}
    assert (body["role"], body["user_id"], body["label_set_version"]) == ("caregiver", None, "awe-intent-7-v1")
    assert body["question_utterance_id"] == 1  # 本服務原有欄位照送
    assert [c[1] for c in server.calls[1:]] == ["/v1/requests/r1"] * 3


@pytest.mark.parametrize("final, status, text", [
    ({"status": "refused", "help_request_id": "h9"}, "awaiting_human", "後端拒答，轉督導（求助單 h9）"),
    ({"status": "human_answered", "human_answer": "主管：可以"}, "answered", "主管：可以"),
    ({"status": "error"}, "awaiting_human", "後端狀態 error"),
])
def test_status_mapping(final, status, text):
    result = backend(FakeServer(final=final, pending=0)).query(QUERY)
    assert result.status == status
    assert text in (result.answer or result.reason)


def test_timeout_and_errors_go_to_supervisor():
    assert backend(FakeServer(pending=10**6), timeout_seconds=5).query(QUERY).reason == "後端逾時（5 秒）"
    assert backend(FakeServer(post_status=401)).query(QUERY).reason == "後端回應 401"
    assert backend(FakeServer(get_status=503)).query(QUERY).status == "awaiting_human"

    def down(request):
        raise httpx.ConnectError("連不上")
    result = backend(down).query(QUERY)
    assert (result.status, result.reason) == ("awaiting_human", "後端連線失敗：ConnectError")


SCENE = {"client_request_id": "s1", "turn_id": 3, "utterances": [
    {"idx": 0, "text": "阿公有糖尿病", "speaker_role": "照護員", "intent": "資訊告知或交接", "start_ms": 1000, "end_ms": 3000},
    {"idx": 1, "text": "香蕉可以吃嗎？", "speaker_role": "未辨識", "intent": "規範詢問", "start_ms": 3500, "end_ms": 6500},
]}


def test_scene_summary_fields_and_no_polling():
    server = FakeServer()
    result = backend(server).scene(SCENE)
    assert result.status == "no_question" and len(server.calls) == 1
    body = server.calls[0][2]
    assert body["intent_counts"] == {"information_handover": 1, "regulation_inquiry": 1}
    assert (body["segment_count"], body["caregiver_segment_count"], body["duration_sec"]) == (2, 1, 5.5)
    assert body["utterances"][0]["text"] == "阿公有糖尿病"


def test_scene_without_text():
    server = FakeServer()
    backend(server, scene_text=False).scene(SCENE)
    assert all("text" not in u for u in server.calls[0][2]["utterances"])


def test_fake_backend_tool_round_trip(monkeypatch):
    """tools/fake_backend.py 依合約回應：202 → pending → answered／refused。"""
    import fake_backend
    monkeypatch.setattr(fake_backend, "ANSWER_DELAY_SECONDS", 0)
    client = TestClient(create_app("abc"))

    def relay(request: httpx.Request) -> httpx.Response:
        r = client.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        return httpx.Response(r.status_code, content=r.content, headers=r.headers)

    http = HttpBackend("http://testserver", token="abc", transport=httpx.MockTransport(relay), sleep=lambda s: None)
    assert http.query(QUERY).status == "answered"
    refused = http.query({**QUERY, "client_request_id": "r2", "text": "這個藥可以磨碎嗎？"})
    assert (refused.status, refused.reason) == ("awaiting_human", "醫療、用藥或個案安全，轉督導（求助單 help-r2）")
    assert refused.refusal_reason == "sensitive" and "醫療" in refused.answer
    out_of_scope = http.query({**QUERY, "client_request_id": "r3", "text": "今天天氣怎麼樣？"})
    assert (out_of_scope.status, out_of_scope.refusal_reason) == ("answered", "out_of_scope")
    wrong = HttpBackend("http://testserver", token="wrong", transport=httpx.MockTransport(relay))
    assert wrong.query(QUERY).reason == "後端回應 401"


def test_build_backend_modes(tmp_path):
    assert isinstance(build_backend("http", tmp_path, url="http://x"), TrackedBackend)
    with pytest.raises(ValueError):
        build_backend("http", tmp_path)
