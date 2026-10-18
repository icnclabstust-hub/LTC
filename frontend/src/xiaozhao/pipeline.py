"""錄音模式的伺服器端串流處理（設計文件 5.1 節路線 1、方案 A）。

每個錄音 session 一個 SessionProcessor：
音訊片段（16 kHz、16-bit PCM）→ 喚醒詞 KWS ＋ VAD 切句 → 切輪 →
每句：Paraformer 轉文字 → 聲紋比對（有同意時）→ 意圖 → 達門檻則預取 → 輪結束送整輪紀錄。
KWS 漏抓喚醒詞時，輪外片段只轉開頭 2 秒比對喚醒詞（結果不保存），命中即開輪。
片段中間的喚醒詞／結束詞依位置切輪（keyword_split，2026-10-13）：輪內片段依文字中關鍵詞的位置
切成幾段依序處理；輪外片段依 KWS 回報時間往前 1.5 秒切開，只處理喚醒詞之後。

切句／切輪門檻有兩種模式（Settings.endpoint_mode）：
- fixed：VAD 靜音 0.5 秒切句、輪內靜音 3 秒結束。
- adaptive：VAD 以 0.25 秒細切，再依說話人語速決定相鄰片段要合併還是斷句，
  輪結束等待時間隨本輪語速最慢者調整（2026-10-12 adaptive-endpointing spike）。
- pause_veto：VAD 以 0.25 秒細切、每段先轉文字；停頓 ≥ 0.5 秒切句，但前句明顯沒說完時不切，
  最多多等 1 秒（2026-10-13 pause-veto-service，即 semantic-merge spike 的方案 1b）。

原始音訊只留在記憶體，處理完即丟棄，不寫入磁碟。
"""

import json
import logging
import re
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import services
from .autogain import AutoGain
from .backend import Backend, BackgroundRunner
from .config import (
    ADAPTIVE_VAD_MIN_SILENCE_SECONDS,
    ANSWER_KEYWORD,
    PREFETCH_INTENTS,
    SAMPLE_RATE,
    SPEAKER_GROUP_THRESHOLD,
    TURN_SILENCE_SECONDS,
    VAD_MIN_SILENCE_SECONDS,
    WAKE_ASR_FALLBACK_SECONDS,
    WAKE_KEYWORD,
)
from .db import Database, now
from .endpointing import GROUP_EMBED_SECONDS, GROUP_THRESHOLD, MIN_EMBED_SECONDS, AdaptiveEndpointer, SpeakerGroups
from .engines import Engines
from .merging import MAX_JOIN_GAP_SECONDS, PauseVetoJudge, is_unfinished
from .questions import bare, split_questions

logger = logging.getLogger(__name__)

# 偵測到關鍵詞的位置與 VAD 句子的對應容許範圍
KEYWORD_MARGIN_SAMPLES = int(0.5 * SAMPLE_RATE)
# 關鍵詞超過這段時間仍沒有對應的句子，就當成獨立事件處理
KEYWORD_ORPHAN_SAMPLES = int(1.5 * SAMPLE_RATE)
# 沒有對應句子的喚醒詞：前面這段時間內要有 VAD 切出的語音才開輪，否則視為噪音誤觸發（電風扇等，
# 2026-10-14 external-mic 第 9 節）
ORPHAN_WAKE_SPEECH_SAMPLES = int(2.0 * SAMPLE_RATE)
# 聽到結束詞後 VAD 仍判定在說話（例如電風扇聲）時，最多等這麼久就強制斷句並結束本輪
ANSWER_MAX_WAIT_SAMPLES = int(1.5 * SAMPLE_RATE)
# 問句的前文中，送後端的「相關陳述」（question-window 第 4.4 節）
CONTEXT_INTENTS = frozenset({"資訊告知或交接", "身體不適陳述"})
# 一輪的意圖清單：信心低於此值的句子不列入（multi-intent-turn 第 4.1 節）
MIN_TURN_INTENT_CONF = 0.5
# 合併片段時，片段間補回的靜音最長長度（讓 ASR 看到自然停頓，又不拉長太多）
MAX_JOIN_SILENCE_SAMPLES = int(0.3 * SAMPLE_RATE)
# pause_veto：VAD 要約 0.25 秒語音才判定「又開始講話」，靜音計時須多等這段，
# 否則停頓 0.3～0.5 秒後接著講的話會被提早斷句（2026-10-13 重播比對發現）
SPEECH_ONSET_MARGIN_SECONDS = 0.3
# 督導回聽用音訊（correction-and-cases-tabs 第 4.7 節）：記憶體只留最近這麼長，開輪時從輪的起點補寫；
# 只存輪的起點到最後一句結束，輪外語音不寫入檔案
AUDIO_RING_SECONDS = 120
MAX_TURN_AUDIO_SECONDS = 15 * 60
# 輪外片段依 KWS 回報位置切開時往前推的長度：「小照你好」約 1 秒＋KWS 回報延遲約 0.5 秒
WAKE_LOOKBACK_SAMPLES = int(1.5 * SAMPLE_RATE)

# 轉文字結果中的喚醒詞與結束詞（ASR 可能把「照」轉成同音字）
# 「道」：2026-10-13 tk03 只轉片段開頭 2 秒時，「小趙你好」被轉成「小道你好」
_ZHAO = r"[小曉筱][照趙找兆罩召朝道]"
WAKE_PATTERN = re.compile(_ZHAO + r"[，,、\s]*你好[，,。！!\s]*")
ANSWER_PATTERN = re.compile(_ZHAO + r"[，,、\s]*[請清]回答[，,。！!\s]*")
# KWS 已聽到關鍵詞、但 ASR 把「小照」轉錯（例如「小套請回答」「趙你好」）時，只在片段開頭／結尾寬鬆刪除
# （2026-10-14 external-mic 第 8 節）
LOOSE_WAKE_PATTERN = re.compile(r"^[一-鿿]{0,2}[，,、\s]*你好[，,。！!\s]*")
# 一輪的第一句只有「〇〇你好」（例如「老趙你好」「照你也好」）：是開輪的喚醒詞本身被轉錯，不列入逐字稿
# （2026-10-14 external-mic 第 10 節）
WAKE_ONLY_PATTERN = re.compile(r"[一-鿿]{0,2}[，,、\s]*你也?好[，,。！!\s]*")
WAKE_ONLY_WINDOW_SAMPLES = int(3.0 * SAMPLE_RATE)
LOOSE_ANSWER_PATTERN = re.compile(r"[一-鿿]{0,2}[，,、\s]*[請清]回答[，,。！!？?\s]*$")


