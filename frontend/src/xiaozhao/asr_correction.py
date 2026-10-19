"""語音辨識照護用詞修正與前後文衝突提醒（2026-10-17 asr-term-correction 設計文件第 5 節）。

- 換字：依 resources/asr_corrections.csv（只用審閱為 OK 的列），錯字前後文符合條件才換；
- 衝突提醒：依 resources/review_terms.csv，只標記、不改字。
兩個檔案由提案學生審閱，可能被 Excel 另存成 Big5，讀檔時兩種編碼都支援。
"""

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

RESOURCES = Path(__file__).resolve().parent / "resources"
CONTEXT_CHARS = 4  # 前文、後文只看同一句、前後 4 個字
_NOT_YET = re.compile(r"(?:沒有|沒|不|未)$")
_NUMERALS = re.compile(r"[零一二三四五六七八九十百千萬兩]")
PROTECTED_CATEGORY = "不比對"


def _read_csv(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return list(csv.DictReader(raw.decode(encoding).splitlines()))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path.name} 編碼無法辨識")


def _reviewed(row: dict) -> bool:
    status = next((v for k, v in row.items() if k and k.startswith("審閱")), "") or ""
    return status.strip().upper() == "OK"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    wrong: str
    right: str
    before: re.Pattern | None
    after: re.Pattern | None


class TermCorrector:
    """照護用詞換字。"""

    def __init__(self, rules: list[Rule]) -> None:
        self.rules = rules

    @classmethod
    def load(cls, path: Path = RESOURCES / "asr_corrections.csv") -> "TermCorrector":
        rules = [
            Rule(r["編號"], r["錯字"], r["正確"],
                 re.compile(f"(?:{r['前文']})$") if r.get("前文") else None,
                 re.compile(r["後文"]) if r.get("後文") else None)
            for r in _read_csv(path) if _reviewed(r) and r.get("錯字") and r.get("正確")
        ]
        logger.info("照護用詞對照表：%d 條", len(rules))
        return cls(rules)

    def correct(self, text: str) -> tuple[str, list[str]]:
        """回傳 (換字後的文字, 用到的規則編號)。規則依檔案順序套用。"""
        used = []
        for rule in self.rules:
            parts, last = [], 0
            for m in re.finditer(re.escape(rule.wrong), text):
                left = text[max(0, m.start() - CONTEXT_CHARS):m.start()]
                right = text[m.end():m.end() + CONTEXT_CHARS]
                # 句首：前文為空字串，讓「^」成立
                if rule.before and not rule.before.search(left):
                    continue
                if rule.after and not rule.after.match(right):
                    continue
                parts.append(text[last:m.start()] + rule.right)
                last = m.end()
                used.append(rule.rule_id)
            if parts:
                text = "".join(parts) + text[last:]
        return text, used


class ConflictChecker:
    """前後文衝突：句子說沒事，卻同時有不舒服的線索。"""

    def __init__(self, symptoms: list[str], negations: list[str], distress: list[str],
                 recovered: list[str], now_words: list[str] = (), past_words: list[str] = ()) -> None:
        self.symptoms = symptoms
        alt = lambda words: "|".join(map(re.escape, sorted(words, key=len, reverse=True)))  # noqa: E731
        # 否定詞＋症狀，例如「不痛、沒有發燒、不太暈」
        self._negated = re.compile(f"(?:{alt(negations)})(?:再|會)?(?P<symptom>{alt(symptoms)})")
        self._recovered = re.compile(f"(?P<symptom>{alt(recovered)})") if recovered else None
        self._distress = re.compile(alt(distress))
        self._symptom = re.compile(alt(symptoms))
        # 時間詞：這句是「現在、已經…」或前一句是「剛剛、昨天…」→ 前後是不同時間點，算好轉、不算矛盾
        self._now = re.compile(alt(now_words)) if now_words else None
        self._past = re.compile(alt(past_words)) if past_words else None

    @classmethod
    def load(cls, path: Path = RESOURCES / "review_terms.csv") -> "ConflictChecker":
        groups: dict[str, list[str]] = {}
        for r in _read_csv(path):
            if _reviewed(r) and r.get("詞"):
                groups.setdefault(r["類別"], []).append(r["詞"])
        return cls(groups.get("症狀", []), groups.get("否定", []), groups.get("不舒服", []), groups.get("好轉", []),
                   groups.get("時間（現在）", []), groups.get("時間（之前）", []))

    def negated(self, text: str) -> list[tuple[int, int, str]]:
        """說沒事的片段：(開始, 結束, 症狀)。排除「痛不痛、會不會痛」這類正反問與「沒退燒」。"""
        found = []
        for m in self._negated.finditer(text):
            i = m.start()
            if 0 < i and i + 1 < len(text) and text[i - 1] == text[i + 1]:
                continue  # A不A：痛不痛、會不會痛
            found.append((m.start(), m.end(), m.group("symptom")))
        if self._recovered:
            for m in self._recovered.finditer(text):
                # 「退燒了」算說沒事；「沒退燒、還沒有退燒」是還在發燒，不算
                if not _NOT_YET.search(text[:m.start()]):
                    found.append((m.start(), m.end(), m.group("symptom")))
        return found

    def affirmed(self, text: str) -> set[str]:
        """說有症狀的部分（扣掉說沒事的片段）。"""
        negated = self.negated(text)
        out = set()
        for m in self._symptom.finditer(text):
            if not any(s <= m.start() < e for s, e, _ in negated):
                out.add(m.group())
        return out

    def check(self, text: str, earlier: list[str]) -> str | None:
        """回傳衝突原因；沒有衝突回傳 None。earlier：同一輪前面的陳述句（不含問句）。"""
        negated = self.negated(text)
        if not negated:
            return None
        said = text[negated[0][0]:negated[0][1]]
        # ① 同一句有說沒事，也有不舒服的詞
        rest = "".join(ch if not any(s <= i < e for s, e, _ in negated) else " " for i, ch in enumerate(text))
        distress = self._distress.search(rest)
        if distress:
            return f"同一句有「{said}」也有「{distress.group()}」"
        # ② 「說沒事，意圖卻判為身體不適」已拿掉：N0 對提到症狀的句子幾乎都判身體不適，
        #    不衝突句誤標 6/10（提案學生 2026-10-17 決定拿掉）
        # ③ 同一輪前面說有這個症狀；有時間詞（剛剛喘、現在沒有喘）是好轉，不提醒
        if self._now and self._now.search(text):
            return None
        for _, _, symptom in negated:
            for prev in reversed(earlier):
                if self._past and self._past.search(prev):
                    continue
                if symptom in self.affirmed(prev):
                    return f"前面說有「{symptom}」，這句說「{said}」"
        return None


