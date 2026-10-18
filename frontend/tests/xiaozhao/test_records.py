"""確認／修正紀錄（2026-10-13 correction-and-cases-tabs P1：照護員）。"""

import json

from test_api import as_user, bind_user, client, state  # noqa: F401
from xiaozhao import services
from xiaozhao.db import now


def make_record(db, turn_id=1, session_id="s1", caregiver="Ucare", codes=("01", "02", "01")):
    if db.one("SELECT 1 FROM residents WHERE id = 1") is None:
        db.execute("INSERT INTO residents (id, caregiver_line_user_id, alias, created_at) VALUES (1, ?, '住民 A', ?)",
                   (caregiver, now()))
    utts = [{"idx": i, "start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": f"第{i}句", "speaker_label": c,
             "speaker_role": "未辨識", "intent": "資訊告知或交接"} for i, c in enumerate(codes)]
    return services.create_record(db, turn_id, session_id, caregiver, 1, utts, {})


def test_record_created_with_original_kept(db):
    seq = make_record(db)
    row = db.one("SELECT * FROM records WHERE seq = ?", (seq,))
    assert row["status"] == "draft" and row["resident_alias"] == "住民 A"
    assert json.loads(row["doc"]) == json.loads(row["original"])
    assert json.loads(row["doc"])["speakers"] == {"01": "未辨識", "02": "未辨識"}


def test_mapping_code_updates_sentences_and_other_turns_in_session(client, db):
    bind_user(db, "Ucare", "caregiver")
    first, second, other_session = make_record(db, 1), make_record(db, 2), make_record(db, 3, session_id="s2")
    res = client.patch(f"/api/records/{first}", json={"speakers": {"01": "照護員"}}, headers=as_user("Ucare"))
    assert res.status_code == 200
    assert [s["role"] for s in res.json()["doc"]["sentences"]] == ["照護員", "未辨識", "照護員"]
    doc = lambda seq: json.loads(db.one("SELECT doc FROM records WHERE seq = ?", (seq,))["doc"])
    assert doc(second)["speakers"]["01"] == "照護員"          # 同一次錄音的其他輪一起套用
    assert doc(other_session)["speakers"]["01"] == "未辨識"   # 其他次錄音不受影響
    orig = json.loads(db.one("SELECT original FROM records WHERE seq = ?", (first,))["original"])
    assert orig["speakers"]["01"] == "未辨識"                 # 系統原始輸出不變


def test_manual_sentence_role_not_overwritten_by_mapping(client, db):
    bind_user(db, "Ucare", "caregiver")
    seq = make_record(db)
    client.patch(f"/api/records/{seq}", json={"sentences": [{"i": 2, "role": "家屬", "text": "改過的字",
                                                             "intent": "服務請求"}]}, headers=as_user("Ucare"))
    res = client.patch(f"/api/records/{seq}", json={"speakers": {"01": "照護員"}}, headers=as_user("Ucare"))
    s = res.json()["doc"]["sentences"][2]
    assert (s["role"], s["text"], s["intent"]) == ("家屬", "改過的字", "服務請求")


def test_invalid_edits_rejected(client, db):
    bind_user(db, "Ucare", "caregiver")
    seq = make_record(db)
    bad = [{"speakers": {"09": "照護員"}}, {"speakers": {"01": "外星人"}}, {"sentences": [{"i": 5, "text": "x"}]},
           {"sentences": [{"i": 0, "intent": "不存在的意圖"}]}, {"sentences": [{"i": 0, "text": "  "}]}]
    for body in bad:
        assert client.patch(f"/api/records/{seq}", json=body, headers=as_user("Ucare")).status_code == 400, body


def test_other_caregiver_cannot_see_or_edit(client, db):
    bind_user(db, "Ucare", "caregiver")
    bind_user(db, "Uother", "caregiver")
    seq = make_record(db)
    assert client.get("/api/records", headers=as_user("Uother")).json() == []
    assert client.patch(f"/api/records/{seq}", json={"speakers": {"01": "照護員"}},
                        headers=as_user("Uother")).status_code == 404
    assert client.post(f"/api/records/{seq}/check", headers=as_user("Uother")).status_code == 404


def test_check_then_confirmed_record_is_locked(client, db):
    bind_user(db, "Ucare", "caregiver")
    seq = make_record(db)
    res = client.post(f"/api/records/{seq}/check", headers=as_user("Ucare"))
    assert res.json()["status"] == "caregiver_checked"
    db.execute("UPDATE records SET status = 'confirmed' WHERE seq = ?", (seq,))
    assert client.patch(f"/api/records/{seq}", json={"speakers": {"01": "照護員"}},
                        headers=as_user("Ucare")).status_code == 409


# ---------- 我的案件（P4） ----------

def test_my_supervisor_requests_only_own(client, db):
    bind_user(db, "Ucare", "caregiver")
    bind_user(db, "Uother", "caregiver")
    bind_user(db, "Usup", "supervisor")
    mine = services.create_supervisor_request(db, "Ucare", "turn", 1, "藥要怎麼吃", "用藥轉人工", None)
    services.create_supervisor_request(db, "Uother", "line_case", 2, "別人的問題", None, None)
    services.reply_supervisor_request(db, mine, "請依醫囑", "督導甲")
    rows = client.get("/api/my/supervisor-requests", headers=as_user("Ucare")).json()
    assert [(r["question"], r["status"], r["reply"]) for r in rows] == [("藥要怎麼吃", "replied", "請依醫囑")]
    assert client.get("/api/my/supervisor-requests", headers=as_user("Usup")).status_code == 403


def test_my_transcripts_include_answer_while_turn_exists(client, db):
    bind_user(db, "Ucare", "caregiver")
    db.execute("INSERT INTO residents (id, caregiver_line_user_id, alias, created_at) VALUES (1, 'Ucare', '住民 A', ?)", (now(),))
    db.execute("INSERT INTO sessions VALUES ('s1', 1, 'Ucare', ?, ?, ?)", (now(), now(), now()))
    db.execute("INSERT INTO turns (id, session_id, seq, started_at, answer, answer_status)"
               " VALUES (7, 's1', 1, ?, '小照的回答', 'answered')", (now(),))
    make_record(db, 7)
    make_record(db, 99)  # 原始輪次已刪除：沒有回答
    rows = client.get("/api/records", headers=as_user("Ucare")).json()
    assert {r["turn_id"]: r["answer"] for r in rows} == {7: "小照的回答", 99: None}
