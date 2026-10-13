"""Real foreground facet state/evidence checks using only temporary synthetic Brains.

When the coordinator has not registered schema 11, the fixture installs additive
DDL and substitutes only SQL admission. It never fakes facet results. The same
cases use the real admitted store unchanged on the integrated schema-11 tree.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.contracts import CaptureReceipt, LocalEngineContext, TextPayload
from open_brain_engine.engine.facet_evidence import validate_portable_facet_sidecars
from open_brain_engine.engine.facet_schema_migration import (
    FACET_TABLE_SCHEMA,
    facet_schema_available,
)
from open_brain_engine.engine.facet_store import events_sidecar, operations_sidecar, semantic_state
from open_brain_engine.engine.facets import FacetTasks
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.engine.local_store import _LocalStore
from open_brain_engine.engine.source_store import (
    publish_source_metadata,
    register_completed_captures,
    register_publication_members,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.providers.base import ProviderMode

TENANT = "tenant_00000000-0000-4000-8000-000000000001"
ACTOR = "actor_00000000-0000-4000-8000-000000000002"


class _AdditiveDDLTestStore:
    """Temporary SQL admission seam pending coordinator registration; real SQLite."""

    def __init__(self, profile: LocalEngineContext) -> None:
        self.profile = profile

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.profile.root / PHASE1_STATE_DATABASE, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            register_completed_captures(connection, self.profile)
            register_publication_members(connection, self.profile)
            connection.execute("COMMIT")
            publish_source_metadata(connection, self.profile)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


class Brain:
    def __init__(self, tmp_path: Path) -> None:
        root = tmp_path / "synthetic-brain"
        root.mkdir(mode=0o700)
        (root / ".open-brain").mkdir(mode=0o700)
        self.profile = LocalEngineContext(
            root=root,
            root_identity=(root.stat().st_dev, root.stat().st_ino),
            tenant_id=TENANT,
            owner_actor_id=ACTOR,
            owner_role_claim={
                "actor_id": ACTOR,
                "capabilities": ["owner"],
                "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000003",
                "role_id": "role_00000000-0000-4000-8000-000000000004",
                "tenant_id": TENANT,
            },
            provider_mode=ProviderMode.NONE,
            starter_spaces=("Notes", "Other"),
        )
        self.engine = BrainEngine.open(
            self.profile, clock=lambda: datetime(2026, 10, 13, 12, tzinfo=UTC)
        )
        self.spaces = self.engine.inbox.spaces()
        self.captures = [
            cast(
                CaptureReceipt,
                self.engine.capture.accept(
                    TextPayload("Equal synthetic evidence"),
                    delivery_id=f"fixture.capture.{index}",
                    space_id=space.space_id,
                    privacy_tier=PrivacyTier.WORK,
                ),
            )
            for index, space in enumerate(self.spaces)
        ]
        connection = self.engine._store.connect()
        self.migration_pending = not facet_schema_available(connection)
        connection.close()
        if self.migration_pending:
            test_store = _AdditiveDDLTestStore(self.profile)
            with test_store.transaction() as connection:
                for statement in FACET_TABLE_SCHEMA:
                    connection.execute(statement)
            self.engine._store = cast(_LocalStore, test_store)
        self.tasks = FacetTasks(self.engine)
        self.owner = EffectiveAuthority(
            principal_id=ACTOR,
            session_id="synthetic-owner",
            capabilities=frozenset(),
            space_ids=None,
            owner=True,
        )
        self.sequence = 0

    def call(
        self, operation: str, arguments: dict[str, Any], authority: EffectiveAuthority | None = None
    ) -> dict[str, Any]:
        request = fc.request_from_wire(operation, {"dto_version": 1, **arguments})
        method = getattr(self.tasks, fc.OPERATION_METHODS[operation])
        return cast(dict[str, Any], method(request, authority=authority or self.owner).to_wire())

    def write(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        return self.call(
            operation, {"operation_id": f"fixture.operation.{self.sequence}", **arguments}
        )

    def vocabulary(self) -> tuple[str, str, str]:
        dimension = self.write(
            "facet.dimension.create", {"expected_version": 0, "name": "Project"}
        )["subject_id"]
        value = self.write(
            "facet.value.create",
            {
                "expected_version": 0,
                "dimension_id": dimension,
                "expected_dimension_version": 1,
                "name": "Apollo",
            },
        )["subject_id"]
        other = self.write(
            "facet.value.create",
            {
                "expected_version": 0,
                "dimension_id": dimension,
                "expected_dimension_version": 1,
                "name": "Lunar",
            },
        )["subject_id"]
        return dimension, value, other

    def patch(
        self,
        capture: CaptureReceipt,
        *,
        version: int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.write(
            "facet.assignment.patch",
            {
                "expected_version": version,
                "record_id": capture.capture_id,
                "expected_revision_id": capture.capture_id,
                "add_value_ids": add or [],
                "remove_value_ids": remove or [],
            },
        )

    def counters(self) -> tuple[int, int, int]:
        connection = self.engine._store.connect()
        try:
            return (
                connection.execute("SELECT COUNT(*) FROM facet_events").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM facet_operations").fetchone()[0],
                connection.execute(
                    "SELECT retrieval_generation FROM engine_generations"
                ).fetchone()[0],
            )
        finally:
            connection.close()


@pytest.fixture
def brain(tmp_path: Path) -> Brain:
    return Brain(tmp_path)


def test_owner_vocabulary_equal_captures_are_independent_and_bytes_unchanged(brain: Brain) -> None:
    connection = brain.engine._store.connect()
    try:
        before = {
            row[0]: (brain.profile.root / row[0]).read_bytes()
            for row in connection.execute("SELECT source_path FROM captures")
        }
    finally:
        connection.close()
    dimension, value, other = brain.vocabulary()
    brain.patch(brain.captures[0], version=0, add=[value, other])
    assigned = brain.call("facet.assignment.get", {"record_id": brain.captures[0].capture_id})[
        "item"
    ]
    independent = brain.call("facet.assignment.get", {"record_id": brain.captures[1].capture_id})[
        "item"
    ]
    assert assigned["version"] == 1 and len(assigned["memberships"]) == 2
    assert independent["version"] == 0 and independent["memberships"] == []
    assert assigned["subject"] != independent["subject"]
    assert brain.call("facet.dimension.list", {})["items"][0]["dimension_id"] == dimension
    assert all((brain.profile.root / path).read_bytes() == raw for path, raw in before.items())


def test_catalogue_names_reservation_rename_and_one_way_archive(brain: Brain) -> None:
    dimension, value, _other = brain.vocabulary()
    with pytest.raises(fc.FacetError, match="name_conflict"):
        brain.write("facet.dimension.create", {"expected_version": 0, "name": " project "})
    brain.write(
        "facet.dimension.rename",
        {"dimension_id": dimension, "expected_version": 1, "name": "Projects"},
    )
    brain.write("facet.dimension.create", {"expected_version": 0, "name": "Project"})
    brain.write("facet.dimension.archive", {"dimension_id": dimension, "expected_version": 2})
    assert brain.call("facet.value.list", {"dimension_id": dimension})["items"] == []
    values = brain.call("facet.value.list", {"dimension_id": dimension, "include_archived": True})[
        "items"
    ]
    assert all(item["effective_archived"] and not item["archived"] for item in values)
    with pytest.raises(fc.FacetError, match="facet_archived"):
        brain.patch(brain.captures[0], version=0, add=[value])
    with pytest.raises(fc.FacetError, match="name_conflict"):
        brain.write("facet.dimension.create", {"expected_version": 0, "name": "Projects"})


def test_exact_retry_noop_stale_and_membership_remove_restore(brain: Brain) -> None:
    _dimension, value, _other = brain.vocabulary()
    receipt = brain.patch(brain.captures[0], version=0, add=[value])
    request = {
        "operation_id": receipt["operation_id"],
        "expected_version": 0,
        "record_id": brain.captures[0].capture_id,
        "expected_revision_id": brain.captures[0].capture_id,
        "add_value_ids": [value],
        "remove_value_ids": [],
    }
    brain.patch(brain.captures[0], version=1, remove=[value])
    brain.patch(brain.captures[0], version=2, add=[value])
    assert brain.call("facet.assignment.patch", request) == receipt
    state = brain.call("facet.assignment.get", {"record_id": brain.captures[0].capture_id})["item"]
    assert state["version"] == 3 and state["memberships"][0]["membership_version"] == 3
    with pytest.raises(fc.FacetError, match="version_conflict"):
        brain.patch(brain.captures[0], version=0, add=[value])
    with pytest.raises(fc.FacetError, match="idempotency_conflict"):
        brain.call("facet.assignment.patch", {**request, "expected_version": 3})
    counters = brain.counters()
    no_op = brain.patch(brain.captures[0], version=3, add=[value])
    assert no_op["status"] == "unchanged" and no_op["event_id"] is None
    after = brain.counters()
    assert after == (counters[0], counters[1] + 1, counters[2])
    brain.write("facet.value.archive", {"value_id": value, "expected_version": 1})
    assert brain.patch(brain.captures[0], version=3, add=[value])["status"] == "unchanged"
    assert brain.patch(brain.captures[0], version=3, remove=[value])["version"] == 4


def test_query_and_same_dimension_filters_no_hidden_counts(brain: Brain) -> None:
    _dimension, value, other = brain.vocabulary()
    brain.patch(brain.captures[0], version=0, add=[value, other])
    brain.patch(brain.captures[1], version=0, add=[value])
    page = brain.call("facet.query.page", {"selector": {"all_value_ids": [value, value, other]}})
    assert len(page["items"]) == 1
    assert page["items"][0]["record"]["record_id"] == brain.captures[0].capture_id
    assert set(page) == {"status", "dto_version", "items", "next_cursor", "complete"}
    filtered = brain.call(
        "facet.query.page",
        {"selector": {"all_value_ids": [value], "space_ids": [brain.spaces[1].space_id]}},
    )
    assert (
        len(filtered["items"]) == 1
        and filtered["items"][0]["record"]["record_id"] == brain.captures[1].capture_id
    )


def test_authority_before_versions_or_retry_and_query_scope(brain: Brain) -> None:
    _dimension, value, _other = brain.vocabulary()
    receipt = brain.patch(brain.captures[1], version=0, add=[value])
    limited = EffectiveAuthority(
        principal_id="synthetic-agent",
        session_id="scoped-session",
        capabilities=frozenset({"facet-read", "facet-assign", "search"}),
        space_ids=frozenset({brain.spaces[0].space_id}),
    )
    with pytest.raises(fc.FacetError, match="not_found"):
        brain.call(
            "facet.assignment.patch",
            {
                "operation_id": receipt["operation_id"],
                "expected_version": 99,
                "record_id": brain.captures[1].capture_id,
                "expected_revision_id": brain.captures[1].capture_id,
                "add_value_ids": [value],
            },
            limited,
        )
    assert (
        brain.call("facet.query.page", {"selector": {"all_value_ids": [value]}}, limited)["items"]
        == []
    )
    query = brain.write(
        "facet.query.create",
        {
            "name": "Other",
            "expected_version": 0,
            "selector": {"all_value_ids": [value], "space_ids": [brain.spaces[1].space_id]},
        },
    )["subject_id"]
    with pytest.raises(fc.FacetError, match="not_found"):
        brain.call("facet.query.get", {"query_id": query}, limited)
    assert brain.call("facet.query.list", {}, limited)["items"] == []
    denied = EffectiveAuthority(
        principal_id="no-grants", session_id="denied", capabilities=frozenset(), space_ids=None
    )
    before = brain.counters()
    with pytest.raises(fc.FacetError, match="unsupported_capability"):
        brain.call(
            "facet.dimension.create",
            {"operation_id": "denied-write", "expected_version": 0, "name": "Denied"},
            denied,
        )
    assert brain.counters() == before


def test_selection_version_display_rename_archive_and_history_cutoff(brain: Brain) -> None:
    dimension, value, other = brain.vocabulary()
    receipt = brain.patch(brain.captures[0], version=0, add=[value])
    subject = brain.call("facet.assignment.get", {"record_id": brain.captures[0].capture_id})[
        "item"
    ]["subject"]
    cutoff = brain.counters()[0]
    brain.patch(brain.captures[0], version=1, remove=[value])
    history = brain.call("facet.history.state", {"subject": subject, "as_of_sequence": cutoff})[
        "item"
    ]
    assert history["version"] == 1 and history["memberships"] == [
        {"value_id": value, "membership_version": 1}
    ]
    entries = brain.call("facet.history.list", {"subject": subject})["items"]
    assert len(entries) == 2 and entries[0]["event_id"] == receipt["event_id"]
    assert all("operation_id" not in event and "brain_id" not in event for event in entries)
    assert entries[-1]["changes"][-1]["after"]["present"] is False
    query = brain.write(
        "facet.query.create",
        {"expected_version": 0, "name": "Apollo", "selector": {"all_value_ids": [value]}},
    )["subject_id"]
    brain.write(
        "facet.query.update",
        {
            "query_id": query,
            "expected_version": 1,
            "name": "Renamed",
            "selector": {"all_value_ids": [value]},
        },
    )
    assert brain.call("facet.query.get", {"query_id": query})["item"]["selection_version"] == 1
    brain.write(
        "facet.query.update",
        {
            "query_id": query,
            "expected_version": 2,
            "name": "Renamed",
            "selector": {"all_value_ids": [other]},
        },
    )
    assert brain.call("facet.query.get", {"query_id": query})["item"]["selection_version"] == 2
    brain.write("facet.dimension.archive", {"dimension_id": dimension, "expected_version": 1})
    before_creation = brain.call(
        "facet.history.state", {"subject": {"kind": "value", "id": value}, "as_of_sequence": 0}
    )
    assert before_creation["item"] is None


def test_cursors_bind_input_scope_generation_and_tampering(brain: Brain) -> None:
    _dimension, value, other = brain.vocabulary()
    for capture in brain.captures:
        brain.patch(capture, version=0, add=[value])
    args = {"selector": {"all_value_ids": [value]}, "limit": 1}
    first = brain.call("facet.query.page", args)
    assert first["next_cursor"] is not None and not first["complete"]
    second = brain.call("facet.query.page", {**args, "cursor": first["next_cursor"]})
    assert second["complete"] and first["items"][0]["subject"] != second["items"][0]["subject"]
    with pytest.raises(fc.FacetError, match="cursor_invalid"):
        brain.call("facet.query.page", {**args, "limit": 2, "cursor": first["next_cursor"]})
    with pytest.raises(fc.FacetError, match="cursor_invalid"):
        brain.call("facet.query.page", {**args, "cursor": "x" * 98})
    brain.patch(brain.captures[0], version=1, add=[other])
    with pytest.raises(fc.FacetError, match="cursor_stale"):
        brain.call("facet.query.page", {**args, "cursor": first["next_cursor"]})


def test_full_semantic_ledger_replays_exactly(brain: Brain) -> None:
    _dimension, value, _other = brain.vocabulary()
    brain.patch(brain.captures[0], version=0, add=[value])
    brain.patch(brain.captures[0], version=1, add=[value])
    brain.patch(brain.captures[0], version=1, remove=[value])
    brain.patch(brain.captures[0], version=2, add=[value])
    connection = brain.engine._store.connect()
    try:
        brain_id = connection.execute("SELECT brain_id FROM brain_identity").fetchone()[0]
        state = semantic_state(connection, brain_id)
        assert (
            validate_portable_facet_sidecars(
                events_sidecar(connection, brain_id),
                operations_sidecar(connection, brain_id),
                state,
            )
            == state
        )
    finally:
        connection.close()


@pytest.mark.parametrize("stage", ["before_semantic_commit", "after_semantic_commit"])
def test_commit_fault_recovery_is_one_event_state_receipt_and_generation(
    brain: Brain, stage: str
) -> None:
    before = brain.counters()
    fired = False

    def checkpoint(current: str) -> None:
        nonlocal fired
        if current == stage and not fired:
            fired = True
            raise RuntimeError("synthetic fault")

    brain.tasks = FacetTasks(brain.engine, checkpoint=checkpoint)
    args = {"operation_id": "fault-create", "expected_version": 0, "name": "Fault"}
    with pytest.raises(RuntimeError, match="synthetic fault"):
        brain.call("facet.dimension.create", args)
    failed = brain.counters()
    if stage == "before_semantic_commit":
        assert failed == before
    else:
        assert failed == tuple(item + 1 for item in before)
    receipt = brain.call("facet.dimension.create", args)
    assert receipt["status"] == "applied"
    assert brain.call("facet.dimension.create", args) == receipt
    assert brain.counters() == tuple(item + 1 for item in before)


def test_delegated_opaque_principal_uses_owner_actor_and_principal_bound_retry(
    brain: Brain,
) -> None:
    delegated = EffectiveAuthority(
        principal_id="open-brain-mcp-local",
        session_id="synthetic-delegate",
        capabilities=frozenset({"facet-catalog-write"}),
        space_ids=None,
    )
    args = {"operation_id": "delegated-create", "expected_version": 0, "name": "Delegated"}
    receipt = brain.call("facet.dimension.create", args, delegated)
    assert brain.call("facet.dimension.create", args, delegated) == receipt
    with pytest.raises(fc.FacetError, match="idempotency_conflict"):
        brain.call("facet.dimension.create", args)
    connection = brain.engine._store.connect()
    try:
        assert connection.execute("SELECT actor_id FROM facet_events").fetchone()[0] == ACTOR
        assert (
            connection.execute("SELECT principal_id FROM facet_operations").fetchone()[0]
            == delegated.principal_id
        )
        brain_id = connection.execute("SELECT brain_id FROM brain_identity").fetchone()[0]
        state = semantic_state(connection, brain_id)
        assert (
            validate_portable_facet_sidecars(
                events_sidecar(connection, brain_id),
                operations_sidecar(connection, brain_id),
                state,
            )
            == state
        )
    finally:
        connection.close()


def test_history_never_discloses_old_denied_query_space_filters(brain: Brain) -> None:
    _dimension, value, _other = brain.vocabulary()
    denied = {"all_value_ids": [value], "space_ids": [brain.spaces[1].space_id]}
    query = brain.write(
        "facet.query.create", {"expected_version": 0, "name": "Scoped", "selector": denied}
    )["subject_id"]
    cutoff = brain.counters()[0]
    brain.write(
        "facet.query.update",
        {
            "query_id": query,
            "expected_version": 1,
            "name": "Scoped",
            "selector": {"all_value_ids": [value], "space_ids": [brain.spaces[0].space_id]},
        },
    )
    limited = EffectiveAuthority(
        principal_id="synthetic-history",
        session_id="history-scope",
        capabilities=frozenset({"facet-read", "history-read"}),
        space_ids=frozenset({brain.spaces[0].space_id}),
    )
    subject = {"kind": "query", "id": query}
    assert brain.call("facet.history.list", {"subject": subject}, limited)["items"] == []
    with pytest.raises(fc.FacetError, match="not_found"):
        brain.call("facet.history.state", {"subject": subject, "as_of_sequence": cutoff}, limited)


def test_caller_transaction_assignment_seam_rolls_back_with_local_work(brain: Brain) -> None:
    _dimension, value, _other = brain.vocabulary()
    capture = brain.captures[0]
    request = fc.AssignmentsPatchRequest(
        operation_id="caller-owned-transaction",
        expected_version=0,
        record_id=capture.capture_id,
        expected_revision_id=capture.capture_id,
        add_value_ids=[value],
    )
    before = brain.counters()
    with (
        pytest.raises(RuntimeError, match="synthetic local custody fault"),
        brain.engine._writer_lease.acquire_shared_writer(),
        brain.engine._store.transaction() as connection,
    ):
        receipt = brain.tasks.run_mutation(connection, request, authority=brain.owner)
        assert receipt["status"] == "applied"
        assert connection.in_transaction
        raise RuntimeError("synthetic local custody fault")
    assert brain.counters() == before
    with (
        brain.engine._writer_lease.acquire_shared_writer(),
        brain.engine._store.transaction() as connection,
    ):
        receipt = brain.tasks.run_mutation(connection, request, authority=brain.owner)
    assert brain.call("facet.assignment.patch", request.to_wire()) == receipt
    assert brain.counters() == tuple(item + 1 for item in before)


def _protocol_conformance(tasks: FacetTasks) -> fc.FacetTasks:
    return tasks


def test_real_task_signatures_conform_to_the_shared_protocol(brain: Brain) -> None:
    assert _protocol_conformance(brain.tasks) is brain.tasks
    assert not brain.migration_pending


@pytest.mark.parametrize("parent_change", ["rename", "archive"])
@pytest.mark.parametrize("name", ["Apollo", "New value"])
def test_value_create_checks_stale_parent_before_name_conflict(
    brain: Brain, parent_change: str, name: str
) -> None:
    dimension, _value, _other = brain.vocabulary()
    arguments: dict[str, Any] = {"dimension_id": dimension, "expected_version": 1}
    if parent_change == "rename":
        arguments["name"] = "Projects"
    brain.write(f"facet.dimension.{parent_change}", arguments)
    counters = brain.counters()
    with pytest.raises(fc.FacetError, match="^version_conflict$"):
        brain.write(
            "facet.value.create",
            {
                "dimension_id": dimension,
                "expected_dimension_version": 1,
                "expected_version": 0,
                "name": name,
            },
        )
    assert brain.counters() == counters


@pytest.mark.parametrize("name", ["Apollo", "New value"])
def test_value_create_checks_current_parent_archive_before_name_conflict(
    brain: Brain, name: str
) -> None:
    dimension, _value, _other = brain.vocabulary()
    brain.write("facet.dimension.archive", {"dimension_id": dimension, "expected_version": 1})
    counters = brain.counters()
    with pytest.raises(fc.FacetError, match="^facet_archived$"):
        brain.write(
            "facet.value.create",
            {
                "dimension_id": dimension,
                "expected_dimension_version": 2,
                "expected_version": 0,
                "name": name,
            },
        )
    assert brain.counters() == counters


def test_value_create_retry_precedes_changed_parent_admission(brain: Brain) -> None:
    dimension, value, _other = brain.vocabulary()
    original = {
        "operation_id": "fixture.operation.2",
        "dimension_id": dimension,
        "expected_dimension_version": 1,
        "expected_version": 0,
        "name": "Apollo",
    }
    receipt = brain.call("facet.value.create", original)
    assert receipt["subject_id"] == value
    brain.write(
        "facet.dimension.rename",
        {"dimension_id": dimension, "expected_version": 1, "name": "Projects"},
    )
    brain.write("facet.dimension.archive", {"dimension_id": dimension, "expected_version": 2})
    counters = brain.counters()
    assert brain.call("facet.value.create", original) == receipt
    assert brain.counters() == counters
