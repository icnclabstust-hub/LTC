"""低信心問題由照護員確認、打字提問規則、LINE 只回答規範詢問（2026-10-15 confirm-send-and-keyword 第 6 節）。"""

import dataclasses
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import FakeEngines, build_audio, to_chunks
from test_api import RecordingLine, as_user, bind_user
from test_multi_intent import MEDS, PAIN, RULE_Q, Backend
from xiaozhao.app import State, create_app, get_principal
from xiaozhao.auth import resolve_principal
from xiaozhao.backend import BackendResult, with_citations
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor

CHAT = "今天天氣很好"


def run_turn(db, intents, backend, confirm_low=True):
    texts = {0: "小照你好", 1: RULE_Q, 2: PAIN}
    engines = FakeEngines(texts=texts, intents=intents, kws_schedule=[(0.9, "小照你好")])
    rid = db.execute("INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at)"
                     " VALUES ('Ucare', '住民 A', '2026-10-01', ?)", (now(),)).lastrowid
    db.execute("INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
               " VALUES ('s1', ?, 'Ucare', ?, ?)", (rid, now(), now()))
    processor = SessionProcessor("s1", rid, "Ucare", db, engines, backend, 0.9, confirm_low=confirm_low)
    audio = build_audio([("speech", 0.8, 0), ("silence", 0.5), ("speech", 1, 1), ("silence", 0.5),
                         ("speech", 1, 2), ("silence", 4)])
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)
    return db.one("SELECT * FROM turns")


def final_queries(backend):
    return [p["intent"] for kind, p in backend.calls if kind == "query" and not p["provisional"]]


def test_low_confidence_waits_for_caregiver(db):
    backend = Backend()
    turn = run_turn(db, {RULE_Q: ("規範詢問", 0.95), PAIN: ("身體不適陳述", 0.7)}, backend)
    assert final_queries(backend) == []  # 0.95 已預取；0.7 不送出
    results = json.loads(turn["intents"])
    assert [r["status"] for r in results] == ["answered", "needs_confirm"]
    assert turn["answer_status"] == "needs_confirm" and "等你確認" in turn["answer"]


def test_below_half_neither_sent_nor_asked(db):
    backend = Backend()
    turn = run_turn(db, {RULE_Q: ("規範詢問", 0.4), PAIN: ("身體不適陳述", 0.3)}, backend)
    assert final_queries(backend) == [] and turn["intents"] is None


def test_switch_off_sends_as_before(db):
    backend = Backend()
    run_turn(db, {RULE_Q: ("規範詢問", 0.95), PAIN: ("身體不適陳述", 0.7)}, backend, confirm_low=False)
    assert final_queries(backend) == ["身體不適陳述"]


@pytest.fixture
def app_client(settings, db):
    engines = FakeEngines(texts={}, intents={RULE_Q: ("規範詢問", 0.95), PAIN: ("身體不適陳述", 0.7),
                                            MEDS: ("規範詢問", 0.6), CHAT: ("情緒安撫或閒聊", 0.9)})
    st = State(dataclasses.replace(settings, confirm_low_confidence=True), db, engines=engines)
    st.backend = Backend(human={"身體不適陳述"})
    app = create_app(st)
    app.dependency_overrides[get_principal] = lambda: resolve_principal(db, st.settings, "Ucare", "Ucare", None)
    bind_user(db, "Ucare", "caregiver", "照護員甲")
    client = TestClient(app)
    rid = client.post("/api/residents", json={"alias": "住民 A"}, headers=as_user("Ucare")).json()["id"]
    return client, st, rid


def ask(client, rid, text):
    return client.post(f"/api/residents/{rid}/text-questions", json={"question": text}, headers=as_user("Ucare")).json()


def test_typed_question_follows_same_rule(app_client):
    client, st, rid = app_client
    assert ask(client, rid, RULE_Q)["status"] == "answered"
    assert ask(client, rid, PAIN)["status"] == "needs_confirm"
    assert ask(client, rid, CHAT)["status"] == "not_sent"
    assert len(st.backend.calls) == 1
    mine = client.get("/api/my/text-questions", headers=as_user("Ucare")).json()
    assert len(mine) == 3  # 每個打字提問都記在我的案件


