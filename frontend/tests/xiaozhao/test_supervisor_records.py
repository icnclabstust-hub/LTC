"""督導修正、移句、確認與回聽音訊（2026-10-13 correction-and-cases-tabs P2）。"""

import json
import os
import wave

import pytest

from conftest import FakeEngines, build_audio, to_chunks
from test_api import as_user, bind_user, client, state  # noqa: F401
from test_records import make_record
from xiaozhao import services
from xiaozhao.backend import MockBackend
from xiaozhao.config import SAMPLE_RATE
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor

WAKE = "小照你好"


def make_session(db, engines, audio_dir):
    resident_id = db.execute(
        "INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at) VALUES (?, ?, ?, ?)",
        ("Ucare", "住民 A", "2026-10-01", now()),
    ).lastrowid
    db.execute(
        "INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
        " VALUES ('s1', ?, 'Ucare', ?, ?)", (resident_id, now(), now()),
    )
    return SessionProcessor("s1", resident_id, "Ucare", db, engines, MockBackend(), 0.9, audio_dir=audio_dir)


def feed(processor, parts):
    for seq, chunk in enumerate(to_chunks(build_audio(parts))):
        processor.accept_chunk(seq, chunk)


def wav_seconds(path):
    with wave.open(str(path)) as f:
        return f.getnframes() / f.getframerate()


# ---------- 輪內音訊 ----------

def test_turn_audio_keeps_only_in_turn_speech(db, tmp_path):
    engines = FakeEngines(texts={0: "輪外閒聊", 1: WAKE, 2: "藥要怎麼吃"}, kws_schedule=[(3.0, WAKE)])
    processor = make_session(db, engines, tmp_path)
    # 輪外閒聊 0～1.5 秒（不存）；喚醒詞 2.0～2.8；問題 3.4～4.9；之後靜音結束本輪
    feed(processor, [("speech", 1.5, 0), ("silence", 0.5), ("speech", 0.8, 1), ("silence", 0.6),
                     ("speech", 1.5, 2), ("silence", 4)])
    turn_id = db.one("SELECT id FROM turns")["id"]
    seconds = wav_seconds(tmp_path / f"{turn_id}.wav")
    assert seconds == pytest.approx(4.9 - 2.0, abs=0.05)  # 從輪的起點到最後一句結束
    doc = json.loads(db.one("SELECT doc FROM records")["doc"])
    assert doc["audio_start_ms"] == pytest.approx(2000, abs=50)  # 網頁逐句回聽的起點


def test_speech_after_answer_keyword_not_saved(db, tmp_path):
    engines = FakeEngines(texts={0: WAKE, 1: "藥要怎麼吃小照回答然後我們聊別的事情"}, kws_schedule=[(1.0, WAKE)])
    processor = make_session(db, engines, tmp_path)
    feed(processor, [("silence", 0.2), ("speech", 0.8, 0), ("silence", 0.6), ("speech", 4.0, 1), ("silence", 4)])
    turn_id = db.one("SELECT id FROM turns")["id"]
    # 輪從喚醒詞 0.2 秒開始；第二段 1.6～5.6 秒共 18 字，結束詞從第 5 字開始 → 約 2.71 秒
    # 存到 2.71 秒（約 2.51 秒長），結束詞之後的話（到 5.6 秒）不存
    assert wav_seconds(tmp_path / f"{turn_id}.wav") == pytest.approx(1.6 + 4.0 * 5 / 18 - 0.2, abs=0.05)


def test_no_audio_without_dir_or_without_sentences(db, tmp_path):
    engines = FakeEngines(texts={0: WAKE}, kws_schedule=[(1.0, WAKE)])
    processor = make_session(db, engines, tmp_path)
    feed(processor, [("silence", 0.2), ("speech", 0.8, 0), ("silence", 4)])  # 只有喚醒詞，沒有句子
    assert list(tmp_path.glob("*.wav")) == []
    assert db.one("SELECT COUNT(*) AS n FROM records")["n"] == 0


# ---------- 督導 API ----------

@pytest.fixture
def two_turns(db):
    first = make_record(db, 1, codes=("01", "02", "01"))
    second = make_record(db, 2, codes=("02", "01"))
    return first, second


def doc_of(db, seq):
    return json.loads(db.one("SELECT doc FROM records WHERE seq = ?", (seq,))["doc"])


def test_supervisor_endpoints_are_supervisor_only(client, db, two_turns):
    bind_user(db, "Ucare", "caregiver")
    bind_user(db, "Usup", "supervisor")
    seq = two_turns[0]
    calls = [("GET", "/api/supervisor/records", None), ("GET", f"/api/supervisor/records/{seq}/audio", None),
             ("PUT", f"/api/supervisor/records/{seq}", {"doc": doc_of(db, seq)}),
             ("POST", f"/api/supervisor/records/{seq}/move", {"i": 0, "direction": "next"}),
             ("POST", f"/api/supervisor/records/{seq}/confirm", {"names_checked": True})]
    for method, path, body in calls:
        assert client.request(method, path, json=body, headers=as_user("Ucare")).status_code == 403, path
    listed = client.get("/api/supervisor/records", headers=as_user("Usup")).json()
    assert {r["seq"] for r in listed} == set(two_turns) and all("has_audio" in r for r in listed)


