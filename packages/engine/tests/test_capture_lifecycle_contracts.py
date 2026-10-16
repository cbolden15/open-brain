"""Synthetic-only contract evidence for the isolated lifecycle journal."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError
from hashlib import sha256
from typing import cast

import pytest
from open_brain_engine.engine.capture_lifecycle_contracts import (
    MAX_CANONICAL_BYTES,
    MAX_CANONICAL_DEPTH,
    GroupIntent,
    LifecycleError,
    LifecycleErrorCode,
    LifecycleRequest,
    SegmentBinding,
    canonical_bytes,
    decode_canonical,
    digest,
    validate_capture_id,
    validate_digest,
    validate_session_id,
)

OPERATION = "123e4567-e89b-42d3-a456-426614174000"
CAPTURE = "capture_123e4567-e89b-42d3-a456-426614174001"
BRAIN = "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa"
REQUEST_GOLDEN = (
    b'{"brain_id":"brn_aaaaaaaaaaaaaaaaaaaaaaaaaa","contract":"ob.capture-lifecycle.v1",'
    b'"issuer_epoch":1,"kind":"consumer_import",'
    b'"operation_id":"123e4567-e89b-42d3-a456-426614174000",'
    b'"payload":{"scope":"local","selection":["synthetic"]}}'
)
MANIFEST_GOLDEN = (
    b'{"expected_revision_sha256":null,'
    b'"group_key":"3333333333333333333333333333333333333333333333333333333333333333",'
    b'"revision_sha256":"4444444444444444444444444444444444444444444444444444444444444444",'
    b'"segments":[{'
    b'"body_sha256":"1111111111111111111111111111111111111111111111111111111111111111",'
    b'"capture_id":"capture_123e4567-e89b-42d3-a456-426614174001",'
    b'"request_sha256":"2222222222222222222222222222222222222222222222222222222222222222"}]}'
)


def _request(**changes: object) -> LifecycleRequest:
    fields: dict[str, object] = {
        "operation_id": OPERATION,
        "kind": "consumer_import",
        "brain_id": BRAIN,
        "issuer_epoch": 1,
        "payload": {"scope": "local", "selection": ["synthetic"]},
    }
    fields.update(changes)
    return LifecycleRequest(
        operation_id=cast(str, fields["operation_id"]),
        kind=cast(str, fields["kind"]),
        brain_id=cast(str, fields["brain_id"]),
        issuer_epoch=cast(int, fields["issuer_epoch"]),
        payload=cast(Mapping[str, object], fields["payload"]),
    )


def _segment(index: int = 1) -> SegmentBinding:
    return SegmentBinding(
        f"capture_123e4567-e89b-42d3-a456-426614174{index:03x}", "1" * 64, "2" * 64
    )


def test_golden_request_and_manifest() -> None:
    request = _request()
    assert request.canonical_bytes == REQUEST_GOLDEN
    assert (
        request.request_sha256 == "6fc5e709b2c583863628a3c637cbcaf773371d64dedc6574ae651037789b5dda"
    )
    assert LifecycleRequest.from_bytes(REQUEST_GOLDEN) == request
    group = GroupIntent("3" * 64, "4" * 64, None, (_segment(),))
    assert canonical_bytes(group.value()) == MANIFEST_GOLDEN
    assert (
        group.manifest_sha256 == "791644c97f72f93283c7218970a800882011d56b72792ee7b3623ad66e92bc32"
    )
    assert GroupIntent.from_value(decode_canonical(MANIFEST_GOLDEN)) == group


def test_nested_payload_is_owned_and_immutable() -> None:
    rows: list[object] = [{"text": "synthetic"}]
    payload: dict[str, object] = {"rows": rows}
    request = _request(payload=payload)
    original = request.canonical_bytes
    original_digest = request.request_sha256
    cast(dict[str, object], rows[0])["text"] = "changed"
    rows.append("extra")
    payload.clear()
    detached = request.value()
    detached_rows = cast(list[object], cast(dict[str, object], detached["payload"])["rows"])
    cast(dict[str, object], detached_rows[0])["text"] = "changed again"
    assert request.canonical_bytes == original
    assert request.request_sha256 == original_digest
    with pytest.raises(TypeError):
        cast(dict[str, object], request.payload)["extra"] = True
    frozen_rows = cast(tuple[Mapping[str, object], ...], request.payload["rows"])
    with pytest.raises(TypeError):
        cast(dict[str, object], frozen_rows[0])["text"] = "cannot change"
    with pytest.raises(FrozenInstanceError):
        request.issuer_epoch = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"null\n",
        b'{"b":1,"a":2}',
        b'{"a":1,"a":2}',
        b'{"a":1,"\\u0061":2}',
        b'{"x":{"a":1,"a":2}}',
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"1.0",
        b"1e0",
        b"-0",
        b'"\\u0000"',
        b'"\\ud800"',
        b'"e\\u0301"',
        b'"\\u00e9"',
        b'"\xff"',
        b"\xef\xbb\xbfnull",
        b"[] []",
    ],
)
def test_strict_decode_rejects_noncanonical_or_invalid_json(data: bytes) -> None:
    with pytest.raises(LifecycleError, match="^invalid_arguments$"):
        decode_canonical(data)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        1.0,
        b"bytes",
        {1: "key"},
        {"nul\x00": True},
        {"x": "\ud800"},
        {"x": "e\u0301"},
        {"e\u0301": 1, "é": 2},
        {"set"},
    ],
)
def test_canonical_encoder_rejects_invalid_domain(value: object) -> None:
    with pytest.raises(LifecycleError, match="^invalid_arguments$"):
        canonical_bytes(value)


def test_canonical_preserves_strings_and_exact_domain() -> None:
    value = {"z": [None, True, False, 123, -3], "a": "é / \\ synthetic\n"}
    data = canonical_bytes(value)
    assert decode_canonical(data) == value
    assert digest(value) == sha256(data).hexdigest()
    assert canonical_bytes({"path": "A/../B"}) == b'{"path":"A/../B"}'


def test_depth_size_and_cycle_bounds() -> None:
    value: object = None
    for _ in range(MAX_CANONICAL_DEPTH):
        value = [value]
    assert decode_canonical(canonical_bytes(value)) == value
    with pytest.raises(LifecycleError):
        canonical_bytes([value])
    with pytest.raises(LifecycleError):
        decode_canonical(b"[" * (MAX_CANONICAL_DEPTH + 1) + b"null" + b"]" * 65)
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(LifecycleError):
        canonical_bytes(cycle)
    boundary = "a" * (MAX_CANONICAL_BYTES - 2)
    assert len(canonical_bytes(boundary)) == MAX_CANONICAL_BYTES
    with pytest.raises(LifecycleError):
        canonical_bytes(boundary + "a")
    with pytest.raises(LifecycleError):
        decode_canonical(b" " * (MAX_CANONICAL_BYTES + 1))
    with pytest.raises(LifecycleError):
        decode_canonical(cast(bytes, bytearray(b"null")))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", OPERATION.upper()),
        ("operation_id", OPERATION.replace("42d3", "12d3")),
        ("operation_id", OPERATION.replace("a456", "7456")),
        ("operation_id", "{" + OPERATION + "}"),
        ("operation_id", OPERATION.replace("-", "")),
        ("kind", "capture"),
        ("kind", []),
        ("brain_id", "brn_synthetic"),
        ("brain_id", "brn_" + "a" * 25 + "b"),
        ("brain_id", BRAIN.upper()),
        ("issuer_epoch", True),
        ("issuer_epoch", 0),
        ("issuer_epoch", -1),
        ("issuer_epoch", 1.0),
        ("payload", []),
    ],
)
def test_request_identity_domain(field: str, value: object) -> None:
    with pytest.raises(LifecycleError):
        _request(**{field: value})


@pytest.mark.parametrize("kind", ["consumer_import", "capture_retract", "session_retract"])
def test_all_internal_kinds_roundtrip(kind: str) -> None:
    request = _request(kind=kind)
    assert LifecycleRequest.from_bytes(request.canonical_bytes) == request


@pytest.mark.parametrize("change", ["unknown", "missing", "contract", "payload"])
def test_request_closed_top_level_schema(change: str) -> None:
    value = _request().value()
    if change == "unknown":
        value["extra"] = 1
    elif change == "missing":
        del value["operation_id"]
    elif change == "contract":
        value["contract"] = "ob.capture-lifecycle.v2"
    else:
        value["payload"] = None
    with pytest.raises(LifecycleError):
        LifecycleRequest.from_bytes(canonical_bytes(value))


@pytest.mark.parametrize(
    ("validator", "valid", "invalid"),
    [
        (validate_capture_id, CAPTURE, CAPTURE.upper()),
        (validate_capture_id, CAPTURE, CAPTURE.replace("42d3", "12d3")),
        (validate_capture_id, CAPTURE, CAPTURE.replace("capture_", "cap_")),
        (validate_session_id, "capsess_" + OPERATION, "capsess_" + OPERATION.replace("-", "")),
        (validate_digest, "a" * 64, "A" * 64),
        (validate_digest, "0" * 64, "0" * 63),
        (validate_digest, "f" * 64, True),
    ],
)
def test_public_identity_helpers(
    validator: Callable[[object], str],
    valid: str,
    invalid: object,
) -> None:
    assert validator(valid) == valid
    with pytest.raises(LifecycleError):
        validator(invalid)


def test_group_manifest_order_and_detached_values() -> None:
    first, second = _segment(1), _segment(2)
    group = GroupIntent("3" * 64, "4" * 64, "5" * 64, (first, second))
    reversed_group = GroupIntent("3" * 64, "4" * 64, "5" * 64, (second, first))
    assert group.manifest_sha256 != reversed_group.manifest_sha256
    original = group.manifest_sha256
    cast(list[object], group.value()["segments"]).clear()
    assert group.manifest_sha256 == original
    with pytest.raises(FrozenInstanceError):
        first.body_sha256 = "9" * 64  # type: ignore[misc]


def test_group_segment_limits_and_duplicate_refusal() -> None:
    maximum = tuple(_segment(index) for index in range(128))
    assert len(GroupIntent("3" * 64, "4" * 64, None, maximum).segments) == 128
    for invalid in ((), maximum + (_segment(128),), (_segment(), _segment())):
        with pytest.raises(LifecycleError):
            GroupIntent("3" * 64, "4" * 64, None, invalid)
    with pytest.raises(LifecycleError):
        GroupIntent("3" * 64, "4" * 64, None, cast(tuple[SegmentBinding, ...], [_segment()]))
    with pytest.raises(LifecycleError):
        GroupIntent("3" * 64, "4" * 64, None, cast(tuple[SegmentBinding, ...], (None,)))
    with pytest.raises(LifecycleError):
        GroupIntent("3" * 64, "4" * 64, "bad", maximum)


@pytest.mark.parametrize("factory", [SegmentBinding.from_value, GroupIntent.from_value])
@pytest.mark.parametrize("value", [None, [], {}, {"unknown": True}])
def test_bindings_reject_unknown_and_missing_fields(
    factory: Callable[[object], object],
    value: object,
) -> None:
    with pytest.raises(LifecycleError):
        factory(value)


def test_group_decode_checks_segment_schema() -> None:
    value = GroupIntent("3" * 64, "4" * 64, None, (_segment(),)).value()
    cast(list[object], value["segments"]).append({"capture_id": CAPTURE})
    with pytest.raises(LifecycleError):
        GroupIntent.from_value(value)


def test_fixed_error_codes_do_not_echo_input() -> None:
    for code in (
        "invalid_arguments",
        "operation_conflict",
        "operation_not_found",
        "revision_changed",
        "operation_pending",
        "retracted",
        "invalid_evidence",
        "local_operation_failed",
    ):
        error = LifecycleError(code)
        assert error.code == code
        assert str(error) == code
    with pytest.raises(ValueError, match="^invalid lifecycle error code$"):
        LifecycleError(cast(LifecycleErrorCode, "synthetic sensitive text"))
    with pytest.raises(LifecycleError) as caught:
        _request(payload={"synthetic sensitive text": object()})
    assert str(caught.value) == "invalid_arguments"
