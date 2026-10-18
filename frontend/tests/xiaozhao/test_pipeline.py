"""切輪狀態機、片段去重、預取、轉人工、聲紋同意分支（設計文件第 7 節單元測試）。"""

import numpy as np

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao.backend import MockBackend
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor, strip_keywords


def make_session(db, engines, consent=None, threshold=0.9):
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
    return SessionProcessor("s1", resident_id, "Ucare", db, engines, MockBackend(), threshold), resident_id


def feed(processor, audio):
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)


def turns(db):
    rows = db.all("SELECT * FROM turns ORDER BY seq")
    for row in rows:
        row["utterances"] = db.all("SELECT * FROM utterances WHERE turn_id = ? ORDER BY idx", (row["id"],))
    return rows


def test_strip_keywords_handles_homophones():
    assert strip_keywords("小趙你好，今天要洗澡嗎") == ("今天要洗澡嗎", True, False)
    assert strip_keywords("好的，小趙請回答") == ("好的", False, True)
    assert strip_keywords("小照你好") == ("", True, False)


def test_answer_keyword_ends_turn_and_keywords_not_transcribed(db):
    engines = FakeEngines(
        texts={0: "小照你好", 1: "阿嬤今天不想洗澡", 2: "小照請回答"},
        kws_schedule=[(1.6, "小照你好"), (5.6, "小照請回答")],
    )
    processor, _ = make_session(db, engines)
    feed(processor, build_audio([
        ("silence", 1), ("speech", 0.8, 0), ("silence", 0.5),
        ("speech", 1.5, 1), ("silence", 0.5), ("speech", 1.2, 2), ("silence", 1),
    ]))
    result = turns(db)
    assert len(result) == 1
    assert result[0]["end_reason"] == "answer_keyword"
    assert [u["text"] for u in result[0]["utterances"]] == ["阿嬤今天不想洗澡"]


def test_speech_outside_turn_is_dropped_and_silence_ends_turn(db):
    engines = FakeEngines(texts={0: "閒聊內容", 1: "小照你好", 2: "藥要怎麼吃"}, kws_schedule=[(3.5, "小照你好")])
    processor, _ = make_session(db, engines)
    feed(processor, build_audio([
        ("speech", 1, 0), ("silence", 1.5),           # 輪外語音：不轉文字
        ("speech", 0.8, 1), ("silence", 0.5),
        ("speech", 1, 2), ("silence", 4),              # 靜音 3 秒結束本輪
    ]))
    result = turns(db)
    assert len(result) == 1
    assert result[0]["end_reason"] == "silence"
    assert [u["text"] for u in result[0]["utterances"]] == ["藥要怎麼吃"]


def test_next_wake_word_starts_new_turn(db):
    engines = FakeEngines(
        texts={0: "小照你好", 1: "第一個問題", 2: "小照你好", 3: "第二個問題"},
        kws_schedule=[(0.9, "小照你好"), (3.0, "小照你好")],
    )
    processor, _ = make_session(db, engines)
    feed(processor, build_audio([
        ("speech", 0.8, 0), ("silence", 0.5), ("speech", 1, 1), ("silence", 0.5),
        ("speech", 0.8, 2), ("silence", 0.5), ("speech", 1, 3), ("silence", 0.5),
    ]))
    processor.stop()
    result = turns(db)
    assert [t["end_reason"] for t in result] == ["next_wake_word", "session_stop"]
    assert [[u["text"] for u in t["utterances"]] for t in result] == [["第一個問題"], ["第二個問題"]]


def test_chunks_are_deduplicated_and_reordered(db):
    engines = FakeEngines(texts={0: "小照你好", 1: "問題"}, kws_schedule=[(0.9, "小照你好")])
    processor, _ = make_session(db, engines)
    chunks = to_chunks(build_audio([("speech", 0.8, 0), ("silence", 0.5), ("speech", 1, 1), ("silence", 1)]))
    assert processor.accept_chunk(1, chunks[1])["acked_seq"] == -1   # 先到的後段片段先暫存
    assert processor.accept_chunk(0, chunks[0])["acked_seq"] == 1
    assert processor.accept_chunk(0, chunks[0])["acked_seq"] == 1    # 重送不重複處理
    for seq in range(2, len(chunks)):
        processor.accept_chunk(seq, chunks[seq])
    processor.accept_chunk(2, chunks[2])
    processor.stop()
    assert [u["text"] for u in turns(db)[0]["utterances"]] == ["問題"]


def test_prefetch_once_per_intent_and_reused_at_turn_end(db):
    question = "機構可以幫忙代購嗎"
    engines = FakeEngines(
        texts={0: "小照你好", 1: question, 2: question},
        intents={question: ("規範詢問", 0.97)},
        kws_schedule=[(0.9, "小照你好")],
    )
    processor, _ = make_session(db, engines)
    calls = {"query": 0, "scene": 0}
    backend = processor._backend
    original_query, original_scene = backend.query, backend.scene
    backend.query = lambda p: calls.__setitem__("query", calls["query"] + 1) or original_query(p)
    backend.scene = lambda p: calls.__setitem__("scene", calls["scene"] + 1) or original_scene(p)
    feed(processor, build_audio([
        ("speech", 0.8, 0), ("silence", 0.5), ("speech", 1, 1), ("silence", 0.5), ("speech", 1, 2), ("silence", 4),
    ]))
    turn = turns(db)[0]
    # 同一意圖只預取一次並在輪結束時沿用（不再送 query）；整輪紀錄每輪送一次（multi-intent-turn）
    assert calls == {"query": 1, "scene": 1}
    assert turn["answer_status"] == "answered"
    assert turn["final_intent"] == "規範詢問"


