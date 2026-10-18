"""給後端的已確認紀錄匯出與保存期限（2026-10-13 correction-and-cases-tabs P3）。"""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from conftest import FakeEngines
from test_records import make_record
from xiaozhao import services
from xiaozhao.app import State, create_app
from xiaozhao.db import now

TOKEN = "export-token-for-test"


@pytest.fixture
def export_client(settings, db):
    app = create_app(State(dataclasses.replace(settings, export_token=TOKEN), db, engines=FakeEngines(texts={})))
    with TestClient(app) as c:
        yield c


def confirm(db, seq):
    db.execute("UPDATE records SET status = 'confirmed', confirmed_at = ? WHERE seq = ?", (now(), seq))


def get(client, token=TOKEN, **params):
    return client.get("/api/export/records", params=params, headers={"Authorization": f"Bearer {token}"})


def test_export_requires_token(export_client):
    assert get(export_client, token="wrong").status_code == 401
    assert export_client.get("/api/export/records").status_code == 401


def test_export_disabled_without_token(settings, db):
    app = create_app(State(settings, db, engines=FakeEngines(texts={})))
    with TestClient(app) as c:
        assert c.get("/api/export/records", headers={"Authorization": "Bearer "}).status_code == 404


def test_only_confirmed_records_in_seq_order_with_paging(export_client, db):
    seqs = [make_record(db, turn_id) for turn_id in (1, 2, 3, 4)]
    for seq in (seqs[0], seqs[2], seqs[3]):
        confirm(db, seq)
    first = get(export_client, limit=2).json()
    assert [r["seq"] for r in first["records"]] == [seqs[0], seqs[2]]
    second = get(export_client, after_seq=first["next_after_seq"]).json()
    assert [r["seq"] for r in second["records"]] == [seqs[3]]
    assert get(export_client, after_seq=second["next_after_seq"]).json() == {"records": [], "next_after_seq": seqs[3]}


def test_export_payload_has_no_line_id_or_original(export_client, db):
    seq = make_record(db, 1)
    confirm(db, seq)
    record = get(export_client).json()["records"][0]
    assert set(record) == {"seq", "session_id", "resident_alias", "confirmed_at", "speakers", "sentences"}
    assert set(record["sentences"][0]) == {"start_ms", "end_ms", "speaker", "role", "text", "intent", "intent_owner"}
    assert "Ucare" not in str(record)


def test_exported_at_set_once(export_client, db):
    seq = make_record(db, 1)
    confirm(db, seq)
    get(export_client)
    first = db.one("SELECT exported_at FROM records WHERE seq = ?", (seq,))["exported_at"]
    get(export_client)
    assert first is not None and db.one("SELECT exported_at FROM records WHERE seq = ?", (seq,))["exported_at"] == first


def test_invalid_paging_rejected(export_client):
    assert get(export_client, after_seq=-1).status_code == 400
    assert get(export_client, limit=0).status_code == 400
    assert get(export_client, limit=501).status_code == 400


def test_confirmed_retention(db):
    day = 24 * 3600
    fresh, exported_old, exported_recent, never_exported_old = (make_record(db, t) for t in (1, 2, 3, 4))
    for seq in (fresh, exported_old, exported_recent, never_exported_old):
        confirm(db, seq)
    db.execute("UPDATE records SET exported_at = ? WHERE seq = ?", (now() - 31 * day, exported_old))
    db.execute("UPDATE records SET exported_at = ? WHERE seq = ?", (now() - 29 * day, exported_recent))
    db.execute("UPDATE records SET confirmed_at = ?, created_at = ? WHERE seq = ?",
               (now() - 91 * day, now() - 91 * day, never_exported_old))
    services.cleanup_expired(db)
    assert sorted(r["seq"] for r in db.all("SELECT seq FROM records")) == sorted([fresh, exported_recent])
