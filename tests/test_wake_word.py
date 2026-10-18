import pytest

from app.core.wake_word import match_wake_word

_POSITIVE_CASES = [
    ("小照你好", ""),
    ("小照你好，喘息服務怎麼申請？", "喘息服務怎麼申請？"),
    ("小照 你好 請問輔具補助", "請問輔具補助"),
    ("小照你好！今天天氣如何", "今天天氣如何"),
    ("小昭你好，我要問法規", "我要問法規"),
    ("小趙你好", ""),
    ("小照妳好，跌倒怎麼辦", "跌倒怎麼辦"),
]


@pytest.mark.parametrize("raw,expected_remainder", _POSITIVE_CASES)
def test_matches_known_wake_word_variants(raw, expected_remainder):
    matched, remainder = match_wake_word(raw)
    assert matched
    assert remainder == expected_remainder


@pytest.mark.parametrize(
    "text",
    [
        "請問喘息服務怎麼申請？",
        "今天天氣真好",
        "小照是誰",
        "你好小照",
        "長照機構評鑑是誰負責",
    ],
)
def test_does_not_match_without_wake_word(text):
    matched, remainder = match_wake_word(text)
    assert not matched
    assert remainder == text
