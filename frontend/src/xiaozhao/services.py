"""業務邏輯：帳號與綁定碼、督導請求、聲紋儲存、保存期限清除。

與 HTTP、LINE 無關，方便單元測試。
"""

import hashlib
import json
import logging
import secrets
import time
from pathlib import Path

from .config import (
    BINDING_CODE_TTL_SECONDS,
    INTENT_LABELS,
    RECORD_AFTER_EXPORT_TTL_SECONDS,
    RECORD_CONFIRMED_MAX_TTL_SECONDS,
    RECORD_TTL_SECONDS,
    RECORD_UNCONFIRMED_TTL_SECONDS,
    ROLES_SPEAKER,
)
from .db import Database, now

logger = logging.getLogger(__name__)

# 綁定碼字元：去掉容易看錯的 0/O、1/I/L
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 8


class ServiceError(Exception):
    status_code = 400


class NotFound(ServiceError):
    status_code = 404


class Forbidden(ServiceError):
    status_code = 403


class Conflict(ServiceError):
    status_code = 409


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


# ---------- 帳號與綁定 ----------
# identity（identity.BackendIdentity）有值時以後端為準、本機 accounts 只當快取（BACKEND_MODE=http）；
# 為 None 時（mock 模式、單元測試）維持本機帳號與綁定碼。

