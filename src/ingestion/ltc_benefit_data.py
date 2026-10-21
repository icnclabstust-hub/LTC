"""長照給付結構化資料（2026-10-19 ltc-kb-graph 第 5.1 節）。

數字逐項取自官方原文，不經 LLM 或推估；改版時更新本檔並調整 VALID_FROM，舊資料由載入程式設 valid_to：
- 長期照顧服務申請及給付辦法（114-06-19 修正，114-09-01 施行）第 14 條、附表二（額度）、附表三（交通分區）、附表五（部分負擔），
  全國法規資料庫 pcode=L0070059
- 衛福部 1966 長照專區：申請長照服務、喘息服務、專業服務、交通接送服務、輔具及居家無障礙環境改善頁面
"""

from datetime import date

LAW_URL = "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0070059"
ANNEX2_URL = "https://law.moj.gov.tw/LawClass/LawGetFile.ashx?lan=C&FileId=0000398330"
ANNEX3_URL = "https://law.moj.gov.tw/LawClass/LawGetFile.ashx?lan=C&FileId=0000398334"
ANNEX5_URL = "https://law.moj.gov.tw/LawClass/LawGetFile.ashx?lan=C&FileId=0000398333"
APPLY_URL = "https://1966.gov.tw/LTC/cp-6533-70777-207.html"
RESPITE_URL = "https://1966.gov.tw/LTC/np-6454-207.html"
PROFESSIONAL_URL = "https://1966.gov.tw/LTC/cp-6451-70123-207.html"
TRANSPORT_URL = "https://1966.gov.tw/LTC/cp-6452-69937-207.html"
ASSISTIVE_URL = "https://1966.gov.tw/LTC/cp-6453-69940-207.html"

LAW_NAME = "長期照顧服務申請及給付辦法"
# 114-06-19 修正條文自 114-09-01 施行（第 22 條）
VALID_FROM = date(2025, 9, 1)
# 輔具第二組、智慧科技輔具自 115-07-01 施行（第 22 條、附表二備註二）
ASSISTIVE_GROUP2_FROM = date(2026, 7, 1)
# 49 歲以下失智症者、急性後期整合照護計畫收案對象自 115-01-01 起納入（1966 申請長照服務頁）
NEW_GROUPS_FROM = date(2026, 1, 1)

NEED_LEVELS = range(2, 9)

BENEFIT_CATEGORIES = [
    {"code": "care_pro", "name": "照顧及專業服務", "service_code_prefixes": "B,C", "period": "month"},
    {"code": "transport", "name": "交通接送服務", "service_code_prefixes": "D", "period": "month"},
    {"code": "assistive", "name": "輔具及居家無障礙環境改善服務", "service_code_prefixes": "E,F", "period": "3year"},
    {"code": "respite", "name": "喘息服務", "service_code_prefixes": "G", "period": "year"},
]

for _row in BENEFIT_CATEGORIES:
    _row.update(source_url=LAW_URL, source_ref=f"{LAW_NAME} 第7條、附表二", valid_from=VALID_FROM)

PERIOD_LABELS = {"month": "每月", "3year": "每三年", "year": "每年"}

# 附表二：照顧及專業服務依等級；喘息第二～六級 32,340、第七～八級 48,510（附表二合併儲存格）
_CARE_PRO_QUOTA = {2: 10020, 3: 15460, 4: 18580, 5: 24100, 6: 28070, 7: 32090, 8: 36180}
_TRANSPORT_QUOTA_BY_ZONE = {1: 1680, 2: 1840, 3: 2000, 4: 2400}
_ASSISTIVE_QUOTA_BY_GROUP = {1: 40000, 2: 60000}
_RESPITE_QUOTA = {**{level: 32340 for level in range(2, 7)}, 7: 48510, 8: 48510}


