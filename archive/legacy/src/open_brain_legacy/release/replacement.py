"""Public replacement-evidence validation boundary."""

from .evidence import (
    Predecessor,
    PredecessorEvidence,
    ReplacementEvidence,
    RuntimeReferenceEvidence,
    validate_replacement_evidence,
)

__all__ = [
    "Predecessor",
    "PredecessorEvidence",
    "ReplacementEvidence",
    "RuntimeReferenceEvidence",
    "validate_replacement_evidence",
]