def create_account(db: Database, display_name: str, role: str, identity=None) -> tuple[dict, str]:
    if role not in ("caregiver", "supervisor"):
        raise ServiceError("身分只能是 caregiver 或 supervisor")
    display_name = display_name.strip()
    if not display_name:
        raise ServiceError("姓名不可空白")
    if identity is not None:
        user = identity.create_user(display_name, role)
        issued = identity.issue_code(user["user_id"])
        cur = db.execute(
            "INSERT INTO accounts (display_name, role, be_user_id, pseudonym, code_expires_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (display_name, role, user["user_id"], user["pseudonym"], _iso_to_epoch(issued["expires_at"]), now()),
        )
        return db.one("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)), issued["code"]
    cur = db.execute(
        "INSERT INTO accounts (display_name, role, created_at) VALUES (?, ?, ?)", (display_name, role, now())
    )
    code = issue_binding_code(db, cur.lastrowid)
    return db.one("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)), code


def _iso_to_epoch(value: str) -> float:
    from datetime import datetime, timezone

    # 後端以 UTC naive datetime 回傳
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def issue_binding_code(db: Database, account_id: int, identity=None) -> str:
    """產生一次性綁定碼，只存雜湊值；同帳號舊的未使用綁定碼一併作廢。"""
    account = db.one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if account is None:
        raise NotFound("帳號不存在")
    if account["status"] == "disabled":
        raise Conflict("帳號已停用")
    if identity is not None:
        if not account["be_user_id"]:
            raise Conflict("這個帳號建立於串接後端之前，請停用後重新建立")
        issued = identity.issue_code(account["be_user_id"])
        db.execute("UPDATE accounts SET code_expires_at = ? WHERE id = ?",
                   (_iso_to_epoch(issued["expires_at"]), account_id))
        return issued["code"]
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
    with db.transaction() as conn:
        conn.execute("DELETE FROM binding_codes WHERE account_id = ? AND used_at IS NULL", (account_id,))
        conn.execute(
            "INSERT INTO binding_codes (code_hash, account_id, expires_at) VALUES (?, ?, ?)",
            (_hash_code(code), account_id, now() + BINDING_CODE_TTL_SECONDS),
        )
    return code


def bind(db: Database, code: str, line_user_id: str, identity=None) -> dict:
    if identity is not None:
        if active_account(db, line_user_id) is not None:
            raise Conflict("這個 LINE 帳號已經綁定過")
        return _cache_backend_user(db, identity.bind(code, line_user_id), line_user_id)
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM binding_codes WHERE code_hash = ?", (_hash_code(code),)).fetchone()
        if row is None or row["used_at"] is not None or row["expires_at"] < now():
            raise ServiceError("綁定碼錯誤、已使用或已過期")
        if conn.execute("SELECT 1 FROM accounts WHERE line_user_id = ?", (line_user_id,)).fetchone():
            raise Conflict("這個 LINE 帳號已經綁定過")
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (row["account_id"],)).fetchone()
        if account["status"] == "disabled":
            raise Conflict("帳號已停用")
        conn.execute("UPDATE binding_codes SET used_at = ? WHERE code_hash = ?", (now(), row["code_hash"]))
        conn.execute(
            "UPDATE accounts SET line_user_id = ?, status = 'active' WHERE id = ?", (line_user_id, account["id"])
        )
    return db.one("SELECT * FROM accounts WHERE id = ?", (account["id"],))


def _cache_backend_user(db: Database, user: dict, line_user_id: str) -> dict:
    """把後端的使用者寫進本機快取；後端允許停用後以同一 LINE 帳號改綁，舊的本機列要先讓出 line_user_id。"""
    with db.transaction() as conn:
        conn.execute("UPDATE accounts SET line_user_id = NULL WHERE line_user_id = ? AND be_user_id IS NOT ?",
                     (line_user_id, user["user_id"]))
        row = conn.execute("SELECT id FROM accounts WHERE be_user_id = ?", (user["user_id"],)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO accounts (display_name, role, line_user_id, status, be_user_id, pseudonym, created_at)"
                " VALUES (?, ?, ?, 'active', ?, ?, ?)",
                (user["name"], user["role"], line_user_id, user["user_id"], user["pseudonym"], now()),
            )
        else:
            conn.execute(
                "UPDATE accounts SET display_name = ?, role = ?, line_user_id = ?, status = 'active', pseudonym = ?,"
                " code_expires_at = NULL WHERE id = ?",
                (user["name"], user["role"], line_user_id, user["pseudonym"], row["id"]),
            )
    return db.one("SELECT * FROM accounts WHERE be_user_id = ?", (user["user_id"],))


def disable_account(db: Database, account_id: int, identity=None) -> dict:
    account = db.one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if account is None:
        raise NotFound("帳號不存在")
    if identity is not None and account["be_user_id"]:
        identity.disable(account["be_user_id"])
    with db.transaction() as conn:
        conn.execute("UPDATE accounts SET status = 'disabled', code_expires_at = NULL WHERE id = ?", (account_id,))
        conn.execute("DELETE FROM binding_codes WHERE account_id = ? AND used_at IS NULL", (account_id,))
    return db.one("SELECT * FROM accounts WHERE id = ?", (account_id,))


def active_account(db: Database, line_user_id: str, identity=None) -> dict | None:
    """先查本機快取；查不到且有串接後端時問後端（例如在別台 FE 綁定、或本機資料庫重建過）。"""
    account = db.one("SELECT * FROM accounts WHERE line_user_id = ? AND status = 'active'", (line_user_id,))
    if account is not None or identity is None:
        return account
    user = identity.lookup(line_user_id)
    return _cache_backend_user(db, user, line_user_id) if user else None


def caller_fields(db: Database, line_user_id: str) -> dict:
    """送後端時的身分欄位：後端 user_id／role；有匿名代號時以代號取代姓名（後端不需要真名）。"""
    account = db.one("SELECT * FROM accounts WHERE line_user_id = ? AND status = 'active'", (line_user_id,))
    if account is None:
        return {"caregiver_name": caregiver_name(db, line_user_id), "user_id": None, "role": "caregiver"}
    return {"caregiver_name": account["pseudonym"] or caregiver_name(db, line_user_id),
            "user_id": account["be_user_id"], "role": account["role"]}


# ---------- 督導請求 ----------

def create_supervisor_request(
    db: Database, caregiver_user_id: str, source: str, source_id: int,
    question: str, reason: str | None, backend_request_id: str | None, intent: str | None = None,
    priority: str = "normal", category: str | None = None,
) -> int:
    """category：emergency（緊急關鍵字）、user_requested（照護員要求轉督導）、其餘為後端拒答原因。"""
    cur = db.execute(
        "INSERT INTO supervisor_requests (caregiver_line_user_id, source, source_id, question, reason,"
        " backend_request_id, intent, priority, category, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (caregiver_user_id, source, source_id, question, reason, backend_request_id, intent, priority, category,
         now()),
    )
    logger.info("新增督導請求 %d（來源 %s %d，%s）", cur.lastrowid, source, source_id, priority)
    return cur.lastrowid


def create_emergency_request(db: Database, caregiver_user_id: str, source: str, source_id: int,
                             question: str, match) -> int:
    """緊急關鍵字命中（emergency.EmergencyMatch）：建立最優先的督導請求。"""
    level = "一級" if match.level == 1 else "二級"
    return create_supervisor_request(db, caregiver_user_id, source, source_id, question,
                                     f"緊急（{level}）：{match.category}", None, priority="urgent",
                                     category="emergency")


def active_supervisor_line_ids(db: Database) -> list[str]:
    return [r["line_user_id"] for r in db.all(
        "SELECT line_user_id FROM accounts WHERE role = 'supervisor' AND status = 'active'"
        " AND line_user_id IS NOT NULL")]


