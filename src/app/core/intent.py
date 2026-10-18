from app.core.models import Intent

_IDENTITY_KEYWORDS = ("你是誰", "妳是誰", "你叫什麼", "妳叫什麼", "你是什麼")
_LTC_POINTS_KEYWORDS = ("據點", "哪裡有", "哪裡找", "地址", "附近", "怎麼找")
_LAW_SOP_KEYWORDS = ("法規", "規定", "條例", "SOP", "流程", "怎麼做", "可以嗎", "標準", "機構", "負責", "定義")


def classify_intent(query: str) -> Intent:
    # 身份問題須先於證據判定攔下，否則檢索不到相關法規會被判「證據不足」而拒答
    if any(keyword in query for keyword in _IDENTITY_KEYWORDS):
        return Intent.IDENTITY
    if any(keyword in query for keyword in _LTC_POINTS_KEYWORDS):
        return Intent.LTC_POINTS
    if any(keyword in query for keyword in _LAW_SOP_KEYWORDS):
        return Intent.LAW_SOP
    return Intent.LAW_SOP
