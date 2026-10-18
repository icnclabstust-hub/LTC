"""拒答分流的規則式判斷（2026-10-19 fe-be-integration 設計文件第 5.1 節）。

這是 P3 sufficient-context judge 上線前的暫代：規則以 eval/golden_queries.csv 校對，
寧可多轉督導也不要答錯（醫療、用藥、個案安全一律轉督導）。
"""

import re

# 醫療、用藥、個案安全：小照不提供建議，一律轉督導。
# 「代領藥／代購藥物」是服務範圍問題（題庫 Q068），不算用藥判斷，故排除前一字為領／購的「藥」
_SENSITIVE = re.compile(
    r"(?<![領購])藥(?!局)|打針|注射|胰島素|抽痰|灌食|鼻胃管|換藥|傷口|病情|保健食品|尿袋|血尿|"
    r"血壓|血糖|發燒|騷擾|毛手毛腳|受虐|虐待|家暴|疏忽|沒人照顧|打他|打她|自殺|不想活"
)

# 去掉這些泛用字詞後幾乎不剩內容的短句，代表沒說清楚是哪項服務或什麼身分，須先追問。
# 依長度由長到短替換，避免「這種事」先被「這種」吃掉而留下「事」
_VAGUE_TOKENS = sorted(
    (
        "這種事", "我們這邊", "是什麼", "在哪裡", "自付額", "這個", "那個", "這種", "我們", "這邊",
        "什麼", "怎麼", "申請", "可以", "找誰", "表格", "哪裡", "多久", "一次", "補助", "多少",
        "規定", "他", "她", "它", "的", "要", "用", "嗎", "算", "呢", "啊",
    ),
    key=len,
    reverse=True,
)
_PUNCTUATION = re.compile(r"[\s\?？!！，,。.、:：；;~～]")
_AMBIGUOUS_MAX_LENGTH = 12

# 長照領域詞：證據不足時，有領域詞代表「知識庫該有但沒有」（kb_missing，轉督導），
# 沒有則視為與長照無關（out_of_scope，只記錄）
_DOMAIN_TERMS = (
    "長照", "照顧", "照護", "照服", "個案", "居服", "居家", "督導", "喘息", "輔具", "失智", "失能",
    "補助", "給付", "申請", "1966", "照專", "機構", "服務", "長輩", "阿公", "阿嬤", "阿伯", "阿母",
    "家屬", "外看", "外勞", "外籍", "看護", "評鑑", "積分", "換班", "請假", "同事", "日照", "據點",
    "接送", "沐浴", "營養", "無障礙", "CMS", "BA", "身障", "低收", "住院", "出院", "SOP", "法規",
    "規定", "通報", "打卡", "紀錄",
)


def is_sensitive(text: str) -> bool:
    return _SENSITIVE.search(text) is not None


def is_ambiguous(text: str) -> bool:
    compact = _PUNCTUATION.sub("", text)
    if not compact or len(compact) > _AMBIGUOUS_MAX_LENGTH:
        return False
    for token in _VAGUE_TOKENS:
        compact = compact.replace(token, "")
    return len(compact) <= 1


def has_domain_term(text: str) -> bool:
    upper = text.upper()
    return any(term in upper for term in _DOMAIN_TERMS)
