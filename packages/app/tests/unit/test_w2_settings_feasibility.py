"""W2.0 synthetic contract spike, NOT a client effective-configuration resolver.

Evidence: version/help only under empty synthetic homes on 2026-10-14:
Claude Code 2.1.300; codex-cli 0.162.1 (--profile advertises <name>.config.toml).
Documented precedence (not empirically exercised client execution):
https://learn.chatgpt.com/docs/config-file/config-basic
https://learn.chatgpt.com/docs/config-file/config-reference
https://code.claude.com/docs/en/settings
https://code.claude.com/docs/en/env-vars
https://code.claude.com/docs/en/network-config

Proposed initial context: standalone CLI, ordinary project root == cwd, explicit
home/config root and launch inputs. Unknown versions, worktrees, hosted clients,
unknown managed inputs and unresolved launch routing overrides are unsupported.
Observer labels configuration candidates; it never verifies inference transport.
All helpers live in this test spike pending coordinator contract freeze.
"""

from __future__ import annotations

import hashlib
import json
import stat
import tomllib
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit

import pytest

VERSIONS = {"claude": "2.1.300", "codex": "0.162.1"}
PROXY_KEYS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")
CLAUDE_ROUTE_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
)
CODEX_PROJECT_IGNORED = frozenset(
    {
        "model_provider",
        "model_providers",
        "openai_base_url",
        "chatgpt_base_url",
        "profile",
        "profiles",
    }
)


class ContextOverrides(TypedDict, total=False):
    surface: str
    root_is_cwd: bool
    worktree: bool
    managed_state: str
    unresolved_overrides: bool


def require_supported_context(
    client: str,
    version: str,
    *,
    surface: str = "standalone-cli",
    root_is_cwd: bool = True,
    worktree: bool = False,
    managed_state: str = "owner-declared-absent",
    unresolved_overrides: bool = False,
) -> None:
    if (
        VERSIONS.get(client) != version
        or surface != "standalone-cli"
        or not root_is_cwd
        or worktree
        or managed_state != "owner-declared-absent"
        or unresolved_overrides
    ):
        raise ValueError("unsupported observation context")


def input_inventory(home: Path, project: Path, system: Path) -> tuple[Path, ...]:
    """Explicit synthetic roots; production adapters must bind actual OS roots.

    Initial proposal refuses present managed policy or managed fragments rather
    than guessing MDM/server/helper composition. Both clients' managed setup
    locations remain inventoried even when configuring only one client.
    """
    return (
        home / ".claude/settings.json",
        project / ".claude/settings.json",
        project / ".claude/settings.local.json",
        home / ".claude.json",
        project / ".mcp.json",
        home / ".codex/config.toml",
        project / ".codex/config.toml",
        system / "claude-code/managed-settings.json",
        system / "claude-code/managed-settings.d",
        system / "claude-code/managed-mcp.json",
        system / "codex/config.toml",
        system / "codex/requirements.toml",
    )


def sanitized_url(value: str) -> str:
    """Keep authority/path metadata only; never echo malformed input."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "unsupported-url"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
    except ValueError:
        return "unsupported-url"


def file_commitment(path: Path) -> tuple[object, ...]:
    """Private preview input: absence, identity, full-byte preimage; no public receipt."""
    try:
        identity = path.lstat()
    except FileNotFoundError:
        return (str(path), "absent")
    if not stat.S_ISREG(identity.st_mode) or identity.st_size > 1024 * 1024:
        raise ValueError("unsupported settings file")
    return (
        str(path),
        identity.st_dev,
        identity.st_ino,
        identity.st_mode,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def codex_route_layers(layers: list[tuple[str, str]]) -> dict[str, object]:
    """Documented route-key subset; order is user, selected profile, project, flags."""
    result: dict[str, object] = {}
    for scope, source in layers:
        parsed = tomllib.loads(source)
        if "profiles" in parsed:
            raise ValueError("legacy profile layout unsupported")
        for key in ("model_provider", "model_providers", "openai_base_url", "chatgpt_base_url"):
            if key in parsed and not (scope == "project" and key in CODEX_PROJECT_IGNORED):
                result[key] = parsed[key]
    return result


def claude_env_candidates(layers: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Retain all provenance; do not guess shell-vs-settings per-key resolution."""
    candidates = []
    for scope, source in layers:
        values = json.loads(source).get("env", {})
        for key in (*CLAUDE_ROUTE_KEYS, *PROXY_KEYS):
            if key in values:
                value = values[key]
                if not isinstance(value, str):
                    raise ValueError("unsupported route value")
                if "BASE_URL" in key or key.lower() in {"https_proxy", "http_proxy"}:
                    value = sanitized_url(value)
                elif key.lower() == "no_proxy":
                    value = "configured"  # Raw private host lists never enter guidance.
                elif value not in {"0", "1", "true", "false"}:
                    value = "unsupported-selector"
                candidates.append((scope, key, value))
    return candidates


