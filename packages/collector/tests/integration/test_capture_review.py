"""Synthetic owner decisions, retry plans and proposal attention."""

from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain_collector.capture_review import CaptureReviewStore
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from packages.engine.tests.contract.test_facets_acceptance import FacetHarness

OWNER = EffectiveAuthority("owner", "owner-session", frozenset(), None, owner=True)


def request(value: str, record: str = "capture_raw") -> dict[str, object]:
    return {
        "operation_id": "candidate-assignment",
        "record_id": record,
        "expected_revision_id": record,
        "expected_version": 0,
        "add_value_ids": [value],
        "remove_value_ids": [],
    }


def test_candidate_scope_rejected_deferred_and_owner_gate(tmp_path: Path) -> None:
    queue = CaptureReviewStore(PrivateJsonStore(tmp_path / "queue"))
    candidate = queue.discover("Apollo", "dimension", "workspace", ("capture_raw",))
    assert candidate == queue.discover("Apollo", "dimension", "workspace", ("capture_new",))
    assert candidate != queue.discover("Apollo", "dimension", "other", ("capture_raw",))
    assert candidate != queue.discover("Apollo", "other", "workspace", ("capture_raw",))
    scoped = EffectiveAuthority("agent", "session", frozenset(), None)
    with pytest.raises(LiveSourceError, match="candidate_owner_required"):
        queue.decide(candidate, "reject", authority=scoped)
    queue.decide(candidate, "reject", authority=OWNER)
    assert queue.discover("Apollo", "dimension", "workspace", ("capture_raw",)) == candidate
    retained = next(c for c in queue.candidates() if c["candidate_id"] == candidate)
    assert retained["state"] == "reject"
    deferred = queue.discover("Other", "dimension", "workspace", ("capture_raw",))
    queue.decide(deferred, "defer", authority=OWNER, deferred_until="2026-11-01T00:00:00+00:00")
    assert queue.discover("Other", "dimension", "workspace", ("capture_new",)) == deferred
    assert next(c for c in queue.candidates() if c["candidate_id"] == deferred)["state"] == "defer"


def test_decision_precedes_frozen_assignment_lost_ack(tmp_path: Path) -> None:
    store = PrivateJsonStore(tmp_path / "queue")
    queue = CaptureReviewStore(store)
    candidate = queue.discover("Apollo", "dimension", "workspace", ("capture_raw",))
    frozen = request("value")
    queue.decide(candidate, "map", authority=OWNER, value_id="value", assignment_requests=(frozen,))
    frozen["expected_version"] = 99
    calls = []

    def assign(value: dict[str, object]) -> None:
        retained = cast(dict[str, Any], store.read("capture-review-v1.json"))
        assert retained["candidates"][candidate]["decision"] == "map"
        calls.append(dict(value))
        if len(calls) == 1:
            raise RuntimeError("synthetic lost acknowledgement")

    with pytest.raises(RuntimeError):
        queue.apply(candidate, authority=OWNER, create_value=lambda r: "unused", assign=assign)
    queue = CaptureReviewStore(store)
    assert (
        queue.apply(candidate, authority=OWNER, create_value=lambda r: "unused", assign=assign)[
            "state"
        ]
        == "resolved"
    )
    assert calls[0] == calls[1]
    assert calls[0]["expected_version"] == 0


def test_real_engine_existing_value_assignment_and_retry(tmp_path: Path) -> None:
    harness = FacetHarness(tmp_path)
    dimension = harness.dimension()
    value = harness.value(dimension)
    record = harness.capture()
    queue = CaptureReviewStore(PrivateJsonStore(tmp_path / "queue"))
    candidate = queue.discover("Apollo", dimension, "workspace", (record,))
    queue.decide(
        candidate,
        "map",
        authority=harness.authority,
        value_id=value,
        assignment_requests=(request(value, record),),
    )
    calls = []

    def assign(arguments: dict[str, Any]) -> None:
        result = harness.call("facet.assignment.patch", **arguments)
        calls.append(result)
        if len(calls) == 1:
            raise RuntimeError("synthetic lost response")

    with pytest.raises(RuntimeError):
        queue.apply(
            candidate, authority=harness.authority, create_value=lambda r: "unused", assign=assign
        )
    queue.apply(
        candidate, authority=harness.authority, create_value=lambda r: "unused", assign=assign
    )
    assert calls[0] == calls[1]
    assert harness.call("facet.assignment.get", record_id=record)["item"]["memberships"] == [
        {"value_id": value, "membership_version": 1}
    ]


