"""Immutable internal lifecycle evidence; these bindings grant no admission authority.

Kind-specific importer intent, source/privacy checks and owner approval remain the
responsibility of engine admission. This module does not activate public DTOs.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Literal, cast
from uuid import UUID

from open_brain_engine.core.ids import portable_canonical_json_bytes

# Internal journal evidence bounds, not the consumer archive acceptance limit.
MAX_CANONICAL_BYTES = 16 * 1024 * 1024
MAX_CANONICAL_DEPTH = 64
MAX_GROUP_SEGMENTS = 128
_CONTRACT = "ob.capture-lifecycle.v1"
_KINDS = frozenset({"consumer_import", "capture_retract", "session_retract"})
type LifecycleErrorCode = Literal[
    "invalid_arguments",
    "operation_conflict",
    "operation_not_found",
    "revision_changed",
    "operation_pending",
    "retracted",
    "invalid_evidence",
    "local_operation_failed",
]
_ERROR_CODES = frozenset(
    {
        "invalid_arguments",
        "operation_conflict",
        "operation_not_found",
        "revision_changed",
        "operation_pending",
        "retracted",
        "invalid_evidence",
        "local_operation_failed",
    }
)


class LifecycleError(ValueError):
    """Closed, body-free diagnostics at the internal evidence boundary."""

    def __init__(self, code: LifecycleErrorCode = "invalid_arguments") -> None:
        if type(code) is not str or code not in _ERROR_CODES:
            raise ValueError("invalid lifecycle error code")
        self.code: LifecycleErrorCode = code
        super().__init__(code)


def _text(value: object) -> str:
    if type(value) is not str or "\x00" in value:
        raise LifecycleError()
    try:
        if len(value.encode("utf-8")) > MAX_CANONICAL_BYTES:
            raise LifecycleError()
    except UnicodeError:
        raise LifecycleError() from None
    # Portable canonical JSON normalizes NFC. Reject changes instead of silently
    # changing caller-supplied identifiers or source strings.
    if unicodedata.normalize("NFC", value) != value:
        raise LifecycleError()
    return value


def _plain(value: object, depth: int = 0) -> object:
    if depth > MAX_CANONICAL_DEPTH:
        raise LifecycleError()
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        return _text(value)
    if isinstance(value, Mapping):
        return {_text(key): _plain(item, depth + 1) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [_plain(item, depth + 1) for item in cast(list[object] | tuple[object, ...], value)]
    raise LifecycleError()


def canonical_bytes(value: object) -> bytes:
    """Encode bounded exact canonical JSON without normalizing input strings."""
    try:
        data = portable_canonical_json_bytes(_plain(value))
    except ValueError, TypeError, RecursionError:
        raise LifecycleError() from None
    if len(data) > MAX_CANONICAL_BYTES:
        raise LifecycleError()
    return data


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LifecycleError()
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise LifecycleError()


def decode_canonical(data: bytes) -> object:
    """Decode only exact canonical UTF-8 bytes, including duplicate-key refusal."""
    if type(data) is not bytes or not 0 < len(data) <= MAX_CANONICAL_BYTES:
        raise LifecycleError()
    try:
        value: object = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_constant
        )
        if canonical_bytes(value) != data:
            raise LifecycleError()
        return value
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise LifecycleError() from None


def digest(value: object) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def _uuid4(value: object) -> str:
    if type(value) is not str:
        raise LifecycleError()
    try:
        parsed = UUID(value)
    except ValueError:
        raise LifecycleError() from None
    if parsed.version != 4 or str(parsed) != value:
        raise LifecycleError()
    return value


def _prefixed_uuid4(value: object, prefix: str) -> str:
    if type(value) is not str or not value.startswith(prefix):
        raise LifecycleError()
    _uuid4(value[len(prefix) :])
    return value


def validate_capture_id(value: object) -> str:
    return _prefixed_uuid4(value, "capture_")


def validate_session_id(value: object) -> str:
    return _prefixed_uuid4(value, "capsess_")


def validate_digest(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise LifecycleError()
    return value


def _brain_id(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"brn_[a-z2-7]{26}", value) is None:
        raise LifecycleError()
    decoded = base64.b32decode(value[4:].upper() + "======")
    if base64.b32encode(decoded).decode("ascii").rstrip("=").lower() != value[4:]:
        raise LifecycleError()
    return value


def _freeze(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def _fields(value: object, expected: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise LifecycleError()
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class LifecycleRequest:
    operation_id: str
    kind: str
    brain_id: str
    issuer_epoch: int
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        _uuid4(self.operation_id)
        if type(self.kind) is not str or self.kind not in _KINDS:
            raise LifecycleError()
        _brain_id(self.brain_id)
        if type(self.issuer_epoch) is not int or self.issuer_epoch <= 0:
            raise LifecycleError()
        if not isinstance(self.payload, Mapping):
            raise LifecycleError()
        # Round-trip to an owned tree before recursively freezing it. Caller
        # aliases and values returned by value() cannot mutate retained intent.
        owned = decode_canonical(canonical_bytes(self.payload))
        object.__setattr__(self, "payload", _freeze(owned))
        canonical_bytes(self.value())

    def value(self) -> dict[str, object]:
        return {
            "contract": _CONTRACT,
            "operation_id": self.operation_id,
            "kind": self.kind,
            "brain_id": self.brain_id,
            "issuer_epoch": self.issuer_epoch,
            "payload": _plain(self.payload),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.value())

    @property
    def request_sha256(self) -> str:
        return sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def from_bytes(cls, data: bytes) -> LifecycleRequest:
        value = _fields(
            decode_canonical(data),
            {
                "contract",
                "operation_id",
                "kind",
                "brain_id",
                "issuer_epoch",
                "payload",
            },
        )
        if value["contract"] != _CONTRACT:
            raise LifecycleError()
        return cls(
            operation_id=cast(str, value["operation_id"]),
            kind=cast(str, value["kind"]),
            brain_id=cast(str, value["brain_id"]),
            issuer_epoch=cast(int, value["issuer_epoch"]),
            payload=cast(Mapping[str, object], value["payload"]),
        )


@dataclass(frozen=True, slots=True)
class SegmentBinding:
    capture_id: str
    body_sha256: str
    request_sha256: str

    def __post_init__(self) -> None:
        validate_capture_id(self.capture_id)
        validate_digest(self.body_sha256)
        validate_digest(self.request_sha256)

    def value(self) -> dict[str, object]:
        return {
            "capture_id": self.capture_id,
            "body_sha256": self.body_sha256,
            "request_sha256": self.request_sha256,
        }

    @classmethod
    def from_value(cls, value: object) -> SegmentBinding:
        fields = _fields(value, {"capture_id", "body_sha256", "request_sha256"})
        return cls(
            capture_id=cast(str, fields["capture_id"]),
            body_sha256=cast(str, fields["body_sha256"]),
            request_sha256=cast(str, fields["request_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class GroupIntent:
    group_key: str
    revision_sha256: str
    expected_revision_sha256: str | None
    segments: tuple[SegmentBinding, ...]

    def __post_init__(self) -> None:
        validate_digest(self.group_key)
        validate_digest(self.revision_sha256)
        if self.expected_revision_sha256 is not None:
            validate_digest(self.expected_revision_sha256)
        if type(self.segments) is not tuple or not 1 <= len(self.segments) <= MAX_GROUP_SEGMENTS:
            raise LifecycleError()
        if any(type(segment) is not SegmentBinding for segment in self.segments):
            raise LifecycleError()
        if len({segment.capture_id for segment in self.segments}) != len(self.segments):
            raise LifecycleError()

    def value(self) -> dict[str, object]:
        return {
            "group_key": self.group_key,
            "revision_sha256": self.revision_sha256,
            "expected_revision_sha256": self.expected_revision_sha256,
            "segments": [segment.value() for segment in self.segments],
        }

    @classmethod
    def from_value(cls, value: object) -> GroupIntent:
        fields = _fields(
            value,
            {
                "group_key",
                "revision_sha256",
                "expected_revision_sha256",
                "segments",
            },
        )
        segments = fields["segments"]
        if type(segments) is not list or not 1 <= len(segments) <= MAX_GROUP_SEGMENTS:
            raise LifecycleError()
        return cls(
            group_key=cast(str, fields["group_key"]),
            revision_sha256=cast(str, fields["revision_sha256"]),
            expected_revision_sha256=cast(str | None, fields["expected_revision_sha256"]),
            segments=tuple(SegmentBinding.from_value(segment) for segment in segments),
        )

    @property
    def manifest_sha256(self) -> str:
        return digest(self.value())
