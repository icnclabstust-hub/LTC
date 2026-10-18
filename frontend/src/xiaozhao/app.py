"""小照服務主程式（FastAPI）：LIFF 網頁 API、LINE webhook、靜態網頁。

啟動（repo 根目錄）：
    .venv-xiaozhao\\Scripts\\python -m uvicorn xiaozhao.app:create_app --factory --app-dir src --host 127.0.0.1 --port 8010
另開 ngrok 把固定網域導到 127.0.0.1:8010。只在提案學生筆電手動啟動，不常駐、不排程。
"""

import asyncio
import hmac
from concurrent.futures import TimeoutError as FutureTimeout
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import unquote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import services
from .auth import IdTokenVerifier, Principal, Unauthorized, require, resolve_principal
from .backend import BackgroundRunner, build_backend
from .config import (
    CLEANUP_INTERVAL_SECONDS,
    EXPORT_MAX_LIMIT,
    INTENT_LABELS,
    ROLES_SPEAKER,
    Settings,
    load_settings,
)
from .db import Database, now
from .pipeline import ReportRecorder, SessionProcessor

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
ALIAS_MAX_LENGTH = 20


class State:
    """服務執行期的共用物件；測試時可替換 engines 與 line。"""

    def __init__(self, settings: Settings, db: Database, engines=None, line=None) -> None:
        self.settings = settings
        self.db = db
        self.backend = build_backend(settings.backend_mode, settings.data_dir if settings.backend_log else None,
                                     settings.backend_url, settings.backend_token, settings.backend_poll_seconds,
                                     settings.backend_timeout_seconds, settings.backend_scene_text)
        # http 模式：後端呼叫在背景執行（async-backend）；模擬後端立即回覆，維持立即執行
        self.runner = BackgroundRunner(settings.backend_workers,
                                       inline=not (settings.backend_async and settings.backend_mode == "http"))
        self.engines = engines
        self.engines_error: str | None = None
        self.line = line
        self.verifier = IdTokenVerifier(settings.login_channel_id)
        self.sessions: dict[str, SessionProcessor] = {}
        self.reports: dict[str, ReportRecorder] = {}
        self.known_names: dict[str, str] = {}
        self.lock = threading.Lock()

    def load_engines(self) -> None:
        from .engines import SherpaEngines

        try:
            self.engines = SherpaEngines(self.settings.model_downloads, self.settings.intent_model_dir,
                                         self.settings.intent_model, self.settings.n0_model_dir)
            logger.info("模型載入完成")
        except Exception as exc:  # 模型缺檔時服務仍可啟動，錄音相關 API 回 503
            self.engines_error = str(exc)
            logger.exception("模型載入失敗")


