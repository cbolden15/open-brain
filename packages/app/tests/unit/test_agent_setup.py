from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import cast

import pytest

import open_brain.services.agent_setup as agent_setup_module
from open_brain.local_data import LocalRootSelection, select_local_root
from open_brain.services.agent_setup import (
    AgentSetupFailure,
    apply_agent_setup,
    preview_agent_setup,
)


def _selection(home: Path) -> LocalRootSelection:
    return select_local_root(
        data_dir=str(home / "brain"),
        environment={"HOME": str(home)},
        platform_name="darwin",
    )


def _runtime(tmp_path: Path) -> Path:
    executable = tmp_path / "runtime/bin/open-brain"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return executable


def _preview(
    selection: LocalRootSelection,
    runtime: Path,
    *,
    client: str,
    scope: str = "project",
    project: Path | None = None,
    capture: bool = True,
    search: bool = True,
    inbox_read: object = False,
    organize: object = False,
    review_read: object = False,
    review_propose: object = False,
    review_decide: object = False,
    action: str = "configure",
    environment: dict[str, object] | None = None,
) -> dict[str, object]:
    return preview_agent_setup(
        selection,
        client=client,
        scope=scope,
        project_dir=None if project is None else str(project),
        allow_capture=capture,
        allow_search=search,
        allow_inbox_read=inbox_read,
        allow_organize=organize,
        allow_review_read=review_read,
        allow_review_propose=review_propose,
        allow_review_decide=review_decide,
        action=action,
        runtime_path=runtime,
        environment=environment or {"HOME": str(selection.home)},
    )


def _apply(
    selection: LocalRootSelection,
    runtime: Path,
    preview: dict[str, object],
    *,
    client: str,
    scope: str = "project",
    project: Path | None = None,
    capture: bool = True,
    search: bool = True,
    inbox_read: object = False,
    organize: object = False,
    review_read: object = False,
    review_propose: object = False,
    review_decide: object = False,
    action: str = "configure",
    environment: dict[str, object] | None = None,
    write_file: agent_setup_module.FileWriter | None = None,
) -> dict[str, object]:
    return apply_agent_setup(
        selection,
        client=client,
        scope=scope,
        project_dir=None if project is None else str(project),
        allow_capture=capture,
        allow_search=search,
        allow_inbox_read=inbox_read,
        allow_organize=organize,
        allow_review_read=review_read,
        allow_review_propose=review_propose,
        allow_review_decide=review_decide,
        action=action,
        preview_id=preview["preview_id"],
        runtime_path=runtime,
        environment=environment or {"HOME": str(selection.home)},
        write_file=write_file,
    )


