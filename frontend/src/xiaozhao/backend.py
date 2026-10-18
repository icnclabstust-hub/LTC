"""後端組 Backend API 的呼叫端。

- mock：模擬後端；是否回答、是否轉人工的規則只為了能走完流程，不代表後端實際判斷。
- http：依後端組 API 合約（《小照前後端串接設計》①②③）呼叫：①② 回 202＋request_id，③ 輪詢取回回答
  （2026-10-14 http-backend 設計文件）。
"""

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class BackendResult:
    request_id: str
    status: str  # answered | awaiting_human | no_question
    answer: str | None = None
    reason: str | None = None
    # 送出到收到回覆的毫秒數（TrackedBackend 量測；2026-10-14 async-backend 第 7 節）
    elapsed_ms: float | None = None


class Backend(Protocol):
    def query(self, payload: dict) -> BackendResult: ...
    def scene(self, payload: dict) -> BackendResult: ...


# 設計文件 2.1 節路線 2 第 3 項：醫療、用藥、補助金額一律轉人工
_HUMAN_ONLY = re.compile(r"藥|醫|病|診|補助|金額|多少錢")
_ANSWER_INTENTS = {"規範詢問", "服務請求", "身體不適陳述"}


class MockBackend:
    """模擬後端：照「回答 or 轉人工」的規則回覆固定格式的假答案。"""

    def query(self, payload: dict) -> BackendResult:
        text = payload["text"]
        return self._decide(text, payload.get("intent"))

    def scene(self, payload: dict) -> BackendResult:
        questions = [u["text"] for u in payload["utterances"] if u.get("intent") in _ANSWER_INTENTS]
        if not questions:
            return BackendResult(request_id=str(uuid.uuid4()), status="no_question")
        return self._decide("；".join(questions), payload.get("final_intent"))

    def _decide(self, text: str, intent: str | None) -> BackendResult:
        request_id = str(uuid.uuid4())
        if _HUMAN_ONLY.search(text):
            return BackendResult(request_id, "awaiting_human", reason="醫療、用藥或補助金額，依規定轉督導")
        answer = f"（模擬後端回答，非真實法規內容）已收到你的問題：「{text}」。意圖：{intent or '未判定'}。出處：模擬資料。"
        return BackendResult(request_id, "answered", answer=answer)


class TrackedBackend:
    """所有送後端的請求都經過這裡：補上本服務產生的請求編號 client_request_id，
    並把送出的 JSON 與回應印在終端機、寫入 <log_dir>/backend_requests-YYYYMMDD.jsonl（測試用，保存 1 天）。"""

    def __init__(self, inner: Backend, log_dir: Path | None) -> None:
        self._inner = inner
        self._log_dir = log_dir
        self._lock = threading.Lock()

    def query(self, payload: dict) -> BackendResult:
        return self._send("query", payload)

    def scene(self, payload: dict) -> BackendResult:
        return self._send("scene", payload)

    def _send(self, kind: str, payload: dict) -> BackendResult:
        payload = {"client_request_id": uuid.uuid4().hex, **payload}
        t0 = time.perf_counter()
        result = getattr(self._inner, kind)(payload)
        result.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.info("送後端 %s：%s → %s（%.0f ms）", kind, json.dumps(payload, ensure_ascii=False), result.status,
                    result.elapsed_ms)
        if self._log_dir is not None:
            entry = {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "elapsed_ms": result.elapsed_ms,
                     "payload": payload, "response": asdict(result)}
            path = self._log_dir / f"backend_requests-{datetime.now():%Y%m%d}.jsonl"
            with self._lock:
                self._log_dir.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return result


# 意圖代碼（http-backend 第 4.3 節，草案，待後端組確認）
INTENT_CODES = {
    "情緒安撫或閒聊": "emotional_support",
    "拒絕或抗拒": "refusal",
    "服務請求": "service_request",
    "確認或同意": "confirmation",
    "規範詢問": "regulation_inquiry",
    "資訊告知或交接": "information_handover",
    "身體不適陳述": "discomfort_report",
}
LABEL_SET_VERSION = "awe-intent-7-v1"


