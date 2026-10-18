"""本機 SQLite（設計文件 5.4 節資料模型）。

單一連線＋鎖：服務只在提案學生筆電上執行，同時錄音 session 以 1～3 個為目標。
時間一律存 Unix 秒（float）。
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('caregiver', 'supervisor')),
    line_user_id  TEXT UNIQUE,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'disabled')),
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS binding_codes (
    code_hash   TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    expires_at  REAL NOT NULL,
    used_at     REAL
);
CREATE TABLE IF NOT EXISTS residents (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    caregiver_line_user_id TEXT NOT NULL,
    alias                  TEXT NOT NULL,
    written_consent_date   TEXT,
    created_at             REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_consents (
    resident_id INTEGER PRIMARY KEY REFERENCES residents(id) ON DELETE CASCADE,
    decision    TEXT NOT NULL CHECK (decision IN ('agreed', 'declined')),
    decided_at  REAL NOT NULL
);
-- 2026-10-13 起不再長期保存聲紋（改為每次錄音內匿名代號）；舊資料庫的聲紋在啟動時刪除
DROP TABLE IF EXISTS voiceprints;
CREATE TABLE IF NOT EXISTS sessions (
    id                   TEXT PRIMARY KEY,
    resident_id          INTEGER NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
    caregiver_line_user_id TEXT NOT NULL,
    started_at           REAL NOT NULL,
    ended_at             REAL,
    consent_confirmed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    started_at     REAL NOT NULL,
    ended_at       REAL,
    end_reason     TEXT CHECK (end_reason IN ('answer_keyword', 'silence', 'next_wake_word', 'session_stop')),
    answer         TEXT,
    answer_status  TEXT,
    final_intent   TEXT,
    prefetch_intents TEXT,
    intents        TEXT  -- JSON：每個意圖的依據句子與回答（2026-10-13 multi-intent-turn）
);
CREATE TABLE IF NOT EXISTS utterances (
    turn_id        INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    idx            INTEGER NOT NULL,
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    text           TEXT NOT NULL,
    speaker_role   TEXT NOT NULL,
    speaker_label  TEXT,
    speaker_source TEXT CHECK (speaker_source IN ('voiceprint', 'manual')),
    intent         TEXT,
    intent_conf    REAL,
    is_question    INTEGER NOT NULL DEFAULT 0,  -- 問句（2026-10-14 question-window）
    window_id      INTEGER,                     -- 一輪內的問句 window 編號；未啟用時為 NULL
    intent_owner   TEXT,                        -- 意圖歸屬（N0 輸出，僅供參考；2026-10-14 n0-intent-model）
    PRIMARY KEY (turn_id, idx)
);
CREATE TABLE IF NOT EXISTS text_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    resident_id INTEGER NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
    caregiver_line_user_id TEXT NOT NULL,
    question    TEXT NOT NULL,
    answer      TEXT,
    status      TEXT NOT NULL,
    reply_ms    REAL,  -- 送出到收到後端回覆的毫秒數（2026-10-14 async-backend）
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS line_cases (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    line_message_id        TEXT UNIQUE,
    caregiver_line_user_id TEXT NOT NULL,
    question               TEXT NOT NULL,
    answer                 TEXT,
    status                 TEXT NOT NULL,
    notify_failed          INTEGER NOT NULL DEFAULT 0,
    reply_ms               REAL,  -- 送出到收到後端回覆的毫秒數（2026-10-14 async-backend）
    created_at             REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS supervisor_requests (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    caregiver_line_user_id TEXT NOT NULL,
    source               TEXT NOT NULL CHECK (source IN ('turn', 'line_case', 'text_question')),
    source_id            INTEGER NOT NULL,
    question             TEXT NOT NULL,
    reason               TEXT,
    status               TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'replied')),
    reply                TEXT,
    replied_by           TEXT,
    replied_at           REAL,
    backend_request_id   TEXT,
    intent               TEXT,  -- 來源為錄音的一輪時，是哪一個意圖轉督導（2026-10-13 multi-intent-turn）
    created_at           REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_reports (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    caregiver_line_user_id TEXT NOT NULL,
    report_date            TEXT NOT NULL,
    transcript             TEXT NOT NULL,
    created_at             REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS latency_logs (
    session_id       TEXT NOT NULL,
    utterance_seq    INTEGER NOT NULL,
    upload_ms        REAL,
    kws_ms           REAL,
    vad_ms           REAL,
    asr_ms           REAL,
    speaker_ms       REAL,
    intent_ms        REAL,
    request_build_ms REAL,
    backend_ms       REAL,
    created_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_names (
    line_user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at   REAL NOT NULL
);
-- 確認／修正紀錄（2026-10-13 correction-and-cases-tabs）：每輪一筆，seq 為給後端的流水編號。
-- 不以外鍵連到 turns：原始輪次 24 小時後刪除，紀錄依自己的期限保存。
-- doc／original 為 JSON：{"speakers": {"01": "照護員"}, "sentences": [{start_ms, end_ms, speaker, role,
--   role_manual, text, intent, intent_owner, source_idx}]}；original 為系統原始輸出，供訓練比對。
CREATE TABLE IF NOT EXISTS records (
    seq                    INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id                INTEGER UNIQUE,
    session_id             TEXT NOT NULL,
    caregiver_line_user_id TEXT NOT NULL,
    resident_alias         TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'caregiver_checked', 'confirmed')),
    doc                    TEXT NOT NULL,
    original               TEXT NOT NULL,
    names_checked          INTEGER NOT NULL DEFAULT 0,
    caregiver_checked_at   REAL,
    confirmed_by           TEXT,
    confirmed_at           REAL,
    exported_at            REAL,
    created_at             REAL NOT NULL,
    updated_at             REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id    TEXT PRIMARY KEY,
    received_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            # 舊資料庫補欄位（CREATE TABLE IF NOT EXISTS 不會改既有的表）
            self._add_column("turns", "intents", "TEXT")
            self._add_column("supervisor_requests", "intent", "TEXT")
            self._add_column("utterances", "is_question", "INTEGER NOT NULL DEFAULT 0")
            self._add_column("utterances", "window_id", "INTEGER")
            self._add_column("utterances", "intent_owner", "TEXT")
            self._add_column("text_questions", "reply_ms", "REAL")
            self._add_column("line_cases", "reply_ms", "REAL")

    def _add_column(self, table: str, column: str, decl: str) -> None:
        if column not in {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @contextmanager
    def transaction(self):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def now() -> float:
    return time.time()