def _quota_rows() -> list[dict]:
    rows = []
    for level in NEED_LEVELS:
        rows.append({"quota_key": f"care_pro:L{level}", "category": "care_pro", "need_level": level, "zone": None,
                     "item_group": None, "amount": _CARE_PRO_QUOTA[level], "period": "month",
                     "valid_from": VALID_FROM, "source_ref": f"{LAW_NAME} 附表二"})
        for zone, amount in _TRANSPORT_QUOTA_BY_ZONE.items():
            rows.append({"quota_key": f"transport:L{level}:Z{zone}", "category": "transport", "need_level": level,
                         "zone": zone, "item_group": None, "amount": amount, "period": "month",
                         "valid_from": VALID_FROM, "source_ref": f"{LAW_NAME} 附表二"})
        for group, amount in _ASSISTIVE_QUOTA_BY_GROUP.items():
            rows.append({"quota_key": f"assistive:L{level}:G{group}", "category": "assistive", "need_level": level,
                         "zone": None, "item_group": group, "amount": amount, "period": "3year",
                         "valid_from": ASSISTIVE_GROUP2_FROM if group == 2 else VALID_FROM,
                         "source_ref": f"{LAW_NAME} 附表二"})
        rows.append({"quota_key": f"respite:L{level}", "category": "respite", "need_level": level, "zone": None,
                     "item_group": None, "amount": _RESPITE_QUOTA[level], "period": "year",
                     "valid_from": VALID_FROM, "source_ref": f"{LAW_NAME} 附表二"})
    for row in rows:
        row["source_url"] = ANNEX2_URL
    return rows


QUOTAS = _quota_rows()

# 第 14 條
IDENTITY_CLASSES = [
    {"class_no": 1, "name": "第一類（低收入戶、中低收入戶）",
     "definition": "列冊之低收入戶、中低收入戶，或符合領取中低收入老人生活津貼發給辦法第五條第一項第一款津貼資格者。"},
    {"class_no": 2, "name": "第二類（中低收入老人生活津貼、身心障礙者生活補助）",
     "definition": "非屬第一類，符合領取中低收入老人生活津貼發給辦法第五條第一項第二款津貼或身心障礙者生活補助資格者。"},
    {"class_no": 3, "name": "第三類（一般戶）", "definition": "第一類、第二類以外者。"},
]
for _row in IDENTITY_CLASSES:
    _row.update(source_url=LAW_URL, source_ref=f"{LAW_NAME} 第14條", valid_from=VALID_FROM)

# 附表五（各等級相同）：(第一類, 第二類, 第三類) 百分比
_COPAY = {
    ("care_pro", None): (0, 5, 16),
    ("transport", 1): (0, 10, 30),
    ("transport", 2): (0, 9, 27),
    ("transport", 3): (0, 8, 25),
    ("transport", 4): (0, 7, 21),
    ("assistive", None): (0, 10, 30),
    ("respite", None): (0, 5, 16),
}
COPAY_RATES = [
    {"rate_key": f"{category}:C{class_no}" + (f":Z{zone}" if zone else ""), "category": category,
     "identity_class": class_no, "zone": zone, "rate_pct": rates[class_no - 1], "source_url": ANNEX5_URL,
     "source_ref": f"{LAW_NAME} 附表五", "valid_from": VALID_FROM}
    for (category, zone), rates in _COPAY.items()
    for class_no in (1, 2, 3)
]

