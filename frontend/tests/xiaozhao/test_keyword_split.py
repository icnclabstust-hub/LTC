"""片段中間的喚醒詞／結束詞依位置切輪（2026-10-13 keyword-split-turns 設計文件第 5 節單元測試）。"""

import pytest

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao.backend import MockBackend
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor, split_keywords

WAKE = "小照你好"
WAKE_PART = [("silence", 0.2), ("speech", 0.8, 0), ("silence", 0.6)]  # 喚醒詞，KWS 於 1.0 秒回報


def make_session(db, engines, mode="fixed", keyword_split=True):
    resident_id = db.execute(
        "INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at) VALUES (?, ?, ?, ?)",
        ("Ucare", "住民 A", "2026-10-01", now()),
    ).lastrowid
    db.execute(
        "INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
        " VALUES ('s1', ?, 'Ucare', ?, ?)", (resident_id, now(), now()),
    )
    return SessionProcessor("s1", resident_id, "Ucare", db, engines, MockBackend(), 0.9, mode, keyword_split)


def feed(processor, parts):
    for seq, chunk in enumerate(to_chunks(build_audio(parts))):
        processor.accept_chunk(seq, chunk)


def turns(db):
    rows = db.all("SELECT id, end_reason FROM turns ORDER BY seq")
    return [(r["end_reason"], [u["text"] for u in db.all(
        "SELECT text FROM utterances WHERE turn_id = ? ORDER BY idx", (r["id"],))]) for r in rows]


def test_split_keywords_keeps_order_and_homophones():
    parts = split_keywords("很飽小趙請回答小趙你好還有問題")
    assert [(k, t) for k, t, *_ in parts] == [("text", "很飽"), ("answer", "小趙請回答"),
                                             ("wake", "小趙你好"), ("text", "還有問題")]


@pytest.mark.parametrize("mode", ["fixed", "pause_veto"])
def test_answer_then_wake_in_one_segment_ends_and_starts_turn(db, mode):
    engines = FakeEngines(texts={0: WAKE, 1: "藥要怎麼吃小照請回答小照你好還有換藥的問題"}, kws_schedule=[(1.0, WAKE)])
    feed(make_session(db, engines, mode), WAKE_PART + [("speech", 4, 1), ("silence", 4)])
    assert turns(db) == [("answer_keyword", ["藥要怎麼吃"]), ("silence", ["還有換藥的問題"])]


def test_speech_after_answer_keyword_is_dropped(db):
    engines = FakeEngines(texts={0: WAKE, 1: "我的肚子很飽小照請回答小照"}, kws_schedule=[(1.0, WAKE)])
    feed(make_session(db, engines), WAKE_PART + [("speech", 3, 1), ("silence", 4)])
    assert turns(db) == [("answer_keyword", ["我的肚子很飽"])]


def test_wake_inside_turn_segment_splits_old_and_new_turn(db):
    engines = FakeEngines(texts={0: WAKE, 1: "先量血壓小照你好那等一下要洗澡嗎"}, kws_schedule=[(1.0, WAKE)])
    feed(make_session(db, engines), WAKE_PART + [("speech", 3, 1), ("silence", 4)])
    assert turns(db) == [("next_wake_word", ["先量血壓"]), ("silence", ["那等一下要洗澡嗎"])]


def test_wake_in_middle_of_out_of_turn_segment_drops_preceding_speech(db):
    # 輪外：「我幫他泡了營養素（3 秒）小照你好這樣啊」，KWS 在 4.0 秒回報
    engines = FakeEngines(texts={0: "我幫他泡了營養素小照你好這樣啊你最近吃很少"}, kws_schedule=[(4.0, WAKE)])
    feed(make_session(db, engines), [("silence", 0.5), ("speech", 6, 0), ("silence", 4)])
    assert turns(db) == [("silence", ["這樣啊你最近吃很少"])]


def test_late_kws_report_of_handled_keyword_is_ignored(db):
    # 結束詞已由文字處理；KWS 在片段結束後 0.7 秒才回報，不得再結束第 2 輪
    engines = FakeEngines(texts={0: WAKE, 1: "藥要怎麼吃小照請回答小照你好還有換藥的問題", 2: "傷口要怎麼照顧"},
                          kws_schedule=[(1.0, WAKE), (6.3, "小照請回答")])
    feed(make_session(db, engines), WAKE_PART + [("speech", 4, 1), ("silence", 1.0), ("speech", 1.5, 2), ("silence", 4)])
    assert turns(db) == [("answer_keyword", ["藥要怎麼吃"]), ("silence", ["還有換藥的問題", "傷口要怎麼照顧"])]


def test_kws_wake_without_keyword_in_text_falls_back_to_segment_level(db):
    # ASR 把喚醒詞轉錯（文字裡沒有關鍵詞），KWS 有回報 → 沿用整段作法：從片段開頭開新輪；
    # 開頭轉錯的喚醒詞寬鬆刪除（2026-10-14 external-mic 第 8 節）
    engines = FakeEngines(texts={0: WAKE, 1: "第一個問題", 2: "小草你好第二個問題"},
                          kws_schedule=[(1.0, WAKE), (4.5, WAKE)])
    feed(make_session(db, engines), WAKE_PART + [("speech", 1.2, 1), ("silence", 0.6), ("speech", 2, 2), ("silence", 4)])
    assert turns(db) == [("next_wake_word", ["第一個問題"]), ("silence", ["第二個問題"])]


def test_keyword_split_off_keeps_segment_level_behavior(db):
    engines = FakeEngines(texts={0: "我幫他泡了營養素小照你好這樣啊你最近吃很少"}, kws_schedule=[(4.0, WAKE)])
    feed(make_session(db, engines, keyword_split=False), [("silence", 0.5), ("speech", 6, 0), ("silence", 4)])
    assert turns(db) == [("silence", ["我幫他泡了營養素這樣啊你最近吃很少"])]