def strip_keywords(text: str) -> tuple[str, bool, bool]:
    """去掉文字中的喚醒詞與結束詞；回傳 (剩下的文字, 是否含喚醒詞, 是否含結束詞)。"""
    has_wake = bool(WAKE_PATTERN.search(text))
    has_answer = bool(ANSWER_PATTERN.search(text))
    text = WAKE_PATTERN.sub("", text)
    text = ANSWER_PATTERN.sub("", text)
    return text.strip(" ，,。"), has_wake, has_answer


_KEYWORD_PATTERN = re.compile(f"(?P<wake>{WAKE_PATTERN.pattern})|(?P<answer>{ANSWER_PATTERN.pattern})")


def split_keywords(text: str) -> list[tuple[str, str, int, int]]:
    """依出現順序把文字切成 (種類, 內容, 起字元, 迄字元)；種類為 text／wake／answer。"""
    parts, pos = [], 0
    for m in _KEYWORD_PATTERN.finditer(text):
        if m.start() > pos:
            parts.append(("text", text[pos:m.start()], pos, m.start()))
        parts.append(("wake" if m.group("wake") else "answer", m.group(0), m.start(), m.end()))
        pos = m.end()
    if pos < len(text):
        parts.append(("text", text[pos:], pos, len(text)))
    return parts


