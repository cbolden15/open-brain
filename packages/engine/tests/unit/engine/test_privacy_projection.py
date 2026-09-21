from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from hashlib import sha256
from typing import cast

import pytest
from open_brain_engine.core.access_contracts import privacy_decision_sha256
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    ValidationError,
)
from open_brain_engine.engine.privacy_projection import (
    InvalidPrivacyEvidenceReason,
    RetainedPrivacyEvidence,
    project_retained_privacy_evidence,
)
from open_brain_engine.engine.search_projection import project_search_privacy

INVALID_EVIDENCE_DOMAIN = "open-brain.privacy.invalid-evidence.v1"


def _privacy(
    tier: PrivacyTier,
    *,
    cloud: bool = False,
    external_egress: bool = False,
    confirmation_ref: str | None = None,
) -> PrivacyDecision:
    reasons = {
        PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
        PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
        PrivacyTier.PERSONAL: (
            PrivacyReason.PERSONAL_CONFIRMED
            if confirmation_ref is not None
            else PrivacyReason.PERSONAL_LOCAL_ONLY
        ),
        PrivacyTier.SECRET: PrivacyReason.SECRET_DETECTED,
        PrivacyTier.UNKNOWN: PrivacyReason.CLASSIFICATION_MISSING,
    }
    return PrivacyDecision.create(
        tier=tier,
        reason=reasons[tier],
        policy_version="privacy-v1",
        authority=Authority(cloud=cloud, external_egress=external_egress),
        confirmation_ref=confirmation_ref,
    )


def _stored(decision: PrivacyDecision) -> str:
    return json.dumps(decision.to_dict())


def _preimage_value(value: object) -> object:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "real", "value": value.hex()}
    if isinstance(value, bytes):
        return {"type": "blob", "value": value.hex()}
    raise AssertionError(f"unsupported digest fixture value: {value!r}")


def _invalid_digest(
    reason: InvalidPrivacyEvidenceReason, values: Sequence[object]
) -> str:
    return sha256(
        portable_canonical_json_bytes(
            {
                "domain": INVALID_EVIDENCE_DOMAIN,
                "reason": reason.value,
                "retained": [_preimage_value(value) for value in values],
            }
        )
    ).hexdigest()


def test_single_valid_decision_projects_complete_evidence() -> None:
    decision = _privacy(PrivacyTier.PUBLIC, cloud=True, external_egress=True)

    projected = project_retained_privacy_evidence([_stored(decision)])

    assert projected.valid
    assert projected.tier is PrivacyTier.PUBLIC
    assert projected.authority == Authority(cloud=True, external_egress=True)
    assert projected.source_decision_sha256s == (privacy_decision_sha256(decision),)
    assert projected.confirmation_refs == ()
    assert projected.invalid_reason is None
    assert projected.invalid_evidence_sha256 is None


def test_single_valid_confirmed_decision_retains_confirmation_ref() -> None:
    decision = _privacy(
        PrivacyTier.PERSONAL, cloud=True, external_egress=True, confirmation_ref="confirm-1"
    )

    projected = project_retained_privacy_evidence([_stored(decision)])

    assert projected.valid
    assert projected.tier is PrivacyTier.PERSONAL
    assert projected.confirmation_refs == ("confirm-1",)


def test_derived_decision_intersects_authority_and_unions_lineage() -> None:
    public_egress = _privacy(PrivacyTier.PUBLIC, cloud=True, external_egress=True)
    personal = _privacy(
        PrivacyTier.PERSONAL, cloud=True, external_egress=False, confirmation_ref="confirm-1"
    )

    projected = project_retained_privacy_evidence(
        [_stored(public_egress), _stored(personal)]
    )

    assert projected.valid
    assert projected.tier is PrivacyTier.PERSONAL
    assert projected.authority == Authority(cloud=True, external_egress=False)
    assert projected.source_decision_sha256s == tuple(
        sorted(
            {
                privacy_decision_sha256(public_egress),
                privacy_decision_sha256(personal),
            }
        )
    )
    assert projected.confirmation_refs == ("confirm-1",)