_SOURCE_TABLES = {"turn": "turns", "line_case": "line_cases", "text_question": "text_questions"}


# ---------- 待照護員確認的問題（2026-10-15 confirm-send-and-keyword 第 4.2 節） ----------

def list_unconfirmed(db: Database, user_id: str) -> list[dict]:
    """照護員還沒確認是否送出的問題（錄音的一輪、打字提問）。"""
    items = []
    turns = db.all(
        "SELECT t.id, t.intents, t.ended_at, r.alias FROM turns t JOIN sessions s ON s.id = t.session_id"
        " JOIN residents r ON r.id = s.resident_id WHERE s.caregiver_line_user_id = ? AND t.intents LIKE ?"
        " ORDER BY t.ended_at DESC", (user_id, "%needs_confirm%"))
    for t in turns:
        for i, r in enumerate(json.loads(t["intents"])):
            if r["status"] == "needs_confirm":
                items.append({"source": "turn", "id": t["id"], "index": i, "question": r["question"],
                              "intent": r["intent"], "confidence": r["confidence"], "created_at": t["ended_at"],
                              "resident_alias": t["alias"]})
    for q in db.all(
            "SELECT q.*, r.alias FROM text_questions q JOIN residents r ON r.id = q.resident_id"
            " WHERE q.caregiver_line_user_id = ? AND q.status = 'needs_confirm' ORDER BY q.created_at DESC", (user_id,)):
        items.append({"source": "text_question", "id": q["id"], "index": 0, "question": q["question"],
                      "intent": q["intent"], "confidence": q["intent_conf"], "created_at": q["created_at"],
                      "resident_alias": q["alias"]})
    items.sort(key=lambda x: x["created_at"] or 0, reverse=True)
    return items


def unconfirmed_payload(db: Database, user_id: str, source: str, item_id: int, index: int) -> dict:
    """取出待確認問題要送後端的內容（並檢查是否屬於這位照護員、仍待確認）。"""
    if source == "turn":
        row = db.one("SELECT t.intents FROM turns t JOIN sessions s ON s.id = t.session_id"
                     " WHERE t.id = ? AND s.caregiver_line_user_id = ?", (item_id, user_id))
        results = json.loads(row["intents"]) if row and row["intents"] else []
        if index >= len(results) or results[index]["status"] != "needs_confirm":
            raise NotFound("找不到待確認的問題")
        return results[index]["payload"]
    if source == "text_question":
        row = db.one("SELECT * FROM text_questions WHERE id = ? AND caregiver_line_user_id = ? AND status = 'needs_confirm'",
                     (item_id, user_id))
        if row is None:
            raise NotFound("找不到待確認的問題")
        return {"text_question_id": row["id"], **caller_fields(db, user_id), "text": row["question"],
                "intent": row["intent"], "intent_conf": row["intent_conf"], "keywords": [], "provisional": False}
    raise NotFound("來源不正確")


def set_unconfirmed_result(db: Database, user_id: str, source: str, item_id: int, index: int,
                           status: str, result=None) -> None:
    """確認後更新：status 為 pending（送出中）、dismissed（不用），或依後端結果（result）。"""
    if result is not None:
        status = result.status
    if source == "turn":
        with db.transaction() as conn:
            row = conn.execute("SELECT intents FROM turns WHERE id = ?", (item_id,)).fetchone()
            results = json.loads(row["intents"])
            item = results[index]
            item["status"] = status
            if result is not None:
                item.update({"request_id": result.request_id, "answer": result.answer, "reason": result.reason,
                             "elapsed_ms": result.elapsed_ms})
            answer, turn_status = combine_intent_answers(results)
            conn.execute("UPDATE turns SET answer = ?, answer_status = ?, intents = ? WHERE id = ?",
                         (answer, turn_status, json.dumps(results, ensure_ascii=False), item_id))
        if status == "awaiting_human":
            create_supervisor_request(db, user_id, "turn", item_id, item["question"], result.reason,
                                      result.request_id, item["intent"])
        return
    db.execute("UPDATE text_questions SET status = ?, answer = ?, reply_ms = ? WHERE id = ?",
               (status, result.answer if result else None, result.elapsed_ms if result else None, item_id))
    if status == "awaiting_human":
        question = db.one("SELECT question FROM text_questions WHERE id = ?", (item_id,))["question"]
        create_supervisor_request(db, user_id, "text_question", item_id, question, result.reason, result.request_id)