def pcm16_to_float(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


@dataclass
class _Turn:
    id: int
    seq: int
    start_sample: int
    last_activity: int
    utterances: list[dict] = field(default_factory=list)
    prefetched: dict[str, dict] = field(default_factory=dict)
    # 自適應模式：本輪出現過的匿名說話人群組（None 表示不知道是誰）
    groups: set[str | None] = field(default_factory=set)
    # 問句 window（question-window）：目前的 window 編號；問句的預取結果（以句子編號為鍵）
    window: int = 0
    prefetched_questions: dict[int, dict] = field(default_factory=dict)


@dataclass
class _Pending:
    """自適應模式中還沒斷句的暫存句（由一或多個 VAD 細片段組成）。"""
    start: int
    end: int
    parts: list[np.ndarray]
    speech_samples: int
    group: str | None
    timing: dict
    answer: bool


@dataclass
class _TextPending:
    """pause_veto 模式中還沒成句的暫存句：片段已各自轉好文字，成句時直接接字串，不重做 ASR。"""
    start: int
    end: int
    parts: list[np.ndarray]
    texts: list[str]
    timing: dict
    asr_ms: float

    @property
    def text(self) -> str:
        return "，".join(self.texts)


class SessionProcessor:
    def __init__(
        self, session_id: str, resident_id: int, caregiver_user_id: str,
        db: Database, engines: Engines, backend: Backend, prefetch_threshold: float,
        endpoint_mode: str = "fixed", keyword_split: bool = True, audio_dir: Path | None = None,
        auto_gain: bool = False, question_window: bool = False, runner: BackgroundRunner | None = None,
    ) -> None:
        self.session_id = session_id
        self.resident_id = resident_id
        self.caregiver_user_id = caregiver_user_id
        self._db = db
        self._engines = engines
        self._backend = backend
        self._prefetch_threshold = prefetch_threshold
        self._adaptive = endpoint_mode == "adaptive"
        self._pause_veto = endpoint_mode == "pause_veto"
        fine_vad = self._adaptive or self._pause_veto
        self._vad = engines.new_vad(ADAPTIVE_VAD_MIN_SILENCE_SECONDS if fine_vad else VAD_MIN_SILENCE_SECONDS)
        self._kws = engines.new_kws()
        self._lock = threading.Lock()
        self._next_seq = 0
        self._out_of_order: dict[int, tuple[bytes, float | None]] = {}
        self._fed = 0
        self._keywords: list[tuple[int, str]] = []
        self._turn: _Turn | None = None
        self._turn_count = 0
        self._utterance_count = 0
        self._stopped = False
        consent = db.one("SELECT decision FROM voice_consents WHERE resident_id = ?", (resident_id,))
        self.voiceprint_agreed = bool(consent and consent["decision"] == "agreed")
        # 送後端時辨識照護員（管理員設定的姓名）
        # 送後端的身分欄位（caregiver_name、後端 user_id、role；2026-10-19 fe-be-integration）
        self.caller = services.caller_fields(db, caregiver_user_id)
        self._endpointer = AdaptiveEndpointer() if self._adaptive else None
        # 匿名分群需要抽聲紋，只在有聲紋同意時啟用；群組只在記憶體，不存檔
        self._groups = SpeakerGroups(GROUP_THRESHOLD) if self._adaptive and self.voiceprint_agreed else None
        self._pending: _Pending | None = None
        # 匿名說話人代號（01、02…）：每次錄音內以整句聲紋即時分群，聲紋只存在記憶體、錄音結束即丟；
        # 照護員在確認分頁把代號對應到角色（map_speaker）
        self._speakers = SpeakerGroups(SPEAKER_GROUP_THRESHOLD) if self.voiceprint_agreed else None
        self._speaker_roles: dict[str, str] = {}
        self._veto_judge = PauseVetoJudge(VAD_MIN_SILENCE_SECONDS) if self._pause_veto else None
        self._text_pending: _TextPending | None = None
        # adaptive 為 spike 專用，不支援依位置切輪
        self._keyword_split = keyword_split and not self._adaptive
        # 已由文字或補抓處理過的關鍵詞：KWS 若延遲才回報，到此位置為止都忽略，避免重複開輪／結束
        self._covered_until: dict[str, int] = {}
        self._audio_dir = audio_dir
        self._ring = np.zeros(0, dtype=np.int16)
        self._ring_start = 0
        self._turn_audio: list[np.ndarray] | None = None
        self._turn_audio_start = 0
        self._turn_audio_len = 0
        # 外接麥克風音量偏小時自動放大（external-mic 第 8 節）；服務由 AUTO_GAIN 開啟，單元測試預設關閉
        self._gain = AutoGain() if auto_gain else None
        # 後端呼叫：http 模式在背景執行，不卡住音訊處理（2026-10-14 async-backend）；預設立即執行
        self._runner = runner or BackgroundRunner(inline=True)
        # 以問句為單位切句（question-window 第 4.3 節）；服務由 QUESTION_WINDOW 開啟，單元測試預設關閉。
        # pause_veto／adaptive 另有合併邏輯，不套用
        self._question_window = question_window and not self._pause_veto and not self._adaptive
        # 最近一個 VAD 句子的結束位置（判斷沒有對應句子的喚醒詞是否為噪音誤觸發）
        self._last_speech_end: int | None = None

    # ---------- 片段接收 ----------

    def accept_chunk(self, seq: int, data: bytes, client_ts_ms: float | None = None) -> dict:
        """接收一個音訊片段；以序號去重並依序處理。回傳已確認的最大連續序號。"""
        with self._lock:
            if self._stopped:
                raise services.Conflict("錄音已停止")
            if seq >= self._next_seq and seq not in self._out_of_order:
                self._out_of_order[seq] = (data, client_ts_ms)
            while self._next_seq in self._out_of_order:
                chunk, ts = self._out_of_order.pop(self._next_seq)
                self._feed(pcm16_to_float(chunk), ts)
                self._next_seq += 1
            return {"acked_seq": self._next_seq - 1, "in_turn": self._turn is not None}

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            for start, samples in self._vad.flush():
                self._on_segment(start, samples, upload_ms=None, kws_ms=0.0, vad_ms=0.0)
            if self._turn:
                self._end_turn("session_stop")
            self._stopped = True
            self._db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (now(), self.session_id))

    # ---------- 串流處理 ----------

    def _feed(self, samples: np.ndarray, client_ts_ms: float | None) -> None:
        received = time.time()
        upload_ms = (received * 1000 - client_ts_ms) if client_ts_ms else None
        raw = samples
        if self._gain is not None:
            samples = self._gain.process(raw)

        if self._audio_dir is not None:
            self._keep_audio(samples)

        t0 = time.perf_counter()
        for keyword in self._kws.accept(samples):
            self._keywords.append((self._fed + len(samples), keyword))
            logger.info("session %s：%.1f 秒偵測到「%s」", self.session_id, (self._fed + len(samples)) / SAMPLE_RATE, keyword)
        t1 = time.perf_counter()
        segments = self._vad.accept(samples)
        t2 = time.perf_counter()
        if self._gain is not None and self._gain.observe(raw, self._vad.is_speech()):
            logger.info("session %s：自動放大調整為 +%.0f dB", self.session_id, self._gain.gain_db)
        self._fed += len(samples)

        for start, seg in segments:
            self._last_speech_end = start + len(seg)
            self._on_segment(start, seg, upload_ms, kws_ms=(t1 - t0) * 1000, vad_ms=(t2 - t1) * 1000)
        self._keywords = [(pos, k) for pos, k in self._keywords if pos > self._covered_until.get(k, -1)]
        self._force_answer_end()
        self._handle_orphan_keywords()
        self._check_silence()

    def _keep_audio(self, samples: np.ndarray) -> None:
        """音訊只在記憶體保留最近 AUDIO_RING_SECONDS；輪進行中時同時累積到本輪的音訊。"""
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        self._ring = np.concatenate([self._ring, pcm])[-AUDIO_RING_SECONDS * SAMPLE_RATE:]
        self._ring_start = self._fed + len(samples) - len(self._ring)
        if self._turn_audio is not None and self._turn_audio_len < MAX_TURN_AUDIO_SECONDS * SAMPLE_RATE:
            self._turn_audio.append(pcm)
            self._turn_audio_len += len(pcm)

    def _take_keywords(self, start: int, end: int) -> list[tuple[int, str]]:
        hit = [(pos, k) for pos, k in self._keywords
               if start - KEYWORD_MARGIN_SAMPLES <= pos <= end + KEYWORD_MARGIN_SAMPLES]
        self._keywords = [(pos, k) for pos, k in self._keywords if pos > end + KEYWORD_MARGIN_SAMPLES]
        return hit

    def _handle_orphan_keywords(self) -> None:
        """VAD 沒切出句子（例如關鍵詞後緊接著講話）時，關鍵詞仍要生效。"""
        if self._vad.is_speech():
            return
        remaining = []
        for pos, keyword in self._keywords:
            if self._fed - pos < KEYWORD_ORPHAN_SAMPLES:
                remaining.append((pos, keyword))
            elif keyword == WAKE_KEYWORD:
                if self._last_speech_end is None or pos - self._last_speech_end > ORPHAN_WAKE_SPEECH_SAMPLES:
                    logger.info("session %s：%.1f 秒的「%s」附近沒有語音，視為噪音忽略",
                                self.session_id, pos / SAMPLE_RATE, keyword)
                    continue
                if self._turn:
                    self._end_turn("next_wake_word")
                self._start_turn(pos)
            elif keyword == ANSWER_KEYWORD and self._turn:
                self._end_turn("answer_keyword")
        self._keywords = remaining

    def _force_answer_end(self) -> None:
        """聽到結束詞後 VAD 一直判定在說話（電風扇等噪音）時，強制斷句，讓結束詞生效。"""
        turn = self._turn
        if turn is None or not self._vad.is_speech():
            return
        if not any(k == ANSWER_KEYWORD and self._fed - pos >= ANSWER_MAX_WAIT_SAMPLES for pos, k in self._keywords):
            return
        logger.info("session %s：聽到結束詞後仍判定在說話，強制斷句", self.session_id)
        for start, seg in self._vad.flush():
            self._last_speech_end = start + len(seg)
            self._on_segment(start, seg, upload_ms=None, kws_ms=0.0, vad_ms=0.0)
        if self._turn is turn:
            # 結束詞不在強制切出的句子範圍內（例如噪音句在結束詞之後才開始）
            self._keywords = [(pos, k) for pos, k in self._keywords if k != ANSWER_KEYWORD]
            self._end_turn("answer_keyword")

    def _check_silence(self) -> None:
        if self._turn is None or self._vad.is_speech():
            return
        if self._text_pending is not None:
            # 前句已說完：靜音 0.5 秒成句；沒說完：最多等到 1 秒（兩者都另加 VAD 偵測延遲）
            pending = self._text_pending
            wait = MAX_JOIN_GAP_SECONDS if is_unfinished(pending.text) else VAD_MIN_SILENCE_SECONDS
            if self._fed - pending.end >= (wait + SPEECH_ONSET_MARGIN_SECONDS) * SAMPLE_RATE:
                self._flush_text_pending()
            return
        if self._pending is not None:
            # 暫存句後的靜音達到該說話人的切句門檻才斷句；斷句前不判斷輪結束
            if self._fed - self._pending.end >= self._endpointer.sentence_gap(self._pending.group) * SAMPLE_RATE:
                self._flush_pending()
            return
        limit = self._endpointer.turn_silence(self._turn.groups) if self._adaptive else TURN_SILENCE_SECONDS
        if self._fed - self._turn.last_activity >= limit * SAMPLE_RATE:
            self._end_turn("silence")

    def _on_segment(self, start: int, samples: np.ndarray, upload_ms, kws_ms: float, vad_ms: float) -> None:
        end = start + len(samples)
        hits = self._take_keywords(start, end)
        timing = {"upload_ms": upload_ms, "kws_ms": kws_ms, "vad_ms": vad_ms}
        if self._keyword_split:
            self._on_segment_split(start, samples, hits, timing)
            return
        keywords = {k for _, k in hits}
        kws_wake = WAKE_KEYWORD in keywords

        if kws_wake:
            if self._turn:
                self._end_turn("next_wake_word")
            self._start_turn(start)
        elif self._turn is None and self._asr_wake(samples):
            self._start_turn(start)
            self._covered_until[WAKE_KEYWORD] = end + KEYWORD_ORPHAN_SAMPLES
            logger.info("session %s：KWS 未偵測到喚醒詞，以 ASR 補抓開輪", self.session_id)
        if self._turn is None:
            # 輪外的語音除喚醒詞補抓外不轉文字、不保存
            return

        answer = ANSWER_KEYWORD in keywords
        if self._adaptive:
            self._buffer_segment(start, samples, timing, answer)
        elif self._pause_veto:
            self._buffer_text_segment(start, samples, timing, answer)
        else:
            self._finish_sentence(start, end, samples, timing, answer, len(samples), None)

    def _on_segment_split(self, start: int, samples: np.ndarray, hits: list[tuple[int, str]], timing: dict) -> None:
        """依關鍵詞位置切輪：輪外依 KWS 時間切開；輪內依文字中關鍵詞的位置，切成幾段依序處理。"""
        end = start + len(samples)
        kws_wake = [pos for pos, k in hits if k == WAKE_KEYWORD]
        kws_answer = any(k == ANSWER_KEYWORD for _, k in hits)
        started_here = False
        if self._turn is None:
            if kws_wake:
                # 只處理喚醒詞之後；之前的輪外語音直接丟棄，不轉文字
                cut = max(start, min(kws_wake) - WAKE_LOOKBACK_SAMPLES)
                samples, start = samples[cut - start:], cut
                self._start_turn(start)
            elif self._asr_wake(samples):
                self._start_turn(start)
                logger.info("session %s：KWS 未偵測到喚醒詞，以 ASR 補抓開輪", self.session_id)
            else:
                return  # 輪外的語音除喚醒詞補抓外不轉文字、不保存
            started_here = True
            self._covered_until[WAKE_KEYWORD] = end + KEYWORD_ORPHAN_SAMPLES

        t0 = time.perf_counter()
        raw_text = self._engines.transcribe(samples)
        asr_ms = (time.perf_counter() - t0) * 1000
        parts = split_keywords(raw_text)

        if all(kind == "text" for kind, *_ in parts):
            # 文字裡找不到關鍵詞（例如 ASR 轉錯字）：沿用 KWS 以整個片段處理的作法
            if kws_wake and not started_here:
                self._end_turn("next_wake_word")
                self._start_turn(start)
            text = raw_text.strip(" ，,。")
            if kws_wake:
                text = LOOSE_WAKE_PATTERN.sub("", text, count=1)
            if kws_answer:
                text = LOOSE_ANSWER_PATTERN.sub("", text)
            self._emit_text(start, end, samples, text, timing, asr_ms)
            if kws_answer and self._turn is not None:
                self._end_turn("answer_keyword")
            return

        for kind in {kind for kind, *_ in parts if kind != "text"}:
            self._covered_until[WAKE_KEYWORD if kind == "wake" else ANSWER_KEYWORD] = end + KEYWORD_ORPHAN_SAMPLES
        if started_here:
            # 開輪用的喚醒詞本身（與其前面切剩的輪外語音）不屬於這一輪
            first_wake = next((i for i, part in enumerate(parts) if part[0] == "wake"), None)
            if first_wake is not None:
                parts = parts[first_wake + 1:]

        n_chars = len(raw_text)

        def at(char_pos: int) -> int:
            """字元位置 → 樣本位置（依字數比例估計）。"""
            return start + int(len(samples) * char_pos / n_chars)

        for kind, text, c0, c1 in parts:
            if kind == "text":
                text = text.strip(" ，,。、！!？?")
                if text and self._turn is not None:
                    a, b = at(c0), at(c1)
                    self._emit_text(a, b, samples[a - start:b - start], text, timing, asr_ms)
                    asr_ms = 0.0  # 整段的 ASR 時間只記在第一句
            elif kind == "answer":
                if self._turn is not None:
                    self._end_turn("answer_keyword")
            else:
                if self._turn is not None:
                    self._end_turn("next_wake_word")
                self._start_turn(at(c0))

    def _emit_text(self, start: int, end: int, samples: np.ndarray, text: str, timing: dict, asr_ms: float) -> None:
        """已轉好文字的一段話進入本輪：pause_veto 進暫存合併，其餘模式直接成句。"""
        self._turn.last_activity = end
        if not text:
            return
        if not self._turn.utterances and start - self._turn.start_sample <= WAKE_ONLY_WINDOW_SAMPLES \
                and WAKE_ONLY_PATTERN.fullmatch(text):
            logger.info("session %s：第 %d 輪第一句只有喚醒詞，不列入逐字稿", self.session_id, self._turn.seq)
            return
        if self._pause_veto:
            self._buffer_text(start, end, samples, text, timing, asr_ms)
        elif self._question_window:
            self._add_question_parts(start, end, samples, text, {**timing, "asr_ms": asr_ms})
        else:
            self._add_utterance(start, end, text, samples, {**timing, "asr_ms": asr_ms})

    def _add_question_parts(self, start: int, end: int, samples: np.ndarray, text: str, timing: dict) -> None:
        """依問句切開：問句單獨一句，問句前的陳述合成一句；音訊位置依字數比例推算。"""
        n = len(bare(text)) or 1
        after_question = bool(self._turn.utterances) and self._turn.utterances[-1]["is_question"]
        for k, part in enumerate(split_questions(text, self._engines.punctuate, after_question)):
            a = start + (end - start) * part.start // n
            b = start + (end - start) * part.end // n
            self._add_utterance(a, b, part.text, samples[a - start:b - start],
                                timing if k == 0 else {**timing, "asr_ms": 0.0}, part.is_question)

    def _asr_wake(self, samples: np.ndarray) -> bool:
        """KWS 漏抓時的補抓：只轉開頭幾秒比對喚醒詞；文字只在此比對，不保存、不寫日誌。"""
        if WAKE_ASR_FALLBACK_SECONDS <= 0:
            return False
        head = self._engines.transcribe(samples[:int(WAKE_ASR_FALLBACK_SECONDS * SAMPLE_RATE)])
        return bool(WAKE_PATTERN.search(head))

    def _buffer_segment(self, start: int, samples: np.ndarray, timing: dict, answer: bool) -> None:
        """自適應模式：與暫存句同一人且間隔短於該人的切句門檻就合併，否則先斷句再開新的暫存句。"""
        end = start + len(samples)
        pending = self._pending
        group = pending.group if pending else None
        if self._groups is not None and len(samples) >= MIN_EMBED_SECONDS * SAMPLE_RATE:
            group = self._groups.assign(self._engines.embed(samples[:int(GROUP_EMBED_SECONDS * SAMPLE_RATE)]))

        if pending is not None:
            same_speaker = pending.group is None or group is None or pending.group == group
            gap = start - pending.end
            if same_speaker and gap < self._endpointer.sentence_gap(pending.group or group) * SAMPLE_RATE:
                pending.parts.append(np.zeros(min(gap, MAX_JOIN_SILENCE_SAMPLES), dtype=np.float32))
                pending.parts.append(samples)
                pending.end = end
                pending.speech_samples += len(samples)
                pending.group = pending.group or group
                pending.timing = timing
                pending.answer = pending.answer or answer
                if answer:
                    self._flush_pending()
                return
            self._flush_pending()
            if self._turn is None:
                # 前一句含結束詞，輪已結束；這段話落在輪外
                return

        self._pending = _Pending(start, end, [samples], len(samples), group, timing, answer)
        if answer:
            self._flush_pending()

    def _buffer_text_segment(self, start: int, samples: np.ndarray, timing: dict, answer: bool) -> None:
        """pause_veto 模式：片段先轉文字，再依停頓與「前句是否說完」決定併入暫存句或另起一句。"""
        end = start + len(samples)
        t0 = time.perf_counter()
        text, _, text_answer = strip_keywords(self._engines.transcribe(samples))
        asr_ms = (time.perf_counter() - t0) * 1000
        answer = answer or text_answer
        self._turn.last_activity = end
        if text:
            self._buffer_text(start, end, samples, text, timing, asr_ms)
        if answer:
            # 結束詞：暫存句立即成句並結束本輪（_end_turn 會先成句）
            self._end_turn("answer_keyword")

    def _buffer_text(self, start: int, end: int, samples: np.ndarray, text: str, timing: dict, asr_ms: float) -> None:
        """pause_veto：一段已轉好文字的話，依停頓與「前句是否說完」併入暫存句或另起一句。"""
        pending = self._text_pending
        gap_s = (start - pending.end) / SAMPLE_RATE if pending else None
        if pending is not None and gap_s <= MAX_JOIN_GAP_SECONDS \
                and not self._veto_judge.should_split(pending.text, None, gap_s):
            pending.parts.append(np.zeros(max(0, min(start - pending.end, MAX_JOIN_SILENCE_SAMPLES)), dtype=np.float32))
            pending.parts.append(samples)
            pending.texts.append(text)
            pending.end = end
            pending.timing = timing
            pending.asr_ms += asr_ms
        else:
            self._flush_text_pending()
            self._text_pending = _TextPending(start, end, [samples], [text], timing, asr_ms)

    def _flush_text_pending(self) -> None:
        pending, self._text_pending = self._text_pending, None
        if pending is None or self._turn is None:
            return
        samples = pending.parts[0] if len(pending.parts) == 1 else np.concatenate(pending.parts)
        self._add_utterance(pending.start, pending.end, pending.text, samples,
                            {**pending.timing, "asr_ms": pending.asr_ms})

    def _flush_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None or self._turn is None:
            return
        samples = pending.parts[0] if len(pending.parts) == 1 else np.concatenate(pending.parts)
        self._finish_sentence(pending.start, pending.end, samples, pending.timing, pending.answer,
                              pending.speech_samples, pending.group)

    def _finish_sentence(
        self, start: int, end: int, samples: np.ndarray, timing: dict, answer: bool,
        speech_samples: int, group: str | None,
    ) -> None:
        t0 = time.perf_counter()
        raw_text = self._engines.transcribe(samples)
        asr_ms = (time.perf_counter() - t0) * 1000
        text, text_wake, text_answer = strip_keywords(raw_text)
        self._turn.last_activity = end

        if text:
            self._add_utterance(start, end, text, samples, {**timing, "asr_ms": asr_ms})
            if self._endpointer is not None:
                self._turn.groups.add(group)
                # 含喚醒詞的句子，語音長度包含喚醒詞但文字已去掉，不列入語速
                if not text_wake:
                    self._endpointer.observe(group, text, speech_samples / SAMPLE_RATE)
        if answer or text_answer:
            self._end_turn("answer_keyword")

    # ---------- 輪 ----------

    def _start_turn(self, start_sample: int) -> None:
        self._turn_count += 1
        cur = self._db.execute(
            "INSERT INTO turns (session_id, seq, started_at) VALUES (?, ?, ?)",
            (self.session_id, self._turn_count, now()),
        )
        self._turn = _Turn(id=cur.lastrowid, seq=self._turn_count, start_sample=start_sample, last_activity=self._fed)
        logger.info("session %s：第 %d 輪開始", self.session_id, self._turn_count)
        if self._audio_dir is not None:
            # 輪的起點通常在已收到的音訊之前（VAD 切出片段後才開輪），從記憶體補寫
            start = max(start_sample, self._ring_start)
            head = self._ring[start - self._ring_start:].copy()
            self._turn_audio, self._turn_audio_start, self._turn_audio_len = [head], start, len(head)

    def _save_turn_audio(self, turn: _Turn) -> None:
        chunks, start = self._turn_audio, self._turn_audio_start
        self._turn_audio = None
        if chunks is None or not turn.utterances:
            return
        audio = np.concatenate(chunks)[:max(0, turn.last_activity - start)]
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self._audio_dir / f"{turn.id}.wav"), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(SAMPLE_RATE)
            f.writeframes(audio.tobytes())

    def _end_turn(self, reason: str) -> None:
        # 暫存句屬於這一輪，先斷句（若暫存句含結束詞，會在這裡以 answer_keyword 結束本輪）
        self._flush_pending()
        self._flush_text_pending()
        turn, self._turn = self._turn, None
        if turn is None:
            return
        # 一輪多個意圖：逐句意圖去重複後全部列出，需要後端回答的意圖各自回答（multi-intent-turn）
        intents = _turn_intents(turn.utterances)
        # 同步：寫入結束與「查詢中」；回答在背景取回後再更新（async-backend 第 4.2 節）
        self._db.execute(
            "UPDATE turns SET ended_at = ?, end_reason = ?, answer_status = ?, prefetch_intents = ? WHERE id = ?",
            (now(), reason, "pending" if turn.utterances else "no_question",
             json.dumps(sorted(turn.prefetched), ensure_ascii=False), turn.id),
        )
        if turn.utterances:
            # 確認／修正紀錄：照護員、督導之後在分頁確認（correction-and-cases-tabs）
            audio_start_ms = (int(self._turn_audio_start * 1000 / SAMPLE_RATE)
                              if self._turn_audio is not None else None)
            services.create_record(self._db, turn.id, self.session_id, self.caregiver_user_id, self.resident_id,
                                   turn.utterances, self._speaker_roles, audio_start_ms)
            self._runner.submit(self._finish_turn_backend, turn, intents, reason)
        self._save_turn_audio(turn)
        logger.info("session %s：第 %d 輪結束（%s，%d 句）", self.session_id, turn.seq, reason, len(turn.utterances))

    def _finish_turn_backend(self, turn: _Turn, intents: list[dict], reason: str) -> None:
        """背景：取回每個問句、意圖的回答，送整輪紀錄，合併回答並轉督導。"""
        try:
            results = self._question_results(turn) if self._question_window else self._intent_results(turn, intents)
            # 整輪紀錄：帶意圖清單；結果不再當作回答
            self._backend.scene({
                "turn_id": turn.id, "session_id": self.session_id, **self.caller,
                "end_reason": reason,
                "utterances": [
                    {"idx": u["idx"], "text": u["text"], "speaker_code": u["speaker_label"],
                     "speaker_role": u["speaker_role"], "intent": u["intent"], "intent_conf": u["intent_conf"],
                     "is_question": u["is_question"], "window_id": u["window_id"],
                     "start_ms": u["start_ms"], "end_ms": u["end_ms"]}
                    for u in turn.utterances
                ],
                "intents": [{k: item[k] for k in ("intent", "utterance_ids", "confidence", "speaker_codes",
                                                  "speaker_roles")} for item in intents],
                "final_intent": results[0]["intent"] if results else None,
                "provisional": False,
            })
        except Exception:
            logger.exception("session %s：第 %d 輪取回回答失敗", self.session_id, turn.seq)
            self._db.execute("UPDATE turns SET answer = ?, answer_status = 'awaiting_human' WHERE id = ?",
                             ("後端處理失敗，請稍後再問或洽督導。", turn.id))
            return
        answer_text, status = services.combine_intent_answers(results)
        self._db.execute(
            "UPDATE turns SET answer = ?, answer_status = ?, final_intent = ?, intents = ? WHERE id = ?",
            (answer_text, status, results[0]["intent"] if results else None,
             json.dumps(results, ensure_ascii=False) if results else None, turn.id),
        )
        for r in results:
            if r["status"] == "awaiting_human":
                services.create_supervisor_request(
                    self._db, self.caregiver_user_id, "turn", turn.id, r["question"], r.get("reason"),
                    r["request_id"], r["intent"],
                )
        logger.info("session %s：第 %d 輪回答完成（%s）", self.session_id, turn.seq, status)

    # ---------- 句 ----------

    def _add_utterance(self, start: int, end: int, text: str, samples: np.ndarray, timing: dict,
                       is_question: bool = False) -> None:
        turn = self._turn
        idx = len(turn.utterances)

        t0 = time.perf_counter()
        role, label, source = "未辨識", None, None
        if self._speakers is not None:
            label = self._speakers.assign(self._engines.embed(samples))
            role, source = self._speaker_roles.get(label, "未辨識"), "voiceprint"
        speaker_ms = (time.perf_counter() - t0) * 1000 if self._speakers is not None else None

        t0 = time.perf_counter()
        # N0 的輸入含說話人：已對應身分用身分，尚未對應為「未辨識」（n0-intent-model 第 4.2 節）
        if hasattr(self._engines, "classify_with_owner"):
            intent, conf, owner = self._engines.classify_with_owner(text, role)
        else:
            (intent, conf), owner = self._engines.classify(text), None
        intent_ms = (time.perf_counter() - t0) * 1000

        utterance = {
            "idx": idx, "start_ms": int(start * 1000 / SAMPLE_RATE), "end_ms": int(end * 1000 / SAMPLE_RATE),
            "text": text, "speaker_role": role, "speaker_label": label, "speaker_source": source,
            "intent": intent, "intent_conf": conf,
            "is_question": is_question, "window_id": turn.window if self._question_window else None,
            "intent_owner": owner,
        }
        turn.utterances.append(utterance)
        self._db.execute(
            "INSERT INTO utterances (turn_id, idx, start_ms, end_ms, text, speaker_role, speaker_label,"
            " speaker_source, intent, intent_conf, is_question, window_id, intent_owner)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (turn.id, idx, utterance["start_ms"], utterance["end_ms"], text, role, label, source, intent, conf,
             int(is_question), utterance["window_id"], owner),
        )

        build_ms = backend_ms = None
        if is_question:
            # 問句：預取時就附上同一個 window 的相關陳述，逐句送（question-window 第 4.4 節）
            if intent in PREFETCH_INTENTS and conf >= self._prefetch_threshold:
                t0 = time.perf_counter()
                payload = self._question_payload(turn, utterance, provisional=True)
                build_ms = (time.perf_counter() - t0) * 1000
                future = self._runner.submit(self._query_result, payload)
                turn.prefetched_questions[idx] = future
                backend_ms = _elapsed(future)
            turn.window += 1
        elif intent in PREFETCH_INTENTS and conf >= self._prefetch_threshold and intent not in turn.prefetched:
            t0 = time.perf_counter()
            payload = {
                "turn_id": turn.id, "utterance_id": idx, **self.caller,
                "text": text, "speaker_code": label, "speaker_role": role,
                "intent": intent, "intent_conf": conf, "keywords": [], "provisional": True,
            }
            build_ms = (time.perf_counter() - t0) * 1000
            future = self._runner.submit(self._query_result, payload)
            turn.prefetched[intent] = future
            backend_ms = _elapsed(future)

        self._utterance_count += 1
        self._db.execute(
            "INSERT INTO latency_logs (session_id, utterance_seq, upload_ms, kws_ms, vad_ms, asr_ms, speaker_ms,"
            " intent_ms, request_build_ms, backend_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.session_id, self._utterance_count, timing["upload_ms"], timing["kws_ms"], timing["vad_ms"],
             timing["asr_ms"], speaker_ms, intent_ms, build_ms, backend_ms, now()),
        )

    def _query_result(self, payload: dict) -> dict:
        res = self._backend.query(payload)
        return {"request_id": res.request_id, "status": res.status, "answer": res.answer, "reason": res.reason,
                "elapsed_ms": res.elapsed_ms}

    def _question_payload(self, turn: _Turn, question: dict, provisional: bool) -> dict:
        """問句的 query：同一個 window 內的相關陳述（資訊告知、身體不適）＋問句。"""
        context = _question_context(turn.utterances, question)
        return {
            "turn_id": turn.id, "question_utterance_id": question["idx"],
            "context_utterance_ids": [u["idx"] for u in context], **self.caller,
            "text": "".join(u["text"].rstrip("，。！") + "。" for u in context) + question["text"],
            "speaker_code": question["speaker_label"], "speaker_role": question["speaker_role"],
            "intent": question["intent"], "intent_conf": question["intent_conf"],
            "keywords": [], "provisional": provisional,
        }

    def _question_results(self, turn: _Turn) -> list[dict]:
        """問句逐句回答；不是問句的句子仍依意圖去重複後各自回答（multi-intent-turn）。"""
        results = []
        for u in turn.utterances:
            if not u["is_question"] or u["intent"] not in PREFETCH_INTENTS \
                    or u["intent_conf"] is None or u["intent_conf"] < MIN_TURN_INTENT_CONF:
                continue
            prefetched = turn.prefetched_questions.get(u["idx"])
            payload = self._question_payload(turn, u, provisional=False)
            answer = _resolve(prefetched) if prefetched is not None else self._query_result(payload)
            results.append({"intent": u["intent"], "utterance_ids": payload["context_utterance_ids"] + [u["idx"]],
                            "confidence": u["intent_conf"], "question": u["text"], **answer})
        statements = [u for u in turn.utterances if not u["is_question"]]
        results += self._intent_results(turn, _turn_intents(statements))
        results.sort(key=lambda r: r["utterance_ids"][-1])
        return results

    def _intent_results(self, turn: _Turn, intents: list[dict]) -> list[dict]:
        results = []
        for item in intents:
            if item["intent"] not in PREFETCH_INTENTS:
                continue
            if item["intent"] in turn.prefetched:
                # 預取命中：沿用（cache-strategy 第 4 節）；背景執行時等它完成
                answer = _resolve(turn.prefetched[item["intent"]])
            else:
                answer = self._query_result({
                    "turn_id": turn.id, "utterance_ids": item["utterance_ids"],
                    **self.caller, "text": item["text"], "intent": item["intent"],
                    "intent_conf": item["confidence"], "speaker_codes": item["speaker_codes"],
                    "keywords": [], "provisional": False,
                })
            results.append({"intent": item["intent"], "utterance_ids": item["utterance_ids"],
                            "confidence": item["confidence"], "question": item["text"], **answer})
        return results

    def map_speaker(self, code: str, role: str) -> None:
        """照護員把匿名代號對應到角色：之後這個代號的句子都標成此角色（本次錄音內有效）。"""
        with self._lock:
            self._speaker_roles[code] = role
            if self._turn:
                for u in self._turn.utterances:
                    if u["speaker_label"] == code:
                        u["speaker_role"] = role


