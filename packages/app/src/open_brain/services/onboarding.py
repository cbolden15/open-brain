"""Foreground first-run guidance over the existing local and integration services."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, TypedDict, cast

from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    CaptureAdmissionError,
    CaptureCustodyReceipt,
    CaptureProtectionPendingError,
    ManagedWorkspaceReceipt,
)
from open_brain_engine.storage.locks import LockBusyError

from open_brain.local_data import FilesystemTypeProbe, LocalDataError, LocalRootSelection
from open_brain.profile import ProfileError
from open_brain.services.agent_setup import (
    AgentSetupFailure,
    apply_agent_setup,
    preview_agent_setup,
    resolve_agent_runtime,
)
from open_brain.services.local_bootstrap import LocalBrainSession, open_local_brain
from open_brain.services.local_operations import capture_text, search_brain
from open_brain.services.local_runtime_session import LocalRuntimeCompatibilityError
from open_brain.services.obsidian_plugin import (
    ObsidianPluginFailure,
    discover_obsidian_plugin_assets,
    install_obsidian_plugin,
    load_obsidian_plugin_bundle,
)

SAMPLE_TEXT = "Open Brain setup example: Copper Orchard meets on Tuesday."
SAMPLE_QUERY = "Open Brain setup example"
SAMPLE_DELIVERY_ID = "delivery.setup.example.v1"


def interactive_terminal() -> bool:
    """Check both terminal surfaces without selecting or creating a Brain."""
    return sys.stdin.isatty() and sys.stdout.isatty()


@dataclass(slots=True)
class SetupIO:
    """Explicit text streams and the CLI's existing terminal sanitizer."""

    input: TextIO
    output: TextIO
    sanitize: Callable[[str], str]

    def say(self, text: str) -> None:
        print(self.sanitize(text), file=self.output, flush=True)

    def ask(self, prompt: str) -> str:
        self.output.write(self.sanitize(prompt) + " ")
        self.output.flush()
        line = self.input.readline(8193)
        if not line:
            raise EOFError
        if len(line) > 8192 or not line.endswith("\n"):
            raise EOFError
        return line.strip()

    def confirm(self, prompt: str) -> bool:
        while True:
            answer = self.ask(prompt).lower()
            if answer in {"", "n", "no"}:
                return False
            if answer in {"y", "yes"}:
                return True
            self.say("Enter yes or no. Press Enter to skip.")

    def choose(self, prompt: str, choices: set[str], default: str) -> str:
        while True:
            answer = self.ask(prompt).lower() or default
            if answer in choices:
                return answer
            self.say("Choose " + ", ".join(sorted(choices)) + ".")


def command_prefix() -> tuple[str, ...]:
    """Keep follow-up commands on the runtime that is running setup."""
    return (
        (sys.executable,) if getattr(sys, "frozen", False) else (sys.executable, "-m", "open_brain")
    )


def headless_hint(data_dir: str | None = None) -> str:
    local_options = () if data_dir is None else ("--data-dir", data_dir)
    return (
        "Use "
        + shlex.join((*command_prefix(), "capture", "Your note", *local_options))
        + "; then "
        + shlex.join((*command_prefix(), "search", "Your note", *local_options))
        + "."
    )


def _command(selection: LocalRootSelection, *arguments: str) -> str:
    return shlex.join((*command_prefix(), *arguments, "--data-dir", str(selection.brain_root)))


def _agent_runtime() -> Path:
    try:
        return resolve_agent_runtime()
    except AgentSetupFailure as error:
        if error.code != "invalid_arguments" or getattr(sys, "frozen", False):
            raise
        # Module entrypoints are not executable. Use the console script from
        # this Python environment, with the same ownership and executable checks.
        return resolve_agent_runtime(str(Path(sys.executable).with_name("open-brain")))


class _AgentOptions(TypedDict):
    client: str
    scope: str
    project_dir: str | None
    allow_capture: bool
    allow_search: bool
    action: str
    runtime_path: Path
    environment: Mapping[str, object]