def combine_intent_answers(results: list[dict]) -> tuple[str | None, str]:
    """一輪多個意圖的回答合併成顯示文字與整體狀態（multi-intent-turn 第 4.2 節）。"""
    if not results:
        return None, "no_question"
    lines = []
    for r in results:
        if r["status"] == "awaiting_human":
            text = "已轉督導，等待回覆"
        elif r["status"] == "needs_confirm":
            text = "小照不太確定，等你確認是否送出"
        elif r["status"] == "pending":
            text = "查詢中…"
        elif r["status"] == "dismissed":
            text = "未送出（照護員選擇不用）"
        else:
            text = r.get("answer") or "（沒有回答）"
        lines.append(f"【{r['intent']}】{text}")
    statuses = {r["status"] for r in results} - {"dismissed"}
    if not statuses:
        status = "no_question"
    elif "awaiting_human" in statuses:
        status = "awaiting_human"
    elif "needs_confirm" in statuses:
        status = "needs_confirm"
    elif "pending" in statuses:
        status = "pending"
    elif "human_answered" in statuses:
        status = "human_answered"
    else:
        status = "answered"
    return "\n".join(lines), status


def reply_supervisor_request(db: Database, request_id: int, reply: str, replied_by: str) -> dict:
    """存入督導回覆並更新原紀錄（置頂顯示）；督導回覆不送後端（設計文件 2.1 節資料保存）。"""
    reply = reply.strip()
    if not reply:
        raise ServiceError("回覆不可空白")
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM supervisor_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise NotFound("請求不存在")
        if row["status"] == "replied":
            raise Conflict("這筆請求已經回覆過")
        conn.execute(
            "UPDATE supervisor_requests SET status = 'replied', reply = ?, replied_by = ?, replied_at = ? WHERE id = ?",
            (reply, replied_by, now(), request_id),
        )
        table = _SOURCE_TABLES[row["source"]]
        status_column = "answer_status" if table == "turns" else "status"
        reply_text = f"督導回覆（{replied_by}）：{reply}"
        turn = conn.execute("SELECT intents FROM turns WHERE id = ?", (row["source_id"],)).fetchone() \
            if table == "turns" else None
        if turn is not None and turn["intents"] and row["intent"]:
            # 一輪多個意圖：只更新轉督導的那一個意圖，其他意圖的回答保留
            results = json.loads(turn["intents"])
            by_request = any(r.get("request_id") == row["backend_request_id"] for r in results)
            for r in results:
                if (r.get("request_id") == row["backend_request_id"]) if by_request else (r["intent"] == row["intent"]):
                    r["status"], r["answer"] = "human_answered", reply_text
            answer, status = combine_intent_answers(results)
            conn.execute("UPDATE turns SET answer = ?, answer_status = ?, intents = ? WHERE id = ?",
                         (answer, status, json.dumps(results, ensure_ascii=False), row["source_id"]))
        else:
            conn.execute(
                f"UPDATE {table} SET answer = ?, {status_column} = 'human_answered' WHERE id = ?",
                (reply_text, row["source_id"]),
            )
    return db.one("SELECT * FROM supervisor_requests WHERE id = ?", (request_id,))


# ---------- 確認／修正紀錄（correction-and-cases-tabs） ----------

def create_record(
    db: Database, turn_id: int, session_id: str, caregiver_user_id: str, resident_id: int,
    utterances: list[dict], speaker_roles: dict[str, str], audio_start_ms: int | None = None,
) -> int:
    """一輪結束時建立紀錄；doc 為修正的起點，original 保留系統原始輸出。

    audio_start_ms：督導回聽音訊檔的起點（與句子時間同一基準），供網頁逐句跳到對應位置。
    """
    resident = db.one("SELECT alias FROM residents WHERE id = ?", (resident_id,))
    codes = sorted({u["speaker_label"] for u in utterances if u.get("speaker_label")})
    doc = {
        "audio_start_ms": audio_start_ms,
        "speakers": {code: speaker_roles.get(code, "未辨識") for code in codes},
        "sentences": [
            {"start_ms": u["start_ms"], "end_ms": u["end_ms"], "speaker": u.get("speaker_label"),
             "role": u["speaker_role"], "role_manual": False, "text": u["text"], "intent": u["intent"],
             "intent_owner": u.get("intent_owner"), "source_idx": [u["idx"]],
             "asr_text": u.get("asr_text"), "review_flag": u.get("review_flag"), "term_hint": u.get("term_hint")}
            for u in utterances
        ],
    }
    body = json.dumps(doc, ensure_ascii=False)
    t = now()
    cur = db.execute(
        "INSERT INTO records (turn_id, session_id, caregiver_line_user_id, resident_alias, doc, original,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (turn_id, session_id, caregiver_user_id, resident["alias"] if resident else "", body, body, t, t),
    )
    return cur.lastrowid


