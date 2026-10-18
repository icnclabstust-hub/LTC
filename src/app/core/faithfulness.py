from app.config import settings
from app.core.models import Chunk, FaithfulnessResult


def _char_bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)} or {text}


def _citation_overlap_score(sentence: str, cited_chunks: list[Chunk]) -> float:
    if not cited_chunks:
        return 0.0
    sentence_grams = _char_bigrams(sentence)
    best = 0.0
    for chunk in cited_chunks:
        chunk_grams = _char_bigrams(chunk.content)
        overlap = len(sentence_grams & chunk_grams) / len(sentence_grams)
        best = max(best, overlap)
    return best


class NliChecker:
    """精篩階段介面。

    正式 NLI（entailment）模型尚未選型（見設計文件第 4-2 節與第 9 節），
    此處以字元 bigram 重疊率作為暫代分數，之後可直接替換 score() 內部
    實作為真正的 NLI 模型推論，不影響 check_faithfulness() 呼叫端。
    """

    def score(self, sentence: str, cited_chunks: list[Chunk]) -> float:
        return _citation_overlap_score(sentence, cited_chunks)


def check_faithfulness(
    sentence: str,
    cited_chunks: list[Chunk],
    nli_checker: NliChecker | None = None,
) -> FaithfulnessResult:
    rule_score = _citation_overlap_score(sentence, cited_chunks)
    if rule_score < settings.faithfulness_citation_overlap_threshold:
        return FaithfulnessResult(
            passed=False,
            rule_score=rule_score,
            nli_score=None,
            reason="規則式粗篩未通過：句子與引用來源重疊度過低，疑似無來源支撐",
        )

    checker = nli_checker or NliChecker()
    nli_score = checker.score(sentence, cited_chunks)
    passed = nli_score >= settings.faithfulness_citation_overlap_threshold
    reason = "通過忠實度檢查" if passed else "NLI 精篩未通過：句子與引用來源語意不一致"
    return FaithfulnessResult(passed=passed, rule_score=rule_score, nli_score=nli_score, reason=reason)
