"""Synthetic audit policy for refusing unauthorized cutover claims."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from open_brain_legacy.operations.doctor import DoctorOutcome


class RowDisposition(StrEnum):
    """Only the Goal #24 terminal capability disposition is accepted."""

    OPEN_BRAIN_LIVE = "open-brain-live"


@dataclass(frozen=True, slots=True)
class RowClassification:
    row_id: str
    disposition: RowDisposition


_AUTHORITATIVE_ROW_COUNTS = (
    ("CLI", 15),
    ("LED", 9),
    ("INT", 14),
    ("CAP", 11),
    ("JOB", 30),
    ("HOOK", 2),
    ("EXT", 2),
)
OWNER_GATED_DEFER_ROW_IDS: frozenset[str] = frozenset()
AUTHORITATIVE_ROW_CLASSIFICATIONS = tuple(
    RowClassification(
        row_id=f"{prefix}-{index:03d}",
        disposition=RowDisposition.OPEN_BRAIN_LIVE,
    )
    for prefix, count in _AUTHORITATIVE_ROW_COUNTS
    for index in range(1, count + 1)
)
_AUTHORITATIVE_ROW_CLASSIFICATION_SET = frozenset(AUTHORITATIVE_ROW_CLASSIFICATIONS)


@dataclass(frozen=True, slots=True)
class PreflightEvidence:
    row_classifications: tuple[RowClassification, ...]
    p0_p2_findings: int
    doctor_outcome: DoctorOutcome
    evidence_scope: Literal["synthetic", "live"]


@dataclass(frozen=True, slots=True)
class PreflightResult:
    ready: bool
    reasons: tuple[str, ...]


def evaluate_cutover_preflight(evidence: PreflightEvidence) -> PreflightResult:
    """Require complete live evidence with no findings or unresolved owner gates."""
    reasons: list[str] = []
    supplied_classifications = frozenset(evidence.row_classifications)
    if (
        len(evidence.row_classifications) != len(AUTHORITATIVE_ROW_CLASSIFICATIONS)
        or supplied_classifications != _AUTHORITATIVE_ROW_CLASSIFICATION_SET
    ):
        reasons.append("row-classifications-not-authoritative")
    if evidence.p0_p2_findings:
        reasons.append("p0-p2-findings-remain")
    if evidence.doctor_outcome is not DoctorOutcome.HEALTHY:
        reasons.append("doctor-not-healthy")
    if evidence.evidence_scope != "live":
        reasons.append("synthetic-evidence-only")
    return PreflightResult(ready=not reasons, reasons=tuple(reasons))
