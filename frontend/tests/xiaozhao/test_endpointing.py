"""自適應切句／切輪門檻（2026-10-12 adaptive-endpointing spike 設計文件第 7 節單元測試）。"""

import numpy as np
import pytest

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao.backend import MockBackend
from xiaozhao.db import now
from xiaozhao.endpointing import AdaptiveEndpointer, SpeakerGroups, count_chars
from xiaozhao.pipeline import SessionProcessor

WAKE = "小照你好"
QUESTION = "阿嬤你今天哪裡不舒服"  # 10 字／1.5 秒：講得快
SLOW_REPLY = "好"                  # 1 字／3.5 秒：講得慢
SLOW_SENTENCE = "肚子痛"


def make_session(db, engines, mode, consent="agreed"):
    cur = db.execute(
        "INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at) VALUES (?, ?, ?, ?)",
        ("Ucare", "住民 A", "2026-10-01", now()),
    )
    resident_id = cur.lastrowid
    if consent:
        db.execute("INSERT INTO voice_consents VALUES (?, ?, ?)", (resident_id, consent, now()))
    db.execute(
        "INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
        " VALUES ('s1', ?, 'Ucare', ?, ?)", (resident_id, now(), now()),
    )
    return SessionProcessor("s1", resident_id, "Ucare", db, engines, MockBackend(), 0.9, mode)


def feed(processor, audio):
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)


def turns(db):
    rows = db.all("SELECT * FROM turns ORDER BY seq")
    for row in rows:
        row["texts"] = [u["text"] for u in db.all(
            "SELECT text FROM utterances WHERE turn_id = ? ORDER BY idx", (row["id"],))]
    return rows


def slow_elder_engines():
    return FakeEngines(
        texts={0: WAKE, 1: QUESTION, 2: SLOW_REPLY, 3: SLOW_SENTENCE},
        speakers={1: 1, 2: 2, 3: 2},
        kws_schedule=[(1.4, WAKE)],
    )


# 照護員問一句（快），長輩慢慢回答，第二句中間停頓 0.8 秒
SLOW_ELDER_DIALOGUE = [
    ("silence", 0.5), ("speech", 0.8, 0), ("silence", 0.6),
    ("speech", 1.5, 1), ("silence", 1.0),
    ("speech", 3.5, 2), ("silence", 1.0),
    ("speech", 1.2, 3), ("silence", 0.8), ("speech", 1.2, 3),
]


# ---------- 門檻換算 ----------

def test_thresholds_default_until_enough_speech():
    endpointer = AdaptiveEndpointer()
    assert endpointer.sentence_gap("01") == 0.5
    assert endpointer.turn_silence({"01"}) == 3.0
    endpointer.observe("01", "一二三四五六", 2.0)  # 未滿 3 秒
    assert endpointer.rate("01") is None
    assert endpointer.sentence_gap("01") == 0.5


def test_thresholds_scale_with_rate_and_are_clamped():
    endpointer = AdaptiveEndpointer()
    endpointer.observe("ref", "一" * 16, 4.0)    # 4 字／秒：與固定模式相同
    endpointer.observe("slow", "一" * 4, 4.0)    # 1 字／秒
    endpointer.observe("fast", "一" * 40, 4.0)   # 10 字／秒
    assert endpointer.sentence_gap("ref") == pytest.approx(0.5)
    assert endpointer.turn_silence({"ref"}) == pytest.approx(3.0)
    assert endpointer.sentence_gap("slow") == 1.2
    assert endpointer.sentence_gap("fast") == 0.35
    assert endpointer.turn_silence({"fast"}) == 2.0
    # 輪結束等待以最慢者為準
    assert endpointer.turn_silence({"fast", "slow"}) == 6.0


def test_unknown_speaker_falls_back_to_session_rate():
    endpointer = AdaptiveEndpointer()
    endpointer.observe(None, "一" * 40, 4.0)
    assert endpointer.sentence_gap(None) == 0.35
    assert endpointer.sentence_gap("新來的人") == 0.35


