"""Frozen binding checks and real publication cases requiring admitted E storage."""

from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
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
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.contracts import (
    CaptureFault,
    CaptureReceipt,
    CaptureSubmission,
    DecisionOutcome,
    InjectedFault,
    LocalEngineContext,
    PublicJobCaptureContext,
    TextPayload,
)
from open_brain_engine.engine.facet_publication import (
    FacetPublicationTasks,
    build_facet_binding,
    ordered_provenance,
    selected_capture_ids,
    verify_facet_snapshot,
)
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.review_bound import validate_current_binding
from open_brain_engine.engine.source_intake import SourceRevisionReceipt, SourceRevisionSubmission
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    RecordReadRequest,
    SourceRouteRequest,
)
from open_brain_engine.portable.review_binding import review_binding_digest, validate_review_binding
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v4 import canonical_revision_id
from open_brain_engine.providers.base import ProviderMode
from open_brain_engine.storage.markdown import parse_markdown

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "facets"


def _fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / name).read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "name",
    [
        "positive/review-create-cross-space.json",
        "positive/review-unrestricted-record-types.json",
        "positive/review-update.json",
    ],
)
def test_v5_accepts_c0_fixture(name: str) -> None:
    binding = _fixture(name)
    assert validate_review_binding(binding) == binding


@pytest.mark.parametrize(
    "name",
    [
        "negative/v5-patch.json",
        "negative/unsorted-binding-memberships.json",
        "negative/create-attachment-version.json",
    ],
)
def test_v5_rejects_c0_fixture(name: str) -> None:
    with pytest.raises(PortableValidationError):
        validate_review_binding(_fixture(name))


@pytest.mark.parametrize("version", [3, 4])
def test_historical_bindings_still_reject_cross_space_sources(version: int) -> None:
    binding = _fixture("positive/review-update.json")
    for key in (
        "selection_kind", "destination_space_id", "query_id", "query_version",
        "query_selection_version", "selector", "attachment_version", "facet_source_states",
    ):
        binding.pop(key)
    binding["schema_version"] = version
    if version == 4:
        body = "synthetic body\n"
        binding["patch"] = {
            "base_body": body,
            "base_body_sha256": sha256(body.encode()).hexdigest(),
            "target_page_sha256": binding["expected_page_sha256"],
            "operations": [{"start_byte": 0, "end_byte": 0, "replacement": "x"}],
        }
    with pytest.raises(PortableValidationError, match="review source"):
        validate_review_binding(binding)


def test_v5_binding_digest_covers_selection_and_proposed_bytes() -> None:
    base = _fixture("positive/review-create-cross-space.json")
    for key in (
        "selection_kind", "destination_space_id", "query_id", "query_version",
        "query_selection_version", "selector", "attachment_version", "facet_source_states",
    ):
        base.pop(key)
    proposal = {"synthetic_content": "first"}
    fixture = _fixture("positive/review-create-cross-space.json")
    binding = build_facet_binding(
        proposal,
        base,
        destination_space_id=fixture["destination_space_id"],
        query_id=fixture["query_id"],
        query_version=fixture["query_version"],
        query_selection_version=fixture["query_selection_version"],
        selector=fixture["selector"],
        attachment_version=fixture["attachment_version"],
        facet_source_states=fixture["facet_source_states"],
    )
    assert binding["review_digest"] == review_binding_digest(proposal, binding)
    assert (
        review_binding_digest({"synthetic_content": "changed"}, binding)
        != binding["review_digest"]
    )
    changed = deepcopy(binding)
    changed["destination_space_id"] = fixture["source_states"][1]["space_id"]
    assert review_binding_digest(proposal, changed) != binding["review_digest"]


def test_v5_validation_requires_decision_transaction() -> None:
    binding = _fixture("positive/review-create-cross-space.json")
    with pytest.raises(ValueError, match="transactional snapshot"):
        validate_current_binding(object(), binding)


