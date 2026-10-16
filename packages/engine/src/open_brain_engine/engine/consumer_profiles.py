"""Engine-owned consumer profile admission and capture-only identity.

No official consumer export profile is active. Normalized synthetic fixtures
inject reviewed records only in tests; caller-supplied records never activate a
profile. A retained record describes evidence, not a runtime authorization.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from uuid import UUID

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .capture_lifecycle_contracts import decode_canonical, validate_digest
from .consumer_contracts import NORMALIZATION_VERSION, SEGMENTATION_VERSION
from .contracts import LocalEngineContext, PublicJobCaptureContext

_CODES = frozenset({
    "invalid_arguments", "unsupported_profile", "scope_conflict", "conversation_limit",
    "preview_not_found", "preview_expired", "preview_changed", "revision_changed",
    "retracted", "operation_conflict", "operation_not_found", "operation_pending",
    "local_operation_busy", "local_operation_failed", "invalid_evidence",
})


class ConsumerIntakeError(ValueError):
    """Closed diagnostics contain no source text, paths, or provider identity."""

    def __init__(self, code: str = "invalid_arguments") -> None:
        if type(code) is not str or code not in _CODES:
            raise ValueError("invalid consumer intake error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ConsumerProfileRecord:
    provider: str
    profile_id: str
    evidence_sha256: str
    normalization_version: str = NORMALIZATION_VERSION
    segmentation_version: str = SEGMENTATION_VERSION

    def __post_init__(self) -> None:
        if type(self.provider) is not str or self.provider not in {"chatgpt", "claude"}:
            raise ConsumerIntakeError()
        if type(self.profile_id) is not str or re.fullmatch(
            r"[a-z][a-z0-9-]{0,95}", self.profile_id
        ) is None:
            raise ConsumerIntakeError()
        validate_digest(self.evidence_sha256)
        if type(self.normalization_version) is not str or (
            self.normalization_version != NORMALIZATION_VERSION
            or type(self.segmentation_version) is not str
            or self.segmentation_version != SEGMENTATION_VERSION
        ):
            raise ConsumerIntakeError()

    def value(self) -> dict[str, object]:
        return {
            "contract": "ob.consumer-profile.v1", "provider": self.provider,
            "profile_id": self.profile_id, "evidence_sha256": self.evidence_sha256,
            "normalization_version": self.normalization_version,
            "segmentation_version": self.segmentation_version,
        }

    @property
    def record_sha256(self) -> str:
        return sha256(canonical(self.value())).hexdigest()

    def to_bytes(self) -> bytes:
        return canonical(self.value())

    @classmethod
    def from_bytes(cls, raw: bytes) -> ConsumerProfileRecord:
        value = decode_canonical(raw)
        expected = {"contract", "provider", "profile_id", "evidence_sha256",
                    "normalization_version", "segmentation_version"}
        if type(value) is not dict or set(value) != expected or (
            value["contract"] != "ob.consumer-profile.v1"
            or any(type(value[key]) is not str for key in expected)
        ):
            raise ConsumerIntakeError("invalid_evidence")
        return cls(value["provider"], value["profile_id"], value["evidence_sha256"],
                   value["normalization_version"], value["segmentation_version"])


# No configuration/CLI setter exists. Provider activation needs its separately
# reviewed mapping/evidence change; the goal's synthetic tests monkeypatch this
# private registry, never accept an export-supplied "active" flag.
_ACTIVE_PROFILES: Mapping[str, ConsumerProfileRecord] = MappingProxyType({})


def require_active_consumer_profile(provider: str, profile_id: str) -> ConsumerProfileRecord:
    record = _ACTIVE_PROFILES.get(profile_id) if type(profile_id) is str else None
    if type(provider) is not str or record is None or record.provider != provider:
        raise ConsumerIntakeError("unsupported_profile")
    return record


def consumer_capture_context(profile: LocalEngineContext) -> PublicJobCaptureContext:
    """Issue a stable per-Brain non-owner identity; export fields supply none of it."""
    def identity(prefix: str) -> str:
        raw = sha256(canonical(["ob.consumer-capture-identity.v1", profile.tenant_id, prefix]))
        return prefix + "_" + str(UUID(bytes=raw.digest()[:16], version=4))

    actor = identity("actor")
    return PublicJobCaptureContext.create(
        profile=profile, actor_id=actor,
        role_claim={"actor_id": actor, "tenant_id": profile.tenant_id,
                    "role_id": identity("role"), "role_claim_id": identity("role_claim"),
                    "capabilities": ["capture.accept"]},
    )
