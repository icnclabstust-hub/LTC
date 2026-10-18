"""一輪多個意圖（2026-10-13 multi-intent-turn 設計文件第 5 節）。"""

import json

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao import services
from xiaozhao.backend import BackendResult
from xiaozhao.db import Database, now
from xiaozhao.pipeline import SessionProcessor, _turn_intents

RULE_Q = "機構可以幫忙代購嗎"
PAIN = "我肚子很痛"
MEDS = "這個藥可以吃嗎"


class Backend:
    """依意圖決定回答；記錄每次呼叫。"""

    def __init__(self, human=()):
        self.calls, self.human = [], set(human)

    def query(self, payload):
        self.calls.append(("query", payload))
        if payload["intent"] in self.human:
            return BackendResult(f"q{len(self.calls)}", "awaiting_human", reason="醫療轉督導")
        return BackendResult(f"q{len(self.calls)}", "answered", answer=f"關於{payload['intent']}的回答")

    def scene(self, payload):
        self.calls.append(("scene", payload))
        return BackendResult("s", "answered", answer="不應被採用")


def run(db, intents, backend, threshold=0.99):
    texts = {0: "小照你好", 1: RULE_Q, 2: PAIN, 3: "好啊"}
    engines = FakeEngines(texts=texts, intents=intents, kws_schedule=[(0.9, "小照你好")])
    rid = db.execute("INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at)"
                     " VALUES ('Ucare', '住民 A', '2026-10-01', ?)", (now(),)).lastrowid
    db.execute("INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
               " VALUES ('s1', ?, 'Ucare', ?, ?)", (rid, now(), now()))
    processor = SessionProcessor("s1", rid, "Ucare", db, engines, backend, threshold)
    audio = build_audio([("speech", 0.8, 0), ("silence", 0.5), ("speech", 1, 1), ("silence", 0.5),
                         ("speech", 1, 2), ("silence", 0.5), ("speech", 1, 3), ("silence", 4)])
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)
    return db.one("SELECT * FROM turns")


def test_turn_intents_dedup_and_drop_low_confidence():
    utts = [
        {"idx": 0, "text": "a", "intent": "規範詢問", "intent_conf": 0.9, "speaker_label": "01", "speaker_role": "照護員"},
        {"idx": 1, "text": "b", "intent": "身體不適陳述", "intent_conf": 0.8, "speaker_label": "02", "speaker_role": "長輩"},
        {"idx": 2, "text": "c", "intent": "規範詢問", "intent_conf": 0.95, "speaker_label": "02", "speaker_role": "長輩"},
        {"idx": 3, "text": "d", "intent": "情緒安撫或閒聊", "intent_conf": 0.3, "speaker_label": "01", "speaker_role": "照護員"},
    ]
    items = _turn_intents(utts)
    assert [(i["intent"], i["utterance_ids"], i["confidence"], i["text"]) for i in items] == [
        ("規範詢問", [0, 2], 0.95, "a；c"), ("身體不適陳述", [1], 0.8, "b")]
    assert items[0]["speaker_codes"] == ["01", "02"]


def test_two_intents_each_answered_and_scene_only_records(db):
    backend = Backend()
    turn = run(db, {RULE_Q: ("規範詢問", 0.95), PAIN: ("身體不適陳述", 0.9)}, backend)
    queries = [p for kind, p in backend.calls if kind == "query" and not p["provisional"]]
    assert [q["intent"] for q in queries] == ["規範詢問", "身體不適陳述"]
    assert turn["answer"] == "【規範詢問】關於規範詢問的回答\n【身體不適陳述】關於身體不適陳述的回答"
    assert turn["answer_status"] == "answered" and "不應被採用" not in turn["answer"]
    scene = [p for kind, p in backend.calls if kind == "scene"][0]
    assert [i["intent"] for i in scene["intents"]] == ["規範詢問", "身體不適陳述", "情緒安撫或閒聊"]


def test_prefetched_intent_reused_and_other_one_queried(db):
    backend = Backend()
    run(db, {RULE_Q: ("規範詢問", 0.995), PAIN: ("身體不適陳述", 0.9)}, backend)
    provisional = [p["intent"] for kind, p in backend.calls if kind == "query" and p["provisional"]]
    final = [p["intent"] for kind, p in backend.calls if kind == "query" and not p["provisional"]]
    assert provisional == ["規範詢問"] and final == ["身體不適陳述"]


def test_one_answered_one_to_supervisor_then_reply_keeps_other(db):
    backend = Backend(human={"身體不適陳述"})
    turn = run(db, {RULE_Q: ("規範詢問", 0.95), PAIN: ("身體不適陳述", 0.9)}, backend)
    assert turn["answer_status"] == "awaiting_human"
    assert turn["answer"] == "【規範詢問】關於規範詢問的回答\n【身體不適陳述】已轉督導，等待回覆"
    [req] = db.all("SELECT * FROM supervisor_requests")
    assert (req["intent"], req["question"]) == ("身體不適陳述", PAIN)
    services.reply_supervisor_request(db, req["id"], "請量體溫並通知護理師", "督導甲")
    turn = db.one("SELECT * FROM turns")
    assert turn["answer_status"] == "human_answered"
    assert turn["answer"] == "【規範詢問】關於規範詢問的回答\n【身體不適陳述】督導回覆（督導甲）：請量體溫並通知護理師"
    assert [r["status"] for r in json.loads(turn["intents"])] == ["answered", "human_answered"]


def test_chitchat_only_is_no_question(db):
    backend = Backend()
    turn = run(db, {}, backend)  # 全部為預設「情緒安撫或閒聊」
    assert turn["answer_status"] == "no_question" and turn["answer"] is None
    assert [kind for kind, _ in backend.calls] == ["scene"]


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, session_id TEXT, seq INTEGER, started_at REAL)")
    conn.execute("CREATE TABLE supervisor_requests (id INTEGER PRIMARY KEY, caregiver_line_user_id TEXT)")
    conn.commit()
    conn.close()
    db = Database(path)
    cols = lambda t: {r["name"] for r in db.all(f"PRAGMA table_info({t})")}
    assert "intents" in cols("turns") and "intent" in cols("supervisor_requests")
    db.close()
