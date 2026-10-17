"""Independent stage-one acceptance expectations; requires real registered producers.

Fixture proofs are named explicitly. Runtime tests never install a fake or skip a
missing producer. The coordinator integrates these tests after the matching gate.
"""

from __future__ import annotations

import json
import os
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine.contracts import (
    AdmissionLimits,
    CaptureAction,
    CaptureReceipt,
    CaptureSubmission,
    DecisionOutcome,
    LocalEngineContext,
    ManagedWorkspaceFailure,
    PublicJobCaptureContext,
    TextPayload,
)
from open_brain_engine.engine.facet_contracts import (
    OPERATION_METHODS,
    FacetError,
    request_from_wire,
    response_to_wire,
)
from open_brain_engine.engine.facet_evidence import (
    FacetEvidenceError,
    validate_portable_facet_sidecars,
)
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.source_intake import SourceRevisionReceipt, SourceRevisionSubmission
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, SourceRouteRequest
from open_brain_engine.portable.versioned import validated_portable_snapshot
from open_brain_engine.providers.base import ProviderMode
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from packages.app.tests.unit.engine.test_portable_v8_custody import _journal_state

TENANT = "tenant_00000000-0000-4000-8000-000000000001"
ACTOR = "actor_00000000-0000-4000-8000-000000000002"
FIXTURES = Path(__file__).parents[1] / "fixtures/facets"