def test_claude_project_setup_preserves_configuration_and_removes_only_owned_fragments(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    config = project / ".mcp.json"
    instructions = project / "CLAUDE.md"
    config.write_text(
        json.dumps(
            {
                "theme": "synthetic-private-setting",
                "mcpServers": {"other": {"command": "/synthetic/other"}},
            }
        ),
        encoding="utf-8",
    )
    instructions.write_text("# Existing project instructions\n", encoding="utf-8")

    preview = _preview(selection, runtime, client="claude-code", project=project)
    rendered_preview = json.dumps(preview)

    assert preview["status"] == "preview"
    assert "synthetic-private-setting" not in rendered_preview
    assert "/synthetic/other" not in rendered_preview
    assert (
        _apply(selection, runtime, preview, client="claude-code", project=project)["status"]
        == "configured"
    )
    configured = json.loads(config.read_text(encoding="utf-8"))
    assert configured["theme"] == "synthetic-private-setting"
    assert configured["mcpServers"]["other"] == {"command": "/synthetic/other"}
    assert configured["mcpServers"]["open-brain"]["args"] == [
        "mcp",
        "--data-dir",
        str(selection.brain_root),
        "--allow-capture",
        "--allow-search",
    ]
    assert instructions.read_text(encoding="utf-8").startswith("# Existing project instructions\n")

    repeated = _preview(selection, runtime, client="claude-code", project=project)
    assert {change["operation"] for change in cast(list[dict[str, str]], repeated["changes"])} == {
        "keep"
    }
    assert (
        _apply(selection, runtime, repeated, client="claude-code", project=project)["status"]
        == "unchanged"
    )

    configured["later_user_setting"] = {"preserved": True}
    configured["mcpServers"]["later-server"] = {"command": "/synthetic/later"}
    config.write_text(json.dumps(configured), encoding="utf-8")
    with instructions.open("a", encoding="utf-8") as stream:
        stream.write("\nUser note added later.\n")
    removal = _preview(
        selection,
        runtime,
        client="claude-code",
        project=project,
        capture=False,
        search=False,
        action="remove",
    )
    removed = _apply(
        selection,
        runtime,
        removal,
        client="claude-code",
        project=project,
        capture=False,
        search=False,
        action="remove",
    )

    assert removed["status"] == "removed"
    final = json.loads(config.read_text(encoding="utf-8"))
    assert "open-brain" not in final["mcpServers"]
    assert final["mcpServers"]["later-server"] == {"command": "/synthetic/later"}
    assert final["later_user_setting"] == {"preserved": True}
    final_instructions = instructions.read_text(encoding="utf-8")
    assert "open-brain-agent-setup" not in final_instructions
    assert "User note added later." in final_instructions


def test_codex_setup_preserves_toml_comments_and_enforces_selected_capability(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = home / "project"
    config = project / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '# preserve this synthetic comment\nmodel = "synthetic-model"\n',
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    preview = _preview(
        selection,
        runtime,
        client="codex",
        project=project,
        capture=False,
        search=True,
    )

    _apply(
        selection,
        runtime,
        preview,
        client="codex",
        project=project,
        capture=False,
        search=True,
    )

    text = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert text.startswith("# preserve this synthetic comment\n")
    assert parsed["model"] == "synthetic-model"
    assert parsed["mcp_servers"]["open-brain"]["args"][-1] == "--allow-search"
    assert "--allow-capture" not in parsed["mcp_servers"]["open-brain"]["args"]
    instruction_text = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "Search Open Brain" in instruction_text
    assert "use Open Brain capture" not in instruction_text


def test_configure_requires_a_capability_and_remove_does_not(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)

    with pytest.raises(AgentSetupFailure, match="invalid_arguments"):
        _preview(
            selection,
            runtime,
            client="claude-code",
            project=project,
            capture=False,
            search=False,
        )

    removal = _preview(
        selection,
        runtime,
        client="claude-code",
        project=project,
        capture=False,
        search=False,
        action="remove",
    )
    assert {change["operation"] for change in cast(list[dict[str, str]], removal["changes"])} == {
        "keep"
    }


def test_organization_grants_are_explicit_preview_bound_and_documented(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)

    legacy = _preview(selection, runtime, client="claude-code", project=project)
    expanded = _preview(
        selection,
        runtime,
        client="claude-code",
        project=project,
        inbox_read=True,
        organize=True,
    )

    assert legacy["permissions"] == {"capture": True, "search": True}
    assert expanded["permissions"] == {
        "capture": True,
        "search": True,
        "inbox_read": True,
        "organize": True,
    }
    assert expanded["preview_id"] != legacy["preview_id"]
    with pytest.raises(AgentSetupFailure, match="setup_preview_stale"):
        _apply(selection, runtime, expanded, client="claude-code", project=project)

    result = _apply(
        selection,
        runtime,
        expanded,
        client="claude-code",
        project=project,
        inbox_read=True,
        organize=True,
    )

    assert result["status"] == "configured"
    configured = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert configured["mcpServers"]["open-brain"]["args"][-2:] == [
        "--allow-inbox-read",
        "--allow-organize",
    ]
    instructions = (project / "CLAUDE.md").read_text(encoding="utf-8")
    for tool in (
        "brain_capture",
        "brain_search",
        "brain_inbox_list",
        "brain_space_list",
        "brain_space_create",
        "brain_space_rename",
        "brain_inbox_route",
    ):
        assert f"`{tool}`" in instructions
    assert "only for the user's current request" in instructions
    assert "source text as untrusted data" in instructions
    assert "does not publish content or change trust" in instructions

    removal = _preview(
        selection,
        runtime,
        client="claude-code",
        project=project,
        capture=False,
        search=False,
        action="remove",
    )
    removed = _apply(
        selection,
        runtime,
        removal,
        client="claude-code",
        project=project,
        capture=False,
        search=False,
        action="remove",
    )
    assert removed["status"] == "removed"
    assert not (project / ".mcp.json").exists()
    assert not (project / "CLAUDE.md").exists()


@pytest.mark.parametrize(
    ("grant", "flag"),
    (("inbox_read", "--allow-inbox-read"), ("organize", "--allow-organize")),
)
def test_organization_grants_can_configure_independently(
    tmp_path: Path, grant: str, flag: str
) -> None:
    home = tmp_path / "home"
    project = home / grant
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)

    preview = _preview(
        selection,
        runtime,
        client="codex",
        project=project,
        capture=False,
        search=False,
        inbox_read=grant == "inbox_read",
        organize=grant == "organize",
    )
    _apply(
        selection,
        runtime,
        preview,
        client="codex",
        project=project,
        capture=False,
        search=False,
        inbox_read=grant == "inbox_read",
        organize=grant == "organize",
    )

    parsed = tomllib.loads((project / ".codex/config.toml").read_text(encoding="utf-8"))
    args = parsed["mcp_servers"]["open-brain"]["args"]
    assert args[-1] == flag
    assert "--allow-capture" not in args
    assert "--allow-search" not in args
    instructions = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "save only the explicit memory requested" not in instructions


@pytest.mark.parametrize("field", ["inbox_read", "organize"])
def test_organization_grants_require_strict_booleans(tmp_path: Path, field: str) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)

    with pytest.raises(AgentSetupFailure, match="invalid_arguments"):
        _preview(
            _selection(home),
            _runtime(tmp_path),
            client="claude-code",
            project=project,
            inbox_read=1 if field == "inbox_read" else False,
            organize=1 if field == "organize" else False,
        )