def _show_client_preview(preview: Mapping[str, object], io: SetupIO) -> None:
    io.say(f"Client: {preview['client']} ({preview['scope']})")
    io.say(f"Runtime: {preview['runtime_path']}")
    io.say(f"Brain: {preview['brain_root']}")
    permissions = cast(Mapping[str, bool], preview["permissions"])
    io.say("Permissions: " + ", ".join(name for name, allowed in permissions.items() if allowed))
    for change in cast(list[dict[str, object]], preview["changes"]):
        io.say(f"{change['operation']} {change['kind']}: {change['path']}")
        io.say(cast(str, change["content"]))
    for notice in cast(list[str], preview["notices"]):
        io.say("Notice: " + notice)


def _configure_client(
    selection: LocalRootSelection, environment: Mapping[str, object], io: SetupIO
) -> str:
    client = io.choose(
        "AI client [skip/claude-code/codex] (default skip):",
        {"skip", "claude-code", "codex"},
        "skip",
    )
    if client == "skip":
        return "skipped"
    scope = io.choose("Scope [project/user] (default project):", {"project", "user"}, "project")
    project = None
    if scope == "project":
        current = str(Path.cwd())
        project = io.ask(f"Project directory [{current}]:") or current
    io.say(
        "Search results may be sent to the AI client's model provider. "
        "Only choose access you want this client to have."
    )
    capture = io.confirm("Allow saving explicit memories? [y/N]")
    search = io.confirm("Allow searching authorized Personal, Work and Public memories? [y/N]")
    if not capture and not search:
        io.say("AI client setup skipped: no permissions selected.")
        return "skipped"
    runtime = _agent_runtime()
    values: _AgentOptions = {
        "client": client,
        "scope": scope,
        "project_dir": project,
        "allow_capture": capture,
        "allow_search": search,
        "action": "configure",
        "runtime_path": runtime,
        "environment": environment,
    }
    preview = preview_agent_setup(selection, **values)
    io.say("Review these Open Brain changes:")
    _show_client_preview(preview, io)
    if not io.confirm("Apply these exact Open Brain changes? [y/N]"):
        io.say("AI client changes skipped.")
        return "skipped"
    applied = apply_agent_setup(selection, preview_id=preview["preview_id"], **values)
    io.say(
        f"AI client {applied['status']}. Restart {client}, then complete its "
        "project trust or MCP approval. Connection has not been tested."
    )
    return str(applied["status"])


def _configure_obsidian(
    selection: LocalRootSelection,
    io: SetupIO,
    setup_workspace: Callable[[LocalBrainSession], ManagedWorkspaceReceipt],
    filesystem_type_probe: FilesystemTypeProbe | None,
) -> str:
    if not io.confirm("Set up the managed Obsidian vault and stage its plugin? [y/N]"):
        return "skipped"
    # Check packaged assets before creating a managed vault.
    bundle = load_obsidian_plugin_bundle(discover_obsidian_plugin_assets())
    with open_local_brain(selection, filesystem_type_probe=filesystem_type_probe) as session:
        setup_workspace(session)
        workspace = session.profile.root.parent / "Open Brain Vault"
        install_obsidian_plugin(workspace, bundle)
    io.say(f"Obsidian plugin staged. Open this directory as a vault: {workspace}")
    io.say(
        "In Obsidian Settings > Community plugins, enable Open Brain. "
        "Vault notes are managed publications; the example remains in capture/search."
    )
    return "staged"


