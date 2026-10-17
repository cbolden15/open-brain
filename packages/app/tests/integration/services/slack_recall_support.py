"""Deterministic W3 acceptance plumbing, never a production/model recall runner.

Only structured search/read responses grant IDs. Message text is inert, even when
it contains JSON. Limits bound this test driver, not an arbitrary same-user client.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from open_brain_engine.engine.slack_selection_validation import validate_selection
from open_brain_engine.engine.slack_sharing_projection import safe_permalink

ToolCall = Callable[[str, dict[str, Any]], dict[str, Any]]
Identity = tuple[str, str]
MAX_SEARCHES = 3
SEARCH_LIMIT = 10
MAX_RECORDS = 8
MAX_SELECTION = 24
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_TOOL_CALLS = 200
MAX_READ_PAGES = 16
READ_TARGET_BYTES = 4096
STDIO_RESPONSE_SECONDS = 15
QUALIFIER = (
    "Based on selected approved messages as observed at their capture times; "
    "later Slack edits, deletions and messages are unknown."
)


class IncompleteEvidence(Exception):
    """A bounded protocol traversal cannot support a complete-evidence answer."""


class StdioSession:
    """Adaptive version of the W2 CLI subprocess harness, with no ambient config."""

    def __init__(self, root: Path, brain: Path, home: Path, policy: Path, consent: Path) -> None:
        home.mkdir(mode=0o700)
        self.stderr = (home / "stdio.stderr").open("wb")
        self.process = subprocess.Popen(
            [sys.executable, "-c",
             "from open_brain.services.local_entrypoints import run_cli; "
             "raise SystemExit(run_cli())", "mcp", "--data-dir", str(brain),
             "--allow-search", "--allow-content-read",
             "--session-policy", str(policy), "--consent-state", str(consent)],
            cwd=root,
            env={"HOME": str(home), "PATH": os.defpath, "PYTHONPATH": os.pathsep.join(
                str(root / "packages" / package / "src") for package in ("app", "engine"))},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
        )
        self.requests: list[dict[str, Any]] = []
        self._pending = b""
        try:
            initialized = self.rpc("initialize", {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "synthetic-w3-acceptance", "version": "1"},
            })
            assert initialized["protocolVersion"] == "2025-03-26"
            self.tools = {tool["name"] for tool in self.rpc("tools/list", {})["tools"]}
            assert {"brain_search_page", "brain_read"} <= self.tools
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=5)
            assert self.process.stdin is not None and self.process.stdout is not None
            self.process.stdin.close()
            self.process.stdout.close()
            self.stderr.close()
            raise

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": len(self.requests),
                   "method": method, "params": params}
        self.requests.append(message)
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + STDIO_RESPONSE_SECONDS
        while b"\n" not in self._pending:
            if len(self._pending) > 1_048_576:
                raise IncompleteEvidence("stdio_response_budget")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise IncompleteEvidence("stdio_timeout")
            chunk = os.read(
                self.process.stdout.fileno(), min(65536, 1_048_577 - len(self._pending))
            )
            if not chunk:
                self.process.wait(timeout=5)
                self.stderr.flush()
                diagnostic = Path(self.stderr.name).read_text()[:1000]
                raise IncompleteEvidence(f"stdio_eof:{self.process.returncode}:{diagnostic}")
            self._pending += chunk
        line, self._pending = self._pending.split(b"\n", 1)
        if len(line) + 1 > 1_048_576:
            raise IncompleteEvidence("stdio_response_budget")
        response = json.loads(line)
        if response.get("id") != message["id"] or "error" in response:
            raise IncompleteEvidence("rpc_error")
        return dict(response["result"])

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name in {"brain_search_page", "brain_read"} and name in self.tools
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise IncompleteEvidence(result["content"][0]["text"])
        if "structuredContent" not in result:
            raise IncompleteEvidence("missing_structured_response")
        return dict(result["structuredContent"])

    def close(self) -> None:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise AssertionError("stdio child did not exit") from None
        finally:
            self.process.stdout.close()
            self.stderr.close()
        assert self.process.returncode == 0


@dataclass(frozen=True)
class Evidence:
    identity: Identity
    message_text: str
    citation: dict[str, Any]
    projected_text: str


@dataclass
class RecallResult:
    status: str = "complete"
    reason: str | None = None
    evidence: dict[Identity, Evidence] = field(default_factory=dict)
    # Every observed search/read page, including final revalidation, is retained.
    pages: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    evidence_bytes: int = 0


class RecallDriver:
    """Three lexical searches, complete reads and final bounded revalidation.

    Max 200 tool calls, 16 pages per full read (4096-byte targets), 10 results per
    search, 8 records or a structured selection's size up to 24, and 64 KiB of
    assembled distinct projected envelopes. Revalidation transfers the same data
    again within the call/session budgets; it does not add distinct evidence.
    """

    def __init__(self, call: ToolCall) -> None:
        self.call = call
        self.result = RecallResult()
        self.identities: dict[str, str] = {}
        self.selections: dict[str, tuple[Identity, ...]] = {}
        self.member_selection: dict[Identity, str] = {}
        self.record_limit = MAX_RECORDS

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if len(self.result.calls) >= MAX_TOOL_CALLS:
            raise IncompleteEvidence("call_budget")
        self.result.calls.append((tool, dict(arguments)))
        response = self.call(tool, arguments)
        if response.get("status") != "ok":
            raise IncompleteEvidence(response.get("error", {}).get("code", "protocol_error"))
        self.result.pages.append({"tool": tool, "arguments": dict(arguments),
                                  "complete": response.get("complete"),
                                  "next_cursor": response.get("next_cursor")})
        if response.get("truncated") or response.get("warnings"):
            raise IncompleteEvidence("truncated_or_stale")
        return response

    def _bind(self, record: dict[str, Any]) -> Identity:
        identity = (record["record_id"], record["revision_id"])
        old = self.identities.setdefault(*identity)
        if old != identity[1]:
            raise IncompleteEvidence("revision_changed")
        return identity

    def _selection(self, response: dict[str, Any], identity: Identity) -> None:
        selection = response.get("selection")
        if selection is None:
            if identity in self.member_selection:
                raise IncompleteEvidence("selection_changed")
            return
        try:
            validate_selection(selection)
        except ValueError as error:
            raise IncompleteEvidence("invalid_selection") from error
        if any(member["status"] != "visible" for member in selection["members"]):
            raise IncompleteEvidence("unavailable_member")
        members = tuple(self._bind(member) for member in selection["members"])
        if len(set(members)) != len(members):
            raise IncompleteEvidence("duplicate_member")
        if members[selection["member_ordinal"]] != identity:
            raise IncompleteEvidence("selection_binding")
        selection_id = selection["selection_id"]
        if self.selections.setdefault(selection_id, members) != members:
            raise IncompleteEvidence("selection_changed")
        for member in members:
            if self.member_selection.setdefault(member, selection_id) != selection_id:
                raise IncompleteEvidence("selection_changed")
        self.record_limit = max(self.record_limit, len(members))

    def _read(self, identity: Identity, *, revalidate: bool = False) -> Evidence:
        chunks: list[str] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        offset = 0
        for _ in range(MAX_READ_PAGES):
            response = self._call("brain_read", {
                "dto_version": 1, "record_id": identity[0],
                "expected_revision_id": identity[1], "target_bytes": READ_TARGET_BYTES,
                "cursor": cursor,
            })
            if self._bind(response["record"]) != identity:
                raise IncompleteEvidence("revision_changed")
            self._selection(response, identity)
            content = response["content"]
            if content.get("kind") != "untrusted_text":
                raise IncompleteEvidence("invalid_content")
            text = content["text"]
            size = len(text.encode("utf-8"))
            if response["start_byte"] != offset or response["end_byte"] != offset + size:
                raise IncompleteEvidence("read_gap_or_overlap")
            offset += size
            if offset + (0 if revalidate else self.result.evidence_bytes) > MAX_EVIDENCE_BYTES:
                raise IncompleteEvidence("evidence_budget")
            chunks.append(text)
            complete, following = response.get("complete"), response.get("next_cursor")
            if complete is True and following is None:
                break
            if complete is not False or not isinstance(following, str) or not following:
                raise IncompleteEvidence("incomplete_read")
            if size == 0 or following in seen_cursors:
                raise IncompleteEvidence("cursor_stale")
            seen_cursors.add(following)
            cursor = following
        else:
            raise IncompleteEvidence("read_page_budget")
        projected = "".join(chunks)
        try:
            envelope = json.loads(projected)
        except ValueError as error:
            raise IncompleteEvidence("invalid_envelope") from error
        if (not isinstance(envelope, dict)
                or set(envelope) != {"envelope_version", "message_text", "citation"}
                or envelope["envelope_version"] != "slack-recall-evidence.v1"
                or not isinstance(envelope["message_text"], str)
                or not isinstance(envelope["citation"], dict)):
            raise IncompleteEvidence("invalid_envelope")
        return Evidence(identity, envelope["message_text"], envelope["citation"], projected)

    def run(self, variants: tuple[str, ...]) -> RecallResult:
        try:
            if not 1 <= len(variants) <= MAX_SEARCHES or len(set(variants)) != len(variants):
                raise IncompleteEvidence("search_budget")
            candidates: dict[Identity, None] = {}
            for query in variants:
                response = self._call("brain_search_page", {
                    "dto_version": 1, "query": query, "mode": "lexical", "limit": SEARCH_LIMIT,
                })
                if (response.get("complete") is not True
                        or response.get("next_cursor") is not None):
                    raise IncompleteEvidence("search_result_budget")
                if len(response["results"]) > SEARCH_LIMIT:
                    raise IncompleteEvidence("search_result_budget")
                for record in response["results"]:
                    candidates[self._bind(record)] = None
            while candidates:
                identity = next(iter(candidates))
                del candidates[identity]
                if identity in self.result.evidence:
                    continue
                if len(self.result.evidence) >= self.record_limit:
                    raise IncompleteEvidence("record_budget")
                evidence = self._read(identity)
                self.result.evidence[identity] = evidence
                self.result.evidence_bytes += len(evidence.projected_text.encode("utf-8"))
                for members in self.selections.values():
                    for member in members:
                        if member not in self.result.evidence:
                            candidates[member] = None
            # Currentness is observed, not an atomic snapshot or live Slack claim.
            # Re-read all bound revisions to catch edits/revocation during traversal.
            for identity, evidence in self.result.evidence.items():
                if self._read(identity, revalidate=True) != evidence:
                    raise IncompleteEvidence("evidence_changed")
            if any(member not in self.result.evidence
                   for members in self.selections.values() for member in members):
                raise IncompleteEvidence("unread_member")
        except IncompleteEvidence as error:
            self.result.status = "incomplete"
            self.result.reason = str(error)
        return self.result


@dataclass(frozen=True)
class ClaimRule:
    """Fixture-owned expected claim text/status and its allowed evidence IDs."""

    text: str
    kind: str
    allowed_evidence: frozenset[Identity]


def validate_answer(
    answer: dict[str, Any], result: RecallResult, rules: dict[str, ClaimRule], *, outcome: str
) -> None:
    """Check a deterministic fixture, not the truth/quality of arbitrary model prose."""
    if set(answer) != {"outcome", "claims", "qualifier"} or answer["qualifier"] != QUALIFIER:
        raise ValueError("answer_shape_or_coverage")
    expected_outcome = "incomplete" if result.status == "incomplete" else outcome
    if answer["outcome"] != expected_outcome:
        raise ValueError("answer_outcome")
    if result.status == "incomplete":
        if answer["claims"]:
            raise ValueError("incomplete_claims")
        return
    claims = answer["claims"]
    if not isinstance(claims, list) or len(claims) != len(rules):
        raise ValueError("missing_claim")
    seen: set[str] = set()
    for claim in claims:
        if set(claim) != {"id", "text", "kind", "citations"}:
            raise ValueError("claim_shape")
        claim_id = claim["id"]
        if claim_id in seen or claim_id not in rules:
            raise ValueError("unknown_or_duplicate_claim")
        seen.add(claim_id)
        rule = rules[claim_id]
        if claim["text"] != rule.text or claim["kind"] != rule.kind or not claim["citations"]:
            raise ValueError("claim_contract")
        for citation in claim["citations"]:
            if set(citation) != {"record_id", "revision_id", "quote", "permalink"}:
                raise ValueError("citation_shape")
            identity = (citation["record_id"], citation["revision_id"])
            if identity not in rule.allowed_evidence or identity not in result.evidence:
                raise ValueError("citation_identity")
            evidence = result.evidence[identity]
            quote = citation["quote"]
            if not isinstance(quote, str) or not quote or quote not in evidence.message_text:
                raise ValueError("citation_quote")
            link = citation["permalink"]
            if link != evidence.citation.get("permalink") or (
                link is not None and (not isinstance(link, str) or not safe_permalink(link))
            ):
                raise ValueError("citation_permalink")
