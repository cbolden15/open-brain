"""Interactive `source guide slack`: terminal prompts over the shared guide coordinator.

Every prompt produces the same closed decision DTO that `advance` reads from a
file, and every write still needs its own typed approval of the displayed
intent. One invocation covers at most five capture pages, each with its own
import checkpoint, then stops with an explicit continuation. Untrusted names
and bodies only reach the terminal through the single safe renderer.
"""

from __future__ import annotations

import builtins
import json
import sys
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final, cast

from open_brain.services.source_guide_display import (
    GuideDisplayError,
    render_body,
    safe_display,
)
from open_brain.services.source_guide_flow import parse_decision, stop_envelope
from open_brain.services.source_guide_journal import JournalError
from open_brain.services.source_guide_release import ReleaseFlow

if TYPE_CHECKING:
    from open_brain.services.source_guide import GuidePaths

__all__ = ["MAX_INTERACTIVE_PAGES", "run_interactive"]

MAX_INTERACTIVE_PAGES: Final = 5
_BODY_LIMIT: Final = 3_500_000


class _Stopped(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.exit_code = code


class _Session:
    def __init__(
        self, paths: GuidePaths, ask: Callable[[str], str], write: Callable[[str], object]
    ) -> None:
        self.flow = ReleaseFlow(paths)
        self.paths = paths
        self.ask = ask
        self.write = write
        self.workflow_id = ""
        self.revision = 0

    def say(self, text: str) -> None:
        self.write(safe_display(text) + "\n")

    def step(
        self,
        action: str,
        arguments: Mapping[str, object] | None = None,
        confirmation: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        decision = parse_decision(
            {
                "schema_version": 1,
                "workflow_id": self.workflow_id,
                "expected_revision": self.revision,
                "action": action,
                "confirmation": None if confirmation is None else dict(confirmation),
                "arguments": dict(arguments or {}),
            }
        )
        result = self.flow.advance(decision)
        if type(result["revision"]) is int:
            self.revision = result["revision"]
        return result

    def refuse(self, result: Mapping[str, object]) -> None:
        self.say(f"Stopped: {result['action']} returned {result['code']}.")
        actions = cast(list[str], result["next_actions"])
        if actions:
            self.say("Next possible actions: " + ", ".join(actions))
        raise _Stopped(78 if result["outcome"] in {"refused", "unknown_outcome"} else 3)

    def checked(
        self, action: str, arguments: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        result = self.step(action, arguments)
        if result["outcome"] not in {"completed", "partial"}:
            self.refuse(result)
        return result

    def confirmed(
        self, action: str, arguments: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        return self.approve(self.step(action, arguments), action, arguments)

    def approve(
        self,
        proposed: Mapping[str, object],
        action: str,
        arguments: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Show the exact intent and require a typed approval before the write runs."""
        if proposed["outcome"] != "needs_confirmation":
            if proposed["outcome"] not in {"completed", "partial"}:
                self.refuse(proposed)
            return dict(proposed)
        intent = cast(dict[str, object], proposed["intent"])
        self.say("")
        self.say(cast(str, intent["summary"]))
        self.say("Exact request: " + cast(str, intent["canonical_request_utf8"]))
        self.say(
            "Expected versions: "
            + json.dumps(intent["expected_versions"], sort_keys=True, separators=(",", ":"))
        )
        binding = cast(dict[str, object], intent["execution_binding"])
        executable = cast(dict[str, object], binding["executable"])
        self.say(f"Collector: {executable['path']} sha256 {executable['sha256']}")
        self.say(f"Intent: {intent['intent_sha256']}")
        answer = self.ask("Type approve to run this step, anything else to stop: ")
        if answer.strip() != "approve":
            self.say("Nothing was run. The workflow keeps its current progress.")
            raise _Stopped(3)
        result = self.step(
            action,
            arguments,
            {"intent_sha256": intent["intent_sha256"], "decision": "approve"},
        )
        if result["outcome"] != "completed":
            self.refuse(result)
        return result

    def choose_number(self, label: str, count: int, *, more: bool = False) -> int | None:
        suffix = ", m for more" if more else ""
        while True:
            answer = self.ask(f"{label} (1-{count}{suffix}): ").strip()
            if more and answer == "m":
                return None
            if answer.isdigit() and 1 <= int(answer) <= count:
                return int(answer) - 1
            self.say("Choose one of the listed numbers.")


def _completed(document: Mapping[str, object]) -> set[str]:
    return {
        cast(str, item["action"])
        for item in cast(list[dict[str, object]], document["operations"])
        if item["state"] == "completed"
    }


def _preflight(session: _Session) -> None:
    ask = session.ask
    client = ask("Client (claude-code or codex): ").strip()
    scope = ask("Setup scope (project or user): ").strip()
    context = ask("Absolute project or context directory: ").strip()
    values: dict[str, object] = {
        "client_kind": client,
        "scope": scope,
        "context_path": context,
        "consent_state": ask("Absolute provider consent state path: ").strip(),
        "policy_path": ask("Absolute scoped policy path: ").strip(),
        "runtime_path": ask("Absolute open-brain runtime path: ").strip(),
        "provider_id": ask("Provider ID: ").strip(),
        "client_version": ask("Installed client version: ").strip(),
        "launch_context": {
            "surface": "standalone-cli",
            "cwd": context,
            "profile": None,
            "setting_sources": ["user", "project", "local"],
            "settings_file": None,
            "overrides": [],
            "managed_state": "owner-declared-absent",
        },
    }
    session.checked("preflight", values)
    session.say("Client settings checked; no unscoped Open Brain entry was found.")


def _choose(session: _Session) -> None:
    accounts = cast(dict[str, object], session.checked("accounts")["data"])
    rows = cast(list[dict[str, object]], accounts["rows"])
    if not rows:
        session.say("No connected Slack account. Connect one with `open-brain-collector "
                    "sources connect --provider slack` first.")
        raise _Stopped(78)
    for index, row in enumerate(rows, 1):
        session.say(f"{index}. {row['display_name']} [{row['connection_id']}]")
    picked_account = session.choose_number("Account", len(rows))
    assert picked_account is not None
    account = rows[picked_account]
    connection = cast(str, account["connection_id"])
    cursor: str | None = None
    while True:
        listed = session.checked("resources", {"connection_id": connection, "cursor": cursor})
        page = cast(dict[str, object], listed["data"])
        channels = cast(list[dict[str, object]], page["rows"])
        for index, row in enumerate(channels, 1):
            session.say(f"{index}. #{row['name']} [{row['resource_id']}]")
        more = page["has_more"] is True
        picked = session.choose_number("Channel", len(channels), more=more) if channels else None
        if picked is not None:
            channel = channels[picked]
            break
        if not more:
            session.say("No channel was chosen.")
            raise _Stopped(3)
        cursor = cast(str, page["next_cursor"])
    floor = session.ask("Earliest message time, timezone-aware ISO 8601: ").strip()
    alias = session.ask("Optional account alias (display only, blank for none): ").strip()
    session.checked(
        "choose",
        {
            "connection_id": connection,
            "resource_id": channel["resource_id"],
            "resource_type": channel["resource_type"],
            "date_floor": floor,
            "account_alias": alias or None,
        },
    )
    session.say("Privacy is fixed to Work for this guided source.")


def _inspect(session: _Session, receipt_id: str) -> None:
    result = session.checked("inspect", {"receipt_id": receipt_id})
    inspection = cast(dict[str, object], result["data"])
    try:
        body = render_body(cast(str, inspection["body"]), limit_bytes=_BODY_LIMIT)
    except GuideDisplayError:
        session.say("This message is larger than the terminal inspection limit; it stays "
                    "uninspected.")
        raise _Stopped(78) from None
    session.say(f"--- {receipt_id} (revision {inspection['revision_id']}) ---")
    session.write(body + "\n")
    session.say("--- end of message ---")
    session.confirmed(
        "acknowledge-inspection",
        {"receipt_id": receipt_id, "inspection_sha256": inspection["projection_sha256"]},
    )


def _capture(session: _Session) -> int:
    for page in range(1, MAX_INTERACTIVE_PAGES + 1):
        proposed = session.step("preview")
        if proposed["outcome"] == "refused" and proposed["code"] == "inspection_required":
            # A retained pending page is imported, never rediscovered by preview.
            data = cast(dict[str, object] | None, proposed["data"])
            if data is None or data["preview_id"] is None:
                session.refuse(proposed)
            preview = cast(dict[str, object], data)
        else:
            preview = cast(dict[str, object], session.approve(proposed, "preview")["data"])
        session.say(
            f"Page {page}: {preview['count']} records pending; more available: "
            f"{preview['has_more']}."
        )
        imported = session.confirmed("import", {"preview_id": preview["preview_id"]})
        progress = cast(dict[str, object], imported["data"])
        receipts = cast(list[str], progress["receipt_ids"])
        session.say(f"Imported {len(receipts)} records from page {page}.")
        for receipt_id in receipts:
            _inspect(session, receipt_id)
        if progress["has_more"] is not True:
            session.say("The selected window has no further page. Coverage is not exhaustive.")
            return 0
        if page == MAX_INTERACTIVE_PAGES:
            break
        if session.ask("Fetch the next page? Type yes to continue: ").strip() != "yes":
            session.say("Stopped before the next page. Run this command again to continue.")
            return 3
    session.say(
        f"Reached the {MAX_INTERACTIVE_PAGES}-page limit for one invocation. More messages "
        "may exist; run this command again to continue."
    )
    return 3


def run_interactive(
    paths: GuidePaths,
    *,
    ask: Callable[[str], str] | None = None,
    write: Callable[[str], object] | None = None,
) -> int:
    session = _Session(
        paths,
        ask if ask is not None else (lambda prompt: builtins.input(prompt)),
        write if write is not None else sys.stdout.write,
    )
    try:
        document = session.flow.journal.read()
        if document is None:
            session.say(f"No guided workflow exists at {paths.journal}.")
            if session.ask("Type start to create a private workflow journal: ").strip() != "start":
                return 3
            session.workflow_id = "guide:" + uuid.uuid4().hex
            session.checked("start")
            document = session.flow.journal.read()
        assert document is not None
        session.workflow_id = cast(str, document["workflow_id"])
        session.revision = cast(int, document["revision"])
        done = _completed(document)
        if "preflight" not in done:
            _preflight(session)
        if "choose" not in done:
            _choose(session)
        if "configure" not in done:
            session.confirmed("configure")
            session.say("Source configured. Collection stays manual; nothing is scheduled.")
        return _capture(session)
    except _Stopped as stopped:
        return stopped.exit_code
    except JournalError as error:
        session.say(f"Stopped: {error.code}.")
        return 78
    except Exception as error:
        stopped_result = stop_envelope("slack", error)
        if stopped_result is None:
            raise
        session.say(f"Stopped: {stopped_result['code']}.")
        return 2 if stopped_result["code"] == "invalid_request" else 78
