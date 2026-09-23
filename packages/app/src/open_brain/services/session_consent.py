"""Durable owner-controlled provider consent for scoped foreground sessions."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from open_brain_engine.core.access_contracts import validate_issuer_epoch
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier, ValidationError
from open_brain_engine.engine.consent_contracts import (
    ConsentContractError,
    ConsentInspection,
    ConsentTransition,
    ProviderConsentState,
    provider_consent_state_from_bytes,
    provider_consent_state_to_bytes,
)
from open_brain_engine.storage.operational import (
    StorageError,
    atomic_replace,
    capture_root_identity,
    read_confined,
)

__all__ = ["DurableProviderConsentStore", "SessionConsentError"]

_FILE_VERSION = "provider-consent-file.v1"
_FILE_FIELDS = frozenset({"state_version", "brain_id", "issuer_epoch", "consent"})
_MAX_FILE_BYTES = 16 * 1024 * 1024 + 4096
_BRAIN_ID = re.compile(r"brn_[a-z2-7]{26}")


class SessionConsentError(ValueError):
    """A durable session-consent file failed its closed trust contract."""

    def __init__(self, code: str) -> None:
        if code not in {
            "consent_unavailable",
            "consent_state_conflict",
            "consent_state_mismatch",
            "invalid_consent_state",
            "unsafe_consent_state",
        }:
            raise ValueError("invalid session consent error code")
        self.code = code
        super().__init__(code)


class DurableProviderConsentStore:
    """One owner-only, Brain-bound, atomic provider-consent state file."""

    def __init__(self, path: Path, *, brain_id: str, issuer_epoch: int) -> None:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or path.name in {"", ".", ".."}
            or path.parent == path
            or not isinstance(brain_id, str)
            or _BRAIN_ID.fullmatch(brain_id) is None
        ):
            raise SessionConsentError("unsafe_consent_state")
        try:
            validate_issuer_epoch(issuer_epoch)
        except ValidationError:
            raise SessionConsentError("unsafe_consent_state") from None
        self.path = path
        self.brain_id = brain_id
        self.issuer_epoch = issuer_epoch

    def load(self) -> ProviderConsentState:
        state, _payload = self._load(required=True)
        return state

    def inspect(self) -> ConsentInspection:
        return self.load().inspect(owner=True)

    def grant(
        self,
        *,
        provider_id: str,
        allowed_tiers: frozenset[PrivacyTier],
        operation_id: str,
        decided_at: str,
        consent_id_factory: Callable[[], str] | None = None,
    ) -> ConsentTransition:
        state, previous = self._load(required=False)
        arguments: dict[str, object] = {
            "owner": True,
            "provider_id": provider_id,
            "allowed_tiers": allowed_tiers,
            "operation_id": operation_id,
            "decided_at": decided_at,
        }
        if consent_id_factory is not None:
            arguments["consent_id_factory"] = consent_id_factory
        transition = state.grant(**arguments)  # type: ignore[arg-type]
        self._persist_transition(transition, previous=previous)
        return transition

    def replace(
        self,
        *,
        consent_id: str,
        provider_id: str,
        allowed_tiers: frozenset[PrivacyTier],
        operation_id: str,
        decided_at: str,
        consent_id_factory: Callable[[], str] | None = None,
    ) -> ConsentTransition:
        state, previous = self._load(required=True)
        arguments: dict[str, object] = {
            "owner": True,
            "consent_id": consent_id,
            "provider_id": provider_id,
            "allowed_tiers": allowed_tiers,
            "operation_id": operation_id,
            "decided_at": decided_at,
        }
        if consent_id_factory is not None:
            arguments["consent_id_factory"] = consent_id_factory
        transition = state.replace(**arguments)  # type: ignore[arg-type]
        self._persist_transition(transition, previous=previous)
        return transition

    def revoke(
        self,
        *,
        consent_id: str,
        operation_id: str,
        decided_at: str,
    ) -> ConsentTransition:
        state, previous = self._load(required=True)
        transition = state.revoke(
            owner=True,
            consent_id=consent_id,
            operation_id=operation_id,
            decided_at=decided_at,
        )
        self._persist_transition(transition, previous=previous)
        return transition

    def _load(self, *, required: bool) -> tuple[ProviderConsentState, bytes | None]:
        root_identity = self._validated_root_identity()
        try:
            payload = read_confined(
                root=self.path.parent,
                relative=self.path.name,
                expected_root_identity=root_identity,
                maximum_bytes=_MAX_FILE_BYTES,
            )
        except StorageError:
            raise SessionConsentError("unsafe_consent_state") from None
        if payload is None:
            if required:
                raise SessionConsentError("consent_unavailable")
            return ProviderConsentState(), None
        self._validate_file_metadata()
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            if type(value) is not dict or frozenset(value) != _FILE_FIELDS:
                raise ValueError
            if value["state_version"] != _FILE_VERSION:
                raise ValueError
            if value["brain_id"] != self.brain_id or value["issuer_epoch"] != self.issuer_epoch:
                raise SessionConsentError("consent_state_mismatch")
            consent = value["consent"]
            if type(consent) is not dict or portable_canonical_json_bytes(value) != payload:
                raise ValueError
            state = provider_consent_state_from_bytes(portable_canonical_json_bytes(consent))
            return state, payload
        except SessionConsentError:
            raise
        except ConsentContractError, KeyError, TypeError, ValueError, UnicodeError:
            raise SessionConsentError("invalid_consent_state") from None

    def _persist_transition(
        self, transition: ConsentTransition, *, previous: bytes | None
    ) -> None:
        payload = self._encode(transition.state)
        if previous == payload:
            return
        root_identity = self._validated_root_identity()
        try:
            atomic_replace(
                root=self.path.parent,
                relative=self.path.name,
                data=payload,
                require_existing=previous is not None,
                expected_existing_sha256=(
                    None if previous is None else sha256(previous).hexdigest()
                ),
                expected_root_identity=root_identity,
            )
            self._validate_file_metadata()
        except StorageError:
            raise SessionConsentError("consent_state_conflict") from None

    def _encode(self, state: ProviderConsentState) -> bytes:
        consent = cast(
            dict[str, object],
            json.loads(provider_consent_state_to_bytes(state).decode("utf-8")),
        )
        return portable_canonical_json_bytes(
            {
                "state_version": _FILE_VERSION,
                "brain_id": self.brain_id,
                "issuer_epoch": self.issuer_epoch,
                "consent": consent,
            }
        )

    def _validated_root_identity(self) -> tuple[int, int]:
        try:
            metadata = self.path.parent.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()
            ):
                raise SessionConsentError("unsafe_consent_state")
            return capture_root_identity(self.path.parent)
        except SessionConsentError:
            raise
        except (OSError, StorageError):
            raise SessionConsentError("unsafe_consent_state") from None

    def _validate_file_metadata(self) -> None:
        try:
            metadata = self.path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
                raise SessionConsentError("unsafe_consent_state")
        except SessionConsentError:
            raise
        except OSError:
            raise SessionConsentError("unsafe_consent_state") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError
