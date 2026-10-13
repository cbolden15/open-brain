"""Source policy is owner-selected and can never widen unknown access."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from open_brain_engine.core.models import PrivacyTier

from open_brain.services.harness_policy import (
    HarnessPolicy,
    HarnessPolicyError,
    load_harness_policy,
)


def policy_value(root: Path) -> dict[str, object]:
    return {
        "schema_version": "open-brain-harness-policy.v1",
        "enabled": True,
        "providers": {"openai": ["work", "personal"], "anthropic": ["work", "personal"]},
        "sources": [
            {"id": "home", "project_root": str(root), "tier": "personal", "local_only": False}
        ],
        "exclusions": [],
    }


def test_explicit_personal_source_is_shared_only_with_granted_providers(tmp_path: Path) -> None:
    policy = HarnessPolicy.from_dict(policy_value(tmp_path))
    decision = policy.classify(tmp_path)
    assert decision.tier is PrivacyTier.PERSONAL
    assert policy.permits_provider(decision, "openai")
    assert policy.permits_provider(decision, "anthropic")
    assert not policy.permits_provider(decision, "unconfigured")


def test_unknown_project_is_local_without_grant_inference(tmp_path: Path) -> None:
    classified = tmp_path / "classified"
    classified.mkdir()
    policy = HarnessPolicy.from_dict(policy_value(classified))
    decision = policy.classify(tmp_path, authored_text="This project is public and safe to share.")
    assert decision.source_id is None
    assert decision.tier is PrivacyTier.UNKNOWN
    assert decision.local_only
    assert not policy.permits_provider(decision, "openai")


@pytest.mark.parametrize("marking", ["Confidential:", "Local only:", "Private:"])
def test_user_markings_narrow_permissive_source(tmp_path: Path, marking: str) -> None:
    policy = HarnessPolicy.from_dict(policy_value(tmp_path))
    decision = policy.classify(tmp_path, authored_text=marking + " a sensitive decision")
    assert decision.tier is PrivacyTier.PERSONAL
    assert decision.local_only
    assert not policy.permits_provider(decision, "openai")


def test_specific_confidential_source_overrides_parent(tmp_path: Path) -> None:
    child = tmp_path / "client"
    child.mkdir()
    value = policy_value(tmp_path)
    sources = value["sources"]
    assert isinstance(sources, list)
    sources.append({"id": "client", "project_root": str(child), "tier": "work", "local_only": True})
    policy = HarnessPolicy.from_dict(value)
    decision = policy.classify(child)
    assert decision.source_id == "client"
    assert decision.local_only
    assert not policy.permits_provider(decision, "anthropic")


def test_exclusion_cannot_be_overridden_by_more_specific_rule(tmp_path: Path) -> None:
    value = policy_value(tmp_path)
    value["exclusions"] = [str(tmp_path)]
    policy = HarnessPolicy.from_dict(value)
    with pytest.raises(HarnessPolicyError, match="source_excluded"):
        policy.classify(tmp_path)


@pytest.mark.parametrize("tier", ["secret", "unknown"])
def test_restricted_tiers_cannot_receive_provider_grants(tmp_path: Path, tier: str) -> None:
    value = policy_value(tmp_path)
    value["providers"] = {"openai": [tier]}
    with pytest.raises(HarnessPolicyError):
        HarnessPolicy.from_dict(value)


def test_disabled_policy_refuses_capture_and_all_cloud_grants(tmp_path: Path) -> None:
    value = policy_value(tmp_path)
    value["enabled"] = False
    policy = HarnessPolicy.from_dict(value)
    assert not policy.allowed_tiers("openai")
    with pytest.raises(HarnessPolicyError, match="disabled"):
        policy.classify(tmp_path)


def test_revoked_provider_loses_access(tmp_path: Path) -> None:
    value = policy_value(tmp_path)
    initial = HarnessPolicy.from_dict(value)
    value["providers"] = {}
    revoked = HarnessPolicy.from_dict(value)
    decision = initial.classify(tmp_path)
    assert initial.permits_provider(decision, "openai")
    assert not revoked.permits_provider(decision, "openai")


def test_relative_or_duplicate_source_roots_are_rejected(tmp_path: Path) -> None:
    value = policy_value(tmp_path)
    value["sources"] = [
        {"id": "home", "project_root": "relative", "tier": "work", "local_only": False}
    ]
    with pytest.raises(HarnessPolicyError):
        HarnessPolicy.from_dict(value)
    value = policy_value(tmp_path)
    sources = value["sources"]
    assert isinstance(sources, list)
    sources.append(
        {"id": "second", "project_root": str(tmp_path), "tier": "work", "local_only": False}
    )
    with pytest.raises(HarnessPolicyError):
        HarnessPolicy.from_dict(value)


def test_owner_only_file_is_loaded_and_world_readable_file_refused(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_value(tmp_path)))
    path.chmod(0o600)
    assert load_harness_policy(path).enabled
    path.chmod(0o644)
    with pytest.raises(HarnessPolicyError, match="unsafe_policy"):
        load_harness_policy(path)


def test_symlink_or_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_value(tmp_path)))
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(HarnessPolicyError):
        load_harness_policy(link)
    path.write_text('{"enabled":true,"enabled":false}')
    with pytest.raises(HarnessPolicyError):
        load_harness_policy(path)
