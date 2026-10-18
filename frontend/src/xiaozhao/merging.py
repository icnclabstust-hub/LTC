"""語意合併切句（設計文件 2026-10-13-ltc-awe-semantic-merge-spike.md 第 3、4 節）。

VAD 以 0.25 秒細切出片段、各自轉文字後，依「合併判斷器」決定同一人的相鄰片段要不要合併成一句：
- 換人，或同一人間隔超過 MAX_JOIN_GAP_SECONDS → 一律斷句；
- 其餘交給 MergeJudge.should_split(前句文字, 下一段文字, 兩段間的停頓秒數)。

「最多多等約 1 秒」：下一段若在前句結束後 max_wait 秒內還沒說完（拿不到文字），
判斷器只能看前句本身（next_text=None）。
"""

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

MAX_JOIN_GAP_SECONDS = 1.0
DEFAULT_MAX_WAIT_SECONDS = 1.0

_PUNCT = re.compile(r"[\s，,。．.？?！!、；;：:「」『』（）()…~～-]+")


def _bare(text: str) -> str:
    return _PUNCT.sub("", text)


@dataclass
class Fragment:
    start: float
    end: float
    text: str
    speaker: str | None


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    speaker: str | None
    fragments: int = 1


@dataclass
class MergeStats:
    judge_calls: int = 0
    judge_ms: list[float] = field(default_factory=list)
    with_next_text: int = 0


class MergeJudge(Protocol):
    def should_split(self, prev_text: str, next_text: str | None, gap: float = 0.0) -> bool: ...


class KeepAllJudge:
    """方案 0（基準）：同一人、間隔 ≤ 1 秒就一律合併，不看文字。"""

    def should_split(self, prev_text: str, next_text: str | None, gap: float = 0.0) -> bool:
        return False


class RuleJudge:
    """方案 1：前句沒說完就合併；前句已說完且（以疑問語氣結尾，或下一段以新話題詞開頭）才斷。

    詞表只依 tk1～3 與 care7ctx 歸納（設計文件第 3 節），不依評估用的新錄音調整。
    """

    # 前句以這些詞結尾 → 句子還沒說完（「第一個想法就是｜」「就會有專人到｜」）
    INCOMPLETE_ENDINGS = (
        "就是", "如果", "然後", "因為", "所以", "但是", "可是", "還有", "或者", "而且", "的話",
        "跟", "和", "到", "在", "把", "讓", "給", "說", "覺得", "想", "要", "會", "可以", "是",
    )
    # 前句以疑問語氣結尾 → 一個問題已經問完（「這叫突然現象好嗎｜」）
    QUESTION_ENDINGS = ("嗎", "呢")
    # 下一段以這些詞開頭 → 開始另一件事（「…這種感覺｜所以我覺得…」）
    NEW_TOPIC_STARTS = ("所以", "那", "對了", "請問", "另外", "我們來", "好那")

    def should_split(self, prev_text: str, next_text: str | None, gap: float = 0.0) -> bool:
        prev = _bare(prev_text)
        if not prev or prev.endswith(self.INCOMPLETE_ENDINGS):
            return False
        if prev.endswith(self.QUESTION_ENDINGS):
            return True
        return next_text is not None and _bare(next_text).startswith(self.NEW_TOPIC_STARTS)


class PauseVetoJudge:
    """方案 1b（2026-10-13 開發資料加做）：固定 0.5 秒停頓切句，但前句明顯沒說完時不切。

    不依賴說話人分群（tk1～3 以系統分群時，方案 0、1 都輸給固定模式）。
    """

    def __init__(self, pause_seconds: float = 0.5) -> None:
        self._pause = pause_seconds

    def should_split(self, prev_text: str, next_text: str | None, gap: float = 0.0) -> bool:
        if gap < self._pause:
            return False
        return not is_unfinished(prev_text)


def is_unfinished(text: str) -> bool:
    """句子明顯還沒說完：結尾是「就是」「如果」「到」這類未完成詞（RuleJudge.INCOMPLETE_ENDINGS）。"""
    return _bare(text).endswith(RuleJudge.INCOMPLETE_ENDINGS)


class PunctJudge:
    """方案 2：前句＋下一段一起加標點（CT-Transformer），交界處為句末標點就斷。

    沒有下一段文字時只對前句加標點；模型通常會在句尾補句號，所以此時多半判為斷句。
    """

    END_MARKS = frozenset("。？！?!")

    def __init__(self, model_path: Path) -> None:
        import sherpa_onnx

        self._punct = sherpa_onnx.OfflinePunctuation(sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(ct_transformer=str(model_path), num_threads=1)))

    def should_split(self, prev_text: str, next_text: str | None, gap: float = 0.0) -> bool:
        prev = _bare(prev_text)
        if not prev:
            return False
        out = self._punct.add_punctuation(prev + _bare(next_text or ""))
        return _mark_after(out, len(prev)) in self.END_MARKS


def _mark_after(punctuated: str, n_chars: int) -> str:
    """加標點後的文字中，第 n_chars 個非標點字元之後緊接的標點（沒有則空字串）。"""
    seen = 0
    for i, ch in enumerate(punctuated):
        if not _PUNCT.fullmatch(ch):
            seen += 1
            if seen == n_chars:
                rest = punctuated[i + 1:i + 2]
                return rest if rest and _PUNCT.fullmatch(rest) else ""
    return ""


def merge_fragments(
    fragments: Iterable[Fragment], judge: MergeJudge, max_wait: float | None = DEFAULT_MAX_WAIT_SECONDS,
) -> tuple[list[Sentence], MergeStats]:
    """依時間排序的片段 → 句子。max_wait 為 None 表示不限等待（一律拿得到下一段文字，只作上限參考）。"""
    sentences: list[Sentence] = []
    stats = MergeStats()
    current: Sentence | None = None
    for frag in fragments:
        if current is None:
            current = Sentence(frag.start, frag.end, frag.text, frag.speaker)
            continue
        same_speaker = current.speaker is None or frag.speaker is None or current.speaker == frag.speaker
        if not same_speaker or frag.start - current.end > MAX_JOIN_GAP_SECONDS:
            sentences.append(current)
            current = Sentence(frag.start, frag.end, frag.text, frag.speaker)
            continue
        next_text = frag.text if max_wait is None or frag.end - current.end <= max_wait else None
        stats.with_next_text += next_text is not None
        t0 = time.perf_counter()
        split = judge.should_split(current.text, next_text, frag.start - current.end)
        stats.judge_ms.append((time.perf_counter() - t0) * 1000)
        stats.judge_calls += 1
        if split:
            sentences.append(current)
            current = Sentence(frag.start, frag.end, frag.text, frag.speaker)
        else:
            current.end = frag.end
            current.text = f"{current.text}，{frag.text}"
            current.speaker = current.speaker or frag.speaker
            current.fragments += 1
    if current is not None:
        sentences.append(current)
    return sentences, stats
