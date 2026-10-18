"""後端呼叫在背景執行（2026-10-14 async-backend 設計文件第 5 節）。

以「等到放行才回覆」的後端模擬慢的後端，不靠 sleep 判斷先後。
"""

import dataclasses
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from conftest import FakeEngines, build_audio, to_chunks
from test_api import RecordingLine, as_user, bind_user
from xiaozhao.app import State, create_app, get_principal
from xiaozhao.auth import resolve_principal
from xiaozhao.backend import BackendResult, BackgroundRunner, TrackedBackend
from xiaozhao.db import now
from xiaozhao.line_service import STILL_QUERYING_TEXT
from xiaozhao.pipeline import SessionProcessor

WAKE = "小照你好"
RULE_Q = "機構可以幫忙代購嗎"


class GatedBackend:
    """收到請求後等到 gate 放行才回覆。"""

    def __init__(self):
        self.gate = threading.Event()
        self.calls = []

    def query(self, payload):
        self.calls.append(payload)
        self.gate.wait(5)
        return BackendResult(f"q{len(self.calls)}", "answered", answer=f"回答：{payload['text']}")

    def scene(self, payload):
        return BackendResult("s", "no_question")


def wait_until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def run_session(db, backend, runner, threshold):
    engines = FakeEngines(texts={0: WAKE, 1: RULE_Q}, intents={RULE_Q: ("規範詢問", 0.95)},
                          kws_schedule=[(0.9, WAKE)])
    rid = db.execute("INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at)"
                     " VALUES ('Ucare', '住民 A', '2026-10-01', ?)", (now(),)).lastrowid
    db.execute("INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
               " VALUES ('s1', ?, 'Ucare', ?, ?)", (rid, now(), now()))
    processor = SessionProcessor("s1", rid, "Ucare", db, engines, TrackedBackend(backend, None), threshold,
                                 runner=runner)
    audio = build_audio([("speech", 0.8, 0), ("silence", 0.6), ("speech", 1.5, 1), ("silence", 4)])
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)  # 後端被擋住時，音訊處理仍要能繼續


def test_turn_answer_arrives_later_without_blocking_audio(db):
    backend, runner = GatedBackend(), BackgroundRunner(2, inline=False)
    run_session(db, backend, runner, threshold=0.99)
    turn = db.one("SELECT * FROM turns")
    assert turn["ended_at"] is not None and turn["answer_status"] == "pending"
    assert db.one("SELECT COUNT(*) AS n FROM records")["n"] == 1  # 確認紀錄不等後端
    backend.gate.set()
    assert wait_until(lambda: db.one("SELECT answer_status FROM turns")["answer_status"] == "answered")
    assert RULE_Q in db.one("SELECT answer FROM turns")["answer"]
    runner.shutdown()


def test_prefetch_future_reused_not_resent(db):
    backend, runner = GatedBackend(), BackgroundRunner(2, inline=False)
    run_session(db, backend, runner, threshold=0.9)
    backend.gate.set()
    assert wait_until(lambda: db.one("SELECT answer_status FROM turns")["answer_status"] == "answered")
    assert [p["provisional"] for p in backend.calls] == [True]  # 輪結束沿用預取，不重送
    runner.shutdown()


def test_text_question_returns_pending_then_answer(settings, db):
    backend, runner = GatedBackend(), BackgroundRunner(2, inline=False)
    st = State(dataclasses.replace(settings, reply_target_seconds=0.05), db, engines=FakeEngines(texts={}))
    st.backend, st.runner = TrackedBackend(backend, None), runner
    app = create_app(st)
    app.dependency_overrides[get_principal] = lambda: resolve_principal(db, st.settings, "Ucare", "Ucare", None)
    client = TestClient(app)
    bind_user(db, "Ucare", "caregiver", "照護員甲")
    rid = client.post("/api/residents", json={"alias": "住民 A"}, headers=as_user("Ucare")).json()["id"]
    q = client.post(f"/api/residents/{rid}/text-questions", json={"question": "長照補助怎麼申請"},
                    headers=as_user("Ucare")).json()
    assert q["status"] == "pending"
    backend.gate.set()
    assert wait_until(lambda: db.one("SELECT status FROM text_questions")["status"] == "answered")
    assert db.one("SELECT reply_ms FROM text_questions")["reply_ms"] is not None  # 回覆時間有記錄
    runner.shutdown()


def test_line_case_tells_still_querying_then_pushes_answer(settings, db):
    backend, runner = GatedBackend(), BackgroundRunner(2, inline=False)
    line = RecordingLine(dataclasses.replace(settings, reply_target_seconds=0.05), db, TrackedBackend(backend, None),
                         runner=runner)
    event = SimpleNamespace(message=SimpleNamespace(id="m1"), reply_token="rt")
    line._create_line_case(event, "Ucare", "長照補助怎麼申請")
    assert line.sent == [("reply", STILL_QUERYING_TEXT)]
    backend.gate.set()
    assert wait_until(lambda: len(line.sent) == 2)
    assert line.sent[1][0] == "push" and "長照補助怎麼申請" in line.sent[1][1]
    row = db.one("SELECT status, reply_ms FROM line_cases")
    assert row["status"] == "answered" and row["reply_ms"] is not None
    runner.shutdown()


def test_line_case_within_target_replies_directly(settings, db):
    backend = GatedBackend()
    backend.gate.set()
    line = RecordingLine(settings, db, TrackedBackend(backend, None), runner=BackgroundRunner(2, inline=False))
    line._create_line_case(SimpleNamespace(message=SimpleNamespace(id="m2"), reply_token="rt"), "Ucare", "補助怎麼申請")
    assert [kind for kind, _ in line.sent] == ["reply"] and STILL_QUERYING_TEXT not in line.sent[0][1]


def test_tracked_backend_records_elapsed(tmp_path):
    backend = GatedBackend()
    backend.gate.set()
    result = TrackedBackend(backend, tmp_path).query({"text": "x", "intent": "規範詢問"})
    assert result.elapsed_ms is not None
    log = next(tmp_path.glob("backend_requests-*.jsonl")).read_text(encoding="utf-8")
    assert '"elapsed_ms"' in log