def create_app(state: State | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st: State = app.state.xz
        if st.engines is None:
            threading.Thread(target=st.load_engines, daemon=True).start()
        if st.line is None and st.settings.channel_secret:
            from .line_service import LineService

            st.line = LineService(st.settings, st.db, st.backend, classify=lambda t: st.engines.classify(t)
                                  if st.engines else (None, None), runner=st.runner)
        cleanup = asyncio.create_task(_cleanup_loop(st))
        yield
        cleanup.cancel()
        for processor in list(st.sessions.values()):
            processor.stop()

    package_logger = logging.getLogger("xiaozhao")
    if not package_logger.handlers:
        # uvicorn 只設定自己的紀錄器；小照的紀錄（輪開始／結束、關鍵詞）另外輸出到主控台
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        package_logger.addHandler(handler)
        package_logger.setLevel(logging.INFO)
    if state is None:
        settings = load_settings()
        state = State(settings, Database(settings.db_path))
    app = FastAPI(title="小照", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.xz = state

    @app.exception_handler(services.ServiceError)
    async def service_error(_: Request, exc: services.ServiceError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    _register_routes(app)
    app.mount("/liff", StaticFiles(directory=STATIC_DIR, html=True), name="liff")
    return app


async def _cleanup_loop(st: State) -> None:
    while True:
        try:
            await asyncio.to_thread(services.cleanup_expired, st.db, None, st.settings.turn_audio_dir,
                                    st.settings.data_dir)
        except Exception:
            logger.exception("清除到期資料失敗")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


def get_state(request: Request) -> State:
    return request.app.state.xz


def get_principal(
    request: Request,
    authorization: str = Header(default=""),
    x_acting_role: str | None = Header(default=None),
) -> Principal:
    st = get_state(request)
    if not authorization.startswith("Bearer "):
        raise Unauthorized("請先登入 LINE")
    token = authorization[len("Bearer "):]
    if st.settings.dev_fake_auth and token.startswith("dev:"):
        # 本機測試用：只接受直接連到本機的請求（經 ngrok 轉送的請求帶 X-Forwarded-For）
        if request.client is None or request.client.host != "127.0.0.1" or "x-forwarded-for" in request.headers:
            raise Unauthorized("測試登入只限本機")
        user_id = name = unquote(token[len("dev:"):])
    else:
        user_id, name = st.verifier.verify(token)
    return track_name(st, resolve_principal(st.db, st.settings, user_id, name, x_acting_role))


def track_name(st: State, principal: Principal) -> Principal:
    """名稱有變才寫入資料庫，供督導頁面顯示。"""
    if st.known_names.get(principal.user_id) != principal.display_name:
        services.remember_name(st.db, principal.user_id, principal.display_name)
        st.known_names[principal.user_id] = principal.display_name
    return principal


def require_engines(st: State):
    if st.engines is None:
        detail = f"模型載入失敗：{st.engines_error}" if st.engines_error else "模型載入中，請稍後再試"
        raise HTTPException(503, detail)
    return st.engines


# ---------- 請求格式 ----------

class ResidentIn(BaseModel):
    alias: str = Field(min_length=1, max_length=ALIAS_MAX_LENGTH)


class WrittenConsentIn(BaseModel):
    date: str  # YYYY-MM-DD


class VoiceConsentIn(BaseModel):
    decision: str  # agreed | declined


class TextQuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class SessionIn(BaseModel):
    resident_id: int
    consent_confirmed: bool


class SentenceEditIn(BaseModel):
    i: int
    role: str | None = None
    text: str | None = Field(default=None, max_length=500)
    intent: str | None = None
    intent_owner: str | None = None


class RecordDocIn(BaseModel):
    doc: dict


class MoveIn(BaseModel):
    i: int
    direction: str  # prev | next


class ConfirmIn(BaseModel):
    names_checked: bool = False


class RecordEditIn(BaseModel):
    speakers: dict[str, str] | None = None
    sentences: list[SentenceEditIn] | None = None


class BindIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)


class AccountIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=30)
    role: str


class AccountPatch(BaseModel):
    status: str  # disabled


class ReplyIn(BaseModel):
    reply: str = Field(min_length=1, max_length=2000)


def _own_resident(st: State, principal: Principal, resident_id: int) -> dict:
    resident = st.db.one("SELECT * FROM residents WHERE id = ?", (resident_id,))
    if resident is None or resident["caregiver_line_user_id"] != principal.user_id:
        raise services.NotFound("住民不存在")
    return resident


def _own_session(st: State, principal: Principal, session_id: str) -> SessionProcessor:
    processor = st.sessions.get(session_id)
    if processor is None or processor.caregiver_user_id != principal.user_id:
        raise services.NotFound("錄音不存在或已結束")
    return processor


def _turn_detail(st: State, turn: dict) -> dict:
    turn = dict(turn)
    turn["utterances"] = st.db.all("SELECT * FROM utterances WHERE turn_id = ? ORDER BY idx", (turn["id"],))
    return turn


def _register_routes(app: FastAPI) -> None:
    @app.get("/")
    def root():
        return RedirectResponse("/liff/")

    @app.get("/api/config")
    def config(st: State = Depends(get_state)):
        return {"liff_id": st.settings.liff_id, "dev_fake_auth": st.settings.dev_fake_auth,
                "speaker_roles": ROLES_SPEAKER, "intent_labels": INTENT_LABELS,
                "engines_ready": st.engines is not None}

    @app.get("/api/me")
    def me(p: Principal = Depends(get_principal)):
        return {"user_id": p.user_id, "role": p.role, "display_name": p.display_name,
                "is_admin": p.is_admin, "available_roles": p.available_roles}

    # ---------- 綁定 ----------

    @app.post("/api/bind")
    def bind(body: BindIn, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        if p.role is not None and not p.is_admin:
            raise services.Conflict("已經綁定過")
        account = services.bind(st.db, body.code, p.user_id)
        if st.line:
            st.line.link_menu(p.user_id, account["role"])
        return {"role": account["role"], "display_name": account["display_name"]}

    # ---------- 住民 ----------

    @app.get("/api/residents")
    def list_residents(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        return st.db.all(
            "SELECT r.*, c.decision AS voice_consent"
            " FROM residents r LEFT JOIN voice_consents c ON c.resident_id = r.id"
            " WHERE r.caregiver_line_user_id = ? ORDER BY r.id", (p.user_id,),
        )

    @app.post("/api/residents")
    def add_resident(body: ResidentIn, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        cur = st.db.execute(
            "INSERT INTO residents (caregiver_line_user_id, alias, created_at) VALUES (?, ?, ?)",
            (p.user_id, body.alias.strip(), now()),
        )
        return st.db.one("SELECT * FROM residents WHERE id = ?", (cur.lastrowid,))

    @app.patch("/api/residents/{resident_id}")
    def rename_resident(resident_id: int, body: ResidentIn, st: State = Depends(get_state),
                        p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        _own_resident(st, p, resident_id)
        st.db.execute("UPDATE residents SET alias = ? WHERE id = ?", (body.alias.strip(), resident_id))
        return st.db.one("SELECT * FROM residents WHERE id = ?", (resident_id,))

    @app.delete("/api/residents/{resident_id}")
    def delete_resident(resident_id: int, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        _own_resident(st, p, resident_id)
        if any(s.resident_id == resident_id for s in st.sessions.values()):
            raise services.Conflict("這位住民正在錄音，請先停止錄音")
        st.db.execute("DELETE FROM residents WHERE id = ?", (resident_id,))
        return {"deleted": resident_id}

    @app.patch("/api/residents/{resident_id}/written-consent")
    def written_consent(resident_id: int, body: WrittenConsentIn, st: State = Depends(get_state),
                        p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        _own_resident(st, p, resident_id)
        try:
            consent_date = date.fromisoformat(body.date)
        except ValueError as exc:
            raise services.ServiceError("日期格式須為 YYYY-MM-DD") from exc
        if consent_date > date.today():
            raise services.ServiceError("書面同意日期不可晚於今天")
        st.db.execute("UPDATE residents SET written_consent_date = ? WHERE id = ?", (consent_date.isoformat(), resident_id))
        return st.db.one("SELECT * FROM residents WHERE id = ?", (resident_id,))

    @app.post("/api/residents/{resident_id}/voiceprint-consent")
    def voiceprint_consent(resident_id: int, body: VoiceConsentIn, st: State = Depends(get_state),
                           p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        _own_resident(st, p, resident_id)
        if body.decision not in ("agreed", "declined"):
            raise services.ServiceError("decision 只能是 agreed 或 declined")
        # 同意＝錄音時即時區分不同說話人（匿名代號 01、02…，不保存聲紋）；不同意＝不計算聲紋
        st.db.execute(
            "INSERT INTO voice_consents (resident_id, decision, decided_at) VALUES (?, ?, ?)"
            " ON CONFLICT(resident_id) DO UPDATE SET decision = excluded.decision, decided_at = excluded.decided_at",
            (resident_id, body.decision, now()),
        )
        return {"resident_id": resident_id, "decision": body.decision}

    @app.post("/api/residents/{resident_id}/text-questions")
    def text_question(resident_id: int, body: TextQuestionIn, st: State = Depends(get_state),
                      p: Principal = Depends(get_principal)):
        """不錄音時的手動打字提問：送後端回答或轉督導。"""
        require(p, "caregiver")
        _own_resident(st, p, resident_id)
        intent, conf = st.engines.classify(body.question) if st.engines else (None, None)
        cur = st.db.execute(
            "INSERT INTO text_questions (resident_id, caregiver_line_user_id, question, status, created_at)"
            " VALUES (?, ?, ?, 'pending', ?)", (resident_id, p.user_id, body.question.strip(), now()),
        )
        qid, question = cur.lastrowid, body.question.strip()

        def finish(result) -> None:
            st.db.execute("UPDATE text_questions SET answer = ?, status = ?, reply_ms = ? WHERE id = ?",
                          (result.answer, result.status, result.elapsed_ms, qid))
            if result.status == "awaiting_human":
                services.create_supervisor_request(st.db, p.user_id, "text_question", qid, question,
                                                   result.reason, result.request_id)

        future = st.runner.submit(st.backend.query, {
            "text_question_id": qid, "caregiver_name": services.caregiver_name(st.db, p.user_id),
            "text": body.question, "intent": intent, "intent_conf": conf, "keywords": [], "provisional": False})
        # 最多等回覆目標（3 秒）；還沒好就先回「查詢中」，回答到了再寫入（async-backend 第 7 節）
        try:
            finish(future.result(timeout=st.settings.reply_target_seconds))
        except FutureTimeout:
            future.add_done_callback(lambda f: f.exception() is None and finish(f.result()))
        return st.db.one("SELECT * FROM text_questions WHERE id = ?", (qid,))

    @app.get("/api/residents/{resident_id}/text-questions")
    def list_text_questions(resident_id: int, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        _own_resident(st, p, resident_id)
        return st.db.all("SELECT * FROM text_questions WHERE resident_id = ? ORDER BY id DESC", (resident_id,))

    # ---------- 錄音模式 ----------

    @app.post("/api/sessions")
    def start_session(body: SessionIn, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        engines = require_engines(st)
        resident = _own_resident(st, p, body.resident_id)
        if not resident["written_consent_date"]:
            raise services.Conflict("這位住民尚未記錄書面同意，不能錄音；可改用手動打字提問")
        if not body.consent_confirmed:
            raise services.ServiceError("請先確認在場者同意錄音")
        if st.db.one("SELECT 1 FROM voice_consents WHERE resident_id = ?", (body.resident_id,)) is None:
            raise services.Conflict("請先詢問在場者是否同意即時區分說話人（不保存聲紋）")
        with st.lock:
            # 每位照護員同時只有一個錄音；舊的（例如重新整理頁面後留下的）直接結束
            stale = [s for s in st.sessions.values() if s.caregiver_user_id == p.user_id]
            for old in stale:
                st.sessions.pop(old.session_id, None)
        for old in stale:
            old.stop()
            logger.info("結束未收掉的錄音 session %s", old.session_id)
        with st.lock:
            session_id = uuid.uuid4().hex
            st.db.execute(
                "INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
                " VALUES (?, ?, ?, ?, ?)", (session_id, body.resident_id, p.user_id, now(), now()),
            )
            processor = SessionProcessor(session_id, body.resident_id, p.user_id, st.db, engines, st.backend,
                                         st.settings.prefetch_threshold, st.settings.endpoint_mode,
                                         st.settings.keyword_split,
                                         st.settings.turn_audio_dir if st.settings.keep_turn_audio else None,
                                         st.settings.auto_gain, st.settings.question_window, st.runner)
            st.sessions[session_id] = processor
        return {"session_id": session_id, "voiceprint_agreed": processor.voiceprint_agreed}

    @app.post("/api/sessions/{session_id}/chunks")
    async def upload_chunk(session_id: str, seq: int, request: Request, ts: float | None = None,
                           st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        processor = _own_session(st, p, session_id)
        data = await request.body()
        if len(data) % 2:
            raise services.ServiceError("音訊須為 16-bit PCM")
        return await asyncio.to_thread(processor.accept_chunk, seq, data, ts)

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        processor = _own_session(st, p, session_id)
        await asyncio.to_thread(processor.stop)
        with st.lock:
            st.sessions.pop(session_id, None)
        return {"stopped": session_id}

    @app.get("/api/sessions/{session_id}/turns")
    def session_turns(session_id: str, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        session = st.db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if session is None or session["caregiver_line_user_id"] != p.user_id:
            raise services.NotFound("錄音不存在")
        turns = st.db.all("SELECT * FROM turns WHERE session_id = ? ORDER BY seq DESC", (session_id,))
        return [_turn_detail(st, t) for t in turns]

    @app.get("/api/turns")
    def list_turns(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        """語音紀錄列表（本人、1 天內）。"""
        require(p, "caregiver")
        return st.db.all(
            "SELECT t.*, r.alias AS resident_alias,"
            " (SELECT COUNT(*) FROM utterances u WHERE u.turn_id = t.id) AS utterance_count,"
            " (SELECT text FROM utterances u WHERE u.turn_id = t.id ORDER BY idx LIMIT 1) AS first_text"
            " FROM turns t JOIN sessions s ON s.id = t.session_id JOIN residents r ON r.id = s.resident_id"
            " WHERE s.caregiver_line_user_id = ? ORDER BY t.id DESC LIMIT 100", (p.user_id,),
        )

    def _own_turn(st: State, p: Principal, turn_id: int) -> dict:
        turn = st.db.one(
            "SELECT t.*, s.caregiver_line_user_id, s.resident_id, r.alias AS resident_alias FROM turns t"
            " JOIN sessions s ON s.id = t.session_id JOIN residents r ON r.id = s.resident_id WHERE t.id = ?",
            (turn_id,),
        )
        if turn is None or turn["caregiver_line_user_id"] != p.user_id:
            raise services.NotFound("語音紀錄不存在")
        return turn

    @app.get("/api/turns/{turn_id}")
    def turn_detail(turn_id: int, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        return _turn_detail(st, _own_turn(st, p, turn_id))

    # ---------- 確認／修正紀錄（correction-and-cases-tabs P1：照護員） ----------

    @app.get("/api/records")
    def list_records(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        return services.list_caregiver_records(st.db, p.user_id)

    @app.patch("/api/records/{seq}")
    def edit_record(seq: int, body: RecordEditIn, st: State = Depends(get_state),
                    p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        record = services.update_record_by_caregiver(
            st.db, seq, p.user_id, body.speakers,
            [e.model_dump(exclude_unset=True) for e in body.sentences] if body.sentences else None,
        )
        processor = st.sessions.get(record["session_id"])
        if processor and body.speakers:
            # 錄音還在進行：之後這個代號的句子直接標成照護員選的角色
            for code, role in body.speakers.items():
                processor.map_speaker(code, role)
        return record

    @app.get("/api/my/supervisor-requests")
    def my_supervisor_requests(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        """我的案件：給督導的問題與回覆（照護員本人）。"""
        require(p, "caregiver")
        return services.my_supervisor_requests(st.db, p.user_id)

    @app.post("/api/records/{seq}/check")
    def check_record(seq: int, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        return services.check_record(st.db, seq, p.user_id)

    # ---------- LINE 案件 ----------

    @app.get("/api/line-cases")
    def line_cases(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        return st.db.all("SELECT * FROM line_cases WHERE caregiver_line_user_id = ? ORDER BY id DESC", (p.user_id,))

    # ---------- 今日狀況回報 ----------

    @app.post("/api/daily-reports")
    def start_report(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        engines = require_engines(st)
        with st.lock:
            # 舊的回報錄音（未送出）直接丟棄
            for old_id in [k for k, r in st.reports.items() if r.caregiver_user_id == p.user_id]:
                st.reports.pop(old_id)
            report_id = uuid.uuid4().hex
            st.reports[report_id] = ReportRecorder(report_id, p.user_id, engines)
        return {"report_id": report_id}

    def _own_report(st: State, p: Principal, report_id: str) -> ReportRecorder:
        recorder = st.reports.get(report_id)
        if recorder is None or recorder.caregiver_user_id != p.user_id:
            raise services.NotFound("回報錄音不存在或已結束")
        return recorder

    @app.post("/api/daily-reports/{report_id}/chunks")
    async def report_chunk(report_id: str, seq: int, request: Request, st: State = Depends(get_state),
                           p: Principal = Depends(get_principal)):
        recorder = _own_report(st, p, report_id)
        data = await request.body()
        if len(data) % 2:
            raise services.ServiceError("音訊須為 16-bit PCM")
        return await asyncio.to_thread(recorder.accept_chunk, seq, data)

    @app.post("/api/daily-reports/{report_id}/stop")
    async def stop_report(report_id: str, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        recorder = _own_report(st, p, report_id)
        with st.lock:
            st.reports.pop(report_id, None)
        transcript = await asyncio.to_thread(recorder.finish)
        if not transcript:
            return {"saved": False, "detail": "沒有辨識到語音"}
        cur = st.db.execute(
            "INSERT INTO daily_reports (caregiver_line_user_id, report_date, transcript, created_at) VALUES (?, ?, ?, ?)",
            (p.user_id, date.today().isoformat(), transcript, now()),
        )
        return {"saved": True, "id": cur.lastrowid, "transcript": transcript}

    @app.get("/api/daily-reports/mine")
    def my_reports(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "caregiver")
        return st.db.all("SELECT * FROM daily_reports WHERE caregiver_line_user_id = ? ORDER BY id DESC", (p.user_id,))

    # ---------- 督導 ----------

    @app.get("/api/supervisor/requests")
    def supervisor_requests(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "supervisor")
        return st.db.all(
            "SELECT q.*, COALESCE(a.display_name, n.display_name, '（未綁定帳號）') AS caregiver_name"
            " FROM supervisor_requests q LEFT JOIN accounts a ON a.line_user_id = q.caregiver_line_user_id"
            " LEFT JOIN user_names n ON n.line_user_id = q.caregiver_line_user_id"
            " ORDER BY q.status = 'replied', q.id DESC"
        )

    @app.post("/api/supervisor/requests/{request_id}/reply")
    def supervisor_reply(request_id: int, body: ReplyIn, st: State = Depends(get_state),
                         p: Principal = Depends(get_principal)):
        require(p, "supervisor")
        row = services.reply_supervisor_request(st.db, request_id, body.reply, p.display_name)
        pushed = False
        if st.line:
            pushed = st.line.push(
                row["caregiver_line_user_id"], f"督導回覆了你的問題：「{row['question']}」\n\n{row['reply']}"
            )
            if not pushed and row["source"] == "line_case":
                st.db.execute("UPDATE line_cases SET notify_failed = 1 WHERE id = ?", (row["source_id"],))
        return {**row, "pushed": pushed}

    @app.get("/api/supervisor/daily-reports")
    def supervisor_reports(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "supervisor")
        return st.db.all(
            "SELECT d.*, COALESCE(a.display_name, n.display_name, '（未綁定帳號）') AS caregiver_name"
            " FROM daily_reports d LEFT JOIN accounts a ON a.line_user_id = d.caregiver_line_user_id"
            " LEFT JOIN user_names n ON n.line_user_id = d.caregiver_line_user_id ORDER BY d.id DESC"
        )

    # ---------- 督導：紀錄確認（correction-and-cases-tabs P2） ----------

    @app.get("/api/supervisor/records")
    def supervisor_records(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "supervisor")
        records = services.supervisor_records(st.db)
        for r in records:
            r["has_audio"] = services.turn_audio_path(st.settings.turn_audio_dir, r["turn_id"]).exists()
        return records

    @app.get("/api/supervisor/records/{seq}/audio")
    def supervisor_record_audio(seq: int, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "supervisor")
        row = st.db.one("SELECT turn_id FROM records WHERE seq = ?", (seq,))
        path = services.turn_audio_path(st.settings.turn_audio_dir, row["turn_id"]) if row else None
        if path is None or not path.exists():
            raise services.NotFound("沒有音訊（已確認或超過 7 天即刪除）")
        return FileResponse(path, media_type="audio/wav")

    @app.put("/api/supervisor/records/{seq}")
    def supervisor_update_record(seq: int, body: RecordDocIn, st: State = Depends(get_state),
                                 p: Principal = Depends(get_principal)):
        require(p, "supervisor")
        return services.supervisor_update(st.db, seq, body.doc)

    @app.post("/api/supervisor/records/{seq}/move")
    def supervisor_move_sentence(seq: int, body: MoveIn, st: State = Depends(get_state),
                                 p: Principal = Depends(get_principal)):
        require(p, "supervisor")
        services.move_sentence(st.db, seq, body.i, body.direction)
        return {"ok": True}

    @app.post("/api/supervisor/records/{seq}/confirm")
    def supervisor_confirm_record(seq: int, body: ConfirmIn, st: State = Depends(get_state),
                                  p: Principal = Depends(get_principal)):
        require(p, "supervisor")
        record = services.confirm_record(st.db, seq, p.display_name, body.names_checked)
        # 確認後立即刪除回聽音訊
        services.turn_audio_path(st.settings.turn_audio_dir, record["turn_id"]).unlink(missing_ok=True)
        return record

    # ---------- 給後端：已確認紀錄（correction-and-cases-tabs P3） ----------

    @app.get("/api/export/records")
    def export_records(after_seq: int = 0, limit: int = 100, authorization: str | None = Header(default=None),
                       st: State = Depends(get_state)):
        """後端以獨立權杖抓取已確認的紀錄；不經 LINE 登入。"""
        if not st.settings.export_token:
            raise services.NotFound("匯出未啟用")
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token.encode(), st.settings.export_token.encode()):
            raise HTTPException(status_code=401, detail="權杖錯誤")
        if after_seq < 0 or not 1 <= limit <= EXPORT_MAX_LIMIT:
            raise services.ServiceError(f"after_seq 須 ≥ 0，limit 須在 1～{EXPORT_MAX_LIMIT}")
        return services.export_records(st.db, after_seq, limit)

    # ---------- 管理員 ----------

    @app.get("/api/admin/accounts")
    def list_accounts(st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "admin")
        return st.db.all(
            "SELECT a.id, a.display_name, a.role, a.status, a.created_at, a.line_user_id IS NOT NULL AS bound,"
            " (SELECT MAX(expires_at) FROM binding_codes b WHERE b.account_id = a.id AND b.used_at IS NULL)"
            " AS code_expires_at FROM accounts a ORDER BY a.id"
        )

    @app.post("/api/admin/accounts")
    def create_account(body: AccountIn, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "admin")
        account, code = services.create_account(st.db, body.display_name, body.role)
        return {"account": account, "binding_code": code}

    @app.post("/api/admin/accounts/{account_id}/binding-code")
    def new_code(account_id: int, st: State = Depends(get_state), p: Principal = Depends(get_principal)):
        require(p, "admin")
        account = st.db.one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if account and account["line_user_id"]:
            raise services.Conflict("帳號已綁定，不需要綁定碼")
        return {"binding_code": services.issue_binding_code(st.db, account_id)}

    @app.patch("/api/admin/accounts/{account_id}")
    def patch_account(account_id: int, body: AccountPatch, st: State = Depends(get_state),
                      p: Principal = Depends(get_principal)):
        require(p, "admin")
        if body.status != "disabled":
            raise services.ServiceError("只支援停用帳號")
        account = services.disable_account(st.db, account_id)
        if st.line and account["line_user_id"]:
            st.line.unlink_menu(account["line_user_id"])
        return account

    # ---------- LINE webhook ----------

    @app.post("/callback")
    async def callback(request: Request, x_line_signature: str = Header(default=""),
                       st: State = Depends(get_state)):
        if st.line is None:
            raise HTTPException(503, "LINE 尚未設定")
        body = (await request.body()).decode("utf-8")
        events = await asyncio.to_thread(st.line.parse_events, body, x_line_signature)
        # 驗證簽章後立即回應 LINE；事件在背景處理（async-backend 第 4.3 節）
        st.runner.submit(st.line.process_events, events)
        return {"ok": True}