def _record_out(row: dict) -> dict:
    row = dict(row)
    row["doc"] = json.loads(row["doc"])
    row.pop("original", None)
    return row


def list_caregiver_records(db: Database, caregiver_user_id: str) -> list[dict]:
    """照護員自己的紀錄；原始輪次（24 小時內）還在時一併帶出小照的回答。"""
    return [_record_out(r) for r in db.all(
        "SELECT r.*, t.answer AS answer, t.answer_status AS answer_status FROM records r"
        " LEFT JOIN turns t ON t.id = r.turn_id"
        " WHERE r.caregiver_line_user_id = ? ORDER BY r.seq DESC LIMIT 100", (caregiver_user_id,)
    )]


def my_supervisor_requests(db: Database, caregiver_user_id: str) -> list[dict]:
    """照護員自己轉給督導的問題與回覆（未回覆的一直保留；回覆後保存 1 天）。"""
    return db.all(
        "SELECT id, source, question, reason, status, reply, replied_by, replied_at, created_at"
        " FROM supervisor_requests WHERE caregiver_line_user_id = ? ORDER BY status = 'replied', id DESC",
        (caregiver_user_id,),
    )


def own_record(db: Database, seq: int, caregiver_user_id: str) -> dict:
    row = db.one("SELECT * FROM records WHERE seq = ?", (seq,))
    if row is None or row["caregiver_line_user_id"] != caregiver_user_id:
        raise NotFound("紀錄不存在")
    return row


def update_record_by_caregiver(
    db: Database, seq: int, caregiver_user_id: str, speakers: dict[str, str] | None, sentences: list[dict] | None,
) -> dict:
    """照護員修正：代號對應角色、單句角色、錯字、意圖、需求歸屬（不能改句子結構，那是督導的權限）。"""
    row = own_record(db, seq, caregiver_user_id)
    if row["status"] == "confirmed":
        raise Conflict("督導已確認，不能再修改")
    doc = json.loads(row["doc"])
    for code, role in (speakers or {}).items():
        if code not in doc["speakers"]:
            raise ServiceError(f"沒有說話人 {code}")
        if role not in ROLES_SPEAKER:
            raise ServiceError("身分不在清單內")
        doc["speakers"][code] = role
        for s in doc["sentences"]:
            if s["speaker"] == code and not s["role_manual"]:
                s["role"] = role
    for edit in sentences or []:
        i = edit.get("i")
        if not isinstance(i, int) or not 0 <= i < len(doc["sentences"]):
            raise ServiceError("句子編號錯誤")
        s = doc["sentences"][i]
        if edit.get("role") is not None:
            if edit["role"] not in ROLES_SPEAKER:
                raise ServiceError("身分不在清單內")
            s["role"], s["role_manual"] = edit["role"], True
        if edit.get("text") is not None:
            text = edit["text"].strip()
            if not text:
                raise ServiceError("句子內容不能是空的")
            s["text"] = text
        for key in ("intent", "intent_owner"):
            if key in edit and edit[key] is not None:
                allowed = INTENT_LABELS if key == "intent" else ROLES_SPEAKER
                if edit[key] not in allowed:
                    raise ServiceError("意圖或需求歸屬不在清單內")
                s[key] = edit[key]
    db.execute("UPDATE records SET doc = ?, updated_at = ? WHERE seq = ?",
               (json.dumps(doc, ensure_ascii=False), now(), seq))
    if speakers:
        # 同一次錄音的其他輪也套用相同的代號對應（尚未被督導確認者）
        apply_session_speaker_roles(db, row["session_id"], speakers, exclude_seq=seq)
    return _record_out(db.one("SELECT * FROM records WHERE seq = ?", (seq,)))


