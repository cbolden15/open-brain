"""Both source witnesses invalidate historical output across retained projections."""

from dataclasses import replace
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.historical_tasks import link_historical_copy
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    RelationshipDecideRequest,
    RelationshipListRequest,
    SearchPageRequest,
    SourceRouteRequest,
    T03Error,
)

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission
from packages.app.tests.unit.engine.test_historical_link import _link_request
from packages.app.tests.unit.engine.test_paging import external_authority, scoped_authority, wire


@pytest.mark.parametrize("source", ["original", "copy"])
@pytest.mark.parametrize("mutation", ["withdraw", "route"])
@pytest.mark.parametrize("external", [False, True])
def test_either_source_change_hides_link_before_projection_and_after_rebuild_reopen(
    tmp_path: Path,
    source: str,
    mutation: str,
    external: bool,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    request, consent = _link_request(engine)
    owner = EffectiveAuthority(profile.owner_actor_id, "session", frozenset(), None, owner=True)
    with exclusive_runtime_admission(profile) as admission:
        receipt = link_historical_copy(
            profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            load_consent=lambda: consent,
        )
    reader = (
        replace(
            external_authority(engine),
            provider_id="openai",
            allowed_read_tiers=frozenset({PrivacyTier.PUBLIC}),
        )
        if external
        else scoped_authority(PrivacyTier.PUBLIC)
    )
    reader = replace(reader, capabilities=reader.capabilities | {"history-read"})
    copy_id = request.retained_copy.capture_id
    read = RecordReadRequest(record_id=copy_id, expected_revision_id=copy_id)
    search = SearchPageRequest(query="synthetic retained")
    history = HistoryListRequest(record_id=copy_id)
    control = engine.capture.submit(
        replace(
            _public_submission(engine.tasks, delivery_id="ordinary.public.control"),
            payload=TextPayload("ordinary public control"),
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.PUBLIC,
                reason=PrivacyReason.POLICY_PUBLIC,
                policy_version="synthetic.public.v1",
                authority=Authority(cloud=False, external_egress=True),
            ),
        )
    ).capture_id
    engine.relationships.decide(
        RelationshipDecideRequest(
            left={"record_id": copy_id, "revision_id": copy_id},
            right={"record_id": control, "revision_id": control},
            kind="duplicate_of",
            decision="accept",
            expected_relationship_version=0,
            operation_id="operation_" + str(uuid4()),
        ),
        authority=owner,
    )
    relations = RelationshipListRequest(record_id=copy_id)
    control_relations = RelationshipListRequest(record_id=control)
    engine.retrieval.read_record(read, authority=reader)
    engine.history.read_history(read, authority=reader)
    assert engine.history.list_history(history, authority=reader).to_wire()["entries"]
    assert engine.relationships.list_relationships(relations, authority=reader).to_wire()["entries"]
    assert [
        item["record_id"]
        for item in wire(engine.retrieval.search_page(search, authority=reader))["results"]
    ] == [copy_id]
    witness = request.source_cas if source == "original" else request.copy_source_cas
    if mutation == "withdraw":
        engine.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.synthetic",
                source_id=witness.source_id,
                expected_head=witness.expected_head,
                expected_lifecycle_version=witness.expected_lifecycle_version,
                brain_id=request.destination.brain_id,
                issuer_epoch=request.destination.issuer_epoch,
                reason_code="synthetic_owner_withdrawal",
            ),
            authority=owner,
        )
    else:
        space = engine.inbox.create_space("Synthetic route", delivery_id="space.synthetic")
        engine.sources.route(
            SourceRouteRequest(
                operation_id="operation_" + str(uuid4()),
                source_id=witness.source_id,
                expected_head=witness.expected_head,
                expected_route_version=witness.expected_route_version,
                space_id=space.space_id,
            ),
            authority=owner,
        )
    with exclusive_runtime_admission(profile) as admission:
        assert (
            link_historical_copy(
                profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                load_consent=lambda: consent,
            )
            == receipt
        )
    for current in (engine, BrainEngine.open(profile)):
        assert current.retrieval.search_page(search, authority=reader).to_wire()["results"] == []
        for operation in (
            partial(current.retrieval.read_record, read, authority=reader),
            partial(current.history.read_history, read, authority=reader),
            partial(current.history.list_history, history, authority=reader),
            partial(current.relationships.list_relationships, relations, authority=reader),
        ):
            with pytest.raises(T03Error, match="not_found"):
                operation()
        assert (
            current.relationships.list_relationships(control_relations, authority=reader).to_wire()[
                "entries"
            ]
            == []
        )
        current.history.read_history(read, authority=owner)
        assert current.history.list_history(history, authority=owner).to_wire()["entries"]
        current.portability.rebuild_index()
        assert current.retrieval.search_page(search, authority=reader).to_wire()["results"] == []
