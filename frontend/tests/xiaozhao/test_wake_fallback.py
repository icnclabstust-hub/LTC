"""喚醒詞漏抓的 ASR 補抓（2026-10-13 wake-word-miss spike 設計文件第 6 節）。"""

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao import pipeline
from xiaozhao.backend import MockBackend
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor

WAKE_AND_QUESTION = "小趙你好阿嬤今天要洗澡嗎"  # 喚醒詞後不停頓直接問（2026-10-13 tk4 的情況）


class CountingEngines(FakeEngines):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transcribe_calls = 0

    def transcribe(self, samples):
        self.transcribe_calls += 1
        return super().transcribe(samples)


def make_session(db, engines):
    resident_id = db.execute(
        "INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at) VALUES (?, ?, ?, ?)",
        ("Ucare", "住民 A", "2026-10-01", now()),
    ).lastrowid
    db.execute(
        "INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
        " VALUES ('s1', ?, 'Ucare', ?, ?)", (resident_id, now(), now()),
    )
    return SessionProcessor("s1", resident_id, "Ucare", db, engines, MockBackend(), 0.9)


def feed(processor, audio):
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)


def turn_texts(db):
    return [[u["text"] for u in db.all("SELECT text FROM utterances WHERE turn_id = ? ORDER BY idx", (t["id"],))]
            for t in db.all("SELECT id FROM turns ORDER BY seq")]


def test_kws_miss_recovered_by_asr_and_question_kept(db):
    engines = CountingEngines(texts={0: WAKE_AND_QUESTION})  # KWS 沒有任何偵測
    feed(make_session(db, engines), build_audio([("silence", 0.5), ("speech", 2.5, 0), ("silence", 4)]))
    assert turn_texts(db) == [["阿嬤今天要洗澡嗎"]]


def test_chatter_outside_turn_still_dropped(db):
    engines = CountingEngines(texts={0: "今天天氣很好"})
    feed(make_session(db, engines), build_audio([("speech", 1.5, 0), ("silence", 4)]))
    assert turn_texts(db) == []


def test_late_kws_report_does_not_open_second_turn(db):
    engines = CountingEngines(texts={0: WAKE_AND_QUESTION, 1: "還有要換藥"}, kws_schedule=[(4.0, "小照你好")])
    feed(make_session(db, engines), build_audio([
        ("silence", 0.5), ("speech", 2.5, 0), ("silence", 0.8), ("speech", 1.5, 1), ("silence", 4),
    ]))
    assert turn_texts(db) == [["阿嬤今天要洗澡嗎", "還有要換藥"]]


def test_kws_hit_skips_asr_fallback(db):
    engines = CountingEngines(texts={0: "小照你好", 1: "藥要怎麼吃"}, kws_schedule=[(1.5, "小照你好")])
    feed(make_session(db, engines), build_audio([
        ("silence", 0.5), ("speech", 0.8, 0), ("silence", 0.6), ("speech", 1.2, 1), ("silence", 4),
    ]))
    assert turn_texts(db) == [["藥要怎麼吃"]]
    assert engines.transcribe_calls == 2  # 只有兩句本身的 ASR，沒有補抓


def test_fallback_can_be_disabled(db, monkeypatch):
    monkeypatch.setattr(pipeline, "WAKE_ASR_FALLBACK_SECONDS", 0)
    engines = CountingEngines(texts={0: WAKE_AND_QUESTION})
    feed(make_session(db, engines), build_audio([("silence", 0.5), ("speech", 2.5, 0), ("silence", 4)]))
    assert turn_texts(db) == []
    assert engines.transcribe_calls == 0
