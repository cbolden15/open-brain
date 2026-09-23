"""Trusted whole-session authority loading and live revocation checks."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from open_brain_engine.engine import EngineTaskSet
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.storage.operational import (
    StorageError,
    capture_root_identity,
    read_confined,
)

from open_brain.services.launcher_policy import (
    LauncherPolicyError,
    parse_launcher_policy,
    validate_startup_policy,
)
from open_brain.services.local_operations import current_brain_identity
from open_brain.services.session_consent import (
    DurableProviderConsentStore,
    SessionConsentError,
)

__all__ = ["TrustedSessionAuthority"]

_MAX_POLICY_BYTES = 65_536


class TrustedSessionAuthority:
    """Reload one trusted launcher policy and its consent before every MCP operation."""

    def __init__(
        self,
        tasks: EngineTaskSet,
        *,
        policy_path: Path,
        consent_state_path: Path | None = None,
    ) -> None:
        if (
            not isinstance(tasks, EngineTaskSet)
            or not isinstance(policy_path, Path)
            or not policy_path.is_absolute()
            or policy_path.name in {"", ".", ".."}
            or consent_state_path is not None
            and (not isinstance(consent_state_path, Path) or not consent_state_path.is_absolute())
        ):
            raise LauncherPolicyError("invalid_policy")
        self._tasks = tasks
        self._policy_path = policy_path
        self._consent_state_path = consent_state_path

    def load(self) -> EffectiveAuthority:
        raw_policy = _read_policy(self._policy_path)
        policy = parse_launcher_policy(raw_policy)
        brain_id, issuer_epoch = current_brain_identity(self._tasks)
        consent_state = None
        authorization_generation = 0
        if policy.egress_mode is EgressMode.EXTERNAL_PROVIDER:
            if self._consent_state_path is None:
                raise LauncherPolicyError("consent_unavailable")
            try:
                consent_state = DurableProviderConsentStore(
                    self._consent_state_path,
                    brain_id=brain_id,
                    issuer_epoch=issuer_epoch,
                ).load()
            except SessionConsentError:
                raise LauncherPolicyError("consent_unavailable") from None
            authorization_generation = consent_state.authorization_generation
        elif self._consent_state_path is not None:
            raise LauncherPolicyError("invalid_policy")
        return validate_startup_policy(
            raw_policy,
            current_brain_id=brain_id,
            current_issuer_epoch=issuer_epoch,
            current_authorization_generation=authorization_generation,
            consent_state=consent_state,
        )

    def revalidate(self, expected: EffectiveAuthority) -> EffectiveAuthority:
        if not isinstance(expected, EffectiveAuthority):
            raise LauncherPolicyError("invalid_policy")
        current = self.load()
        if current != expected:
            raise LauncherPolicyError("stale_policy")
        return expected


def _read_policy(path: Path) -> bytes:
    try:
        parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise LauncherPolicyError("invalid_policy")
        root_identity = capture_root_identity(path.parent)
        payload = read_confined(
            root=path.parent,
            relative=path.name,
            expected_root_identity=root_identity,
            maximum_bytes=_MAX_POLICY_BYTES,
        )
        if payload is None:
            raise LauncherPolicyError("invalid_policy")
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise LauncherPolicyError("invalid_policy")
        return payload
    except LauncherPolicyError:
        raise
    except (OSError, StorageError):
        raise LauncherPolicyError("invalid_policy") from None
