"""Owner-selected local source privacy, bound to retained intake and selection."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import PrivacyDecision
from open_brain_engine.engine.source_observation import SourceRevisionObservation

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.source_intake import SourceRecordIntake

_TIERS = frozenset({"public", "work", "personal", "secret", "unknown"})


def source_privacy(tier: object) -> PrivacyDecision:
    if type(tier) is not str or tier not in _TIERS:
        raise LiveSourceError("source_invalid_privacy_policy")
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": False},
            "confirmation_ref": None,
            "policy_version": "collector-source-privacy-v1",
            "reason": {
                "public": "policy_public",
                "work": "policy_work",
                "personal": "personal_local_only",
                "secret": "secret_detected",
                "unknown": "classification_missing",
            }[tier],
            "tier": tier,
        }
    )


def validate_retained_privacy(privacy: PrivacyDecision) -> None:
    from open_brain_connectors.runtime.live_common import local_source_privacy

    if privacy != local_source_privacy() and privacy != source_privacy(privacy.tier.value):
        raise LiveSourceError("source_invalid_preview")


def validate_options(options: dict[str, object]) -> None:
    if "privacy_tier" in options:
        source_privacy(options["privacy_tier"])


def provider_options(options: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in options.items() if key != "privacy_tier"}


def admitted_intake(intake: SourceRecordIntake, options: dict[str, object]) -> SourceRecordIntake:
    """Select privacy before persistence; default source bytes remain compatible."""
    if "privacy_tier" not in options:
        return intake
    privacy = source_privacy(options["privacy_tier"])
    if intake.observation is not None:
        raise LiveSourceError("source_privacy_observation_mismatch")
    # The source observation identity includes policy: retrying changed authority
    # cannot reuse a capture operation reserved for different admitted bytes.
    revision = hashlib.sha256(bounded_json([intake.key.revision_id, privacy.to_dict()])).hexdigest()
    return replace(
        intake,
        key=replace(intake.key, revision_id=intake.key.revision_id + "~policy:" + revision),
        privacy=privacy,
    )


def observe_slack(intake: SourceRecordIntake) -> SourceRecordIntake:
    """Attest transformed payload and privacy for the existing revision capability."""
    if intake.key.connector_name != "slack" or intake.observation is not None:
        return intake
    digest = hashlib.sha256(intake.text.encode()).hexdigest()
    privacy = intake.privacy.to_dict()
    payload = intake.payload().to_dict()
    bounded_json(payload)  # Preserve the existing intake size admission limit.
    return replace(
        intake,
        observation=SourceRevisionObservation(
            original_sha256=digest,
            transformed_sha256=digest,
            normalization_version="slack-source-v1",
            privacy_policy_version=cast(str, privacy["policy_version"]),
            privacy_policy_sha256=hashlib.sha256(bounded_json(privacy)).hexdigest(),
            admitted_payload_sha256=hashlib.sha256(
                portable_canonical_json_bytes(payload)
            ).hexdigest(),
        ),
    )