def test_medical_question_goes_to_supervisor(db):
    question = "阿公的藥可以磨粉嗎"
    engines = FakeEngines(
        texts={0: "小照你好", 1: question}, intents={question: ("規範詢問", 0.5)}, kws_schedule=[(0.9, "小照你好")],
    )
    processor, _ = make_session(db, engines)
    feed(processor, build_audio([("speech", 0.8, 0), ("silence", 0.5), ("speech", 1, 1), ("silence", 4)]))
    assert turns(db)[0]["answer_status"] == "awaiting_human"
    request = db.one("SELECT * FROM supervisor_requests")
    assert request["source"] == "turn" and request["question"] == question


def test_anonymous_speaker_codes_and_role_mapping_within_session(db):
    # 說話人 3 說第 1、3 句，說話人 5 說第 2 句；不保存聲紋，代號只在本次錄音內有效
    engines = FakeEngines(
        texts={0: "小照你好", 1: "我來幫你量血壓", 2: "好啊", 3: "小照你好", 4: "等一下要吃飯"},
        speakers={1: 3, 2: 5, 4: 3},
        kws_schedule=[(0.9, "小照你好"), (5.5, "小照你好")],
    )
    processor, _ = make_session(db, engines, consent="agreed")
    chunks = to_chunks(build_audio([("speech", 0.8, 0), ("silence", 0.5), ("speech", 1, 1), ("silence", 0.5),
                                    ("speech", 1, 2), ("silence", 0.8)]))
    for seq, chunk in enumerate(chunks):
        processor.accept_chunk(seq, chunk)
    first = turns(db)[0]
    assert [(u["speaker_label"], u["speaker_role"]) for u in first["utterances"]] == [("01", "未辨識"), ("02", "未辨識")]
    processor.map_speaker("01", "照護員")

    rest = to_chunks(build_audio([("speech", 0.8, 3), ("silence", 0.5), ("speech", 1, 4), ("silence", 0.5)]))
    for offset, chunk in enumerate(rest):
        processor.accept_chunk(len(chunks) + offset, chunk)
    processor.stop()
    second = turns(db)[1]["utterances"][0]
    assert (second["speaker_label"], second["speaker_role"]) == ("01", "照護員")
    assert "voiceprints" not in {r["name"] for r in db.all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    record = db.one("SELECT doc FROM records WHERE turn_id = ?", (turns(db)[1]["id"],))
    assert '"01": "照護員"' in record["doc"]


def test_declined_consent_never_computes_voiceprint(db):
    engines = FakeEngines(texts={0: "小照你好", 1: "問題"}, kws_schedule=[(0.9, "小照你好")])
    processor, _ = make_session(db, engines, consent="declined")
    feed(processor, build_audio([("speech", 0.8, 0), ("silence", 0.5), ("speech", 1, 1), ("silence", 4)]))
    turn = turns(db)[0]
    assert engines.embed_calls == 0
    assert turn["utterances"][0]["speaker_label"] is None
    assert '"speakers": {}' in db.one("SELECT doc FROM records")["doc"]


def test_latency_logged_without_text(db):
    engines = FakeEngines(texts={0: "小照你好", 1: "問題"}, kws_schedule=[(0.9, "小照你好")])
    processor, _ = make_session(db, engines)
    feed(processor, build_audio([("speech", 0.8, 0), ("silence", 0.5), ("speech", 1, 1), ("silence", 4)]))
    row = db.one("SELECT * FROM latency_logs")
    assert row["asr_ms"] is not None and row["intent_ms"] is not None
    assert all(not isinstance(v, str) or v == "s1" for v in row.values())
    assert np.isfinite(row["asr_ms"])


def test_short_pause_does_not_split_sentence(db):
    # 2026-10-12 手機實測：「請問我可以幫您｜量血壓嗎」被句中停頓切開，VAD 靜音改 0.5 秒
    engines = FakeEngines(texts={0: "小照你好", 1: "請問我可以幫您量血壓嗎"}, kws_schedule=[(0.9, "小照你好")])
    processor, _ = make_session(db, engines)
    feed(processor, build_audio([
        ("speech", 0.8, 0), ("silence", 0.7), ("speech", 1, 1), ("silence", 0.3), ("speech", 0.8, 1), ("silence", 4),
    ]))
    assert [u["text"] for u in turns(db)[0]["utterances"]] == ["請問我可以幫您量血壓嗎"]


def test_report_auto_stops_after_silence_only_once_speech_started():
    from xiaozhao.pipeline import ReportRecorder

    engines = FakeEngines(texts={0: "今天住民 A 精神不錯", 1: "住民 B 午餐吃得少"})
    recorder = ReportRecorder("r1", "Ucare", engines)
    results = [recorder.accept_chunk(seq, chunk) for seq, chunk in enumerate(to_chunks(build_audio([
        ("silence", 4), ("speech", 1.5, 0), ("silence", 1), ("speech", 1.5, 1), ("silence", 3.5),
    ])))]
    flags = [r["auto_stop"] for r in results]
    assert not any(flags[:16])          # 開始說話前的 4 秒靜音不算
    assert flags[-1] is True
    assert recorder.finish() == "今天住民 A 精神不錯\n住民 B 午餐吃得少"


def test_strip_keywords_handles_dao_misrecognition():
    # 2026-10-13 tk03：只轉開頭 2 秒時「小趙你好」被轉成「小道你好」
    assert strip_keywords("小道你好陳奶奶") == ("陳奶奶", True, False)
