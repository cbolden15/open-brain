"""Guided Slack capture through real collector, intake and engine components.

Only the Slack HTTP boundary and the OS credential store are synthetic (see
`source_guide_support`). Each guide command runs through `run_cli`; each
collector operation is a real child process of the installed workspace
collector, so fetch counts are exact across processes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import open_local_engine

import open_brain.services.local_entrypoints as entrypoints
from open_brain.profile import compile_single_user_local
from packages.app.tests.integration.services import source_guide_support as support

DATE_FLOOR = "2025-10-01T00:00:00Z"
HOSTILE = (
    "Approved?\u001b[2J\u001b]52;c;VEVTVA==\u0007‮owner‬\n"
    "Decision: keep the workspace invite-only."
)


class Guide:
    def __init__(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys = capsys
        self.root = tmp_path
        self.brain = tmp_path / "brain"
        open_local_engine(compile_single_user_local(self.brain))
        (tmp_path / "collector").mkdir(mode=0o700)
        self.state = tmp_path / "collector" / "state.json"
        (tmp_path / "guide").mkdir(mode=0o700)
        self.journal = tmp_path / "guide" / "journal.json"
        (tmp_path / "decisions").mkdir(mode=0o700)
        self.fixture = tmp_path / "fixture"
        self.executable = support.write_launcher(tmp_path / "bin", self.fixture)
        self.project = tmp_path / "project"
        self.project.mkdir()
        support.connect_account(self.state)
        self.revision = 0
        self.workflow_id = "guide:synthetic-1"
        self._decisions = 0

    def common(self) -> list[str]:
        return [
            "--data-dir", str(self.brain),
            "--collector-state", str(self.state),
            "--workflow-state", str(self.journal),
            "--collector-executable", str(self.executable),
        ]

    def run(self, *arguments: str) -> tuple[int, dict[str, Any] | None, str]:
        code = entrypoints.run_cli(["source", "guide", *arguments])
        output = self.capsys.readouterr()
        payload = json.loads(output.out) if output.out.startswith("{") else None
        return code, payload, output.out + output.err

    def advance(
        self,
        action: str,
        arguments: dict[str, object] | None = None,
        *,
        confirmation: dict[str, object] | None = None,
        expected: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self._decisions += 1
        path = self.root / "decisions" / f"{self._decisions}.json"
        decision = {
            "schema_version": 1,
            "workflow_id": self.workflow_id,
            "expected_revision": self.revision if expected is None else expected,
            "action": action,
            "confirmation": confirmation,
            "arguments": arguments or {},
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, json.dumps(decision).encode())
        os.close(fd)
        code, payload, _ = self.run(
            "advance", *self.common(), "--decision-file", str(path), "--json"
        )
        assert payload is not None
        if payload["revision"] is not None:
            self.revision = payload["revision"]
        return code, payload

    def confirm(
        self, action: str, arguments: dict[str, object] | None = None
    ) -> tuple[int, dict[str, Any]]:
        code, proposed = self.advance(action, arguments)
        assert (code, proposed["outcome"]) == (3, "needs_confirmation"), proposed
        intent = proposed["intent"]
        return self.advance(
            action,
            arguments,
            confirmation={"intent_sha256": intent["intent_sha256"], "decision": "approve"},
        )

    def preflight_arguments(self) -> dict[str, object]:
        return {
            "client_kind": "claude-code",
            "scope": "project",
            "context_path": str(self.project),
            "consent_state": str(self.root / "consent" / "state.json"),
            "policy_path": str(self.root / "policy" / "policy.json"),
            "runtime_path": str(self.root / "runtime" / "open-brain"),
            "provider_id": "openai",
            "client_version": "2.1.300",
            "launch_context": {
                "surface": "standalone-cli",
                "cwd": str(self.project),
                "profile": None,
                "setting_sources": ["user", "project", "local"],
                "settings_file": None,
                "overrides": [],
                "managed_state": "owner-declared-absent",
            },
        }

    def history(self) -> int:
        return len(support.calls(self.fixture, "conversations.history"))

    def listing(self) -> int:
        return len(support.calls(self.fixture, "conversations.list"))

    def through_choose(self, channel: str = "CGENERAL") -> None:
        assert self.advance("start")[1]["outcome"] == "completed"
        assert self.advance("preflight", self.preflight_arguments())[1]["outcome"] == "completed"
        support.queue(
            self.fixture,
            "conversations.list",
            [support.channel_page([("CGENERAL", "general"), ("COTHER", "general")])] * 2,
        )
        assert self.advance("resources", {"connection_id": support.ACCOUNT, "cursor": None})[1][
            "outcome"
        ] == "completed"
        code, chosen = self.advance("choose", self.choice(channel))
        assert (code, chosen["outcome"]) == (0, "completed"), chosen

    @staticmethod
    def choice(channel: str = "CGENERAL") -> dict[str, object]:
        return {
            "connection_id": support.ACCOUNT,
            "resource_id": f"channel:{channel}",
            "resource_type": "channel",
            "date_floor": DATE_FLOOR,
            "account_alias": None,
        }


def new_guide(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Guide:
    """A synthetic home, initialized Brain, connected account and launcher per test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    return Guide(tmp_path, capsys)


