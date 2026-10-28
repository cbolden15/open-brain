from __future__ import annotations

import io
import json
import shlex
import sqlite3
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TextIO, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import CaptureCustodyReceipt

from open_brain.local_data import LocalRootSelection, select_local_root
from open_brain.services import onboarding
from open_brain.services.agent_setup import (
    apply_agent_setup,
    preview_agent_setup,
    resolve_agent_runtime,
)
from open_brain.services.local_bootstrap import LocalBrainSession, open_local_brain
from open_brain.services.local_entrypoints import (
    _setup_managed_workspace,
    _terminal_markdown,
    run_cli,
)
from open_brain.services.local_mcp import LocalMcpAdapter
from open_brain.services.obsidian_plugin import ObsidianPluginFailure
from open_brain.services.onboarding import SAMPLE_DELIVERY_ID, SAMPLE_QUERY, SetupIO


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@dataclass
class Fixture:
    home: Path
    project: Path
    runtime: Path
    selection: LocalRootSelection

    @property
    def environment(self) -> dict[str, object]:
        return {"HOME": str(self.home)}


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    project = home / "project"
    project.mkdir()
    runtime = home / "bin/open-brain"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\nexit 0\n")
    runtime.chmod(0o700)
    selection = select_local_root(
        data_dir=str(home / "brain"), environment={"HOME": str(home)}, platform_name="linux"
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(onboarding, "resolve_agent_runtime", lambda: runtime)
    return Fixture(home, project, runtime, selection)


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _run(fixture: Fixture, answers: str | TextIO) -> tuple[int, str]:
    output = Terminal()
    result = onboarding.run_setup(
        fixture.selection,
        environment=fixture.environment,
        io=SetupIO(
            Terminal(answers) if isinstance(answers, str) else answers, output, _terminal_markdown
        ),
        setup_workspace=_setup_managed_workspace,
        filesystem_type_probe=_filesystem,
    )
    return result, output.getvalue()


def _captures(fixture: Fixture) -> list[tuple[Any, ...]]:
    with sqlite3.connect(fixture.selection.brain_root / ".open-brain/state/phase1.sqlite3") as conn:
        return conn.execute("SELECT capture_id,delivery_id,privacy_json FROM captures").fetchall()


@pytest.mark.parametrize("answers", ["\n", "no\n", ""])
def test_decline_and_eof_do_not_create_brain_or_client_files(
    fixture: Fixture, answers: str
) -> None:
    code, output = _run(fixture, answers)
    assert code == (130 if answers == "" else 0)
    assert "No Brain or client files were changed" in output
    assert not fixture.selection.brain_root.exists()
    assert not (fixture.project / ".mcp.json").exists()
    assert not (fixture.project / ".codex").exists()


def test_ctrl_c_before_confirmation_does_not_mutate(fixture: Fixture) -> None:
    class Interrupted(Terminal):
        def readline(self, size: int | None = -1) -> NoReturn:
            raise KeyboardInterrupt

    code, output = _run(fixture, Interrupted())
    assert code == 130 and "Setup interrupted" in output
    assert not fixture.selection.brain_root.exists()


@pytest.mark.parametrize("input_tty,output_tty", [(False, True), (True, False), (False, False)])
def test_cli_requires_both_ttys_before_selecting_or_creating_root(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    input_tty: bool,
    output_tty: bool,
) -> None:
    incoming = Terminal("yes\n") if input_tty else io.StringIO("yes\n")
    outgoing = Terminal() if output_tty else io.StringIO()
    errors = io.StringIO()
    monkeypatch.setattr(sys, "stdin", incoming)
    monkeypatch.setattr(sys, "stdout", outgoing)
    monkeypatch.setattr(sys, "stderr", errors)
    assert run_cli(("setup",), environment={}) == 2
    assert "requires interactive stdin and stdout" in errors.getvalue()
    assert "-m open_brain capture" in errors.getvalue()
    assert not fixture.selection.brain_root.exists()


def test_json_refusal_preserves_existing_bounded_error_before_bootstrap(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = Terminal()
    monkeypatch.setattr(sys, "stdin", Terminal("yes\n"))
    monkeypatch.setattr(sys, "stdout", output)
    assert run_cli(("setup", "--json"), environment={}) == 2
    assert json.loads(output.getvalue()) == {
        "error": {"code": "invalid_command", "message": "Open Brain could not parse the command."},
        "status": "failed",
    }
    assert not fixture.selection.brain_root.exists()


def test_noninteractive_hint_keeps_the_explicit_brain(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", errors)
    assert run_cli(("setup", "--data-dir", str(fixture.selection.brain_root)), environment={}) == 2
    commands = errors.getvalue().split("Use ", 1)[1].strip().removesuffix(".").split("; then ")
    assert len(commands) == 2
    for command in commands:
        assert shlex.split(command)[-2:] == ["--data-dir", str(fixture.selection.brain_root)]
    assert not fixture.selection.brain_root.exists()


def test_module_invocation_configures_the_same_environment_console_script(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = Path(onboarding.__file__).parents[1] / "__main__.py"
    monkeypatch.setattr(sys, "argv", [str(module)])
    monkeypatch.setattr(sys, "executable", str(fixture.runtime.with_name("python")))
    monkeypatch.setattr(onboarding, "resolve_agent_runtime", resolve_agent_runtime)
    code, output = _run(fixture, "yes\ncodex\n\n\nno\nyes\nyes\n\n")
    assert code == 0, output
    settings = tomllib.loads((fixture.project / ".codex/config.toml").read_text())
    assert settings["mcp_servers"]["open-brain"]["command"] == str(fixture.runtime)
    assert "AI client: configured" in output


def test_noninteractive_hint_sanitizes_terminal_controls(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", errors)
    assert run_cli(("setup", "--data-dir", "/synthetic/\x1b]52;clipboard\x07"), environment={}) == 2
    assert "\x1b" not in errors.getvalue() and "\x07" not in errors.getvalue()
    assert not fixture.selection.brain_root.exists()


def test_bare_command_guides_humans_and_keeps_json_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output, errors = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    assert run_cli((), environment={}) == 2
    assert "open-brain setup" in errors.getvalue() and "open-brain --help" in errors.getvalue()
    errors.seek(0)
    errors.truncate(0)
    assert run_cli(("--json",), environment={}) == 2
    assert json.loads(output.getvalue())["error"]["code"] == "invalid_command"
    assert errors.getvalue() == ""


def test_real_cli_sample_retrieval_defaults_and_repeat_idempotency(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _ in range(2):
        output = Terminal()
        monkeypatch.setattr(sys, "stdin", Terminal("yes\n\n\n"))
        monkeypatch.setattr(sys, "stdout", output)
        assert (
            run_cli(
                ("setup", "--data-dir", str(fixture.selection.brain_root)),
                environment=fixture.environment,
                platform_name="linux",
                filesystem_type_probe=_filesystem,
            )
            == 0
        )
        text = output.getvalue()
        assert "Capture and retrieval verified" in text and "Setup completed" in text
        assert "AI client: skipped. Obsidian: skipped." in text
        assert str(fixture.selection.brain_root) in text
    captures = _captures(fixture)
    assert len(captures) == 1 and captures[0][1] == SAMPLE_DELIVERY_ID
    assert json.loads(captures[0][2])["tier"] == "personal"
    assert not (fixture.home / "Open Brain Vault").exists()
    assert not (fixture.project / ".mcp.json").exists()


def test_queued_custody_is_not_ready_and_skips_integrations(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = CaptureCustodyReceipt(
        ingestion_id="ingestion_00000000-0000-4000-8000-000000000001",
        brain_id="brn_" + "a" * 26,
        issuer_epoch=1,
        delivery_id=SAMPLE_DELIVERY_ID,
        request_sha256="a" * 64,
        requested_tier=PrivacyTier.PERSONAL,
        final_admitted_tier=PrivacyTier.PERSONAL,
        queued_at="2026-10-28T00:00:00Z",
    )
    monkeypatch.setattr(onboarding, "capture_text", lambda *_a, **_k: receipt)

    def no_search(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("queued custody is not searchable")

    monkeypatch.setattr(onboarding, "search_brain", no_search)
    code, output = _run(fixture, "yes\n")
    assert code == 1 and "example is queued" in output
    assert "Capture and retrieval verified" not in output
    assert "AI client [" not in output and "journal status" in output


def test_search_proof_requires_exact_capture_identity(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(onboarding, "search_brain", lambda *_a, **_k: ())
    code, output = _run(fixture, "yes\n")
    assert code == 1 and "search did not return its exact ID" in output
    assert len(_captures(fixture)) == 1
    assert "AI client [" not in output


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_all_grants_default_off_with_no_client_mutation(fixture: Fixture, client: str) -> None:
    code, output = _run(fixture, f"yes\n{client}\n\n\n\n\n\n")
    assert code == 0 and "no permissions selected" in output
    assert "Apply these exact" not in output
    assert not (fixture.project / ".mcp.json").exists()
    assert not (fixture.project / ".codex").exists()
    assert not (fixture.project / "AGENTS.md").exists()


@pytest.mark.parametrize(
    "client,capture,search",
    [
        ("claude-code", "yes", "no"),
        ("codex", "no", "yes"),
        ("codex", "yes", "yes"),
    ],
)
def test_existing_preview_exact_id_and_independent_grants_are_applied_after_session_close(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
    capture: str,
    search: str,
) -> None:
    active = 0
    original_open = open_local_brain

    @contextmanager
    def tracked(*args: Any, **kwargs: Any) -> Iterator[LocalBrainSession]:
        nonlocal active
        with original_open(*args, **kwargs) as session:
            active += 1
            try:
                yield session
            finally:
                active -= 1

    monkeypatch.setattr(onboarding, "open_local_brain", tracked)
    original_preview, original_apply = preview_agent_setup, apply_agent_setup
    preview_ids: list[object] = []

    def preview(*args: Any, **kwargs: Any) -> dict[str, object]:
        assert active == 0
        assert kwargs["allow_capture"] is (capture == "yes")
        assert kwargs["allow_search"] is (search == "yes")
        assert "allow_content_read" not in kwargs  # Service default remains False.
        value = original_preview(*args, **kwargs)
        preview_ids.append(value["preview_id"])
        return value

    def apply(*args: Any, **kwargs: Any) -> dict[str, object]:
        assert active == 0 and kwargs["preview_id"] == preview_ids[-1]
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(onboarding, "preview_agent_setup", preview)
    monkeypatch.setattr(onboarding, "apply_agent_setup", apply)
    code, output = _run(fixture, f"yes\n{client}\n\n\n{capture}\n{search}\nyes\n\n")
    assert code == 0 and "AI client configured" in output
    assert "Connection has not been tested" in output
    path = fixture.project / (".mcp.json" if client == "claude-code" else ".codex/config.toml")
    configured = path.read_text()
    assert ("--allow-capture" in configured) is (capture == "yes")
    assert ("--allow-search" in configured) is (search == "yes")
    assert "--allow-content-read" not in configured and "--allow-organize" not in configured
    assert "model provider" in output
    assert str(path) in output and "Permissions:" in output
    assert "preview_id" not in output
    assert "\\n" not in output


def test_user_scope_uses_selected_synthetic_profile(fixture: Fixture) -> None:
    code, output = _run(fixture, "yes\ncodex\nuser\nno\nyes\nyes\n\n")
    assert code == 0 and "AI client configured" in output
    assert (fixture.home / ".codex/config.toml").is_file()
    assert not (fixture.project / ".codex").exists()


def test_declined_apply_does_not_modify_client(fixture: Fixture) -> None:
    code, output = _run(fixture, "yes\ncodex\n\n\nno\nyes\n\n\n")
    assert code == 0 and "AI client changes skipped" in output
    assert not (fixture.project / ".codex").exists()


def test_stale_preview_is_refused_by_existing_apply_service(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SetupIO.confirm

    def confirm(self: SetupIO, prompt: str) -> bool:
        if prompt.startswith("Apply these exact"):
            (fixture.project / "AGENTS.md").write_text("Synthetic concurrent edit\n")
        return original(self, prompt)

    monkeypatch.setattr(SetupIO, "confirm", confirm)
    code, output = _run(fixture, "yes\ncodex\n\n\nno\nyes\nyes\n\n")
    assert code == 1 and "setup_preview_stale" in output
    assert (fixture.project / "AGENTS.md").read_text() == "Synthetic concurrent edit\n"
    assert not (fixture.project / ".codex").exists()
    assert "Capture and retrieval verified" in output and "Setup partial" in output


def _assets(directory: Path) -> Path:
    directory.mkdir()
    (directory / "main.js").write_text("module.exports = {};\n")
    (directory / "styles.css").write_text(".open-brain {}\n")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": "open-brain",
                "isDesktopOnly": True,
                "minAppVersion": "1.12.7",
                "name": "Open Brain",
                "version": "0.1.0",
            }
        )
    )
    return directory


def test_obsidian_reuses_workspace_setup_and_stages_real_owned_assets(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(fixture.home / "assets")
    monkeypatch.setattr(onboarding, "discover_obsidian_plugin_assets", lambda: assets)
    code, output = _run(fixture, "yes\n\nyes\n")
    vault = fixture.home / "Open Brain Vault"
    assert code == 0 and "Obsidian plugin staged" in output
    assert str(vault) in output and "Community plugins" in output
    assert (vault / ".obsidian/plugins/open-brain/main.js").read_text() == "module.exports = {};\n"
    assert not (vault / ".obsidian/community-plugins.json").exists()


def test_obsidian_workspace_symlink_is_refused_without_touching_target(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(fixture.home / "assets")
    monkeypatch.setattr(onboarding, "discover_obsidian_plugin_assets", lambda: assets)
    target = fixture.home / "foreign-vault"
    target.mkdir()
    (fixture.home / "Open Brain Vault").symlink_to(target, target_is_directory=True)
    code, output = _run(fixture, "yes\n\nyes\n")
    assert code == 1 and "vault could not be configured safely" in output
    assert list(target.iterdir()) == []


def test_optional_asset_failure_preserves_working_local_setup(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing() -> Path:
        raise ObsidianPluginFailure("assets_unavailable")

    monkeypatch.setattr(onboarding, "discover_obsidian_plugin_assets", missing)
    code, output = _run(fixture, "yes\n\nyes\n")
    assert (
        code == 1 and "assets_unavailable" in output and "Capture and retrieval verified" in output
    )
    assert not (fixture.home / "Open Brain Vault").exists()


def test_profile_permission_error_is_actionable_without_configured_claim(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args: Any, **kwargs: Any) -> dict[str, object]:
        raise PermissionError

    monkeypatch.setattr(onboarding, "preview_agent_setup", denied)
    code, output = _run(fixture, "yes\ncodex\n\n\nno\nyes\n\n")
    assert code == 1 and "permissions" in output and "AI client: partial" in output
    assert "configuration configured" not in output


def test_generated_instructions_refusal_remains_visible(
    fixture: Fixture,
) -> None:
    (fixture.project / "AGENTS.md").write_text("<!-- GENERATED: DO NOT EDIT -->\n")
    code, output = _run(fixture, "yes\ncodex\n\n\nno\nyes\n\n")
    assert code == 1 and "generated_instructions" in output
    assert not (fixture.project / ".codex").exists()


def test_next_commands_use_source_python_and_native_executable(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "executable", "/synthetic/source venv/bin/python")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    source = shlex.split(onboarding._command(fixture.selection, "search", SAMPLE_QUERY))
    assert source[:4] == ["/synthetic/source venv/bin/python", "-m", "open_brain", "search"]
    assert source[-2:] == ["--data-dir", str(fixture.selection.brain_root)]
    monkeypatch.setattr(sys, "executable", "/synthetic/native/open-brain")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert shlex.split(onboarding._command(fixture.selection, "setup"))[:2] == [
        "/synthetic/native/open-brain",
        "setup",
    ]


def test_terminal_controls_in_preview_are_not_executed(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = preview_agent_setup

    def preview(*args: Any, **kwargs: Any) -> dict[str, object]:
        value = original(*args, **kwargs)
        value["notices"] = ["synthetic\x1b[2J\u202ehostile"]
        return value

    monkeypatch.setattr(onboarding, "preview_agent_setup", preview)
    code, output = _run(fixture, "yes\ncodex\n\n\nno\nyes\nno\n\n")
    assert code == 0 and "\x1b" not in output and "\u202e" not in output
    assert "synthetic [2J hostile" in output


def test_production_mcp_personal_capture_is_searchable_in_a_new_session(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[LocalMcpAdapter, dict[str, object]]] = []

    def serve(adapter: LocalMcpAdapter, **_kwargs: object) -> None:
        if not observed:
            result = adapter.call_tool(
                "brain_capture",
                {
                    "text": "Synthetic cross session Copper Orchard memory",
                    "idempotency_key": "synthetic.setup.cross-session",
                },
            )
        else:
            result = adapter.call_tool(
                "brain_search_page",
                {
                    "dto_version": 1,
                    "query": "cross session Copper Orchard",
                },
            )
        observed.append((adapter, result))

    monkeypatch.setattr("open_brain.services.mcp_protocol.serve_stdio_mcp", serve)
    arguments = (
        "mcp",
        "--data-dir",
        str(fixture.selection.brain_root),
        "--allow-capture",
        "--allow-search",
    )
    for _ in range(2):
        assert (
            run_cli(
                arguments,
                environment=fixture.environment,
                platform_name="linux",
                filesystem_type_probe=_filesystem,
            )
            == 0
        )
    assert observed[0][0].authority.session_id != observed[1][0].authority.session_id
    assert not observed[0][0].authority.owner
    captured = observed[0][1]
    assert captured["status"] == "captured"
    rows = cast(list[dict[str, object]], observed[1][1]["results"])
    assert [row["record_id"] for row in rows] == [captured["capture_id"]]
    assert json.loads(_captures(fixture)[0][2])["tier"] == "personal"


def test_eof_after_local_proof_preserves_success_and_stops_optional_work(fixture: Fixture) -> None:
    code, output = _run(fixture, "yes\n")
    assert code == 130 and "Setup interrupted" in output
    assert "Capture and retrieval remain verified" in output
    assert len(_captures(fixture)) == 1
    assert not (fixture.project / ".codex").exists()
    assert output.splitlines()[-1].startswith("Next: ")


def test_interruption_after_client_apply_reports_configured_state(fixture: Fixture) -> None:
    code, output = _run(fixture, "yes\ncodex\n\n\nno\nyes\nyes\n")
    assert code == 130 and "Setup interrupted" in output
    assert "AI client: configured. Obsidian: skipped." in output
    assert (fixture.project / ".codex/config.toml").is_file()


def test_initial_permission_failure_is_actionable_and_does_not_offer_integrations(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError

    monkeypatch.setattr(onboarding, "open_local_brain", denied)
    code, output = _run(fixture, "yes\n")
    assert code == 78 and "ownership and permissions" in output
    assert "AI client [" not in output
    assert not fixture.selection.brain_root.exists()