def _snapshot(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": {
            "query_id": binding["query_id"],
            "archived": False,
            "version": binding["query_version"],
            "selection_version": binding["query_selection_version"],
            "selector": binding["selector"],
        },
        "attachment": None,
        "selected_heads": {
            state["source_id"]: {
                "capture_id": state["capture_id"],
                "head_version": state["head_version"],
                "memberships": {
                    item["value_id"]: item["membership_version"]
                    for item in state["memberships"]
                },
            }
            for state in binding["facet_source_states"]
        },
        "provenance_states": {
            state["capture_id"]: {
                key: state[key] for key in ("sha256", "route_id", "space_id")
            }
            for state in binding["source_states"]
        },
        "authorized_capture_ids": frozenset(binding["provenance"]),
        "authority": EffectiveAuthority(
            principal_id="synthetic-owner", session_id="synthetic-session",
            capabilities=frozenset(), space_ids=None, owner=True,
        ),
        "target": None,
    }


def test_snapshot_ignores_display_rename_new_match_and_unrelated_membership() -> None:
    binding = _fixture("positive/review-create-cross-space.json")
    current = _snapshot(binding)
    current["query"]["version"] += 1
    current["query"]["name"] = "Renamed query"
    current["selected_heads"]["source_extra"] = {"new_match": True}
    head = next(iter(current["selected_heads"].values()))
    head["memberships"]["facetval_unrelated"] = 1
    verify_facet_snapshot(binding, **current)


@pytest.mark.parametrize("change", ["membership", "head", "route", "query", "authority"])
def test_snapshot_rejects_relevant_drift(change: str) -> None:
    binding = _fixture("positive/review-create-cross-space.json")
    current = _snapshot(binding)
    head = next(iter(current["selected_heads"].values()))
    if change == "membership":
        value_id = binding["selector"]["all_value_ids"][0]
        head["memberships"][value_id] += 2  # remove and restore
    elif change == "head":
        head["head_version"] += 1
    elif change == "route":
        capture_id = binding["provenance"][0]
        current["provenance_states"][capture_id]["route_id"] = None
    elif change == "query":
        current["query"]["selection_version"] += 1
    else:
        current["authorized_capture_ids"] = frozenset()
    with pytest.raises(ValueError, match="conflict"):
        verify_facet_snapshot(binding, **current)


def test_update_attachment_and_target_are_precise() -> None:
    binding = _fixture("positive/review-update.json")
    current = _snapshot(binding)
    current["attachment"] = {
        "page_id": binding["page_id"], "query_id": binding["query_id"],
        "version": binding["attachment_version"],
    }
    current["target"] = {
        "space_id": binding["destination_space_id"],
        "publication_id": binding["expected_publication_id"],
        "sha256": binding["expected_page_sha256"],
    }
    verify_facet_snapshot(binding, **current)
    current["attachment"]["version"] += 1
    with pytest.raises(ValueError, match="target conflict"):
        verify_facet_snapshot(binding, **current)


def test_selection_and_cumulative_provenance_are_bounded() -> None:
    assert ordered_provenance(("old", "shared"), ("shared", "new")) == (
        "old", "shared", "new"
    )
    assert selected_capture_ids(({"capture_id": "one", "source_id": "source-one"},)) == (
        "one",
    )
    with pytest.raises(ValueError, match="selected no sources"):
        selected_capture_ids(())
    with pytest.raises(ValueError, match="limit exceeded"):
        selected_capture_ids(
            tuple({"capture_id": str(n), "source_id": str(n)} for n in range(33))
        )
    with pytest.raises(ValueError, match="limit exceeded"):
        ordered_provenance(tuple(str(n) for n in range(32)), ("new",))


