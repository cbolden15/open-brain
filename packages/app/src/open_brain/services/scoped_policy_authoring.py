"""Narrow create-only owner helper for one guide-scoped launcher-policy.v1 file.

RECOVERY.md "Scoped policy authoring gap": the helper builds the UNCHANGED
launcher-policy.v1 document for one dedicated Work space, one provider and the
current provider consent, with generated principal/session IDs. Preview
returns the complete canonical bytes and their SHA-256 ephemerally; apply
regenerates them from the confirmed non-content inputs, compares the SHA,
revalidates consent and identity, then creates the file atomically (0600 under
a 0700 parent). It never overwrites, repairs, widens or reconstructs a policy;
an existing file is only reported when a fresh parse proves the exact scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.consent_contracts import EgressMode, ProviderConsentState

from open_brain.services.launcher_policy import (
    LauncherPolicyError,
    parse_launcher_policy,
    validate_startup_policy,
)
from open_brain.services.session_authority import _read_policy
from open_brain.services.session_consent import DurableProviderConsentStore, SessionConsentError

__all__ = [
    "PolicyAuthoringError",
    "PolicyInputs",
    "PolicyPreview",
    "apply_scoped_policy",
    "current_work_consent",
    "guide_principal",
    "preview_scoped_policy",
]

_POLICY_VERSION: Final = "launcher-policy.v1"
_MAX_POLICY_BYTES: Final = 65_536


class PolicyAuthoringError(ValueError):
    """Closed guide code; never carries policy or consent contents."""

    def __init__(self, code: str) -> None:
        if code not in {"authority_refused", "revision_conflict", "unsafe_path"}:
            raise ValueError("invalid policy authoring code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PolicyInputs:
    """Confirmed non-content inputs; the policy body is always regenerated from these."""

    workflow_id: str
    destination_space_id: str
    provider_id: str
    consent_id: str
    authorization_generation: int
    brain_id: str
    issuer_epoch: int


@dataclass(frozen=True, slots=True)
class PolicyPreview:
    payload: bytes
    policy_sha256: str
    expected_absent: bool


def guide_principal(workflow_id: str) -> tuple[str, str]:
    """`guide:`+SHA256(UTF8(workflow_id)) with `:client` and `:session` suffixes."""
    digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
    return f"guide:{digest}:client", f"guide:{digest}:session"


def _consent_state(
    consent_path: Path, *, brain_id: str, issuer_epoch: int
) -> ProviderConsentState:
    try:
        return DurableProviderConsentStore(
            consent_path, brain_id=brain_id, issuer_epoch=issuer_epoch
        ).load()
    except SessionConsentError:
        raise PolicyAuthoringError("authority_refused") from None


def current_work_consent(
    consent_path: Path, *, provider_id: str, brain_id: str, issuer_epoch: int
) -> tuple[str, int]:
    """The single active Work-only consent for this provider and the current generation."""
    state = _consent_state(consent_path, brain_id=brain_id, issuer_epoch=issuer_epoch)
    active = [
        record
        for record in state.inspect(owner=True).records
        if record.active and record.provider_id == provider_id
    ]
    if (
        len(active) != 1
        or active[0].egress_mode is not EgressMode.EXTERNAL_PROVIDER
        or active[0].allowed_tiers != frozenset({PrivacyTier.WORK})
    ):
        raise PolicyAuthoringError("authority_refused")
    return active[0].consent_id, state.authorization_generation


def _document(inputs: PolicyInputs) -> dict[str, object]:
    principal_id, session_id = guide_principal(inputs.workflow_id)
    return {
        "policy_version": _POLICY_VERSION,
        "principal_id": principal_id,
        "session_id": session_id,
        "capabilities": ["content-read", "search"],
        "space_ids": [inputs.destination_space_id],
        "allowed_read_tiers": ["work"],
        "allowed_capture_tiers": [],
        "egress_mode": "external_provider",
        "provider_id": inputs.provider_id,
        "consent_id": inputs.consent_id,
        "authorization_generation": inputs.authorization_generation,
        "brain_id": inputs.brain_id,
        "issuer_epoch": inputs.issuer_epoch,
    }


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _validate(payload: bytes, inputs: PolicyInputs, state: ProviderConsentState) -> None:
    """Exact guide scope, then the existing startup validation against current consent."""
    try:
        policy = parse_launcher_policy(payload)
        if (
            policy.capabilities != frozenset({"search", "content-read"})
            or policy.space_ids != frozenset({inputs.destination_space_id})
            or policy.allowed_read_tiers != frozenset({PrivacyTier.WORK})
            or policy.allowed_capture_tiers
            or policy.egress_mode is not EgressMode.EXTERNAL_PROVIDER
            or policy.provider_id != inputs.provider_id
            or policy.consent_id != inputs.consent_id
        ):
            raise PolicyAuthoringError("authority_refused")
        validate_startup_policy(
            payload,
            current_brain_id=inputs.brain_id,
            current_issuer_epoch=inputs.issuer_epoch,
            current_authorization_generation=state.authorization_generation,
            consent_state=state,
        )
    except LauncherPolicyError:
        raise PolicyAuthoringError("authority_refused") from None


def _existing(path: Path) -> bytes | None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise PolicyAuthoringError("unsafe_path") from None
    try:
        return _read_policy(path)
    except LauncherPolicyError:
        raise PolicyAuthoringError("unsafe_path") from None


def preview_scoped_policy(
    policy_path: Path, inputs: PolicyInputs, *, consent_path: Path
) -> PolicyPreview:
    """Full proposed (or exact existing) policy bytes and SHA, never written anywhere."""
    state = _consent_state(consent_path, brain_id=inputs.brain_id, issuer_epoch=inputs.issuer_epoch)
    existing = _existing(policy_path)
    payload = _canonical(_document(inputs)) if existing is None else existing
    # An existing file is only offered for reuse when a fresh parse proves exact scope.
    _validate(payload, inputs, state)
    return PolicyPreview(payload, hashlib.sha256(payload).hexdigest(), existing is None)


def _private_parent(parent: Path) -> int:
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    except OSError:
        raise PolicyAuthoringError("unsafe_path") from None
    try:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise PolicyAuthoringError("unsafe_path") from None
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise PolicyAuthoringError("unsafe_path")
    return fd


def apply_scoped_policy(
    policy_path: Path,
    inputs: PolicyInputs,
    *,
    policy_sha256: str,
    expected_absent: bool,
    consent_path: Path,
) -> str:
    """Regenerate, compare, revalidate, then create-only; an exact existing file is reused."""
    state = _consent_state(consent_path, brain_id=inputs.brain_id, issuer_epoch=inputs.issuer_epoch)
    existing = _existing(policy_path)
    if not expected_absent:
        # Reuse is never a write: the exact confirmed bytes must still be there.
        if existing is None or hashlib.sha256(existing).hexdigest() != policy_sha256:
            raise PolicyAuthoringError("revision_conflict")
        _validate(existing, inputs, state)
        return policy_sha256
    if existing is not None:
        raise PolicyAuthoringError("revision_conflict")
    payload = _canonical(_document(inputs))
    if hashlib.sha256(payload).hexdigest() != policy_sha256 or len(payload) > _MAX_POLICY_BYTES:
        raise PolicyAuthoringError("revision_conflict")
    _validate(payload, inputs, state)
    parent = _private_parent(policy_path.parent)
    try:
        temporary = f".{policy_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            # Create-only: an existing name, including a concurrent writer's, is never replaced.
            os.link(temporary, policy_path.name, src_dir_fd=parent, dst_dir_fd=parent)
        except FileExistsError:
            raise PolicyAuthoringError("revision_conflict") from None
        finally:
            os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
    except OSError:
        raise PolicyAuthoringError("unsafe_path") from None
    finally:
        os.close(parent)
    return policy_sha256
