"""Repeatable installed-executable acceptance for review and publication."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

_TIMEOUT_SECONDS = 60
_CHECK_NAMES = (
    "A10_candidate_executable",
    "A1_cli_review_workflow",
    "A8_generated_agent_configs",
    "A1_all_operations_all_transports",
    "A2_explicit_consolidation_2_3_2",
    "A3_stable_page_update",
    "exact_portable_artifacts",
)


class AcceptanceFailure(RuntimeError):
    """One bounded acceptance failure safe to include in the private report."""


class _Acceptance:
    def __init__(self, executable: Path, output: Path, root: Path) -> None:
        self.executable = executable
        self.output = output
        self.root = root
        self.brain = root / "brain"
        self.home = root / "home"
        self.drafts = root / "drafts"
        self.claude_project = root / "claude-project"
        self.codex_project = root / "codex-project"
        for directory in (
            self.home,
            self.drafts,
            self.claude_project,
            self.codex_project,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.environment = {
            **os.environ,
            "HOME": os.fspath(self.home),
            "XDG_CONFIG_HOME": os.fspath(self.home / ".config"),
            "XDG_DATA_HOME": os.fspath(self.home / ".local/share"),
        }
        self.checks: list[dict[str, object]] = []
        self.evidence: dict[str, object] = {
            "selected_capture_count": 0,
            "canonical_page_count": 0,
        }
        self.capture_ids: list[str] = []
        self.source_bytes: dict[str, bytes] = {}
        self.space_id = ""
        self.pages: list[dict[str, object]] = []
        self.decisions: list[dict[str, object]] = []
        self._claude_entry: dict[str, object] | None = None
        self._codex_entry: dict[str, object] | None = None

    def run(self) -> None:
        phases: tuple[tuple[str, Callable[[], None]], ...] = (
            (_CHECK_NAMES[0], self._candidate_check),
            (_CHECK_NAMES[1], self._cli_workflow),
            (_CHECK_NAMES[2], self._generated_configs),
            (_CHECK_NAMES[3], self._all_operations),
            (_CHECK_NAMES[4], self._consolidate),
            (_CHECK_NAMES[5], self._update_page),
            (_CHECK_NAMES[6], self._verify_artifacts),
        )
        for phase_index, (name, phase) in enumerate(phases):
            started = time.monotonic()
            try:
                phase()
            except Exception as error:
                self.checks.append(
                    {
                        "name": name,
                        "status": "failed",
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "error": _safe_error(error),
                    }
                )
                self.checks.extend(
                    {"name": pending, "status": "skipped", "reason": "earlier check failed"}
                    for pending, _ in phases[phase_index + 1 :]
                )
                raise
            self.checks.append(
                {
                    "name": name,
                    "status": "passed",
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }
            )

    def _all_operations(self) -> None:
        main_brain = self.brain
        completed: dict[str, list[str]] = {}
        try:
            for client in ("cli", "claude-code", "codex"):
                self.brain = self.root / f"operation-matrix-{client}"
                self._cli("init")
                space = self._cli("space", "create", "Matrix")
                space_id = _string(_object(space, "space"), "space_id")
                source = self._cli("capture", "Synthetic operation matrix evidence")
                capture_id = _string(source, "capture_id")
                self._cli("inbox", "route", capture_id, space_id)
                entry = None
                if client != "cli":
                    project = self.root / f"matrix-project-{client}"
                    project.mkdir()
                    entry = self._configure(client, project, ("read", "propose", "decide"))
                proposals: list[dict[str, object]] = []
                for ordinal in range(3):
                    proposal = self._review_call(
                        entry,
                        "propose",
                        {
                            "capture_ids": [capture_id],
                            "title": f"Matrix {ordinal}",
                            "markdown": f"Matrix body {ordinal}\n\n- retained line\n",
                            "idempotency_key": f"matrix-{ordinal}",
                        },
                    )
                    proposals.append(
                        self._review_call(
                            entry,
                            "show",
                            {
                                "proposal_id": _string(proposal, "proposal_id"),
                            },
                        )
                    )
                seen: list[str] = []
                offset = 0
                while True:
                    listed = self._review_call(
                        entry,
                        "list",
                        {
                            "capture_id": capture_id,
                            "space_id": space_id,
                            "status": "pending",
                            "limit": 1,
                            "offset": offset,
                        },
                    )
                    rows = listed.get("proposals")
                    if not isinstance(rows, list) or len(rows) != 1:
                        raise AcceptanceFailure("review pagination lost a proposal")
                    seen.append(_string(cast(dict[str, object], rows[0]), "proposal_id"))
                    next_offset = listed.get("next_offset")
                    if next_offset is None:
                        break
                    if not isinstance(next_offset, int) or next_offset <= offset or len(seen) > 3:
                        raise AcceptanceFailure("review pagination did not advance")
                    offset = next_offset
                if set(seen) != {_string(p, "proposal_id") for p in proposals}:
                    raise AcceptanceFailure("review filters changed the selected proposals")
                for ordinal, operation in enumerate(("approve", "reject", "edit_and_approve")):
                    shown = proposals[ordinal]
                    arguments: dict[str, object] = {
                        "proposal_id": _string(shown, "proposal_id"),
                        "review_token": _string(shown, "review_token"),
                        "idempotency_key": f"matrix-decision-{ordinal}",
                    }
                    if operation == "edit_and_approve":
                        arguments["markdown"] = "Edited matrix body\n\n- preserved line\n"
                    before = {p: p.read_bytes() for p in _canonical_pages(self.brain)}
                    decision = self._review_call(entry, operation, arguments)
                    repeated = self._review_call(entry, operation, arguments)
                    if repeated.get("decision_id") != decision.get("decision_id"):
                        raise AcceptanceFailure("terminal retry changed decision identity")
                    if operation == "reject":
                        if {p: p.read_bytes() for p in _canonical_pages(self.brain)} != before:
                            raise AcceptanceFailure("rejection changed canonical page bytes")
                    else:
                        path = _only(
                            self.brain / "content/spaces", _string(decision, "page_id") + ".md"
                        )
                        _, body = _parse_markdown(path.read_bytes())
                        expected = str(arguments.get("markdown", shown["markdown"]))
                        if body != expected.rstrip("\n") + "\n":
                            raise AcceptanceFailure("decision did not publish inspected Markdown")
                if len(_canonical_pages(self.brain)) != 2:
                    raise AcceptanceFailure("operation matrix published an unexpected page count")
                completed[client] = [
                    "propose",
                    "list",
                    "show",
                    "approve",
                    "reject",
                    "edit_and_approve",
                ]
        finally:
            self.brain = main_brain
        self.evidence["operation_matrix"] = completed

    def _review_call(
        self, entry: Mapping[str, object] | None, operation: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        if entry is not None:
            _, responses = self._mcp(entry, (("brain_review_" + operation, arguments),))
            return _mcp_content(responses[0])
        if operation == "propose":
            return self._propose_cli(
                tuple(_strings(arguments, "capture_ids")),
                title=_string(arguments, "title"),
                body=_string(arguments, "markdown"),
                key=_string(arguments, "idempotency_key"),
            )
        argv = ["review", operation.replace("_", "-")]
        if operation != "list":
            argv.append(_string(arguments, "proposal_id"))
        for name, value in arguments.items():
            if name == "proposal_id":
                continue
            if name == "markdown":
                path = self.drafts / "matrix-edit.md"
                path.write_text(str(value), encoding="utf-8")
                argv.extend(("--markdown-file", str(path)))
            else:
                argv.extend(("--" + name.replace("_", "-"), str(value)))
        return self._cli(*argv)

    def _candidate_check(self) -> None:
        if not self.executable.is_absolute() or not self.executable.is_file():
            raise AcceptanceFailure("candidate executable is unavailable")
        mode = self.executable.stat(follow_symlinks=False).st_mode
        if not stat.S_ISREG(mode) or mode & 0o111 == 0:
            raise AcceptanceFailure("candidate is not an executable regular file")
        version = self._process((os.fspath(self.executable), "--version"))
        if not version.stdout.startswith("open-brain "):
            raise AcceptanceFailure("candidate version response is invalid")

    def _cli_workflow(self) -> None:
        initialized = self._cli("init")
        if initialized.get("status") not in {"initialized", "existing", "ready"}:
            raise AcceptanceFailure("candidate did not initialize the synthetic Brain")
        created = self._cli(
            "space",
            "create",
            "Review acceptance",
            "--idempotency-key",
            "acceptance-space",
        )
        self.space_id = _string(_object(created, "space"), "space_id")
        payloads = (
            "# Shared acceptance title\nGroup one evidence A",
            "# Group one second\nGroup one evidence B",
            "# Group two first\nGroup two evidence A",
            "# Group two second\nGroup two evidence B",
            "# Group two third\nGroup two evidence C",
            "# Group three first\nGroup three evidence A",
            "# Group three second\nGroup three evidence B",
            "# Shared acceptance title\nSame-title capture excluded from the 2/3/2 groups",
        )
        for index, payload in enumerate(payloads):
            captured = self._cli("capture", payload)
            capture_id = _string(captured, "capture_id")
            self.capture_ids.append(capture_id)
            source = _only(self.brain / "sources/captures", f"{capture_id}.json")
            self.source_bytes[capture_id] = source.read_bytes()
            routed = self._cli(
                "inbox",
                "route",
                capture_id,
                self.space_id,
                "--idempotency-key",
                f"acceptance-route-{index}",
            )
            if routed.get("capture_id") != capture_id:
                raise AcceptanceFailure("capture route identity changed")

        distractor = self.capture_ids[7]
        rejected = self._propose_cli(
            (distractor,),
            title="Shared acceptance title",
            body="This rejected draft must never publish.\n",
            key="acceptance-reject-proposal",
        )
        repeated = self._propose_cli(
            (distractor,),
            title="Shared acceptance title",
            body="This rejected draft must never publish.\n",
            key="acceptance-reject-proposal",
        )
        if repeated.get("proposal_id") != rejected.get("proposal_id"):
            raise AcceptanceFailure("proposal idempotency changed proposal identity")
        shown = self._cli("review", "show", _string(rejected, "proposal_id"))
        token = _string(shown, "review_token")
        wrong = self._cli_failure(
            "review",
            "reject",
            _string(rejected, "proposal_id"),
            "--review-token",
            "0" * 64,
            "--idempotency-key",
            "acceptance-wrong-token",
        )
        if _object(wrong, "error").get("code") != "review_conflict":
            raise AcceptanceFailure("wrong review token was not rejected")
        decision = self._cli(
            "review",
            "reject",
            _string(rejected, "proposal_id"),
            "--review-token",
            token,
            "--idempotency-key",
            "acceptance-reject-decision",
        )
        replay = self._cli(
            "review",
            "reject",
            _string(rejected, "proposal_id"),
            "--review-token",
            token,
            "--idempotency-key",
            "acceptance-reject-decision",
        )
        if decision.get("decision_id") != replay.get("decision_id"):
            raise AcceptanceFailure("decision idempotency changed decision identity")
        if _canonical_pages(self.brain):
            raise AcceptanceFailure("rejection published a canonical page")
        self.decisions.append(decision)
        self.evidence["selected_capture_count"] = 7

    def _generated_configs(self) -> None:
        bindings_before = len(tuple((self.brain / "history/review-bindings").rglob("*.json")))
        read_only = self._configure("claude-code", self.claude_project, ("read",))
        tools, responses = self._mcp(
            read_only,
            (
                (
                    "brain_review_propose",
                    {
                        "capture_ids": [self.capture_ids[0]],
                        "title": "Denied",
                        "markdown": "Denied\n",
                    },
                ),
            ),
        )
        if tools != {"brain_review_list", "brain_review_show"}:
            raise AcceptanceFailure("read-only review grant exposed unexpected MCP tools")
        denial = responses[0]
        denied = "error" in denial or bool(_object(denial, "result").get("isError"))
        if not denied:
            raise AcceptanceFailure("review proposal succeeded without its grant")
        bindings_after = len(tuple((self.brain / "history/review-bindings").rglob("*.json")))
        if bindings_after != bindings_before:
            raise AcceptanceFailure("grant denial changed review history")

        grants = ("read", "propose", "decide")
        self._claude_entry = self._configure("claude-code", self.claude_project, grants)
        self._codex_entry = self._configure("codex", self.codex_project, grants)
        expected_tools = {
            "brain_review_list",
            "brain_review_show",
            "brain_review_propose",
            "brain_review_approve",
            "brain_review_reject",
            "brain_review_edit_and_approve",
        }
        claude_tools, _ = self._mcp(self._claude_entry, ())
        codex_tools, _ = self._mcp(self._codex_entry, ())
        if claude_tools != expected_tools or codex_tools != expected_tools:
            raise AcceptanceFailure("generated full review grants exposed unexpected MCP tools")
        for entry in (self._claude_entry, self._codex_entry):
            if entry.get("command") != os.fspath(self.executable):
                raise AcceptanceFailure("generated client config changed candidate identity")

    def _consolidate(self) -> None:
        first = self._propose_cli(
            tuple(self.capture_ids[0:2]),
            title="Acceptance page one",
            body="Exact body for page one.\n",
            key="acceptance-page-one",
        )
        first_show = self._cli("review", "show", _string(first, "proposal_id"))
        first_decision = self._cli(
            "review",
            "approve",
            _string(first, "proposal_id"),
            "--review-token",
            _string(first_show, "review_token"),
            "--idempotency-key",
            "acceptance-page-one-approve",
        )
        self.pages.append(
            {
                "proposal": first,
                "shown": first_show,
                "decision": first_decision,
                "body": "Exact body for page one.\n",
            }
        )
        self.decisions.append(first_decision)

        claude = self._required_entry(self._claude_entry)
        second, second_show, second_decision = self._mcp_create_and_decide(
            claude,
            tuple(self.capture_ids[2:5]),
            title="Acceptance page two",
            body="Exact body for page two.\n",
            edit=None,
            key="acceptance-page-two",
        )
        self.pages.append(
            {
                "proposal": second,
                "shown": second_show,
                "decision": second_decision,
                "body": "Exact body for page two.\n",
            }
        )
        self.decisions.append(second_decision)

        codex = self._required_entry(self._codex_entry)
        third, third_show, third_decision = self._mcp_create_and_decide(
            codex,
            tuple(self.capture_ids[5:7]),
            title="Acceptance page three",
            body="Original body for page three.\n",
            edit="Edited exact body for page three.\n",
            key="acceptance-page-three",
        )
        self.pages.append(
            {
                "proposal": third,
                "shown": third_show,
                "decision": third_decision,
                "body": "Edited exact body for page three.\n",
            }
        )
        self.decisions.append(third_decision)

        page_ids = {_string(_object(page, "decision"), "page_id") for page in self.pages}
        canonical = _canonical_pages(self.brain)
        if len(page_ids) != 3 or len(canonical) != 3:
            raise AcceptanceFailure("explicit 2/3/2 grouping did not create exactly three pages")
        for page, expected in zip(
            self.pages,
            (self.capture_ids[0:2], self.capture_ids[2:5], self.capture_ids[5:7]),
            strict=True,
        ):
            self._assert_group(page, expected, expected)
        self.evidence["canonical_page_count"] = 3

    def _assert_group(
        self, page: Mapping[str, object], expected: list[str], selected: list[str]
    ) -> None:
        shown = _object(page, "shown")
        proposal_id = _string(shown, "proposal_id")
        proposal = _json_object(
            _only(self.brain / "history/proposals", proposal_id + ".json").read_text()
        )
        binding = _json_object(
            _only(self.brain / "history/review-bindings", proposal_id + ".json").read_text()
        )
        for record in (shown, proposal):
            if _strings(record, "capture_ids") != expected:
                raise AcceptanceFailure("proposal lost exact ordered source membership")
            evidence = record.get("evidence")
            if (
                not isinstance(evidence, list)
                or [_string(cast(dict[str, object], item), "capture_id") for item in evidence]
                != expected
            ):
                raise AcceptanceFailure("proposal evidence differs from selected group")
            for raw in evidence:
                item = cast(dict[str, object], raw)
                if hashlib.sha256(_string(item, "excerpt").encode()).hexdigest() != item.get(
                    "sha256"
                ):
                    raise AcceptanceFailure("proposal evidence digest is incorrect")
        states = binding.get("source_states")
        if (
            _strings(shown, "selected_capture_ids") != selected
            or _strings(binding, "selected_capture_ids") != selected
            or _strings(binding, "provenance") != expected
            or not isinstance(states, list)
            or [_string(cast(dict[str, object], item), "capture_id") for item in states] != expected
        ):
            raise AcceptanceFailure("review binding lost selected or cumulative sources")
        for raw in states:
            state = cast(dict[str, object], raw)
            capture_id = _string(state, "capture_id")
            if state.get("sha256") != hashlib.sha256(self.source_bytes[capture_id]).hexdigest():
                raise AcceptanceFailure("review binding does not match original source bytes")
        canonical = _only(self.brain / "content/spaces", _string(shown, "page_id") + ".md")
        fields, _ = _parse_markdown(canonical.read_bytes())
        if fields.get("provenance") != expected or fields.get("space_id") != self.space_id:
            raise AcceptanceFailure("canonical page lost structured source provenance")

    def _update_page(self) -> None:
        first_page_id = _string(_object(self.pages[0], "decision"), "page_id")
        old_decision = _object(self.pages[0], "decision")
        old_path = _only(self.brain / "content/spaces", first_page_id + ".md")
        old_bytes = old_path.read_bytes()
        proposed = self._propose_cli(
            (self.capture_ids[7],),
            title="Acceptance page one",
            body="Updated exact body for page one.\n",
            key="acceptance-page-one-update",
            target_page_id=first_page_id,
        )
        shown = self._cli("review", "show", _string(proposed, "proposal_id"))
        if (
            shown.get("operation") != "update"
            or shown.get("target_page_id") != first_page_id
            or _strings(shown, "selected_capture_ids") != [self.capture_ids[7]]
            or _strings(shown, "capture_ids") != [*self.capture_ids[0:2], self.capture_ids[7]]
        ):
            raise AcceptanceFailure("update did not expose ordered cumulative provenance")
        decision = self._cli(
            "review",
            "approve",
            _string(proposed, "proposal_id"),
            "--review-token",
            _string(shown, "review_token"),
            "--idempotency-key",
            "acceptance-page-one-update-approve",
        )
        if decision.get("page_id") != first_page_id:
            raise AcceptanceFailure("update changed canonical page identity")
        self.pages[0] = {
            "proposal": proposed,
            "shown": shown,
            "decision": decision,
            "body": "Updated exact body for page one.\n",
        }
        self.decisions.append(decision)
        self._assert_group(
            self.pages[0], [*self.capture_ids[0:2], self.capture_ids[7]], [self.capture_ids[7]]
        )
        binding = _json_object(
            _only(
                self.brain / "history/review-bindings", _string(proposed, "proposal_id") + ".json"
            ).read_text()
        )
        if (
            binding.get("expected_publication_id") != old_decision.get("publication_id")
            or binding.get("expected_page_sha256") != hashlib.sha256(old_bytes).hexdigest()
            or _only(self.brain / "content/spaces", first_page_id + ".md") != old_path
        ):
            raise AcceptanceFailure("update lost its exact predecessor or canonical location")
        if len(_canonical_pages(self.brain)) != 3:
            raise AcceptanceFailure("update changed canonical page count")

    def _verify_artifacts(self) -> None:
        self._cli("export", str(self.root / "verified-export"), "--verify")
        if self._cli("status").get("portable_export") != "verified":
            raise AcceptanceFailure("verified export evidence is unavailable")
        for capture_id, expected in self.source_bytes.items():
            actual = _only(self.brain / "sources/captures", f"{capture_id}.json").read_bytes()
            if actual != expected:
                raise AcceptanceFailure("immutable source capture bytes changed")

        artifact_paths = tuple(
            sorted(
                (
                    *self.brain.glob("sources/captures/*/*/*.json"),
                    *_canonical_pages(self.brain),
                    *self.brain.glob("history/proposals/*/*/*.json"),
                    *self.brain.glob("history/review-bindings/*/*/*.json"),
                    *self.brain.glob("history/decisions/*/*/*.json"),
                    *self.brain.glob("history/publications/*/*/*.json"),
                ),
                key=lambda path: path.relative_to(self.brain).as_posix(),
            )
        )
        manifest: dict[str, str] = {}
        for path in artifact_paths:
            payload = path.read_bytes()
            manifest[path.relative_to(self.brain).as_posix()] = hashlib.sha256(payload).hexdigest()
            if path.suffix == ".json":
                value = json.loads(payload)
                if not isinstance(value, dict) or _canonical_json(value) != payload:
                    raise AcceptanceFailure(
                        "portable history or source JSON bytes are not canonical"
                    )

        publications = _records(self.brain / "history/publications")
        decisions = _records(self.brain / "history/decisions")
        bindings = _records(self.brain / "history/review-bindings")
        if len(publications) != 4 or len(decisions) != 5 or len(bindings) != 5:
            raise AcceptanceFailure("portable review history counts are incorrect")
        publications_by_id = {_string(item, "publication_id"): item for item in publications}
        decisions_by_id = {_string(item, "decision_id"): item for item in decisions}
        bindings_by_proposal = {_string(item, "proposal_id"): item for item in bindings}

        for public in self.decisions:
            decision_id = _string(public, "decision_id")
            history = decisions_by_id.get(decision_id)
            if history is None or history.get("proposal_id") != public.get("proposal_id"):
                raise AcceptanceFailure("decision public identity disagrees with portable history")
            proposal_id = _string(public, "proposal_id")
            if proposal_id not in bindings_by_proposal:
                raise AcceptanceFailure("decision has no exact review binding")
            publication_id = public.get("publication_id")
            if publication_id is None:
                if public.get("page_id") is not None:
                    raise AcceptanceFailure("rejected decision unexpectedly identifies a page")
                continue
            if not isinstance(publication_id, str):
                raise AcceptanceFailure("publication identity is invalid")
            publication = publications_by_id.get(publication_id)
            if (
                publication is None
                or publication.get("decision_id") != decision_id
                or publication.get("page_id") != public.get("page_id")
            ):
                raise AcceptanceFailure("publication public identity disagrees with history")
            published = base64.b64decode(
                _string(publication, "published_bytes_base64"), validate=True
            )
            if hashlib.sha256(published).hexdigest() != publication.get("published_sha256"):
                raise AcceptanceFailure("publication bytes disagree with their digest")

        for page in self.pages:
            decision = _object(page, "decision")
            page_id = _string(decision, "page_id")
            canonical = _only(self.brain / "content/spaces", f"{page_id}.md")
            fields, body = _parse_markdown(canonical.read_bytes())
            if fields.get("page_id") != page_id or body != page.get("body"):
                raise AcceptanceFailure("canonical page bytes disagree with reviewed content")
            publication_id = _string(decision, "publication_id")
            publication = publications_by_id[publication_id]
            if (
                base64.b64decode(_string(publication, "published_bytes_base64"))
                != canonical.read_bytes()
            ):
                raise AcceptanceFailure("canonical bytes disagree with head publication bytes")

        self.evidence.update(
            {
                "artifact_manifest": manifest,
                "capture_count": len(self.capture_ids),
                "canonical_page_count": 3,
                "decision_history_count": len(decisions),
                "publication_history_count": len(publications),
                "review_binding_count": len(bindings),
                "same_title_unselected_capture_id": self.capture_ids[7],
                "selected_groups": [
                    self.capture_ids[0:2],
                    self.capture_ids[2:5],
                    self.capture_ids[5:7],
                ],
            }
        )

    def _configure(self, client: str, project: Path, grants: Sequence[str]) -> dict[str, object]:
        flags = tuple(f"--allow-review-{grant}" for grant in grants)
        arguments = (
            "agent",
            "setup",
            "--client",
            client,
            "--scope",
            "project",
            "--project-dir",
            os.fspath(project),
            "--runtime",
            os.fspath(self.executable),
            *flags,
        )
        preview = self._cli(*arguments)
        applied = self._cli(
            *arguments,
            "--apply",
            "--preview-id",
            _string(preview, "preview_id"),
        )
        if applied.get("status") not in {"configured", "applied"}:
            raise AcceptanceFailure("agent setup did not report an applied configuration")
        if client == "claude-code":
            config = json.loads((project / ".mcp.json").read_bytes())
            servers = _object(cast(dict[str, object], config), "mcpServers")
            return cast(dict[str, object], servers["open-brain"])
        config = tomllib.loads((project / ".codex/config.toml").read_text(encoding="utf-8"))
        servers = _object(cast(dict[str, object], config), "mcp_servers")
        return cast(dict[str, object], servers["open-brain"])

    def _mcp_create_and_decide(
        self,
        entry: Mapping[str, object],
        captures: tuple[str, ...],
        *,
        title: str,
        body: str,
        edit: str | None,
        key: str,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        _, proposed_responses = self._mcp(
            entry,
            (
                (
                    "brain_review_propose",
                    {
                        "capture_ids": list(captures),
                        "title": title,
                        "markdown": body,
                        "idempotency_key": key,
                    },
                ),
            ),
        )
        proposed = _mcp_content(proposed_responses[0])
        _, shown_responses = self._mcp(
            entry, (("brain_review_show", {"proposal_id": _string(proposed, "proposal_id")}),)
        )
        shown = _mcp_content(shown_responses[0])
        name = "brain_review_approve" if edit is None else "brain_review_edit_and_approve"
        arguments: dict[str, object] = {
            "proposal_id": _string(proposed, "proposal_id"),
            "review_token": _string(shown, "review_token"),
            "idempotency_key": key + "-decision",
        }
        if edit is not None:
            arguments["markdown"] = edit
        _, decided_responses = self._mcp(entry, ((name, arguments),))
        return proposed, shown, _mcp_content(decided_responses[0])

    def _mcp(
        self,
        entry: Mapping[str, object],
        calls: Sequence[tuple[str, Mapping[str, object]]],
    ) -> tuple[set[str], list[dict[str, object]]]:
        command = _string(entry, "command")
        args = entry.get("args")
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise AcceptanceFailure("generated MCP arguments are invalid")
        requests: list[dict[str, object]] = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        requests.extend(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments)},
            }
            for index, (name, arguments) in enumerate(calls, start=3)
        )
        process = self._process(
            (command, *cast(list[str], args)),
            input_text="".join(json.dumps(request) + "\n" for request in requests),
        )
        try:
            responses = {
                cast(int, response["id"]): response
                for line in process.stdout.splitlines()
                if isinstance((response := json.loads(line)), dict) and "id" in response
            }
        except KeyError, TypeError, ValueError, json.JSONDecodeError:
            raise AcceptanceFailure(
                "generated MCP subprocess returned invalid protocol output"
            ) from None
        tools_value = _object(responses[2], "result").get("tools")
        if not isinstance(tools_value, list):
            raise AcceptanceFailure("generated MCP subprocess omitted its tool list")
        tool_names = {_string(cast(dict[str, object], tool), "name") for tool in tools_value}
        return tool_names, [responses[index] for index in range(3, 3 + len(calls))]

    def _propose_cli(
        self,
        captures: tuple[str, ...],
        *,
        title: str,
        body: str,
        key: str,
        target_page_id: str | None = None,
    ) -> dict[str, object]:
        draft = self.drafts / f"{key}.md"
        draft.write_text(body, encoding="utf-8")
        capture_arguments = tuple(
            argument for capture_id in captures for argument in ("--capture-id", capture_id)
        )
        target = () if target_page_id is None else ("--target-page-id", target_page_id)
        return self._cli(
            "review",
            "propose",
            *capture_arguments,
            "--title",
            title,
            "--markdown-file",
            os.fspath(draft),
            *target,
            "--idempotency-key",
            key,
        )

    def _cli(self, *arguments: str) -> dict[str, object]:
        command = (
            os.fspath(self.executable),
            *arguments,
            "--data-dir",
            os.fspath(self.brain),
            "--json",
        )
        process = self._process(command)
        return _json_object(process.stdout)

    def _cli_failure(self, *arguments: str) -> dict[str, object]:
        command = (
            os.fspath(self.executable),
            *arguments,
            "--data-dir",
            os.fspath(self.brain),
            "--json",
        )
        process = self._process(command, expected_success=False)
        if process.returncode == 0:
            raise AcceptanceFailure("candidate command unexpectedly succeeded")
        return _json_object(process.stdout)

    def _process(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        expected_success: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                env=self.environment,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            raise AcceptanceFailure("candidate subprocess could not complete") from None
        if expected_success and result.returncode != 0:
            raise AcceptanceFailure(f"candidate subprocess failed with exit {result.returncode}")
        return result

    @staticmethod
    def _required_entry(value: dict[str, object] | None) -> dict[str, object]:
        if value is None:
            raise AcceptanceFailure("generated MCP configuration is unavailable")
        return value


def _mcp_content(response: Mapping[str, object]) -> dict[str, object]:
    result = _object(response, "result")
    if result.get("isError"):
        raise AcceptanceFailure("granted MCP review call returned an error")
    return _object(result, "structuredContent")


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise AcceptanceFailure("candidate command returned invalid JSON") from None
    if not isinstance(parsed, dict):
        raise AcceptanceFailure("candidate command returned a non-object")
    return cast(dict[str, object], parsed)


def _object(value: Mapping[str, object], key: str) -> dict[str, object]:
    selected = value.get(key)
    if not isinstance(selected, dict):
        raise AcceptanceFailure(f"candidate response omitted {key}")
    return cast(dict[str, object], selected)


def _string(value: Mapping[str, object], key: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise AcceptanceFailure(f"candidate response omitted {key}")
    return selected


def _strings(value: Mapping[str, object], key: str) -> list[str]:
    selected = value.get(key)
    if not isinstance(selected, (list, tuple)) or any(
        not isinstance(item, str) for item in selected
    ):
        raise AcceptanceFailure(f"candidate response omitted {key}")
    return list(cast(Sequence[str], selected))


def _only(root: Path, name: str) -> Path:
    matches = tuple(root.rglob(name)) if root.is_dir() else ()
    if len(matches) != 1:
        raise AcceptanceFailure("portable artifact identity is missing or ambiguous")
    return matches[0]


def _canonical_pages(brain: Path) -> tuple[Path, ...]:
    root = brain / "content/spaces"
    return tuple(sorted(root.rglob("page_*.md"))) if root.is_dir() else ()


def _records(root: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for path in sorted(root.rglob("*.json")):
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise AcceptanceFailure("portable history record is not an object")
        result.append(cast(dict[str, object], value))
    return result


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _parse_markdown(payload: bytes) -> tuple[dict[str, object], str]:
    try:
        text = payload.decode("utf-8")
        frontmatter, body = text.removeprefix("---\n").split("\n---\n\n", 1)
        fields = {
            key: json.loads(encoded)
            for key, encoded in (line.split(": ", 1) for line in frontmatter.splitlines())
        }
    except UnicodeDecodeError, ValueError, json.JSONDecodeError:
        raise AcceptanceFailure("canonical page bytes are invalid") from None
    return fields, body


def _safe_error(error: Exception) -> str:
    if isinstance(error, AcceptanceFailure):
        return str(error)
    return f"unexpected {type(error).__name__}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_report(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    rendered = json.dumps(dict(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    temporary.write_bytes(rendered)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.open_brain_dev.review_publish_acceptance"
    )
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args(argv)
    executable = cast(Path, parsed.executable)
    output = cast(Path, parsed.output)
    if not executable.is_absolute() or not output.is_absolute():
        parser.error("--executable and --output must be absolute paths")
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="review-publish-evidence-", dir=output.parent))
    root.chmod(0o700)
    acceptance = _Acceptance(executable, output, root)
    started = time.monotonic()
    status = "passed"
    failure: str | None = None
    try:
        acceptance.run()
    except Exception as error:
        status = "failed"
        failure = _safe_error(error)
    skipped = sum(check["status"] == "skipped" for check in acceptance.checks)
    acceptance.evidence["skipped_mandatory_checks"] = skipped
    if len(acceptance.checks) != len(_CHECK_NAMES) or any(
        check["status"] != "passed" for check in acceptance.checks
    ):
        status = "failed"
    failed_checks = [check["name"] for check in acceptance.checks if check["status"] == "failed"]
    report: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "executable": os.fspath(executable),
        "executable_sha256": _sha256(executable) if executable.is_file() else None,
        "synthetic_root": os.fspath(root),
        "evidence_paths": {
            "brain": os.fspath(acceptance.brain),
            "claude_config": os.fspath(acceptance.claude_project / ".mcp.json"),
            "codex_config": os.fspath(acceptance.codex_project / ".codex/config.toml"),
        },
        "checks": acceptance.checks,
        "failed_checks": failed_checks,
        "mandatory_check_count": len(_CHECK_NAMES),
        "evidence": acceptance.evidence,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
    if failure is not None:
        report["failure"] = failure
    _write_report(output, report)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