@pytest.fixture
def guide(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Guide:
    return new_guide(tmp_path, capsys, monkeypatch)


def test_one_phase_json_flow_from_start_to_acknowledged_inspection(guide: Guide) -> None:
    code, started = guide.advance("start")
    assert (code, started["outcome"], started["revision"]) == (0, "completed", 1)
    assert oct(os.lstat(guide.journal).st_mode & 0o777) == "0o600"

    assert guide.advance("preflight", guide.preflight_arguments())[1]["outcome"] == "completed"
    code, accounts = guide.advance("accounts")
    assert code == 0
    assert accounts["data"] == {
        "kind": "accounts",
        "rows": [
            {"connection_id": support.ACCOUNT, "display_name": "Synthetic Slack", "alias": None}
        ],
        "has_more": False,
    }

    support.queue(
        guide.fixture,
        "conversations.list",
        [support.channel_page([("CGENERAL", "general"), ("COTHER", "general")])] * 2,
    )
    code, resources = guide.advance("resources", {"connection_id": support.ACCOUNT, "cursor": None})
    assert code == 0 and resources["data"]["has_more"] is False
    assert [row["name"] for row in resources["data"]["rows"]] == ["general", "general"]
    # Duplicate labels: the returned row ID, never the label, is the identity.
    code, chosen = guide.advance("choose", guide.choice("COTHER"))
    assert (code, chosen["outcome"]) == (0, "completed"), chosen
    assert guide.listing() == 2 and guide.history() == 0

    code, proposed = guide.advance("configure")
    assert (code, proposed["outcome"]) == (3, "needs_confirmation")
    intent = proposed["intent"]
    request = json.loads(intent["canonical_request_utf8"])
    assert request["options"] == {"date_floor": DATE_FLOOR, "privacy_tier": "work"}
    assert request["selection"] == {
        "connector_name": "slack",
        "connection_id": support.ACCOUNT,
        "resource_id": "channel:COTHER",
        "resource_type": "channel",
    }
    assert "reset" not in request
    assert intent["request_sha256"] == hashlib.sha256(
        intent["canonical_request_utf8"].encode()
    ).hexdigest()
    code, configured = guide.advance(
        "configure",
        confirmation={"intent_sha256": intent["intent_sha256"], "decision": "approve"},
    )
    assert (code, configured["outcome"]) == (0, "completed"), configured
    source_id = configured["data"]["source_id"]
    assert source_id == request["source_id"]
    assert guide.history() == 0

    # Status now reads the journal and renders a real HealthV1 through run_cli.
    code, status, _ = guide.run("status", *guide.common(), "--json")
    assert code == 0 and status is not None
    report = status["data"]["report"]
    assert report["source"]["source_id"] == source_id
    assert report["source"]["configured_status"] == "disabled"
    assert report["source"]["stored_next_run_epoch"] is None
    assert status["workflow_id"] == guide.workflow_id
    code, _, human = guide.run("status", *guide.common())
    assert code == 0 and "Global consistency: unknown" in human

    support.queue(
        guide.fixture,
        "conversations.history",
        [
            support.history_page([support.message("1760000000.000001", HOSTILE)], "page-2"),
            support.history_page([support.message("1760000001.000001", "second page")]),
        ],
    )
    code, previewed = guide.confirm("preview")
    assert (code, previewed["outcome"]) == (0, "completed"), previewed
    progress = previewed["data"]
    assert progress["kind"] == "progress" and progress["count"] == 1
    assert progress["has_more"] is True
    assert guide.history() == 1

    code, imported = guide.confirm("import", {"preview_id": progress["preview_id"]})
    assert (code, imported["outcome"]) == (0, "completed"), imported
    receipts = imported["data"]["receipt_ids"]
    assert len(receipts) == 1 and imported["data"]["has_more"] is True
    assert "preview" in imported["next_actions"]
    # JSON import never fetches, previews or imports the next page.
    assert guide.history() == 1

    code, inspected = guide.advance("inspect", {"receipt_id": receipts[0]})
    assert (code, inspected["outcome"]) == (0, "completed"), inspected
    inspection = inspected["data"]
    assert inspection["kind"] == "inspection" and inspection["current"] is True
    assert HOSTILE in inspection["body"]
    assert inspection["projection_sha256"] == hashlib.sha256(
        inspection["body"].encode()
    ).hexdigest()

    code, acknowledged = guide.confirm(
        "acknowledge-inspection",
        {"receipt_id": receipts[0], "inspection_sha256": inspection["projection_sha256"]},
    )
    assert (code, acknowledged["outcome"]) == (0, "completed"), acknowledged

    stored = guide.journal.read_bytes()
    assert b"invite-only" not in stored and support.TOKEN.encode() not in stored
    assert b"slack.com" not in stored and b"VEVTVA" not in stored
    journal = json.loads(stored)
    assert journal["retained_receipt_ids"] == receipts
    assert [item["action"] for item in journal["operations"] if item["state"] == "completed"] == [
        "preflight", "accounts", "resources", "choose", "configure", "preview", "import",
        "inspect", "acknowledge-inspection",
    ]
    assert guide.history() == 1


def test_unscoped_client_refuses_before_any_collector_fetch(guide: Guide) -> None:
    (guide.root / "home" / ".claude.json").write_text(
        json.dumps({"mcpServers": {"brain": {"command": "open-brain", "args": ["mcp"]}}})
    )
    guide.advance("start")
    code, refused = guide.advance("preflight", guide.preflight_arguments())
    assert (code, refused["outcome"], refused["code"]) == (78, "refused", "unscoped_client")
    code, configure = guide.advance("configure")
    assert configure["outcome"] == "refused"
    assert guide.listing() == 0 and guide.history() == 0


def test_configure_requires_preflight_and_choice(guide: Guide) -> None:
    guide.advance("start")
    code, refused = guide.advance("configure")
    assert (code, refused["code"]) == (78, "inspection_required")


def test_unseen_or_wrong_resource_refuses_choice(guide: Guide) -> None:
    guide.advance("start")
    guide.advance("preflight", guide.preflight_arguments())
    support.queue(
        guide.fixture, "conversations.list", [support.channel_page([("CGENERAL", "general")])] * 2
    )
    guide.advance("resources", {"connection_id": support.ACCOUNT, "cursor": None})
    code, refused = guide.advance("choose", guide.choice("CMISSING"))
    assert (code, refused["code"]) == (2, "invalid_request")
    code, refused = guide.advance("choose", {**guide.choice(), "date_floor": "2025-10-01"})
    assert (code, refused["code"]) == (2, "invalid_request")
    code, refused = guide.advance(
        "choose", {**guide.choice(), "connection_id": "account:" + "f" * 64}
    )
    assert (code, refused["code"]) == (2, "invalid_request")


def test_inventory_stops_after_five_pages_with_explicit_continuation(guide: Guide) -> None:
    guide.advance("start")
    guide.advance("preflight", guide.preflight_arguments())
    pages = [
        support.channel_page([(f"C{index:04d}", f"name\u009b2J\u202e{index}")],
                             f"cursor-{index + 1}" if index < 6 else "")
        for index in range(7)
    ]
    support.queue(guide.fixture, "conversations.list", pages)
    code, first = guide.advance("resources", {"connection_id": support.ACCOUNT, "cursor": None})
    assert (code, first["outcome"]) == (3, "partial"), first
    assert first["data"]["has_more"] is True and first["data"]["next_cursor"] == "cursor-5"
    assert len(first["data"]["rows"]) == 5 and guide.listing() == 5
    # C1 and bidi controls pass the collector; they stay exact data, escaped only on display.
    assert first["data"]["rows"][0]["name"] == "name\u009b2J\u202e0"
    assert first["next_actions"] == ["resources", "choose"]
    code, rest = guide.advance(
        "resources", {"connection_id": support.ACCOUNT, "cursor": "cursor-5"}
    )
    assert (code, rest["outcome"]) == (0, "completed")
    assert [row["resource_id"] for row in rest["data"]["rows"]] == [
        "channel:C0005", "channel:C0006"
    ]
    support.queue(guide.fixture, "conversations.list", pages[5:])
    code, chosen = guide.advance("choose", guide.choice("C0006"))
    assert (code, chosen["outcome"]) == (0, "completed"), chosen


def test_existing_source_with_other_options_requires_owner_reset(guide: Guide) -> None:
    from dataclasses import asdict

    from open_brain_collector.live_manager import LiveSourceManager
    from open_brain_connectors.runtime.source_registry import SourceResourceSelection

    guide.through_choose()
    LiveSourceManager(guide.state.parent / "live", guide.brain).dispatch(
        "sources.configure",
        {
            "selection": asdict(
                SourceResourceSelection("slack", support.ACCOUNT, "channel:CGENERAL", "channel")
            ),
            "options": {"privacy_tier": "personal", "date_floor": DATE_FLOOR},
        },
    )
    code, refused = guide.confirm("configure")
    assert (code, refused["outcome"], refused["code"]) == (78, "refused", "source_reset_required")
    assert guide.history() == 0
    code, preview = guide.advance("preview")
    assert preview["code"] == "inspection_required"


def test_rate_limit_is_retained_without_retry_loop(guide: Guide) -> None:
    guide.through_choose()
    guide.confirm("configure")
    support.queue(
        guide.fixture, "conversations.history", [{"_status": 429, "ok": False}]
    )
    code, refused = guide.confirm("preview")
    assert (code, refused["code"], refused["retry_after_epoch"]) == (78, "retry_wait", None)
    assert guide.history() == 1
    journal = json.loads(guide.journal.read_bytes())
    assert journal["operations"][-1]["state"] == "refused"


def test_import_requires_the_exact_pending_preview(guide: Guide) -> None:
    guide.through_choose()
    guide.confirm("configure")
    support.queue(
        guide.fixture,
        "conversations.history",
        [support.history_page([support.message("1760000000.000001", "one")])],
    )
    guide.confirm("preview")
    code, refused = guide.advance("import", {"preview_id": "preview:" + "0" * 64})
    assert (code, refused["code"]) == (78, "authority_refused")
    assert refused["next_actions"] == ["preview"]


def test_altered_intent_confirmation_is_refused(guide: Guide) -> None:
    guide.through_choose()
    code, proposed = guide.advance("configure")
    assert code == 3
    code, refused = guide.advance(
        "configure", confirmation={"intent_sha256": "0" * 64, "decision": "approve"}
    )
    assert (code, refused["code"]) == (78, "revision_conflict")
    code, stale = guide.advance("configure", expected=0)
    assert (code, stale["code"]) == (78, "revision_conflict")
    assert proposed["intent"]["intent_sha256"] != "0" * 64


def test_cancel_retains_completed_work_and_stops_future_actions(guide: Guide) -> None:
    guide.through_choose()
    guide.confirm("configure")
    code, cancelled = guide.advance("cancel")
    assert (code, cancelled["outcome"]) == (0, "cancelled")
    code, refused = guide.advance("preview")
    assert refused["outcome"] in {"refused", "cancelled"} and code != 0
    code, status, _ = guide.run("status", *guide.common(), "--json")
    assert code == 0 and status is not None
    assert status["data"]["report"]["source"]["configured_status"] == "disabled"
    assert guide.history() == 0


def test_decision_file_must_be_private_and_closed(guide: Guide) -> None:
    path = guide.root / "decisions" / "loose.json"
    path.write_text(json.dumps({"schema_version": 1}))
    path.chmod(0o644)
    code, payload, _ = guide.run(
        "advance", *guide.common(), "--decision-file", str(path), "--json"
    )
    assert code == 2 and payload is not None and payload["code"] == "invalid_request"
    path.chmod(0o600)
    code, payload, _ = guide.run(
        "advance", *guide.common(), "--decision-file", str(path), "--json"
    )
    assert code == 2 and payload is not None and payload["code"] == "invalid_request"
    code, extra = guide.advance("start", {"unexpected": True})
    assert (code, extra["code"]) == (2, "invalid_request")
    assert not guide.journal.exists()


def test_slack_leaf_requires_a_terminal(guide: Guide) -> None:
    code, payload, _ = guide.run("slack", *guide.common(), "--json")
    assert code == 2
    code, _, output = guide.run("slack", *guide.common())
    assert code == 2 and "terminal" in output
    assert not guide.journal.exists()


def test_status_reports_binding_mismatch_for_another_collector_state(guide: Guide) -> None:
    guide.through_choose()
    guide.confirm("configure")
    other = guide.root / "other"
    other.mkdir(mode=0o700)
    common = guide.common()
    common[3] = str(other / "state.json")
    code, payload, _ = guide.run("status", *common, "--json")
    assert code == 78 and payload is not None and payload["code"] == "binding_mismatch"


def test_receipts_from_journal_only_and_journal_never_holds_inventory(guide: Guide) -> None:
    guide.through_choose()
    code, refused = guide.advance("inspect", {"receipt_id": "custody:" + "1" * 64})
    assert (code, refused["code"]) == (78, "receipt_unresolved")
    stored = guide.journal.read_bytes()
    assert b"general" not in stored


def test_interactive_flow_stops_after_five_pages_with_distinct_import_checkpoints(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins
    import sys

    pages = [
        support.history_page(
            [support.message(f"176000000{index}.000001", HOSTILE if index == 0 else f"m{index}")],
            f"page-{index + 1}",
        )
        for index in range(7)
    ]
    support.queue(guide.fixture, "conversations.history", pages)
    support.queue(
        guide.fixture,
        "conversations.list",
        [support.channel_page([("CGENERAL", "general‮"), ("COTHER", "general")])] * 2,
    )
    arguments = guide.preflight_arguments()
    answers = [
        "start",
        "claude-code", "project", str(guide.project),
        cast(str, arguments["consent_state"]), cast(str, arguments["policy_path"]),
        cast(str, arguments["runtime_path"]), "openai", "2.1.300",
        "1", "2", DATE_FLOOR, "",
        "approve",
    ]
    for page in range(5):
        answers += ["approve", "approve", "approve"]
        if page < 4:
            answers.append("yes")
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr(builtins, "input", ask)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    code, _, output = guide.run("slack", *guide.common())

    assert code == 3, output
    assert answers == []
    assert guide.history() == 5
    assert "5-page limit" in output and "run this command again" in output
    assert "\u001b" not in output and "‮" not in output and "\u0007" not in output
    assert "\\x1b[2J" in output and "\\u202e" in output
    assert "Decision: keep the workspace invite-only." in output
    journal = json.loads(guide.journal.read_bytes())
    imports = [
        item for item in journal["operations"]
        if item["action"] == "import" and item["state"] == "completed"
    ]
    assert len(imports) == 5
    assert len({item["evidence"]["preview_id"] for item in imports}) == 5
    assert len(journal["retained_receipt_ids"]) == 5
    assert all(item["confirmation"] == "confirmed" for item in imports)
    assert sum("Type approve" in prompt for prompt in prompts) == 16
