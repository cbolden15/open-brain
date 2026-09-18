from __future__ import annotations

from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, DecisionOutcome, ProposalDraft, TextPayload
from open_brain_engine.engine import spaces as spaces_module
from open_brain_engine.engine.normalization import _new_id

from open_brain.profile import compile_single_user_local


@pytest.mark.parametrize("stale", (False, True))
def test_imported_review_binds_causal_route_head_not_import_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "source", starter_spaces=("Notes",))
    )
    space = engine.inbox.spaces()[0]
    capture = engine.capture.accept(TextPayload("Synthetic route evidence"), delivery_id="source")
    routes = iter(
        (
            "route_ffffffff-ffff-4fff-8fff-ffffffffffff",
            "route_00000000-0000-4000-8000-000000000001",
        )
    )
    original = _new_id
    monkeypatch.setattr(
        spaces_module, "_new_id", lambda kind: next(routes) if kind == "route" else original(kind)
    )
    engine.inbox.route(capture.capture_id, space.space_id, delivery_id="route.first")
    if not stale:
        engine.inbox.route(capture.capture_id, space.space_id, delivery_id="route.second")
    proposal = engine.review.propose(
        (capture.capture_id,),
        (ProposalDraft("Route bound", "Synthetic route head"),),
        delivery_id="proposal",
    )[0]
    if stale:
        engine.inbox.route(capture.capture_id, space.space_id, delivery_id="route.second")
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_00000000-0000-4000-8000-000000000001")
    target = tmp_path / "restored"
    engine.portability.import_clean(
        archive, target, import_id="import_00000000-0000-4000-8000-000000000001"
    )
    restored = BrainEngine.open(compile_single_user_local(target))
    if stale:
        with pytest.raises(ValueError, match="review source state conflict"):
            restored.review.decide(
                proposal.proposal_id,
                DecisionOutcome.APPROVED,
                delivery_id="decision",
                expected_review_digest=proposal.review_digest,
            )
        assert not tuple((target / "history/publications").rglob("*.json"))
    else:
        decision = restored.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="decision",
            expected_review_digest=proposal.review_digest,
        )
        assert decision.page_id == proposal.page_id
        assert restored.retrieval.fetch(str(proposal.page_id)) is not None


@pytest.fixture(autouse=True)
def historical_schema_six_recipes(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    from packages.app.tests.integration.engine._local_schema_fixtures import use_schema_six_runtime

    if request.node.name.startswith(
        ("test_imported_review_binds_causal_route_head_not_import_order",)
    ):
        use_schema_six_runtime(monkeypatch, globals())