def _resolve(future) -> dict:
    """預取的結果（Future）；後端失敗時視為轉督導。"""
    try:
        return future.result()
    except Exception:
        return {"request_id": None, "status": "awaiting_human", "answer": None, "reason": "後端處理失敗",
                "elapsed_ms": None}


def _elapsed(future) -> float | None:
    """立即執行（模擬後端）時記錄後端耗時；背景執行時尚未完成，不記錄。"""
    return future.result().get("elapsed_ms") if future.done() and future.exception() is None else None


def _question_context(utterances: list[dict], question: dict) -> list[dict]:
    """問句的相關前文：同一個 window、不是問句、意圖為資訊告知或身體不適陳述的句子。"""
    return [u for u in utterances
            if u["window_id"] == question["window_id"] and u["idx"] < question["idx"]
            and not u["is_question"] and u["intent"] in CONTEXT_INTENTS]


def _turn_intents(utterances: list[dict]) -> list[dict]:
    """一輪的意圖清單：逐句意圖去重複，依第一次出現排序；附依據句子、最高信心、說話人。"""
    items: dict[str, dict] = {}
    for u in utterances:
        if not u["intent"] or u["intent_conf"] is None or u["intent_conf"] < MIN_TURN_INTENT_CONF:
            continue
        item = items.setdefault(u["intent"], {"intent": u["intent"], "utterance_ids": [], "confidence": 0.0,
                                              "speaker_codes": [], "speaker_roles": [], "texts": []})
        item["utterance_ids"].append(u["idx"])
        item["confidence"] = max(item["confidence"], u["intent_conf"])
        item["texts"].append(u["text"])
        for key, value in (("speaker_codes", u["speaker_label"]), ("speaker_roles", u["speaker_role"])):
            if value and value not in item[key]:
                item[key].append(value)
    return [{**{k: v for k, v in item.items() if k != "texts"}, "text": "；".join(item["texts"])}
            for item in items.values()]