@pytest.mark.parametrize(
    ("tiers", "expected"),
    [
        ((PrivacyTier.PUBLIC, PrivacyTier.WORK), PrivacyTier.WORK),
        ((PrivacyTier.WORK, PrivacyTier.PERSONAL), PrivacyTier.PERSONAL),
        ((PrivacyTier.PERSONAL, PrivacyTier.UNKNOWN), PrivacyTier.UNKNOWN),
        ((PrivacyTier.UNKNOWN, PrivacyTier.SECRET), PrivacyTier.SECRET),
        ((PrivacyTier.PUBLIC, PrivacyTier.SECRET, PrivacyTier.UNKNOWN), PrivacyTier.SECRET),
    ],
)
def test_derived_tier_follows_most_restrictive_precedence(
    tiers: tuple[PrivacyTier, ...], expected: PrivacyTier
) -> None:
    stored = [_stored(_privacy(tier)) for tier in tiers]

    projected = project_retained_privacy_evidence(stored)

    assert projected.valid
    assert projected.tier is expected


def test_projection_is_independent_of_source_order() -> None:
    first = _stored(_privacy(PrivacyTier.PUBLIC, cloud=True, external_egress=True))
    second = _stored(_privacy(PrivacyTier.WORK))
    third = _stored(_privacy(PrivacyTier.PERSONAL, confirmation_ref="confirm-2"))

    forward = project_retained_privacy_evidence([first, second, third])
    reverse = project_retained_privacy_evidence([third, second, first])

    assert forward == reverse


def test_missing_evidence_fails_closed_to_unknown_local_only() -> None:
    projected = project_retained_privacy_evidence([None])

    assert not projected.valid
    assert projected.tier is PrivacyTier.UNKNOWN
    assert projected.authority == Authority(cloud=False, external_egress=False)
    assert projected.source_decision_sha256s == ()
    assert projected.confirmation_refs == ()
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MISSING
    assert projected.invalid_evidence_sha256 == _invalid_digest(
        InvalidPrivacyEvidenceReason.MISSING, [None]
    )


def test_no_retained_values_is_missing() -> None:
    projected = project_retained_privacy_evidence([])

    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MISSING


def test_missing_beats_other_retained_values() -> None:
    projected = project_retained_privacy_evidence([_stored(_privacy(PrivacyTier.WORK)), None])

    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MISSING
    assert projected.tier is PrivacyTier.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    ["not json", "", "  ", '"public"', "42", "[]", "null", "{}", '{"tier": "public"}'],
)
def test_malformed_retained_json_fails_closed(payload: str) -> None:
    projected = project_retained_privacy_evidence([payload])

    assert not projected.valid
    assert projected.tier is PrivacyTier.UNKNOWN
    assert projected.authority == Authority(cloud=False, external_egress=False)
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED


def test_structurally_invalid_decision_fails_closed() -> None:
    valid = _privacy(PrivacyTier.PUBLIC).to_dict()
    missing_key = {key: value for key, value in valid.items() if key != "reason"}
    extra_key = {**valid, "extra": 1}
    non_bool_authority = {**valid, "authority": {"cloud": 1, "external_egress": False}}

    for payload in (missing_key, extra_key, non_bool_authority):
        projected = project_retained_privacy_evidence([json.dumps(payload)])

        assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED
        assert projected.tier is PrivacyTier.UNKNOWN


def test_internally_inconsistent_decision_fails_closed() -> None:
    public = _privacy(PrivacyTier.PUBLIC).to_dict()
    tier_reason_mismatch = {**public, "tier": "secret", "reason": "policy_public"}
    stray_confirmation_ref = {**public, "confirmation_ref": "confirm-3"}
    local_only_with_egress = {
        **_privacy(PrivacyTier.PERSONAL).to_dict(),
        "authority": {"cloud": True, "external_egress": False},
    }

    for payload in (tier_reason_mismatch, stray_confirmation_ref, local_only_with_egress):
        projected = project_retained_privacy_evidence([json.dumps(payload)])

        assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED
        assert projected.tier is PrivacyTier.UNKNOWN


