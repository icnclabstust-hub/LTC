"""照護用詞讀音相近就比對（2026-10-17 phonetic-term-match 設計文件第 7 節）。"""

import csv
import json
from pathlib import Path

import pytest

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao.asr_correction import PhoneticMatcher, TextFixer
from xiaozhao.backend import MockBackend
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor

DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture(scope="module")
def matcher():
    return PhoneticMatcher.load()


# 已蒐集的錯誤組合中，讀音相同或只差聲調、3 個字以上的（設計文件第 3 節）
@pytest.mark.parametrize("text, expected", [
    ("媽媽的鼻味管要固定好", "媽媽的鼻胃管要固定好"),
    ("鼻位管要固定好", "鼻胃管要固定好"),
    ("鼻胃館要固定好", "鼻胃管要固定好"),
    ("麻煩幫我聯絡附件師", "麻煩幫我聯絡復健師"),             # 對到 3 字的「復健師」
    ("鼻衛管鬆掉了", "鼻胃管鬆掉了"),                      # 沒看過的同音錯法（聲調也相同）
    # 不看聲調時「附件室」「附件師」都像復健師、復健室；連聲調比對才各自換對（室 shì、師 shī）
    ("今天下午要去附件室", "今天下午要去復健室"),
])
def test_long_terms_replaced(matcher, text, expected):
    fixed, used, _ = matcher.match(text)
    assert fixed == expected and used


# 2 個字：只提醒，不改字
@pytest.mark.parametrize("text, hint", [
    ("下午要去做附件", "復健"),
    ("阿罵晚上會自己起來", "阿嬤"),
    ("屁股的壓床比上禮拜小一點", "壓瘡"),
    ("家屬想問傳息的事", "喘息"),
    # 只差聲調（提案學生 2026-10-17 選 (b)）：3 字以上也只提醒
    ("鼻尾管昨天晚上滑出來了", "鼻胃管"),
    ("家屬想問傳息服務", "喘息服務"),
    ("代購尿不嗎", "尿布"),
    ("剛剛幫他抽談", "抽痰"),
])
def test_short_terms_only_hinted(matcher, text, hint):
    fixed, used, hints = matcher.match(text)
    assert fixed == text and not used and hint in hints


def test_correct_terms_untouched(matcher):
    text = "鼻胃管和復健室都在三樓，喘息服務下個月開始"
    assert matcher.match(text) == (text, [], [])


def test_correct_sentences_never_replaced_and_few_hints(matcher):
    # 成功標準：1179 句正確句子換字 0 句、2 字提醒 ≤ 1%
    texts = []
    for path in (DATA / "care7ctx").glob("draft_batch*.csv"):
        texts += [r["text"] for r in csv.DictReader(path.open(encoding="utf-8-sig")) if r.get("text")]
    for line in (DATA / "care7" / "care7.jsonl").open(encoding="utf-8"):
        texts.append(json.loads(line).get("text", ""))
    results = [matcher.match(t) for t in texts]
    assert [t for t, (_, used, _) in zip(texts, results) if used] == []
    hinted = sum(1 for _, _, hints in results if hints)
    assert hinted <= len(texts) * 0.01


def test_pipeline_stores_hint_and_replacement(db):
    texts = {0: "小照你好", 1: "鼻位管昨天晚上滑出來了", 2: "下午要去做附件"}
    engines = FakeEngines(texts=texts, kws_schedule=[(0.9, "小照你好")])
    rid = db.execute("INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at)"
                     " VALUES ('Ucare', '住民 A', '2026-10-01', ?)", (now(),)).lastrowid
    db.execute("INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
               " VALUES ('s1', ?, 'Ucare', ?, ?)", (rid, now(), now()))
    # 只開讀音比對：2 字的「附件」只提醒（規則對照表關閉時才看得出來）
    fixer = TextFixer(None, None, PhoneticMatcher.load())
    processor = SessionProcessor("s1", rid, "Ucare", db, engines, MockBackend(), 0.9, text_fixer=fixer)
    audio = build_audio([("speech", 0.8, 0), ("silence", 0.5), ("speech", 1.5, 1), ("silence", 0.5),
                         ("speech", 1.5, 2), ("silence", 4)])
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)
    rows = db.all("SELECT text, asr_text, term_hint FROM utterances ORDER BY idx")
    assert rows[0]["text"] == "鼻胃管昨天晚上滑出來了" and rows[0]["asr_text"] == "鼻位管昨天晚上滑出來了"
    assert rows[1]["text"] == "下午要去做附件" and rows[1]["term_hint"] == "復健"
    doc = json.loads(db.one("SELECT doc FROM records")["doc"])
    assert doc["sentences"][1]["term_hint"] == "復健"


def test_protected_phrases_and_numerals_not_matched():
    # 2026-10-17 實測：「尿不出來」被提醒成「尿布」、「收縮壓一百」像「牙醫」
    matcher = PhoneticMatcher(["尿布", "牙醫"], protected=["尿不出來"])
    assert matcher.match("阿公說他尿不出來，肚子脹脹的")[2] == []
    assert matcher.match("早上擠壓一百一十")[2] == []
    assert matcher.match("代購尿不嗎")[2] == ["尿布"]
