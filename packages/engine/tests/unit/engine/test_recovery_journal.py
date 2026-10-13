"""Recovery closure requires a separately supplied complete expected head."""

import json
from dataclasses import replace
from hashlib import sha256
from typing import Any, cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.recovery_journal import (
    MAX_RECOVERY_PAYLOAD_BYTES,
    MAX_RECOVERY_RECORD_BYTES,
    RecoveryBaseline,
    RecoveryHead,
    RecoveryRecord,
    verify_recovery_chain,
)

BRAIN = "brn_" + "a" * 26
BASELINE = RecoveryBaseline(BRAIN, 1, "0" * 64)


def _record(sequence: int = 1, previous: str = BASELINE.artifact_sha256) -> RecoveryRecord:
    return RecoveryRecord(
        baseline=BASELINE, sequence=sequence, previous_sha256=previous,
        kind="managed_revision", payload=b"synthetic immutable replay bytes",
    )


def _head(record: RecoveryRecord) -> RecoveryHead:
    return RecoveryHead(BASELINE, record.sequence, record.record_sha256)


def test_recovery_chain_roundtrip_and_expected_head() -> None:
    first = _record()
    second = _record(2, first.record_sha256)
    assert RecoveryRecord.from_bytes(first.to_bytes()) == first
    assert verify_recovery_chain(BASELINE, (first, second), expected_head=_head(second)) == second
    assert verify_recovery_chain(
        BASELINE, (), expected_head=RecoveryHead(BASELINE, 0, BASELINE.artifact_sha256),
    ) is None


@pytest.mark.parametrize("case", ("truncated", "reordered", "wrong_parent", "wrong_head"))
def test_incomplete_or_wrong_chain_refuses(case: str) -> None:
    first = _record()
    second = _record(2, first.record_sha256)
    records: tuple[RecoveryRecord, ...] = (first, second)
    head = _head(second)
    if case == "truncated":
        records = (first,)
    elif case == "reordered":
        records = (second, first)
    elif case == "wrong_parent":
        records = (first, replace(second, previous_sha256="1" * 64))
    else:
        head = replace(head, record_sha256="2" * 64)
    with pytest.raises(ValueError):
        verify_recovery_chain(BASELINE, records, expected_head=head)


def test_missing_dependency_and_altered_body_refuse() -> None:
    dependency = b"synthetic retained dependency"
    first = replace(_record(), dependencies=(sha256(dependency).hexdigest(),))
    with pytest.raises(ValueError, match="dependency"):
        verify_recovery_chain(BASELINE, (first,), expected_head=_head(first))
    assert verify_recovery_chain(
        BASELINE, (first,), expected_head=_head(first),
        dependency_payloads=(dependency,),
    ) == first
    altered = replace(first, payload=b"altered")
    with pytest.raises(ValueError):
        verify_recovery_chain(
            BASELINE, (altered,), expected_head=_head(first),
            dependency_payloads=(dependency,),
        )
    with pytest.raises(ValueError, match="dependency"):
        verify_recovery_chain(
            BASELINE, (first,), expected_head=_head(first),
            dependency_payloads=(b"altered dependency",),
        )


@pytest.mark.parametrize("payloads", (
    (b"",), ("unverified digest claim",), [b"mutable list"],
    (b"x",) * 65, (b"x" * MAX_RECOVERY_PAYLOAD_BYTES, b"x"),
))
def test_dependency_input_bounds_refuse(payloads: object) -> None:
    with pytest.raises(ValueError, match="dependency"):
        verify_recovery_chain(
            BASELINE, (), expected_head=RecoveryHead(BASELINE, 0, BASELINE.artifact_sha256),
            dependency_payloads=cast(Any, payloads),
        )


@pytest.mark.parametrize("field,value", (
    ("sequence", True), ("sequence", 1.0), ("sequence", 0),
    ("sequence", 9_007_199_254_740_992), ("kind", "arbitrary_sql"),
    ("previous_sha256", "A" * 64), ("dependencies", ("2" * 64, "1" * 64)),
    ("dependencies", ("1" * 64, "1" * 64)), ("payload", b""),
))
def test_invalid_fields_refuse(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(cast(Any, _record()), **{field: value})


@pytest.mark.parametrize("case", ("extra", "duplicate", "noncanonical", "digest", "float"))
def test_closed_canonical_decoder_refuses(case: str) -> None:
    raw = _record().to_bytes()
    value = json.loads(raw)
    if case == "extra":
        value["owner"] = True
    elif case == "digest":
        value["record_sha256"] = "f" * 64
    elif case == "float":
        value["sequence"] = 1.0
    if case == "duplicate":
        raw = raw.replace(b'"sequence":1', b'"sequence":1,"sequence":1')
    elif case == "float":
        raw = raw.replace(b'"sequence":1', b'"sequence":1.0')
    elif case == "noncanonical":
        raw = json.dumps(value, indent=2).encode()
    else:
        raw = portable_canonical_json_bytes(value)
    with pytest.raises(ValueError):
        RecoveryRecord.from_bytes(raw)


@pytest.mark.parametrize("change", ("brain", "epoch", "baseline"))
def test_destination_and_baseline_mismatch_refuse(change: str) -> None:
    if change == "brain":
        baseline = replace(BASELINE, brain_id="brn_" + "b" * 26)
    elif change == "epoch":
        baseline = replace(BASELINE, issuer_epoch=2)
    else:
        baseline = replace(BASELINE, artifact_sha256="3" * 64)
    record = replace(
        _record(), baseline=baseline, previous_sha256=baseline.artifact_sha256,
    )
    with pytest.raises(ValueError):
        verify_recovery_chain(BASELINE, (record,), expected_head=_head(record))


@pytest.mark.parametrize("field,value", (
    ("brain_id", "brn_invalid"), ("issuer_epoch", True), ("issuer_epoch", 0),
    ("artifact_sha256", "A" * 64),
))
def test_invalid_baseline_refuses(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(cast(Any, BASELINE), **{field: value})


@pytest.mark.parametrize("raw", (
    b"not json", b"\xff", b"null", b"[]", b"[" * 20_000 + b"]" * 20_000,
))
def test_malformed_decoder_input_refuses(raw: bytes) -> None:
    with pytest.raises(ValueError, match="invalid recovery record"):
        RecoveryRecord.from_bytes(raw)


def test_bounds_refuse_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        replace(_record(), payload=b"x" * (MAX_RECOVERY_PAYLOAD_BYTES + 1))
    with pytest.raises(ValueError):
        replace(_record(), dependencies=tuple(f"{n:064x}" for n in range(65)))
    monkeypatch.setattr(json, "loads", lambda *a, **k: pytest.fail("oversize reached decoder"))
    with pytest.raises(ValueError, match="size"):
        RecoveryRecord.from_bytes(b"x" * (MAX_RECOVERY_RECORD_BYTES + 1))


def test_initial_predecessor_and_empty_head_refuse() -> None:
    with pytest.raises(ValueError):
        replace(_record(), previous_sha256="1" * 64)
    with pytest.raises(ValueError):
        RecoveryHead(BASELINE, 0, "1" * 64)
