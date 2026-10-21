"""語音辨識照護用詞修正與前後文衝突提醒（2026-10-17 asr-term-correction 設計文件第 7 節）。"""

import csv
import json
from pathlib import Path

import pytest

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao import services
from xiaozhao.asr_correction import ConflictChecker, TermCorrector, TextFixer
from xiaozhao.backend import MockBackend
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor

DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture(scope="module")
def corrector():
    return TermCorrector.load()


@pytest.fixture(scope="module")
def checker():
    return ConflictChecker.load()


@pytest.mark.parametrize("text, expected", [
    ("阿公等一下兩點要去附件室做附件哦", "阿公等一下兩點要去復健室做復健哦"),
    ("麻煩幫我聯絡附件師", "麻煩幫我聯絡復健師"),
    ("蛤蟆今天早上血壓一百四十", "阿嬤今天早上血壓一百四十"),
    ("請問這種情況需要寫異常是墊鋪嗎", "請問這種情況需要寫異常事件紀錄嗎"),
    ("可以先請護理師過來幫他晾個血壓嗎", "可以先請護理師過來幫他量個血壓嗎"),
    ("機構可以幫我媽代購尿不嗎", "機構可以幫我媽代購尿布嗎"),
    # LIFF 實測第二份文稿（2026-10-17）
    ("註明，今天下午要去附件市做復件", "住民，今天下午要去復健室做復健"),
    ("茫然記得拉起來，不然會跌倒", "床欄記得拉起來，不然會跌倒"),
    ("剛剛幫，阿公出臺，有一點血絲", "剛剛幫，阿公抽痰，有一點血絲"),
    ("剛剛幫阿公抽盤有一點血絲", "剛剛幫阿公抽痰有一點血絲"),
    ("阿嬤屁股的壓床比上禮拜小一點", "阿嬤屁股的壓瘡比上禮拜小一點"),
    ("家屬想問傳奇服務和日間賬戶怎麼申請", "家屬想問喘息服務和日間照顧怎麼申請"),
    ("媽媽的鼻味管昨天晚上劃出來了", "媽媽的鼻胃管昨天晚上劃出來了"),
    ("阿嬤的，鼻尾管昨天晚上劃出來了", "阿嬤的，鼻胃管昨天晚上劃出來了"),
    ("剛剛幫，阿公出壇，有一點血絲", "剛剛幫，阿公抽痰，有一點血絲"),
    ("阿嬤的屁股上痤瘡比上禮拜小一點", "阿嬤的屁股上壓瘡比上禮拜小一點"),
    # LIFF 實測第三份文稿
    ("鼻位管要固定好，不要讓它拉掉", "鼻胃管要固定好，不要讓它拉掉"),
    ("移位管要固定好", "鼻胃管要固定好"),
    ("等一下要幫他出痰", "等一下要幫他抽痰"),
    # LIFF 實測第四份文稿
    ("等一下要用，位移機把阿嬤抱到輪椅上", "等一下要用，移位機把阿嬤抱到輪椅上"),
    ("位移腰帶要綁緊一點", "移位腰帶要綁緊一點"),
    ("搓痰機的管子要每天清洗，攜氧機顯示九十二", "抽痰機的管子要每天清洗，血氧機顯示九十二"),
    ("阿公春天困難吃飯容易嗆殼", "阿公吞嚥困難吃飯容易嗆殼"),
    ("氣血管旁邊有一點分泌物", "氣切管旁邊有一點分泌物"),
])
def test_observed_errors_corrected(corrector, text, expected):
    assert corrector.correct(text)[0] == expected


@pytest.mark.parametrize("text", [
    "他尿不出來", "尿不乾淨", "請把附件寄給我", "我們去附近走走", "媽媽下午會來", "帶著樣品進來",
    "請註明今天的日期", "他一臉茫然", "床板壞了", "新政策出臺了", "這是一個傳奇", "他想去機構附近的公園走走", "他臉上長了痤瘡",
    "他咳出痰了", "幫阿公移位到輪椅", "床單一定要拉起來", "物體的位移", "細血管旁邊", "據服務員說",
])
def test_context_keeps_real_words(corrector, text):
    assert corrector.correct(text) == (text, [])


def test_correct_training_sentences_unchanged(corrector):
    # 成功標準：正確的句子 0 句被改動（設計文件第 7 節）
    texts = []
    for path in (DATA / "care7ctx").glob("draft_batch*.csv"):
        texts += [r["text"] for r in csv.DictReader(path.open(encoding="utf-8-sig")) if r.get("text")]
    for line in (DATA / "care7" / "care7.jsonl").open(encoding="utf-8"):
        texts.append(json.loads(line).get("text", ""))
    assert len(texts) > 1000
    assert [t for t in texts if corrector.correct(t)[1]] == []


def test_only_reviewed_rows_loaded_and_big5_supported(tmp_path):
    path = tmp_path / "rules.csv"
    path.write_bytes(("編號,錯字,正確,前文,後文,審閱(OK/改/不收)\n"
                      "A,血價,血壓,,,OK\nB,雞乎,機構,,,\nC,看視,探視,,,不收\n").encode("cp950"))
    corrector = TermCorrector.load(path)
    assert [r.rule_id for r in corrector.rules] == ["A"]
    assert corrector.correct("血價雞乎看視") == ("血壓雞乎看視", ["A"])