class FacetHarness:
    """Synthetic real Brain setup, using public engine construction and task DTOs."""

    def __init__(self, tmp_path: Path, *, initialize: bool = True) -> None:
        self.root = tmp_path / "brain"
        self.root.mkdir(mode=0o700)
        (self.root / ".open-brain").mkdir(mode=0o700)
        identity = self.root.stat()
        self.profile = LocalEngineContext(
            root=self.root,
            root_identity=(identity.st_dev, identity.st_ino),
            tenant_id=TENANT,
            owner_actor_id=ACTOR,
            owner_role_claim={
                "actor_id": ACTOR,
                "capabilities": ["canonical.publish", "capture.accept", "space.write"],
                "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000003",
                "role_id": "role_00000000-0000-4000-8000-000000000004",
                "tenant_id": TENANT,
            },
            provider_mode=ProviderMode.NONE,
            starter_spaces=("Synthetic notes",),
        )
        profile_text = (
            'layout_version = 1\nprofile = "single-user-local"\n'
            f'tenant_id = "{TENANT}"\nowner_actor_id = "{ACTOR}"\n'
            'owner_role_id = "role_00000000-0000-4000-8000-000000000004"\n'
            'owner_role_claim_id = "role_claim_00000000-0000-4000-8000-000000000003"\n'
            'owner_capabilities = ["canonical.publish", "capture.accept", "space.write"]\n'
        )
        descriptor = os.open(self.root / "brain.toml", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
            sink.write(profile_text)
        if initialize:
            self.engine = BrainEngine.open(
                self.profile,
                clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
                admission_limits=AdmissionLimits(requests_per_minute_per_principal=500),
            )
        self.authority = EffectiveAuthority(ACTOR, "synthetic-owner", frozenset(), None, owner=True)
        self.serial = 0

    def call(
        self,
        operation: str,
        *,
        authority: EffectiveAuthority | None = None,
        **arguments: Any,
    ) -> dict[str, Any]:
        property_name = (
            "facet_publication"
            if operation.startswith("review.")
            else "facet_vault"
            if operation.startswith("facet.vault.")
            else "facets"
        )
        producer = getattr(self.engine, property_name, None)
        assert producer is not None, f"Missing real BrainEngine.{property_name} registration"
        response = getattr(producer, OPERATION_METHODS[operation])(
            request_from_wire(operation, {"dto_version": 1, **arguments}),
            authority=authority or self.authority,
        )
        return cast(dict[str, Any], response_to_wire(operation, response))

    def write(self, operation: str, **arguments: Any) -> dict[str, Any]:
        self.serial += 1
        arguments.setdefault("operation_id", f"independent-{self.serial}")
        return self.call(operation, **arguments)

    def dimension(self, name: str = "Project") -> str:
        return cast(
            str, self.write("facet.dimension.create", name=name, expected_version=0)["subject_id"]
        )

    def value(self, dimension_id: str, name: str = "Apollo") -> str:
        return cast(
            str,
            self.write(
                "facet.value.create",
                dimension_id=dimension_id,
                name=name,
                expected_version=0,
                expected_dimension_version=1,
            )["subject_id"],
        )

    def capture(self, text: str = "Synthetic independent source") -> str:
        self.serial += 1
        receipt = self.engine.capture.accept(
            TextPayload(text),
            delivery_id=f"capture-{self.serial}",
            space_id=self.engine.inbox.spaces()[0].space_id,
            privacy_tier=PrivacyTier.WORK,
        )
        assert isinstance(receipt, CaptureReceipt), "Synthetic capture must materialize immediately"
        return receipt.capture_id

    def patch(
        self,
        record: str,
        *,
        version: int = 0,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
        operation_id: str | None = None,
        revision: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "record_id": record,
            "expected_revision_id": revision or record,
            "expected_version": version,
            "add_value_ids": list(add),
            "remove_value_ids": list(remove),
        }
        if operation_id is not None:
            arguments["operation_id"] = operation_id
        return self.write("facet.assignment.patch", **arguments)

    def rows(self, table: str) -> list[tuple[Any, ...]]:
        allowed = {
            "facet_events",
            "facet_operations",
            "facet_subject_versions",
            "facet_assignments",
            "facet_dimensions",
            "engine_generations",
        }
        assert table in allowed
        with sqlite3.connect(self.root / ".open-brain/state/phase1.sqlite3") as connection:
            return connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


@pytest.fixture
def brain(tmp_path: Path) -> FacetHarness:
    return FacetHarness(tmp_path)


def test_f01_empty_catalogue_has_no_builtin_vocabulary(brain: FacetHarness) -> None:
    assert brain.call("facet.dimension.list")["items"] == []
    assert brain.call("facet.query.list")["items"] == []


def test_f01_names_normalize_and_rename_releases_only_previous_name(brain: FacetHarness) -> None:
    dimension = brain.dimension(" Cafe\u0301 ")
    assert brain.call("facet.dimension.list")["items"][0]["name"] == "Caf\u00e9"
    with pytest.raises(FacetError, match="name_conflict"):
        brain.dimension("CAF\u00c9")
    renamed = brain.write(
        "facet.dimension.rename", dimension_id=dimension, expected_version=1, name="Research"
    )
    assert renamed["subject_id"] == dimension and renamed["version"] == 2
    assert brain.dimension("Caf\u00e9") != dimension
    brain.write("facet.dimension.archive", dimension_id=dimension, expected_version=2)
    with pytest.raises(FacetError, match="name_conflict"):
        brain.dimension("research")


def test_f02_independent_equal_text_captures_do_not_inherit_membership(brain: FacetHarness) -> None:
    value = brain.value(brain.dimension())
    first, second = (
        brain.capture("Identical synthetic bytes"),
        brain.capture("Identical synthetic bytes"),
    )
    brain.patch(first, add=(value,))
    left = brain.call("facet.assignment.get", record_id=first)["item"]
    right = brain.call("facet.assignment.get", record_id=second)["item"]
    assert left["subject"] != right["subject"]
    assert right["version"] == 0 and right["memberships"] == []


def test_f03_classification_preserves_retained_content_bytes(brain: FacetHarness) -> None:
    record = brain.capture()
    retained = {
        path: path.read_bytes()
        for path in brain.root.rglob("*")
        if path.is_file() and ".open-brain" not in path.relative_to(brain.root).parts
    }
    value = brain.value(brain.dimension())
    brain.patch(record, add=(value,))
    brain.patch(record, version=1, remove=(value,))
    assert {path: path.read_bytes() for path in retained} == retained
    assert brain.call("facet.assignment.get", record_id=record)["item"]["revision_id"] == record


def test_f04_dimension_archive_preserves_memberships_and_allows_valid_noop_removal(
    brain: FacetHarness,
) -> None:
    dimension = brain.dimension()
    value = brain.value(dimension)
    assigned, unassigned = brain.capture(), brain.capture()
    brain.patch(assigned, add=(value,))
    brain.write("facet.dimension.archive", dimension_id=dimension, expected_version=1)
    assert brain.call("facet.value.list", dimension_id=dimension)["items"] == []
    item = brain.call("facet.value.list", dimension_id=dimension, include_archived=True)["items"][0]
    assert item["archived"] is False and item["effective_archived"] is True
    assert len(brain.call("facet.assignment.get", record_id=assigned)["item"]["memberships"]) == 1
    unchanged = brain.patch(assigned, version=1, add=(value,))
    assert unchanged["status"] == "unchanged" and unchanged["event_id"] is None
    with pytest.raises(FacetError, match="facet_archived"):
        brain.patch(unassigned, add=(value,))
    assert brain.patch(assigned, version=1, remove=(value,))["version"] == 2


def test_f05_retry_returns_original_receipt_after_later_mutations(brain: FacetHarness) -> None:
    value, record = brain.value(brain.dimension()), brain.capture()
    original = brain.patch(record, add=(value,), operation_id="replayed-assignment")
    brain.patch(record, version=1, remove=(value,))
    brain.patch(record, version=2, add=(value,))
    assert brain.patch(record, add=(value,), operation_id="replayed-assignment") == original
    with pytest.raises(FacetError, match="version_conflict"):
        brain.patch(record, add=(value,))
    with pytest.raises(FacetError, match="idempotency_conflict"):
        brain.patch(record, version=3, add=(value,), operation_id="replayed-assignment")


def test_f05_noop_is_durable_without_event_or_generation_increment(brain: FacetHarness) -> None:
    value, record = brain.value(brain.dimension()), brain.capture()
    before_events, before_generation = brain.rows("facet_events"), brain.rows("engine_generations")
    result = brain.patch(record, remove=(value,), operation_id="absent-removal")
    assert result["status"] == "unchanged" and result["version"] == 0 and result["event_id"] is None
    assert brain.rows("facet_events") == before_events
    assert brain.rows("engine_generations") == before_generation
    brain.engine = BrainEngine.open(brain.profile)
    assert brain.patch(record, remove=(value,), operation_id="absent-removal") == result


def test_f05_operation_namespace_is_bound_to_effective_principal(brain: FacetHarness) -> None:
    brain.write(
        "facet.dimension.create", name="Scoped", expected_version=0, operation_id="principal"
    )
    other = EffectiveAuthority("synthetic-other", "other", frozenset(), None, owner=True)
    with pytest.raises(FacetError, match="idempotency_conflict"):
        brain.call(
            "facet.dimension.create",
            name="Scoped",
            expected_version=0,
            operation_id="principal",
            authority=other,
        )


def test_f07_same_dimension_predicates_are_and_and_duplicate_terms_normalize(
    brain: FacetHarness,
) -> None:
    dimension = brain.dimension()
    a, b = brain.value(dimension, "A"), brain.value(dimension, "B")
    both, only_a = brain.capture(), brain.capture()
    brain.patch(both, add=(a, b))
    brain.patch(only_a, add=(a,))
    result = brain.call("facet.query.page", selector={"all_value_ids": [b, a, b]})
    assert [item["record"]["record_id"] for item in result["items"]] == [both]
    assert result["complete"] is True and result["next_cursor"] is None
    assert "total_count" not in result and "skipped_count" not in result


def test_f07_201_results_have_complete_stable_keyset_pagination(brain: FacetHarness) -> None:
    value = brain.value(brain.dimension())
    records = [brain.capture(f"Synthetic pagination {number}") for number in range(201)]
    for record in records:
        brain.patch(record, add=(value,))
    seen: list[str] = []
    subjects: list[str] = []
    cursor = None
    lengths = []
    for _ in range(3):
        result = brain.call(
            "facet.query.page", selector={"all_value_ids": [value]}, limit=100, cursor=cursor
        )
        lengths.append(len(result["items"]))
        seen.extend(item["record"]["record_id"] for item in result["items"])
        subjects.extend(item["subject"]["id"] for item in result["items"])
        cursor = result["next_cursor"]
    assert lengths == [100, 100, 1] and cursor is None
    assert set(seen) == set(records) and len(set(seen)) == 201
    assert subjects == sorted(subjects)


def test_f08_query_filters_never_broaden_read_authority(brain: FacetHarness) -> None:
    value, record = brain.value(brain.dimension()), brain.capture()
    brain.patch(record, add=(value,))
    authority = EffectiveAuthority(
        "synthetic-reader", "reader", frozenset({"facet-read", "search"}), frozenset()
    )
    result = brain.call(
        "facet.query.page", selector={"all_value_ids": [value]}, authority=authority
    )
    assert result["items"] == []
    assert record not in json.dumps(result)
    assert set(result) == {"dto_version", "status", "items", "next_cursor", "complete"}


def test_f08_cursor_rejects_unrelated_mutation_and_changed_input(brain: FacetHarness) -> None:
    value = brain.value(brain.dimension())
    for _ in range(2):
        brain.patch(brain.capture(), add=(value,))
    selector = {"all_value_ids": [value]}
    first = brain.call("facet.query.page", selector=selector, limit=1)
    assert first["next_cursor"] is not None
    with pytest.raises(FacetError) as changed:
        brain.call("facet.query.page", selector=selector, limit=2, cursor=first["next_cursor"])
    assert changed.value.code in {"cursor_invalid", "cursor_stale"}
    brain.dimension("Unrelated vocabulary")
    with pytest.raises(FacetError, match="cursor_stale"):
        brain.call("facet.query.page", selector=selector, limit=1, cursor=first["next_cursor"])


def test_f11_membership_remove_restore_retains_monotonic_membership_versions(
    brain: FacetHarness,
) -> None:
    value, record = brain.value(brain.dimension()), brain.capture()
    brain.patch(record, add=(value,))
    brain.patch(record, version=1, remove=(value,))
    brain.patch(record, version=2, add=(value,))
    state = brain.call("facet.assignment.get", record_id=record)["item"]
    assert state["version"] == 3
    assert state["memberships"] == [{"value_id": value, "membership_version": 3}]


def test_f12_display_rename_preserves_saved_selector_and_selection_version(
    brain: FacetHarness,
) -> None:
    dimension = brain.dimension()
    value = brain.value(dimension)
    query = brain.write(
        "facet.query.create",
        expected_version=0,
        name="Original",
        selector={"all_value_ids": [value]},
    )["subject_id"]
    original = brain.call("facet.query.get", query_id=query)["item"]
    brain.write("facet.dimension.rename", dimension_id=dimension, expected_version=1, name="Area")
    brain.write("facet.value.rename", value_id=value, expected_version=1, name="Renamed")
    brain.write(
        "facet.query.update",
        query_id=query,
        expected_version=1,
        name="Display rename",
        selector=original["selector"],
    )
    updated = brain.call("facet.query.get", query_id=query)["item"]
    assert updated["version"] == 2 and updated["selection_version"] == 1
    assert updated["selector"] == original["selector"]


def test_f15_assignment_grant_never_authorizes_catalogue_mutation(brain: FacetHarness) -> None:
    authority = EffectiveAuthority(
        "synthetic-assigner", "assigner", frozenset({"facet-assign"}), None
    )
    before = brain.rows("facet_dimensions")
    with pytest.raises(FacetError, match="unsupported_capability"):
        brain.call(
            "facet.dimension.create",
            name="Denied",
            expected_version=0,
            operation_id="denied-catalogue",
            authority=authority,
        )
    assert brain.rows("facet_dimensions") == before
    assert brain.rows("facet_operations") == []


def test_fixture_f17_replay_rejects_independent_receipt_and_projection_tampering() -> None:
    """Fixture-only semantic validation, not runtime export/import acceptance."""
    kit: dict[str, Any] = json.loads((FIXTURES / "positive/portable-replay.json").read_bytes())
    mutations = []
    wrong_state = deepcopy(kit)
    wrong_state["state"]["subjects"][0]["version"] += 1
    mutations.append(wrong_state)
    wrong_receipt = deepcopy(kit)
    wrong_receipt["operations"]["operations"][0]["receipt_json"]["version"] += 1
    mutations.append(wrong_receipt)
    for changed in mutations:
        with pytest.raises(FacetEvidenceError):
            validate_portable_facet_sidecars(
                changed["events"], changed["operations"], changed["state"]
            )


def _saved_selection(brain: FacetHarness) -> tuple[str, str, str]:
    value, record = brain.value(brain.dimension()), brain.capture()
    brain.patch(record, add=(value,))
    query = brain.write(
        "facet.query.create",
        name="Publication sources",
        expected_version=0,
        selector={"all_value_ids": [value], "record_types": ["source"]},
    )["subject_id"]
    return query, value, record


def _propose(brain: FacetHarness, query: str, **changes: Any) -> dict[str, Any]:
    return brain.write(
        "review.propose_from_facet_query",
        **{
            "query_id": query,
            "expected_query_version": 1,
            "target_page_id": None,
            "expected_page_revision_id": None,
            "destination_space_id": brain.engine.inbox.spaces()[0].space_id,
            "title": "Synthetic facet publication",
            "markdown": "Synthetic reviewed body.",
            **changes,
        },
    )


def _approve(brain: FacetHarness, proposal: dict[str, Any], *, digest: str | None = None) -> str:
    brain.serial += 1
    decision = brain.engine.review.decide(
        proposal["proposal_id"],
        DecisionOutcome.APPROVED,
        delivery_id=f"independent-approve-{brain.serial}",
        expected_review_digest=digest
        or brain.engine.review.show(proposal["proposal_id"]).review_digest,
    )
    assert decision.page_id is not None
    return decision.page_id


def test_f09_cross_space_sources_publish_to_explicit_independent_destination(
    brain: FacetHarness,
) -> None:
    query, value, first = _saved_selection(brain)
    other = brain.engine.inbox.create_space("Other source space", delivery_id="other-source-space")
    captured = brain.engine.capture.accept(
        TextPayload("Synthetic second routed source"),
        delivery_id="cross-space-source",
        space_id=other.space_id,
        privacy_tier=PrivacyTier.WORK,
    )
    assert isinstance(captured, CaptureReceipt)
    brain.patch(captured.capture_id, add=(value,))
    destination = brain.engine.inbox.create_space(
        "Explicit publication destination", delivery_id="explicit-destination"
    )
    proposal = _propose(brain, query, destination_space_id=destination.space_id)
    inspected = brain.engine.review.show(proposal["proposal_id"])
    assert set(inspected.selected_capture_ids) == {first, captured.capture_id}
    assert inspected.space_id == destination.space_id
    page = _approve(brain, proposal)
    attachment = brain.call("facet.query.attachment.get", page_id=page)["item"]
    assert attachment["query_id"] == query and attachment["version"] == 1
    state = brain.call("facet.assignment.get", record_id=page)["item"]
    assert state["version"] == 0 and state["memberships"] == []


@pytest.mark.parametrize("count", [0, 33])
def test_f10_publication_refuses_empty_or_overflowing_complete_selection(
    brain: FacetHarness,
    count: int,
) -> None:
    value = brain.value(brain.dimension())
    for number in range(count):
        brain.patch(brain.capture(f"Synthetic selection bound {number}"), add=(value,))
    query = brain.write(
        "facet.query.create",
        name="Bounded publication",
        expected_version=0,
        selector={"all_value_ids": [value], "record_types": ["source"]},
    )["subject_id"]
    with pytest.raises(FacetError) as error:
        _propose(brain, query)
    assert error.value.code == ("invalid_arguments" if count == 0 else "limit_exceeded")
    assert brain.engine.review.list() == ()


def test_f10_canonical_only_selector_cannot_be_publication_evidence(brain: FacetHarness) -> None:
    value = brain.value(brain.dimension())
    query = brain.write(
        "facet.query.create",
        name="Canonical-only",
        expected_version=0,
        selector={"all_value_ids": [value], "record_types": ["canonical"]},
    )["subject_id"]
    with pytest.raises(FacetError, match="invalid_arguments"):
        _propose(brain, query)


def test_f11_pending_proposal_conflicts_after_membership_remove_restore(
    brain: FacetHarness,
) -> None:
    query, value, record = _saved_selection(brain)
    proposal = _propose(brain, query)
    digest = brain.engine.review.show(proposal["proposal_id"]).review_digest
    brain.patch(record, version=1, remove=(value,))
    brain.patch(record, version=2, add=(value,))
    with pytest.raises(ValueError, match="conflict"):
        _approve(brain, proposal, digest=digest)


def test_f12_new_match_display_rename_and_value_archive_do_not_rebind_proposal(
    brain: FacetHarness,
) -> None:
    query, value, original = _saved_selection(brain)
    proposal = _propose(brain, query)
    inspected = brain.engine.review.show(proposal["proposal_id"])
    brain.patch(brain.capture("New match after proposal"), add=(value,))
    selector = brain.call("facet.query.get", query_id=query)["item"]["selector"]
    brain.write(
        "facet.query.update",
        query_id=query,
        name="Renamed query",
        selector=selector,
        expected_version=1,
    )
    brain.write("facet.value.rename", value_id=value, name="Renamed value", expected_version=1)
    brain.write("facet.value.archive", value_id=value, expected_version=2)
    _approve(brain, proposal, digest=inspected.review_digest)
    published = brain.engine.review.show(proposal["proposal_id"])
    assert published.selected_capture_ids == (original,)


def test_f13_f14_shared_evidence_pages_have_independent_attachments_and_assignments(
    brain: FacetHarness,
) -> None:
    query, value, _ = _saved_selection(brain)
    first = _propose(brain, query, title="Independent page one")
    second = _propose(brain, query, title="Independent page two")
    pages = (_approve(brain, first), _approve(brain, second))
    assert pages[0] != pages[1]
    selector = brain.call("facet.query.get", query_id=query)["item"]["selector"]
    for page in pages:
        state = brain.call("facet.assignment.get", record_id=page)["item"]
        assert state["version"] == 0 and state["memberships"] == []
        assert brain.call("facet.query.attachment.get", page_id=page)["item"]["query_id"] == query
    state = brain.call("facet.assignment.get", record_id=pages[0])["item"]
    brain.patch(pages[0], add=(value,), revision=state["revision_id"])
    assert brain.call("facet.query.get", query_id=query)["item"]["selector"] == selector
    assert brain.call("facet.assignment.get", record_id=pages[1])["item"]["memberships"] == []


def test_f18_pending_facet_proposal_survives_clean_import_and_valid_approval(
    brain: FacetHarness,
) -> None:
    query, _, selected = _saved_selection(brain)
    proposal = _propose(brain, query)
    original = brain.engine.review.show(proposal["proposal_id"])
    archive, restored = brain.root.parent / "export", brain.root.parent / "restored"
    exported = brain.engine.portability.export(
        archive, export_id="export_00000000-0000-4000-8000-000000000010"
    )
    assert exported.schema_version == 15
    brain.engine.portability.import_clean(
        archive, restored, import_id="import_00000000-0000-4000-8000-000000000011"
    )
    identity = restored.stat()
    brain.engine = BrainEngine.open(
        replace(brain.profile, root=restored, root_identity=(identity.st_dev, identity.st_ino))
    )
    imported = brain.engine.review.show(proposal["proposal_id"])
    assert imported.review_digest == original.review_digest
    assert imported.selected_capture_ids == (selected,)
    page = _approve(brain, proposal, digest=imported.review_digest)
    assert brain.call("facet.query.attachment.get", page_id=page)["item"]["query_id"] == query


def _managed_projection(brain: FacetHarness) -> tuple[str, str, str, Path]:
    value = brain.value(brain.dimension())
    brain.engine.capture.accept(
        TextPayload("Synthetic managed canonical body"),
        delivery_id="managed-canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=brain.engine.inbox.spaces()[0].space_id,
    )
    page = brain.engine.retrieval.search(
        "Synthetic managed canonical body", record_type="canonical"
    )[0].result_id
    revision = brain.call("facet.assignment.get", record_id=page)["item"]["revision_id"]
    brain.patch(page, add=(value,), revision=revision)
    workspace = brain.root.parent / "vault"
    workspace.mkdir(mode=0o700)
    workspace_id = brain.engine.managed_workspace.setup(
        str(workspace), operation_id="managed-setup"
    ).workspace_id
    brain.engine.managed_workspace.refresh(workspace_id, operation_id="managed-initial-refresh")
    file = next(workspace.rglob(f"{page}.md"))
    assert "facets" in parse_markdown(file.read_bytes()).fields
    return workspace_id, page, value, file


def test_f20_f21_missing_facet_subtree_preview_requires_apply_and_preserves_body(
    brain: FacetHarness,
) -> None:
    workspace, page, value, file = _managed_projection(brain)
    original_revision = brain.call("facet.assignment.get", record_id=page)["item"]["revision_id"]
    parsed = parse_markdown(file.read_bytes())
    fields = {key: val for key, val in parsed.fields.items() if key != "facets"}
    file.write_text(render_markdown(fields=fields, body=parsed.body))
    preview = brain.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    assert preview["conflicts"] == []
    assert preview["add_value_ids"] == [] and preview["remove_value_ids"] == [value]
    assert brain.call("facet.assignment.get", record_id=page)["item"]["version"] == 1
    applied = brain.call(
        "facet.vault.apply", preview_id=preview["preview_id"], operation_id="managed-remove"
    )
    assert applied["receipt"]["version"] == 2
    after = parse_markdown(file.read_bytes())
    assert after.body == parsed.body
    assert {key: val for key, val in after.fields.items() if key != "facets"} == fields
    assert (
        brain.call("facet.assignment.get", record_id=page)["item"]["revision_id"]
        == original_revision
    )


def test_f20_file_changed_after_preview_cannot_commit_assignment(brain: FacetHarness) -> None:
    workspace, page, _, file = _managed_projection(brain)
    preview = brain.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    version = brain.call("facet.assignment.get", record_id=page)["item"]["version"]
    file.write_bytes(file.read_bytes() + b"Owner edit after preview.\n")
    edited = file.read_bytes()
    with pytest.raises(FacetError) as error:
        brain.call("facet.vault.apply", preview_id=preview["preview_id"], operation_id="stale-file")
    assert error.value.code in {"preview_stale", "projection_conflict"}
    assert file.read_bytes() == edited
    assert brain.call("facet.assignment.get", record_id=page)["item"]["version"] == version


def test_f20_unknown_label_preview_never_creates_vocabulary(brain: FacetHarness) -> None:
    workspace, page, _, file = _managed_projection(brain)
    parsed = parse_markdown(file.read_bytes())
    fields = {**parsed.fields, "facets": {"Project": ["Unknown synthetic value"]}}
    file.write_text(render_markdown(fields=fields, body=parsed.body))
    before = brain.call(
        "facet.value.list",
        dimension_id=brain.call("facet.dimension.list")["items"][0]["dimension_id"],
    )
    preview = brain.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    assert {"code": "unknown_name", "value_id": None} in preview["conflicts"]
    after = brain.call(
        "facet.value.list",
        dimension_id=brain.call("facet.dimension.list")["items"][0]["dimension_id"],
    )
    assert after == before


def test_f19_legacy_v1_clean_import_has_empty_facets_after_current_admission(
    brain: FacetHarness,
) -> None:
    fixture = Path(__file__).parents[2] / "src/open_brain_engine/portable/conformance/v1/brain-root"
    target = brain.root.parent / "legacy-import"
    receipt = brain.engine.portability.import_clean(
        fixture, target, import_id="import_00000000-0000-4000-8000-000000000021"
    )
    assert receipt.schema_version == 1
    with sqlite3.connect(target / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        assert connection.execute("SELECT count(*) FROM facet_dimensions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM facet_events").fetchone()[0] == 0


def test_f19_future_portable_format_is_refused_before_creating_destination(
    brain: FacetHarness,
) -> None:
    brain.capture()
    archive, target = brain.root.parent / "future-export", brain.root.parent / "future-target"
    brain.engine.portability.export(
        archive, export_id="export_00000000-0000-4000-8000-000000000022"
    )
    manifest = archive / "portable-manifest.json"
    value = json.loads(manifest.read_bytes())
    value.update(schema_version=999, layout_version=999, contract_version="999")
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        brain.engine.portability.import_clean(
            archive, target, import_id="import_00000000-0000-4000-8000-000000000023"
        )
    assert not target.exists()


def test_f19_ordinary_pending_ingress_retains_main_lossless_portable_custody(
    brain: FacetHarness,
) -> None:
    submission = CaptureSubmission.for_local_owner(
        profile=brain.profile,
        payload=TextPayload("Synthetic pending ingress"),
        delivery_id="independent-pending-ingress",
    )
    brain.engine.ingestion.enqueue(submission)
    original = _journal_state(brain.profile)
    target = brain.root.parent / "pending-export"
    exported = brain.engine.portability.export(
        target, export_id="export_00000000-0000-4000-8000-000000000024"
    )
    assert exported.schema_version == 15
    assert _journal_state(brain.profile) == original
    restored = brain.root.parent / "pending-restored"
    brain.engine.portability.import_clean(
        target, restored, import_id="import_00000000-0000-4000-8000-000000000027"
    )
    identity = restored.stat()
    restored_profile = replace(
        brain.profile, root=restored, root_identity=(identity.st_dev, identity.st_ino)
    )
    assert _journal_state(restored_profile) == original
    restored_engine = BrainEngine.open(restored_profile)
    replay = restored_engine.capture.submit(submission)
    assert isinstance(replay, CaptureReceipt) and replay.duplicate


def test_f21_generated_indexes_are_excluded_from_authoritative_portable_files(
    brain: FacetHarness,
) -> None:
    _, _, value, file = _managed_projection(brain)
    dimension = brain.call("facet.dimension.list")["items"][0]["dimension_id"]
    index = brain.root.parent / "vault/Facets" / dimension / f"{value}.md"
    assert index.is_file(), "The value index must be generated in the registered workspace"
    archive = brain.root.parent / "managed-export"
    brain.engine.portability.export(
        archive, export_id="export_00000000-0000-4000-8000-000000000025"
    )
    files = validated_portable_snapshot(archive).files
    assert not any(relative.startswith("Facets/") or "/Facets/" in relative for relative in files)


def test_f21_modified_generated_index_survives_refresh_and_blocks_export(
    brain: FacetHarness,
) -> None:
    workspace, _, value, _ = _managed_projection(brain)
    dimension = brain.call("facet.dimension.list")["items"][0]["dimension_id"]
    index = brain.root.parent / "vault/Facets" / dimension / f"{value}.md"
    assert index.is_file()
    edited = b"# Synthetic owner index edit\nPreserve these exact bytes.\n"
    index.write_bytes(edited)
    try:
        brain.engine.managed_workspace.refresh(workspace, operation_id="modified-index-refresh")
    except (FacetError, ManagedWorkspaceFailure) as error:
        assert error.code in {"projection_conflict", "conflict_open"}
    assert index.read_bytes() == edited
    target = brain.root.parent / "conflicted-export"
    with pytest.raises((ValueError, ManagedWorkspaceFailure)):
        brain.engine.portability.export(
            target, export_id="export_00000000-0000-4000-8000-000000000026"
        )
    assert not target.exists()


def test_f20_unchanged_historical_display_label_resolves_by_retained_mapping(
    brain: FacetHarness,
) -> None:
    workspace, page, value, file = _managed_projection(brain)
    old_fields = parse_markdown(file.read_bytes()).fields
    brain.write("facet.value.rename", value_id=value, expected_version=1, name="Current name")
    # The existing generated label may remain until foreground refresh; if it
    # does, preview must resolve that label through its retained ID mapping.
    assert "facets" in old_fields
    preview = brain.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    assert preview["conflicts"] == []
    assert preview["add_value_ids"] == [] and preview["remove_value_ids"] == []


def test_f20_expired_uncommitted_preview_fails_without_mutation(brain: FacetHarness) -> None:
    workspace, page, _, file = _managed_projection(brain)
    parsed = parse_markdown(file.read_bytes())
    file.write_text(
        render_markdown(
            fields={key: val for key, val in parsed.fields.items() if key != "facets"},
            body=parsed.body,
        )
    )
    preview = brain.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    brain.engine = BrainEngine.open(
        brain.profile, clock=lambda: datetime(2026, 10, 13, tzinfo=UTC) + timedelta(minutes=16)
    )
    with pytest.raises(FacetError, match="preview_stale"):
        brain.call(
            "facet.vault.apply", preview_id=preview["preview_id"], operation_id="expired-only"
        )
    assert brain.call("facet.assignment.get", record_id=page)["item"]["version"] == 1


def test_f20_expired_preview_cannot_apply_but_committed_retry_survives_expiry(
    brain: FacetHarness,
) -> None:
    workspace, page, value, file = _managed_projection(brain)
    parsed = parse_markdown(file.read_bytes())
    file.write_text(
        render_markdown(
            fields={key: val for key, val in parsed.fields.items() if key != "facets"},
            body=parsed.body,
        )
    )
    stale = brain.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    fresh = brain.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    committed = brain.call(
        "facet.vault.apply", preview_id=fresh["preview_id"], operation_id="expiry-retry"
    )
    assert committed["receipt"]["version"] == 2
    brain.engine = BrainEngine.open(
        brain.profile, clock=lambda: datetime(2026, 10, 13, tzinfo=UTC) + timedelta(minutes=16)
    )
    replayed = brain.call(
        "facet.vault.apply", preview_id=fresh["preview_id"], operation_id="expiry-retry"
    )
    assert replayed["receipt"] == committed["receipt"]
    with pytest.raises(FacetError, match="preview_stale"):
        brain.call(
            "facet.vault.apply",
            preview_id=stale["preview_id"],
            operation_id="expired-fresh-operation",
        )
    assert brain.call("facet.assignment.get", record_id=page)["item"]["memberships"] == []
    assert value in stale["remove_value_ids"]


def public_revision(engine: BrainEngine, sequence: int, head: str | None) -> SourceRevisionReceipt:
    """Submit explicit proven ordering through the existing public source seam."""
    actor = "actor_00000000-0000-4000-8000-000000000091"
    context = PublicJobCaptureContext.create(
        profile=engine.profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000092",
            "role_id": "role_00000000-0000-4000-8000-000000000093",
            "tenant_id": engine.profile.tenant_id,
        },
    )
    reference = "https://example.test/independent-proven-record"
    submission = CaptureSubmission.for_public_job(
        context=context,
        payload=TextPayload(f"Synthetic proven revision {sequence}"),
        delivery_id="independent-proven-source",
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=reference,
        provenance=Provenance.create(
            source_ref=reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.WORK,
            reason=PrivacyReason.POLICY_WORK,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )
    return engine.sources.submit_revision(
        SourceRevisionSubmission(
            capture=submission,
            namespace={
                "connector_name": "synthetic",
                "connection_id": "independent",
                "resource_id": "record",
                "external_id": "one",
            },
            revision_key=str(sequence),
            canonical_sha256=submission.request_sha256(),
            expected_head=head,
            ordering={
                "kind": "monotonic",
                "provider_namespace": "synthetic",
                "epoch": "one",
                "sequence": sequence,
            },
            expected_control_epoch=0,
        )
    )


def test_f02_proven_successor_inherits_membership_without_classification_event(
    brain: FacetHarness,
) -> None:
    first = public_revision(brain.engine, 1, None)
    assert first.capture_id is not None and first.source_id is not None
    brain.engine.sources.route(
        SourceRouteRequest(
            source_id=first.source_id,
            expected_head=first.capture_id,
            expected_route_version=0,
            space_id=brain.engine.inbox.spaces()[0].space_id,
            operation_id="operation_00000000-0000-4000-8000-000000000094",
        ),
        authority=brain.authority,
    )
    value = brain.value(brain.dimension())
    brain.patch(first.capture_id, add=(value,))
    before = brain.rows("facet_events")
    next_revision = public_revision(brain.engine, 2, first.capture_id)
    assert next_revision.capture_id is not None and next_revision.source_id == first.source_id
    assert next_revision.capture_id != first.capture_id
    state = brain.call("facet.assignment.get", record_id=next_revision.capture_id)["item"]
    assert state["subject"] == {"kind": "source", "id": first.source_id}
    assert state["version"] == 1 and state["memberships"] == [
        {"value_id": value, "membership_version": 1}
    ]
    assert brain.rows("facet_events") == before
    result = brain.call("facet.query.page", selector={"all_value_ids": [value]})
    assert [row["record"]["record_id"] for row in result["items"]] == [next_revision.capture_id]
