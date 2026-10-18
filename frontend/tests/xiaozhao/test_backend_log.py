"""送後端的 JSON：請求編號、照護員姓名、說話人代號與送出記錄（2026-10-13）。"""

import json
import os

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao import services
from xiaozhao.backend import BackendResult, MockBackend, TrackedBackend
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor


class Recorder:
    def __init__(self):
        self.sent = []

    def query(self, payload):
        self.sent.append(("query", payload))
        return BackendResult("r1", "answered", answer="答")

    def scene(self, payload):
        self.sent.append(("scene", payload))
        return BackendResult("r2", "answered", answer="答")


def test_tracked_backend_adds_request_id_and_logs(tmp_path):
    inner = Recorder()
    backend = TrackedBackend(inner, tmp_path)
    backend.query({"text": "問題"})
    backend.query({"text": "問題"})
    ids = [p["client_request_id"] for _, p in inner.sent]
    assert len(set(ids)) == 2 and all(len(i) == 32 for i in ids)
    [log] = list(tmp_path.glob("backend_requests-*.jsonl"))
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["payload"]["client_request_id"] for e in entries] == ids
    assert entries[0]["response"]["status"] == "answered" and entries[0]["kind"] == "query"


def test_no_log_file_when_disabled(tmp_path):
    TrackedBackend(MockBackend(), None).query({"text": "問題"})
    assert list(tmp_path.iterdir()) == []


def test_payloads_carry_caregiver_name_and_speaker_code(db):
    services.bind(db, services.create_account(db, "小吳", "caregiver")[1], "Ucare")
    rid = db.execute("INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at)"
                     " VALUES ('Ucare', '住民 A', '2026-10-01', ?)", (now(),)).lastrowid
    db.execute("INSERT INTO voice_consents VALUES (?, 'agreed', ?)", (rid, now()))
    db.execute("INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
               " VALUES ('s1', ?, 'Ucare', ?, ?)", (rid, now(), now()))
    question = "機構可以幫忙代購嗎"
    engines = FakeEngines(texts={0: "小照你好", 1: question}, intents={question: ("規範詢問", 0.95)},
                          kws_schedule=[(0.9, "小照你好")])
    backend = Recorder()
    processor = SessionProcessor("s1", rid, "Ucare", db, engines, backend, 0.9)
    for seq, chunk in enumerate(to_chunks(build_audio([("speech", 0.8, 0), ("silence", 0.5),
                                                       ("speech", 1.5, 1), ("silence", 4)]))):
        processor.accept_chunk(seq, chunk)
    kind, prefetch = backend.sent[0]
    assert kind == "query" and prefetch["caregiver_name"] == "小吳" and prefetch["speaker_code"] == "01"


def test_caregiver_name_falls_back_to_line_name(db):
    services.remember_name(db, "Uadmin", "管理員的 LINE 名稱")
    assert services.caregiver_name(db, "Uadmin") == "管理員的 LINE 名稱"
    assert services.caregiver_name(db, "Unknown") == "（未綁定帳號）"


def test_cleanup_removes_backend_logs_after_one_day(db, tmp_path):
    old, fresh = tmp_path / "backend_requests-20261001.jsonl", tmp_path / "backend_requests-20261013.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    fresh.write_text("{}\n", encoding="utf-8")
    two_days_ago = now() - 2 * 24 * 3600
    os.utime(old, (two_days_ago, two_days_ago))
    services.cleanup_expired(db, log_dir=tmp_path)
    assert not old.exists() and fresh.exists()