def apply_session_speaker_roles(db: Database, session_id: str, speakers: dict[str, str], exclude_seq: int) -> None:
    for other in db.all(
        "SELECT seq, doc FROM records WHERE session_id = ? AND seq != ? AND status != 'confirmed'",
        (session_id, exclude_seq),
    ):
        doc = json.loads(other["doc"])
        changed = False
        for code, role in speakers.items():
            if code in doc["speakers"]:
                doc["speakers"][code] = role
                for s in doc["sentences"]:
                    if s["speaker"] == code and not s["role_manual"]:
                        s["role"] = role
                changed = True
        if changed:
            db.execute("UPDATE records SET doc = ?, updated_at = ? WHERE seq = ?",
                       (json.dumps(doc, ensure_ascii=False), now(), other["seq"]))
    for code, role in speakers.items():
        # 錄音頁顯示的原始語句也同步（24 小時內仍可在錄音頁看到）
        db.execute(
            "UPDATE utterances SET speaker_role = ? WHERE speaker_label = ? AND speaker_source = 'voiceprint'"
            " AND turn_id IN (SELECT id FROM turns WHERE session_id = ?)", (role, code, session_id),
        )


# ---------- 督導（correction-and-cases-tabs P2） ----------

def supervisor_records(db: Database) -> list[dict]:
    return [_record_out(r) for r in db.all(
        "SELECT r.*, COALESCE(a.display_name, n.display_name, '（未綁定帳號）') AS caregiver_name FROM records r"
        " LEFT JOIN accounts a ON a.line_user_id = r.caregiver_line_user_id"
        " LEFT JOIN user_names n ON n.line_user_id = r.caregiver_line_user_id"
        " ORDER BY r.status = 'confirmed', r.status = 'draft', r.seq DESC LIMIT 200"
    )]


def _editable(db: Database, seq: int) -> dict:
    row = db.one("SELECT * FROM records WHERE seq = ?", (seq,))
    if row is None:
        raise NotFound("紀錄不存在")
    if row["status"] == "confirmed":
        raise Conflict("已確認的紀錄不能再修改")
    return row