class PhoneticMatcher:
    """讀音相近就比對（2026-10-17 phonetic-term-match 設計文件第 5.1 節）。

    片段的拼音去掉聲調後和長照用詞相同、但字不同：
    - 3 個字以上、連聲調都相同的候選詞剛好一個 → 直接換；
    - 其他（2 個字、只差聲調、連聲調都相同的不只一個）→ 只提醒「可能是」。
    「附件室」不看聲調像「復健師」，連聲調比對（室 shì ≠ 師 shī）才不會換錯（提案學生 2026-10-17 決定）。
    """

    def __init__(self, terms: list[str], protected: list[str] = ()) -> None:
        from pypinyin import Style, lazy_pinyin

        self._pinyin = lambda text: lazy_pinyin(text, style=Style.NORMAL, errors=lambda s: list(s))
        self._toned = lambda text: lazy_pinyin(text, style=Style.TONE3, errors=lambda s: list(s))
        self.terms = {t for t in terms if len(t) >= 2}
        index: dict[tuple[str, ...], set[str]] = {}
        for term in self.terms:
            index.setdefault(tuple(self._pinyin(term)), set()).add(term)
        self._index = index
        self._lengths = sorted({len(t) for t in self.terms}, reverse=True)
        # 不比對的說法：「尿不出來」是排尿困難，不是「尿布」（2026-10-17 實測）
        self.protected = [p for p in protected if p]

    @classmethod
    def load(cls, path: Path = RESOURCES / "care_terms.csv") -> "PhoneticMatcher":
        rows = [r for r in _read_csv(path) if _reviewed(r) and r.get("用詞")]
        terms = [r["用詞"].strip() for r in rows if (r.get("類別") or "") != PROTECTED_CATEGORY]
        protected = [r["用詞"].strip() for r in rows if (r.get("類別") or "") == PROTECTED_CATEGORY]
        logger.info("長照用詞清單（讀音比對）：%d 個，不比對的說法 %d 個", len(terms), len(protected))
        return cls(terms, protected)

    def match(self, text: str) -> tuple[str, list[str], list[str]]:
        """回傳 (換字後的文字, 換字紀錄, 2 字提醒的用詞)。由長到短、不重疊；已正確出現的用詞不再比對。"""
        covered = [False] * len(text)
        for term in [*self.terms, *self.protected]:
            for m in re.finditer(re.escape(term), text):
                covered[m.start():m.end()] = [True] * len(term)
        py, toned = self._pinyin(text), self._toned(text)
        chars, used, hints = list(text), [], []
        for n in self._lengths:
            for i in range(len(text) - n + 1):
                if any(covered[i:i + n]):
                    continue
                seg = text[i:i + n]
                candidates = self._index.get(tuple(py[i:i + n]))
                if not candidates or not all("一" <= ch <= "鿿" for ch in seg) or _NUMERALS.search(seg):
                    continue  # 含數字的片段（「收縮壓一百」像「牙醫」）不比對
                covered[i:i + n] = [True] * n
                exact = [c for c in candidates if self._toned(c) == toned[i:i + n]]
                if n >= 3 and len(exact) == 1:
                    term = exact[0]
                    chars[i:i + n] = list(term)
                    used.append(f"讀音:{term}")
                else:
                    hints.extend(sorted(candidates))
        return "".join(chars), used, hints


class TextFixer:
    """服務用：規則換字、讀音比對、衝突提醒；各自可關閉。"""

    def __init__(self, corrector: TermCorrector | None, checker: ConflictChecker | None,
                 phonetic: PhoneticMatcher | None = None) -> None:
        self.corrector = corrector
        self.checker = checker
        self.phonetic = phonetic

    @classmethod
    def load(cls, correction: bool, review_flags: bool, phonetic: bool = False) -> "TextFixer":
        return cls(TermCorrector.load() if correction else None, ConflictChecker.load() if review_flags else None,
                   PhoneticMatcher.load() if phonetic else None)

    def correct(self, text: str) -> tuple[str, list[str], list[str]]:
        """先套規則對照表，再做讀音比對；回傳 (文字, 換字紀錄, 「可能是」的用詞)。"""
        text, used = self.corrector.correct(text) if self.corrector else (text, [])
        hints = []
        if self.phonetic:
            text, more, hints = self.phonetic.match(text)
            used += more
        return text, used, hints

    def flag(self, text: str, earlier: list[str]) -> str | None:
        return self.checker.check(text, earlier) if self.checker else None
