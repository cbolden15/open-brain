"""V2 managed-copy damage cannot influence search or source expansion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from open_brain_engine.engine import DecisionOutcome, ProposalDraft
from open_brain_engine.engine.t03_contracts import RecordReadRequest, SearchPageRequest, T03Error

from packages.app.tests.integration.engine.test_slack_sharing import _revoke, _single


@pytest.mark.parametrize(
    "damage", ["unknown-marker", "removed-marker", "delivery", "provenance", "bytes", "link"]
)
def test_damaged_v2_copy_withheld_before_ranking_paging_and_history(
    tmp_path: Path, damage: str
) -> None:
    journey = _single(tmp_path)
    approvals = [journey.approve(journey.sources()[0])[3] for _ in range(3)]
    damaged = approvals[0].copy_capture_id
    controls = {approval.copy_capture_id for approval in approvals[1:]}
    assert damaged is not None
    engine = journey.tasks.sharing._engine
    with engine._store.transaction() as connection:
        if damage == "link":
            trigger = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='sharing_links_delete_immutable'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER sharing_links_delete_immutable")
            connection.execute("DELETE FROM sharing_links WHERE copy_capture_id=?", (damaged,))
            connection.execute(trigger)
        elif damage == "bytes":
            connection.execute(
                "UPDATE source_revisions SET source_bytes=? WHERE capture_id=?", (b"{}", damaged)
            )
        else:
            key, value = {
                "unknown-marker": (
                    "source_reference",
                    "urn:open-brain:sharing-copy:v999:synthetic",
                ),
                "removed-marker": ("source_reference", "urn:synthetic:ordinary"),
                "delivery": ("delivery_id", "synthetic-unrelated-delivery"),
                "provenance": ("provenance_json", "{}"),
            }[damage]
            connection.execute(f"UPDATE captures SET {key}=? WHERE capture_id=?", (value, damaged))  # noqa: S608
    authority = journey.mcp().authority
    reading = RecordReadRequest(record_id=damaged, expected_revision_id=damaged)
    for method in (journey.tasks.retrieval.read_record, journey.tasks.history.read_history):
        with pytest.raises(T03Error, match="not_found"):
            method(reading, authority=authority)
    search = SearchPageRequest(query="onboarding", limit=1)
    first = journey.tasks.retrieval.search_page(search, authority=authority).to_wire()
    assert first["next_cursor"] is not None
    assert first["results"][0]["record_id"] in controls
    from dataclasses import replace

    continuation = replace(search, cursor=first["next_cursor"])
    expected = journey.tasks.retrieval.search_page(continuation, authority=authority).to_wire()
    assert expected["next_cursor"] is None
    assert {first["results"][0]["record_id"], expected["results"][0]["record_id"]} == controls
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE search_documents SET title='onboarding hidden changed' WHERE result_id=?",
            (damaged,),
        )
    assert (
        journey.tasks.retrieval.search_page(continuation, authority=authority).to_wire() == expected
    )


def test_v2_canonical_source_expansion_requires_every_current_approval(tmp_path: Path) -> None:
    journey = _single(tmp_path)
    approvals = [journey.approve(journey.sources()[0])[3] for _ in range(2)]
    copies = tuple(approval.copy_capture_id for approval in approvals)
    proposal = journey.tasks.review.propose(
        copies,
        (ProposalDraft("Onboarding rationale", "Onboarding retained rationale"),),
        delivery_id="canonical.slack.propose",
    )[0]
    decided = journey.tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="canonical.slack.decide",
        expected_review_digest=proposal.review_digest,
    )
    authority = journey.mcp().authority
    page = journey.tasks.retrieval.search_page(
        SearchPageRequest(query="onboarding"), authority=authority
    ).to_wire()
    canonical = next(row for row in page["results"] if row["record_id"] == decided.page_id)
    reading = RecordReadRequest(
        record_id=decided.page_id, expected_revision_id=canonical["revision_id"]
    )
    response = journey.tasks.retrieval.read_record(reading, authority=authority).to_wire()
    assert set(response["record"]["provenance"]["capture_ids"]) == set(copies)
    assert "retained rationale" in response["content"]["text"]
    _revoke(journey, approvals[1])
    assert decided.page_id not in {
        row["record_id"]
        for row in journey.tasks.retrieval.search_page(
            SearchPageRequest(query="onboarding"), authority=authority
        ).to_wire()["results"]
    }
    for method in (journey.tasks.retrieval.read_record, journey.tasks.history.read_history):
        with pytest.raises(T03Error, match="not_found"):
            method(reading, authority=authority)


def test_slack_copy_policy_marker_does_not_authorize_an_ordinary_capture(tmp_path: Path) -> None:
    from dataclasses import replace

    from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
    from open_brain_engine.engine import TextPayload

    from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission

    journey = _single(tmp_path)
    ordinary = journey.tasks.capture.submit(
        replace(
            _public_submission(journey.tasks),
            payload=TextPayload("Onboarding unauthorized egress flag"),
            delivery_id="ordinary.work.egress",
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.WORK,
                reason=PrivacyReason.POLICY_WORK,
                policy_version="sharing-slack-work-copy.v1",
                authority=Authority(cloud=False, external_egress=True),
            ),
        )
    )
    journey.tasks.inbox.route(ordinary.capture_id, journey.space, delivery_id="ordinary.work.route")
    assert (
        journey.mcp().call_tool("brain_search_page", {"dto_version": 1, "query": "unauthorized"})[
            "results"
        ]
        == []
    )
    from open_brain.services.mcp_protocol import McpCallError

    with pytest.raises(McpCallError, match="not_found"):
        journey.read(ordinary.capture_id)
    assert json.dumps(ordinary.capture_id)