@pytest.mark.parametrize("text, earlier, flagged", [
    ("腰還是不痛，坐久了就受不了", [], "同一句"),          # ① 三人實測
    ("不痛，可是走路都走不動了", [], "同一句"),            # ① 「走不動」（提案學生 2026-10-17 加入）
    ("腳不痛了", ["阿嬤說腳很痛"], "前面說有"),            # ③
    ("腳不痛了", [], None),                                # ② 已拿掉：只說不痛不提醒
    ("痛不痛？", [], None),                                # 正反問不算否定
    ("會不會痛", [], None),
    ("還沒退燒", [], None),                                # 沒退燒＝還在發燒
    ("已經退燒了，可是一直咳", ["昨天有發燒"], None),     # 退燒是好轉，不是矛盾
    ("今天精神不錯", [], None),
])
def test_conflicts(checker, text, earlier, flagged):
    reason = checker.check(text, earlier)
    assert (reason is None) if flagged is None else (flagged in reason)


def test_time_words_mean_recovery():
    # #9「剛剛走路的時候一直喘」→「現在沒有喘」是真的好轉（提案學生 2026-10-17）
    checker = ConflictChecker(["喘", "痛"], ["不", "沒有"], ["受不了"], [], ["現在"], ["剛剛"])
    assert checker.check("現在沒有喘", ["剛剛走路的時候一直喘"]) is None
    assert checker.check("沒有喘", ["剛剛走路的時候一直喘"]) is None
    assert checker.check("現在沒有喘", ["走路的時候一直喘"]) is None
    assert "前面說有" in checker.check("沒有喘", ["走路的時候一直喘"])


def make_processor(db, texts, intents, fixer):
    engines = FakeEngines(texts=texts, intents=intents, kws_schedule=[(0.9, "小照你好")])
    rid = db.execute("INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at)"
                     " VALUES ('Ucare', '住民 A', '2026-10-01', ?)", (now(),)).lastrowid
    db.execute("INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
               " VALUES ('s1', ?, 'Ucare', ?, ?)", (rid, now(), now()))
    return SessionProcessor("s1", rid, "Ucare", db, engines, MockBackend(), 0.9, text_fixer=fixer)


def run(db, fixer):
    texts = {0: "小照你好", 1: "麻煩幫我聯絡附件師", 2: "腰還是不痛，坐久了就受不了"}
    intents = {"麻煩幫我聯絡復健師": ("服務請求", 0.95)}
    processor = make_processor(db, texts, intents, fixer)
    audio = build_audio([("speech", 0.8, 0), ("silence", 0.5), ("speech", 1.5, 1), ("silence", 0.5),
                         ("speech", 1.5, 2), ("silence", 4)])
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)
    return db.all("SELECT * FROM utterances ORDER BY idx")


def test_pipeline_corrects_before_intent_and_keeps_original(db):
    rows = run(db, TextFixer.load(True, True))
    assert rows[0]["text"] == "麻煩幫我聯絡復健師" and rows[0]["asr_text"] == "麻煩幫我聯絡附件師"
    assert rows[0]["intent"] == "服務請求"  # 意圖用修正後的文字判斷
    assert rows[0]["review_flag"] is None
    assert rows[1]["asr_text"] is None and "受不了" in rows[1]["review_flag"]
    doc = json.loads(db.one("SELECT doc FROM records")["doc"])
    assert doc["sentences"][0]["asr_text"] == "麻煩幫我聯絡附件師"
    # 督導修改後仍保留系統產生的原文與提醒
    clean = services.validate_doc(doc)
    assert clean["sentences"][0]["asr_text"] == "麻煩幫我聯絡附件師" and clean["sentences"][1]["review_flag"]


def test_pipeline_unchanged_when_switched_off(db):
    rows = run(db, None)
    assert rows[0]["text"] == "麻煩幫我聯絡附件師" and rows[0]["asr_text"] is None and rows[1]["review_flag"] is None


def test_preroll_prepends_audio_without_overlap(db):
    # 2026-10-17：停頓後第一個字被切掉（「麻煩」→「煩」）→ 片段開頭往前補 0.3 秒，不與上一段重疊
    engines = FakeEngines(texts={}, kws_schedule=[])
    rid = db.execute("INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at)"
                     " VALUES ('Ucare', '住民 A', '2026-10-01', ?)", (now(),)).lastrowid
    db.execute("INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
               " VALUES ('s1', ?, 'Ucare', ?, ?)", (rid, now(), now()))
    processor = SessionProcessor("s1", rid, "Ucare", db, engines, MockBackend(), 0.9, preroll_seconds=0.3)
    seen = []
    processor._on_segment = lambda start, samples, *a, **k: seen.append((start, len(samples)))
    audio = build_audio([("silence", 1.0), ("speech", 1.0, 0), ("silence", 0.1), ("speech", 1.0, 1), ("silence", 2)])
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)
    sr = 16000
    (s0, n0), *rest = seen
    assert s0 == int(0.7 * sr)  # 往前補 0.3 秒
    for (a, n), (b, _) in zip(seen, seen[1:]):
        assert b >= a + n  # 不與上一段重疊