def validate_doc(doc: dict) -> dict:
    """督導送來的整份紀錄（可能已合併、拆句）：檢查欄位與順序，回傳整理後的版本。"""
    if not isinstance(doc, dict) or not isinstance(doc.get("speakers"), dict) \
            or not isinstance(doc.get("sentences"), list):
        raise ServiceError("紀錄格式錯誤")
    speakers = {}
    for code, role in doc["speakers"].items():
        if role not in ROLES_SPEAKER:
            raise ServiceError("身分不在清單內")
        speakers[str(code)] = role
    if not doc["sentences"]:
        raise ServiceError("一輪至少要有一句")
    sentences, last_start = [], -1
    for s in doc["sentences"]:
        if not isinstance(s, dict):
            raise ServiceError("紀錄格式錯誤")
        try:
            start, end = int(s["start_ms"]), int(s["end_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceError("句子時間錯誤") from exc
        if start < 0 or end < start or start < last_start:
            raise ServiceError("句子時間錯誤或順序不對")
        last_start = start
        text = str(s.get("text") or "").strip()
        if not text or len(text) > 500:
            raise ServiceError("句子內容不能是空的，也不能超過 500 字")
        speaker = s.get("speaker")
        if speaker is not None and speaker not in speakers:
            raise ServiceError(f"沒有說話人 {speaker}")
        role = s.get("role") or "未辨識"
        intent, owner = s.get("intent"), s.get("intent_owner")
        if role not in ROLES_SPEAKER or (owner is not None and owner not in ROLES_SPEAKER):
            raise ServiceError("身分或需求歸屬不在清單內")
        if intent is not None and intent not in INTENT_LABELS:
            raise ServiceError("意圖不在清單內")
        source = s.get("source_idx") or []
        if not isinstance(source, list) or not all(isinstance(x, int) for x in source):
            raise ServiceError("source_idx 格式錯誤")
        # 換字前的原文與衝突提醒由系統產生，原樣保留（2026-10-17 asr-term-correction）
        extra = {k: str(s[k])[:500] for k in ("asr_text", "review_flag", "term_hint") if s.get(k)}
        sentences.append({"start_ms": start, "end_ms": end, "speaker": speaker, "role": role,
                          "role_manual": bool(s.get("role_manual")), "text": text, "intent": intent,
                          "intent_owner": owner, "source_idx": source, **extra})
    return {"speakers": speakers, "sentences": sentences}


def supervisor_update(db: Database, seq: int, doc: dict) -> dict:
    row = _editable(db, seq)
    clean = validate_doc(doc)
    clean["audio_start_ms"] = json.loads(row["doc"]).get("audio_start_ms")  # 由伺服器決定，不接受修改
    db.execute("UPDATE records SET doc = ?, updated_at = ? WHERE seq = ?",
               (json.dumps(clean, ensure_ascii=False), now(), seq))
    return _record_out(db.one("SELECT * FROM records WHERE seq = ?", (seq,)))


def move_sentence(db: Database, seq: int, i: int, direction: str) -> None:
    """把一輪的第一句移到上一輪結尾，或最後一句移到下一輪開頭（同一次錄音內）。"""
    row = _editable(db, seq)
    doc = json.loads(row["doc"])
    n = len(doc["sentences"])
    if direction == "prev":
        if i != 0:
            raise ServiceError("只有第一句可以移到上一輪")
        neighbor = db.one("SELECT * FROM records WHERE session_id = ? AND seq < ? ORDER BY seq DESC LIMIT 1",
                          (row["session_id"], seq))
    elif direction == "next":
        if i != n - 1:
            raise ServiceError("只有最後一句可以移到下一輪")
        neighbor = db.one("SELECT * FROM records WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT 1",
                          (row["session_id"], seq))
    else:
        raise ServiceError("direction 只能是 prev 或 next")
    if neighbor is None:
        raise ServiceError("同一次錄音中沒有相鄰的輪")
    if neighbor["status"] == "confirmed":
        raise Conflict("相鄰的輪已確認，不能移入")
    if n == 1:
        raise ServiceError("這一輪只剩一句，不能移走")
    sentence = doc["sentences"].pop(i)
    other = json.loads(neighbor["doc"])
    if sentence["speaker"] and sentence["speaker"] not in other["speakers"]:
        other["speakers"][sentence["speaker"]] = doc["speakers"].get(sentence["speaker"], "未辨識")
    if direction == "prev":
        other["sentences"].append(sentence)
    else:
        other["sentences"].insert(0, sentence)
    t = now()
    with db.transaction() as conn:
        conn.execute("UPDATE records SET doc = ?, updated_at = ? WHERE seq = ?",
                     (json.dumps(doc, ensure_ascii=False), t, seq))
        conn.execute("UPDATE records SET doc = ?, updated_at = ? WHERE seq = ?",
                     (json.dumps(other, ensure_ascii=False), t, neighbor["seq"]))


def confirm_record(db: Database, seq: int, supervisor_name: str, names_checked: bool) -> dict:
    """督導確認：須先檢查人名並換成代號；確認後才會提供給後端。"""
    _editable(db, seq)
    if not names_checked:
        raise ServiceError("請先檢查逐字稿中的人名，並換成代號")
    t = now()
    db.execute("UPDATE records SET status = 'confirmed', names_checked = 1, confirmed_by = ?, confirmed_at = ?,"
               " updated_at = ? WHERE seq = ?", (supervisor_name, t, t, seq))
    return _record_out(db.one("SELECT * FROM records WHERE seq = ?", (seq,)))


# ---------- 給後端：已確認紀錄（correction-and-cases-tabs P3） ----------

EXPORT_SENTENCE_FIELDS = ("start_ms", "end_ms", "speaker", "role", "text", "intent", "intent_owner")


def export_records(db: Database, after_seq: int, limit: int) -> dict:
    """依流水編號回傳 seq > after_seq 的已確認紀錄；第一次被取走時記下 exported_at。

    只給修正後的內容：不含照護員 LINE ID、系統原始輸出與音訊。
    """
    rows = db.all(
        "SELECT * FROM records WHERE status = 'confirmed' AND seq > ? ORDER BY seq LIMIT ?", (after_seq, limit)
    )
    t = now()
    out = []
    for row in rows:
        doc = json.loads(row["doc"])
        out.append({
            "seq": row["seq"], "session_id": row["session_id"], "resident_alias": row["resident_alias"],
            "confirmed_at": row["confirmed_at"], "speakers": doc["speakers"],
            "sentences": [{k: s.get(k) for k in EXPORT_SENTENCE_FIELDS} for s in doc["sentences"]],
        })
    if rows:
        db.execute(
            f"UPDATE records SET exported_at = ? WHERE exported_at IS NULL AND seq IN ({','.join('?' * len(rows))})",
            (t, *[r["seq"] for r in rows]),
        )
    return {"records": out, "next_after_seq": rows[-1]["seq"] if rows else after_seq}


def turn_audio_path(audio_dir: Path, turn_id: int) -> Path:
    return audio_dir / f"{turn_id}.wav"


def check_record(db: Database, seq: int, caregiver_user_id: str) -> dict:
    row = own_record(db, seq, caregiver_user_id)
    if row["status"] == "confirmed":
        raise Conflict("督導已確認")
    t = now()
    db.execute("UPDATE records SET status = 'caregiver_checked', caregiver_checked_at = ?, updated_at = ? WHERE seq = ?",
               (t, t, seq))
    return _record_out(db.one("SELECT * FROM records WHERE seq = ?", (seq,)))


# ---------- 保存期限 ----------

def cleanup_expired(
    db: Database, current: float | None = None, audio_dir: Path | None = None, log_dir: Path | None = None,
) -> dict[str, int]:
    """刪除到期資料（設計文件 2.1 節資料保存）；帳號不在此清除。"""
    current = time.time() if current is None else current
    cutoff = current - RECORD_TTL_SECONDS
    deleted = {}
    with db.transaction() as conn:
        deleted["turns"] = conn.execute(
            "DELETE FROM turns WHERE COALESCE(ended_at, started_at) < ?", (cutoff,)
        ).rowcount
        deleted["sessions"] = conn.execute(
            "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?"
            " AND id NOT IN (SELECT session_id FROM turns)", (cutoff,)
        ).rowcount
        deleted["line_cases"] = conn.execute("DELETE FROM line_cases WHERE created_at < ?", (cutoff,)).rowcount
        deleted["text_questions"] = conn.execute(
            "DELETE FROM text_questions WHERE created_at < ?", (cutoff,)
        ).rowcount
        deleted["daily_reports"] = conn.execute("DELETE FROM daily_reports WHERE created_at < ?", (cutoff,)).rowcount
        # 督導請求：未回覆前一直保留；回覆後 1 天
        deleted["supervisor_requests"] = conn.execute(
            "DELETE FROM supervisor_requests WHERE status = 'replied' AND replied_at < ?", (cutoff,)
        ).rowcount
        deleted["binding_codes"] = conn.execute(
            "DELETE FROM binding_codes WHERE used_at IS NULL AND expires_at < ?", (current,)
        ).rowcount
        deleted["records_unconfirmed"] = conn.execute(
            "DELETE FROM records WHERE status != 'confirmed' AND created_at < ?",
            (current - RECORD_UNCONFIRMED_TTL_SECONDS,),
        ).rowcount
        # 已確認：後端取走後 30 天；一直未被取走的，確認後最長 90 天
        deleted["records_exported"] = conn.execute(
            "DELETE FROM records WHERE status = 'confirmed' AND exported_at IS NOT NULL AND exported_at < ?",
            (current - RECORD_AFTER_EXPORT_TTL_SECONDS,),
        ).rowcount
        deleted["records_never_exported"] = conn.execute(
            "DELETE FROM records WHERE status = 'confirmed' AND exported_at IS NULL AND confirmed_at < ?",
            (current - RECORD_CONFIRMED_MAX_TTL_SECONDS,),
        ).rowcount
        deleted["webhook_events"] = conn.execute(
            "DELETE FROM webhook_events WHERE received_at < ?", (cutoff,)
        ).rowcount
    if audio_dir is not None and audio_dir.exists():
        # 督導回聽音訊：確認時即刪；未確認者最長 7 天
        expired = [f for f in audio_dir.glob("*.wav") if f.stat().st_mtime < current - RECORD_UNCONFIRMED_TTL_SECONDS]
        for f in expired:
            f.unlink(missing_ok=True)
        deleted["turn_audio"] = len(expired)
    if log_dir is not None and log_dir.exists():
        # 送後端記錄（含逐字稿）：保存 1 天
        old_logs = [f for f in log_dir.glob("backend_requests-*.jsonl") if f.stat().st_mtime < cutoff]
        for f in old_logs:
            f.unlink(missing_ok=True)
        deleted["backend_logs"] = len(old_logs)
    if any(deleted.values()):
        logger.info("清除到期資料：%s", deleted)
    return deleted


def caregiver_name(db: Database, line_user_id: str) -> str:
    """送後端時辨識照護員：管理員建立帳號時設定的姓名；管理員切換成照護員時沒有帳號，改用 LINE 顯示名稱。"""
    row = db.one(
        "SELECT COALESCE((SELECT display_name FROM accounts WHERE line_user_id = ?),"
        " (SELECT display_name FROM user_names WHERE line_user_id = ?), '（未綁定帳號）') AS name",
        (line_user_id, line_user_id),
    )
    return row["name"]


def remember_name(db: Database, line_user_id: str, display_name: str) -> None:
    """記下使用者的顯示名稱，供督導頁面顯示（管理員切換成照護員時沒有帳號資料可查）。"""
    if not display_name:
        return
    db.execute(
        "INSERT INTO user_names (line_user_id, display_name, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(line_user_id) DO UPDATE SET display_name = excluded.display_name, updated_at = excluded.updated_at",
        (line_user_id, display_name, now()),
    )
