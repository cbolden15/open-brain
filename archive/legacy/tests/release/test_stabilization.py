from __future__ import annotations

from datetime import date, timedelta

import pytest

from open_brain_legacy.release.evidence import (
    REQUIRED_DAY_CHECKS,
    DailyEvidence,
    DayCheck,
    EvidenceValidationError,
    ProductionState,
    StabilizationEvidence,
    StabilizationReset,
    StabilizationResetReason,
)
from open_brain_legacy.release.stabilization import validate_stabilization_evidence


def test_stabilization_reset_restarts_the_seven_day_clock() -> None:
    first_day = date(2026, 8, 19)
    days = tuple(
        DailyEvidence(
            observed_on=first_day + timedelta(days=offset),
            checks=tuple(
                DayCheck(check=check, state=ProductionState.PASSED_DIRECT)
                for check in REQUIRED_DAY_CHECKS
            ),
            evidence_digest_sha256="a" * 64,
        )
        for offset in range(7)
    )
    evidence = StabilizationEvidence(
        days=days,
        resets=(
            StabilizationReset(
                occurred_on=first_day + timedelta(days=2),
                reason=StabilizationResetReason.ROLLBACK,
                evidence_digest_sha256="b" * 64,
            ),
        ),
    )

    with pytest.raises(EvidenceValidationError, match="stabilization-clock-not-reset"):
        validate_stabilization_evidence(evidence)
