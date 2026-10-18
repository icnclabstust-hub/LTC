from dataclasses import dataclass
from datetime import date
from enum import Enum


class Intent(str, Enum):
    LAW_SOP = "law_sop"
    LTC_POINTS = "ltc_points"
    IDENTITY = "identity"
    UNKNOWN = "unknown"


class EvidenceVerdict(str, Enum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    AMBIGUOUS = "ambiguous"


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    content: str
    law_name: str
    article: str | None
    source_url: str
    updated_at: date
    similarity: float = 0.0


@dataclass
class FaithfulnessResult:
    passed: bool
    rule_score: float
    nli_score: float | None
    reason: str