class LiveFacetBrain:
    """Only the real admitted engine; missing E/schema dependencies fail visibly."""

    def __init__(self, tmp_path: Path) -> None:
        engine_sources = Path(__file__).resolve().parents[2] / "src/open_brain_engine/engine"
        for module in ("facets.py", "facet_queries.py", "facet_store.py"):
            if not (engine_sources / module).is_file():
                pytest.fail(f"E producer unavailable in this worktree: {module}")
        from open_brain_engine.engine.facets import FacetTasks

        root = tmp_path / "synthetic-facet-publication"
        root.mkdir(mode=0o700)
        (root / ".open-brain").mkdir(mode=0o700)
        tenant = "tenant_00000000-0000-4000-8000-000000000001"
        actor = "actor_00000000-0000-4000-8000-000000000002"
        self.profile = LocalEngineContext(
            root=root, root_identity=(root.stat().st_dev, root.stat().st_ino),
            tenant_id=tenant, owner_actor_id=actor,
            owner_role_claim={
                "actor_id": actor, "tenant_id": tenant, "capabilities": ["owner"],
                "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000003",
                "role_id": "role_00000000-0000-4000-8000-000000000004",
            },
            provider_mode=ProviderMode.NONE, starter_spaces=("First", "Second"),
        )
        self.clock = lambda: datetime(2026, 10, 13, 12, tzinfo=UTC)
        self.engine = BrainEngine.open(self.profile, clock=self.clock)
        connection = self.engine._store.connect()
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 23, (
                "Coordinator schema-16 admission is required; no substitute test store is used"
            )
        finally:
            connection.close()
        self.facets = FacetTasks(self.engine)
        self.publication = FacetPublicationTasks(self.engine)
        self.owner = EffectiveAuthority(
            principal_id=actor, session_id="synthetic-owner", capabilities=frozenset(),
            space_ids=None, owner=True,
        )
        self.sequence = 0
        self.spaces = self.engine.inbox.spaces()
        self.dimension = self.write("facet.dimension.create", {
            "name": "Project", "expected_version": 0,
        })["subject_id"]
        self.value = self.new_value("Apollo")
        self.unrelated = self.new_value("Unrelated")
        self.captures = [self.capture(space.space_id) for space in self.spaces]
        self.captures.append(self.capture(None))
        for capture in self.captures:
            self.patch(capture, expected_version=0, add=[self.value])
        self.selector = fc.FacetSelector(all_value_ids=[self.value]).to_wire()
        self.query = self.write("facet.query.create", {
            "name": "Apollo sources", "selector": self.selector, "expected_version": 0,
        })["subject_id"]

    def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        request = fc.request_from_wire(method, {"dto_version": 1, **args})
        result = getattr(self.facets, fc.OPERATION_METHODS[method])(request, authority=self.owner)
        return cast(dict[str, Any], result.to_wire())

    def write(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        return self.call(method, {"operation_id": f"synthetic-operation-{self.sequence}", **args})

    def new_value(self, name: str) -> str:
        return cast(str, self.write("facet.value.create", {
            "name": name, "dimension_id": self.dimension, "expected_dimension_version": 1,
            "expected_version": 0,
        })["subject_id"])

    def capture(
        self, space_id: str | None, privacy_tier: PrivacyTier = PrivacyTier.WORK
    ) -> CaptureReceipt:
        self.sequence += 1
        return cast(CaptureReceipt, self.engine.capture.accept(
            TextPayload(f"Synthetic evidence {self.sequence}"),
            delivery_id=f"synthetic-capture-{self.sequence}", space_id=space_id,
            privacy_tier=privacy_tier,
        ))

    def patch(
        self, capture: CaptureReceipt, *, expected_version: int,
        add: list[str] | None = None, remove: list[str] | None = None,
    ) -> None:
        self.write("facet.assignment.patch", {
            "record_id": capture.capture_id, "expected_revision_id": capture.capture_id,
            "expected_version": expected_version, "add_value_ids": add or [],
            "remove_value_ids": remove or [],
        })

    def propose(self, operation_id: str, **overrides: Any) -> dict[str, Any]:
        args = {
            "operation_id": operation_id, "query_id": self.query, "expected_query_version": 1,
            "target_page_id": None, "expected_page_revision_id": None,
            "destination_space_id": self.spaces[0].space_id,
            "title": "Synthetic facet page", "markdown": "Frozen synthetic synthesis.",
            **overrides,
        }
        return self.publication.propose_from_facet_query(
            fc.ProposeFromFacetQueryRequest(**args), authority=self.owner
        ).to_wire()

    def approve(self, proposal: dict[str, Any]) -> Any:
        inspected = self.engine.review.show(proposal["proposal_id"])
        return self.engine.review.decide(
            inspected.proposal_id, DecisionOutcome.APPROVED,
            delivery_id="decision-" + inspected.proposal_id,
            expected_review_digest=inspected.review_digest,
        )


@pytest.fixture
def live_brain(tmp_path: Path) -> LiveFacetBrain:
    return LiveFacetBrain(tmp_path)


def test_real_cross_space_create_update_and_independent_pages(live_brain: LiveFacetBrain) -> None:
    brain = live_brain
    proposal = brain.propose("create-one")
    assert brain.propose("create-one") == proposal
    decision = brain.approve(proposal)
    attachment = brain.call("facet.query.attachment.get", {"page_id": proposal["page_id"]})
    assert attachment["item"]["query_id"] == brain.query
    assert attachment["item"]["version"] == 1
    assert brain.call("facet.assignment.get", {"record_id": proposal["page_id"]})["item"][
        "memberships"
    ] == []
    second = brain.propose("create-two", destination_space_id=brain.spaces[1].space_id)
    brain.approve(second)
    assert second["page_id"] != proposal["page_id"]
    update = brain.propose(
        "update-one", target_page_id=proposal["page_id"], destination_space_id=None,
        expected_page_revision_id=canonical_revision_id(decision.publication_id),
    )
    assert update["space_id"] == proposal["space_id"]
    brain.approve(update)
    assert brain.call("facet.query.attachment.get", {"page_id": proposal["page_id"]}) == attachment


def test_real_remove_restore_conflicts(live_brain: LiveFacetBrain) -> None:
    proposal = live_brain.propose("stale-membership")
    capture = live_brain.captures[0]
    live_brain.patch(capture, expected_version=1, remove=[live_brain.value])
    live_brain.patch(capture, expected_version=2, add=[live_brain.value])
    with pytest.raises(ValueError, match="conflict"):
        live_brain.approve(proposal)


@pytest.mark.parametrize("change", ["route", "query", "archive", "attachment", "target", "draft"])
def test_real_bound_evidence_drift_conflicts(live_brain: LiveFacetBrain, change: str) -> None:
    brain = live_brain
    target_args: dict[str, Any] = {}
    if change in {"attachment", "target"}:
        original = brain.propose("base-page")
        decision = brain.approve(original)
        target_args = {
            "target_page_id": original["page_id"], "destination_space_id": None,
            "expected_page_revision_id": canonical_revision_id(decision.publication_id),
        }
    proposal = brain.propose("bound-draft", **target_args)
    if change == "route":
        brain.engine.inbox.route(
            brain.captures[-1].capture_id, brain.spaces[0].space_id,
            delivery_id="synthetic-reroute",
        )
    elif change == "query":
        brain.write("facet.query.update", {
            "query_id": brain.query, "expected_version": 1, "name": "Changed",
            "selector": fc.FacetSelector(all_value_ids=[brain.unrelated]).to_wire(),
        })
    elif change == "archive":
        brain.write("facet.query.archive", {"query_id": brain.query, "expected_version": 1})
    elif change == "attachment":
        other = brain.write("facet.query.create", {
            "name": "Other query", "selector": brain.selector, "expected_version": 0,
        })["subject_id"]
        brain.write("facet.query.attach", {
            "page_id": proposal["page_id"], "query_id": other,
            "expected_version": 1, "expected_query_version": 1,
            "expected_revision_id": target_args["expected_page_revision_id"],
        })
    elif change == "target":
        brain.approve(brain.propose("other-update", **target_args))
    else:
        with brain.engine._store.transaction() as connection:
            connection.execute(
                "UPDATE proposals SET proposed_bytes=? WHERE proposal_id=?",
                (b"tampered synthetic draft", proposal["proposal_id"]),
            )
    with pytest.raises(ValueError):
        brain.approve(proposal)


def test_real_rename_new_match_and_unrelated_assignment_do_not_stale(
    live_brain: LiveFacetBrain,
) -> None:
    brain = live_brain
    proposal = brain.propose("stable-selection")
    brain.write("facet.query.update", {
        "query_id": brain.query, "expected_version": 1,
        "name": "Renamed", "selector": brain.selector,
    })
    brain.patch(brain.captures[0], expected_version=1, add=[brain.unrelated])
    new_capture = brain.capture(None)
    brain.patch(new_capture, expected_version=0, add=[brain.value])
    inspected = brain.engine.review.show(proposal["proposal_id"])
    assert new_capture.capture_id not in inspected.selected_capture_ids
    brain.approve(proposal)


def test_real_current_authority_covers_every_source(live_brain: LiveFacetBrain) -> None:
    brain = live_brain
    proposal = brain.propose("scoped-review")
    authority = EffectiveAuthority(
        principal_id="synthetic-reviewer", session_id="synthetic-session",
        capabilities=frozenset({"review-read", "review-decide"}),
        space_ids=frozenset({proposal["space_id"]}),
    )
    review = brain.engine.review.with_authority(authority)
    with pytest.raises(ValueError, match="not_found"):
        review.show(proposal["proposal_id"])
    assert review.list() == ()
    with pytest.raises(ValueError, match="not_found"):
        review.decide(
            proposal["proposal_id"], DecisionOutcome.APPROVED, delivery_id="denied-decision",
            expected_review_digest=brain.engine.review.show(proposal["proposal_id"]).review_digest,
        )


@pytest.mark.parametrize("count", [0, 33])
def test_real_selection_bounds_do_not_partially_reserve(
    live_brain: LiveFacetBrain, count: int,
) -> None:
    brain = live_brain
    if count == 0:
        query_id = brain.write("facet.query.create", {
            "name": "No sources", "expected_version": 0,
            "selector": fc.FacetSelector(all_value_ids=[brain.unrelated]).to_wire(),
        })["subject_id"]
    else:
        query_id = brain.query
        for _ in range(count - len(brain.captures)):
            brain.patch(brain.capture(None), expected_version=0, add=[brain.value])
    with pytest.raises(fc.FacetError) as error:
        brain.propose("bounded-proposal", query_id=query_id)
    assert error.value.code == ("invalid_arguments" if count == 0 else "limit_exceeded")
    assert brain.engine.review.list() == ()


def test_real_page_assignment_does_not_change_query(live_brain: LiveFacetBrain) -> None:
    brain = live_brain
    proposal = brain.propose("assignment-independent")
    decision = brain.approve(proposal)
    before = brain.call("facet.query.get", {"query_id": brain.query})
    brain.write("facet.assignment.patch", {
        "record_id": proposal["page_id"],
        "expected_revision_id": canonical_revision_id(decision.publication_id),
        "expected_version": 0, "add_value_ids": [brain.unrelated], "remove_value_ids": [],
    })
    assert brain.call("facet.query.get", {"query_id": brain.query}) == before


def test_real_cumulative_provenance_overflow_does_not_reserve(
    live_brain: LiveFacetBrain,
) -> None:
    brain = live_brain
    proposal = brain.propose("first-generation")
    decision = brain.approve(proposal)
    for capture in brain.captures:
        brain.patch(capture, expected_version=1, remove=[brain.value])
    for _ in range(30):
        brain.patch(brain.capture(None), expected_version=0, add=[brain.value])
    with pytest.raises(fc.FacetError) as error:
        brain.propose(
            "overflow-update", target_page_id=proposal["page_id"], destination_space_id=None,
            expected_page_revision_id=canonical_revision_id(decision.publication_id),
        )
    assert error.value.code == "limit_exceeded"
    assert len(brain.engine.review.list()) == 1


def test_real_rejection_does_not_materialize_attachment(live_brain: LiveFacetBrain) -> None:
    brain = live_brain
    proposal = brain.propose("reject-facet-draft")
    inspected = brain.engine.review.show(proposal["proposal_id"])
    brain.engine.review.decide(
        inspected.proposal_id, DecisionOutcome.REJECTED,
        delivery_id="synthetic-rejection", expected_review_digest=inspected.review_digest,
    )
    connection = brain.engine._store.connect()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM facet_page_queries WHERE page_id=?", (proposal["page_id"],)
        ).fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize("mutation", ["selector", "archive"])
