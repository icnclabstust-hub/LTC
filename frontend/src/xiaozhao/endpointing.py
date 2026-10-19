"""依說話人自適應的切句／切輪門檻（設計文件 2026-10-12-ltc-awe-adaptive-endpointing-spike.md 第 5 節）。

說話人以聲紋在記憶體內分成匿名群組（01、02…），只用來決定門檻，不存檔；
每個群組依語速（每秒字數）調整門檻：講得慢的人等久一點，講得快的人等短一點。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from .config import TURN_SILENCE_SECONDS, VAD_MIN_SILENCE_SECONDS
from .engines import cosine

# 參考語速（字／秒）：語速等於此值時，門檻與固定模式相同
REFERENCE_RATE = 4.0
# 累積語音未滿這個秒數，語速視為資料不足
MIN_SPEECH_SECONDS = 3.0
SENTENCE_GAP_RANGE = (0.35, 1.2)
TURN_SILENCE_RANGE = (2.0, 6.0)
# 短於此長度的片段聲紋不可靠，不抽聲紋
MIN_EMBED_SECONDS = 1.0
# 匿名分群的餘弦門檻：與聲紋比對門檻（VOICEPRINT_THRESHOLD）分開，只影響切句門檻。
# 原 0.35 沿用聲紋 spike，但 2026-10-12 tk1、tk2 多位真人全被併成一群；
# 2026-10-13 以 tk1～3 的說話人標註掃 0.35～0.65：0.5 的分群 F1（purity 與 coverage 的調和平均）最高，平均 0.881；
# 0.35 把多人併成一群（0.749），≥ 0.65 同一人被拆成十幾群（0.634）。樣本僅 3 段、8 人，錄音增加後重掃
GROUP_THRESHOLD = 0.5
# 分群只取片段開頭這麼長：聲紋耗時約與長度成正比（本機 CPU 約 50 ms／秒，2026-10-12 實測）
GROUP_EMBED_SECONDS = 1.5

_SESSION = "session"
_CHAR = re.compile(r"\w")


def count_chars(text: str) -> int:
    """語速用的字數：只算文字與數字，不算標點與空白。"""
    return len(_CHAR.findall(text))


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(max(value, bounds[0]), bounds[1])


@dataclass
class _Profile:
    chars: int = 0
    seconds: float = 0.0

    def rate(self) -> float | None:
        if self.seconds < MIN_SPEECH_SECONDS or self.chars == 0:
            return None
        return self.chars / self.seconds


class SpeakerGroups:
    """session 內的匿名說話人分群：與各群中心比對，未達門檻即開新群。"""

    def __init__(self, threshold: float) -> None:
        self._threshold = threshold
        self._centroids: list[np.ndarray] = []
        self._counts: list[int] = []

    def assign(self, embedding: np.ndarray, allow_new: bool = True) -> str:
        """allow_new=False（短句）：不開新群，也不更新群中心，直接歸給最接近的群。"""
        norm = float(np.linalg.norm(embedding))
        vector = embedding / norm if norm else embedding
        if not allow_new and self._centroids:
            scores = [cosine(vector, centroid) for centroid in self._centroids]
            return f"{int(np.argmax(scores)) + 1:02d}"
        best, best_score = None, self._threshold
        for i, centroid in enumerate(self._centroids):
            score = cosine(vector, centroid)
            if score >= best_score:
                best, best_score = i, score
        if best is None:
            self._centroids.append(vector.copy())
            self._counts.append(1)
            best = len(self._centroids) - 1
        else:
            n = self._counts[best]
            self._centroids[best] = (self._centroids[best] * n + vector) / (n + 1)
            self._counts[best] = n + 1
        return f"{best + 1:02d}"


class AdaptiveEndpointer:
    """記錄各群組的語速，換算切句與切輪門檻（秒）。群組為 None 表示不知道是誰。"""

    def __init__(self) -> None:
        self._profiles: dict[str, _Profile] = {_SESSION: _Profile()}

    def observe(self, group: str | None, text: str, speech_seconds: float) -> None:
        chars = count_chars(text)
        keys = [_SESSION] if group is None else [_SESSION, group]
        for key in keys:
            profile = self._profiles.setdefault(key, _Profile())
            profile.chars += chars
            profile.seconds += speech_seconds

    def rate(self, group: str | None) -> float | None:
        """該群組的語速；資料不足時退回整個 session 的語速，仍不足則為 None。"""
        if group is not None and group in self._profiles:
            rate = self._profiles[group].rate()
            if rate is not None:
                return rate
        return self._profiles[_SESSION].rate()

    def sentence_gap(self, group: str | None) -> float:
        rate = self.rate(group)
        if rate is None:
            return VAD_MIN_SILENCE_SECONDS
        return _clamp(VAD_MIN_SILENCE_SECONDS * REFERENCE_RATE / rate, SENTENCE_GAP_RANGE)

    def turn_silence(self, groups: Iterable[str | None]) -> float:
        """本輪出現過的人中，以語速最慢者決定輪結束要等多久。"""
        rates = [r for r in (self.rate(g) for g in set(groups) or {None}) if r is not None]
        if not rates:
            return TURN_SILENCE_SECONDS
        return _clamp(TURN_SILENCE_SECONDS * REFERENCE_RATE / min(rates), TURN_SILENCE_RANGE)
