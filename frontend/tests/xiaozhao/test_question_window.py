"""以問句為單位的語意 window（2026-10-14 question-window 設計文件第 4 節）。"""

import json

import pytest

from conftest import FakeEngines, build_audio, to_chunks
from xiaozhao import services
from xiaozhao.backend import BackendResult
from xiaozhao.db import now
from xiaozhao.pipeline import SessionProcessor
from xiaozhao.questions import insert_pause_commas, rule_question, split_questions

WAKE = "小照你好"
# 句間長停頓補「。」、句中停頓補「，」（2026-10-14 question-window 第 9.5 節）
FAMILY = "家屬今天下午會來探視，想帶水果給阿公吃，阿公有糖尿病。"
FRUIT = "香蕉跟葡萄可以吃嗎？"
BUY = "另外機構可以幫忙代購尿布嗎？"
PAIN = "我肚子很痛"
SEGMENT = "家屬今天下午會來探視，想帶水果給阿公吃，阿公有糖尿病。香蕉跟葡萄可以吃嗎，另外機構可以幫忙代購尿布嗎。我肚子很痛"
INTENTS = {FAMILY: ("資訊告知或交接", 0.9), FRUIT: ("規範詢問", 0.95), BUY: ("規範詢問", 0.95),
           PAIN: ("身體不適陳述", 0.95)}


def plain(text):
    return text


def parts(text):
    return [(p.is_question, p.text) for p in split_questions(text, plain)]


def test_pause_commas_from_asr_timestamps():
    tokens = list("阿公有糖尿病香蕉")
    times = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.6, 1.8]
    assert insert_pause_commas(tokens, times) == "阿公有糖尿病，香蕉"  # 0.6 秒：句中停頓
    times = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 2.0, 2.2]
    assert insert_pause_commas(tokens, times) == "阿公有糖尿病。香蕉"  # 1.0 秒：句子結束
    assert insert_pause_commas(tokens, times[:-1]) == "阿公有糖尿病香蕉"  # 字數不一致不補


def test_questions_split_from_statements_by_pause_commas():
    assert parts(SEGMENT) == [(False, FAMILY), (True, FRUIT), (True, BUY), (False, PAIN)]


@pytest.mark.parametrize("text, expected", [
    ("你最近怎麼越吃越少，是不是又在痛了", [(True, "你最近怎麼越吃越少？"), (True, "是不是又在痛了？")]),
    ("飯前吃，還是飯後吃", [(True, "飯前吃，還是飯後吃？")]),
    ("請問血壓偏高的時候，我可以幫他洗澡嗎", [(True, "請問血壓偏高的時候，我可以幫他洗澡嗎？")]),
    ("有什麼不舒服，就按床頭的鈴", [(False, "有什麼不舒服，就按床頭的鈴")]),
    ("我去問護理站，有沒有多的手套", [(False, "我去問護理站，有沒有多的手套")]),
])
def test_sentence_level_rules_survive_commas(text, expected):
    assert parts(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("你覺得呢", True), ("很痛苦呢", False), ("你懂什麼", False), ("你知道什麼時候吃藥嗎", True),
    ("你不要一天到晚詛咒他出車禍好不好", False), ("不要吃藥可以嗎", True), ("我們等一下去洗澡好不好", True),
    ("難道我自己的錢不能自己管嗎", False), ("你今天中午吃飽了沒", True), ("洗澡的時候水溫要調幾度", True),
    ("他這陣子好幾天都睡不好", False), ("那如果他半夜又跌倒呢", True), ("教教我吧", False),
])
def test_question_rules(text, expected):
    assert rule_question(text) is expected


class Backend:
    def __init__(self, human=()):
        self.calls, self.human = [], set(human)

    def query(self, payload):
        self.calls.append(("query", payload))
        rid = f"q{len(self.calls)}"
        if payload["intent"] in self.human:
            return BackendResult(rid, "awaiting_human", reason="轉督導")
        return BackendResult(rid, "answered", answer=f"回答：{payload['text']}")

    def scene(self, payload):
        self.calls.append(("scene", payload))
        return BackendResult("s", "no_question")


def run(db, backend, threshold=0.99, question_window=True):
    engines = FakeEngines(texts={0: WAKE, 1: SEGMENT}, intents=INTENTS, kws_schedule=[(0.9, WAKE)])
    rid = db.execute("INSERT INTO residents (caregiver_line_user_id, alias, written_consent_date, created_at)"
                     " VALUES ('Ucare', '住民 A', '2026-10-01', ?)", (now(),)).lastrowid
    db.execute("INSERT INTO sessions (id, resident_id, caregiver_line_user_id, started_at, consent_confirmed_at)"
               " VALUES ('s1', ?, 'Ucare', ?, ?)", (rid, now(), now()))
    processor = SessionProcessor("s1", rid, "Ucare", db, engines, backend, threshold,
                                 question_window=question_window)
    audio = build_audio([("speech", 0.8, 0), ("silence", 0.6), ("speech", 8, 1), ("silence", 4)])
    for seq, chunk in enumerate(to_chunks(audio)):
        processor.accept_chunk(seq, chunk)
    return db.one("SELECT * FROM turns")


def test_long_segment_split_into_windows_and_questions(db):
    run(db, Backend())
    rows = db.all("SELECT text, is_question, window_id FROM utterances ORDER BY idx")
    assert [(r["text"], r["is_question"], r["window_id"]) for r in rows] == [
        (FAMILY, 0, 0), (FRUIT, 1, 0), (BUY, 1, 1), (PAIN, 0, 2)]


