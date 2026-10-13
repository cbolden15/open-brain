"""Optional user-wide lifecycle integrations with preimage-bound owned edits.

Setup never opens a Brain, reads a transcript, or grants provider access. Policy
and destination configuration must already exist at the explicit owner path.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from .agent_setup import (
    AgentSetupFailure,
    FileWriter,
    _atomic_apply,
    _canonical_json,
    _json_document,
    _Mutation,
    _read_safe,
    _render_json,
    _toml_text,
    resolve_agent_runtime,
)

_LIMIT = 2 * 1024 * 1024
_SERVER = "open-brain-memory"
_HOOK_OWNER = "OPEN_BRAIN_HARNESS_SETUP="
_ENV_OWNER = "OPEN_BRAIN_HARNESS_SETUP"
_BEGIN = "# open-brain-harness-setup begin sha256="
_END = "# open-brain-harness-setup end\n"


def _path(value: object) -> Path:
    if not isinstance(value, str | Path):
        raise AgentSetupFailure("invalid_arguments")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\0" in os.fspath(path):
        raise AgentSetupFailure("invalid_arguments")
    return path


def _action(value: object) -> str:
    if not isinstance(value, str) or value not in {"configure", "remove", "install", "uninstall"}:
        raise AgentSetupFailure("invalid_arguments")
    return {"install": "configure", "uninstall": "remove"}.get(value, value)


def discover_harness_assets(runtime_path: Path) -> Path:
    """Find persistent installed assets, never a frozen extraction directory."""
    installed = runtime_path.parent.parent / "share/open-brain/harness"
    if installed.is_dir():
        return installed
    if not bool(getattr(sys, "frozen", False)):
        return Path(__file__).parent.parent / "assets"
    raise AgentSetupFailure("operation_failed")


def preview_harness_setup(
    *,
    client: object,
    action: object,
    runtime_path: Path,
    config_path: Path,
    environment: Mapping[str, object],
    node_path: Path,
    assets_dir: Path | None = None,
) -> dict[str, object]:
    """Return reviewable owned fragments and a digest of their exact preimages."""
    return _plan(client, action, runtime_path, config_path, environment, node_path, assets_dir)[0]


def apply_harness_setup(
    *,
    client: object,
    action: object,
    runtime_path: Path,
    config_path: Path,
    environment: Mapping[str, object],
    node_path: Path,
    preview_id: object,
    assets_dir: Path | None = None,
    write_file: FileWriter | None = None,
) -> dict[str, object]:
    """Rebuild the preview, check every preimage, and roll back partial failure."""
    preview, mutations = _plan(
        client, action, runtime_path, config_path, environment, node_path, assets_dir
    )
    if not isinstance(preview_id, str) or preview_id != preview["preview_id"]:
        raise AgentSetupFailure("setup_preview_stale")
    if _action(action) == "configure":
        # A freshly installed native runtime can spend seconds validating its
        # libraries. Finish that side-effect-free launch before enabling hooks
        # whose capture and retrieval deadlines are deliberately much shorter.
        try:
            subprocess.run(
                [os.fspath(_harness_runtime(runtime_path)), "harness", "--help"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            raise AgentSetupFailure("operation_failed") from None
        # Warming cannot make a stale preview safe to apply.
        checked, mutations = _plan(
            client, action, runtime_path, config_path, environment, node_path, assets_dir
        )
        if checked["preview_id"] != preview_id:
            raise AgentSetupFailure("setup_preview_stale")
    changed = [item for item in mutations if item.before != item.after]
    applied: list[_Mutation] = []
    try:
        for item in changed:
            if _read_safe(item.path, maximum_bytes=_LIMIT) != item.before:
                raise AgentSetupFailure("setup_preview_stale")
            applied.append(item)
            if write_file is None:
                _atomic_apply(item.path, item.after, expected=item.before)
            else:
                write_file(item.path, item.after)
            if _read_safe(item.path, maximum_bytes=_LIMIT) != item.after:
                raise AgentSetupFailure("operation_failed")
    except BaseException as error:
        rollback_failed = False
        for item in reversed(applied):
            try:
                current = _read_safe(item.path, maximum_bytes=_LIMIT)
                if current == item.after:
                    _atomic_apply(item.path, item.before, expected=item.after)
                elif current != item.before:
                    rollback_failed = True
            except BaseException:
                rollback_failed = True
        if rollback_failed:
            raise AgentSetupFailure("setup_conflict") from None
        if isinstance(error, AgentSetupFailure):
            raise
        raise AgentSetupFailure("operation_failed") from None
    return {
        "status": (
            "unchanged"
            if not changed
            else "configured"
            if _action(action) == "configure"
            else "removed"
        ),
        "client": client,
        "changes": [os.fspath(item.path) for item in changed],
        "notices": preview["notices"],
    }


def _harness_runtime(runtime_path: Path) -> Path:
    base_runtime = resolve_agent_runtime(runtime_path)
    helper = base_runtime.parent.parent / "libexec/open-brain-harness/open-brain-harness"
    return resolve_agent_runtime(helper) if helper.exists() else base_runtime


def _plan(
    client: object,
    action: object,
    runtime_path: Path,
    config_path: Path,
    environment: Mapping[str, object],
    node_path: Path,
    assets_dir: Path | None,
) -> tuple[dict[str, object], list[_Mutation]]:
    if not isinstance(client, str) or client not in {"codex", "claude-code", "pi"}:
        raise AgentSetupFailure("invalid_arguments")
    action = _action(action)
    base_runtime = resolve_agent_runtime(runtime_path)
    runtime = _harness_runtime(base_runtime)
    node = resolve_agent_runtime(node_path)
    config = _path(config_path)
    # The bridge checks owner configuration when invoked; setup only binds its
    # path and preimage and does not inspect policy or initialize canonical data.
    config_before = _read_safe(config, maximum_bytes=_LIMIT)
    if config_before is None and action == "configure":
        raise AgentSetupFailure("invalid_arguments")
    home = _path(environment.get("HOME"))
    assets = _path(assets_dir) if assets_dir is not None else discover_harness_assets(base_runtime)
    names = ("pi-entry.mjs", "pi-memory.mjs") if client == "pi" else ("harness-hook.mjs",)
    asset_digests: dict[str, str | None] = {}
    for name in names:
        asset = assets / name
        try:
            metadata = asset.stat(follow_symlinks=False)
            if (
                asset.resolve(strict=True) != asset
                or not asset.is_file()
                or metadata.st_uid not in {0, os.geteuid()}
                or metadata.st_mode & 0o022
            ):
                raise OSError
            content = asset.read_bytes()
            if not content or len(content) > _LIMIT:
                raise OSError
            asset_digests[name] = sha256(content).hexdigest()
        except OSError:
            if action == "configure":
                raise AgentSetupFailure("operation_failed") from None
            asset_digests[name] = None
    args = ["harness", "mcp", "--config", os.fspath(config), "--client", str(client)]
    mutations: list[_Mutation] = []

    def add(path: Path, kind: str) -> None:
        before = _read_safe(path, maximum_bytes=_LIMIT)
        if kind == "hooks":
            after = _hooks(before, str(action), str(client), node, assets, runtime, config)
        elif kind == "json-mcp":
            after = _mcp_json(before, str(action), runtime, args)
        elif kind == "toml-mcp":
            body = (
                f"[mcp_servers.{_SERVER}]\ncommand = {json.dumps(os.fspath(runtime))}\n"
                f"args = {json.dumps(args)}\nstartup_timeout_sec = 15\ntool_timeout_sec = 5\n"
            )
            after = _text(before, str(action), body, toml=True)
        else:
            body = (
                f"import configure from {json.dumps((assets / 'pi-entry.mjs').as_uri())};\n"
                "export default pi => configure(pi, "
                + json.dumps({"runtime": os.fspath(runtime), "config": os.fspath(config)})
                + ");\n"
            )
            after = _text(before, str(action), body, toml=False)
        # Include only the owned fragment in previews, never unrelated settings.
        fragment = body if kind in {"toml-mcp", "extension"} else _fragment(after, kind)
        mutations.append(
            _Mutation(
                path,
                before,
                after,
                kind,
                "keep" if before == after else "remove" if action == "remove" else "configure",
                fragment,
            )
        )

    notices = ["Restart the harness after setup; grants are controlled by owner policy."]
    if client == "codex":
        root = _path(environment.get("CODEX_HOME", home / ".codex"))
        add(root / "hooks.json", "hooks")
        add(root / "config.toml", "toml-mcp")
        notices.append(
            "Codex requires trust review of non-managed hook definitions before execution."
        )
    elif client == "claude-code":
        root = _path(environment.get("CLAUDE_CONFIG_DIR", home / ".claude"))
        add(root / "settings.json", "hooks")
        add(
            root / ".claude.json" if "CLAUDE_CONFIG_DIR" in environment else home / ".claude.json",
            "json-mcp",
        )
    else:
        root = _path(environment.get("PI_CODING_AGENT_DIR", home / ".pi/agent"))
        add(root / "extensions/open-brain-memory.js", "extension")
        notices.append(
            "Generic Pi denies confidential/unknown reads; "
            "private recall needs a trusted local host."
        )
    changes = [
        {
            "path": os.fspath(item.path),
            "kind": item.kind,
            "operation": item.operation,
            "content": item.content,
        }
        for item in mutations
    ]
    seed = {
        "client": client,
        "action": action,
        "runtime_path": os.fspath(runtime),
        "runtime_digest": sha256(runtime.read_bytes()).hexdigest(),
        "node_digest": sha256(node.read_bytes()).hexdigest(),
        "node_path": os.fspath(node),
        "config_path": os.fspath(config),
        "config_preimage": None if config_before is None else sha256(config_before).hexdigest(),
        "assets_dir": os.fspath(assets),
        "assets": asset_digests,
        "changes": changes,
        "preimages": [
            None if item.before is None else sha256(item.before).hexdigest() for item in mutations
        ],
    }
    return {
        "status": "preview",
        "preview_id": "harness_setup_" + sha256(_canonical_json(seed)).hexdigest(),
        "client": client,
        "action": action,
        "scope": "user",
        "changes": changes,
        "notices": notices,
    }, mutations


def _owned_hook(group: object) -> tuple[str, str] | None:
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return None
    handlers = group["hooks"]
    marked = [
        h
        for h in handlers
        if isinstance(h, dict)
        and isinstance(h.get("command"), str)
        and h["command"].startswith(_HOOK_OWNER)
    ]
    if not marked:
        if any(
            isinstance(handler, dict)
            and isinstance(handler.get("command"), str)
            and "harness-hook.mjs" in handler["command"]
            for handler in handlers
        ):
            # An existing manual producer or a removed ownership marker must
            # not cause duplicate capture writers during installation.
            raise AgentSetupFailure("setup_conflict")
        return None
    if len(handlers) != 1 or set(group) != {"hooks"}:
        raise AgentSetupFailure("setup_conflict")
    handler = dict(marked[0])
    marker, separator, command = handler["command"].partition(" ")
    parts = marker.removeprefix(_HOOK_OWNER).split(":")
    if not separator or len(parts) != 4 or parts[0] != "v1":
        raise AgentSetupFailure("setup_conflict")
    _, origin, event_origin, digest = parts
    handler["command"] = command
    if origin not in {"absent", "no_hooks", "present"} or event_origin not in {"absent", "present"}:
        raise AgentSetupFailure("setup_conflict")
    if (
        digest
        != sha256(
            _canonical_json({"handler": handler, "origin": origin, "event_origin": event_origin})
        ).hexdigest()
    ):
        raise AgentSetupFailure("setup_conflict")
    return origin, event_origin


def _hooks(
    before: bytes | None,
    action: str,
    client: str,
    node: Path,
    assets: Path,
    runtime: Path,
    config: Path,
) -> bytes | None:
    document = _json_document(before)
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        raise AgentSetupFailure("client_config_invalid")
    updated = dict(hooks)
    original_file = (
        "absent" if before is None else "no_hooks" if "hooks" not in document else "present"
    )
    origins: set[str] = set()
    for event, operation, timeout in (("Stop", "capture", 2), ("UserPromptSubmit", "recall", 3)):
        groups = updated.get(event, [])
        if not isinstance(groups, list):
            raise AgentSetupFailure("client_config_invalid")
        owned = []
        for index, group in enumerate(groups):
            value = _owned_hook(group)
            if value is not None:
                owned.append((index, value))
        if len(owned) > 1:
            raise AgentSetupFailure("setup_conflict")
        origin, event_origin = (
            owned[0][1] if owned else (original_file, "present" if event in updated else "absent")
        )
        origins.add(origin)
        remaining = [
            group for index, group in enumerate(groups) if not owned or index != owned[0][0]
        ]
        if action == "configure":
            command = shlex.join(
                [
                    os.fspath(node),
                    os.fspath(assets / "harness-hook.mjs"),
                    "--runtime",
                    os.fspath(runtime),
                    "--config",
                    os.fspath(config),
                    "--client",
                    client,
                    "--event",
                    operation,
                ]
            )
            handler = {"type": "command", "command": command, "timeout": timeout}
            digest = sha256(
                _canonical_json(
                    {"handler": handler, "origin": origin, "event_origin": event_origin}
                )
            ).hexdigest()
            handler["command"] = f"{_HOOK_OWNER}v1:{origin}:{event_origin}:{digest} {command}"
            group = {"hooks": [handler]}
            remaining.insert(owned[0][0] if owned else len(remaining), group)
        if remaining or event_origin == "present":
            updated[event] = remaining
        else:
            updated.pop(event, None)
    if len(origins) > 1:
        raise AgentSetupFailure("setup_conflict")
    result = dict(document)
    if updated or "present" in origins:
        result["hooks"] = updated
    else:
        result.pop("hooks", None)
    after = None if not result and "absent" in origins else _render_json(result)
    return before if result == document else after


def _mcp_json(before: bytes | None, action: str, runtime: Path, args: list[str]) -> bytes | None:
    document = _json_document(before)
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise AgentSetupFailure("client_config_invalid")
    existing = servers.get(_SERVER)
    origin = (
        "absent" if before is None else "no_servers" if "mcpServers" not in document else "present"
    )
    if _SERVER in servers:
        if not isinstance(existing, dict) or not isinstance(existing.get("env"), dict):
            raise AgentSetupFailure("setup_conflict")
        marker = existing["env"].get(_ENV_OWNER, "")
        parts = marker.split(":") if isinstance(marker, str) else []
        core = {key: value for key, value in existing.items() if key != "env"}
        if (
            len(parts) != 3
            or parts[0] != "v1"
            or parts[1] not in {"absent", "no_servers", "present"}
        ):
            raise AgentSetupFailure("setup_conflict")
        origin = parts[1]
        if (
            set(existing["env"]) != {_ENV_OWNER}
            or parts[2] != sha256(_canonical_json({"core": core, "origin": origin})).hexdigest()
        ):
            raise AgentSetupFailure("setup_conflict")
    updated = dict(servers)
    if action == "configure":
        core = {"type": "stdio", "command": os.fspath(runtime), "args": args}
        digest = sha256(_canonical_json({"core": core, "origin": origin})).hexdigest()
        updated[_SERVER] = {**core, "env": {_ENV_OWNER: f"v1:{origin}:{digest}"}}
    else:
        updated.pop(_SERVER, None)
    result = dict(document)
    if updated or origin == "present":
        result["mcpServers"] = updated
    else:
        result.pop("mcpServers", None)
    if result == document:
        return before
    return None if not result and origin == "absent" else _render_json(result)


def _text(before: bytes | None, action: str, body: str, *, toml: bool) -> bytes | None:
    try:
        text = _toml_text(before) if toml else (before or b"").decode()
    except UnicodeDecodeError:
        raise AgentSetupFailure("client_config_invalid") from None
    prefix, suffix = ("", "") if toml else ("// ", "// ")
    begin, end = prefix + _BEGIN, suffix + _END
    # The separator inserted after a preexisting final line belongs to us.
    owned = re.search(
        r"\n?"
        + re.escape(begin)
        + r"([0-9a-f]{64}) origin=(absent|present)\n(.*?)"
        + re.escape(end),
        text,
        re.S,
    )
    if text.count(begin) != int(owned is not None) or text.count(end) != int(owned is not None):
        raise AgentSetupFailure("setup_conflict")
    if owned and sha256(owned[3].encode()).hexdigest() != owned[1]:
        raise AgentSetupFailure("setup_conflict")
    outside = text if owned is None else text[: owned.start()] + text[owned.end() :]
    if toml:
        servers = tomllib.loads(outside).get("mcp_servers", {})
        if not isinstance(servers, dict) or _SERVER in servers:
            raise AgentSetupFailure("setup_conflict")
        if owned:
            actual = tomllib.loads(text).get("mcp_servers", {}).get(_SERVER)
            if actual != tomllib.loads(owned[3])["mcp_servers"][_SERVER]:
                raise AgentSetupFailure("setup_conflict")
    elif outside:
        raise AgentSetupFailure("setup_conflict")
    origin = owned[2] if owned else "absent" if before is None else "present"
    block = "\n" + begin + sha256(body.encode()).hexdigest() + f" origin={origin}\n" + body + end
    if action == "remove":
        after = outside
    elif owned:
        after = text[: owned.start()] + block + text[owned.end() :]
    else:
        after = text + block
    if toml:
        tomllib.loads(after)
    return after.encode() if after or origin == "present" else None


def _fragment(after: bytes | None, kind: str) -> str:
    document = _json_document(after)
    if kind == "json-mcp":
        servers = document.get("mcpServers", {})
        assert isinstance(servers, dict)
        return json.dumps({_SERVER: servers.get(_SERVER)}, indent=2)
    hooks = document.get("hooks", {})
    assert isinstance(hooks, dict)
    return json.dumps(
        {
            event: [group for group in groups if _owned_hook(group)]
            for event, groups in hooks.items()
            if event in {"Stop", "UserPromptSubmit"}
        },
        indent=2,
    )


__all__ = ["preview_harness_setup", "apply_harness_setup", "discover_harness_assets"]
