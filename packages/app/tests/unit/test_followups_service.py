from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.followup_contracts import (
    AddEvidenceRequest,
    DecideRequest,
    DecisionAction,
    EvidenceRole,
    FollowupError,
    FollowupKind,
    ListFilter,
    OwnerKind,
    ProposeRequest,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.services.followups import (
    FOLLOWUP_ERROR_CODES,
    MAX_FOLLOWUP_LIST_JSON_BYTES,
    FollowupServiceError,
    bind_followup_operations,
    followup_add_evidence,
    followup_decide,
    followup_list,
    followup_propose,
    followup_session_authority,
    followup_show,
    validate_followup_arguments,
)
from open_brain.services.t03_adapters import agent_authority, owner_authority_for_principal

ITEM = "followup_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
OTHER_ITEM = "followup_8d87546c-3008-42ee-8632-0f2401904b35"
CAPTURE = "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
REVISION = "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
DIGEST = "a" * 64
EVIDENCE = {
    "capture_id": CAPTURE,
    "revision_id": REVISION,
    "quote_start": 0,
    "quote_end": 12,
    "quote_sha256": DIGEST,
    "role": "origin",
}
OWNER = EffectiveAuthority("synthetic-owner", "synthetic-session", frozenset(), None, owner=True)
PROPOSE = {"operation_id": "op", "kind": "commitment", "summary": "x", "owner": "self"}
DECIDE = {"operation_id": "op", "item_id": ITEM, "expected_version": 1}


@dataclass(frozen=True)
class _Item:
    item_id: str
    kind: FollowupKind
    state: str
    summary: str | None
    project: str | None
    owner: OwnerKind
    due: date | None
    version: int
    evidence_retracted: bool = False
    new_evidence_since_decision: bool = False


class _FakeFollowupTasks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, EffectiveAuthority]] = []
        self.list_result: tuple[tuple[_Item, ...], str | None] = (
            (
                _Item(
                    ITEM,
                    FollowupKind.COMMITMENT,
                    "suggested",
                    "Send the deck",
                    "synthetic",
                    OwnerKind.SELF,
                    None,
                    1,
                ),
            ),
            None,
        )
        self.error: FollowupError | None = None

    def _record(self, name: str, request: Any, authority: EffectiveAuthority) -> None:
        self.calls.append((name, request, authority))
        if self.error is not None:
            raise self.error

    def list(self, filter_: ListFilter, *, authority: EffectiveAuthority) -> Any:
        self._record("list", filter_, authority)
        return self.list_result

    def show(self, item_id: str, *, authority: EffectiveAuthority) -> Any:
        self._record("show", item_id, authority)
        return self.list_result[0][0], (), ()

    def propose(self, request: ProposeRequest, *, authority: EffectiveAuthority) -> Any:
        self._record("propose", request, authority)
        return self.list_result[0][0]

    def add_evidence(self, request: AddEvidenceRequest, *, authority: EffectiveAuthority) -> Any:
        self._record("add_evidence", request, authority)
        return self.list_result[0][0]

    def decide(self, request: DecideRequest, *, authority: EffectiveAuthority) -> Any:
        self._record("decide", request, authority)
        return self.list_result[0][0]


def test_error_code_set_is_exactly_the_spec_table() -> None:
    expected = frozenset(
        {
            "evidence_mismatch",
            "not_found",
            "tier_mismatch",
            "version_conflict",
            "operation_conflict",
            "duplicate_item",
            "invalid_request",
            "capacity_reached",
        }
    )
    assert expected == FOLLOWUP_ERROR_CODES


@pytest.mark.parametrize(
    ("operation", "arguments"),
    (
        ("list", {"unknown": 1}),
        ("list", {"limit": 0}),
        ("list", {"limit": 101}),
        ("list", {"kinds": ["commitment", "meeting"]}),
        ("list", {"due_before": "2026-13-01"}),
        ("list", {"include_closed": 1}),
        ("list", {"project": "p" * 129}),
        ("show", {}),
        ("show", {"item_id": "proposal_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"}),
        ("propose", PROPOSE),
        ("propose", {**PROPOSE, "evidence": []}),
        ("propose", {**PROPOSE, "owner": "other", "evidence": [EVIDENCE]}),
        ("propose", {**PROPOSE, "summary": "x" * 281, "evidence": [EVIDENCE]}),
        ("propose", {**PROPOSE, "evidence": [{**EVIDENCE, "quote_end": 0}]}),
        ("propose", {**PROPOSE, "evidence": [{**EVIDENCE, "extra": 1}]}),
        ("propose", {**PROPOSE, "evidence": [EVIDENCE], "due_evidence_index": 1}),
        ("propose", {**PROPOSE, "evidence": [EVIDENCE], "project": "p" * 129}),
        (
            "propose",
            {**PROPOSE, "owner": "other", "owner_name": "n" * 129, "evidence": [EVIDENCE]},
        ),
        ("add_evidence", {"operation_id": "op", "item_id": ITEM}),
        ("decide", {**DECIDE, "action": "approve"}),
        ("decide", {**DECIDE, "expected_version": -1, "action": "confirm"}),
        ("decide", {**DECIDE, "expected_version": 0, "action": "confirm"}),
        ("decide", {**DECIDE, "action": "snooze"}),
        ("decide", {**DECIDE, "action": "confirm", "snoozed_until": "2027-01-01"}),
        ("decide", {**DECIDE, "action": "link"}),
        ("decide", {**DECIDE, "action": "link", "ticket_key": "k" * 201}),
        (
            "decide",
            {
                **DECIDE,
                "action": "link",
                "ticket_key": "k",
                "ticket_url": "https://example.test/" + "u" * 2028,
            },
        ),
        ("decide", {**DECIDE, "action": "supersede"}),
        ("decide", {**DECIDE, "action": "correct", "corrections": {}}),
        ("decide", {**DECIDE, "action": "correct", "corrections": {"ticket_key": "x"}}),
        ("decide", {**DECIDE, "action": "correct", "corrections": {"project": "p" * 129}}),
        ("decide", {**DECIDE, "action": "resolve", "resolution": "guess"}),
        ("unknown", {}),
    ),
)
def test_closed_shapes_reject_unknown_or_malformed_arguments(
    operation: str, arguments: dict[str, object]
) -> None:
    with pytest.raises(FollowupServiceError, match="^invalid_arguments$"):
        validate_followup_arguments(operation, arguments)