def test_confirm_sends_and_dismiss_keeps_record(app_client, db):
    client, st, rid = app_client
    pain = ask(client, rid, PAIN)
    meds = ask(client, rid, MEDS)
    items = client.get("/api/my/unconfirmed", headers=as_user("Ucare")).json()
    assert {i["question"] for i in items} == {PAIN, MEDS}
    client.post("/api/unconfirmed/confirm", json={"send": True, "items": [{"source": "text_question", "id": pain["id"]}]},
                headers=as_user("Ucare"))
    client.post("/api/unconfirmed/confirm", json={"send": False, "items": [{"source": "text_question", "id": meds["id"]}]},
                headers=as_user("Ucare"))
    rows = {r["question"]: r["status"] for r in db.all("SELECT question, status FROM text_questions")}
    assert rows == {PAIN: "awaiting_human", MEDS: "dismissed"}
    assert db.one("SELECT source FROM supervisor_requests")["source"] == "text_question"
    assert client.get("/api/my/unconfirmed", headers=as_user("Ucare")).json() == []
    # 已處理的不能再確認一次
    res = client.post("/api/unconfirmed/confirm", json={"send": True, "items": [{"source": "text_question", "id": meds["id"]}]},
                      headers=as_user("Ucare"))
    assert res.status_code == 404


def test_confirm_turn_item_updates_answer(app_client, db):
    client, st, rid = app_client
    turn = run_turn(db, {RULE_Q: ("規範詢問", 0.7), PAIN: ("情緒安撫或閒聊", 0.9)}, Backend())
    # run_turn 另建了住民與錄音，同一位照護員
    items = client.get("/api/my/unconfirmed", headers=as_user("Ucare")).json()
    assert [(i["source"], i["id"]) for i in items] == [("turn", turn["id"])]
    client.post("/api/unconfirmed/confirm", json={"send": True, "items": [items[0]]}, headers=as_user("Ucare"))
    turn = db.one("SELECT * FROM turns")
    assert turn["answer_status"] == "answered" and "關於規範詢問的回答" in turn["answer"]


def test_other_caregiver_cannot_confirm(app_client, db):
    client, st, rid = app_client
    q = ask(client, rid, PAIN)
    bind_user(db, "Uother", "caregiver", "照護員乙")
    client.app.dependency_overrides[get_principal] = lambda: resolve_principal(db, st.settings, "Uother", "Uother", None)
    res = client.post("/api/unconfirmed/confirm", json={"send": True, "items": [{"source": "text_question", "id": q["id"]}]})
    assert res.status_code == 404


# ---------- LINE 聊天室 ----------

class CitingBackend(Backend):
    def query(self, payload):
        self.calls.append(("query", payload))
        return BackendResult("q1", "answered", answer=with_citations("可以代購", [{"title": "長照服務手冊", "article": "第 3 條"}]))


def line_case(settings, db, text, intent):
    line = RecordingLine(dataclasses.replace(settings, line_regulation_only=True), db, CitingBackend(),
                         classify=lambda _: intent)
    line._create_line_case(SimpleNamespace(message=SimpleNamespace(id="m1"), reply_token="rt"), "Ucare", text)
    return line, db.one("SELECT * FROM line_cases")


def test_line_regulation_question_answered_with_citation(settings, db):
    line, case = line_case(settings, db, RULE_Q, ("規範詢問", 0.6))
    assert case["status"] == "answered"
    assert line.sent[0][0] == "reply" and "出處：長照服務手冊 第 3 條" in line.sent[0][1]


def test_line_medical_goes_to_supervisor_even_if_regulation(settings, db):
    line, case = line_case(settings, db, MEDS, ("規範詢問", 0.99))
    assert case["status"] == "awaiting_human" and line._backend.calls == []
    assert line.sent == [("reply", "這個問題已轉給督導，督導回覆後會通知你。")]
    assert db.one("SELECT source FROM supervisor_requests")["source"] == "line_case"


def test_line_other_intent_not_answered(settings, db):
    line, case = line_case(settings, db, "可以幫我倒水嗎", ("服務請求", 0.99))
    assert case["status"] == "not_answered" and line.sent == [] and line._backend.calls == []


def test_with_citations():
    assert with_citations("答", []) == "答"
    assert with_citations("答", ["A", "A", {"title": "B"}]) == "答\n出處：A；B"
