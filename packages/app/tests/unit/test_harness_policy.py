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


def test_source_rule_overrides_every_client_default(tmp_path: Path) -> None:
    value = policy_value(tmp_path)
    value["client_defaults"] = {"pi": "work", "codex": "public"}
    policy = HarnessPolicy.from_dict(value)
    assert policy.classify(tmp_path).tier is PrivacyTier.PERSONAL
    for client in ("pi", "codex", "claude-code", "hermes", "future-client"):
        decision = policy.capture_decision(client, tmp_path)
        assert decision.source_id == "home"
        assert decision.tier is PrivacyTier.PERSONAL


def test_unmatched_project_uses_builtin_defaults_and_guardrail_two(tmp_path: Path) -> None:
    classified = tmp_path / "classified"
    classified.mkdir()
    policy = HarnessPolicy.from_dict(policy_value(classified))
    decision = policy.classify(tmp_path, authored_text="This project is public and safe to share.")
    assert decision.source_id is None
    assert decision.tier is PrivacyTier.UNKNOWN
    assert decision.local_only
    tiers = {
        client: policy.capture_decision(client, tmp_path).tier
        for client in ("pi", "claude-code", "codex", "hermes", "future-client")
    }
    assert tiers == {
        "pi": PrivacyTier.UNKNOWN,
        "claude-code": PrivacyTier.PERSONAL,
        "codex": PrivacyTier.PERSONAL,
        "hermes": PrivacyTier.UNKNOWN,
        # Guardrail 2: a client with no explicit owner default is unknown.
        "future-client": PrivacyTier.UNKNOWN,
    }


def test_owner_client_defaults_replace_builtin_defaults(tmp_path: Path) -> None:
    classified = tmp_path / "classified"
    classified.mkdir()
    value = policy_value(classified)
    value["client_defaults"] = {"pi": "personal", "codex": "work", "future-client": "public"}
    policy = HarnessPolicy.from_dict(value)
    assert policy.capture_decision("pi", tmp_path).tier is PrivacyTier.PERSONAL
    assert policy.capture_decision("codex", tmp_path).tier is PrivacyTier.WORK
    assert policy.capture_decision("claude-code", tmp_path).tier is PrivacyTier.PERSONAL
    assert policy.capture_decision("future-client", tmp_path).tier is PrivacyTier.PUBLIC


@pytest.mark.parametrize("marking", ["Confidential:", "Local only:", "Private:"])
def test_user_markings_lower_capture_tier_to_unknown(tmp_path: Path, marking: str) -> None:
    policy = HarnessPolicy.from_dict(policy_value(tmp_path))
    decision = policy.classify(tmp_path, authored_text=marking + " a sensitive decision")
    assert decision.tier is PrivacyTier.PERSONAL
    assert decision.local_only
    marked = policy.capture_decision("codex", tmp_path, authored_text=marking + " a decision")
    assert marked.tier is PrivacyTier.UNKNOWN
    assert policy.capture_decision("codex", tmp_path, local_only=True).tier is PrivacyTier.UNKNOWN


def test_local_only_marking_never_raises_a_tier(tmp_path: Path) -> None:
    child = tmp_path / "client"
    child.mkdir()
    value = policy_value(tmp_path)
    sources = value["sources"]
    assert isinstance(sources, list)
    sources.append({"id": "client", "project_root": str(child), "tier": "work", "local_only": True})
    sealed = tmp_path / "vault"
    sealed.mkdir()
    sources.append(
        {"id": "vault", "project_root": str(sealed), "tier": "secret", "local_only": True}
    )
    policy = HarnessPolicy.from_dict(value)
    decision = policy.classify(child)
    assert decision.source_id == "client"
    assert decision.local_only
    assert policy.capture_decision("codex", child).tier is PrivacyTier.UNKNOWN
    assert policy.capture_decision("codex", sealed).tier is PrivacyTier.SECRET
    assert policy.capture_decision("pi", sealed, local_only=True).tier is PrivacyTier.SECRET


def test_exclusion_cannot_be_overridden_by_more_specific_rule(tmp_path: Path) -> None:
    value = policy_value(tmp_path)
    value["exclusions"] = [str(tmp_path)]
    policy = HarnessPolicy.from_dict(value)
    with pytest.raises(HarnessPolicyError, match="source_excluded"):
        policy.classify(tmp_path)
    with pytest.raises(HarnessPolicyError, match="source_excluded"):
        policy.capture_decision("codex", tmp_path)


@pytest.mark.parametrize(
    "defaults",
    [{"pi": "secret"}, {"pi": "confidential"}, {"Bad Name": "work"}, ["pi"], {"pi": 1}],
)
def test_invalid_client_defaults_are_rejected(tmp_path: Path, defaults: object) -> None:
    value = policy_value(tmp_path)
    value["client_defaults"] = defaults
    with pytest.raises(HarnessPolicyError):
        HarnessPolicy.from_dict(value)


def test_v1_files_load_with_or_without_legacy_provider_grants(tmp_path: Path) -> None:
    value = policy_value(tmp_path)
    legacy = HarnessPolicy.from_dict(value)
    del value["providers"]
    current = HarnessPolicy.from_dict(value)
    # Provider grants are no longer a read authority, so both files are equal.
    assert legacy == current
    assert not hasattr(current, "allowed_tiers")


def test_disabled_policy_refuses_capture(tmp_path: Path) -> None:
    value = policy_value(tmp_path)
    value["enabled"] = False
    policy = HarnessPolicy.from_dict(value)
    with pytest.raises(HarnessPolicyError, match="disabled"):
        policy.classify(tmp_path)
    with pytest.raises(HarnessPolicyError, match="disabled"):
        policy.capture_decision("codex", tmp_path)


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
