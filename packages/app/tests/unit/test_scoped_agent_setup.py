"""Synthetic scoped setup exercises the real policy/consent and owned writers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import open_local_engine

import open_brain.services.agent_settings_observation as observation_module
from open_brain.local_data import select_local_root
from open_brain.profile import compile_single_user_local
from open_brain.services.agent_settings_observation import (
    SettingsObservationError,
    public_authority,
    read_observed,
)
from open_brain.services.agent_setup import (
    AgentSetupFailure,
    apply_agent_setup,
    preview_agent_setup,
)
from open_brain.services.local_operations import current_brain_identity
from open_brain.services.session_consent import DurableProviderConsentStore


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    system = tmp_path / "system"
    monkeypatch.setattr(
        observation_module, "system_roots", lambda _platform: (system / "claude", system / "codex")
    )
    selection = select_local_root(
        data_dir=str(home / "brain"), environment={"HOME": str(home)}, platform_name="darwin"
    )
    tasks = open_local_engine(
        compile_single_user_local(selection.brain_root, starter_spaces=("Recall",))
    )
    brain_id, issuer_epoch = current_brain_identity(tasks)
    # Read through the same confined database accessor as production validation.
    from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
    from open_brain_engine.storage.sqlite import connect_database_read_only

    connection = connect_database_read_only(
        root=tasks.profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=tasks.profile.root_identity,
    )
    try:
        space_id = connection.execute("SELECT space_id FROM spaces WHERE name='Recall'").fetchone()[
            0
        ]
    finally:
        connection.close()
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    consent_path = authority / "consent.json"
    store = DurableProviderConsentStore(consent_path, brain_id=brain_id, issuer_epoch=issuer_epoch)
    consent_id = "consent_" + "a" * 32
    store.grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.WORK}),
        operation_id="synthetic-grant",
        decided_at="2026-10-14T12:00:00Z",
        consent_id_factory=lambda: consent_id,
    )
    policy_path = authority / "policy.json"
    policy = {
        "policy_version": "launcher-policy.v1",
        "principal_id": "synthetic",
        "session_id": "synthetic-session",
        "capabilities": ["search", "content-read"],
        "space_ids": [space_id],
        "allowed_read_tiers": ["work"],
        "allowed_capture_tiers": [],
        "egress_mode": "external_provider",
        "provider_id": "synthetic-provider",
        "consent_id": consent_id,
        "authorization_generation": 1,
        "brain_id": brain_id,
        "issuer_epoch": issuer_epoch,
    }
    policy_path.write_text(json.dumps(policy))
    runtime = tmp_path / "runtime/open-brain"
    runtime.parent.mkdir(mode=0o700)
    runtime.write_text("#!/bin/sh\nexit 0\n")
    runtime.chmod(0o700)
    inputs = {
        "client": "claude-code",
        "scope": "project",
        "project_dir": str(project),
        "allow_capture": False,
        "allow_search": True,
        "allow_content_read": True,
        "action": "configure",
        "runtime_path": runtime,
        "environment": {"HOME": str(home)},
        "session_policy": str(policy_path),
        "consent_state": str(consent_path),
        "client_version": "2.1.300",
        "launch_context": {
            "surface": "standalone-cli",
            "cwd": str(project),
            "profile": None,
            "setting_sources": ["user", "project", "local"],
            "settings_file": None,
            "overrides": [],
            "managed_state": "owner-declared-absent",
        },
        "acknowledge_unscoped": False,
    }
    return {
        "selection": selection,
        "inputs": inputs,
        "project": project,
        "home": home,
        "system": system,
        "store": store,
        "consent_id": consent_id,
        "policy": policy,
        "policy_path": policy_path,
    }


def preview(state: dict[str, Any]) -> dict[str, object]:
    return preview_agent_setup(state["selection"], **state["inputs"])


def apply(state: dict[str, Any], before: dict[str, object], **extra: object) -> dict[str, object]:
    return apply_agent_setup(
        state["selection"], preview_id=before["preview_id"], **state["inputs"], **extra
    )


def test_scoped_setup_launches_policy_and_preserves_unrelated_settings(
    configured: dict[str, Any],
) -> None:
    state = configured
    settings = state["project"] / ".claude/settings.json"
    settings.parent.mkdir()
    original = b'{"model":"synthetic-model","env":{"ANTHROPIC_API_KEY":"synthetic-secret","ANTHROPIC_BASE_URL":"https://alice:secret@gateway.invalid/private?secret#secret"}}\n'
    settings.write_bytes(original)
    before = preview(state)
    public = before["settings_observation"]
    assert "https://gateway.invalid" in repr(public)
    assert "secret" not in repr(public)
    assert "/private" not in repr(public)
    assert apply(state, before)["status"] == "configured"
    entry = json.loads((state["project"] / ".mcp.json").read_text())["mcpServers"]["open-brain"]
    assert entry["args"][-2:] == ["--allow-search", "--allow-content-read"]
    assert entry["args"][3:7] == [
        "--session-policy",
        str(state["policy_path"]),
        "--consent-state",
        state["inputs"]["consent_state"],
    ]
    assert settings.read_bytes() == original


@pytest.mark.parametrize(
    "change", ["new-file", "same-bytes-replaced", "environment", "acknowledgment"]
)
def test_observed_changes_stale_preview_before_any_write(
    configured: dict[str, Any], change: str
) -> None:
    state = configured
    settings = state["home"] / ".claude/settings.json"
    settings.parent.mkdir()
    if change == "same-bytes-replaced":
        settings.write_text("{}")
    before = preview(state)
    if change == "new-file":
        settings.write_text("{}")
    elif change == "same-bytes-replaced":
        replacement = settings.with_suffix(".new")
        replacement.write_bytes(settings.read_bytes())
        replacement.replace(settings)
    elif change == "environment":
        state["inputs"]["environment"]["HTTPS_PROXY"] = "https://proxy.invalid"
    else:
        state["inputs"]["acknowledge_unscoped"] = True
    with pytest.raises(AgentSetupFailure, match="setup_preview_stale"):
        apply(state, before)
    assert not (state["project"] / ".mcp.json").exists()


def test_unowned_other_client_entry_requires_bound_acknowledgment(
    configured: dict[str, Any],
) -> None:
    state = configured
    config = state["home"] / ".codex/config.toml"
    config.parent.mkdir()
    original = b'[mcp_servers.other-name]\ncommand="open-brain"\nargs=["mcp"]\n'
    config.write_bytes(original)
    with pytest.raises(AgentSetupFailure, match="setup_conflict"):
        preview(state)
    state["inputs"]["acknowledge_unscoped"] = True
    before = preview(state)
    assert before["settings_observation"]["unscoped_entries"] == [
        {"source": "codex-user", "count": 1}
    ]
    apply(state, before)
    assert config.read_bytes() == original


@pytest.mark.parametrize(
    "which",
    [
        "managed-file",
        "managed-directory",
        "worktree",
        "unknown-version",
        "unknown-context-field",
        "launch-overrides",
        "nondefault-sources",
        "helper",
        "legacy-profile",
        "symlink",
    ],
)
def test_unsupported_context_and_layers_refuse(configured: dict[str, Any], which: str) -> None:
    state = configured
    if which.startswith("managed"):
        root = state["system"] / "claude"
        root.mkdir(parents=True)
        if which == "managed-file":
            (root / "managed-settings.json").write_text("{}")
        else:
            (root / "managed-settings.d").mkdir()
    elif which == "worktree":
        (state["project"] / ".git").write_text("gitdir: /synthetic/elsewhere")
    elif which == "unknown-version":
        state["inputs"]["client_version"] = "unknown"
    elif which == "unknown-context-field":
        state["inputs"]["launch_context"]["unexpected"] = True
    elif which == "launch-overrides":
        state["inputs"]["launch_context"]["overrides"] = ["--remote"]
    elif which == "nondefault-sources":
        state["inputs"]["launch_context"]["setting_sources"] = ["project"]
    elif which == "legacy-profile":
        path = state["home"] / ".codex/config.toml"
        path.parent.mkdir()
        path.write_text('[profiles.old]\nmodel_provider="old"')
    else:
        path = state["home"] / ".claude/settings.json"
        path.parent.mkdir()
        if which == "helper":
            path.write_text('{"policyHelper":"never execute"}')
        else:
            target = path.parent / "target.json"
            target.write_text("{}")
            path.symlink_to(target)
    with pytest.raises(AgentSetupFailure, match="invalid_arguments"):
        preview(state)
    assert not (state["project"] / ".mcp.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowed_read_tiers", ["personal"]),
        ("capabilities", ["search", "content-read", "history-read"]),
        ("space_ids", None),
        ("space_ids", ["space_11111111-1111-1111-1111-111111111111"]),
        ("egress_mode", "owner_local"),
    ],
)
def test_policy_scope_restrictions(configured: dict[str, Any], field: str, value: object) -> None:
    configured["policy"][field] = value
    configured["policy_path"].write_text(json.dumps(configured["policy"]))
    with pytest.raises(AgentSetupFailure, match="invalid_arguments"):
        preview(configured)


def test_revoked_policy_rolls_back_and_removal_ignores_deleted_authority(
    configured: dict[str, Any],
) -> None:
    state = configured
    before = preview(state)
    writes: list[Path] = []

    def revoke_after_write(path: Path, payload: bytes | None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        assert payload is not None
        path.write_bytes(payload)
        writes.append(path)
        state["policy_path"].unlink(missing_ok=True)

    with pytest.raises(AgentSetupFailure, match="operation_failed"):
        apply(state, before, write_file=revoke_after_write)
    assert writes
    assert not (state["project"] / ".mcp.json").exists()
    state["policy_path"].write_text(json.dumps(state["policy"]))
    apply(state, preview(state))
    state["policy_path"].unlink()
    Path(state["inputs"]["consent_state"]).unlink()
    state["inputs"].update(
        action="remove",
        client_version="unknown",
        launch_context={"invalid": True},
        acknowledge_unscoped="invalid",
    )
    removal = preview(state)
    assert "settings_observation" not in removal
    assert apply(state, removal)["status"] == "removed"
    assert not (state["project"] / ".mcp.json").exists()


def test_codex_profile_and_user_scope_launch_project_are_bound(configured: dict[str, Any]) -> None:
    state = configured
    state["inputs"].update(client="codex", client_version="0.162.1", scope="user", project_dir=None)
    state["inputs"]["launch_context"]["profile"] = "recall"
    config = state["home"] / ".codex/config.toml"
    config.parent.mkdir()
    original = b'model_provider="synthetic-provider"\n'
    config.write_bytes(original)
    profile = config.parent / "recall.config.toml"
    profile.write_text('openai_base_url="https://profile.invalid/private?secret"')
    before = preview(state)
    assert "https://profile.invalid" in repr(before["settings_observation"])
    apply(state, before)
    assert config.read_bytes().startswith(original)
    assert "--session-policy" in config.read_text()


def test_observer_authority_only_and_no_proxy_values_are_redacted() -> None:
    assert (
        public_authority("https://name:synthetic-secret@[::1]:8443/private?secret#secret")
        == "https://[::1]:8443"
    )
    with pytest.raises(SettingsObservationError):
        public_authority("https://example.invalid:bad?secret")


def test_read_observed_rejects_oversize_and_unsafe_modes(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"12345")
    with pytest.raises(SettingsObservationError):
        read_observed(path, maximum=4)
    path.chmod(0o666)
    with pytest.raises(SettingsObservationError):
        read_observed(path)


def test_selected_project_nested_claude_mcp_is_detected(configured: dict[str, Any]) -> None:
    state = configured
    nested = {
        "projects": {
            str(state["project"]): {
                "mcpServers": {
                    "arbitrary-name": {"command": "python", "args": ["-m", "open_brain", "mcp"]}
                }
            }
        }
    }
    path = state["home"] / ".claude.json"
    path.write_text(json.dumps(nested))
    with pytest.raises(AgentSetupFailure, match="setup_conflict"):
        preview(state)
    state["inputs"]["acknowledge_unscoped"] = True
    assert preview(state)["settings_observation"]["unscoped_entries"] == [
        {"source": "claude-user-mcp", "count": 1}
    ]


def test_prewrite_barrier_revalidates_observation_after_plan_computation(
    configured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_brain.services.agent_setup as setup_module

    state = configured
    before = preview(state)
    original = setup_module._instructions

    def replacing_instructions(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        path = state["home"] / ".claude/settings.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text("{}")
        return result

    monkeypatch.setattr(setup_module, "_instructions", replacing_instructions)
    with pytest.raises(AgentSetupFailure, match="setup_preview_stale"):
        apply(state, before)
    assert not (state["project"] / ".mcp.json").exists()


def test_unsafe_observation_parent_refuses(configured: dict[str, Any]) -> None:
    state = configured
    parent = state["home"] / ".claude"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    (parent / "settings.json").write_text("{}")
    with pytest.raises(AgentSetupFailure, match="invalid_arguments"):
        preview(state)


@pytest.mark.parametrize(
    "original",
    [
        b'{  "model" : "synthetic", "env": { "KEY": "synthetic-secret" } }\n',
        b'{"model":"synthetic", "mcpServers" : { }, "env" : {"KEY":"synthetic-secret"}}\n',
        b'{"mcpServers":{"unrelated" : { "command" : "other" }}, "model" : "synthetic"}\n',
        '{"model":"synthetic-\\u0061", "display":"\u00e9", "mcpServers" : {}}\r\n'.encode(),
    ],
)
def test_claude_owned_json_edit_preserves_unrelated_bytes_and_roundtrips(
    configured: dict[str, Any], original: bytes
) -> None:
    state = configured
    path = state["project"] / ".mcp.json"
    path.write_bytes(original)
    apply(state, preview(state))
    changed = path.read_bytes()
    assert b'"model"' in changed
    state["inputs"]["action"] = "remove"
    apply(state, preview(state))
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "grant",
    [
        "allow_capture",
        "allow_history_read",
        "allow_facet_read",
        "allow_facet_assign",
        "allow_inbox_read",
        "allow_organize",
        "allow_review_propose",
        "allow_review_decide",
    ],
)
def test_scoped_setup_rejects_additional_grants(configured: dict[str, Any], grant: str) -> None:
    configured["inputs"][grant] = True
    with pytest.raises(AgentSetupFailure, match="invalid_arguments"):
        preview(configured)


def test_revoked_consent_refuses_apply_before_config_write(configured: dict[str, Any]) -> None:
    state = configured
    before = preview(state)
    state["store"].revoke(
        consent_id=state["consent_id"], operation_id="revoke", decided_at="2026-10-14T13:00:00Z"
    )
    with pytest.raises(AgentSetupFailure, match="invalid_arguments"):
        apply(state, before)
    assert not (state["project"] / ".mcp.json").exists()


def test_public_observation_omits_no_proxy_and_selector_identifiers(
    configured: dict[str, Any],
) -> None:
    state = configured
    state["inputs"]["environment"].update(
        NO_PROXY="private.synthetic.invalid", CLAUDE_CODE_USE_BEDROCK="1"
    )
    path = state["home"] / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text(
        'model_provider="synthetic-private-selector"\n[model_providers.synthetic-private-selector]\nbase_url="https://name:secret@provider.invalid/private?secret"\nhttp_headers={"X-Key"="synthetic-secret"}\n'
    )
    public = repr(preview(state)["settings_observation"])
    assert "private.synthetic.invalid" not in public
    assert "synthetic-private-selector" not in public
    assert "secret" not in public
    assert "https://provider.invalid" in public


def test_parent_directory_replacement_stales_before_apply(configured: dict[str, Any]) -> None:
    state = configured
    parent = state["home"] / ".claude"
    parent.mkdir()
    (parent / "settings.json").write_text("{}")
    before = preview(state)
    moved = parent.with_name("old-claude")
    parent.rename(moved)
    parent.mkdir()
    (parent / "settings.json").write_bytes((moved / "settings.json").read_bytes())
    with pytest.raises(AgentSetupFailure, match="setup_preview_stale"):
        apply(state, before)
    assert not (state["project"] / ".mcp.json").exists()


def test_mac_system_root_uses_only_known_canonical_os_alias() -> None:
    claude, codex = observation_module.system_roots("darwin")
    assert str(claude) == "/Library/Application Support/ClaudeCode"
    assert str(codex) == "/private/etc/codex"