# 附表三
_ZONE_COUNTIES = {
    1: ("嘉義市", "新竹市", "基隆市", "臺北市"),
    2: ("彰化縣", "桃園市", "雲林縣", "新竹縣", "苗栗縣", "嘉義縣", "新北市", "宜蘭縣", "臺南市", "臺中市"),
    3: ("屏東縣", "高雄市", "南投縣"),
    4: ("臺東縣", "花蓮縣", "澎湖縣", "金門縣", "連江縣"),
}
# 附表三第四區「偏遠鄉鎮市區（計四十八個）」
REMOTE_TOWNS = {
    "新北市": ("烏來區", "石碇區", "坪林區", "平溪區", "雙溪區", "貢寮區"),
    "桃園市": ("復興區",),
    "臺南市": ("楠西區", "南化區", "左鎮區", "龍崎區"),
    "新竹縣": ("五峰鄉", "尖石鄉", "關西鎮", "峨眉鄉"),
    "苗栗縣": ("泰安鄉", "南庄鄉", "獅潭鄉", "三灣鄉"),
    "臺中市": ("和平區",),
    "南投縣": ("仁愛鄉", "信義鄉", "魚池鄉", "中寮鄉", "國姓鄉", "鹿谷鄉"),
    "嘉義縣": ("阿里山鄉", "番路鄉", "大埔鄉"),
    "高雄市": ("那瑪夏區", "桃源區", "茂林區", "田寮區", "六龜區", "甲仙區", "杉林區"),
    "屏東縣": ("三地門鄉", "霧臺鄉", "瑪家鄉", "泰武鄉", "來義鄉", "春日鄉", "獅子鄉", "牡丹鄉", "滿州鄉", "琉球鄉"),
    "宜蘭縣": ("大同鄉", "南澳鄉"),
}
TRANSPORT_ZONES = [
    {"zone_key": f"{county}", "county": county, "town": None, "zone": zone}
    for zone, counties in _ZONE_COUNTIES.items() for county in counties
] + [
    {"zone_key": f"{county}{town}", "county": county, "town": town, "zone": 4}
    for county, towns in REMOTE_TOWNS.items() for town in towns
]
for _row in TRANSPORT_ZONES:
    _row.update(source_url=ANNEX3_URL, source_ref=f"{LAW_NAME} 附表三", valid_from=VALID_FROM)

# 1966 申請長照服務頁：須經照管中心評估長照需要等級 2 級以上，並符合下列之一
ELIGIBLE_GROUPS = [
    {"group_key": "elderly_65", "name": "65歲以上老人", "valid_from": VALID_FROM},
    {"group_key": "indigenous_55", "name": "55歲以上原住民", "valid_from": VALID_FROM},
    {"group_key": "dementia_50", "name": "50歲以上失智症者", "valid_from": VALID_FROM},
    {"group_key": "dementia_under_50", "name": "49歲以下失智症者", "valid_from": NEW_GROUPS_FROM},
    {"group_key": "disabled", "name": "失能身心障礙者", "valid_from": VALID_FROM},
    {"group_key": "pac", "name": "急性後期整合照護計畫之收案對象", "valid_from": NEW_GROUPS_FROM},
]
for _row in ELIGIBLE_GROUPS:
    _row.update(source_url=APPLY_URL, source_ref="衛福部1966長照專區「申請長照服務」")

