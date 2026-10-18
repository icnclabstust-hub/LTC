import re

# 規則式遮罩只處理格式固定、誤判率低的個資；姓名、地址規則式做不準，留待 P3 評估 NER
# （見 2026-10-19 fe-be-integration 設計文件第 2 節非目標）
_NATIONAL_ID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][1289ABCD]\d{8}(?!\d)")
# 手機 09xx-xxx-xxx；市話 0x-xxxxxxx(x)。「1966」這類專線不以 0 開頭，不會被遮
_PHONE = re.compile(r"(?<!\d)(?:09\d{2}[-\s]?\d{3}[-\s]?\d{3}|0\d{1,2}[-\s]?\d{6,8})(?!\d)")

ID_PLACEHOLDER = "[身分證]"
PHONE_PLACEHOLDER = "[電話]"


def mask_pii(text: str) -> str:
    """送 LLM、寫快取鍵與資料庫前先遮罩身分證字號（含居留證）與電話；重複呼叫結果不變。"""
    text = _NATIONAL_ID.sub(ID_PLACEHOLDER, text)
    return _PHONE.sub(PHONE_PLACEHOLDER, text)