def test_real_admitted_publication_recovers_after_query_edit(
    live_brain: LiveFacetBrain, mutation: str,
) -> None:
    brain = live_brain
    proposal = brain.propose("recovery-query-edit")
    brain.engine._faults.add(CaptureFault.AFTER_DECISION_RESERVATION)
    with pytest.raises(InjectedFault):
        brain.approve(proposal)
    brain.engine._faults.clear()
    # A no-op and a display rename remain safe while the bound selection is reserved.
    brain.write("facet.query.update", {
        "query_id": brain.query, "expected_version": 1,
        "name": "Apollo sources", "selector": brain.selector,
    })
    brain.write("facet.query.update", {
        "query_id": brain.query, "expected_version": 1,
        "name": "Renamed during recovery", "selector": brain.selector,
    })
    method = "facet.query.update" if mutation == "selector" else "facet.query.archive"
    update: dict[str, Any] = {"query_id": brain.query, "expected_version": 2}
    if mutation == "selector":
        update.update({
            "name": "Changed selection",
            "selector": fc.FacetSelector(all_value_ids=[brain.unrelated]).to_wire(),
        })
    with pytest.raises(fc.FacetError) as error:
        brain.write(method, update)
    assert error.value.code == "operation_pending"
    reopened = BrainEngine.open(brain.profile, clock=brain.clock)
    brain.write(method, update)
    connection = reopened._store.connect()
    try:
        assert connection.execute(
            "SELECT query_id FROM facet_page_queries WHERE page_id=?", (proposal["page_id"],)
        ).fetchone()[0] == brain.query
    finally:
        connection.close()


