"""Evaluation acceptance for the daily work brief.

Drives the owner CLI and the production `open-brain mcp --session-policy` composition against
one real local Brain whose old captures were recorded under an injected clock. Each test maps to
one sentence of the 2026-10-14 evaluation acceptance for section 5, plus scope and storage.
Synthetic text only; no product code.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.followups import FollowupTasks
from open_brain_engine.engine.paging import read_snapshot

from open_brain.profile import compile_single_user_local
from open_brain.services.brief_contracts import (
    BRIEF_GRANT_ACK,
    BRIEF_GRANT_READ,
    BRIEF_MAX_FAMILY_ITEMS,
    BRIEF_MAX_ITEMS,
    BriefItemKind,
)
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS
from open_brain.services.local_operations import current_brain_identity
from open_brain.services.mcp_protocol import McpCallError
from packages.app.tests.unit.engine.test_followups import (
    Clock,
    SyntheticSource,
    _projected_text,
    _revise_source,
)

# Every "old" capture is recorded here; the CLI runs under the real clock, which is later.
RECORDED = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

RANK_ORDER = (
    BriefItemKind.FOLLOWUP_DUE,
    BriefItemKind.SOURCE_UNAVAILABLE,
    BriefItemKind.FOLLOWUP_NEW_EVIDENCE,
    BriefItemKind.FOLLOWUP_OPEN,
    BriefItemKind.SOURCE_STALE,
)
FAMILY_KINDS = {BriefItemKind.SOURCE_UNAVAILABLE, BriefItemKind.SOURCE_STALE}
FOLLOWUP_KINDS = set(BriefItemKind) - FAMILY_KINDS
BRIEF_TOOLS = {"brain_brief": BRIEF_GRANT_READ, "brain_brief_ack": BRIEF_GRANT_ACK}
BRIEF_FLAGS = {
    "brief-read": "--allow-brief-read",
    "brief-ack": "--allow-brief-ack",
    "followup-read": "--allow-followup-read",
}

# Synthetic texts. The due quote states its own (past) date.
DUE_TEXT = "Synthetic standup: I will send the synthetic forecast to Dana by 2026-09-15."
DUE_PROMISE = "I will send the synthetic forecast to Dana by 2026-09-15"
OPEN_TEXT = "Synthetic recap: I will draft the synthetic vendor brief after the review."
OPEN_PROMISE = "I will draft the synthetic vendor brief after the review"
QUESTION_TEXT = "Synthetic thread: who owns the synthetic renewal checklist?"
QUESTION = "who owns the synthetic renewal checklist?"
DONE_TEXT = "Synthetic note: the synthetic renewal checklist now has an owner."
DONE = "the synthetic renewal checklist now has an owner"
RESOLVED_TEXT = "Synthetic email: I will file the synthetic expense report."
RESOLVED_PROMISE = "I will file the synthetic expense report"
PERSONAL_TEXT = "Personal note: I will book the synthetic dentist visit."
PERSONAL_PROMISE = "I will book the synthetic dentist visit"
WORK_SOURCE_TEXT = "Synthetic agent session: Work notes from the synthetic coding agent."
PERSONAL_SOURCE_TEXT = "Synthetic agent session: Personal notes from the synthetic journal agent."


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Capture:
    capture_id: str
    text: str

    def quote(self, phrase: str, role: str) -> dict[str, object]:
        """Exact-quote evidence reference as the owner CLI and an MCP client must build it."""
        start = self.text.index(phrase)
        end = start + len(phrase)
        return {
            "capture_id": self.capture_id,
            "revision_id": self.capture_id,
            "quote_start": start,
            "quote_end": end,
            "quote_sha256": _sha256(self.text[start:end]),
            "role": role,
        }


@dataclass
class Brain:
    root: Path
    engine: BrainEngine
    clock: Clock
    capsys: pytest.CaptureFixture[str]
    monkeypatch: pytest.MonkeyPatch
    captures: dict[str, Capture]
    vault: Path
    sessions: int = 0
    items: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- owner CLI -------------------------------------------------------------------------

    def cli(self, *arguments: str, expect: int = 0) -> dict[str, Any]:
        """Owner CLI with --json; unwraps `data` when the command prints the outcome wire."""
        status = run_cli((*arguments, "--data-dir", str(self.root), "--json"))
        output = self.capsys.readouterr()
        assert status == expect, (arguments, output.out, output.err)
        payload = cast(dict[str, Any], json.loads(output.out))
        if expect != 0 or "outcome" not in payload:
            return payload
        assert payload["outcome"] == "ok", payload
        return cast(dict[str, Any], payload["data"])

    def brief(self, *arguments: str) -> dict[str, Any]:
        return self.cli("brief", *arguments)

    def propose(
        self,
        capture: Capture,
        phrase: str,
        *,
        summary: str,
        operation: str,
        kind: str = "commitment",
        owner: str = "self",
        due: str | None = None,
    ) -> dict[str, Any]:
        arguments = [
            "followup",
            "propose",
            "--kind",
            kind,
            "--summary",
            summary,
            "--owner",
            owner,
            "--evidence",
            json.dumps(capture.quote(phrase, "origin")),
            "--operation-id",
            operation,
        ]
        if due is not None:
            arguments += ["--due", due, "--due-evidence-index", "0"]
        return cast(dict[str, Any], self.cli(*arguments)["item"])

    def decide(
        self, item: dict[str, Any], action: str, *extra: str, operation: str
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.cli(
                "followup",
                "decide",
                item["item_id"],
                "--action",
                action,
                "--expected-version",
                str(item["version"]),
                "--operation-id",
                operation,
                *extra,
            )["item"],
        )

    def add_evidence(
        self, item_id: str, capture: Capture, phrase: str, role: str, *, operation: str
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.cli(
                "followup",
                "add-evidence",
                item_id,
                "--evidence",
                json.dumps(capture.quote(phrase, role)),
                "--operation-id",
                operation,
            )["item"],
        )

    def show(self, item_id: str) -> dict[str, Any]:
        return self.cli("followup", "show", item_id)

    # -- MCP ---------------------------------------------------------------------------------

    def mcp(
        self,
        exercise: Callable[[Any], None],
        *,
        grants: tuple[str, ...],
        read_tiers: tuple[str, ...] = ("work",),
    ) -> None:
        """Run `exercise(adapter)` inside the real `open-brain mcp --session-policy` composition."""
        from open_brain.services import mcp_protocol

        errors: list[BaseException] = []
        self.sessions += 1
        authority_root = self.root.parent / f"brief-authority-{self.sessions}"
        authority_root.mkdir(mode=0o700)
        policy_path = authority_root / "session.json"
        brain_id, issuer_epoch = current_brain_identity(self.engine.tasks)
        policy_path.write_text(
            json.dumps(
                {
                    "policy_version": "launcher-policy.v1",
                    "principal_id": "synthetic-brief-agent",
                    "session_id": f"synthetic-brief-session-{self.sessions}",
                    "capabilities": list(grants),
                    "space_ids": None,
                    "allowed_read_tiers": list(read_tiers),
                    "allowed_capture_tiers": [],
                    "egress_mode": "owner_local",
                    "provider_id": None,
                    "consent_id": None,
                    "authorization_generation": 0,
                    "brain_id": brain_id,
                    "issuer_epoch": issuer_epoch,
                }
            ),
            encoding="utf-8",
        )

        def serve(adapter: Any, **_transport: Any) -> None:
            try:
                assert adapter.authority.owner is False
                assert set(adapter.authority.capabilities) == set(grants)
                exercise(adapter)
            except BaseException as error:  # noqa: BLE001 - re-raised after cleanup
                errors.append(error)

        self.monkeypatch.setattr(mcp_protocol, "serve_stdio_mcp", serve)
        flags = tuple(BRIEF_FLAGS[grant] for grant in grants)
        status = run_cli(
            ("mcp", *flags, "--session-policy", str(policy_path), "--data-dir", str(self.root))
        )
        self.capsys.readouterr()
        if errors:
            raise errors[0]
        assert status == 0

    # -- state snapshots -----------------------------------------------------------------------

    def table_counts(self) -> dict[str, int]:
        """Row count of every table in the state database; a brief read must not change any."""
        with read_snapshot(self.engine) as connection:
            names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            return {
                name: int(connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
                for name in names
            }

    def files(self) -> set[str]:
        """Every file under the Brain root except the brief log, which a brief may write."""
        briefs = self.root / ".open-brain" / "state" / "briefs"
        return {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and briefs not in path.parents
        }

    def log_files(self) -> list[Path]:
        return sorted((self.root / ".open-brain" / "state" / "briefs").glob("*"))


def _ok(result: dict[str, object]) -> dict[str, Any]:
    assert result.get("outcome") == "ok", result
    return cast(dict[str, Any], result["data"])


def _kinds(brief: dict[str, Any]) -> list[str]:
    return [cast(str, item["kind"]) for item in brief["items"]]


def _refs(brief: dict[str, Any]) -> set[str]:
    return {cast(str, item["ref"]) for item in brief["items"]}


def _by_kind(brief: dict[str, Any], kind: BriefItemKind) -> list[dict[str, Any]]:
    return [cast(dict[str, Any], item) for item in brief["items"] if item["kind"] == kind]


def _assert_well_formed(brief: dict[str, Any]) -> None:
    """Shape rules every brief must keep: five items, ranks 1..n, fixed kind order, reasons."""
    items = cast(list[dict[str, Any]], brief["items"])
    assert len(items) <= BRIEF_MAX_ITEMS
    assert [item["rank"] for item in items] == list(range(1, len(items) + 1))
    levels = [RANK_ORDER.index(BriefItemKind(item["kind"])) for item in items]
    assert levels == sorted(levels), _kinds(brief)
    assert sum(1 for item in items if item["kind"] in FAMILY_KINDS) <= BRIEF_MAX_FAMILY_ITEMS
    for item in items:
        assert item["reasons"], item
        assert isinstance(item["reason_text"], str) and item["reason_text"].strip(), item
        assert item["changed"] in {"new", "still_open", "changed"}, item
        prefix = "family:" if item["kind"] in FAMILY_KINDS else "followup:"
        assert cast(str, item["ref"]).startswith(prefix), item
        if item["kind"] in FAMILY_KINDS:
            assert item["evidence"] == []
        assert "withheld" not in json.dumps(item)
    assert brief["nothing_needs_attention"] is (len(items) == 0)
    assert brief["model"] == "none"
    assert brief["coverage"]["collector"] == "not_read"
    assert set(brief["coverage"]["log"]) == {"entries", "bytes", "unavailable"}


def _consumer_family(engine: BrainEngine, text: str, *, tier: PrivacyTier) -> SyntheticSource:
    """One consumer-namespace family per tier: each source gets its own connection_id, because
    `_new_source` shares one connection and its sources would collapse into a single family."""
    source = SyntheticSource(
        namespace={
            "connector_name": "synthetic",
            "connection_id": f"brief-acceptance-{tier.value}",
            "resource_id": f"brief-acceptance-{tier.value}-resource",
            "external_id": "one",
        },
        head="",
        revisions=0,
    )
    _revise_source(engine, source, text, tier=tier)
    return source


@pytest.fixture
def brain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Brain:
    root = tmp_path / "brain"
    clock = Clock(RECORDED)
    engine = BrainEngine.open(compile_single_user_local(root, starter_spaces=()), clock=clock)
    captures: dict[str, Capture] = {}
    texts = (
        ("due", DUE_TEXT, PrivacyTier.WORK),
        ("open", OPEN_TEXT, PrivacyTier.WORK),
        ("question", QUESTION_TEXT, PrivacyTier.WORK),
        ("done", DONE_TEXT, PrivacyTier.WORK),
        ("resolved", RESOLVED_TEXT, PrivacyTier.WORK),
        ("personal", PERSONAL_TEXT, PrivacyTier.PERSONAL),
    )
    for name, text, tier in texts:
        receipt = engine.capture.accept(
            TextPayload(text), delivery_id=f"delivery.synthetic-brief-{name}", privacy_tier=tier
        )
        # Quotes must be built from the projected revision text, exactly as a client reads it.
        assert _projected_text(engine, receipt.capture_id) == text
        captures[name] = Capture(receipt.capture_id, text)

    # Two consumer-namespace families recorded under the injected clock, one day apart, so both
    # are stale at the default seven days and "longest silence first" has one winner.
    _consumer_family(engine, PERSONAL_SOURCE_TEXT, tier=PrivacyTier.PERSONAL)
    clock.now = RECORDED + timedelta(days=1)
    _consumer_family(engine, WORK_SOURCE_TEXT, tier=PrivacyTier.WORK)

    # One Markdown import family, recorded by the CLI under the real clock (fresh). One note
    # disappears before the second scan, so the family holds one `missing` capture.
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "kept.md").write_text("# Kept\nsynthetic brief note that stays\n", encoding="utf-8")
    (vault / "gone.md").write_text("# Gone\nsynthetic brief note that goes\n", encoding="utf-8")
    brain = Brain(root, engine, clock, capsys, monkeypatch, captures, vault)
    imported = brain.cli("import", str(vault), "--yes", "--privacy-tier", "work")
    assert imported["status"] == "completed"
    (vault / "gone.md").unlink()
    imported = brain.cli("import", str(vault), "--yes", "--privacy-tier", "work")
    assert imported["status"] == "completed"
    return brain


def _seed_followups(brain: Brain) -> dict[str, dict[str, Any]]:
    """Follow-ups in due, open, dismissed-with-new-evidence, resolved and Personal-open states."""
    c = brain.captures
    due = brain.propose(
        c["due"],
        DUE_PROMISE,
        summary="Send the synthetic forecast to Dana",
        due="2026-09-15",
        operation="op-brief-due",
    )
    open_work = brain.propose(
        c["open"],
        OPEN_PROMISE,
        summary="Draft the synthetic vendor brief",
        operation="op-brief-open",
    )
    question = brain.propose(
        c["question"],
        QUESTION,
        kind="question",
        owner="unknown",
        summary="Who owns the synthetic renewal checklist?",
        operation="op-brief-question",
    )
    dismissed = brain.decide(question, "dismiss", operation="op-brief-dismiss")
    assert dismissed["state"] == "dismissed"
    with_evidence = brain.add_evidence(
        question["item_id"], c["done"], DONE, "supporting", operation="op-brief-new-evidence"
    )
    assert with_evidence["new_evidence_since_decision"] is True
    resolved_open = brain.propose(
        c["resolved"],
        RESOLVED_PROMISE,
        summary="File the synthetic expense report",
        operation="op-brief-resolved",
    )
    resolved = brain.decide(
        resolved_open, "resolve", "--resolution", "owner_statement", operation="op-brief-resolve"
    )
    assert resolved["state"] == "resolved"
    personal = brain.propose(
        c["personal"],
        PERSONAL_PROMISE,
        summary="Book the synthetic dentist visit",
        operation="op-brief-personal",
    )
    assert personal["privacy_tier"] == "personal"
    brain.items = {
        "due": due,
        "open": open_work,
        "new_evidence": with_evidence,
        "resolved": resolved,
        "personal": personal,
    }
    return brain.items


def _ref(item: dict[str, Any]) -> str:
    return f"followup:{item['item_id']}"


def test_every_item_is_inspectable_and_actionable_through_existing_operations(
    brain: Brain,
) -> None:
    items = _seed_followups(brain)
    first = brain.brief()
    _assert_well_formed(first)
    assert first["since"] is None
    assert first["saved"] is True
    assert first["coverage"]["followups"] == "read"
    assert first["coverage"]["freshness"] == "read"
    assert first["coverage"]["families_checked"] == 3
    assert first["coverage"]["families_stale"] == 2
    assert first["coverage"]["captures_unavailable"] == 1
    assert first["coverage"]["hidden_dropped"] == 0
    assert all(item["changed"] == "new" for item in first["items"])
    # Owner view: due, the unavailable Markdown family, the question with new evidence, then the
    # two open items oldest first. The two stale families lose to open work on the item cap.
    assert _kinds(first) == [
        "followup_due",
        "source_unavailable",
        "followup_new_evidence",
        "followup_open",
        "followup_open",
    ]
    assert _ref(items["due"]) in _refs(first)
    assert _ref(items["new_evidence"]) in _refs(first)
    assert [item["ref"] for item in _by_kind(first, BriefItemKind.FOLLOWUP_OPEN)] == [
        _ref(items["open"]),
        _ref(items["personal"]),
    ]
    assert _ref(items["resolved"]) not in _refs(first)

    # Every follow-up item is a pointer into the existing store: `followup show` reads it and
    # the brief's evidence IDs are rows of that item. No quote text travels in the brief.
    for item in first["items"]:
        if item["kind"] in FAMILY_KINDS:
            continue
        shown = brain.show(cast(str, item["ref"]).removeprefix("followup:"))
        evidence_ids = {row["evidence_id"] for row in shown["evidence"]}
        assert set(item["evidence"]) <= evidence_ids, (item, evidence_ids)
        assert item["evidence"], item
        for quote in (DUE_PROMISE, OPEN_PROMISE, QUESTION, DONE, PERSONAL_PROMISE):
            assert quote not in json.dumps(item)
    # The due item names its reason in both code and prose.
    (due_item,) = _by_kind(first, BriefItemKind.FOLLOWUP_DUE)
    assert "overdue" in due_item["reasons"]
    assert "2026-09-15" in due_item["reason_text"]

    # Act through existing operations: resolve the due item with `followup decide`, acknowledge
    # the unavailable family with `brief ack`.
    due_now = brain.show(items["due"]["item_id"])["item"]
    assert (
        brain.decide(
            due_now, "resolve", "--resolution", "owner_statement", operation="op-brief-resolve-due"
        )["state"]
        == "resolved"
    )
    (family_item,) = _by_kind(first, BriefItemKind.SOURCE_UNAVAILABLE)
    assert "captures_unavailable" in family_item["reasons"]
    brain.cli("brief", "ack", cast(str, family_item["ref"]))

    second = brain.brief()
    _assert_well_formed(second)
    assert second["since"] == first["generated_at"]
    assert _ref(items["due"]) not in _refs(second)
    assert family_item["ref"] not in _refs(second)
    # The freed slots go to the next ranked candidates: both stale families now fit under the
    # two-family cap, longest silence first, and both are new to this visibility.
    assert _kinds(second) == [
        "followup_new_evidence",
        "followup_open",
        "followup_open",
        "source_stale",
        "source_stale",
    ], _kinds(second)
    for item in second["items"]:
        expected = "new" if item["kind"] == "source_stale" else "still_open"
        assert item["changed"] == expected, item
    for stale in _by_kind(second, BriefItemKind.SOURCE_STALE):
        assert "no_capture_since" in stale["reasons"]
        assert "2026-09-0" in stale["reason_text"], stale
    # Both ack and resolve were ordinary writes with their own history; nothing brief-only.
    history = [row["action"] for row in brain.show(items["due"]["item_id"])["decisions"]]
    assert history == ["propose", "resolve"]


def test_resolved_item_does_not_reappear_without_new_evidence(brain: Brain) -> None:
    items = _seed_followups(brain)
    resolved_ref = _ref(items["resolved"])
    for _ in range(2):
        brief = brain.brief()
        _assert_well_formed(brief)
        assert resolved_ref not in _refs(brief)

    # New evidence on the resolved item brings it back, once, as `followup_new_evidence`.
    returned = brain.add_evidence(
        items["resolved"]["item_id"],
        brain.captures["done"],
        DONE,
        "supporting",
        operation="op-brief-resolved-evidence",
    )
    assert returned["state"] == "resolved"
    assert returned["new_evidence_since_decision"] is True
    brief = brain.brief()
    _assert_well_formed(brief)
    (item,) = [row for row in brief["items"] if row["ref"] == resolved_ref]
    assert item["kind"] == "followup_new_evidence"
    assert item["changed"] == "new"
    assert "new_evidence" in item["reasons"]

    # Handling it through the existing state machine (reopen, then resolve again) removes it,
    # and it does not come back on the next request.
    reopened = brain.decide(returned, "reopen", operation="op-brief-reopen")
    assert reopened["new_evidence_since_decision"] is False
    resolved_again = brain.decide(
        reopened, "resolve", "--resolution", "owner_statement", operation="op-brief-resolve-again"
    )
    assert resolved_again["state"] == "resolved"
    for _ in range(2):
        brief = brain.brief()
        assert resolved_ref not in _refs(brief)
    actions = [row["action"] for row in brain.show(items["resolved"]["item_id"])["decisions"]]
    assert actions == ["propose", "resolve", "add_evidence", "reopen", "resolve"]


def test_last_dated_brief_stays_readable_when_followup_reads_fail(brain: Brain) -> None:
    items = _seed_followups(brain)
    first = brain.brief()
    _assert_well_formed(first)
    assert first["coverage"]["followups"] == "read"

    def outage(self: FollowupTasks, *args: object, **kwargs: object) -> object:
        raise RuntimeError("synthetic follow-up store outage")

    brain.monkeypatch.setattr(FollowupTasks, "list", outage)

    # The provider failure is reported, the other provider still appears, and the brief is saved.
    degraded = brain.brief()
    _assert_well_formed(degraded)
    assert degraded["coverage"]["followups"] == "failed"
    assert degraded["coverage"]["freshness"] == "read"
    assert degraded["saved"] is True
    assert degraded["since"] == first["generated_at"]
    assert set(_kinds(degraded)) <= {"source_unavailable", "source_stale"}
    assert len(degraded["items"]) == BRIEF_MAX_FAMILY_ITEMS
    assert not _refs(degraded) & {_ref(item) for item in items.values()}

    # The owner's last dated brief stays readable through `brief last` while the outage lasts,
    # and the source records behind the earlier follow-up items stay readable too.
    last = brain.cli("brief", "last")
    assert last["generated_at"] == degraded["generated_at"]
    assert _refs(last) == _refs(degraded)
    assert last["since"] == first["generated_at"]
    # `coverage` is the logged snapshot (the log as the degraded brief found it); `log` is the
    # log as it stands now, both briefs included.
    assert last["coverage"]["followups"] == "failed"
    assert last["log"]["entries"] == 2
    for name in ("due", "open", "question"):
        capture = brain.captures[name]
        projected = brain.cli(
            "read", capture.capture_id, "--expected-revision-id", capture.capture_id
        )
        assert projected["content"]["text"] == capture.text
    assert brain.show(items["due"]["item_id"])["item"]["item_id"] == items["due"]["item_id"]

    brain.monkeypatch.undo()
    recovered = brain.brief()
    assert recovered["coverage"]["followups"] == "read"
    assert _ref(items["due"]) in _refs(recovered)
    assert recovered["since"] == degraded["generated_at"]


def test_reading_a_brief_publishes_nothing_and_sends_nothing(brain: Brain) -> None:
    _seed_followups(brain)
    root = Path(__file__).resolve().parents[5]
    sources = (
        root / "packages/app/src/open_brain/services/brief.py",
        root / "packages/app/src/open_brain/services/brief_log.py",
        root / "packages/app/src/open_brain/services/brief_contracts.py",
        root / "packages/app/src/open_brain/services/brief_authority.py",
        root / "packages/engine/src/open_brain_engine/engine/source_freshness.py",
    )
    forbidden = (
        "urllib",
        "http.client",
        "httpx",
        "requests",
        "socket",
        "smtplib",
        "subprocess",
        "webbrowser",
        "threading",
        "asyncio",
        "sched",
        "managed_providers",
        "provider_credentials",
    )
    for path in sources:
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        assert not [
            name
            for name in imported
            for token in forbidden
            if name == token or name.startswith(token + ".") or token in name.split(".")
        ], path.name

    registered = {
        cast(str, tool["name"]): tuple(cast(list[str], tool["required_grants"]))
        for tool in MCP_REGISTERED_TOOLS
        if cast(str, tool["name"]).startswith("brain_brief")
    }
    assert registered == {name: (grant,) for name, grant in BRIEF_TOOLS.items()}
    assert not [
        name
        for name in (cast(str, tool["name"]) for tool in MCP_REGISTERED_TOOLS)
        if re.search(r"send|notify|schedule|publish|summar", name)
    ]

    # No socket may open while a brief is read, through the CLI or through MCP.
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a brief read opened a network connection")

    brain.monkeypatch.setattr(socket.socket, "connect", refuse)
    brain.monkeypatch.setattr(socket, "create_connection", refuse)

    tables_before = brain.table_counts()
    files_before = brain.files()
    owner_brief = brain.brief()
    _assert_well_formed(owner_brief)
    assert owner_brief["model"] == "none"
    assert owner_brief["items"]

    def read_brief(adapter: Any) -> None:
        tools = {tool["name"]: tool for tool in adapter.list_tools()}
        assert set(tools) == {"brain_catalog", "brain_brief", "brain_brief_ack"}
        for name in ("brain_brief", "brain_brief_ack"):
            schema = tools[name]["inputSchema"]
            assert schema["additionalProperties"] is False
            assert not set(schema["properties"]) & {
                "body",
                "channel",
                "message",
                "recipient",
                "send",
                "summary",
                "model",
                "schedule",
            }
            assert "untrusted" in tools[name]["description"]
        assert set(tools["brain_brief"]["inputSchema"]["properties"]) == {"space_ids", "stale_days"}
        assert set(tools["brain_brief_ack"]["inputSchema"]["properties"]) == {"family_ref"}
        brief = _ok(adapter.call_tool("brain_brief", {}))
        _assert_well_formed(brief)
        assert brief["model"] == "none"
        assert brief["coverage"]["followups"] == "not_granted"

    brain.mcp(read_brief, grants=("brief-read", "brief-ack"))

    # Reading produced no note, no revision, no decision row, no page: every table count is
    # unchanged and the only new files are the per-key brief log and the empty write lock its
    # atomic replace keeps in the state directory it is rooted at.
    assert brain.table_counts() == tables_before
    lock = ".open-brain/state/.write.lock"
    assert brain.files() - files_before <= {lock}
    assert files_before <= brain.files()
    assert (brain.root / lock).stat().st_size == 0
    assert brain.log_files()
    assert all(path.suffix == ".json" for path in brain.log_files())
    # And the log holds pointers only: no quote text, summary or title.
    for path in brain.log_files():
        text = path.read_text(encoding="utf-8")
        for quote in (
            DUE_PROMISE,
            OPEN_PROMISE,
            QUESTION,
            DONE,
            PERSONAL_PROMISE,
            "Send the synthetic forecast",
            "Draft the synthetic vendor brief",
        ):
            assert quote not in text, path.name


def test_storage_growth_is_visible_in_coverage_log(brain: Brain) -> None:
    _seed_followups(brain)
    logs: list[dict[str, Any]] = []
    for _ in range(3):
        brief = brain.brief()
        _assert_well_formed(brief)
        assert brief["saved"] is True
        logs.append(brief["coverage"]["log"])
    assert all(log["unavailable"] is False for log in logs)
    entries = [log["entries"] for log in logs]
    size = [log["bytes"] for log in logs]
    assert entries == sorted(entries) and len(set(entries)) == 3, entries
    assert size == sorted(size) and len(set(size)) == 3, size
    (key_file,) = brain.log_files()
    assert key_file.suffix == ".json"
    assert re.fullmatch(r"[0-9a-f]{64}\.json", key_file.name), key_file.name
    assert (key_file.stat().st_mode & 0o077) == 0, oct(key_file.stat().st_mode)

    # A corrupt log is set aside, never deleted; the brief still generates, says so, and only
    # that key's history is reset.
    key_file.write_text("{ not json", encoding="utf-8")
    reset = brain.brief()
    _assert_well_formed(reset)
    assert reset["since"] is None
    assert reset["coverage"]["log"]["unavailable"] is True
    names = {path.name for path in brain.log_files()}
    assert names == {key_file.name, key_file.name + ".aside"}, names
    aside = key_file.parent / (key_file.name + ".aside")
    assert aside.read_text(encoding="utf-8") == "{ not json"
    after = brain.brief()
    assert after["coverage"]["log"]["unavailable"] is False
    assert after["since"] == reset["generated_at"]
    assert after["coverage"]["log"]["bytes"] < size[-1]


def test_work_only_mcp_session_scope(brain: Brain) -> None:
    items = _seed_followups(brain)
    personal_ref = _ref(items["personal"])
    seen: dict[str, dict[str, Any]] = {}

    # brief-read alone: family items only, follow-ups reported as not granted, Work tier only.
    def read_only(adapter: Any) -> None:
        brief = _ok(adapter.call_tool("brain_brief", {}))
        _assert_well_formed(brief)
        assert brief["coverage"]["followups"] == "not_granted"
        assert brief["coverage"]["followups_scanned"] == 0
        assert set(_kinds(brief)) <= {"source_unavailable", "source_stale"}
        assert brief["scope"]["tiers"] == ["work"]
        assert brief["scope"]["space_ids"] == "all"
        # Work sees one Work consumer family and the Work Markdown family, never the Personal one.
        assert brief["coverage"]["families_checked"] == 2
        assert brief["coverage"]["families_stale"] == 1
        with pytest.raises(McpCallError, match="^unknown tool$"):
            adapter.call_tool("brain_brief_ack", {"family_ref": brief["items"][0]["ref"]})
        seen["read_only"] = brief

    brain.mcp(read_only, grants=("brief-read",))

    # brief-read + followup-read: one Work item per kind in rank order; the Personal follow-up
    # and the Personal family never appear; nothing is withheld, nothing hidden is counted.
    def with_followups(adapter: Any) -> None:
        brief = _ok(adapter.call_tool("brain_brief", {}))
        _assert_well_formed(brief)
        assert brief["coverage"]["followups"] == "read"
        assert _kinds(brief) == [
            "followup_due",
            "source_unavailable",
            "followup_new_evidence",
            "followup_open",
            "source_stale",
        ]
        assert personal_ref not in _refs(brief)
        assert _ref(items["resolved"]) not in _refs(brief)
        assert _ref(items["open"]) in _refs(brief)
        assert brief["coverage"]["hidden_dropped"] == 0
        assert brief["since"] is None  # a different visibility key from the read-only session
        seen["work"] = brief

    brain.mcp(with_followups, grants=("brief-read", "followup-read"))

    # Acks are per visibility key: this session's ack hides the family here, not for the owner.
    def ack_family(adapter: Any) -> None:
        brief = _ok(adapter.call_tool("brain_brief", {}))
        (family,) = _by_kind(brief, BriefItemKind.SOURCE_UNAVAILABLE)
        _ok(adapter.call_tool("brain_brief_ack", {"family_ref": family["ref"]}))
        again = _ok(adapter.call_tool("brain_brief", {}))
        assert family["ref"] not in _refs(again)
        assert again["since"] == brief["generated_at"]
        seen["acked_family"] = family

    brain.mcp(ack_family, grants=("brief-read", "brief-ack", "followup-read"))
    owner = brain.brief()
    assert seen["acked_family"]["ref"] in _refs(owner)
    assert owner["since"] is None  # the owner key has never been written in this test

    # A Personal-allowed launcher policy is the only way a session sees the Personal item.
    def personal_allowed(adapter: Any) -> None:
        brief = _ok(adapter.call_tool("brain_brief", {}))
        assert personal_ref in _refs(brief)
        assert sorted(brief["scope"]["tiers"]) == ["personal", "work"]

    brain.mcp(
        personal_allowed, grants=("brief-read", "followup-read"), read_tiers=("work", "personal")
    )

    # "Nothing needs attention" is a real answer: resolve the Work items and ack both Work
    # families, then the Work view is empty instead of padded.
    for name in ("due", "open"):
        current = brain.show(items[name]["item_id"])["item"]
        brain.decide(
            current,
            "resolve",
            "--resolution",
            "owner_statement",
            operation=f"op-brief-clear-{name}",
        )
    current = brain.show(items["new_evidence"]["item_id"])["item"]
    brain.decide(current, "reopen", operation="op-brief-clear-question")
    current = brain.show(items["new_evidence"]["item_id"])["item"]
    brain.decide(
        current, "resolve", "--resolution", "owner_statement", operation="op-brief-clear-question-2"
    )

    def nothing(adapter: Any) -> None:
        brief = _ok(adapter.call_tool("brain_brief", {}))
        for item in list(brief["items"]):
            assert item["kind"] in FAMILY_KINDS, item
            _ok(adapter.call_tool("brain_brief_ack", {"family_ref": item["ref"]}))
        empty = _ok(adapter.call_tool("brain_brief", {}))
        _assert_well_formed(empty)
        assert empty["items"] == []
        assert empty["nothing_needs_attention"] is True
        assert empty["coverage"]["followups"] == "read"
        assert empty["coverage"]["families_checked"] == 2

    brain.mcp(nothing, grants=("brief-read", "brief-ack", "followup-read"))


def test_owner_space_selection_filters_followups_by_evidence_capture_space(
    brain: Brain,
) -> None:
    items = _seed_followups(brain)
    created = brain.cli("space", "create", "Synthetic Brief", "--idempotency-key", "brief-space")
    space_id = cast(str, created["space"]["space_id"])
    # Route only the open item's origin capture into the space; its follow-up is the one item
    # whose evidence head capture lies in the selection.
    brain.cli(
        "inbox",
        "route",
        brain.captures["open"].capture_id,
        space_id,
        "--idempotency-key",
        "brief-route-open",
    )
    for selector in ("Synthetic Brief", space_id):
        selected = brain.brief("--space", selector)
        _assert_well_formed(selected)
        assert selected["scope"]["space_ids"] == [space_id]
        assert _refs(selected) == {_ref(items["open"])}, _kinds(selected)
        # Every family here is unassigned, so no family belongs to the selection.
        assert selected["coverage"]["families_checked"] == 0
    # The unselected owner brief still sees every follow-up and family.
    everything = brain.brief()
    assert everything["scope"]["space_ids"] == "all"
    assert _ref(items["due"]) in _refs(everything)
    assert everything["coverage"]["families_checked"] == 3
    # A selection holding a space that does not exist, or the `unassigned` literal, is refused.
    for selector in ("no-such-synthetic-space", "unassigned"):
        failure = brain.cli("brief", "--space", selector, expect=2)
        assert failure["error"]["code"] == "invalid_request", failure


def test_stale_days_and_unknown_space_are_validated_not_guessed(brain: Brain) -> None:
    _seed_followups(brain)
    # Out-of-range or non-integer windows are usage failures, refused before bootstrap.
    for value in ("0", "366", "seven"):
        failure = brain.cli("brief", "--stale-days", value, expect=2)
        assert failure["status"] == "failed", failure
        assert failure["error"]["code"] == "invalid_command", failure
    failure = brain.cli("brief", "--space", "no-such-synthetic-space", expect=2)
    assert failure["error"]["code"] == "invalid_request", failure
    # A well-formed family ref the Brain does not hold is not_found; a follow-up ref is not a
    # family ref and is refused as a usage failure.
    failure = brain.cli("brief", "ack", "family:consumer:synthetic/no-such:unassigned", expect=1)
    assert failure["error"]["code"] == "not_found", failure
    failure = brain.cli("brief", "ack", "followup:not-a-family", expect=2)
    assert failure["status"] == "failed", failure
    # A very long stale window makes every family fresh; the follow-up items remain.
    wide = brain.brief("--stale-days", "365")
    _assert_well_formed(wide)
    assert "source_stale" not in _kinds(wide)
    assert wide["coverage"]["families_stale"] == 0
    assert wide["coverage"]["families_checked"] == 3
