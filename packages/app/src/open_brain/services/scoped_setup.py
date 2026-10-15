"""Read-only validation of a scoped setup's existing launcher authority."""

from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.storage.operational import capture_root_identity
from open_brain_engine.storage.sqlite import connect_database_read_only

from open_brain.services.agent_settings_observation import absolute_path, read_observed
from open_brain.services.launcher_policy import parse_launcher_policy, validate_startup_policy
from open_brain.services.session_authority import _read_policy
from open_brain.services.session_consent import DurableProviderConsentStore


def validate_scoped_setup(
    *, brain_root: Path, session_policy: object, consent_state: object
) -> tuple[list[str], str, Callable[[], None]]:
    policy_path = absolute_path(session_policy)
    consent_path = absolute_path(consent_state)

    def load() -> str:
        _payload, policy_identity = read_observed(policy_path, maximum=65536)
        raw = _read_policy(policy_path)
        policy = parse_launcher_policy(raw)
        if (
            policy.egress_mode is not EgressMode.EXTERNAL_PROVIDER
            or policy.allowed_read_tiers != frozenset({PrivacyTier.WORK})
            or policy.allowed_capture_tiers
            or policy.capabilities != frozenset({"search", "content-read"})
            or policy.space_ids is None
            or len(policy.space_ids) != 1
        ):
            raise ValueError("invalid scoped setup policy")
        root_identity = capture_root_identity(brain_root)
        connection = connect_database_read_only(
            root=brain_root,
            database_name=PHASE1_STATE_DATABASE,
            expected_root_identity=root_identity,
        )
        try:
            connection.execute("BEGIN")
            identity = connection.execute(
                "SELECT brain_id, issuer_epoch FROM brain_identity"
            ).fetchone()
            if (
                identity is None
                or connection.execute(
                    "SELECT 1 FROM spaces WHERE space_id=?", (next(iter(policy.space_ids)),)
                ).fetchone()
                is None
            ):
                raise ValueError("invalid scoped setup destination")
            brain_id, issuer_epoch = identity
        finally:
            connection.close()
        _consent, consent_identity = read_observed(consent_path, maximum=16 * 1024 * 1024 + 4096)
        state = DurableProviderConsentStore(
            consent_path, brain_id=brain_id, issuer_epoch=issuer_epoch
        ).load()
        validate_startup_policy(
            raw,
            current_brain_id=brain_id,
            current_issuer_epoch=issuer_epoch,
            current_authorization_generation=state.authorization_generation,
            consent_state=state,
        )
        # Ensure the validated bytes are the committed bytes, including same-byte replacement.
        if (
            read_observed(policy_path, maximum=65536)[1] != policy_identity
            or read_observed(consent_path, maximum=16 * 1024 * 1024 + 4096)[1] != consent_identity
        ):
            raise ValueError("scoped setup changed")
        return sha256(
            json.dumps(
                [policy_identity, consent_identity, root_identity, brain_id, issuer_epoch],
                sort_keys=True,
            ).encode()
        ).hexdigest()

    commitment = load()

    def revalidate() -> None:
        if load() != commitment:
            raise ValueError("scoped setup changed")

    return (
        ["--session-policy", str(policy_path), "--consent-state", str(consent_path)],
        commitment,
        revalidate,
    )
