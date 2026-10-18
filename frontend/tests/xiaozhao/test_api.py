"""權限、綁定碼、保存期限、LINE webhook（設計文件第 7 節；評估表第六節成功指標）。"""

import base64
import hashlib
import hmac
import json

import pytest
from fastapi import Header, Request
from fastapi.testclient import TestClient

from conftest import FakeEngines
from xiaozhao import services
from xiaozhao.app import State, create_app, get_principal, get_state, track_name
from xiaozhao.auth import resolve_principal
from xiaozhao.db import now
from xiaozhao.line_service import LineService


class RecordingLine(LineService):
    """不連 LINE：記錄 reply／push 內容。"""

    def __init__(self, *args, fail_push=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: list[tuple[str, str]] = []
        self.fail_push = fail_push

    def _reply_messages(self, reply_token, messages):
        for message in messages:
            self.sent.append(("reply", getattr(message, "text", None) or message.alt_text))
        return True

    def push(self, user_id, text):
        if self.fail_push:
            return False
        self.sent.append(("push", text))
        return True

    def link_menu(self, user_id, role):
        self.sent.append(("menu", role))
        return True

    def unlink_menu(self, user_id):
        self.sent.append(("unmenu", user_id))


@pytest.fixture
def state(settings, db):
    st = State(settings, db, engines=FakeEngines(texts={}))
    st.line = RecordingLine(settings, db, st.backend)
    return st


@pytest.fixture
def client(state):
    app = create_app(state)

    def fake_principal(request: Request, x_test_user: str = Header(default=""),
                       x_acting_role: str | None = Header(default=None)):
        # 以 X-Test-User 取代 LINE ID token 驗證，身分判斷仍走 resolve_principal
        if not x_test_user:
            from xiaozhao.auth import Unauthorized
            raise Unauthorized("請先登入 LINE")
        st = get_state(request)
        return track_name(st, resolve_principal(st.db, st.settings, x_test_user, x_test_user, x_acting_role))

    app.dependency_overrides[get_principal] = fake_principal
    with TestClient(app) as c:
        yield c


def as_user(user, role=None):
    headers = {"X-Test-User": user}
    if role:
        headers["X-Acting-Role"] = role
    return headers


def bind_user(db, user, role, name="測試"):
    _, code = services.create_account(db, name, role)
    services.bind(db, code, user)


# ---------- 權限 ----------

ENDPOINTS = [
    ("GET", "/api/residents", None, {"caregiver"}),
    ("POST", "/api/residents", {"alias": "住民 B"}, {"caregiver"}),
    ("GET", "/api/turns", None, {"caregiver"}),
    ("GET", "/api/records", None, {"caregiver"}),
    ("GET", "/api/my/supervisor-requests", None, {"caregiver"}),
    ("GET", "/api/line-cases", None, {"caregiver"}),
    ("GET", "/api/daily-reports/mine", None, {"caregiver"}),
    ("GET", "/api/supervisor/requests", None, {"supervisor"}),
    ("GET", "/api/supervisor/daily-reports", None, {"supervisor"}),
    ("GET", "/api/admin/accounts", None, {"admin"}),
    ("POST", "/api/admin/accounts", {"display_name": "新人", "role": "caregiver"}, {"admin"}),
]


@pytest.mark.parametrize("method,path,body,allowed", ENDPOINTS)
def test_each_api_only_allows_its_roles(client, db, method, path, body, allowed):
    bind_user(db, "Ucare", "caregiver")
    bind_user(db, "Usup", "supervisor")
    callers = {"caregiver": as_user("Ucare"), "supervisor": as_user("Usup"), "admin": as_user("Uadmin")}
    for role, headers in callers.items():
        res = client.request(method, path, json=body, headers=headers)
        assert (res.status_code == 200) == (role in allowed), (role, path, res.status_code, res.text)
    assert client.request(method, path, json=body, headers=as_user("Ustranger")).status_code == 403
    assert client.request(method, path, json=body).status_code == 401


def test_only_admin_can_switch_role(client, db):
    bind_user(db, "Ucare", "caregiver")
    assert client.get("/api/admin/accounts", headers=as_user("Ucare", "admin")).status_code == 403
    assert client.get("/api/residents", headers=as_user("Uadmin", "caregiver")).status_code == 200
    assert client.get("/api/supervisor/requests", headers=as_user("Uadmin", "supervisor")).status_code == 200
    me = client.get("/api/me", headers=as_user("Ucare", "admin")).json()
    assert me["role"] == "caregiver" and me["is_admin"] is False


def test_caregiver_cannot_see_others_residents(client, db):
    bind_user(db, "Ucare", "caregiver")
    bind_user(db, "Uother", "caregiver")
    rid = client.post("/api/residents", json={"alias": "住民 A"}, headers=as_user("Ucare")).json()["id"]
    assert client.patch(f"/api/residents/{rid}", json={"alias": "改名"}, headers=as_user("Uother")).status_code == 404
    assert client.delete(f"/api/residents/{rid}", headers=as_user("Uother")).status_code == 404
    assert client.get("/api/residents", headers=as_user("Uother")).json() == []


def test_recording_requires_written_and_voice_consent(client, db):
    bind_user(db, "Ucare", "caregiver")
    rid = client.post("/api/residents", json={"alias": "住民 A"}, headers=as_user("Ucare")).json()["id"]
    start = {"resident_id": rid, "consent_confirmed": True}
    assert client.post("/api/sessions", json=start, headers=as_user("Ucare")).status_code == 409
    client.patch(f"/api/residents/{rid}/written-consent", json={"date": "2026-10-01"}, headers=as_user("Ucare"))
    assert client.post("/api/sessions", json=start, headers=as_user("Ucare")).status_code == 409
    client.post(f"/api/residents/{rid}/voiceprint-consent", json={"decision": "declined"}, headers=as_user("Ucare"))
    unconfirmed = {"resident_id": rid, "consent_confirmed": False}
    assert client.post("/api/sessions", json=unconfirmed, headers=as_user("Ucare")).status_code == 400
    stale = client.post("/api/sessions", json=start, headers=as_user("Ucare")).json()["session_id"]
    res = client.post("/api/sessions", json=start, headers=as_user("Ucare"))
    assert res.status_code == 200  # 沒收掉的舊錄音不會擋住新的錄音
    sid = res.json()["session_id"]
    assert client.post(f"/api/sessions/{stale}/chunks?seq=0", content=b"\0\0", headers=as_user("Ucare")).status_code == 404
    assert db.one("SELECT ended_at FROM sessions WHERE id = ?", (stale,))["ended_at"] is not None
    bind_user(db, "Uother", "caregiver")
    assert client.post(f"/api/sessions/{sid}/chunks?seq=0", content=b"\0\0", headers=as_user("Uother")).status_code == 404
    assert client.post(f"/api/sessions/{sid}/stop", headers=as_user("Ucare")).status_code == 200


# ---------- 綁定碼 ----------

def test_binding_code_single_use_and_expiry(db):
    _, code = services.create_account(db, "小吳", "caregiver")
    with pytest.raises(services.ServiceError):
        services.bind(db, "WRONGCODE", "U1")
    services.bind(db, code, "U1")
    with pytest.raises(services.ServiceError):
        services.bind(db, code, "U2")  # 重複使用

    account, expired = services.create_account(db, "小陳", "supervisor")
    db.execute("UPDATE binding_codes SET expires_at = ? WHERE account_id = ?", (now() - 1, account["id"]))
    with pytest.raises(services.ServiceError):
        services.bind(db, expired, "U3")

    account, old = services.create_account(db, "小林", "caregiver")
    new = services.issue_binding_code(db, account["id"])
    with pytest.raises(services.ServiceError):
        services.bind(db, old, "U4")  # 重新產生後舊碼失效
    services.bind(db, new.lower(), "U4")  # 不分大小寫


def test_disabled_account_loses_access(client, db, state):
    bind_user(db, "Ucare", "caregiver")
    account = db.one("SELECT id FROM accounts WHERE line_user_id = 'Ucare'")
    assert client.patch(f"/api/admin/accounts/{account['id']}", json={"status": "disabled"},
                        headers=as_user("Uadmin")).status_code == 200
    assert client.get("/api/residents", headers=as_user("Ucare")).status_code == 403
    assert ("unmenu", "Ucare") in state.line.sent


def test_bind_api_links_menu(client, db, state):
    _, code = services.create_account(db, "督導甲", "supervisor")
    res = client.post("/api/bind", json={"code": code}, headers=as_user("Unew"))
    assert res.status_code == 200 and res.json()["role"] == "supervisor"
    assert ("menu", "supervisor") in state.line.sent
    assert client.post("/api/bind", json={"code": code}, headers=as_user("Unew")).status_code == 409


# ---------- 督導流程 ----------

def test_supervisor_reply_updates_source_and_pushes(client, db, state):
    bind_user(db, "Ucare", "caregiver", "照護員甲")
    bind_user(db, "Usup", "supervisor", "督導乙")
    rid = client.post("/api/residents", json={"alias": "住民 A"}, headers=as_user("Ucare")).json()["id"]
    q = client.post(f"/api/residents/{rid}/text-questions", json={"question": "補助金額是多少"},
                    headers=as_user("Ucare")).json()
    assert q["status"] == "awaiting_human"
    requests = client.get("/api/supervisor/requests", headers=as_user("Usup")).json()
    assert requests[0]["caregiver_name"] == "照護員甲" and requests[0]["source"] == "text_question"
    res = client.post(f"/api/supervisor/requests/{requests[0]['id']}/reply", json={"reply": "請洽社工"},
                      headers=as_user("Usup"))
    assert res.json()["pushed"] is True
    updated = client.get(f"/api/residents/{rid}/text-questions", headers=as_user("Ucare")).json()[0]
    assert updated["status"] == "human_answered" and "請洽社工" in updated["answer"]
    assert client.post(f"/api/supervisor/requests/{requests[0]['id']}/reply", json={"reply": "再回一次"},
                       headers=as_user("Usup")).status_code == 409


# ---------- 保存期限 ----------

def test_cleanup_respects_retention_rules(db):
    old = now() - 2 * 24 * 3600
    db.execute("INSERT INTO residents (id, caregiver_line_user_id, alias, created_at) VALUES (1, 'U', 'A', ?)", (old,))
    db.execute("INSERT INTO voice_consents VALUES (1, 'agreed', ?)", (old,))
    db.execute("INSERT INTO sessions VALUES ('s', 1, 'U', ?, ?, ?)", (old, old, old))
    db.execute("INSERT INTO turns (session_id, seq, started_at, ended_at) VALUES ('s', 1, ?, ?)", (old, old))
    db.execute("INSERT INTO utterances (turn_id, idx, start_ms, end_ms, text, speaker_role)"
               " VALUES (1, 0, 0, 1, '文字', '照護員')")
    db.execute("INSERT INTO line_cases (caregiver_line_user_id, question, status, created_at) VALUES ('U', 'q', 'answered', ?)", (old,))
    db.execute("INSERT INTO daily_reports (caregiver_line_user_id, report_date, transcript, created_at) VALUES ('U', 'd', 't', ?)", (old,))
    open_id = services.create_supervisor_request(db, "U", "turn", 1, "未回覆", None, None)
    replied_id = services.create_supervisor_request(db, "U", "line_case", 1, "已回覆", None, None)
    db.execute("UPDATE supervisor_requests SET created_at = ? WHERE id = ?", (old, open_id))
    db.execute("UPDATE supervisor_requests SET status = 'replied', replied_at = ? WHERE id = ?", (old, replied_id))

    services.cleanup_expired(db)
    count = lambda table: db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
    assert count("turns") == count("utterances") == count("line_cases") == count("daily_reports") == 0
    assert count("sessions") == 0
    assert [r["id"] for r in db.all("SELECT id FROM supervisor_requests")] == [open_id]


def test_cleanup_keeps_unconfirmed_records_for_seven_days(db):
    db.execute("INSERT INTO residents (id, caregiver_line_user_id, alias, created_at) VALUES (1, 'U', 'A', ?)", (now(),))
    utt = [{"idx": 0, "start_ms": 0, "end_ms": 1, "text": "文字", "speaker_label": "01", "speaker_role": "未辨識",
            "intent": None}]
    recent = services.create_record(db, 1, "s", "U", 1, utt, {})
    stale = services.create_record(db, 2, "s", "U", 1, utt, {})
    confirmed = services.create_record(db, 3, "s", "U", 1, utt, {})
    eight_days_ago = now() - 8 * 24 * 3600
    db.execute("UPDATE records SET created_at = ? WHERE seq IN (?, ?)", (eight_days_ago, stale, confirmed))
    db.execute("UPDATE records SET status = 'confirmed' WHERE seq = ?", (confirmed,))
    db.execute("UPDATE records SET created_at = ? WHERE seq = ?", (now() - 2 * 24 * 3600, recent))
    services.cleanup_expired(db)
    assert sorted(r["seq"] for r in db.all("SELECT seq FROM records")) == [recent, confirmed]


# ---------- LINE webhook ----------

def signed(secret, payload):
    body = json.dumps(payload, ensure_ascii=False)
    signature = base64.b64encode(hmac.new(secret.encode(), body.encode("utf-8"), hashlib.sha256).digest()).decode()
    return body, signature


def text_event(event_id, message_id, text, user="Ucare"):
    return {
        "type": "message", "mode": "active", "timestamp": 1760000000000, "webhookEventId": event_id,
        "deliveryContext": {"isRedelivery": False}, "replyToken": "r" * 32,
        "source": {"type": "user", "userId": user},
        "message": {"type": "text", "id": message_id, "quoteToken": "q", "text": text},
    }


def post_webhook(client, settings, events):
    body, signature = signed(settings.channel_secret, {"destination": "Ubot", "events": events})
    return client.post("/callback", content=body.encode("utf-8"),
                       headers={"X-Line-Signature": signature, "Content-Type": "application/json"})


def test_webhook_rejects_bad_signature(client):
    res = client.post("/callback", content=b'{"events":[]}', headers={"X-Line-Signature": "forged"})
    assert res.status_code == 400


def test_webhook_prefix_filter_and_dedup(client, db, settings, state):
    bind_user(db, "Ucare", "caregiver")
    event = text_event("E1", "M1", "小照你好，家屬要我幫忙代購，機構規定可以嗎？")
    assert post_webhook(client, settings, [event]).status_code == 200
    assert post_webhook(client, settings, [event]).status_code == 200  # 重送
    post_webhook(client, settings, [text_event("E2", "M2", "今天天氣很好")])
    cases = db.all("SELECT * FROM line_cases")
    assert len(cases) == 1
    assert cases[0]["question"] == "家屬要我幫忙代購，機構規定可以嗎？"
    assert cases[0]["status"] == "answered"
    assert len([s for s in state.line.sent if s[0] == "reply"]) == 1


def test_webhook_unbound_user_gets_bind_prompt(client, db, settings, state):
    post_webhook(client, settings, [text_event("E3", "M3", "小照你好，請問", user="Ustranger")])
    assert db.all("SELECT * FROM line_cases") == []
    assert state.line.sent == [("reply", "請輸入綁定碼")]


def test_webhook_medical_question_goes_to_supervisor(client, db, settings, state):
    bind_user(db, "Ucare", "caregiver")
    post_webhook(client, settings, [text_event("E4", "M4", "小照你好 阿嬤的藥可以停嗎")])
    case = db.one("SELECT * FROM line_cases")
    assert case["status"] == "awaiting_human"
    assert db.one("SELECT source FROM supervisor_requests")["source"] == "line_case"
    assert "轉給督導" in state.line.sent[-1][1]


def test_supervisor_sees_admin_name_when_admin_acts_as_caregiver(client, db):
    bind_user(db, "Usup", "supervisor")
    caregiver = as_user("Uadmin", "caregiver")
    rid = client.post("/api/residents", json={"alias": "住民 A"}, headers=caregiver).json()["id"]
    client.post(f"/api/residents/{rid}/text-questions", json={"question": "藥可以磨粉嗎"}, headers=caregiver)
    requests = client.get("/api/supervisor/requests", headers=as_user("Usup")).json()
    assert requests[0]["caregiver_name"] == "Uadmin"  # 測試替身以 userId 當 LINE 名稱


def follow_event(event_id, user):
    return {
        "type": "follow", "mode": "active", "timestamp": 1760000000000, "webhookEventId": event_id,
        "deliveryContext": {"isRedelivery": False}, "replyToken": "f" * 32,
        "source": {"type": "user", "userId": user}, "follow": {"isUnblocked": False},
    }


def test_follow_sends_welcome_then_bind_prompt(client, db, settings, state):
    from xiaozhao.line_service import WELCOME_TEXT

    post_webhook(client, settings, [follow_event("F1", "Unew")])
    assert state.line.sent == [("reply", WELCOME_TEXT), ("reply", "請輸入綁定碼")]
    state.line.sent.clear()
    bind_user(db, "Ucare", "caregiver")
    post_webhook(client, settings, [follow_event("F2", "Ucare")])
    assert state.line.sent == [("menu", "caregiver"), ("reply", WELCOME_TEXT)]