def test_supervisor_merge_and_split_accepted(client, db, two_turns):
    bind_user(db, "Usup", "supervisor")
    seq = two_turns[0]
    doc = doc_of(db, seq)
    a, b, c = doc["sentences"]
    merged = {**a, "end_ms": b["end_ms"], "text": f"{a['text']}，{b['text']}", "source_idx": [0, 1]}
    split_1 = {**c, "end_ms": c["start_ms"] + 400, "text": "第2", "source_idx": [2]}
    split_2 = {**c, "start_ms": c["start_ms"] + 400, "text": "句（王阿姨→家屬甲）", "source_idx": [2]}
    doc["sentences"] = [merged, split_1, split_2]
    res = client.put(f"/api/supervisor/records/{seq}", json={"doc": doc}, headers=as_user("Usup"))
    assert res.status_code == 200
    assert [s["text"] for s in res.json()["doc"]["sentences"]] == ["第0句，第1句", "第2", "句（王阿姨→家屬甲）"]
    orig = json.loads(db.one("SELECT original FROM records WHERE seq = ?", (seq,))["original"])
    assert len(orig["sentences"]) == 3 and orig["sentences"][0]["text"] == "第0句"   # 原始輸出不變


@pytest.mark.parametrize("mutate", [
    lambda d: d["sentences"].clear(),
    lambda d: d["sentences"][0].update(text="  "),
    lambda d: d["sentences"][1].update(start_ms=-5),
    lambda d: d["sentences"].reverse(),
    lambda d: d["sentences"][0].update(speaker="09"),
    lambda d: d["sentences"][0].update(intent="不存在"),
    lambda d: d["speakers"].update({"01": "外星人"}),
])
def test_supervisor_invalid_doc_rejected(client, db, two_turns, mutate):
    bind_user(db, "Usup", "supervisor")
    seq = two_turns[0]
    doc = doc_of(db, seq)
    mutate(doc)
    assert client.put(f"/api/supervisor/records/{seq}", json={"doc": doc}, headers=as_user("Usup")).status_code == 400


def test_move_sentence_between_adjacent_turns(client, db, two_turns):
    bind_user(db, "Usup", "supervisor")
    first, second = two_turns
    move = lambda seq, i, d: client.post(f"/api/supervisor/records/{seq}/move", json={"i": i, "direction": d},
                                         headers=as_user("Usup"))
    assert move(first, 0, "next").status_code == 400          # 只有最後一句能移到下一輪
    assert move(first, 0, "prev").status_code == 400          # 沒有上一輪
    assert move(first, 2, "next").status_code == 200
    assert len(doc_of(db, first)["sentences"]) == 2
    assert doc_of(db, second)["sentences"][0]["text"] == "第2句"
    assert move(second, 0, "prev").status_code == 200         # 再移回去
    assert doc_of(db, first)["sentences"][-1]["text"] == "第2句"


def test_confirm_requires_name_check_deletes_audio_and_locks(client, db, settings, two_turns):
    bind_user(db, "Ucare", "caregiver")
    bind_user(db, "Usup", "supervisor")
    seq = two_turns[0]
    audio = settings.turn_audio_dir / "1.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(audio), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(SAMPLE_RATE); f.writeframes(b"\0\0" * 1600)
    res = client.get(f"/api/supervisor/records/{seq}/audio", headers=as_user("Usup"))
    assert res.status_code == 200 and res.headers["content-type"] == "audio/wav"

    confirm = lambda checked: client.post(f"/api/supervisor/records/{seq}/confirm", json={"names_checked": checked},
                                          headers=as_user("Usup"))
    assert confirm(False).status_code == 400
    res = confirm(True)
    assert res.json()["status"] == "confirmed" and res.json()["names_checked"] == 1
    assert not audio.exists()
    assert client.get(f"/api/supervisor/records/{seq}/audio", headers=as_user("Usup")).status_code == 404
    assert client.put(f"/api/supervisor/records/{seq}", json={"doc": doc_of(db, seq)},
                      headers=as_user("Usup")).status_code == 409
    assert client.patch(f"/api/records/{seq}", json={"speakers": {"01": "照護員"}},
                        headers=as_user("Ucare")).status_code == 409


def test_cleanup_removes_audio_after_seven_days(db, tmp_path):
    old, fresh = tmp_path / "1.wav", tmp_path / "2.wav"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    eight_days_ago = now() - 8 * 24 * 3600
    os.utime(old, (eight_days_ago, eight_days_ago))
    services.cleanup_expired(db, audio_dir=tmp_path)
    assert not old.exists() and fresh.exists()
