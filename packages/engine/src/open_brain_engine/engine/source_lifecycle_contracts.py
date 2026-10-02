"""Closed owner-local contracts for managed source lifecycle decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .t03_contracts import T03Error


def _text(value: object, *, prefix: str | None = None, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise T03Error("invalid_arguments")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise T03Error("invalid_arguments") from None
    if prefix is not None and not value.startswith(prefix):
        raise T03Error("invalid_arguments")
    return value


def _digest(value: object) -> str:
    return sha256(portable_canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceInspectRequest:
    source_id: str
    dto_version: int = 1

    def __post_init__(self) -> None:
        if self.dto_version != 1:
            raise T03Error("invalid_arguments")
        object.__setattr__(self, "source_id", _text(self.source_id, prefix="source_"))


@dataclass(frozen=True, slots=True)
class SourceInspection:
    source_id: str
    head_capture_id: str
    head_version: int
    route_version: int
    lifecycle_version: int
    lifecycle: str
    availability: str
    destination_brain_id: str
    issuer_epoch: int
    withdrawal_receipt: Mapping[str, object] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceWithdrawRequest:
    operation_id: str
    source_id: str
    expected_head: str
    expected_lifecycle_version: int
    brain_id: str
    issuer_epoch: int
    reason_code: str
    absence_evidence_digest: str | None = None
    dto_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.dto_version != 1
            or type(self.expected_lifecycle_version) is not int
            or self.expected_lifecycle_version < 0
            or type(self.issuer_epoch) is not int
            or self.issuer_epoch < 1
        ):
            raise T03Error("invalid_arguments")
        for name, prefix in (("operation_id", "withdraw."), ("source_id", "source_"),
                             ("expected_head", "capture_"), ("brain_id", "brn_")):
            object.__setattr__(self, name, _text(getattr(self, name), prefix=prefix))
        object.__setattr__(self, "reason_code", _text(self.reason_code, maximum=96))
        if self.absence_evidence_digest is not None:
            digest = _text(self.absence_evidence_digest, maximum=64)
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise T03Error("invalid_arguments")
            object.__setattr__(self, "absence_evidence_digest", digest)

    def value(self) -> dict[str, object]:
        return {
            "dto_version": self.dto_version,
            "operation_id": self.operation_id,
            "source_id": self.source_id,
            "expected_head": self.expected_head,
            "expected_lifecycle_version": self.expected_lifecycle_version,
            "brain_id": self.brain_id,
            "issuer_epoch": self.issuer_epoch,
            "reason_code": self.reason_code,
            "absence_evidence_digest": self.absence_evidence_digest,
        }

    @property
    def request_sha256(self) -> str:
        return _digest(self.value())


@dataclass(frozen=True, slots=True)
class SourceWithdrawReceipt:
    operation_id: str
    request_sha256: str
    source_id: str
    head_capture_id: str
    lifecycle_version: int
    lifecycle: str
    receipt_sha256: str