@pytest.mark.parametrize("fault", [
    CaptureFault.AFTER_DECISION_RESERVATION,
    CaptureFault.AFTER_REVIEW_PAGE_WRITE,
    CaptureFault.AFTER_REVIEW_PUBLICATION_WRITE,
])
def test_real_publication_recovery_attaches_once(
    live_brain: LiveFacetBrain, fault: CaptureFault,
) -> None:
    brain = live_brain
    proposal = brain.propose("recover-attachment")
    brain.engine._faults.add(fault)
    with pytest.raises(InjectedFault):
        brain.approve(proposal)
    brain.engine._faults.clear()
    reopened = BrainEngine.open(brain.profile, clock=brain.clock)
    reopened = BrainEngine.open(brain.profile, clock=brain.clock)
    connection = reopened._store.connect()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM facet_page_queries WHERE page_id=?", (proposal["page_id"],)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM facet_operations WHERE operation_id LIKE 'publication-facet-%'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM facet_events WHERE kind='query.attach'"
        ).fetchone()[0] == 1
        event = connection.execute(
            "SELECT actor_id FROM facet_events WHERE kind='query.attach'"
        ).fetchone()
        assert event[0] == brain.profile.owner_actor_id
    finally:
        connection.close()


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



def test_real_selected_successor_stales_pending_and_current_target_but_retains_owner_history(
    live_brain: LiveFacetBrain,
) -> None:
    brain = live_brain
    first = public_revision(brain.engine, 1, None)
    assert first.capture_id is not None
    brain.write("facet.assignment.patch", {
        "record_id": first.capture_id, "expected_revision_id": first.capture_id,
        "expected_version": 0, "add_value_ids": [brain.value], "remove_value_ids": [],
    })
    published = brain.propose("before-successor")
    decision = brain.approve(published)
    pending = brain.propose("pending-before-successor")
    successor = public_revision(brain.engine, 2, first.capture_id)
    assert successor.capture_id != first.capture_id
    with pytest.raises(ValueError, match="head conflict"):
        brain.approve(pending)
    revision = canonical_revision_id(decision.publication_id)
    with pytest.raises(fc.FacetError, match="^not_found$"):
        brain.propose(
            "after-successor", target_page_id=published["page_id"], destination_space_id=None,
            expected_page_revision_id=revision,
        )
    retained = brain.engine.history.read_history(
        RecordReadRequest(record_id=published["page_id"], expected_revision_id=revision),
        authority=brain.owner,
    ).to_wire()
    summary = cast(dict[str, Any], retained["record"])
    assert first.capture_id in summary["provenance"]["capture_ids"]
    current = brain.propose("after-successor-current-sources")
    inspected = brain.engine.review.show(current["proposal_id"])
    assert first.capture_id not in inspected.selected_capture_ids
    assert successor.capture_id in inspected.selected_capture_ids
    brain.approve(current)