def test_each_question_queried_with_related_context(db):
    backend = Backend()
    turn = run(db, backend)
    queries = [p for kind, p in backend.calls if kind == "query"]
    assert [(p.get("question_utterance_id"), p["text"]) for p in queries] == [
        (1, f"{FAMILY}{FRUIT}"),   # 相關陳述（資訊告知）＋問句
        (2, BUY),                    # 這個 window 沒有前文
        (None, PAIN),                # 不是問句的陳述仍依意圖回答
    ]
    assert queries[0]["context_utterance_ids"] == [0]
    assert turn["answer"].count("【規範詢問】") == 2  # 同意圖的兩個問句各自得到回答
    scene = [p for kind, p in backend.calls if kind == "scene"][0]
    assert [u["is_question"] for u in scene["utterances"]] == [False, True, True, False]


def test_question_prefetch_includes_context_and_is_reused(db):
    backend = Backend()
    run(db, backend, threshold=0.9)
    provisional = [p for kind, p in backend.calls if kind == "query" and p["provisional"]]
    final = [p for kind, p in backend.calls if kind == "query" and not p["provisional"]]
    assert [p["text"] for p in provisional] == [f"{FAMILY}{FRUIT}", BUY, PAIN]
    assert final == []


def test_supervisor_reply_updates_only_that_question(db):
    turn = run(db, Backend(human={"規範詢問"}))
    requests = db.all("SELECT * FROM supervisor_requests ORDER BY id")
    assert [r["question"] for r in requests] == [FRUIT, BUY]
    services.reply_supervisor_request(db, requests[0]["id"], "糖尿病可以少量吃", "督導甲")
    turn = db.one("SELECT * FROM turns WHERE id = ?", (turn["id"],))
    results = json.loads(turn["intents"])
    assert [r["status"] for r in results] == ["human_answered", "awaiting_human", "answered"]
    assert turn["answer_status"] == "awaiting_human"


def test_question_window_off_keeps_one_sentence(db):
    run(db, Backend(), question_window=False)
    rows = db.all("SELECT text, is_question, window_id FROM utterances")
    assert [(r["text"], r["is_question"], r["window_id"]) for r in rows] == [(SEGMENT, 0, None)]


def test_argument_clause_does_not_make_rest_a_question():
    assert parts("你懂什麼，他就是那個，閨女們串通好的，你可能") == [
        (False, "你懂什麼，他就是那個，閨女們串通好的，你可能")]


@pytest.mark.parametrize("text, expected", [
    # 外接麥克風錄音：問句從上一個句號之後的整句開始（句中的停頓逗號不再把問句切短）
    ("中午只喝了幾口湯。發燒的時候，可以讓他洗頭嗎",
     [(False, "中午只喝了幾口湯。"), (True, "發燒的時候，可以讓他洗頭嗎？")]),
    ("住民想要自己出院的話機構，要怎麼處理", [(True, "住民想要自己出院的話機構，要怎麼處理？")]),
    ("註明可以自己決定，不去，復健嗎", [(True, "註明可以自己決定，不去，復健嗎？")]),
    ("家屬說這個週末要帶他去看牙醫。看牙醫要，請誰陪同",
     [(False, "家屬說這個週末要帶他去看牙醫。"), (True, "看牙醫要，請誰陪同？")]),
])
def test_question_is_whole_sentence(text, expected):
    assert parts(text) == expected


def test_statements_split_at_sentence_end():
    # 陳述各自保留意圖：遇到句號就分開
    assert parts("好的我知道了。今天晚班由小陳接手。阿公你今天看起來精神很好喔") == [
        (False, "好的我知道了。"), (False, "今天晚班由小陳接手。"), (False, "阿公你今天看起來精神很好喔")]


def test_unfinished_sentence_joins_next():
    # 猶豫的長停頓被補成句號，但「把一條」明顯沒說完：併入下一句
    assert parts("我們等一下再試。可以幫我把一條。乾淨的毛巾拿過來嗎") == [
        (False, "我們等一下再試。"), (True, "可以幫我把一條，乾淨的毛巾拿過來嗎？")]


def test_haishi_after_question_is_second_half_of_choice():
    assert [(p.is_question, p.text) for p in split_questions("還是要早點，關燈睡覺比較好", plain, True)] == [
        (True, "還是要早點，關燈睡覺比較好？")]
    assert parts("還是要早點，關燈睡覺比較好") == [(False, "還是要早點，關燈睡覺比較好")]


def test_haishi_in_same_segment_after_question():
    assert parts("晚上可以讓他多看點電視嗎，還是要早點，關燈睡覺比較好") == [
        (True, "晚上可以讓他多看點電視嗎？"), (True, "還是要早點，關燈睡覺比較好？")]


def test_le_mei_split_by_comma():
    assert parts("阿公，你今天吃藥了，沒，你家人下午會來") == [
        (True, "阿公，你今天吃藥了，沒？"), (False, "你家人下午會來")]


def test_lone_qingwen_joins_next_sentence():
    # 手機實測：「請問」後停頓較長被補成句號，單獨成為「請問？」
    assert parts("早餐只吃了半碗粥。請問。血壓偏高的時候可以幫他洗澡嗎") == [
        (False, "早餐只吃了半碗粥。"), (True, "請問，血壓偏高的時候可以幫他洗澡嗎？")]
