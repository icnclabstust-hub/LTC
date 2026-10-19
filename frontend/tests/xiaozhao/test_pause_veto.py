"""pause_veto 模式（2026-10-13 pause-veto-service 設計文件第 5 節單元測試）。"""

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao.backend import MockBackend
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor

WAKE = "小照你好"
DONE = "我們來聊天"                 # 已說完
UNFINISHED = "第一個想法就是"       # 沒說完
NEXT = "今天早餐要吃什麼"


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
    return SessionProcessor("s1", resident_id, "Ucare", db, engines, MockBackend(), 0.9, "pause_veto")


class Feeder:
    """可分次餵音訊，序號接續。"""

    def __init__(self, processor):
        self.processor, self.seq = processor, 0

    def __call__(self, parts):
        for chunk in to_chunks(build_audio(parts)):
            self.processor.accept_chunk(self.seq, chunk)
            self.seq += 1


def texts(db):
    return [u["text"] for u in db.all("SELECT text FROM utterances ORDER BY turn_id, idx")]


def engines_for(first, second, **kwargs):
    return CountingEngines(texts={0: WAKE, 1: first, 2: second}, kws_schedule=[(1.0, WAKE)], **kwargs)


WAKE_PART = [("silence", 0.2), ("speech", 0.8, 0), ("silence", 0.6)]


def run(db, first, second, gap):
    engines = engines_for(first, second)
    Feeder(make_session(db, engines))(WAKE_PART + [("speech", 1.5, 1), ("silence", gap), ("speech", 1.5, 2), ("silence", 4)])
    return engines


def test_finished_sentence_splits_on_pause(db):
    run(db, DONE, NEXT, gap=0.75)
    assert texts(db) == [DONE, NEXT]


def test_unfinished_sentence_merges_across_pause(db):
    run(db, UNFINISHED, NEXT, gap=0.75)
    assert texts(db) == [f"{UNFINISHED}，{NEXT}"]


def test_short_pause_merges_even_if_finished(db):
    run(db, DONE, NEXT, gap=0.3)
    assert texts(db) == [f"{DONE}，{NEXT}"]


def test_pause_over_one_second_always_splits(db):
    run(db, UNFINISHED, NEXT, gap=1.5)
    assert texts(db) == [UNFINISHED, NEXT]


def test_merging_does_not_rerun_asr(db):
    engines = run(db, UNFINISHED, NEXT, gap=0.75)
    assert engines.transcribe_calls == 3  # 喚醒詞片段＋兩個片段，合併後不再轉一次


def test_unfinished_sentence_waits_up_to_one_second(db):
    engines = engines_for(UNFINISHED, NEXT)
    feed = Feeder(make_session(db, engines))
    feed(WAKE_PART + [("speech", 1.5, 1), ("silence", 1.0)])
    assert texts(db) == []          # 沒說完：1 秒時還在等（1 秒＋偵測延遲 0.3 秒）
    feed([("silence", 0.5)])
    assert texts(db) == [UNFINISHED]  # 1.5 秒：成句


def test_finished_sentence_is_sent_after_half_second_plus_onset_margin(db):
    engines = engines_for(DONE, NEXT)
    feed = Feeder(make_session(db, engines))
    feed(WAKE_PART + [("speech", 1.5, 1), ("silence", 0.5)])
    assert texts(db) == []          # 0.5 秒：可能是下一句剛開始、VAD 還沒偵測到
    feed([("silence", 0.5)])
    assert texts(db) == [DONE]      # 1.0 秒（≥ 0.5＋0.3）：成句


def test_answer_keyword_flushes_and_ends_turn(db):
    engines = CountingEngines(texts={0: WAKE, 1: UNFINISHED, 2: "小照回答"}, kws_schedule=[(1.0, WAKE)])
    Feeder(make_session(db, engines))(
        WAKE_PART + [("speech", 1.5, 1), ("silence", 0.3), ("speech", 1.0, 2), ("silence", 1)])
    assert texts(db) == [UNFINISHED]
    assert db.one("SELECT end_reason FROM turns")["end_reason"] == "answer_keyword"
