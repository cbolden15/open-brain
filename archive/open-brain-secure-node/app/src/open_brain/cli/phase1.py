"""Thin CLI representations for the Phase 1 engine task facades."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import cast

from open_brain_engine.engine import (
    CaptureAction,
    CaptureTask,
    DecisionOutcome,
    EventPayload,
    FilePayload,
    InboxSpaceTask,
    MeasurementPayload,
    Phase1TaskSet,
    ReferencePayload,
    RetrievalTask,
    ReviewTask,
    TextPayload,
    project_public_space,
)

from open_brain.cli._common import CommandDispatchResult, ExitCode, redacted_error
from open_brain.cli.phase1_registry import Phase1CommandAdapterRegistry


@dataclass(frozen=True, slots=True)
class Phase1CliResult:
    exit_code: ExitCode
    envelope: dict[str, object]


@dataclass(frozen=True, slots=True)
class Phase1CommandAdapter:
    family: str
    task: CaptureTask | InboxSpaceTask | ReviewTask | RetrievalTask

    def dispatch(self, argv: tuple[str, ...]) -> CommandDispatchResult:
        try:
            positional, options, flags = _request(argv)
            if "dry-run" in flags:
                return _invalid(self.family)
            if self.family == "capture":
                return self._capture(positional, options, flags)
            if self.family == "inbox":
                return self._inbox(positional, options, flags)
            if self.family == "spaces":
                return self._spaces(positional, options, flags)
            if self.family == "proposals":
                return self._proposals(positional, options, flags)
            if self.family == "review":
                return self._review(positional, options, flags)
            if self.family == "query":
                return self._query(positional, options, flags)
        except Exception:
            return _failed(self.family)
        return _invalid(self.family)

    def _capture(
        self,
        positional: tuple[str, ...],
        options: dict[str, str],
        flags: frozenset[str],
    ) -> Phase1CliResult:
        if len(positional) != 3 or positional[0] not in {"quick", "canonical"} or flags:
            return _invalid("capture")
        payload_kind = positional[1]
        common_options = {"delivery", "space", "intent", "why", "title"}
        payload_options = {
            "text": set(),
            "reference": {"supplied-text"},
            "file": {"data-base64", "media-type"},
            "event": {"attributes-json", "occurrence-at"},
            "measurement": {"dimensions-json", "occurrence-at", "unit"},
        }
        required_options = {
            "text": set(),
            "reference": set(),
            "file": {"data-base64", "media-type"},
            "event": set(),
            "measurement": {"unit"},
        }
        if (
            payload_kind not in payload_options
            or set(options) - common_options - payload_options[payload_kind]
            or not {"delivery", *required_options[payload_kind]} <= set(options)
            or positional[0] == "canonical"
            and (payload_kind != "text" or "space" not in options)
        ):
            return _invalid("capture")
        try:
            payload = _capture_payload(payload_kind, positional[2], options)
        except (ValueError, binascii.Error, json.JSONDecodeError):
            return _invalid("capture")
        action = (
            CaptureAction.CANONICAL_NOTE
            if positional[0] == "canonical"
            else CaptureAction.QUICK
        )
        receipt = cast(CaptureTask, self.task).accept(
            payload,
            delivery_id=options["delivery"],
            action=action,
            space_id=options.get("space"),
            intent=options.get("intent"),
            capture_why=options.get("why"),
            title=options.get("title"),
        )
        return Phase1CliResult(
            ExitCode.SUCCESS,
            {
                "canonical": receipt.canonical_path is not None,
                "capture_id": receipt.capture_id,
                "command": "capture",
                "duplicate": receipt.duplicate,
                "enrichment_state": receipt.enrichment_state,
                "payload_family": receipt.payload_family,
                "space_id": receipt.space_id,
                "state": receipt.state,
                "status": "accepted",
            },
        )

    def _inbox(
        self,
        positional: tuple[str, ...],
        options: dict[str, str],
        flags: frozenset[str],
    ) -> Phase1CliResult:
        if positional not in {(), ("list",)} or options or flags - {"unassigned"}:
            return _invalid("inbox")
        items = cast(InboxSpaceTask, self.task).list(unassigned_only="unassigned" in flags)
        return Phase1CliResult(
            ExitCode.SUCCESS,
            {
                "command": "inbox",
                "captures": [
                    {
                        "capture_id": item.capture_id,
                        "payload_family": item.payload_family,
                        "space_id": item.space_id,
                        "state": item.state,
                    }
                    for item in items
                ],
                "status": "listed",
            },
        )

    def _spaces(
        self,
        positional: tuple[str, ...],
        options: dict[str, str],
        flags: frozenset[str],
    ) -> Phase1CliResult:
        if flags or not positional:
            return _invalid("spaces")
        action = positional[0]
        if action == "list" and len(positional) == 1 and not options:
            spaces = cast(InboxSpaceTask, self.task).spaces()
            return Phase1CliResult(
                ExitCode.SUCCESS,
                {
                    "command": "spaces",
                    "spaces": [
                        {
                            "name": projected.name,
                            "slug": projected.slug,
                            "space_id": projected.space_id,
                        }
                        for space in spaces
                        for projected in (project_public_space(space),)
                    ],
                    "status": "listed",
                },
            )
        if set(options) != {"delivery"}:
            return _invalid("spaces")
        if action == "create" and len(positional) == 2:
            space = cast(InboxSpaceTask, self.task).create_space(
                positional[1], delivery_id=options["delivery"]
            )
            status = "created"
        elif action == "rename" and len(positional) == 3:
            space = cast(InboxSpaceTask, self.task).rename_space(
                positional[1], positional[2], delivery_id=options["delivery"]
            )
            status = "renamed"
        elif action == "route" and len(positional) == 3:
            routed = cast(InboxSpaceTask, self.task).route(
                positional[1], positional[2], delivery_id=options["delivery"]
            )
            return Phase1CliResult(
                ExitCode.SUCCESS,
                {
                    "capture_id": routed.capture_id,
                    "command": "spaces",
                    "space_id": routed.space_id,
                    "status": "routed",
                },
            )
        else:
            return _invalid("spaces")
        return Phase1CliResult(
            ExitCode.SUCCESS,
            {"command": "spaces", "space_id": space.space_id, "status": status},
        )

    def _proposals(
        self,
        positional: tuple[str, ...],
        options: dict[str, str],
        flags: frozenset[str],
    ) -> Phase1CliResult:
        if positional not in {(), ("list",)} or flags or set(options) - {"capture", "status"}:
            return _invalid("proposals")
        proposals = cast(ReviewTask, self.task).list(
            capture_id=options.get("capture"), status=options.get("status")
        )
        return Phase1CliResult(
            ExitCode.SUCCESS,
            {
                "command": "proposals",
                "proposals": [
                    {
                        "capture_id": proposal.capture_id,
                        "decision_id": proposal.terminal_decision_id,
                        "proposal_id": proposal.proposal_id,
                        "space_id": proposal.space_id,
                        "state": proposal.status,
                    }
                    for proposal in proposals
                ],
                "status": "listed",
            },
        )

    def _review(
        self,
        positional: tuple[str, ...],
        options: dict[str, str],
        flags: frozenset[str],
    ) -> Phase1CliResult:
        if (
            flags
            or len(positional) not in {2, 3}
            or positional[0] not in {"approve", "reject", "edit"}
            or set(options) != {"delivery"}
            or (positional[0] == "edit") != (len(positional) == 3)
        ):
            return _invalid("review")
        outcome = {
            "approve": DecisionOutcome.APPROVED,
            "reject": DecisionOutcome.REJECTED,
            "edit": DecisionOutcome.EDITED,
        }[positional[0]]
        decision = cast(ReviewTask, self.task).decide(
            positional[1],
            outcome,
            delivery_id=options["delivery"],
            edited_markdown=positional[2] if len(positional) == 3 else None,
        )
        return Phase1CliResult(
            ExitCode.SUCCESS,
            {
                "action": positional[0],
                "command": "review",
                "decision_id": decision.decision_id,
                "duplicate": decision.duplicate,
                "page_id": decision.page_id,
                "proposal_id": decision.proposal_id,
                "publication_id": decision.publication_id,
                "state": decision.outcome.value,
                "status": "decided",
            },
        )

    def _query(
        self,
        positional: tuple[str, ...],
        options: dict[str, str],
        flags: frozenset[str],
    ) -> Phase1CliResult:
        if len(positional) != 1 or flags or set(options) - {"space", "family", "type", "limit"}:
            return _invalid("query")
        limit = int(options.get("limit", "10"))
        results = cast(RetrievalTask, self.task).search(
            positional[0],
            space_id=options.get("space"),
            payload_family=options.get("family"),
            record_type=options.get("type"),
            limit=limit,
        )
        return Phase1CliResult(
            ExitCode.SUCCESS,
            {
                "command": "query",
                "results": [
                    {
                        "capture_id": result.capture_id,
                        "excerpt": result.excerpt,
                        "explanation": result.explanation,
                        "payload_family": result.payload_family,
                        "provenance": result.provenance.as_dict(),
                        "record_type": result.record_type,
                        "result_id": result.result_id,
                        "space_id": result.space_id,
                        "title": result.title,
                        "trust": result.trust,
                    }
                    for result in results
                ],
                "status": "ok",
            },
        )


def build_phase1_command_adapters(tasks: Phase1TaskSet) -> Phase1CommandAdapterRegistry:
    if not isinstance(tasks, Phase1TaskSet):
        raise ValueError("invalid Phase 1 tasks")
    tasks_by_family: dict[str, CaptureTask | InboxSpaceTask | ReviewTask | RetrievalTask] = {
        "capture": tasks.capture,
        "inbox": tasks.inbox,
        "proposals": tasks.review,
        "query": tasks.retrieval,
        "review": tasks.review,
        "spaces": tasks.inbox,
    }
    return Phase1CommandAdapterRegistry(
        {
            family: Phase1CommandAdapter(family=family, task=task)
            for family, task in tasks_by_family.items()
        }
    )


def _capture_payload(
    kind: str,
    value: str,
    options: dict[str, str],
) -> TextPayload | ReferencePayload | FilePayload | EventPayload | MeasurementPayload:
    if kind == "text":
        return TextPayload(value)
    if kind == "reference":
        return ReferencePayload(value, options.get("supplied-text"))
    if kind == "file":
        return FilePayload(
            value,
            options["media-type"],
            base64.b64decode(options["data-base64"], validate=True),
        )
    if kind == "event":
        return EventPayload(
            value,
            options.get("occurrence-at"),
            _string_mapping(options.get("attributes-json", "{}")),
        )
    if kind == "measurement":
        return MeasurementPayload(
            value,
            options["unit"],
            options.get("occurrence-at"),
            _string_mapping(options.get("dimensions-json", "{}")),
        )
    raise ValueError("invalid capture payload")


def _string_mapping(value: str) -> dict[str, str]:
    parsed = json.loads(value, object_pairs_hook=_unique_object)
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in parsed.items()
    ):
        raise ValueError("invalid string mapping")
    return parsed


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _request(
    argv: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, str], frozenset[str]]:
    if not isinstance(argv, tuple) or any(
        not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv
    ):
        raise ValueError("invalid Phase 1 request")
    positional: list[str] = []
    options: dict[str, str] = {}
    flags: set[str] = set()
    for argument in argv:
        if argument == "--json":
            continue
        if argument.startswith("--"):
            key, marker, value = argument[2:].partition("=")
            if marker:
                if not key or not value or key in options or key in flags:
                    raise ValueError("invalid Phase 1 request")
                options[key] = value
            else:
                if not key or key in flags or key in options:
                    raise ValueError("invalid Phase 1 request")
                flags.add(key)
            continue
        if argument.startswith("-"):
            raise ValueError("invalid Phase 1 request")
        positional.append(argument)
    return tuple(positional), options, frozenset(flags)


def _invalid(command: str) -> Phase1CliResult:
    return Phase1CliResult(
        ExitCode.USAGE,
        {
            "command": command,
            "error": redacted_error("invalid_phase1_request"),
            "status": "invalid",
        },
    )


def _failed(command: str) -> Phase1CliResult:
    return Phase1CliResult(
        ExitCode.FAILURE,
        {
            "command": command,
            "error": redacted_error("phase1_operation_failed"),
            "status": "failed",
        },
    )