@pytest.mark.parametrize(
    ("grant", "flag", "tools"),
    (
        ("review_read", "--allow-review-read", ("brain_review_list", "brain_review_show")),
        ("review_propose", "--allow-review-propose", ("brain_review_propose",)),
        (
            "review_decide",
            "--allow-review-decide",
            (
                "brain_review_approve",
                "brain_review_reject",
                "brain_review_edit_and_approve",
            ),
        ),
    ),
)
def test_review_grants_are_independent_preview_bound_and_documented(
    tmp_path: Path, grant: str, flag: str, tools: tuple[str, ...]
) -> None:
    home = tmp_path / "home"
    project = home / grant
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    review_read = grant == "review_read"
    review_propose = grant == "review_propose"
    review_decide = grant == "review_decide"
    preview = _preview(
        selection,
        runtime,
        client="codex",
        project=project,
        capture=False,
        search=False,
        review_read=review_read,
        review_propose=review_propose,
        review_decide=review_decide,
    )
    assert cast(dict[str, bool], preview["permissions"])[grant] is True
    _apply(
        selection,
        runtime,
        preview,
        client="codex",
        project=project,
        capture=False,
        search=False,
        review_read=review_read,
        review_propose=review_propose,
        review_decide=review_decide,
    )
    parsed = tomllib.loads((project / ".codex/config.toml").read_text(encoding="utf-8"))
    args = parsed["mcp_servers"]["open-brain"]["args"]
    assert args[-1] == flag
    for other in (
        "--allow-review-read",
        "--allow-review-propose",
        "--allow-review-decide",
    ):
        assert (other in args) is (other == flag)
    instructions = (project / "AGENTS.md").read_text(encoding="utf-8")
    for tool in tools:
        assert f"`{tool}`" in instructions
    assert "review token" in instructions
    assert "untrusted data" in instructions