class ReportRecorder:
    """今日狀況回報：只有照護員本人說話，不偵測喚醒詞、不判斷意圖、不做語者辨識。

    錄音期間邊收邊切句，只保留語音片段在記憶體，停止時轉文字後丟棄。
    開始說話後靜音 3 秒即自動結束（2026-10-12 手機實測後新增）。
    """

    MAX_SECONDS = 15 * 60

    def __init__(self, report_id: str, caregiver_user_id: str, engines: Engines) -> None:
        self.report_id = report_id
        self.caregiver_user_id = caregiver_user_id
        self._engines = engines
        self._vad = engines.new_vad()
        self._lock = threading.Lock()
        self._next_seq = 0
        self._out_of_order: dict[int, bytes] = {}
        self._segments: list[np.ndarray] = []
        self._fed = 0
        self._last_speech_end: int | None = None
        self._stopped = False

    def accept_chunk(self, seq: int, data: bytes) -> dict:
        with self._lock:
            if self._stopped:
                raise services.Conflict("錄音已停止")
            if seq >= self._next_seq:
                self._out_of_order.setdefault(seq, data)
            while self._next_seq in self._out_of_order:
                samples = pcm16_to_float(self._out_of_order.pop(self._next_seq))
                if self._fed + len(samples) > self.MAX_SECONDS * SAMPLE_RATE:
                    raise services.ServiceError("今日狀況回報最長 15 分鐘")
                for start, segment in self._vad.accept(samples):
                    self._segments.append(segment)
                    self._last_speech_end = start + len(segment)
                self._fed += len(samples)
                self._next_seq += 1
            return {"acked_seq": self._next_seq - 1, "auto_stop": self._silence_reached()}

    def _silence_reached(self) -> bool:
        """已經說過話，且之後靜音達 3 秒（開始說話前的靜音不算）。"""
        if self._last_speech_end is None or self._vad.is_speech():
            return False
        return self._fed - self._last_speech_end >= TURN_SILENCE_SECONDS * SAMPLE_RATE

    def finish(self) -> str:
        with self._lock:
            self._stopped = True
            segments = self._segments + [seg for _, seg in self._vad.flush()]
            self._segments = []
        texts = [self._engines.transcribe(seg) for seg in segments]
        return "\n".join(t for t in texts if t.strip())