def test_caller_declared_inconsistency_fails_closed_despite_valid_values() -> None:
    stored = [_stored(_privacy(PrivacyTier.WORK)), _stored(_privacy(PrivacyTier.PUBLIC))]

    projected = project_retained_privacy_evidence(
        stored, caller_declared_inconsistent=True
    )

    assert not projected.valid
    assert projected.tier is PrivacyTier.UNKNOWN
    assert projected.authority == Authority(cloud=False, external_egress=False)
    assert projected.source_decision_sha256s == ()
    assert projected.confirmation_refs == ()
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.INCONSISTENT
    assert projected.invalid_evidence_sha256 == _invalid_digest(
        InvalidPrivacyEvidenceReason.INCONSISTENT, stored
    )


def test_malformed_evidence_beats_declared_inconsistency() -> None:
    projected = project_retained_privacy_evidence(
        ["not json"], caller_declared_inconsistent=True
    )

    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED


@pytest.mark.parametrize(
    ("retained_values", "declared_inconsistent", "expected_value"),
    [
        ([], False, "missing"),
        ([None], False, "missing"),
        (["not json"], False, "malformed"),
        (["{}"], False, "malformed"),
        ([_stored(_privacy(PrivacyTier.WORK))], True, "inconsistent"),
    ],
)
def test_invalid_reason_values_are_exact(
    retained_values: list[str | None], declared_inconsistent: bool, expected_value: str
) -> None:
    projected = project_retained_privacy_evidence(
        retained_values, caller_declared_inconsistent=declared_inconsistent
    )

    assert projected.invalid_reason is not None
    assert projected.invalid_reason.value == expected_value


def test_invalid_evidence_digest_is_domain_separated_and_stable() -> None:
    stored = _stored(_privacy(PrivacyTier.WORK, cloud=True, external_egress=True))

    malformed = project_retained_privacy_evidence(["not json"])
    repeated = project_retained_privacy_evidence(["not json"])
    assert malformed.invalid_evidence_sha256 == repeated.invalid_evidence_sha256
    assert malformed.invalid_evidence_sha256 == _invalid_digest(
        InvalidPrivacyEvidenceReason.MALFORMED, ["not json"]
    )

    inconsistent = project_retained_privacy_evidence(
        [stored], caller_declared_inconsistent=True
    )
    assert inconsistent.invalid_evidence_sha256 is not None
    assert inconsistent.invalid_evidence_sha256 != malformed.invalid_evidence_sha256
    assert (
        inconsistent.invalid_evidence_sha256
        not in project_retained_privacy_evidence([stored]).source_decision_sha256s
    )


def test_one_invalid_record_does_not_block_unrelated_records() -> None:
    good = _stored(_privacy(PrivacyTier.WORK, cloud=True, external_egress=True))
    rows: list[list[str | None]] = [
        ["not json"],
        [None],
        [good, _stored(_privacy(PrivacyTier.SECRET))],
    ]

    projected = [project_retained_privacy_evidence(row) for row in rows]

    assert [evidence.invalid_reason for evidence in projected] == [
        InvalidPrivacyEvidenceReason.MALFORMED,
        InvalidPrivacyEvidenceReason.MISSING,
        None,
    ]
    assert projected[2].valid
    assert projected[2].tier is PrivacyTier.SECRET
    assert projected[2].authority == Authority(cloud=False, external_egress=False)


def test_bare_json_string_is_treated_as_one_retained_value() -> None:
    projected = project_retained_privacy_evidence(_stored(_privacy(PrivacyTier.PUBLIC)))

    assert projected.valid
    assert projected.tier is PrivacyTier.PUBLIC


