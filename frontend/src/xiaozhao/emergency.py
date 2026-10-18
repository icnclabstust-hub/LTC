"""緊急關鍵字與固定回覆（2026-10-19 需求決議「緊急固定回覆模板」與「喚醒詞與緊急關鍵字」）。

緊急訊息不經 LLM、不呼叫後端：在 FE 直接回固定安全指引，同時建立緊急督導請求並通知督導，
後端或模型掛掉時照護員仍能收到急救指引（2026-10-19 fe-be-integration 設計文件第 4.3 節）。

注意：以下固定回覆依需求決議表採用，**尚未經居服督導或護理人員逐條審定**；這是急救指引，正式上線前必須審定。
居服員多半一個人在個案家，所以寫「請家屬或鄰居協助」，不寫「叫同事」。
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EmergencyMatch:
    level: int  # 1 或 2
    category: str
    guidance: str


@dataclass(frozen=True)
class _Rule:
    level: int
    category: str
    pattern: re.Pattern
    guidance: str


def _rule(level: int, category: str, pattern: str, guidance: str) -> _Rule:
    return _Rule(level, category, re.compile(pattern), guidance)


_LEVEL1_RULES = (
    _rule(1, "沒有呼吸或叫不醒", r"叫不醒|沒有?(在)?呼吸|無咧喘氣|無喘氣|沒有?心跳|昏迷",
          "請立即撥打 119。依訓練進行 CPR，附近有 AED 就使用；請家屬或鄰居協助。"),
    _rule(1, "噎到", r"噎到|噎住|梗到|咳不出來|卡在喉嚨",
          "受過訓練請做哈姆立克法；無效或個案失去意識，立即撥打 119。"),
    _rule(1, "疑似中風或胸痛", r"嘴巴?歪|手.{0,2}沒有?力|半邊.{0,2}沒有?力|口齒不清|講話不清楚|胸口.{0,2}[痛悶]|胸痛|冒冷汗",
          "立即撥打 119，記下發作時間；不要給個案吃東西或喝水。"),
    _rule(1, "抽搐或昏倒", r"全身抽筋|抽搐|口吐白沫|昏倒|暈倒|失去意識",
          "讓個案側躺、移開周圍危險物品，不要往嘴裡塞東西；立即撥打 119。"),
    _rule(1, "大量出血", r"血.{0,4}止不住|流血不止|大量出血|大出血|吐血",
          "用乾淨的布直接加壓止血，立即撥打 119。"),
    _rule(1, "火災或瓦斯外洩", r"冒煙|燒焦味|起火|火災|失火|瓦斯味|瓦斯外洩|聞到瓦斯",
          "先帶個案離開現場。有瓦斯味不要開關任何電器、打開門窗；到安全的地方撥打 119。"),
    _rule(1, "疑似自傷中", r"割腕|上吊|跳樓|正在.{0,4}(自殘|傷害自己)",
          "立即撥打 119，在安全的前提下阻止並陪伴個案。"),
)

_HEAD_INJURY = _rule(1, "跌倒撞到頭", r"撞到頭|撞頭|頭.{0,2}撞到",
                     "頭部撞擊後若嗜睡、嘔吐或意識不清，請立即撥打 119，不要隨意移動個案。")
_DROWSY = re.compile(r"想睡|嗜睡|叫不醒|吐了|嘔吐|意識不清")

_LOW_BLOOD_SUGAR = re.compile(r"低血糖|血糖[^\d]{0,4}(\d{1,3})(?!\d)")
_LOW_BLOOD_SUGAR_GUIDANCE = "個案意識清楚：依指引給含糖飲料；意識不清：立即撥打 119。"
_LOW_BLOOD_SUGAR_THRESHOLD = 70

_LEVEL2_RULES = (
    _rule(2, "跌倒", r"跌倒|跋倒|摔倒|滑倒|站不起來",
          "先不要立刻扶起。確認意識、哪裡痛、肢體有沒有變形；頭部撞擊、嗜睡或無法動彈請撥打 119。"),
    _rule(2, "個案走失", r"(人|阿公|阿嬤|阿伯|阿姨|個案|長輩|爸|媽|他|她).{0,4}不見了|找不到人|走失|失蹤",
          "請立即通知家屬並在附近尋找；必要時撥打 110 報案。"),
    _rule(2, "自傷言論", r"不想活|想死|自殺|輕生|傷害自己",
          "不要讓個案獨處，移開危險物品；可撥打安心專線 1925。正在自傷請立即撥打 119。"),
    _rule(2, "暴力或疑似受虐", r"在打(他|她|個案)|打個案|家暴|虐待|被打",
          "先確保自己的安全；有立即危險請撥打 110。事後依法通報。"),
    _rule(2, "用藥錯誤或誤食", r"吃錯藥|給錯藥|藥.{0,2}吃太多|誤食|(喝|吃|吞)(到|了|下)(清潔劑|漂白水|農藥|電池|洗衣)",
          "留下藥袋或容器，不要催吐；個案有任何症狀立即撥打 119。"),
)

# 文字訊息出現這些線索時，多半是在問流程或紀錄，不是正在發生的緊急狀況（題庫 Q107–Q111、Q175、Q178）。
# 一級只認強線索，「之前」「上週」這類時間詞不降級，寧可多回一次急救指引
_STRONG_GENERAL_CUES = re.compile(r"課程|上課|教學|演練|預防|怎麼寫|紀錄|表格|哪張表|SOP|上傳|核准")
_WEAK_GENERAL_CUES = re.compile(r"上週|上禮拜|之前|以前|方法")


def detect(text: str, is_voice: bool = False) -> EmergencyMatch | None:
    """語音一律當緊急處理（不降級）；文字有一般問題線索時降為一般問題。"""
    match = _match(text)
    if match is None or is_voice:
        return match
    if _STRONG_GENERAL_CUES.search(text):
        return None
    if match.level == 2 and _WEAK_GENERAL_CUES.search(text):
        return None
    return match


def _match(text: str) -> EmergencyMatch | None:
    for rule in _LEVEL1_RULES:
        if rule.pattern.search(text):
            return EmergencyMatch(rule.level, rule.category, rule.guidance)
    sugar = _LOW_BLOOD_SUGAR.search(text)
    if sugar and (sugar.group(1) is None or int(sugar.group(1)) < _LOW_BLOOD_SUGAR_THRESHOLD):
        return EmergencyMatch(1, "疑似低血糖", _LOW_BLOOD_SUGAR_GUIDANCE)
    if _HEAD_INJURY.pattern.search(text) and _DROWSY.search(text):
        return EmergencyMatch(1, _HEAD_INJURY.category, _HEAD_INJURY.guidance)
    for rule in _LEVEL2_RULES:
        if rule.pattern.search(text):
            return EmergencyMatch(rule.level, rule.category, rule.guidance)
    return None


def reply_text(match: EmergencyMatch, agency_phone: str, supervisors_notified: bool) -> str:
    """「已通知督導」只在真的通知到時才寫，避免照護員以為督導知道了而不再聯絡。"""
    level = "一級" if match.level == 1 else "二級"
    notice = "已通知督導。" if supervisors_notified else "請盡快自行聯絡督導。"
    return f"【緊急・{level}】{match.category}\n{match.guidance}\n\n{notice}機構電話：{agency_phone}"
