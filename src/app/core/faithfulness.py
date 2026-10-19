import re

from app.config import settings
from app.core.models import Chunk, FaithfulnessResult

# 逐句檢查（設計文件 2026-09-13 rag-backend 第 4-2 節、2026-10-19 ltc-kb-graph 第 5.4 節）：
# 原實作把整段回答拿去比對單一 chunk，回答綜合多個 chunk 並經白話改寫時幾乎都過不了門檻
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")
_MIN_SENTENCE_CHARS = 8
# 引用標示與轉介建議（系統提示詞要求附上的）不是事實陳述，不列入檢查
_BOILERPLATE = re.compile(r"更新日期|^(以上|資料來源|參考資料|來源|引用)|1966|洽詢")


def _char_bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)} or {text}


def _chunk_bigrams(cited_chunks: list[Chunk]) -> set[str]:
    grams: set[str] = set()
    for chunk in cited_chunks:
        grams |= _char_bigrams(chunk.content)
    return grams


def _citation_overlap_score(sentence: str, cited_chunks: list[Chunk]) -> float:
    """句子的 bigram 有多少比例出現在引用 chunk 的聯集中（一句話可能綜合多個 chunk）。"""
    if not cited_chunks:
        return 0.0
    sentence_grams = _char_bigrams(sentence)
    return len(sentence_grams & _chunk_bigrams(cited_chunks)) / len(sentence_grams)


def split_sentences(answer: str) -> list[str]:
    """切出要檢查的事實陳述句：去掉過短的句子、引用標示與轉介建議。"""
    sentences = (s.strip(" \t-•*·、0123456789.）)") for s in _SENTENCE_SPLIT.split(answer))
    return [s for s in sentences if len(s) >= _MIN_SENTENCE_CHARS and not _BOILERPLATE.search(s)]


class NliChecker:
    """精篩階段介面。

    正式 NLI（entailment）模型尚未選型（見設計文件第 4-2 節與第 9 節），
    此處以字元 bigram 重疊率作為暫代分數，之後可直接替換 score() 內部
    實作為真正的 NLI 模型推論，不影響 check_faithfulness() 呼叫端。
    """

    def score(self, sentence: str, cited_chunks: list[Chunk]) -> float:
        return _citation_overlap_score(sentence, cited_chunks)


def check_faithfulness(
    answer: str,
    cited_chunks: list[Chunk],
    nli_checker: NliChecker | None = None,
) -> FaithfulnessResult:
    sentences = split_sentences(answer)
    if not cited_chunks or not sentences:
        return FaithfulnessResult(
            passed=False, rule_score=0.0, nli_score=None, reason="沒有引用來源或沒有可檢查的陳述句"
        )

    threshold = settings.faithfulness_citation_overlap_threshold
    supported = [s for s in sentences if _citation_overlap_score(s, cited_chunks) >= threshold]
    rule_score = len(supported) / len(sentences)
    if rule_score < settings.faithfulness_sentence_pass_ratio:
        return FaithfulnessResult(
            passed=False,
            rule_score=rule_score,
            nli_score=None,
            reason=f"規則式粗篩未通過：{len(sentences) - len(supported)}／{len(sentences)} 句與引用來源重疊度過低",
        )

    # 精篩只看通過粗篩的句子（設計文件 4-2 節：粗篩先擋掉明顯無來源的句子）
    checker = nli_checker or NliChecker()
    nli_scores = [checker.score(s, cited_chunks) for s in supported]
    nli_score = sum(nli_scores) / len(nli_scores)
    passed = nli_score >= threshold
    reason = "通過忠實度檢查" if passed else "NLI 精篩未通過：句子與引用來源語意不一致"
    return FaithfulnessResult(passed=passed, rule_score=rule_score, nli_score=nli_score, reason=reason)