def test_result_shape_rejects_contradictory_states() -> None:
    with pytest.raises(ValidationError):
        RetainedPrivacyEvidence(
            tier=PrivacyTier.WORK,
            authority=Authority(cloud=False, external_egress=False),
            source_decision_sha256s=(),
            confirmation_refs=(),
            invalid_reason=InvalidPrivacyEvidenceReason.MISSING,
            invalid_evidence_sha256="0" * 64,
        )
    with pytest.raises(ValidationError):
        RetainedPrivacyEvidence(
            tier=PrivacyTier.WORK,
            authority=Authority(cloud=True, external_egress=True),
            source_decision_sha256s=(),
            confirmation_refs=(),
            invalid_reason=None,
            invalid_evidence_sha256=None,
        )


def test_float_inside_retained_iterable_fails_closed_without_raising() -> None:
    row: list[str | float] = [_stored(_privacy(PrivacyTier.WORK)), 1.5]

    projected = project_retained_privacy_evidence(row)

    assert projected == project_retained_privacy_evidence(row)
    assert not projected.valid
    assert projected.tier is PrivacyTier.UNKNOWN
    assert projected.authority == Authority(cloud=False, external_egress=False)
    assert projected.source_decision_sha256s == ()
    assert projected.confirmation_refs == ()
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED
    assert projected.invalid_evidence_sha256 == _invalid_digest(
        InvalidPrivacyEvidenceReason.MALFORMED, row
    )


def test_bare_none_is_missing() -> None:
    projected = project_retained_privacy_evidence(None)

    assert not projected.valid
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MISSING
    assert projected.tier is PrivacyTier.UNKNOWN
    assert projected.invalid_evidence_sha256 == _invalid_digest(
        InvalidPrivacyEvidenceReason.MISSING, [None]
    )


def test_bare_integer_is_malformed() -> None:
    projected = project_retained_privacy_evidence(7)

    assert not projected.valid
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED
    assert projected.tier is PrivacyTier.UNKNOWN
    assert projected.invalid_evidence_sha256 == _invalid_digest(
        InvalidPrivacyEvidenceReason.MALFORMED, [7]
    )


def test_bare_float_is_malformed() -> None:
    projected = project_retained_privacy_evidence(1.5)

    assert not projected.valid
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED
    assert projected.tier is PrivacyTier.UNKNOWN
    assert projected.invalid_evidence_sha256 == _invalid_digest(
        InvalidPrivacyEvidenceReason.MALFORMED, [1.5]
    )


def test_bare_bytes_are_malformed_as_one_retained_value() -> None:
    projected = project_retained_privacy_evidence(b"\x00blob\xff")

    assert not projected.valid
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED
    assert projected.tier is PrivacyTier.UNKNOWN
    assert projected.invalid_evidence_sha256 == _invalid_digest(
        InvalidPrivacyEvidenceReason.MALFORMED, [b"\x00blob\xff"]
    )


def test_empty_bare_bytes_are_present_and_malformed() -> None:
    projected = project_retained_privacy_evidence(b"")

    assert not projected.valid
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED


def test_bytes_inside_retained_iterable_fail_closed() -> None:
    row: list[str | bytes] = [_stored(_privacy(PrivacyTier.WORK)), b"\x89PNG"]

    projected = project_retained_privacy_evidence(row)

    assert not projected.valid
    assert projected.invalid_reason is InvalidPrivacyEvidenceReason.MALFORMED
    assert projected.invalid_evidence_sha256 == _invalid_digest(
        InvalidPrivacyEvidenceReason.MALFORMED, row
    )


def test_invalid_digest_type_tags_disambiguate_storage_classes() -> None:
    integer = project_retained_privacy_evidence(7)
    real = project_retained_privacy_evidence(1.5)
    text = project_retained_privacy_evidence("7")

    digests = {
        integer.invalid_evidence_sha256,
        real.invalid_evidence_sha256,
        text.invalid_evidence_sha256,
    }

    assert len(digests) == 3


