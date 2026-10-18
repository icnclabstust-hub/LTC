"""小照服務測試共用：假模型（不載入 sherpa-onnx／BERT）與測試用設定。

假音訊編碼：振幅 0 為靜音；語音以固定振幅 0.1 + 0.01 * 句子編號 表示，
假 ASR 依振幅還原成句子文字，假 KWS 依時間表回報關鍵詞。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xiaozhao.config import SAMPLE_RATE, VAD_MIN_SILENCE_SECONDS, Settings  # noqa: E402
from xiaozhao.db import Database  # noqa: E402

ENERGY_THRESHOLD = 0.05


class FakeVad:
    """以振幅判斷語音，靜音達 min_silence 秒切句（預設與實際 VAD 設定相同）。"""

    def __init__(self, min_silence: float = VAD_MIN_SILENCE_SECONDS) -> None:
        self.min_silence = int(min_silence * SAMPLE_RATE)
        self.pos = 0
        self.start = None
        self.buffer: list[np.ndarray] = []
        self.silence = 0

    def accept(self, samples):
        out = []
        for value in samples:
            if abs(value) > ENERGY_THRESHOLD:
                if self.start is None:
                    self.start = self.pos
                    self.buffer = []
                self.buffer.append(value)
                self.silence = 0
            elif self.start is not None:
                self.silence += 1
                if self.silence >= self.min_silence:
                    out.append((self.start, np.array(self.buffer, dtype=np.float32)))
                    self.start = None
            self.pos += 1
        return out

    def flush(self):
        if self.start is None:
            return []
        seg = (self.start, np.array(self.buffer, dtype=np.float32))
        self.start = None
        return [seg]

    def is_speech(self):
        return self.start is not None


class FakeKws:
    def __init__(self, schedule):
        self.schedule = sorted(schedule)
        self.pos = 0

    def accept(self, samples):
        self.pos += len(samples)
        found = [k for t, k in self.schedule if t * SAMPLE_RATE <= self.pos]
        self.schedule = [(t, k) for t, k in self.schedule if t * SAMPLE_RATE > self.pos]
        return found


class FakeEngines:
    def __init__(self, texts, intents=None, speakers=None, kws_schedule=None):
        self.texts = texts  # 句子編號 → 文字
        self.intents = intents or {}  # 文字 → (意圖, 信心)
        self.speakers = speakers or {}  # 句子編號 → 說話者編號（聲紋向量）
        self.kws_schedule = kws_schedule or []
        self.embed_calls = 0

    def new_vad(self, min_silence=VAD_MIN_SILENCE_SECONDS):
        return FakeVad(min_silence)

    def new_kws(self):
        return FakeKws(list(self.kws_schedule))

    @staticmethod
    def _sentence_id(samples):
        return round((float(np.median(np.abs(samples))) - 0.1) / 0.01)

    def transcribe(self, samples):
        return self.texts[self._sentence_id(samples)]

    def embed(self, samples):
        self.embed_calls += 1
        vector = np.zeros(8, dtype=np.float32)
        vector[self.speakers.get(self._sentence_id(samples), 0)] = 1.0
        return vector

    def classify(self, text):
        return self.intents.get(text, ("情緒安撫或閒聊", 0.6))

    def punctuate(self, text):
        # 不加標點：問句切分只依文字中的停頓逗號與規則
        return text


def build_audio(parts):
    """parts：[("speech", 秒數, 句子編號) 或 ("silence", 秒數)] → float32 音訊。"""
    chunks = []
    for part in parts:
        n = int(part[1] * SAMPLE_RATE)
        if part[0] == "silence":
            chunks.append(np.zeros(n, dtype=np.float32))
        else:
            chunks.append(np.full(n, 0.1 + 0.01 * part[2], dtype=np.float32))
    return np.concatenate(chunks)


def to_chunks(audio, seconds=0.5):
    pcm = (audio * 32767).astype("<i2")
    step = int(seconds * SAMPLE_RATE)
    return [pcm[i:i + step].tobytes() for i in range(0, len(pcm), step)]


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        channel_secret="test-secret", channel_access_token="test-token", login_channel_id="123",
        liff_id="liff-test", ngrok_domain="example.ngrok.app", admin_user_ids=frozenset({"Uadmin"}),
        data_dir=tmp_path, intent_model_dir=tmp_path, prefetch_threshold=0.9, backend_mode="mock", port=8010,
    )
