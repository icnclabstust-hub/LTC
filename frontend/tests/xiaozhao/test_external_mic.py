"""外接麥克風：自動放大與轉錯關鍵詞的寬鬆刪除（2026-10-14 external-mic 第 8 節）。"""

import numpy as np
import pytest

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao.autogain import MAX_GAIN_DB, AutoGain
from xiaozhao.backend import MockBackend
from xiaozhao.config import SAMPLE_RATE
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor

WAKE = "小照你好"


def feed_gain(gain, amplitude, seconds, speech=True):
    for _ in range(int(seconds * 2)):
        gain.observe(np.full(SAMPLE_RATE // 2, amplitude, dtype=np.float32), speech)


def test_quiet_speech_is_raised_to_target():
    gain = AutoGain()
    feed_gain(gain, 0.02, 1.5)  # 說話不到 2 秒：還不放大
    assert gain.gain_db == 0.0
    feed_gain(gain, 0.02, 1.0)  # -34 dBFS → 放大 16 dB
    assert gain.gain_db == pytest.approx(16.0, abs=0.5)
    out = gain.process(np.full(10, 0.02, dtype=np.float32))
    assert out[0] == pytest.approx(0.02 * 10 ** (16 / 20), rel=0.05)


def test_loud_speech_is_not_reduced_and_silence_is_ignored():
    gain = AutoGain()
    feed_gain(gain, 0.3, 3)
    assert gain.gain_db == 0.0
    quiet = AutoGain()
    feed_gain(quiet, 0.02, 10, speech=False)  # 沒有說話：不估計音量
    feed_gain(quiet, 0.0, 10)  # 全零（瀏覽器降噪）：不列入
    assert quiet.gain_db == 0.0


def test_gain_is_capped_and_output_clipped():
    gain = AutoGain()
    feed_gain(gain, 0.0005, 3)
    assert gain.gain_db == MAX_GAIN_DB
    assert gain.process(np.array([0.5, -0.5], dtype=np.float32)).tolist() == [1.0, -1.0]


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


def turns(db, parts, engines):
    processor = make_session(db, engines)
    for seq, chunk in enumerate(to_chunks(build_audio(parts))):
        processor.accept_chunk(seq, chunk)
    rows = db.all("SELECT id, end_reason FROM turns ORDER BY seq")
    return [(r["end_reason"], [u["text"] for u in db.all(
        "SELECT text FROM utterances WHERE turn_id = ? ORDER BY idx", (r["id"],))]) for r in rows]


def test_misrecognized_answer_keyword_at_end_is_removed(db):
    # 手機實測：KWS 聽到「小照回答」，ASR 轉成「小套回答」
    engines = FakeEngines(texts={0: WAKE, 1: "早上只吃了半碗粥小套回答"},
                          kws_schedule=[(1.0, WAKE), (4.4, "小照回答")])
    parts = [("silence", 0.2), ("speech", 0.8, 0), ("silence", 0.6), ("speech", 3, 1), ("silence", 4)]
    assert turns(db, parts, engines) == [("answer_keyword", ["早上只吃了半碗粥"])]


def test_misrecognized_wake_word_at_start_is_removed(db):
    # 外接麥克風實測：喚醒詞和後面的話連成一句，ASR 轉成「趙你好…」
    engines = FakeEngines(texts={0: "趙你好阿嬤今天早上血壓比較高"}, kws_schedule=[(1.2, WAKE)])
    parts = [("silence", 0.2), ("speech", 3, 0), ("silence", 4)]
    assert turns(db, parts, engines) == [("silence", ["阿嬤今天早上血壓比較高"])]


def test_answer_text_kept_without_kws(db):
    # KWS 沒聽到結束詞時不刪（避免把一般的話刪掉）
    engines = FakeEngines(texts={0: WAKE, 1: "請他回答"}, kws_schedule=[(1.0, WAKE)])
    parts = [("silence", 0.2), ("speech", 0.8, 0), ("silence", 0.6), ("speech", 2, 1), ("silence", 4)]
    assert turns(db, parts, engines) == [("silence", ["請他回答"])]


def test_wake_far_from_any_speech_is_ignored(db):
    # 電風扇：KWS 誤報喚醒詞，前 2 秒內 VAD 沒有切出語音 → 不開輪（external-mic 第 9 節 A）
    engines = FakeEngines(texts={0: "阿嬤吃飽了"}, kws_schedule=[(6.0, WAKE)])
    assert turns(db, [("speech", 1, 0), ("silence", 9)], engines) == []


def test_answer_keyword_ends_turn_while_noise_keeps_vad_in_speech(db):
    # 說完「小照回答」後噪音不斷（VAD 一直判定在說話）：最多等 1.5 秒就強制斷句並結束本輪（第 9 節 B）
    engines = FakeEngines(texts={0: WAKE, 1: "早上只吃了半碗粥小照回答"},
                          kws_schedule=[(1.0, WAKE), (4.5, "小照回答")])
    processor = make_session(db, engines)
    parts = [("silence", 0.2), ("speech", 0.8, 0), ("silence", 0.6), ("speech", 30, 1)]
    for seq, chunk in enumerate(to_chunks(build_audio(parts))[:16]):  # 只送到第 8 秒，噪音還沒停
        processor.accept_chunk(seq, chunk)
    row = db.one("SELECT id, end_reason FROM turns")
    assert row["end_reason"] == "answer_keyword"
    assert [u["text"] for u in db.all("SELECT text FROM utterances WHERE turn_id = ?", (row["id"],))] == ["早上只吃了半碗粥"]


@pytest.mark.parametrize("text", ["老趙你好", "照你好", "照你也好", "教你哈"])
def test_wake_only_first_sentence_is_not_kept(db, text):
    # 外接麥克風實測：只說「小照你好」，ASR 轉成「老趙你好」等，被當成這一輪的第一句（第 10 節）
    engines = FakeEngines(texts={0: text, 1: "阿嬤今天血壓比較高"}, kws_schedule=[(1.0, WAKE)])
    parts = [("silence", 0.2), ("speech", 0.8, 0), ("silence", 0.6), ("speech", 2, 1), ("silence", 4)]
    assert turns(db, parts, engines) == [("silence", ["阿嬤今天血壓比較高"])]


def test_greeting_later_in_turn_is_kept(db):
    engines = FakeEngines(texts={0: WAKE, 1: "阿嬤今天血壓比較高", 2: "阿公你好"}, kws_schedule=[(1.0, WAKE)])
    parts = [("silence", 0.2), ("speech", 0.8, 0), ("silence", 0.6), ("speech", 2, 1), ("silence", 0.6),
             ("speech", 1, 2), ("silence", 4)]
    assert turns(db, parts, engines) == [("silence", ["阿嬤今天血壓比較高", "阿公你好"])]