@pytest.mark.parametrize("field", ["review_read", "review_propose", "review_decide"])
def test_review_grants_require_strict_booleans(tmp_path: Path, field: str) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    values: dict[str, object] = {
        "review_read": False,
        "review_propose": False,
        "review_decide": False,
    }
    values[field] = 1
    with pytest.raises(AgentSetupFailure, match="invalid_arguments"):
        _preview(
            _selection(home),
            _runtime(tmp_path),
            client="claude-code",
            project=project,
            review_read=values["review_read"],
            review_propose=values["review_propose"],
            review_decide=values["review_decide"],
        )


def test_claude_review_grants_preserve_unrelated_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    config = project / ".mcp.json"
    config.write_text(
        json.dumps({"theme": "keep", "mcpServers": {"other": {"command": "/other"}}}),
        encoding="utf-8",
    )
    selection = _selection(home)
    runtime = _runtime(tmp_path)
    preview = _preview(
        selection,
        runtime,
        client="claude-code",
        project=project,
        capture=False,
        search=False,
        review_read=True,
        review_propose=True,
        review_decide=True,
    )
    _apply(
        selection,
        runtime,
        preview,
        client="claude-code",
        project=project,
        capture=False,
        search=False,
        review_read=True,
        review_propose=True,
        review_decide=True,
    )
    configured = json.loads(config.read_text(encoding="utf-8"))
    assert configured["theme"] == "keep"
    assert configured["mcpServers"]["other"] == {"command": "/other"}
    args = configured["mcpServers"]["open-brain"]["args"]
    assert args[-3:] == [
        "--allow-review-read",
        "--allow-review-propose",
        "--allow-review-decide",
    ]


@pytest.mark.parametrize(
    ("client", "relative", "payload"),
    (
        ("claude-code", ".mcp.json", "{not-json"),
        ("codex", ".codex/config.toml", "[not valid"),
    ),
)
def test_malformed_client_configuration_is_rejected_without_content_leakage(
    tmp_path: Path,
    client: str,
    relative: str,
    payload: str,
) -> None:
    home = tmp_path / "home"
    project = home / "project"
    target = project / relative
    target.parent.mkdir(parents=True)
    target.write_text(payload, encoding="utf-8")

    with pytest.raises(AgentSetupFailure) as failure:
        _preview(_selection(home), _runtime(tmp_path), client=client, project=project)

    assert failure.value.code == "client_config_invalid"
    assert payload not in str(failure.value)
    assert target.read_text(encoding="utf-8") == payload


