"""文字訊息開頭的喚醒詞（2026-10-19 需求決議：文字訊息免喚醒詞）。

文字訊息不需要喚醒詞；開頭有「小照你好」或說反成「你好小照」時去掉再處理。
同音／形近錯字變體沿用原 BE `core/wake_word.py`（10/11 會議決議 3），BE 不再處理喚醒詞後移到這裡。
語音的喚醒詞由 KWS（resources/keywords.txt）處理，不經過本模組。
"""

import re

_NAMES = ("小照", "小昭", "小趙", "小招")
_GREETINGS = ("你好", "妳好")
_SEPARATOR = r"[\s,!！，、。.?？:：]*"


def _spaced(word: str) -> str:
    return _SEPARATOR.join(re.escape(c) for c in word)


_VARIANTS = [f"{_spaced(n)}{_SEPARATOR}{_spaced(g)}" for n in _NAMES for g in _GREETINGS] + [
    f"{_spaced(g)}{_SEPARATOR}{_spaced(n)}" for g in _GREETINGS for n in _NAMES
]
_PREFIX = re.compile(rf"^{_SEPARATOR}(?:{'|'.join(_VARIANTS)}){_SEPARATOR}")


def strip_wake_word(text: str) -> tuple[bool, str]:
    """回傳 (開頭是否有喚醒詞, 去掉喚醒詞後的文字)。"""
    match = _PREFIX.match(text)
    if match is None:
        return False, text.strip()
    return True, text[match.end():].strip()
