"""Finite settings candidates for explicitly declared standalone CLI contexts.

This observes files and the setup process environment, never another client's
actual network route. No client process, helper, include or managed fragment runs.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

VERSIONS = {"claude-code": "2.1.300", "codex": "0.162.1"}
PROXIES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
        "ALL_PROXY",
        "all_proxy",
    }
)
ROUTES = frozenset(
    {
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
        "OPENAI_BASE_URL",
    }
)
ROOT_KEYS = frozenset({"CODEX_HOME", "CLAUDE_CONFIG_DIR"})
_CONTEXT_KEYS = frozenset(
    {"surface", "cwd", "profile", "setting_sources", "settings_file", "overrides", "managed_state"}
)


class SettingsObservationError(ValueError):
    """An unsafe or unsupported bounded input; contains no private values."""


@dataclass(frozen=True)
class SettingsObservation:
    public: dict[str, object]
    commitment: str
    revalidate: Callable[[], None]


def absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SettingsObservationError("unsupported settings context")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SettingsObservationError("unsupported settings context")
    return path


def read_observed(
    path: Path, *, maximum: int = 2 * 1024 * 1024, directory_only: bool = False
) -> tuple[bytes | None, object]:
    """No-follow component traversal, bounded FD read, identity/absence commitment."""
    absolute_path(str(path))
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    file_fd = -1
    chain: list[object] = []
    descriptors = [directory]
    links: list[tuple[int, str, int, os.stat_result]] = []

    def finish(payload: bytes | None, commitment: object) -> tuple[bytes | None, object]:
        for parent, name, child, expected in links:
            current = os.fstat(child)
            if (current.st_dev, current.st_ino, current.st_mode, current.st_uid) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_mode,
                expected.st_uid,
            ):
                raise SettingsObservationError("settings parent changed")
            actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (actual.st_dev, actual.st_ino, actual.st_mode, actual.st_uid) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_mode,
                expected.st_uid,
            ):
                raise SettingsObservationError("settings parent changed")
        return payload, commitment

    try:
        for index, part in enumerate(path.parts[1:-1]):
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
            except FileNotFoundError:
                return finish(None, [str(path), chain, "absent-parent", index])
            metadata = os.fstat(child)
            if (
                metadata.st_uid not in {0, os.geteuid()}
                or metadata.st_mode & 0o022
                and not (metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX)
            ):
                os.close(child)
                raise SettingsObservationError("unsafe settings path")
            chain.append([metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid])
            links.append((directory, part, child, metadata))
            descriptors.append(child)
            directory = child
        try:
            file_fd = os.open(
                path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory
            )
        except FileNotFoundError:
            return finish(None, [str(path), chain, "absent"])
        before = os.fstat(file_fd)
        if directory_only:
            if (
                not stat.S_ISDIR(before.st_mode)
                or before.st_uid not in {0, os.geteuid()}
                or before.st_mode & 0o022
            ):
                raise SettingsObservationError("unsafe project directory")
            return finish(b"", [str(path), chain, before.st_dev, before.st_ino, before.st_mode])
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.geteuid()}
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or before.st_size > maximum
        ):
            raise SettingsObservationError("unsafe settings file")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(file_fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise SettingsObservationError("settings file too large")
            chunks.append(chunk)
        after = os.fstat(file_fd)
        named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)

        def identity(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if identity(before) != identity(after) or identity(after) != identity(named):
            raise SettingsObservationError("settings changed during observation")
        payload = b"".join(chunks)
        return finish(
            payload, [str(path), chain, list(identity(after)), sha256(payload).hexdigest()]
        )
    except OSError:
        raise SettingsObservationError("unsafe settings path") from None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def system_roots(platform_name: str) -> tuple[Path, Path]:
    if platform_name == "darwin":
        return Path("/Library/Application Support/ClaudeCode"), Path("/private/etc/codex")
    if platform_name.startswith("linux"):
        return Path("/etc/claude-code"), Path("/etc/codex")
    raise SettingsObservationError("unsupported settings platform")


def public_authority(value: object) -> str:
    if not isinstance(value, str):
        raise SettingsObservationError("invalid endpoint value")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError
        host = parsed.hostname
        if any(ord(character) < 33 for character in host):
            raise ValueError
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, host + port, "", "", ""))
    except ValueError:
        raise SettingsObservationError("unsupported endpoint") from None


def _object(payload: bytes | None, *, toml: bool) -> dict[str, object]:
    if payload is None:
        return {}
    try:
        if toml:
            return tomllib.loads(payload.decode("utf-8"))

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        value = json.loads(payload, object_pairs_hook=pairs)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except ValueError, UnicodeError:
        raise SettingsObservationError("invalid settings document") from None


def _unscoped(document: Mapping[str, object], *, toml: bool, runtime: Path) -> int:
    servers = document.get("mcp_servers" if toml else "mcpServers", {})
    if not isinstance(servers, dict):
        raise SettingsObservationError("invalid MCP settings")
    count = 0
    for entry in servers.values():
        if not isinstance(entry, dict):
            raise SettingsObservationError("invalid MCP settings")
        command, args = entry.get("command"), entry.get("args", [])
        if not isinstance(command, str) or not isinstance(args, list):
            continue
        tokens = [command, *[arg for arg in args if isinstance(arg, str)]]
        invokes = any(
            Path(token).name in {runtime.name, "open-brain", "open-brain-harness", "open_brain"}
            for token in tokens
        )
        if invokes and "mcp" in tokens:
            scoped = any(
                token == "--session-policy"
                and index + 1 < len(tokens)
                and not tokens[index + 1].startswith("--")
                for index, token in enumerate(tokens)
            )
            scoped |= any(
                token.startswith("--session-policy=") and len(token) > len("--session-policy=")
                for token in tokens
            )
            if not scoped:
                count += 1
    return count


def observe_client_settings(
    *,
    client: str,
    client_version: object,
    project: Path | None,
    home: Path,
    environment: Mapping[str, object],
    launch_context: object,
    platform_name: str,
    runtime: Path,
) -> SettingsObservation:
    if (
        VERSIONS.get(client) != client_version
        or not isinstance(launch_context, dict)
        or set(launch_context) != _CONTEXT_KEYS
    ):
        raise SettingsObservationError("unsupported settings context")
    context = launch_context
    cwd = absolute_path(context["cwd"])
    if (
        context["surface"] != "standalone-cli"
        or project is not None
        and cwd != project
        or context["setting_sources"] != ["user", "project", "local"]
        or context["settings_file"] is not None
        or context["overrides"] != []
        or context["managed_state"] != "owner-declared-absent"
    ):
        raise SettingsObservationError("unsupported settings context")
    directory_payload, directory_commitment = read_observed(cwd, directory_only=True)
    if directory_payload is None:
        raise SettingsObservationError("missing project directory")
    git_entry = cwd / ".git"
    if git_entry.is_symlink() or git_entry.is_file():
        raise SettingsObservationError("worktrees unsupported")
    # A repository ancestor makes a subdirectory launch unsupported. Bounded
    # component traversal only; no home enumeration or Git subprocess.
    if any((parent / ".git").exists() for parent in cwd.parents if parent != cwd):
        raise SettingsObservationError("subdirectory launch unsupported")
    profile = context["profile"]
    if profile is not None and (
        client != "codex"
        or not isinstance(profile, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", profile) is None
    ):
        raise SettingsObservationError("unsupported profile")
    codex = absolute_path(environment.get("CODEX_HOME") or str(home / ".codex"))
    claude = absolute_path(environment.get("CLAUDE_CONFIG_DIR") or str(home / ".claude"))
    claude_mcp = (
        claude / ".claude.json" if environment.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    )
    claude_system, codex_system = system_roots(platform_name)
    files = [
        ("claude-user-settings", claude / "settings.json", False, False),
        ("claude-project-settings", cwd / ".claude/settings.json", False, False),
        ("claude-local-settings", cwd / ".claude/settings.local.json", False, False),
        ("claude-user-mcp", claude_mcp, False, True),
        ("claude-project-mcp", cwd / ".mcp.json", False, True),
        ("codex-user", codex / "config.toml", True, True),
        ("codex-project", cwd / ".codex/config.toml", True, True),
    ]
    if profile is not None:
        files.append(("codex-profile", codex / f"{profile}.config.toml", True, True))
    managed = [
        claude_system / name
        for name in ("managed-settings.json", "managed-settings.d", "managed-mcp.json")
    ]
    managed += [
        codex_system / name for name in ("config.toml", "requirements.toml", "managed_config.toml")
    ]
    commitments: list[object] = []
    snapshots: list[tuple[Path, object]] = []
    for path in managed:
        # Directories (including empty managed-settings.d) fail before enumeration.
        payload, commitment = read_observed(path)
        commitments.append(commitment)
        snapshots.append((path, commitment))
        if payload is not None:
            raise SettingsObservationError("managed settings unsupported")
    candidates: list[dict[str, object]] = []
    coexistence: list[dict[str, object]] = []

    def env_candidates(source: str, values: Mapping[str, object]) -> None:
        for key in sorted(PROXIES | ROUTES):
            if key not in values:
                continue
            value = values[key]
            if not isinstance(value, str) or len(value) > 8192:
                raise SettingsObservationError("invalid route value")
            if key in {
                "ALL_PROXY",
                "all_proxy",
                "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
            } and value not in {None, "", "0", "false"}:
                raise SettingsObservationError("unsupported route override")
            if not isinstance(value, str) or len(value) > 8192:
                raise SettingsObservationError("invalid route value")
            metadata = (
                public_authority(value)
                if "BASE_URL" in key or key.lower() in {"http_proxy", "https_proxy"}
                else "configured"
            )
            candidates.append(
                {"source": source, "key": key, "value": metadata, "status": "observed"}
            )

    for label, path, toml, mcp in files:
        payload, commitment = read_observed(path)
        commitments.append(commitment)
        snapshots.append((path, commitment))
        document = _object(payload, toml=toml)
        if (
            "policyHelper" in document
            or "managedSourcesBehavior" in document
            or "profiles" in document
        ):
            raise SettingsObservationError("unsupported settings layout")
        if mcp:
            count = _unscoped(document, toml=toml, runtime=runtime)
            if label == "claude-user-mcp":
                projects = document.get("projects", {})
                if not isinstance(projects, dict):
                    raise SettingsObservationError("invalid project MCP settings")
                selected = projects.get(str(cwd), {})
                if not isinstance(selected, dict):
                    raise SettingsObservationError("invalid project MCP settings")
                count += _unscoped(selected, toml=False, runtime=runtime)
            if count:
                coexistence.append({"source": label, "count": count})
        if toml:
            if (
                label == "codex-user"
                and document.get("profile") is not None
                and document.get("profile") != profile
            ):
                raise SettingsObservationError("undeclared profile")
            for key in ("model_provider", "openai_base_url", "chatgpt_base_url"):
                if key in document:
                    value = (
                        public_authority(document[key])
                        if key.endswith("base_url")
                        else "configured"
                    )
                    candidates.append(
                        {
                            "source": label,
                            "key": key,
                            "value": value,
                            "status": "observed",
                            "ignored_for_routing": label == "codex-project",
                        }
                    )
            providers = document.get("model_providers", {})
            if not isinstance(providers, dict):
                raise SettingsObservationError("invalid provider table")
            for provider in providers.values():
                if not isinstance(provider, dict):
                    raise SettingsObservationError("invalid provider table")
                if "base_url" in provider:
                    candidates.append(
                        {
                            "source": label,
                            "key": "model_providers.*.base_url",
                            "value": public_authority(provider["base_url"]),
                            "status": "observed",
                            "ignored_for_routing": label == "codex-project",
                        }
                    )
        else:
            values = document.get("env", {})
            if not isinstance(values, dict):
                raise SettingsObservationError("invalid environment settings")
            env_candidates(label, values)
    env_candidates("setup-process-environment", environment)
    private = {
        "files": commitments,
        "cwd_identity": directory_commitment,
        "client": client,
        "version": client_version,
        "declaration": context,
        "environment": {key: environment.get(key) for key in sorted(PROXIES | ROUTES | ROOT_KEYS)},
    }
    declaration_bytes = json.dumps(context, sort_keys=True)
    environment_bytes = json.dumps(private["environment"], sort_keys=True)
    commitment = sha256(
        json.dumps(private, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    def revalidate() -> None:
        if read_observed(cwd, directory_only=True)[1] != directory_commitment:
            raise SettingsObservationError("project directory stale")
        for path, expected in snapshots:
            if read_observed(path)[1] != expected:
                raise SettingsObservationError("settings observation stale")
        current_env = {key: environment.get(key) for key in sorted(PROXIES | ROUTES | ROOT_KEYS)}
        if (
            json.dumps(context, sort_keys=True) != declaration_bytes
            or json.dumps(current_env, sort_keys=True) != environment_bytes
        ):
            raise SettingsObservationError("settings observation stale")

    return SettingsObservation(
        {
            "candidates": candidates,
            "unscoped_entries": coexistence,
            "managed_inputs": "owner-declared-absent",
            "unobserved": [
                "MDM",
                "server-managed",
                "host-policy",
                "future-launch-flags",
                "client-launch-environment",
                "arbitrary-launcher-wrappers",
                "other-project-configurations",
            ],
            "claim": "observed-configuration-candidates-only",
        },
        commitment,
        revalidate,
    )