def test_real_privacy_join_survives_edit_and_inherited_provenance(
    live_brain: LiveFacetBrain,
) -> None:
    brain = live_brain
    personal = brain.capture(None, PrivacyTier.PERSONAL)
    brain.patch(personal, expected_version=0, add=[brain.value])
    proposal = brain.propose("mixed-privacy")
    inspected = brain.engine.review.show(proposal["proposal_id"])
    assert inspected.facet_binding_json is not None
    binding = json.loads(inspected.facet_binding_json)
    assert binding["schema_version"] == 5
    assert set(binding["selected_capture_ids"]) == set(inspected.selected_capture_ids)
    with closing(brain.engine._store.connect()) as connection:
        retained = connection.execute(
            "SELECT binding_json FROM review_contexts WHERE proposal_id=?",
            (inspected.proposal_id,),
        ).fetchone()[0]
    assert json.loads(retained) == binding
    limited = EffectiveAuthority(
        principal_id="synthetic-reviewer", session_id="synthetic-work-only",
        capabilities=frozenset({"review-read", "review-decide"}), space_ids=None,
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
    )
    review = brain.engine.review.with_authority(limited)
    assert review.list() == ()
    with pytest.raises(ValueError, match="not_found"):
        review.show(inspected.proposal_id)
    with pytest.raises(ValueError, match="not_found"):
        review.decide(
            inspected.proposal_id, DecisionOutcome.APPROVED, delivery_id="privacy-denied",
            expected_review_digest=inspected.review_digest,
        )
    reviewer = EffectiveAuthority(
        principal_id="synthetic-reviewer", session_id="synthetic-personal-review",
        capabilities=frozenset({"review-read", "review-decide"}), space_ids=None,
    )
    # Approval alone authorizes the already-bound attachment, with no proposal/query grants.
    decision = brain.engine.review.with_authority(reviewer).decide(
        inspected.proposal_id, DecisionOutcome.EDITED, delivery_id="edited-private",
        edited_markdown="Synthetic edited personal synthesis.",
        expected_review_digest=inspected.review_digest,
    )
    with closing(brain.engine._store.connect()) as connection:
        row = connection.execute(
            "SELECT effective_bytes FROM decisions WHERE decision_id=?", (decision.decision_id,),
        ).fetchone()
    privacy = cast(dict[str, Any], parse_markdown(row[0]).fields["privacy"])
    assert privacy["tier"] == "personal"
    assert privacy["authority"] == {"cloud": False, "external_egress": False}
    assert decision.publication_id is not None
    brain.patch(personal, expected_version=1, remove=[brain.value])
    update = brain.propose(
        "inherited-private", target_page_id=proposal["page_id"], destination_space_id=None,
        expected_page_revision_id=canonical_revision_id(decision.publication_id),
    )
    inherited = brain.engine.review.show(update["proposal_id"])
    assert personal.capture_id in inherited.capture_ids
    assert personal.capture_id not in inherited.selected_capture_ids
    with pytest.raises(ValueError, match="not_found"):
        review.show(inherited.proposal_id)
    approved = brain.approve(update)
    with closing(brain.engine._store.connect()) as connection:
        row = connection.execute(
            "SELECT effective_bytes FROM decisions WHERE decision_id=?", (approved.decision_id,),
        ).fetchone()
    assert parse_markdown(row[0]).fields["privacy"] == privacy


