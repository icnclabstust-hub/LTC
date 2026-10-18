"""伺服器端自動放大（2026-10-14 external-mic 第 8 節）。

外接麥克風（例如 RODE Wireless Micro）的說話音量比手機內建麥克風小 9～14 dB，Silero VAD 在這個音量下
切句不穩定，句子常常整段連在一起。依最近說話時的音量，把音訊放大到手機的水準；只放大、不縮小。
"""

from collections import deque

import numpy as np

from .config import SAMPLE_RATE

FRAME = 480  # 30 毫秒
# 手機內建麥克風的說話音量（30 毫秒音框 RMS 的第 90 百分位）約 -17 dBFS（2026-10-14 實測）
TARGET_DB = -18.0
MAX_GAIN_DB = 24.0
# 只用最近這麼長的「說話中」音框估計音量
HISTORY_SECONDS = 20
# 累積到這麼長的說話音框之後才開始放大
MIN_SPEECH_SECONDS = 2.0
# 低於此值的音框視為數位靜音（瀏覽器降噪會輸出全零），不列入音量估計
SILENT_DB = -90.0


def frame_db(samples: np.ndarray) -> np.ndarray:
    """每 30 毫秒音框的 RMS（dBFS）；不足一個音框的尾巴不計。"""
    n = len(samples) // FRAME * FRAME
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = samples[:n].reshape(-1, FRAME)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)


class AutoGain:
    def __init__(self) -> None:
        self._levels: deque[float] = deque(maxlen=int(HISTORY_SECONDS * SAMPLE_RATE / FRAME))
        self.gain_db = 0.0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if self.gain_db == 0.0:
            return samples
        return np.clip(samples * 10 ** (self.gain_db / 20), -1.0, 1.0).astype(np.float32)

    def observe(self, raw: np.ndarray, speech: bool) -> bool:
        """以放大前的音訊更新音量估計；只用 VAD 判定為說話中的片段。回傳增益是否改變超過 1 dB。"""
        if not speech:
            return False
        self._levels.extend(float(d) for d in frame_db(raw) if d > SILENT_DB)
        if len(self._levels) < MIN_SPEECH_SECONDS * SAMPLE_RATE / FRAME:
            return False
        level = float(np.percentile(np.fromiter(self._levels, dtype=np.float32), 90))
        gain_db = float(np.clip(TARGET_DB - level, 0.0, MAX_GAIN_DB))
        changed = abs(gain_db - self.gain_db) >= 1.0
        self.gain_db = gain_db
        return changed
