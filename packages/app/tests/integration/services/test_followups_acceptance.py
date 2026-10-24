"""Evaluation acceptance for commitments and open questions (follow-ups).

Drives the production CLI MCP composition with a synthetic launcher-policy client session
holding follow-up grants, plus the owner CLI, against one real local Brain. Each test maps
to one line of the 2026-10-14 evaluation acceptance for section 3, plus grant isolation,
launcher-policy tiers and the "drafts never sent" boundary. Synthetic text only.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS
from open_brain.services.local_operations import current_brain_identity
from open_brain.services.mcp_protocol import McpCallError
from packages.app.tests.unit.engine.test_followups import (
    _new_source,
    _projected_text,
    _revise_source,
)

ALL_GRANTS = ("followup-read", "followup-propose", "followup-decide")
ALL_FLAGS = ("--allow-followup-read", "--allow-followup-propose", "--allow-followup-decide")
FOLLOWUP_TOOLS = {
    "brain_followup_list": "followup-read",
    "brain_followup_show": "followup-read",
    "brain_followup_propose": "followup-propose",
    "brain_followup_add_evidence": "followup-propose",
    "brain_followup_decide": "followup-decide",
}

# Synthetic Work-tier texts. The same promise appears in two places (a chat recap and
# an email recap) so the duplicate case has two independent sources.
CHAT_TEXT = (
    "Standup recap. I will send the Q3 forecast to Dana by Friday 2026-10-31. "
    "Still open: who owns the vendor renewal?"
)
EMAIL_TEXT = "Email recap: Rowan said he will send the Q3 forecast to Dana by Friday."
DONE_TEXT = "Forecast sent to Dana this morning; closing the loop on the Q3 forecast."
PERSONAL_TEXT = "Personal note: I will book the synthetic dentist visit next week."
PROMISE = "I will send the Q3 forecast to Dana by Friday 2026-10-31"
PROMISE_AGAIN = "Rowan said he will send the Q3 forecast to Dana by Friday"
QUESTION = "who owns the vendor renewal?"
DONE = "Forecast sent to Dana this morning"
PERSONAL_PROMISE = "I will book the synthetic dentist visit next week"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Capture:
    capture_id: str
    text: str

    def quote(self, phrase: str, role: str) -> dict[str, object]:
        """Exact-quote evidence reference as an MCP client must build it."""
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
    capsys: pytest.CaptureFixture[str]
    monkeypatch: pytest.MonkeyPatch
    chat: Capture
    email: Capture
    done: Capture
    personal: Capture
    sessions: int = 0

    def cli(self, *arguments: str, expect: int = 0) -> dict[str, Any]:
        """Owner CLI with --json; returns the `data` object of the wire dict on success."""
        status = run_cli((*arguments, "--data-dir", str(self.root), "--json"))
        output = self.capsys.readouterr()
        assert status == expect, (arguments, output.out, output.err)
        payload = cast(dict[str, Any], json.loads(output.out))
        if expect != 0:
            return payload
        assert payload["outcome"] == "ok", payload
        return cast(dict[str, Any], payload["data"])

    def cli_text(self, *arguments: str) -> str:
        status = run_cli((*arguments, "--data-dir", str(self.root)))
        output = self.capsys.readouterr()
        assert status == 0, (arguments, output.out, output.err)
        return output.out

    def mcp(
        self,
        exercise: Callable[[Any], None],
        *,
        grants: tuple[str, ...] = ALL_GRANTS,
        flags: tuple[str, ...] = ALL_FLAGS,
        read_tiers: tuple[str, ...] = ("work",),
        expected_capabilities: frozenset[str] | None = None,
    ) -> None:
        """Run `exercise(adapter)` inside the real `open-brain mcp --session-policy` composition."""
        from open_brain.services import mcp_protocol

        errors: list[BaseException] = []
        self.sessions += 1
        authority_root = self.root.parent / f"followup-authority-{self.sessions}"
        authority_root.mkdir(mode=0o700)
        policy_path = authority_root / "session.json"
        brain_id, issuer_epoch = current_brain_identity(self.engine.tasks)
        policy_path.write_text(
            json.dumps(
                {
                    "policy_version": "launcher-policy.v1",
                    "principal_id": "synthetic-followup-agent",
                    "session_id": f"synthetic-followup-session-{self.sessions}",
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
        expected = frozenset(grants) if expected_capabilities is None else expected_capabilities

        def serve(adapter: Any, **_transport: Any) -> None:
            try:
                assert adapter.authority.owner is False
                assert set(adapter.authority.capabilities) == set(expected)
                exercise(adapter)
            except BaseException as error:  # noqa: BLE001 - re-raised after cleanup
                errors.append(error)

        self.monkeypatch.setattr(mcp_protocol, "serve_stdio_mcp", serve)
        status = run_cli(
            ("mcp", *flags, "--session-policy", str(policy_path), "--data-dir", str(self.root))
        )
        self.capsys.readouterr()
        if errors:
            raise errors[0]
        assert status == 0


def _ok(result: dict[str, object]) -> dict[str, Any]:
    assert result.get("outcome") == "ok", result
    return cast(dict[str, Any], result["data"])


def _refusal(adapter: Any, name: str, arguments: dict[str, object]) -> str:
    """Return the closed refusal code whether it arrives as a wire dict or McpCallError."""
    try:
        result = adapter.call_tool(name, arguments)
    except McpCallError as error:
        return str(error)
    assert result.get("outcome") == "refused", result
    return cast(str, result["code"])


@pytest.fixture
def brain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Brain:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root, starter_spaces=()))
    captures: list[Capture] = []
    texts = (
        (CHAT_TEXT, PrivacyTier.WORK),
        (EMAIL_TEXT, PrivacyTier.WORK),
        (DONE_TEXT, PrivacyTier.WORK),
        (PERSONAL_TEXT, PrivacyTier.PERSONAL),
    )
    for index, (text, tier) in enumerate(texts):
        receipt = engine.capture.accept(
            TextPayload(text),
            delivery_id=f"delivery.synthetic-followup-{index}",
            privacy_tier=tier,
        )
        # Quotes must be built from the projected revision text, exactly as a client reads it.
        assert _projected_text(engine, receipt.capture_id) == text
        captures.append(Capture(receipt.capture_id, text))
    chat, email, done, personal = captures
    return Brain(root, engine, capsys, monkeypatch, chat, email, done, personal)


def _propose_promise(adapter: Any, brain: Brain, operation: str = "op-propose-1") -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _ok(
            adapter.call_tool(
                "brain_followup_propose",
                {
                    "operation_id": operation,
                    "kind": "commitment",
                    "summary": "Send the Q3 forecast to Dana",
                    "project": "Q3 forecast",
                    "owner": "self",
                    "due": "2026-10-31",
                    "due_evidence_index": 0,
                    "evidence": [brain.chat.quote(PROMISE, "origin")],
                },
            )
        )["item"],
    )


def test_duplicate_mentions_become_one_item_with_several_sources(brain: Brain) -> None:
    def exercise(adapter: Any) -> None:
        item = _propose_promise(adapter, brain)
        assert item["state"] == "suggested"

        # A second proposal from the same origin quote is refused and names the existing item.
        result = adapter.call_tool(
            "brain_followup_propose",
            {
                "operation_id": "op-propose-duplicate",
                "kind": "commitment",
                "summary": "Forecast to Dana (duplicate)",
                "owner": "self",
                "evidence": [brain.chat.quote(PROMISE, "origin")],
            },
        )
        assert result == {
            "outcome": "refused",
            "data": {"existing_item_id": item["item_id"]},
            "code": "duplicate_item",
        }

        # The right move is to add the second mention as supporting evidence.
        updated = _ok(
            adapter.call_tool(
                "brain_followup_add_evidence",
                {
                    "operation_id": "op-evidence-email",
                    "item_id": item["item_id"],
                    "evidence": brain.email.quote(PROMISE_AGAIN, "supporting"),
                },
            )
        )["item"]
        assert updated["item_id"] == item["item_id"]

        listed = _ok(adapter.call_tool("brain_followup_list", {}))
        assert [entry["item_id"] for entry in listed["items"]] == [item["item_id"]]

        shown = _ok(adapter.call_tool("brain_followup_show", {"item_id": item["item_id"]}))
        quotes = {(row["role"], row["quote"]) for row in shown["evidence"]}
        assert quotes == {("origin", PROMISE), ("supporting", PROMISE_AGAIN)}
        assert {row["capture_id"] for row in shown["evidence"]} == {
            brain.chat.capture_id,
            brain.email.capture_id,
        }

    brain.mcp(exercise)


def test_correction_and_completion_update_the_item_without_erasing_history(brain: Brain) -> None:
    def exercise(adapter: Any) -> None:
        item = _propose_promise(adapter, brain)
        version = item["version"]

        # Correction: the email shows Dana, not the owner, carries the action.
        corrected = _ok(
            adapter.call_tool(
                "brain_followup_decide",
                {
                    "operation_id": "op-correct-owner",
                    "item_id": item["item_id"],
                    "expected_version": version,
                    "action": "correct",
                    "corrections": {"owner": "other", "owner_name": "Dana"},
                },
            )
        )["item"]
        assert corrected["owner"] == "other"
        assert corrected["owner_name"] == "Dana"
        assert corrected["version"] > version
        assert corrected["state"] == "suggested"

        # A stale expected_version is refused and writes nothing.
        assert (
            _refusal(
                adapter,
                "brain_followup_decide",
                {
                    "operation_id": "op-stale",
                    "item_id": item["item_id"],
                    "expected_version": version,
                    "action": "confirm",
                },
            )
            == "version_conflict"
        )

        with_correction = _ok(
            adapter.call_tool(
                "brain_followup_add_evidence",
                {
                    "operation_id": "op-evidence-correction",
                    "item_id": item["item_id"],
                    "evidence": brain.email.quote(PROMISE_AGAIN, "correction"),
                },
            )
        )["item"]
        with_resolution = _ok(
            adapter.call_tool(
                "brain_followup_add_evidence",
                {
                    "operation_id": "op-evidence-resolution",
                    "item_id": item["item_id"],
                    "evidence": brain.done.quote(DONE, "resolution"),
                },
            )
        )["item"]
        resolved = _ok(
            adapter.call_tool(
                "brain_followup_decide",
                {
                    "operation_id": "op-resolve",
                    "item_id": item["item_id"],
                    "expected_version": with_resolution["version"],
                    "action": "resolve",
                    "resolution": "evidence",
                },
            )
        )["item"]
        assert resolved["state"] == "resolved"
        assert resolved["summary"] == item["summary"]
        assert resolved["due"] == "2026-10-31"

        shown = _ok(adapter.call_tool("brain_followup_show", {"item_id": item["item_id"]}))
        # Nothing was erased: every evidence row and every log row is still there, in order.
        assert [row["role"] for row in shown["evidence"]] == [
            "origin",
            "correction",
            "resolution",
        ]
        assert [row["quote"] for row in shown["evidence"]] == [PROMISE, PROMISE_AGAIN, DONE]
        actions = [row["action"] for row in shown["decisions"]]
        assert actions == ["propose", "correct", "add_evidence", "add_evidence", "resolve"]
        versions = [row["new_version"] for row in shown["decisions"]]
        assert versions == sorted(versions) and len(set(versions)) == len(versions)
        assert shown["decisions"][-1]["prior_state"] == "suggested"
        assert shown["decisions"][-1]["new_state"] == "resolved"
        assert shown["decisions"][1]["prior_state"] == "suggested"
        assert shown["decisions"][1]["new_state"] == "suggested"
        assert with_correction["version"] < with_resolution["version"]
        # Decision rows carry no internal principal identity.
        assert all("principal_id" not in row for row in shown["decisions"])

    brain.mcp(exercise)


def test_unknown_owner_and_no_stated_date_are_valid_results(brain: Brain) -> None:
    item_ids: list[str] = []

    def exercise(adapter: Any) -> None:
        item = _ok(
            adapter.call_tool(
                "brain_followup_propose",
                {
                    "operation_id": "op-propose-question",
                    "kind": "question",
                    "summary": "Who owns the vendor renewal?",
                    "owner": "unknown",
                    "evidence": [brain.chat.quote(QUESTION, "origin")],
                },
            )
        )["item"]
        assert item["kind"] == "question"
        assert item["owner"] == "unknown"
        assert item["owner_name"] is None
        assert item["due"] is None
        assert item["owner_confirmed"] is False
        item_ids.append(item["item_id"])

        # A due date with no quote stating it is not valid.
        assert (
            _refusal(
                adapter,
                "brain_followup_propose",
                {
                    "operation_id": "op-propose-undated-due",
                    "kind": "question",
                    "summary": "Vendor renewal owner (dated without a quote)",
                    "owner": "unknown",
                    "due": "2026-11-15",
                    "evidence": [brain.email.quote(PROMISE_AGAIN, "origin")],
                },
            )
            == "invalid_request"
        )

    brain.mcp(exercise)

    # The owner CLI shows the two phrases the evaluation asks for, in human output and JSON.
    (item_id,) = item_ids
    rendered = brain.cli_text("followup", "show", item_id)
    assert "no owner identified" in rendered
    assert "no date stated" in rendered
    shown = brain.cli("followup", "show", item_id)
    assert shown["item"]["owner"] == "unknown"
    assert shown["item"]["due"] is None
    listed = brain.cli("followup", "list", "--kind", "question")
    assert [entry["item_id"] for entry in listed["items"]] == [item_id]


def test_resolved_item_stays_out_of_the_default_list_until_new_evidence(brain: Brain) -> None:
    item_ids: list[str] = []

    def propose(adapter: Any) -> None:
        item_ids.append(_propose_promise(adapter, brain)["item_id"])

    brain.mcp(propose)
    (item_id,) = item_ids

    before = brain.cli("followup", "show", item_id)["item"]
    resolved = brain.cli(
        "followup",
        "decide",
        item_id,
        "--action",
        "resolve",
        "--expected-version",
        str(before["version"]),
        "--operation-id",
        "op-owner-resolve",
        "--resolution",
        "owner_statement",
    )["item"]
    assert resolved["state"] == "resolved"
    assert resolved["new_evidence_since_decision"] is False

    assert brain.cli("followup", "list")["items"] == []
    closed = brain.cli("followup", "list", "--include-closed")["items"]
    assert [(entry["item_id"], entry["state"]) for entry in closed] == [(item_id, "resolved")]

    def add_new_evidence(adapter: Any) -> None:
        assert _ok(adapter.call_tool("brain_followup_list", {}))["items"] == []
        updated = _ok(
            adapter.call_tool(
                "brain_followup_add_evidence",
                {
                    "operation_id": "op-evidence-after-resolve",
                    "item_id": item_id,
                    "evidence": brain.email.quote(PROMISE_AGAIN, "supporting"),
                },
            )
        )["item"]
        assert updated["state"] == "resolved"
        assert updated["new_evidence_since_decision"] is True
        listed = _ok(adapter.call_tool("brain_followup_list", {}))["items"]
        assert [entry["item_id"] for entry in listed] == [item_id]

    brain.mcp(add_new_evidence)

    returned = brain.cli("followup", "list")["items"]
    assert [(entry["item_id"], entry["new_evidence_since_decision"]) for entry in returned] == [
        (item_id, True)
    ]
    history = brain.cli("followup", "show", item_id)["decisions"]
    assert [row["action"] for row in history] == ["propose", "resolve", "add_evidence"]


def test_each_followup_grant_is_enforced_independently(brain: Brain) -> None:
    def readers_only(adapter: Any) -> None:
        names = {tool["name"] for tool in adapter.list_tools()} & set(FOLLOWUP_TOOLS)
        assert names == {"brain_followup_list", "brain_followup_show"}
        with pytest.raises(McpCallError, match="^unknown tool$"):
            adapter.call_tool(
                "brain_followup_propose",
                {
                    "operation_id": "op-denied",
                    "kind": "question",
                    "summary": "denied",
                    "owner": "unknown",
                    "evidence": [brain.chat.quote(QUESTION, "origin")],
                },
            )
        assert _ok(adapter.call_tool("brain_followup_list", {}))["items"] == []

    brain.mcp(readers_only, grants=("followup-read",), flags=("--allow-followup-read",))

    def proposer_only(adapter: Any) -> None:
        names = {tool["name"] for tool in adapter.list_tools()} & set(FOLLOWUP_TOOLS)
        assert names == {"brain_followup_propose", "brain_followup_add_evidence"}
        with pytest.raises(McpCallError, match="^unknown tool$"):
            adapter.call_tool("brain_followup_list", {})

    brain.mcp(proposer_only, grants=("followup-propose",), flags=("--allow-followup-propose",))

    assert brain.cli("followup", "list")["items"] == []


def test_launcher_policy_intersects_grants_and_gates_the_personal_tier(brain: Brain) -> None:
    # (a) The session's follow-up capabilities are the policy's capabilities intersected
    # with the --allow-followup-* flags: a flag the policy does not grant adds nothing.
    def policy_without_decide(adapter: Any) -> None:
        names = {tool["name"] for tool in adapter.list_tools()} & set(FOLLOWUP_TOOLS)
        assert names == set(FOLLOWUP_TOOLS) - {"brain_followup_decide"}
        item = _propose_promise(adapter, brain)
        with pytest.raises(McpCallError, match="^unknown tool$"):
            adapter.call_tool(
                "brain_followup_decide",
                {
                    "operation_id": "op-decide-without-policy-grant",
                    "item_id": item["item_id"],
                    "expected_version": item["version"],
                    "action": "confirm",
                },
            )

    brain.mcp(
        policy_without_decide,
        grants=("followup-read", "followup-propose"),
        expected_capabilities=frozenset({"followup-read", "followup-propose"}),
    )
    (owner_view,) = brain.cli("followup", "list")["items"]
    assert (owner_view["state"], owner_view["version"]) == ("suggested", 1)

    # A policy grant the launcher flags do not select is not exercised either.
    def flags_without_propose(adapter: Any) -> None:
        names = {tool["name"] for tool in adapter.list_tools()} & set(FOLLOWUP_TOOLS)
        assert names == {"brain_followup_list", "brain_followup_show"}

    brain.mcp(
        flags_without_propose,
        flags=("--allow-followup-read",),
        expected_capabilities=frozenset({"followup-read"}),
    )

    # (b) A Personal item, proposed by the owner CLI, is visible only to a session whose
    # launcher policy allows the Personal tier.
    personal = brain.cli(
        "followup",
        "propose",
        "--kind",
        "commitment",
        "--summary",
        "Book the synthetic dentist visit",
        "--owner",
        "self",
        "--evidence",
        json.dumps(brain.personal.quote(PERSONAL_PROMISE, "origin")),
        "--operation-id",
        "op-owner-personal",
    )["item"]
    assert personal["privacy_tier"] == "personal"
    personal_id = personal["item_id"]

    def work_only(adapter: Any) -> None:
        listed = _ok(adapter.call_tool("brain_followup_list", {}))["items"]
        assert personal_id not in {entry["item_id"] for entry in listed}
        assert len(listed) == 1
        assert _refusal(adapter, "brain_followup_show", {"item_id": personal_id}) == "not_found"

    brain.mcp(work_only)

    def personal_allowed(adapter: Any) -> None:
        listed = _ok(adapter.call_tool("brain_followup_list", {}))["items"]
        assert personal_id in {entry["item_id"] for entry in listed}
        shown = _ok(adapter.call_tool("brain_followup_show", {"item_id": personal_id}))
        assert [row["quote"] for row in shown["evidence"]] == [PERSONAL_PROMISE]

    brain.mcp(personal_allowed, read_tiers=("work", "personal"))


def test_edited_message_keeps_the_item_and_marks_changed_quotes(brain: Brain) -> None:
    """An edited source keeps the item; the quote follows the head or becomes source_changed."""
    source = _new_source(brain.engine, "Synthetic promise: I will send the forecast by Friday")
    text = _projected_text(brain.engine, source.head)
    quote = Capture(source.head, text).quote("send the forecast by Friday", "origin")
    item_ids: list[str] = []

    def propose(adapter: Any) -> None:
        item_ids.append(
            _ok(
                adapter.call_tool(
                    "brain_followup_propose",
                    {
                        "operation_id": "op-edited-propose",
                        "kind": "commitment",
                        "summary": "Send the forecast by Friday",
                        "owner": "self",
                        "evidence": [quote],
                    },
                )
            )["item"]["item_id"]
        )

    brain.mcp(propose)
    (item_id,) = item_ids

    moved_head = _revise_source(
        brain.engine, source, "Edited: I will send the forecast by Friday, after the review"
    )

    def still_visible_with_moved_quote(adapter: Any) -> None:
        listed = _ok(adapter.call_tool("brain_followup_list", {}))["items"]
        assert item_id in {entry["item_id"] for entry in listed}
        (evidence,) = _ok(adapter.call_tool("brain_followup_show", {"item_id": item_id}))[
            "evidence"
        ]
        assert evidence["status"] == "moved"
        assert evidence["quote"] == "send the forecast by Friday"
        assert evidence["head_revision_id"] == moved_head
        assert evidence["head_quote_start"] != quote["quote_start"]

    brain.mcp(still_visible_with_moved_quote)

    _revise_source(brain.engine, source, "Edited again: the forecast is cancelled")

    def visible_but_source_changed(adapter: Any) -> None:
        listed = _ok(adapter.call_tool("brain_followup_list", {}))["items"]
        assert item_id in {entry["item_id"] for entry in listed}
        (evidence,) = _ok(adapter.call_tool("brain_followup_show", {"item_id": item_id}))[
            "evidence"
        ]
        assert (evidence["status"], evidence["quote"], evidence["head_quote_start"]) == (
            "source_changed",
            None,
            None,
        )

    brain.mcp(visible_but_source_changed)

    # The owner never receives older revision text either; the CLI says the quote is withheld.
    (owner_evidence,) = brain.cli("followup", "show", item_id)["evidence"]
    assert (owner_evidence["status"], owner_evidence["quote"]) == ("source_changed", None)
    assert "(quote withheld)" in brain.cli_text("followup", "show", item_id)


def test_drafts_are_never_sent_no_ticket_or_message_egress_path(brain: Brain) -> None:
    root = Path(__file__).resolve().parents[5]
    sources = (
        root / "packages/engine/src/open_brain_engine/engine/followups.py",
        root / "packages/engine/src/open_brain_engine/engine/followup_visibility.py",
        root / "packages/engine/src/open_brain_engine/engine/followup_contracts.py",
        root / "packages/app/src/open_brain/services/followups.py",
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
        "managed_providers",
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
        if cast(str, tool["name"]).startswith("brain_followup_")
    }
    assert registered == {name: (grant,) for name, grant in FOLLOWUP_TOOLS.items()}
    assert not [
        name
        for name in (cast(str, tool["name"]) for tool in MCP_REGISTERED_TOOLS)
        if re.search(r"send|notify|ticket|message|jira|slack_post", name)
    ]

    def link_only_records(adapter: Any) -> None:
        item = _propose_promise(adapter, brain)
        linked = _ok(
            adapter.call_tool(
                "brain_followup_decide",
                {
                    "operation_id": "op-link",
                    "item_id": item["item_id"],
                    "expected_version": item["version"],
                    "action": "link",
                    "ticket_key": "SYN-123",
                    "ticket_url": "https://tickets.example.com/browse/SYN-123",
                },
            )
        )["item"]
        assert linked["ticket_key"] == "SYN-123"
        assert linked["ticket_url"] == "https://tickets.example.com/browse/SYN-123"
        assert linked["state"] == "suggested"
        # The link tool schema has no destination, body, or send parameter.
        (decide_tool,) = [
            tool for tool in adapter.list_tools() if tool["name"] == "brain_followup_decide"
        ]
        properties = set(decide_tool["inputSchema"]["properties"])
        assert decide_tool["inputSchema"]["additionalProperties"] is False
        assert not properties & {"body", "comment", "message", "recipient", "send", "transition"}
        assert "untrusted" in decide_tool["description"]

    brain.mcp(link_only_records)
