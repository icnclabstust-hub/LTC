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
    parts = split_keywords("很飽小趙回答小趙你好還有問題")
    assert [(k, t) for k, t, *_ in parts] == [("text", "很飽"), ("answer", "小趙回答"),
                                             ("wake", "小趙你好"), ("text", "還有問題")]


@pytest.mark.parametrize("mode", ["fixed", "pause_veto"])
def test_answer_then_wake_in_one_segment_ends_and_starts_turn(db, mode):
    engines = FakeEngines(texts={0: WAKE, 1: "藥要怎麼吃小照回答小照你好還有換藥的問題"}, kws_schedule=[(1.0, WAKE)])
    feed(make_session(db, engines, mode), WAKE_PART + [("speech", 4, 1), ("silence", 4)])
    assert turns(db) == [("answer_keyword", ["藥要怎麼吃"]), ("silence", ["還有換藥的問題"])]


def test_speech_after_answer_keyword_is_dropped(db):
    engines = FakeEngines(texts={0: WAKE, 1: "我的肚子很飽小照回答小照"}, kws_schedule=[(1.0, WAKE)])
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
    engines = FakeEngines(texts={0: WAKE, 1: "藥要怎麼吃小照回答小照你好還有換藥的問題", 2: "傷口要怎麼照顧"},
                          kws_schedule=[(1.0, WAKE), (6.3, "小照回答")])
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


def test_old_answer_keyword_does_not_end_turn(db):
    # 2026-10-15 結束詞改為「小照回答」：說「小照請回答」時，即使 KWS 誤報也不結束這一輪
    engines = FakeEngines(texts={0: WAKE, 1: "藥要怎麼吃小照請回答"}, kws_schedule=[(1.0, WAKE), (4.4, "小照回答")])
    feed(make_session(db, engines), WAKE_PART + [("speech", 3, 1), ("silence", 4)])
    assert turns(db) == [("silence", ["藥要怎麼吃小照請回答"])]


def test_new_answer_keyword_ends_turn(db):
    engines = FakeEngines(texts={0: WAKE, 1: "藥要怎麼吃小照回答"}, kws_schedule=[(1.0, WAKE), (4.4, "小照回答")])
    feed(make_session(db, engines), WAKE_PART + [("speech", 3, 1), ("silence", 4)])
    assert turns(db) == [("answer_keyword", ["藥要怎麼吃"])]


def test_old_answer_keyword_removed_before_backend(db):
    # kw3 實測：「小照請回答」留在逐字稿，但送後端的文字去掉
    text = "阿嬤說她想回家，小趙請回答。"
    engines = FakeEngines(texts={0: WAKE, 1: text}, intents={text.rstrip("。"): ("服務請求", 0.95)}, kws_schedule=[(1.0, WAKE)])
    processor = make_session(db, engines)
    sent = []
    query = processor._backend.query
    processor._backend.query = lambda payload: sent.append(payload["text"]) or query(payload)
    feed(processor, WAKE_PART + [("speech", 3, 1), ("silence", 4)])
    assert turns(db) == [("silence", ["阿嬤說她想回家，小趙請回答"])]
    assert sent and all(t == "阿嬤說她想回家" for t in sent)


@pytest.mark.parametrize("text", ["小趙雷答", "早上只吃了半碗粥小趙雷答"])
def test_misheard_answer_keyword_removed(db, text):
    # 2026-10-17：「小照回答」轉成「小趙雷答」，KWS 有聽到結束詞時一起刪掉
    engines = FakeEngines(texts={0: WAKE, 1: text}, kws_schedule=[(1.0, WAKE), (4.4, "小照回答")])
    feed(make_session(db, engines), WAKE_PART + [("speech", 3, 1), ("silence", 4)])
    expected = [t for t in [text.replace("小趙雷答", "")] if t]
    assert turns(db) == [("answer_keyword", expected)]


def test_late_answer_keyword_strips_trailing_words(db):
    # 2026-10-17 第三份文稿：KWS 較晚回報「小照回答」，那句已處理完，句尾留著「小壯回答」→ 結束本輪前刪掉
    engines = FakeEngines(texts={0: WAKE, 1: "附近的藥局今天沒開小壯回答"},
                          kws_schedule=[(1.0, WAKE), (5.2, "小照回答")])
    feed(make_session(db, engines), WAKE_PART + [("speech", 3, 1), ("silence", 4)])
    assert turns(db) == [("answer_keyword", ["附近的藥局今天沒開"])]


def test_misheard_wake_word_removed(db):
    # 2026-10-17：「小照你好」說成「小照雷好」，KWS 有聽到喚醒詞時一起刪掉
    engines = FakeEngines(texts={0: "小照雷好阿嬤今天血壓比較高"}, kws_schedule=[(1.2, WAKE)])
    feed(make_session(db, engines), [("silence", 0.2), ("speech", 3, 0), ("silence", 4)])
    assert turns(db) == [("silence", ["阿嬤今天血壓比較高"])]


def test_misheard_wake_only_first_sentence_dropped(db):
    engines = FakeEngines(texts={0: "小照雷好", 1: "阿嬤今天血壓比較高"}, kws_schedule=[(1.0, WAKE)])
    feed(make_session(db, engines), WAKE_PART + [("speech", 2, 1), ("silence", 4)])
    assert turns(db) == [("silence", ["阿嬤今天血壓比較高"])]
