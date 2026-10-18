"""意圖模型 N0（2026-10-14 n0-intent-model 設計文件第 6 節）。"""

import json

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao.backend import MockBackend
from xiaozhao.db import now
from xiaozhao.intent_n0 import DualHeadIntentClassifier
from xiaozhao.pipeline import SessionProcessor

WAKE = "小照你好"


class OwnerEngines(FakeEngines):
    """N0 介面：記錄收到的說話人，回傳固定的意圖歸屬。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.speakers_seen = []

    def classify_with_owner(self, text, speaker):
        self.speakers_seen.append(speaker)
        intent, conf = self.classify(text)
        return intent, conf, "長輩"


def run(db, engines):
    rid = db.execute("INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at)"
                     " VALUES ('Ucare', '住民 A', '2026-10-01', ?)", (now(),)).lastrowid
    db.execute("INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
               " VALUES ('s1', ?, 'Ucare', ?, ?)", (rid, now(), now()))
    processor = SessionProcessor("s1", rid, "Ucare", db, engines, MockBackend(), 0.99)
    audio = build_audio([("speech", 0.8, 0), ("silence", 0.6), ("speech", 1.5, 1), ("silence", 4)])
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)


def test_speaker_role_passed_and_owner_stored(db):
    engines = OwnerEngines(texts={0: WAKE, 1: "我肚子很痛"}, intents={"我肚子很痛": ("身體不適陳述", 0.95)},
                           kws_schedule=[(0.9, WAKE)])
    run(db, engines)
    assert engines.speakers_seen == ["未辨識"]  # 尚未對應身分
    row = db.one("SELECT intent, intent_owner FROM utterances")
    assert (row["intent"], row["intent_owner"]) == ("身體不適陳述", "長輩")
    doc = json.loads(db.one("SELECT doc FROM records")["doc"])
    assert doc["sentences"][0]["intent_owner"] == "長輩"


def test_engines_without_owner_keep_old_behavior(db):
    run(db, FakeEngines(texts={0: WAKE, 1: "我肚子很痛"}, kws_schedule=[(0.9, WAKE)]))
    assert db.one("SELECT intent_owner FROM utterances")["intent_owner"] is None


def test_unknown_speaker_label():
    clf = object.__new__(DualHeadIntentClassifier)
    clf._roles = ["照護員", "家屬", "長輩", "督導", "其他工作人員"]
    assert clf.speaker_label("長輩") == "長輩"
    assert clf.speaker_label("未辨識") == "未辨識"
    assert clf.speaker_label(None) == "未辨識"
