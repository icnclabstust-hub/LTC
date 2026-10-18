"""LINE 聊天室：緊急關鍵字、轉交督導、拒答分流（2026-10-19 fe-be-integration 設計文件第 5.1 節）。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import FakeEngines
from test_api import RecordingLine, as_user, bind_user, post_webhook, text_event
from xiaozhao import services
from xiaozhao.app import State, create_app, get_principal
from xiaozhao.auth import resolve_principal
from xiaozhao.backend import BackendResult
from xiaozhao.line_service import ESCALATED_TEXT, HANDOFF_TEXT


class QuickReplyLine(RecordingLine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.quick_replies: list[list[str]] = []

    def _reply_messages(self, reply_token, messages):
        for message in messages:
            quick = getattr(message, "quick_reply", None)
            self.quick_replies.append([i.action.label for i in quick.items] if quick else [])
        return super()._reply_messages(reply_token, messages)


class ScriptedBackend:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def query(self, payload):
        self.calls.append(payload)
        return self.result

    def scene(self, payload):
        return BackendResult("s", "no_question")


@pytest.fixture
def setup(settings, db):
    backend = ScriptedBackend(BackendResult("r1", "answered", answer="回答"))
    st = State(settings, db, engines=FakeEngines(texts={}))
    st.backend = backend
    st.line = QuickReplyLine(settings, db, backend)
    app = create_app(st)
    app.dependency_overrides[get_principal] = lambda: resolve_principal(db, settings, "Ucare", "Ucare", None)
    return SimpleNamespace(client=TestClient(app), db=db, settings=settings, line=st.line, backend=backend)


def test_emergency_replies_template_notifies_supervisor_without_backend(setup):
    bind_user(setup.db, "Ucare", "caregiver", "照護員甲")
    bind_user(setup.db, "Usup", "supervisor", "督導乙")
    post_webhook(setup.client, setup.settings, [text_event("E1", "M1", "個案吃東西噎到，咳不出來")])
    assert setup.backend.calls == []
    push = [t for kind, t in setup.line.sent if kind == "push"]
    assert len(push) == 1 and "噎到" in push[0] and "照護員甲" in push[0]
    assert "咳不出來" not in push[0]  # 推播不放問題原文
    reply = [t for kind, t in setup.line.sent if kind == "reply"][-1]
    assert "哈姆立克" in reply and "已通知督導" in reply and setup.settings.agency_phone in reply
    request = setup.db.one("SELECT * FROM supervisor_requests")
    assert (request["priority"], request["category"]) == ("urgent", "emergency")
    assert setup.db.one("SELECT status FROM line_cases")["status"] == "emergency"


def test_emergency_without_supervisor_does_not_claim_notified(setup):
    bind_user(setup.db, "Ucare", "caregiver")
    post_webhook(setup.client, setup.settings, [text_event("E2", "M2", "小照你好，個案突然全身抽筋")])
    reply = setup.line.sent[-1][1]
    assert "請盡快自行聯絡督導" in reply and "已通知督導" not in reply


def test_unbound_user_still_gets_emergency_guidance(setup):
    post_webhook(setup.client, setup.settings, [text_event("E3", "M3", "一進門就聞到很重的瓦斯味", user="Ustranger")])
    assert "119" in setup.line.sent[-1][1]
    assert setup.db.all("SELECT * FROM supervisor_requests") == []


def test_direct_question_to_supervisor_skips_backend(setup):
    bind_user(setup.db, "Ucare", "caregiver")
    post_webhook(setup.client, setup.settings,
                 [text_event("E4", "M4", "我想直接問督導：家屬要求調整探視時間可以嗎？")])
    assert setup.backend.calls == []
    request = setup.db.one("SELECT * FROM supervisor_requests")
    assert (request["question"], request["category"]) == ("家屬要求調整探視時間可以嗎？", "user_requested")
    assert setup.line.sent[-1][1] == ESCALATED_TEXT


def test_out_of_scope_offers_handoff_button_then_hands_off_previous_question(setup):
    bind_user(setup.db, "Ucare", "caregiver")
    setup.backend.result = BackendResult("r2", "answered", answer="這個問題不在長照服務範圍內",
                                         refusal_reason="out_of_scope")
    post_webhook(setup.client, setup.settings, [text_event("E5", "M5", "我下週的班可以跟同事換嗎")])
    assert setup.line.quick_replies[-1] == [HANDOFF_TEXT]
    assert setup.db.all("SELECT * FROM supervisor_requests") == []

    post_webhook(setup.client, setup.settings, [text_event("E6", "M6", HANDOFF_TEXT)])
    request = setup.db.one("SELECT * FROM supervisor_requests")
    assert (request["question"], request["category"]) == ("我下週的班可以跟同事換嗎", "user_requested")
    post_webhook(setup.client, setup.settings, [text_event("E7", "M7", HANDOFF_TEXT)])
    assert len(setup.db.all("SELECT * FROM supervisor_requests")) == 1
    assert "已經轉給督導" in setup.line.sent[-1][1]


def test_escalated_refusal_shows_safety_text_and_records_reason(setup):
    bind_user(setup.db, "Ucare", "caregiver")
    setup.backend.result = BackendResult("r3", "awaiting_human", answer="須由督導或醫護人員判斷",
                                         reason="醫療、用藥或個案安全，轉督導", refusal_reason="sensitive")
    post_webhook(setup.client, setup.settings, [text_event("E8", "M8", "這顆藥可以磨成粉給個案吃嗎？")])
    assert setup.line.sent[-1][1] == f"須由督導或醫護人員判斷\n\n{ESCALATED_TEXT}"
    assert setup.db.one("SELECT category FROM supervisor_requests")["category"] == "sensitive"


def test_backend_payload_carries_backend_identity(setup):
    bind_user(setup.db, "Ucare", "caregiver", "照護員甲")
    setup.db.execute("UPDATE accounts SET be_user_id = 'u-1', pseudonym = 'CG-0001' WHERE line_user_id = 'Ucare'")
    post_webhook(setup.client, setup.settings, [text_event("E9", "M9", "喘息服務怎麼申請？")])
    payload = setup.backend.calls[0]
    assert (payload["user_id"], payload["role"], payload["caregiver_name"]) == ("u-1", "caregiver", "CG-0001")


def test_liff_text_question_emergency(setup):
    bind_user(setup.db, "Ucare", "caregiver")
    rid = setup.client.post("/api/residents", json={"alias": "住民 A"}, headers=as_user("Ucare")).json()["id"]
    q = setup.client.post(f"/api/residents/{rid}/text-questions", json={"question": "我好像給個案吃錯藥了"},
                          headers=as_user("Ucare")).json()
    assert q["status"] == "emergency" and "不要催吐" in q["answer"]
    assert setup.backend.calls == []
    assert setup.db.one("SELECT priority FROM supervisor_requests")["priority"] == "urgent"


def test_supervisor_list_puts_urgent_first(setup):
    bind_user(setup.db, "Usup", "supervisor")
    services.create_supervisor_request(setup.db, "Ucare", "line_case", 1, "一般", None, None)
    services.create_supervisor_request(setup.db, "Ucare", "line_case", 2, "緊急", None, None, priority="urgent")
    services.create_supervisor_request(setup.db, "Ucare", "line_case", 3, "一般2", None, None)
    app = setup.client.app
    app.dependency_overrides[get_principal] = lambda: resolve_principal(setup.db, setup.settings, "Usup", "Usup", None)
    questions = [r["question"] for r in setup.client.get("/api/supervisor/requests").json()]
    assert questions[0] == "緊急"