@pytest.mark.parametrize(
    ("operation", "arguments"),
    (
        ("list", {"project": "p" * 128}),
        ("propose", {**PROPOSE, "evidence": [EVIDENCE], "project": "p" * 128}),
        (
            "propose",
            {**PROPOSE, "owner": "other", "owner_name": "n" * 128, "evidence": [EVIDENCE]},
        ),
        ("propose", {**PROPOSE, "summary": "x" * 280, "evidence": [EVIDENCE]}),
        (
            "decide",
            {
                **DECIDE,
                "action": "link",
                "ticket_key": "k" * 200,
                "ticket_url": "https://example.test/" + "u" * 2027,
            },
        ),
        ("decide", {**DECIDE, "action": "correct", "corrections": {"project": "p" * 128}}),
    ),
)
def test_field_limits_match_the_engine_contracts(
    operation: str, arguments: dict[str, object]
) -> None:
    validate_followup_arguments(operation, arguments)


def test_list_builds_filter_and_returns_json_safe_rows() -> None:
    tasks = _FakeFollowupTasks()
    result = followup_list(
        tasks,  # type: ignore[arg-type]
        {
            "kinds": ["question"],
            "states": ["snoozed"],
            "owner": "other",
            "project": "p",
            "due_before": "2026-12-01",
            "include_closed": True,
            "limit": 10,
        },
        authority=OWNER,
    )
    name, filter_, authority = tasks.calls[0]
    assert (name, authority) == ("list", OWNER)
    assert isinstance(filter_, ListFilter)
    assert filter_.kinds == frozenset({FollowupKind.QUESTION})
    assert filter_.due_before == date(2026, 12, 1)
    assert filter_.include_closed is True and filter_.limit == 10 and filter_.cursor is None
    assert result["outcome"] == "ok" and result["code"] is None
    data = result["data"]
    assert isinstance(data, dict)
    assert data["next_cursor"] is None and data["truncated"] is False
    assert data["items"] == [
        {
            "item_id": ITEM,
            "kind": "commitment",
            "state": "suggested",
            "summary": "Send the deck",
            "project": "synthetic",
            "owner": "self",
            "due": None,
            "version": 1,
            "evidence_retracted": False,
            "new_evidence_since_decision": False,
        }
    ]


def test_list_without_filters_uses_empty_sets() -> None:
    tasks = _FakeFollowupTasks()
    assert followup_list(tasks, {}, authority=OWNER)["outcome"] == "ok"  # type: ignore[arg-type]
    filter_ = tasks.calls[0][1]
    assert filter_.kinds == frozenset() and filter_.states == frozenset()
    assert filter_.limit == 50


def test_list_output_is_byte_bounded_and_marks_truncation() -> None:
    tasks = _FakeFollowupTasks()
    big = tuple(
        _Item(
            ITEM,
            FollowupKind.COMMITMENT,
            "suggested",
            "s" * 280,
            "p" * 128,
            OwnerKind.SELF,
            None,
            i,
        )
        for i in range(1000)
    )
    tasks.list_result = (big, "cursor-next")
    data = followup_list(tasks, {"limit": 100}, authority=OWNER)["data"]  # type: ignore[arg-type]
    assert isinstance(data, dict)
    assert 0 < len(data["items"]) < 1000
    assert data["truncated"] is True and data["next_cursor"] == "cursor-next"
    assert len(json.dumps(data, separators=(",", ":"))) <= MAX_FOLLOWUP_LIST_JSON_BYTES