def test_v5_create_requires_complete_selected_provenance() -> None:
    binding = _fixture("positive/review-create-cross-space.json")
    binding["selected_capture_ids"].pop()
    binding["facet_source_states"].pop()
    with pytest.raises(PortableValidationError, match="provenance differs"):
        validate_review_binding(binding)


def route_proven_source(brain: LiveFacetBrain, capture_id: str, space_id: str) -> str:
    with closing(brain.engine._store.connect()) as connection:
        source = connection.execute(
            "SELECT s.* FROM source_revisions r JOIN logical_sources s USING(source_id) "
            "WHERE r.capture_id=?", (capture_id,),
        ).fetchone()
    brain.sequence += 1
    operation_id = f"operation_00000000-0000-4000-8000-{brain.sequence:012d}"
    brain.engine.sources.route(SourceRouteRequest(
        source_id=source["source_id"], space_id=space_id,
        expected_head=capture_id, expected_route_version=source["route_version"],
        operation_id=operation_id,
    ), authority=brain.owner)
    with closing(brain.engine._store.connect()) as connection:
        return cast(str, connection.execute(
            "SELECT route_id FROM route_operations WHERE delivery_id=?",
            ("source.route." + operation_id,),
        ).fetchone()[0])


def facet_state(brain: LiveFacetBrain, proposal_id: str, capture_id: str) -> dict[str, Any]:
    inspected = brain.engine.review.show(proposal_id)
    assert inspected.facet_binding_json is not None
    binding = json.loads(inspected.facet_binding_json)
    return cast(dict[str, Any], next(
        state for state in binding["source_states"] if state["capture_id"] == capture_id
    ))