def test_codex_v2_profile_route_precedence_and_ignored_project_keys() -> None:
    user = 'model_provider = "user"\nopenai_base_url = "https://user.invalid/v1"'
    profile = 'model_provider = "profile"\nopenai_base_url = "https://profile.invalid/v1"'
    project = 'model_provider = "project"\nopenai_base_url = "https://project.invalid/v1"'
    flags = 'model_provider = "declared-flag"'
    observed = codex_route_layers([("user", user), ("profile", profile), ("project", project)])
    assert observed["model_provider"] == "profile"
    assert observed["openai_base_url"] == "https://profile.invalid/v1"
    assert codex_route_layers([("user", user), ("flags", flags)])["model_provider"] == (
        "declared-flag"
    )
    with pytest.raises(ValueError, match="legacy profile"):
        codex_route_layers([("user", '[profiles.work]\nmodel_provider = "legacy"')])


def test_claude_keeps_overridden_candidates_without_claiming_effective_environment() -> None:
    layers = [
        (scope, json.dumps({"env": {"ANTHROPIC_BASE_URL": f"https://{scope}.invalid/v1"}}))
        for scope in ("user", "project", "local", "launch-settings", "managed", "setup-env")
    ]
    observed = claude_env_candidates(layers)
    assert len(observed) == 6
    assert [scope for scope, _, _ in observed] == [scope for scope, _ in layers]
    # Managed precedes --settings for ordinary keys, but shell env is per-key;
    # this contract deliberately supplies candidates rather than an inferred URL.
    assert observed[-1] == ("setup-env", "ANTHROPIC_BASE_URL", "https://setup-env.invalid/v1")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "https://alice:synthetic-secret@proxy.invalid:443/v1?token=hidden#secret",
            "https://proxy.invalid:443/v1",
        ),
        ("http://alice:secret@[::1]:8080/proxy?secret", "http://[::1]:8080/proxy"),
        ("socks5://alice:secret@proxy.invalid", "unsupported-url"),
        ("https://proxy.invalid:bad?secret", "unsupported-url"),
        ("https://[bad?secret", "unsupported-url"),
    ],
)
def test_route_redaction(value: str, expected: str) -> None:
    assert sanitized_url(value) == expected


def test_allowlist_omits_credentials_headers_helpers_and_private_no_proxy_values() -> None:
    source = json.dumps(
        {
            "env": {
                "HTTPS_PROXY": "https://alice:secret@proxy.invalid?secret",
                "NO_PROXY": "private.synthetic.invalid",
                "ANTHROPIC_API_KEY": "synthetic-secret",
                "ANTHROPIC_CUSTOM_HEADERS": "X-Secret: synthetic-secret",
                "apiKeyHelper": "touch /never-execute",
            }
        }
    )
    observed = claude_env_candidates([("user", source)])
    assert observed == [
        ("user", "HTTPS_PROXY", "https://proxy.invalid"),
        ("user", "NO_PROXY", "configured"),
    ]
    assert "secret" not in repr(observed)
    assert "private.synthetic.invalid" not in repr(observed)


def test_preview_commitment_stales_on_presence_bytes_and_same_byte_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    absent = file_commitment(path)
    path.write_text("{}\n")
    present = file_commitment(path)
    assert absent != present
    path.write_text('{"env":{}}\n')
    assert present != file_commitment(path)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(path.read_bytes())
    before = file_commitment(path)
    replacement.replace(path)
    assert before != file_commitment(path)
    path.unlink()
    assert file_commitment(path) == absent


def test_symlink_and_directory_are_unsupported_inputs(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    for path in (link, tmp_path):
        with pytest.raises(ValueError, match="unsupported settings file"):
            file_commitment(path)


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_version_and_context_support_is_explicit(client: str) -> None:
    require_supported_context(client, VERSIONS[client])
    with pytest.raises(ValueError, match="unsupported observation context"):
        require_supported_context(client, "unknown")


@pytest.mark.parametrize(
    "context",
    [
        {"surface": "desktop"},
        {"root_is_cwd": False},
        {"worktree": True},
        {"managed_state": "not-observed"},
        {"managed_state": "known-present"},
        {"unresolved_overrides": True},
    ],
)
def test_known_unsupported_context_refuses(context: ContextOverrides) -> None:
    with pytest.raises(ValueError, match="unsupported observation context"):
        require_supported_context("claude", VERSIONS["claude"], **context)


def test_finite_inventory_commits_absent_managed_layers_and_new_presence(tmp_path: Path) -> None:
    paths = input_inventory(tmp_path / "home", tmp_path / "project", tmp_path / "system")
    before = tuple(file_commitment(path) for path in paths)
    managed = tmp_path / "system/claude-code/managed-settings.json"
    managed.parent.mkdir(parents=True)
    managed.write_text("{}")
    after = tuple(file_commitment(path) for path in paths)
    assert before != after
    assert len(before) == len(after) == 12


def test_unreadable_input_propagates_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{}")

    def unreadable(_path: Path) -> bytes:
        raise PermissionError("synthetic unreadable settings")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(PermissionError, match="synthetic unreadable"):
        file_commitment(path)