_OFFSHORE_20 = "離島及原住民地區支付價格加計20%"
_ASSISTIVE_REF = "衛福部1966長照專區「輔具及居家無障礙環境改善」長期照顧輔具租賃服務項目"
SERVICE_ITEMS = [
    # 1966 喘息服務頁「長照喘息服務照顧組合及支付價格」
    {"code": "GA03", "name": "日間照顧中心喘息服務-全日", "category": "respite", "price": 1250, "unit": "全日", "note": _OFFSHORE_20},
    {"code": "GA04", "name": "日間照顧中心喘息服務-半日", "category": "respite", "price": 625, "unit": "半日", "note": _OFFSHORE_20},
    {"code": "GA05", "name": "機構住宿式喘息服務", "category": "respite", "price": 2310, "unit": "24小時(天)", "note": _OFFSHORE_20},
    {"code": "GA06", "name": "小規模多機能夜間喘息服務", "category": "respite", "price": 2000,
     "unit": "下午6點至翌日上午8點", "note": _OFFSHORE_20},
    {"code": "GA07", "name": "巷弄長照站喘息服務", "category": "respite", "price": 170, "unit": "1小時", "note": _OFFSHORE_20},
    {"code": "GA09", "name": "居家喘息服務", "category": "respite", "price": 770, "unit": "2小時", "note": _OFFSHORE_20},
    # 1966 專業服務頁（給付金額含民眾部分負擔；原民區或離島支付加計兩成）
    {"code": "CA07", "name": "IADLs復能、ADLs復能照護", "category": "care_pro", "price": 4500, "unit": "1組(3次)"},
    {"code": "CA08", "name": "「個別化服務計畫(ISP)」擬定與執行", "category": "care_pro", "price": 6000, "unit": "1組(4次)"},
    {"code": "CB01a", "name": "營養照護", "category": "care_pro", "price": 4500, "unit": "1組(3次)"},
    {"code": "CB02", "name": "進食與吞嚥照護", "category": "care_pro", "price": 9000, "unit": "1組(6次)"},
    {"code": "CB03", "name": "困擾行為照護", "category": "care_pro", "price": 4500, "unit": "1組(3次)"},
    {"code": "CB04", "name": "臥床或長期活動受限照護", "category": "care_pro", "price": 9000, "unit": "1組(6次)"},
    {"code": "CC01", "name": "居家環境安全或無障礙空間規劃指導", "category": "care_pro", "price": 2000, "unit": "1組(2次)"},
    {"code": "CD02", "name": "居家護理指導與諮詢", "category": "care_pro", "price": 6000, "unit": "1組(4次)"},
    # 1966 交通接送服務頁
    {"code": "BD03", "name": "社區式服務交通接送", "category": "care_pro", "price": 115, "unit": "趟",
     "note": "屬照顧及專業服務額度；離島及原住民地區140元；限住家與社區式服務類長照機構距離十公里以內"},
    {"code": "DA01", "name": "交通接送（就醫、定期式復健或透析治療）", "category": "transport", "price": None, "unit": "趟",
     "note": "每趟給付價格由地方主管機關訂定公告"},
] + [
    # 1966 輔具頁「長期照顧輔具租賃服務項目」（給付方式，無價格）
    {"code": code, "name": name, "category": "assistive", "price": None, "unit": None, "note": f"給付方式：{way}"}
    for code, name, way in (
        ("EB04", "帶輪型助步車（助行椅）", "可租可購"),
        ("EC02", "輪椅-B款（輕量化量產型）", "可租可購"),
        ("EC04", "輪椅附加功能-A款（具利於移位功能）", "可租可購"),
        ("EC05", "輪椅附加功能-B款（具仰躺功能）", "可租可購"),
        ("EC06", "輪椅附加功能-C款（具空中傾倒功能）", "可租可購"),
        ("EC11", "電動輪椅", "限租賃"),
        ("EC12", "電動代步車", "限租賃"),
        ("ED07", "移位機", "可租可購"),
        ("EG01", "氣墊床-A款", "可租可購"),
        ("EG02", "氣墊床-B款", "可租可購"),
        ("EH01", "居家用照顧床", "可租可購"),
        ("EH02", "居家用照顧床-附加功能A款（床面升降功能）", "可租可購"),
        ("EH03", "居家用照顧床-附加功能B款（電動升降功能）", "可租可購"),
        ("EH04", "爬梯機", "限租賃"),
        ("EH05", "爬梯機", "限租賃"),
        ("EI01", "智慧科技輔具-移位類", "限租賃"),
        ("EI02", "智慧科技輔具-移動類", "限租賃"),
        ("EI03", "智慧科技輔具-沐浴排泄類", "限租賃"),
        ("EI04", "智慧科技輔具-居家照顧床類", "限租賃"),
        ("EI05", "智慧科技輔具-安全看視類", "限租賃"),
    )
]
_ITEM_SOURCES = {"G": (RESPITE_URL, "衛福部1966長照專區「喘息服務」照顧組合及支付價格"),
                 "C": (PROFESSIONAL_URL, "衛福部1966長照專區「專業服務」項目說明"),
                 "B": (TRANSPORT_URL, "衛福部1966長照專區「交通接送服務」"),
                 "D": (TRANSPORT_URL, "衛福部1966長照專區「交通接送服務」"),
                 "E": (ASSISTIVE_URL, _ASSISTIVE_REF)}
for _row in SERVICE_ITEMS:
    _row.setdefault("note", None)
    _row["source_url"], _row["source_ref"] = _ITEM_SOURCES[_row["code"][0]]
    _row["valid_from"] = ASSISTIVE_GROUP2_FROM if _row["code"].startswith("EI") else VALID_FROM
