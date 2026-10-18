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


class RefusalReason(str, Enum):
    """結構化拒答原因（2026-10-19 fe-be-integration 設計文件第 5.1 節），FE 依此分流，不比對回答字串。"""

    OUT_OF_SCOPE = "out_of_scope"
    KB_MISSING = "kb_missing"
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS = "ambiguous"
    SENSITIVE = "sensitive"


# 需要督導定案的拒答；out_of_scope 只記錄，ambiguous 先回追問
ESCALATE_REASONS = frozenset({RefusalReason.KB_MISSING, RefusalReason.LOW_CONFIDENCE, RefusalReason.SENSITIVE})