def test_count_chars_ignores_punctuation():
    assert count_chars("好，我知道了。 OK") == 7


def test_speaker_groups_are_anonymous_and_stable():
    groups = SpeakerGroups(threshold=0.35)
    a, b = np.eye(4, dtype=np.float32)[0], np.eye(4, dtype=np.float32)[1]
    assert groups.assign(a) == "01"
    assert groups.assign(b) == "02"
    assert groups.assign(a * 3) == "01"


# ---------- 串流處理 ----------

def test_fixed_mode_splits_slow_speaker_pause(db):
    processor = make_session(db, slow_elder_engines(), "fixed")
    feed(processor, build_audio(SLOW_ELDER_DIALOGUE + [("silence", 4)]))
    result = turns(db)
    assert result[0]["texts"] == [QUESTION, SLOW_REPLY, SLOW_SENTENCE, SLOW_SENTENCE]
    assert result[0]["end_reason"] == "silence"


def test_adaptive_mode_keeps_slow_speaker_sentence_whole(db):
    processor = make_session(db, slow_elder_engines(), "adaptive")
    feed(processor, build_audio(SLOW_ELDER_DIALOGUE + [("silence", 7)]))
    result = turns(db)
    assert len(result) == 1
    assert result[0]["texts"] == [QUESTION, SLOW_REPLY, SLOW_SENTENCE]
    assert result[0]["end_reason"] == "silence"


def test_adaptive_turn_waits_longer_for_slow_speaker(db):
    processor = make_session(db, slow_elder_engines(), "adaptive")
    # 長輩講完後靜音 4.5 秒（固定模式已結束本輪），照護員再問一句仍屬同一輪
    feed(processor, build_audio(SLOW_ELDER_DIALOGUE + [("silence", 4.5), ("speech", 1.5, 1), ("silence", 7)]))
    result = turns(db)
    assert len(result) == 1
    assert result[0]["texts"] == [QUESTION, SLOW_REPLY, SLOW_SENTENCE, QUESTION]


def test_adaptive_speaker_change_splits_even_with_short_gap(db):
    engines = FakeEngines(texts={0: WAKE, 1: QUESTION, 2: SLOW_SENTENCE}, speakers={1: 1, 2: 2},
                          kws_schedule=[(1.4, WAKE)])
    processor = make_session(db, engines, "adaptive")
    feed(processor, build_audio([
        ("silence", 0.5), ("speech", 0.8, 0), ("silence", 0.6),
        ("speech", 1.5, 1), ("silence", 0.3), ("speech", 1.2, 2), ("silence", 4),
    ]))
    assert turns(db)[0]["texts"] == [QUESTION, SLOW_SENTENCE]


def test_adaptive_without_consent_never_computes_voiceprint(db):
    engines = slow_elder_engines()
    processor = make_session(db, engines, "adaptive", consent="declined")
    feed(processor, build_audio(SLOW_ELDER_DIALOGUE + [("silence", 7)]))
    processor.stop()
    assert engines.embed_calls == 0
    assert turns(db)[0]["texts"][:2] == [QUESTION, SLOW_REPLY]


def test_adaptive_answer_keyword_flushes_pending_sentence(db):
    engines = FakeEngines(texts={0: WAKE, 1: QUESTION, 2: "小照請回答"},
                          kws_schedule=[(1.4, WAKE), (4.4, "小照請回答")])
    processor = make_session(db, engines, "adaptive")
    feed(processor, build_audio([
        ("silence", 0.5), ("speech", 0.8, 0), ("silence", 0.6),
        ("speech", 1.5, 1), ("silence", 0.3), ("speech", 1.0, 2), ("silence", 1),
    ]))
    result = turns(db)
    assert result[0]["end_reason"] == "answer_keyword"
    assert result[0]["texts"] == [QUESTION]