def test_real_route_before_successor_binds_retained_ancestor_route(
    live_brain: LiveFacetBrain,
) -> None:
    brain = live_brain
    first = public_revision(brain.engine, 1, None)
    assert first.capture_id is not None
    brain.write("facet.assignment.patch", {
        "record_id": first.capture_id, "expected_revision_id": first.capture_id,
        "expected_version": 0, "add_value_ids": [brain.value], "remove_value_ids": [],
    })
    route_proven_source(brain, first.capture_id, brain.spaces[0].space_id)
    latest = route_proven_source(brain, first.capture_id, brain.spaces[1].space_id)
    successor = public_revision(brain.engine, 2, first.capture_id)
    assert successor.capture_id is not None
    with closing(brain.engine._store.connect()) as connection:
        original_bytes = connection.execute(
            "SELECT source_bytes FROM source_revisions WHERE capture_id=?",
            (successor.capture_id,),
        ).fetchone()[0]
    assert json.loads(original_bytes)["space_id"] is None
    proposal = brain.propose("ancestor-route-create")
    state = facet_state(brain, proposal["proposal_id"], successor.capture_id)
    assert state["route_id"] == latest
    assert state["space_id"] == brain.spaces[1].space_id
    assert state["sha256"] == sha256(original_bytes).hexdigest()
    brain.approve(proposal)
    with closing(brain.engine._store.connect()) as connection:
        assert connection.execute(
            "SELECT source_bytes FROM source_revisions WHERE capture_id=?",
            (successor.capture_id,),
        ).fetchone()[0] == original_bytes


def test_real_lineage_reroute_conflicts_and_successor_provenance_uses_current_route(
    live_brain: LiveFacetBrain,
) -> None:
    brain = live_brain
    first = public_revision(brain.engine, 1, None)
    assert first.capture_id is not None
    brain.write("facet.assignment.patch", {
        "record_id": first.capture_id, "expected_revision_id": first.capture_id,
        "expected_version": 0, "add_value_ids": [brain.value], "remove_value_ids": [],
    })
    original_route = route_proven_source(brain, first.capture_id, brain.spaces[0].space_id)
    base = brain.propose("lineage-base")
    published = brain.approve(base)
    successor = public_revision(brain.engine, 2, first.capture_id)
    assert successor.capture_id is not None
    target = {
        "target_page_id": base["page_id"], "destination_space_id": None,
        "expected_page_revision_id": canonical_revision_id(published.publication_id),
    }
    with pytest.raises(fc.FacetError, match="^not_found$"):
        brain.propose("lineage-stale-target", **target)
    pending = brain.propose("lineage-before-reroute")
    frozen = brain.engine.review.show(pending["proposal_id"]).facet_binding_json
    assert facet_state(brain, pending["proposal_id"], successor.capture_id)[
        "route_id"
    ] == original_route
    # A newer revision starts its own supersedes chain; old capture SQL space stays stale.
    route_proven_source(brain, successor.capture_id, brain.spaces[1].space_id)
    # Route away and back: the identity change still conflicts even if the space matches.
    current_route = route_proven_source(brain, successor.capture_id, brain.spaces[0].space_id)
    assert current_route != original_route
    assert brain.engine.review.show(pending["proposal_id"]).facet_binding_json == frozen
    with pytest.raises(ValueError, match="provenance conflict"):
        brain.approve(pending)
    # A new page uses the current successor route without reviving stale target eligibility.
    current_route = route_proven_source(brain, successor.capture_id, brain.spaces[1].space_id)
    update = brain.propose("lineage-after-reroute")
    state = facet_state(brain, update["proposal_id"], successor.capture_id)
    assert state["route_id"] == current_route
    assert state["space_id"] == brain.spaces[1].space_id
    with closing(brain.engine._store.connect()) as connection:
        assert connection.execute(
            "SELECT space_id FROM captures WHERE capture_id=?", (first.capture_id,),
        ).fetchone()[0] == brain.spaces[0].space_id
    brain.approve(update)
    # Frozen historical evidence remains inspectable after later routes.
    assert brain.engine.review.show(pending["proposal_id"]).facet_binding_json == frozen