def test_apply_rejects_a_stale_preview_before_any_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    instructions = project / "CLAUDE.md"
    instructions.write_text("Initial instructions.\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    preview = _preview(selection, runtime, client="claude-code", project=project)
    instructions.write_text("Changed after preview.\n", encoding="utf-8")

    with pytest.raises(AgentSetupFailure, match="setup_preview_stale"):
        _apply(selection, runtime, preview, client="claude-code", project=project)

    assert not (project / ".mcp.json").exists()
    assert instructions.read_text(encoding="utf-8") == "Changed after preview.\n"


def test_partial_apply_rolls_back_without_persisting_whole_file_backups(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    config = project / ".mcp.json"
    instructions = project / "CLAUDE.md"
    config.write_text('{"unrelated":"preserve"}\n', encoding="utf-8")
    instructions.write_text("Preserve instructions.\n", encoding="utf-8")
    before_config = config.read_bytes()
    before_instructions = instructions.read_bytes()
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    preview = _preview(selection, runtime, client="claude-code", project=project)
    calls = 0

    def fail_second(path: Path, payload: bytes | None) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic write failure")
        agent_setup_module._atomic_apply(path, payload)

    with pytest.raises(AgentSetupFailure, match="operation_failed"):
        _apply(
            selection,
            runtime,
            preview,
            client="claude-code",
            project=project,
            write_file=fail_second,
        )

    assert config.read_bytes() == before_config
    assert instructions.read_bytes() == before_instructions
    assert not tuple(project.rglob("*.tmp"))


def test_user_profile_environment_locations_are_respected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    claude_home = home / "profiles/claude"
    codex_home = home / "profiles/codex"

    claude = _preview(
        selection,
        runtime,
        client="claude-code",
        scope="user",
        environment={"HOME": str(home), "CLAUDE_CONFIG_DIR": str(claude_home)},
    )
    codex = _preview(
        selection,
        runtime,
        client="codex",
        scope="user",
        environment={"HOME": str(home), "CODEX_HOME": str(codex_home)},
    )

    assert (
        _apply(
            selection,
            runtime,
            claude,
            client="claude-code",
            scope="user",
            environment={"HOME": str(home), "CLAUDE_CONFIG_DIR": str(claude_home)},
        )["status"]
        == "configured"
    )
    assert (
        _apply(
            selection,
            runtime,
            codex,
            client="codex",
            scope="user",
            environment={"HOME": str(home), "CODEX_HOME": str(codex_home)},
        )["status"]
        == "configured"
    )

    assert [change["path"] for change in cast(list[dict[str, str]], claude["changes"])] == [
        str(claude_home / ".claude.json"),
        str(claude_home / "CLAUDE.md"),
    ]
    assert [change["path"] for change in cast(list[dict[str, str]], codex["changes"])] == [
        str(codex_home / "config.toml"),
        str(codex_home / "AGENTS.md"),
    ]
    assert (claude_home / ".claude.json").is_file()
    assert (codex_home / "config.toml").is_file()
    assert not selection.brain_root.exists()


def test_unowned_server_name_collision_is_preserved_and_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    config = project / ".mcp.json"
    original = '{"mcpServers":{"open-brain":{"command":"/owner/runtime"}}}\n'
    config.write_text(original, encoding="utf-8")

    with pytest.raises(AgentSetupFailure, match="setup_conflict"):
        _preview(
            _selection(home),
            _runtime(tmp_path),
            client="claude-code",
            project=project,
        )

    assert config.read_text(encoding="utf-8") == original


def test_user_edited_owned_fragment_and_generated_instructions_fail_closed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    configured = _preview(selection, runtime, client="codex", project=project)
    _apply(selection, runtime, configured, client="codex", project=project)
    config = project / ".codex/config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "tool_timeout_sec = 60", "tool_timeout_sec = 61"
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentSetupFailure, match="setup_conflict"):
        _preview(
            selection,
            runtime,
            client="codex",
            project=project,
            capture=False,
            search=False,
            action="remove",
        )

    other_project = home / "generated"
    other_project.mkdir()
    (other_project / "CLAUDE.md").write_text(
        "<!-- GENERATED: DO NOT EDIT -->\n",
        encoding="utf-8",
    )
    with pytest.raises(AgentSetupFailure, match="generated_instructions"):
        _preview(selection, runtime, client="claude-code", project=other_project)


def test_symlinked_config_target_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    outside = home / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (project / ".mcp.json").symlink_to(outside)

    with pytest.raises(AgentSetupFailure, match="unsafe_config_path"):
        _preview(
            _selection(home),
            _runtime(tmp_path),
            client="claude-code",
            project=project,
        )

    assert outside.read_text(encoding="utf-8") == "{}\n"


@pytest.mark.parametrize("client", ["claude-code", "codex"])
@pytest.mark.parametrize("ending", ["", "\n", "\n\n\n", "\r\n\r\n"])
def test_removal_preserves_exact_outside_instruction_bytes(
    tmp_path: Path, client: str, ending: str
) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    instructions = project / ("CLAUDE.md" if client == "claude-code" else "AGENTS.md")
    original = ("# Keep these instructions" + ending).encode()
    instructions.write_bytes(original)
    preview = _preview(selection, runtime, client=client, project=project)
    _apply(selection, runtime, preview, client=client, project=project)
    repeated = _preview(selection, runtime, client=client, project=project)
    assert (
        _apply(selection, runtime, repeated, client=client, project=project)["status"]
        == "unchanged"
    )
    later = b"\nLater unrelated instructions.\n\n"
    instructions.write_bytes(instructions.read_bytes() + later)
    removal = _preview(selection, runtime, client=client, project=project, action="remove")
    _apply(selection, runtime, removal, client=client, project=project, action="remove")
    assert instructions.read_bytes() == original + later


def test_claude_removal_restores_absent_document_and_owned_container(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    config = project / ".mcp.json"
    originals: tuple[dict[str, object] | None, ...] = (None, {"theme": "keep"}, {"mcpServers": {}})
    for original in originals:
        if original is not None:
            config.write_text(json.dumps(original))
        preview = _preview(selection, runtime, client="claude-code", project=project)
        _apply(selection, runtime, preview, client="claude-code", project=project)
        removal = _preview(
            selection, runtime, client="claude-code", project=project, action="remove"
        )
        _apply(selection, runtime, removal, client="claude-code", project=project, action="remove")
        if original is None:
            assert not config.exists()
        else:
            assert json.loads(config.read_text()) == original


def test_special_file_config_is_rejected_without_waiting(tmp_path: Path) -> None:
    import subprocess
    import sys

    fifo = tmp_path / ".mcp.json"
    os.mkfifo(fifo, 0o600)
    code = """
import sys
from pathlib import Path
from open_brain.services.agent_setup import _read_safe, AgentSetupFailure
try:
    _read_safe(Path(sys.argv[1]), maximum_bytes=1024)
except AgentSetupFailure as error:
    assert error.code == 'unsafe_config_path'
else:
    raise AssertionError('special file accepted')
"""
    subprocess.run([sys.executable, "-c", code, str(fifo)], check=True, timeout=5)


def test_atomic_writer_rechecks_preimage_after_preparing_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".mcp.json"
    config.write_bytes(b"original")
    original_fsync = os.fsync
    changed = False

    def concurrent_edit(descriptor: int) -> None:
        nonlocal changed
        original_fsync(descriptor)
        if not changed:
            changed = True
            config.write_bytes(b"concurrent owner edit")

    monkeypatch.setattr(os, "fsync", concurrent_edit)
    with pytest.raises(AgentSetupFailure, match="setup_preview_stale"):
        agent_setup_module._atomic_apply(config, b"replacement", expected=b"original")
    assert config.read_bytes() == b"concurrent owner edit"
    assert not tuple(tmp_path.glob("*.tmp"))


def test_config_only_interruption_can_be_completed_and_removed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    preview = _preview(selection, runtime, client="claude-code", project=project)
    _apply(selection, runtime, preview, client="claude-code", project=project)
    # Model process death after the config rename, before the instruction rename.
    (project / "CLAUDE.md").unlink()
    resumed = _preview(selection, runtime, client="claude-code", project=project)
    assert (
        _apply(selection, runtime, resumed, client="claude-code", project=project)["status"]
        == "configured"
    )
    removal = _preview(selection, runtime, client="claude-code", project=project, action="remove")
    _apply(selection, runtime, removal, client="claude-code", project=project, action="remove")
    assert not (project / ".mcp.json").exists()


def test_oversized_codex_instructions_fail_actionably(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    (project / "AGENTS.md").write_text("x" * (32 * 1024))
    with pytest.raises(AgentSetupFailure, match="instructions_too_large"):
        _preview(_selection(home), _runtime(tmp_path), client="codex", project=project)


@pytest.mark.parametrize(
    "extension",
    [
        "enabled = false\n",
        '[mcp_servers.open-brain.tools.brain_search]\napproval_mode = "prompt"\n',
    ],
)
def test_codex_server_extensions_outside_owned_block_are_not_removed(
    tmp_path: Path, extension: str
) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    runtime = _runtime(tmp_path)
    selection = _selection(home)
    preview = _preview(selection, runtime, client="codex", project=project)
    _apply(selection, runtime, preview, client="codex", project=project)
    config = project / ".codex/config.toml"
    config.write_text(config.read_text() + extension)
    before = config.read_bytes()
    with pytest.raises(AgentSetupFailure, match="setup_conflict"):
        _preview(selection, runtime, client="codex", project=project, action="remove")
    assert config.read_bytes() == before