def test_propose_builds_typed_request() -> None:
    tasks = _FakeFollowupTasks()
    result = followup_propose(
        tasks,  # type: ignore[arg-type]
        {
            "operation_id": "op-1",
            "kind": "question",
            "summary": "Which vendor?",
            "owner": "other",
            "owner_name": "Synthetic Person",
            "project": "proj",
            "due": "2026-11-30",
            "due_evidence_index": 0,
            "evidence": [EVIDENCE],
        },
        authority=OWNER,
    )
    assert result["outcome"] == "ok"
    request = tasks.calls[0][1]
    assert isinstance(request, ProposeRequest)
    assert request.kind is FollowupKind.QUESTION and request.owner is OwnerKind.OTHER
    assert request.due == date(2026, 11, 30) and request.due_evidence_index == 0
    assert request.evidence[0].role is EvidenceRole.ORIGIN and request.evidence[0].quote_end == 12


def test_decide_builds_typed_request_and_passes_authority() -> None:
    tasks = _FakeFollowupTasks()
    result = followup_decide(
        tasks,  # type: ignore[arg-type]
        {
            "operation_id": "op-2",
            "item_id": ITEM,
            "expected_version": 3,
            "action": "snooze",
            "snoozed_until": "2027-01-15",
        },
        authority=OWNER,
    )
    assert result["outcome"] == "ok"
    request = tasks.calls[0][1]
    assert isinstance(request, DecideRequest)
    assert request.action is DecisionAction.SNOOZE and request.snoozed_until == date(2027, 1, 15)
    assert request.expected_version == 3 and request.item_id == ITEM


def test_add_evidence_and_show_pass_through() -> None:
    tasks = _FakeFollowupTasks()
    added = followup_add_evidence(
        tasks,  # type: ignore[arg-type]
        {"operation_id": "op-3", "item_id": ITEM, "evidence": {**EVIDENCE, "role": "supporting"}},
        authority=OWNER,
    )
    assert added["outcome"] == "ok"
    assert isinstance(tasks.calls[0][1], AddEvidenceRequest)
    shown = followup_show(tasks, {"item_id": ITEM}, authority=OWNER)  # type: ignore[arg-type]
    assert shown["outcome"] == "ok"
    data = shown["data"]
    assert isinstance(data, dict)
    assert set(data) == {"item", "evidence", "decisions"}


def test_engine_refusals_map_to_closed_codes_with_detail() -> None:
    tasks = _FakeFollowupTasks()
    tasks.error = FollowupError("version_conflict", current_version=7)
    refused = followup_decide(
        tasks,  # type: ignore[arg-type]
        {**DECIDE, "action": "confirm"},
        authority=OWNER,
    )
    assert refused == {
        "outcome": "refused",
        "data": {"current_version": 7},
        "code": "version_conflict",
    }
    tasks.error = FollowupError("duplicate_item", existing_item_id=OTHER_ITEM)
    duplicate = followup_propose(
        tasks,  # type: ignore[arg-type]
        {**PROPOSE, "evidence": [EVIDENCE]},
        authority=OWNER,
    )
    assert duplicate == {
        "outcome": "refused",
        "data": {"existing_item_id": OTHER_ITEM},
        "code": "duplicate_item",
    }
    tasks.error = FollowupError("not_found")
    assert followup_show(tasks, {"item_id": ITEM}, authority=OWNER) == {  # type: ignore[arg-type]
        "outcome": "refused",
        "data": None,
        "code": "not_found",
    }


def test_bound_operations_close_over_tasks_and_authority() -> None:
    tasks = _FakeFollowupTasks()
    bound = bind_followup_operations(tasks, authority=OWNER)  # type: ignore[arg-type]
    assert set(bound) == {"list", "show", "propose", "add_evidence", "decide"}
    assert bound["list"]({})["outcome"] == "ok"
    assert tasks.calls[0][2] is OWNER


def test_followup_session_authority_clamps_unscoped_sessions_to_work() -> None:
    owner = owner_authority_for_principal(principal_id="owner-1", session_id="mcp-1")
    clamped = followup_session_authority(
        owner, grants=frozenset({"followup-read", "followup-decide"}), launcher_policy=False
    )
    assert clamped.owner is False
    assert clamped.allowed_read_tiers == frozenset({PrivacyTier.WORK})
    assert {"followup-read", "followup-decide"} <= clamped.capabilities
    assert "followup-propose" not in clamped.capabilities
    assert (clamped.principal_id, clamped.session_id) == ("owner-1", "mcp-1")
    assert clamped.permits_read_tier(PrivacyTier.PERSONAL) is False
    assert clamped.permits_read_tier(PrivacyTier.PUBLIC) is False
    agent = agent_authority(
        principal_id="agent-1", session_id="mcp-2", grants=frozenset({"followup-read"})
    )
    assert followup_session_authority(
        agent, grants=frozenset({"followup-read"}), launcher_policy=False
    ).allowed_read_tiers == frozenset({PrivacyTier.WORK})


def test_followup_session_authority_defers_to_a_launcher_policy() -> None:
    policy_bound = replace(
        agent_authority(
            principal_id="agent-1", session_id="mcp-3", grants=frozenset({"followup-read"})
        ),
        allowed_read_tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
    )
    same = followup_session_authority(
        policy_bound, grants=frozenset({"followup-read"}), launcher_policy=True
    )
    assert same is policy_bound