def test_attention_resurfaces_preserves_terminal_and_expires_routine(tmp_path: Path) -> None:
    queue = CaptureReviewStore(PrivateJsonStore(tmp_path / "queue"))
    for proposal, kind in (
        ("routine", "routine"),
        ("commitment", "commitment"),
        ("terminal", "contradiction"),
    ):
        queue.admit_attention(proposal, "2026-10-01T00:00:00+00:00", kind=kind)
    queue.defer_attention("commitment", "2026-10-03T00:00:00+00:00", authority=OWNER)

    def status(proposal: str) -> str:
        return "approved" if proposal == "terminal" else "pending"

    assert [r["proposal_id"] for r in queue.attention("2026-10-02T00:00:00+00:00", status)] == [
        "routine"
    ]
    assert len(queue.attention("2026-10-04T00:00:00+00:00", status)) == 2
    assert [r["proposal_id"] for r in queue.attention("2026-11-01T00:00:00+00:00", status)] == [
        "commitment"
    ]
    queue.dismiss_attention("commitment", authority=OWNER)
    assert not queue.attention("2026-11-02T00:00:00+00:00", status)


def test_creation_reserved_before_callbacks_and_weekly_limit(tmp_path: Path) -> None:
    store = PrivateJsonStore(tmp_path / "queue")
    queue = CaptureReviewStore(store)
    candidate = queue.discover("Apollo", "dimension", "workspace", ("capture_raw",))
    creation = {
        "operation_id": "candidate-create",
        "dimension_id": "dimension",
        "name": "Apollo",
        "expected_dimension_version": 1,
        "expected_version": 0,
    }
    queue.decide(candidate, "create", authority=OWNER, creation_request=creation)
    calls = []

    def create(arguments: dict[str, object]) -> str:
        retained = cast(dict[str, Any], store.read("capture-review-v1.json"))["candidates"][
            candidate
        ]
        assert retained["creation_request"] == arguments
        calls.append(arguments)
        if len(calls) == 1:
            raise RuntimeError("synthetic creation lost acknowledgement")
        return "value"

    with pytest.raises(RuntimeError):
        queue.apply(candidate, authority=OWNER, create_value=create, assign=lambda r: None)
    queue = CaptureReviewStore(store)
    queue.apply(
        candidate,
        authority=OWNER,
        create_value=create,
        assign=lambda r: None,
        assignment_plan=lambda c, v: (request(v),),
    )
    assert calls[0] == calls[1]
    for i in range(10):
        queue.admit_attention(str(i), "2026-10-01T00:00:00+00:00")
    with pytest.raises(LiveSourceError, match="attention_weekly_limit"):
        queue.admit_attention("eleventh", "2026-10-02T00:00:00+00:00")


def test_new_candidate_evidence_requires_visible_backfill(tmp_path: Path) -> None:
    queue = CaptureReviewStore(PrivateJsonStore(tmp_path / "queue"))
    identity = queue.discover("Apollo", "dimension", "workspace", ("capture_one",))
    first = {**request("value", "capture_one"), "operation_id": "assignment-one"}
    queue.decide(identity, "map", authority=OWNER, value_id="value", assignment_requests=(first,))
    assigned: list[str] = []

    def assign(arguments: dict[str, object]) -> None:
        assigned.append(cast(str, arguments["record_id"]))

    queue.apply(identity, authority=OWNER, create_value=lambda _: "unused", assign=assign)
    queue.discover("Apollo", "dimension", "workspace", ("capture_two",))
    assert (
        queue.apply(identity, authority=OWNER, create_value=lambda _: "unused", assign=assign)[
            "state"
        ]
        == "backfill_pending"
    )
    second = {**request("value", "capture_two"), "operation_id": "assignment-two"}
    queue.replan(identity, (first, second), authority=OWNER)
    assert (
        queue.apply(identity, authority=OWNER, create_value=lambda _: "unused", assign=assign)[
            "state"
        ]
        == "resolved"
    )
    assert assigned == ["capture_one", "capture_two"]
