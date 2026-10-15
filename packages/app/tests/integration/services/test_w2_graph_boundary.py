"""A whole-workspace structural cache is refused before implicit MCP reads it."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from open_brain_engine.engine import CaptureAction, DecisionOutcome, ProposalDraft, TextPayload
from open_brain_engine.engine.t03_contracts import SessionOrigin

from open_brain.services.graph_projection_store import PROJECTION_PATH
from open_brain.services.local_operations import graph_projection
from packages.app.tests.integration.engine.test_slack_sharing import _single


def test_implicit_graph_refuses_global_cache_but_preserves_authorized_engine_view(
    tmp_path: Path,
) -> None:
    journey = _single(tmp_path)
    copy = journey.approve(journey.sources()[0])[3].copy_capture_id
    proposal = journey.tasks.review.propose(
        (copy,), (ProposalDraft("Protected graph", "Protected rationale"),),
        delivery_id="w2.app.graph.propose",
    )[0]
    decision = journey.tasks.review.decide(
        proposal.proposal_id, DecisionOutcome.APPROVED,
        delivery_id="w2.app.graph.decide", expected_review_digest=proposal.review_digest,
    )
    ordinary = journey.tasks.capture.accept(
        TextPayload("Independent graph note"), action=CaptureAction.CANONICAL_NOTE,
        space_id=journey.space, delivery_id="w2.app.graph.control",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    journey.tasks.managed_workspace.setup(str(workspace), operation_id="w2.app.graph.setup")
    authority = replace(journey.owner, session_origin=SessionOrigin.MCP_IMPLICIT)
    bound = replace(
        journey.tasks,
        managed_workspace=journey.tasks.managed_workspace.with_authority(authority),
        managed_inference=journey.tasks.managed_inference.with_authority(authority),
    )
    before = graph_projection(bound, authority=authority)
    assert before["status"] == "missing"
    assert bound.managed_workspace.status().active_notes == 1
    assert len(bound.managed_workspace.graph_snapshot(before["workspace_id"]).sources) == 1
    cache = journey.tasks.profile.root / PROJECTION_PATH
    # Invalid cache proves this path refuses access before parsing or projection.
    cache.write_text(json.dumps({"protected": decision.page_id, "capture": copy}))
    assert graph_projection(bound, authority=authority) == before
    assert decision.page_id not in json.dumps(before)
    assert copy not in json.dumps(before)
    assert before["structural_links"] == []
    assert before["inferred_suggestions"] == []
    assert graph_projection(journey.tasks)["status"] == "unavailable"
    assert ordinary.capture_id