def run_setup(
    selection: LocalRootSelection,
    *,
    environment: Mapping[str, object],
    io: SetupIO,
    setup_workspace: Callable[[LocalBrainSession], ManagedWorkspaceReceipt],
    filesystem_type_probe: FilesystemTypeProbe | None = None,
) -> int:
    """Prove local first value, then offer separately confirmed integrations."""
    began = False
    ready = False
    client = "skipped"
    obsidian = "skipped"
    next_command = _command(selection, "setup")
    try:
        io.say(f"Brain directory: {selection.brain_root}")
        io.say(
            "Capture and search run offline. The example is Personal and local-only. "
            "Your operating-system account protects these files; Open Brain does not encrypt them."
        )
        io.say(f"Example: {SAMPLE_TEXT}")
        if not io.confirm("Save this example and test retrieval? [y/N]"):
            io.say("Setup skipped. No Brain or client files were changed.")
            io.say("Next: " + next_command)
            return 0
        began = True
        with open_local_brain(selection, filesystem_type_probe=filesystem_type_probe) as session:
            receipt = capture_text(
                session.tasks.capture,
                SAMPLE_TEXT,
                delivery_id=SAMPLE_DELIVERY_ID,
                privacy_tier=PrivacyTier.PERSONAL,
            )
            if isinstance(receipt, CaptureCustodyReceipt):
                io.say(
                    "Setup partial: the example is queued; retrieval is not verified. "
                    "Optional integrations were skipped."
                )
                io.say("Next: " + _command(selection, "journal", "status"))
                return 1
            results = search_brain(
                session.tasks.retrieval,
                session.tasks.reconciliation,
                SAMPLE_QUERY,
                limit=100,
            )
            if not any(result.capture_id == receipt.capture_id for result in results):
                io.say(
                    "Setup partial: the example was saved, but search did not return its exact ID. "
                    "Optional integrations were skipped."
                )
                io.say("Next: " + _command(selection, "search", SAMPLE_QUERY))
                return 1
        ready = True
        next_command = _command(selection, "search", SAMPLE_QUERY)
        io.say("Capture and retrieval verified: " + SAMPLE_TEXT)
        try:
            client = _configure_client(selection, environment, io)
        except AgentSetupFailure as error:
            client = "partial"
            io.say(
                f"AI client setup stopped: {error.code}. Existing ownership and preview "
                "checks were preserved. Review "
                + shlex.join((*command_prefix(), "agent", "setup", "--help"))
                + " before retrying."
            )
        except OSError:
            client = "partial"
            io.say(
                "AI client setup stopped: file access failed. Check project and profile "
                "permissions, then preview setup again."
            )
        try:
            obsidian = _configure_obsidian(selection, io, setup_workspace, filesystem_type_probe)
        except ObsidianPluginFailure as error:
            obsidian = "partial"
            io.say(
                f"Obsidian setup stopped: {error.code}. Use an installed Open Brain with "
                "packaged plugin assets and check the managed vault before retrying."
            )
        except OSError, ValueError:
            obsidian = "partial"
            io.say(
                "Obsidian setup stopped: the vault could not be configured safely. "
                "Check its ownership, permissions and directory type before retrying."
            )
        partial = client == "partial" or obsidian == "partial"
        io.say(
            f"Setup {'partial' if partial else 'completed'}. Capture and retrieval verified. "
            f"AI client: {client}. Obsidian: {obsidian}."
        )
        io.say("Next: " + next_command)
        return 1 if partial else 0
    except EOFError, KeyboardInterrupt:
        state = (
            f"Capture and retrieval remain verified. AI client: {client}. Obsidian: {obsidian}."
            if ready
            else "A local operation may have completed; check status before retrying."
            if began
            else "No Brain or client files were changed."
        )
        io.say("Setup interrupted. " + state)
        io.say("Next: " + (next_command if ready or not began else _command(selection, "status")))
        return 130
    except CaptureProtectionPendingError:
        io.say("Setup partial: capture protection is pending; retrieval is not verified.")
        io.say("Next: " + _command(selection, "journal", "status"))
        return 1
    except CaptureAdmissionError as error:
        io.say(
            f"Setup partial: capture was refused ({error.result.value}). "
            "Resolve the admission issue before retrying."
        )
    except LockBusyError:
        io.say("Setup partial: the Brain is busy. Close other clients before retrying.")
    except LocalRuntimeCompatibilityError:
        io.say(
            "Setup partial: another runtime prevents safe admission. Stop older clients "
            "before retrying with this runtime."
        )
    except LocalDataError, ProfileError, OSError:
        io.say(
            "Setup partial: local files could not be opened safely. Check the selected "
            "Brain directory's ownership and permissions before retrying."
        )
    io.say("Next: " + next_command)
    return 78
