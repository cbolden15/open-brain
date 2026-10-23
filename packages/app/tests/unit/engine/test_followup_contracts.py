"""Pure follow-up contract validation: closed enums, request invariants, error codes."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from open_brain_engine.engine.followup_contracts import (
    MAX_EVIDENCE_PER_ITEM,
    MAX_ITEMS,
    MAX_SUMMARY_CHARS,
    AddEvidenceRequest,
    DecideRequest,
    DecisionAction,
    EvidenceRef,
    EvidenceRole,
    FollowupError,
    FollowupKind,
    FollowupState,
    ListFilter,
    OwnerKind,
    ProposeRequest,
    request_wire,
)
from open_brain_engine.engine.followup_schema import FOLLOWUP_MAX_ITEMS

SHA = "a" * 64


def origin(start: int = 0, end: int = 5, role: EvidenceRole = EvidenceRole.ORIGIN) -> EvidenceRef:
    return EvidenceRef("capture_1", "capture_1", start, end, SHA, role)


def propose(**overrides: object) -> ProposeRequest:
    fields: dict[str, object] = {
        "operation_id": "op-1",
        "kind": FollowupKind.COMMITMENT,
        "summary": "Send the deck by Friday",
        "project": None,
        "owner": OwnerKind.UNKNOWN,
        "owner_name": None,
        "due": None,
        "due_evidence_index": None,
        "evidence": (origin(),),
    }
    fields.update(overrides)
    return ProposeRequest(**fields)  # type: ignore[arg-type]


def test_limits_and_enum_values_are_closed() -> None:
    assert (MAX_SUMMARY_CHARS, MAX_ITEMS, MAX_EVIDENCE_PER_ITEM) == (280, 10_000, 50)
    assert MAX_ITEMS == FOLLOWUP_MAX_ITEMS
    assert [k.value for k in FollowupKind] == ["commitment", "question"]
    assert [s.value for s in FollowupState] == [
        "suggested",
        "confirmed",
        "snoozed",
        "dismissed",
        "resolved",
        "superseded",
    ]
    assert [o.value for o in OwnerKind] == ["self", "other", "unknown"]
    assert [r.value for r in EvidenceRole] == ["origin", "supporting", "correction", "resolution"]
    assert [a.value for a in DecisionAction] == [
        "confirm",
        "dismiss",
        "snooze",
        "correct",
        "link",
        "resolve",
        "reopen",
        "supersede",
    ]


def test_error_codes_are_closed_and_carry_optional_attributes() -> None:
    error = FollowupError("version_conflict", current_version=3)
    assert (error.code, error.current_version, error.existing_item_id) == (
        "version_conflict",
        3,
        None,
    )
    duplicate = FollowupError("duplicate_item", existing_item_id="followup_x")
    assert duplicate.existing_item_id == "followup_x"
    with pytest.raises(ValueError, match="invalid error code"):
        FollowupError("unsupported_capability")


def test_valid_propose_request_round_trips_to_wire() -> None:
    request = propose(due=date(2026, 11, 1), due_evidence_index=0)
    assert request_wire(request) == {
        "operation_id": "op-1",
        "kind": "commitment",
        "summary": "Send the deck by Friday",
        "project": None,
        "owner": "unknown",
        "owner_name": None,
        "due": "2026-11-01",
        "due_evidence_index": 0,
        "evidence": [
            {
                "capture_id": "capture_1",
                "revision_id": "capture_1",
                "quote_start": 0,
                "quote_end": 5,
                "quote_sha256": SHA,
                "role": "origin",
            }
        ],
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": ""},
        {"summary": "x" * (MAX_SUMMARY_CHARS + 1)},
        {"summary": "two\nlines"},
        {"operation_id": ""},
        {"operation_id": "x" * 129},
        {"owner": OwnerKind.OTHER, "owner_name": None},
        {"owner": OwnerKind.SELF, "owner_name": "Someone"},
        {"due": date(2026, 11, 1), "due_evidence_index": None},
        {"due": None, "due_evidence_index": 0},
        {"due": date(2026, 11, 1), "due_evidence_index": 1},
        {"evidence": ()},
        {"evidence": (origin(role=EvidenceRole.SUPPORTING),)},
        {"evidence": (origin(), origin(6, 9))},
        {"evidence": tuple(origin(i, i + 1) for i in range(MAX_EVIDENCE_PER_ITEM + 1))},
        {"project": ""},
    ],
)
def test_invalid_propose_requests_are_refused_before_io(overrides: dict[str, object]) -> None:
    with pytest.raises(FollowupError, match="invalid_request"):
        propose(**overrides)


@pytest.mark.parametrize(
    "ref",
    [
        ("capture_1", "capture_1", 5, 5, SHA),
        ("capture_1", "capture_1", -1, 5, SHA),
        ("capture_1", "capture_1", 0, 5, "A" * 64),
        ("capture_1", "capture_1", 0, 5, "a" * 63),
        ("page_1", "page_1", 0, 5, SHA),
        ("capture_1", "capture_2", 0, 5, SHA),
    ],
)
def test_invalid_evidence_refs_are_refused(ref: tuple[object, ...]) -> None:
    with pytest.raises(FollowupError, match="invalid_request"):
        EvidenceRef(*ref, EvidenceRole.ORIGIN)  # type: ignore[arg-type, call-arg]


def test_add_evidence_refuses_a_second_origin() -> None:
    with pytest.raises(FollowupError, match="invalid_request"):
        AddEvidenceRequest("op-2", "followup_1", origin())
    AddEvidenceRequest("op-2", "followup_1", origin(role=EvidenceRole.SUPPORTING))


@pytest.mark.parametrize(
    "fields",
    [
        {"action": DecisionAction.SNOOZE},
        {"action": DecisionAction.CONFIRM, "snoozed_until": date(2026, 12, 1)},
        {"action": DecisionAction.LINK, "ticket_url": "https://t/ABC-1"},
        {"action": DecisionAction.CONFIRM, "ticket_url": "https://t/ABC-1"},
        {"action": DecisionAction.LINK, "ticket_key": "ABC-1", "ticket_url": "ftp://x"},
        {"action": DecisionAction.RESOLVE},
        {"action": DecisionAction.RESOLVE, "resolution": "guess"},
        {"action": DecisionAction.SUPERSEDE},
        {"action": DecisionAction.SUPERSEDE, "superseded_by": "followup_1"},
        {"action": DecisionAction.CORRECT},
        {"action": DecisionAction.CORRECT, "corrections": {}},
        {"action": DecisionAction.CORRECT, "corrections": {"ticket_key": "x"}},
        {"action": DecisionAction.CORRECT, "corrections": {"summary": ""}},
        {"action": DecisionAction.CORRECT, "corrections": {"owner": "other"}},
        {"action": DecisionAction.CORRECT, "corrections": {"due": "2026-11-01"}},
        {"action": DecisionAction.DISMISS, "expected_version": 0},
    ],
)
def test_decide_requests_require_fields_consistent_with_the_action(
    fields: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "operation_id": "op-3",
        "item_id": "followup_1",
        "expected_version": 1,
    }
    values.update(fields)
    with pytest.raises(FollowupError, match="invalid_request"):
        DecideRequest(**values)  # type: ignore[arg-type]


def test_decide_requests_accept_each_well_formed_action() -> None:
    base: dict[str, Any] = {
        "operation_id": "op-3",
        "item_id": "followup_1",
        "expected_version": 1,
    }
    DecideRequest(**base, action=DecisionAction.CONFIRM)
    DecideRequest(**base, action=DecisionAction.SNOOZE, snoozed_until=date(2027, 1, 1))
    DecideRequest(
        **base, action=DecisionAction.LINK, ticket_key="ABC-1", ticket_url="https://t/ABC-1"
    )
    DecideRequest(**base, action=DecisionAction.LINK, ticket_key="ABC-2")
    DecideRequest(**base, action=DecisionAction.RESOLVE, resolution="owner_statement")
    DecideRequest(**base, action=DecisionAction.SUPERSEDE, superseded_by="followup_2")
    DecideRequest(
        **base,
        action=DecisionAction.CORRECT,
        corrections={"owner": "other", "owner_name": "Dana", "due": None, "project": "Atlas"},
    )
    DecideRequest(
        **base,
        action=DecisionAction.CORRECT,
        corrections={"due": "2026-11-01", "due_evidence_id": "evidence_1"},
    )


@pytest.mark.parametrize("limit", [0, 201])
def test_list_filter_bounds_limit(limit: int) -> None:
    with pytest.raises(FollowupError, match="invalid_request"):
        ListFilter(limit=limit)
    assert ListFilter().limit == 50