class HttpBackend:
    """依後端組 API 合約：① POST /v1/query、② POST /v1/scene 回 202＋request_id，③ GET /v1/requests/{id} 輪詢。

    網路錯誤、401、5xx、error 狀態與逾時一律回 awaiting_human，讓問題轉到督導頁，不會遺失。
    """

    def __init__(self, base_url: str, token: str = "", poll_seconds: float = 0.1, timeout_seconds: float = 30.0,
                 scene_text: bool = True, transport=None, sleep=time.sleep, clock=time.monotonic) -> None:
        import httpx

        self._httpx = httpx
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_seconds,
                                    transport=transport)
        self._poll, self._timeout, self._scene_text = poll_seconds, timeout_seconds, scene_text
        self._sleep, self._clock = sleep, clock

    def query(self, payload: dict) -> BackendResult:
        body = {k: v for k, v in payload.items() if k != "client_request_id"}
        body.update({
            "request_id": payload["client_request_id"], "role": "caregiver", "user_id": None,
            "keywords": payload.get("keywords") or [],
            "client_intent": {"code": INTENT_CODES.get(payload.get("intent")), "confidence": payload.get("intent_conf")},
            "label_set_version": LABEL_SET_VERSION, "lexicon_version": None, "client_timing_ms": None,
        })
        request_id = payload["client_request_id"]
        try:
            response = self._client.post("/v1/query", json=body)
            if response.status_code not in (200, 201, 202):
                return self._failed(request_id, f"後端回應 {response.status_code}")
            request_id = (response.json() or {}).get("request_id") or request_id
            return self._poll_result(request_id)
        except self._httpx.HTTPError as exc:
            return self._failed(request_id, f"後端連線失敗：{type(exc).__name__}")

    def scene(self, payload: dict) -> BackendResult:
        utterances = payload.get("utterances") or []
        if not self._scene_text:
            utterances = [{k: v for k, v in u.items() if k != "text"} for u in utterances]
        counts: dict[str, int] = {}
        for u in utterances:
            code = INTENT_CODES.get(u.get("intent"))
            if code:
                counts[code] = counts.get(code, 0) + 1
        starts = [u["start_ms"] for u in utterances if u.get("start_ms") is not None]
        ends = [u["end_ms"] for u in utterances if u.get("end_ms") is not None]
        body = {k: v for k, v in payload.items() if k not in ("client_request_id", "utterances")}
        body.update({
            "request_id": payload["client_request_id"], "caregiver_user_ids": [], "scene": None,
            "intent_counts": counts, "segment_count": len(utterances),
            "caregiver_segment_count": sum(u.get("speaker_role") == "照護員" for u in utterances),
            "keywords": [], "duration_sec": round((max(ends) - min(starts)) / 1000, 1) if starts and ends else None,
            "label_set_version": LABEL_SET_VERSION, "lexicon_version": None, "utterances": utterances,
        })
        request_id = payload["client_request_id"]
        try:
            response = self._client.post("/v1/scene", json=body)
            if response.status_code not in (200, 201, 202):
                logger.warning("後端 scene 回應 %s", response.status_code)
        except self._httpx.HTTPError as exc:
            logger.warning("後端 scene 連線失敗：%s", exc)
        # scene 的回應不當作回答（multi-intent-turn 第 4.3 節），不輪詢
        return BackendResult(request_id, "no_question")

    def _poll_result(self, request_id: str) -> BackendResult:
        deadline = self._clock() + self._timeout
        while True:
            response = self._client.get(f"/v1/requests/{request_id}")
            if response.status_code != 200:
                return self._failed(request_id, f"後端回應 {response.status_code}")
            data = response.json()
            status = data.get("status")
            if status == "answered":
                return BackendResult(request_id, "answered", answer=data.get("answer_full") or data.get("answer_spoken"))
            if status == "human_answered":
                return BackendResult(request_id, "answered", answer=data.get("human_answer"))
            if status == "refused":
                help_id = data.get("help_request_id")
                return BackendResult(request_id, "awaiting_human",
                                     reason=f"後端拒答轉人工（求助單 {help_id}）" if help_id else "後端拒答轉人工")
            if status != "pending":
                return self._failed(request_id, f"後端狀態 {status}")
            if self._clock() >= deadline:
                return self._failed(request_id, f"後端逾時（{self._timeout:g} 秒）")
            self._sleep(self._poll)

    @staticmethod
    def _failed(request_id: str, reason: str) -> BackendResult:
        logger.warning("後端請求 %s 失敗：%s", request_id, reason)
        return BackendResult(request_id, "awaiting_human", reason=reason)


class BackgroundRunner:
    """後端呼叫的背景執行（2026-10-14 async-backend 第 4.1 節）。inline=True 時立即執行（模擬後端、單元測試）。"""

    def __init__(self, workers: int = 4, inline: bool = True) -> None:
        self.inline = inline
        self._executor = None if inline else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="backend")

    def submit(self, fn, *args) -> Future:
        if self._executor is not None:
            return self._executor.submit(self._logged, fn, *args)
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:  # 與背景執行相同：例外記錄後放進 Future
            logger.exception("後端工作失敗")
            future.set_exception(exc)
        return future

    @staticmethod
    def _logged(fn, *args):
        try:
            return fn(*args)
        except Exception:
            logger.exception("背景後端工作失敗")
            raise

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False)


def build_backend(mode: str, log_dir: Path | None = None, url: str = "", token: str = "",
                  poll_seconds: float = 0.1, timeout_seconds: float = 30.0, scene_text: bool = True) -> Backend:
    if mode == "mock":
        return TrackedBackend(MockBackend(), log_dir)
    if mode == "http":
        if not url:
            raise ValueError("BACKEND_MODE=http 時須設定 BACKEND_URL")
        return TrackedBackend(HttpBackend(url, token, poll_seconds, timeout_seconds, scene_text), log_dir)
    raise ValueError(f"BACKEND_MODE={mode} 不支援（mock、http）")
