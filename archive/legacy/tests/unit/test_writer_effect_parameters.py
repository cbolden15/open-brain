from __future__ import annotations

from hashlib import sha256

import pytest
from open_brain_engine.core.ids import canonical_json_bytes

from open_brain_legacy.operations.writer_jobs import (
    EffectCommand,
    EffectParameter,
    EffectReceipt,
    PreparedEffect,
    ScheduledEffect,
    WriterJobError,
)


def test_empty_effect_parameters_preserve_existing_digest_shape() -> None:
    prepared = PreparedEffect(ScheduledEffect.DIAGNOSTICS)
    expected = sha256(
        canonical_json_bytes(
            {
                "effect": "diagnostics",
                "records": [],
                "review_item_ids": [],
            }
        )
    ).hexdigest()

    assert prepared.digest_sha256() == expected
    assert "parameters" not in prepared.to_dict()


def test_effect_receipt_durably_carries_validated_parameters() -> None:
    prepared = PreparedEffect(
        ScheduledEffect.BACKUP_SNAPSHOT,
        parameters=(
            EffectParameter("created_at", "2026-08-16T12:00:00.000000Z"),
            EffectParameter("profile", "capture"),
        ),
    )
    receipt = EffectReceipt.from_command(
        EffectCommand("JOB-011", "backup-2026-08-16", "a" * 64, prepared)
    )

    assert receipt.parameters == prepared.parameters
    assert receipt.effect_digest_sha256 == prepared.digest_sha256()


def test_effect_parameters_require_sorted_unique_names() -> None:
    with pytest.raises(WriterJobError, match="prepared scheduled effect"):
        PreparedEffect(
            ScheduledEffect.BACKUP_SNAPSHOT,
            parameters=(
                EffectParameter("profile", "capture"),
                EffectParameter("created_at", "2026-08-16T12:00:00.000000Z"),
            ),
        )
