import re

# 已知同音/形近錯字變體（見會議決議文件決議 3）；清單不足時可日後擴充
_WAKE_VARIANTS = ["小照你好", "小照妳好", "小昭你好", "小趙你好", "小招你好"]
_SEPARATOR = r"[\s,!！，。.?？、]*"

_PATTERNS = [
    re.compile(rf"^{_SEPARATOR}{_SEPARATOR.join(re.escape(c) for c in variant)}{_SEPARATOR}")
    for variant in _WAKE_VARIANTS
]


def match_wake_word(text: str) -> tuple[bool, str]:
    """比對開頭是否為喚醒詞（含空白/標點/已知錯字容錯）。
    回傳 (是否命中, 去除喚醒詞後的剩餘文字)；未命中則剩餘文字為原文。"""
    for pattern in _PATTERNS:
        match = pattern.match(text)
        if match:
            return True, text[match.end() :].strip()
    return False, text
