from app.config import settings
from app.core.models import Chunk, EvidenceVerdict

_AMBIGUOUS_MARGIN = 0.1


def judge_evidence(chunks: list[Chunk]) -> EvidenceVerdict:
    if not chunks:
        return EvidenceVerdict.INSUFFICIENT

    top_similarity = chunks[0].similarity
    threshold = settings.evidence_similarity_threshold

    if top_similarity >= threshold + _AMBIGUOUS_MARGIN:
        return EvidenceVerdict.SUFFICIENT
    if top_similarity < threshold - _AMBIGUOUS_MARGIN:
        return EvidenceVerdict.INSUFFICIENT
    return EvidenceVerdict.AMBIGUOUS
