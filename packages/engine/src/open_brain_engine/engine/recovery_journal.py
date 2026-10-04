"""Bounded recovery commitments. Hash closure is not semantic admission or durability.

An independently supplied expected head is mandatory: a locally valid prefix does
not prove freshness. Typed operation validation, exact materialization, protection
ports and mandatory write coverage remain separate obligations. This module does
not grant owner/model authority or change any frozen capture-protection format.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

MAX_RECOVERY_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_RECOVERY_RECORD_BYTES = ((MAX_RECOVERY_PAYLOAD_BYTES + 2) // 3) * 4 + 16_384
MAX_RECOVERY_DEPENDENCIES = 64
_MAX_SEQUENCE = 9_007_199_254_740_991
_KINDS = frozenset({
    "capture_custody", "capture", "capture_journal", "managed_revision", "withdrawal", "sharing",
    "historical", "control",
})
_DOMAIN = b"open-brain-recovery-record.v1\0"


def _digest(value: object) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid recovery digest")


def _sequence(value: object, *, minimum: int) -> None:
    if type(value) is not int or not minimum <= value <= _MAX_SEQUENCE:
        raise ValueError("invalid recovery sequence")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate recovery field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class RecoveryBaseline:
    brain_id: str
    issuer_epoch: int
    artifact_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.brain_id) is not str
            or re.fullmatch(r"brn_[a-z2-7]{26}", self.brain_id) is None
        ):
            raise ValueError("invalid recovery destination")
        _sequence(self.issuer_epoch, minimum=1)
        _digest(self.artifact_sha256)

    def value(self) -> dict[str, object]:
        return {
            "brain_id": self.brain_id,
            "issuer_epoch": self.issuer_epoch,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass(frozen=True, slots=True)
class RecoveryHead:
    baseline: RecoveryBaseline
    sequence: int
    record_sha256: str

    def __post_init__(self) -> None:
        if type(self.baseline) is not RecoveryBaseline:
            raise ValueError("invalid recovery baseline")
        _sequence(self.sequence, minimum=0)
        _digest(self.record_sha256)
        if self.sequence == 0 and self.record_sha256 != self.baseline.artifact_sha256:
            raise ValueError("invalid empty recovery head")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryRecord:
    baseline: RecoveryBaseline
    sequence: int
    previous_sha256: str
    kind: str
    payload: bytes
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.baseline) is not RecoveryBaseline:
            raise ValueError("invalid recovery baseline")
        _sequence(self.sequence, minimum=1)
        _digest(self.previous_sha256)
        if self.sequence == 1 and self.previous_sha256 != self.baseline.artifact_sha256:
            raise ValueError("invalid initial recovery predecessor")
        if type(self.kind) is not str or self.kind not in _KINDS:
            raise ValueError("invalid recovery kind")
        if (
            type(self.payload) is not bytes
            or not 0 < len(self.payload) <= MAX_RECOVERY_PAYLOAD_BYTES
        ):
            raise ValueError("invalid recovery payload")
        if (
            type(self.dependencies) is not tuple
            or len(self.dependencies) > MAX_RECOVERY_DEPENDENCIES
        ):
            raise ValueError("invalid recovery dependencies")
        for dependency in self.dependencies:
            _digest(dependency)
        if self.dependencies != tuple(sorted(set(self.dependencies))):
            raise ValueError("invalid recovery dependencies")

    def _body(self) -> dict[str, object]:
        return {
            "contract_version": "recovery-record.v1",
            "baseline": self.baseline.value(),
            "sequence": self.sequence,
            "previous_sha256": self.previous_sha256,
            "kind": self.kind,
            "payload_base64": base64.b64encode(self.payload).decode("ascii"),
            "dependencies": list(self.dependencies),
        }

    @property
    def record_sha256(self) -> str:
        return sha256(_DOMAIN + canonical(self._body())).hexdigest()

    def to_bytes(self) -> bytes:
        return canonical(dict(self._body(), record_sha256=self.record_sha256))

    @classmethod
    def from_bytes(cls, raw: bytes) -> RecoveryRecord:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_RECOVERY_RECORD_BYTES:
            raise ValueError("invalid recovery record size")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
            if type(value) is not dict or set(value) != {
                "contract_version", "baseline", "sequence", "previous_sha256", "kind",
                "payload_base64", "dependencies", "record_sha256",
            } or value["contract_version"] != "recovery-record.v1":
                raise ValueError("invalid recovery record fields")
            baseline = value["baseline"]
            if type(baseline) is not dict or set(baseline) != {
                "brain_id", "issuer_epoch", "artifact_sha256",
            }:
                raise ValueError("invalid recovery baseline fields")
            if type(value["dependencies"]) is not list or type(value["payload_base64"]) is not str:
                raise ValueError("invalid recovery record fields")
            result = cls(
                baseline=RecoveryBaseline(**baseline),
                sequence=value["sequence"], previous_sha256=value["previous_sha256"],
                kind=value["kind"],
                payload=base64.b64decode(value["payload_base64"], validate=True),
                dependencies=tuple(value["dependencies"]),
            )
            if result.to_bytes() != raw:
                raise ValueError("invalid recovery canonical bytes or commitment")
            return result
        except UnicodeError, json.JSONDecodeError, TypeError, ValueError, binascii.Error:
            raise ValueError("invalid recovery record") from None


def verify_recovery_chain(
    baseline: RecoveryBaseline,
    records: tuple[RecoveryRecord, ...],
    *,
    expected_head: RecoveryHead,
    dependency_payloads: tuple[bytes, ...] = (),
) -> RecoveryRecord | None:
    """Verify exact ordered hash/dependency closure, never infer semantic permission.

    External dependencies are checked against actual bounded bytes, not inventory
    digest claims. The caller must independently authenticate/retrieve the baseline,
    dependencies and latest head; local bytes alone do not prove independent custody.
    """
    if (
        type(baseline) is not RecoveryBaseline or type(expected_head) is not RecoveryHead
        or expected_head.baseline != baseline or type(records) is not tuple
    ):
        raise ValueError("invalid recovery closure")
    if (
        type(dependency_payloads) is not tuple
        or len(dependency_payloads) > MAX_RECOVERY_DEPENDENCIES
    ):
        raise ValueError("invalid recovery dependency payloads")
    total = 0
    for payload in dependency_payloads:
        if type(payload) is not bytes or not 0 < len(payload) <= MAX_RECOVERY_PAYLOAD_BYTES:
            raise ValueError("invalid recovery dependency payloads")
        total += len(payload)
        if total > MAX_RECOVERY_PAYLOAD_BYTES:
            raise ValueError("invalid recovery dependency payloads")
    known = {baseline.artifact_sha256}
    known.update(sha256(payload).hexdigest() for payload in dependency_payloads)
    previous = baseline.artifact_sha256
    for sequence, record in enumerate(records, start=1):
        if type(record) is not RecoveryRecord or record.baseline != baseline or (
            record.sequence != sequence or record.previous_sha256 != previous
        ):
            raise ValueError("invalid recovery chain")
        if not set(record.dependencies) <= known:
            raise ValueError("missing recovery dependency")
        previous = record.record_sha256
        known.add(previous)
    if expected_head.sequence != len(records) or expected_head.record_sha256 != previous:
        raise ValueError("incomplete recovery head")
    return records[-1] if records else None
