"""語意合併切句（2026-10-13 semantic-merge spike 設計文件第 7 節單元測試）。"""

import pytest

from xiaozhao.merging import Fragment, KeepAllJudge, PauseVetoJudge, RuleJudge, _mark_after, merge_fragments


def frags(*items):
    """items：(開始秒, 結束秒, 文字, 說話人)。"""
    return [Fragment(*item) for item in items]


class Recorder:
    """記錄判斷器收到的參數，回傳固定答案。"""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def should_split(self, prev_text, next_text, gap=0.0):
        self.calls.append((prev_text, next_text))
        return self.answer


def test_speaker_change_always_splits_without_asking_judge():
    judge = Recorder(answer=False)
    sentences, stats = merge_fragments(frags((0, 2, "你好嗎", "A"), (2.2, 3, "很好", "B")), judge)
    assert [s.text for s in sentences] == ["你好嗎", "很好"]
    assert judge.calls == [] and stats.judge_calls == 0


def test_long_gap_always_splits():
    sentences, _ = merge_fragments(frags((0, 2, "第一個想法就是", "A"), (3.5, 5, "天哪", "A")), KeepAllJudge())
    assert len(sentences) == 2


def test_keep_all_merges_same_speaker_and_counts_fragments():
    sentences, _ = merge_fragments(frags((0, 2, "第一個想法就是", "A"), (2.4, 4, "天哪今天早餐要吃什麼", "A")),
                                   KeepAllJudge())
    assert len(sentences) == 1
    assert (sentences[0].start, sentences[0].end, sentences[0].fragments) == (0, 4, 2)
    assert sentences[0].text == "第一個想法就是，天哪今天早餐要吃什麼"


def test_next_text_withheld_when_it_ends_after_max_wait():
    judge = Recorder(answer=False)
    # 下一段在前句結束後 0.4 秒開始、3.0 秒才說完：1 秒內拿不到它的文字
    merge_fragments(frags((0, 2, "我們來聊天", "A"), (2.4, 5.0, "如果現在給你們一個禮拜的假", "A")), judge, max_wait=1.0)
    assert judge.calls == [("我們來聊天", None)]
    judge = Recorder(answer=False)
    merge_fragments(frags((0, 2, "我們來聊天", "A"), (2.4, 5.0, "如果現在給你們一個禮拜的假", "A")), judge, max_wait=None)
    assert judge.calls == [("我們來聊天", "如果現在給你們一個禮拜的假")]


def test_unknown_speaker_is_treated_as_same_and_inherits():
    sentences, _ = merge_fragments(frags((0, 2, "就會有專人到", None), (2.3, 4, "府幫奶奶做評估", "B")), KeepAllJudge())
    assert len(sentences) == 1 and sentences[0].speaker == "B"


@pytest.mark.parametrize("prev, nxt, split", [
    ("第一個想法就是", "天哪今天早餐要吃什麼", False),   # 前句沒說完
    ("就會有專人到", None, False),                       # 前句沒說完，沒有下一段文字
    ("這叫突然現象好嗎", None, True),                    # 問句已問完
    ("沒錯就是這種感覺", "所以我覺得我們不能再這樣下去了", True),  # 下一段開新話題
    ("沒錯就是這種感覺", None, False),                   # 看不到下一段，預設合併
    ("跟我每天都差不多", "大腦自動開啟節能模式", False),  # 同一件事的說明
])
def test_rule_judge(prev, nxt, split):
    assert RuleJudge().should_split(prev, nxt) is split


def test_mark_after_finds_punctuation_at_junction():
    assert _mark_after("我們來聊天。如果現在放假，你想幹嘛？", 5) == "。"
    assert _mark_after("第一個想法就是天哪", 7) == ""


def test_pause_veto_judge_splits_on_pause_unless_unfinished():
    judge = PauseVetoJudge(0.5)
    assert judge.should_split("我們來聊天", None, gap=0.6) is True
    assert judge.should_split("我們來聊天", None, gap=0.3) is False
    assert judge.should_split("第一個想法就是", None, gap=0.9) is False