def test_missing_capture_row_fails_closed_as_missing_search_evidence() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE captures (capture_id TEXT PRIMARY KEY, privacy_json TEXT)")
    connection.execute(
        "INSERT INTO captures VALUES ('present-capture', ?)", (_stored(_privacy(PrivacyTier.WORK)),)
    )
    connection.commit()

    present = project_search_privacy(
        connection, result_id="present", capture_id="present-capture", record_type="source"
    )
    assert present.valid
    assert present.tier is PrivacyTier.WORK

    # A search row whose capture row is missing must fail closed to typed missing
    # evidence, never raise an untyped error from the missing row.
    missing = project_search_privacy(
        connection, result_id="missing", capture_id="absent-capture", record_type="source"
    )
    assert not missing.valid
    assert missing.tier is PrivacyTier.UNKNOWN
    assert missing.invalid_reason is InvalidPrivacyEvidenceReason.MISSING
    assert (
        missing.invalid_evidence_sha256
        == project_retained_privacy_evidence([None]).invalid_evidence_sha256
    )


@pytest.mark.parametrize(
    "declared_inconsistent",
    [1, 0, None, "yes", 1.5],
)
def test_caller_declared_inconsistent_must_be_strict_bool(
    declared_inconsistent: object,
) -> None:
    with pytest.raises(ValidationError):
        project_retained_privacy_evidence(
            [_stored(_privacy(PrivacyTier.WORK))],
            caller_declared_inconsistent=cast(bool, declared_inconsistent),
        )


def test_apply_privacy_repair_replaces_value_and_keeps_lineage() -> None:
    from open_brain_engine.engine.privacy_projection import apply_privacy_repair

    base = project_retained_privacy_evidence([None])
    replacement = _privacy(PrivacyTier.WORK, cloud=True, external_egress=True)
    confirmed = _privacy(PrivacyTier.PERSONAL, confirmation_ref="confirm-1")

    repaired = apply_privacy_repair(
        base,
        replacement,
        applied_repair_id="repair_1",
        applied_repair_sequence=1,
    )
    confirmed_repair = apply_privacy_repair(
        project_retained_privacy_evidence(["not json"]),
        confirmed,
        applied_repair_id="repair_2",
        applied_repair_sequence=2,
    )

    # The replacement decision supplies the effective tier, authority, decision
    # digest, and confirmation reference; the original invalid reason and digest
    # survive as retained lineage.
    assert repaired.tier is PrivacyTier.WORK
    assert repaired.authority == Authority(cloud=True, external_egress=True)
    assert repaired.source_decision_sha256s == (privacy_decision_sha256(replacement),)
    assert repaired.confirmation_refs == ()
    assert repaired.invalid_reason == base.invalid_reason
    assert repaired.invalid_evidence_sha256 == base.invalid_evidence_sha256
    assert repaired.applied_repair_id == "repair_1"
    assert repaired.applied_repair_sequence == 1
    assert confirmed_repair.confirmation_refs == ("confirm-1",)
    assert confirmed_repair.source_decision_sha256s == (privacy_decision_sha256(confirmed),)


def test_apply_privacy_repair_rejects_valid_base_and_secret_egress() -> None:
    from open_brain_engine.engine.privacy_projection import apply_privacy_repair

    with pytest.raises(ValidationError):
        apply_privacy_repair(
            project_retained_privacy_evidence([_stored(_privacy(PrivacyTier.WORK))]),
            _privacy(PrivacyTier.PUBLIC),
            applied_repair_id="repair_1",
            applied_repair_sequence=1,
        )
    with pytest.raises(ValidationError):
        apply_privacy_repair(
            project_retained_privacy_evidence([None]),
            _privacy(PrivacyTier.SECRET, cloud=True),
            applied_repair_id="repair_1",
            applied_repair_sequence=1,
        )
    with pytest.raises(ValidationError):
        apply_privacy_repair(
            project_retained_privacy_evidence([None]),
            _privacy(PrivacyTier.SECRET),
            applied_repair_id="repair_1",
            applied_repair_sequence=0,
        )
