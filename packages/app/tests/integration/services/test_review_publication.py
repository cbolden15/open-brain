from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from open_brain_engine.engine import (
    DecisionOutcome,
    ProposalRecord,
    ReviewEvidence,
    ReviewProposal,
    ReviewTask,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.engine.contracts import DecisionRecord

from open_brain.profile import compile_single_user_local
from open_brain.services.review_publication import (
    MAX_REVIEW_MARKDOWN_BYTES,
    ReviewPublicationError,
    ReviewPublicationService,
    validate_review_arguments,
)

CAPTURE_A = "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
CAPTURE_B = "capture_a877b476-b57b-4b77-840d-7cd38c3a12da"
SPACE = "space_568c80bd-a243-43be-a419-8646f66be312"
PROPOSAL = "proposal_8d87546c-3008-42ee-8632-0f2401904b35"
PAGE = "page_42c7f07e-d26e-4e1f-8184-b1d60f40ce68"
DIGEST = "d" * 64


def _record(*, status: str = "pending") -> ProposalRecord:
    return ProposalRecord(
        proposal_id=PROPOSAL,
        capture_id=CAPTURE_A,
        proposed_kind="page_update",
        status=status,
        space_id=SPACE,
        sibling_proposal_ids=(PROPOSAL,),
        terminal_decision_id=None,
        title="Combined note",
        capture_ids=(CAPTURE_A, CAPTURE_B),
        selected_capture_ids=(CAPTURE_A, CAPTURE_B),
        page_id=PAGE,
        target_page_id=None,
        operation="create",
        review_digest=DIGEST,
    )


def _detail() -> ReviewProposal:
    return ReviewProposal(
        proposal_id=PROPOSAL,
        status="pending",
        title="Combined note",
        markdown="Projected body with [private-path].\n",
        space_id=SPACE,
        page_id=PAGE,
        target_page_id=None,
        operation="create",
        capture_ids=(CAPTURE_A, CAPTURE_B),
        selected_capture_ids=(CAPTURE_A, CAPTURE_B),
        evidence=(
            ReviewEvidence(CAPTURE_A, "Evidence A", "a" * 64, False),
            ReviewEvidence(CAPTURE_B, "[protected]", "b" * 64, True),
        ),
        review_digest=DIGEST,
        expected_page_sha256=None,
        expected_publication_id=None,
        projection_applied=True,
    )


def _service() -> tuple[ReviewPublicationService, Mock]:
    task = Mock(spec=ReviewTask)
    return ReviewPublicationService(cast(ReviewTask, task)), task


def test_propose_uses_bound_source_tuple_and_returns_effective_retry_key() -> None:
    service, task = _service()
    task.propose.return_value = (_record(),)

    result = service.propose(
        {
            "capture_ids": [CAPTURE_A, CAPTURE_B],
            "title": "Combined note",
            "markdown": "# Body\n",
            "idempotency_key": "proposal-retry",
        }
    )

    assert result["status"] == "proposed"
    assert result["effective_idempotency_key"] == "proposal-retry"
    assert "review_token" not in result
    capture_ids, drafts = task.propose.call_args.args
    assert capture_ids == (CAPTURE_A, CAPTURE_B)
    assert drafts[0].title == "Combined note" and drafts[0].markdown == "# Body\n"


def test_proposal_receipt_fits_four_kib_at_unicode_and_source_limits() -> None:
    service, task = _service()
    sources = tuple(f"capture_00000000-0000-4000-8000-{index:012d}" for index in range(32))
    task.propose.return_value = (
        replace(_record(), title="🌿" * 200, capture_ids=sources, selected_capture_ids=sources),
    )
    result = service.propose(
        {
            "capture_ids": list(sources),
            "title": "🌿" * 200,
            "markdown": "Draft",
            "idempotency_key": "🌿" * 128,
        }
    )
    assert len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()) <= 4096
    assert result["proposal_id"] == PROPOSAL
    assert result["effective_idempotency_key"] == "🌿" * 128


def test_show_exposes_full_projected_detail_and_maps_digest_to_review_token() -> None:
    service, task = _service()
    task.show.return_value = _detail()

    result = service.show({"proposal_id": PROPOSAL})

    assert result["status"] == "shown" and result["proposal_status"] == "pending"
    assert result["markdown"] == "Projected body with [private-path].\n"
    assert result["review_token"] == DIGEST
    assert result["projection_applied"] is True
    evidence = cast(list[dict[str, object]], result["evidence"])
    assert evidence[1]["projection_applied"] is True
    assert "review_digest" not in result


def test_decisions_bind_review_token_and_generated_retry_key() -> None:
    service, task = _service()
    task.decide.return_value = DecisionRecord(
        decision_id="decision_20770d1b-e42a-4e77-b3de-bf127b36cfe6",
        proposal_id=PROPOSAL,
        outcome=DecisionOutcome.EDITED,
        page_id=PAGE,
        publication_id="publication_a640a04f-801b-42e4-b09f-e7f57bb7ac22",
    )

    result = service.edit_and_approve(
        {"proposal_id": PROPOSAL, "review_token": DIGEST, "markdown": "Edited\n"}
    )

    assert result["status"] == "edited"
    assert str(result["effective_idempotency_key"]).startswith("review-")
    assert task.decide.call_args.kwargs["expected_review_digest"] == DIGEST
    assert task.decide.call_args.kwargs["edited_markdown"] == "Edited\n"


def test_list_is_bounded_and_has_a_continuation_cursor() -> None:
    service, task = _service()
    task.list.side_effect = ((_record(),), (_record(status="approved"),))

    result = service.list({"space_id": SPACE, "limit": 1, "offset": 4})

    proposals = cast(list[dict[str, object]], result["proposals"])
    assert len(proposals) == 1
    assert result["next_offset"] == 5
    assert task.list.call_args_list[0].kwargs == {
        "capture_id": None,
        "status": None,
        "space_id": SPACE,
        "limit": 1,
        "offset": 4,
    }
    assert task.list.call_args_list[1].kwargs["limit"] == 1
    assert task.list.call_args_list[1].kwargs["offset"] == 5


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("propose", {"capture_ids": [], "title": "Title", "markdown": "Body"}),
        (
            "propose",
            {"capture_ids": [CAPTURE_A, CAPTURE_A], "title": "Title", "markdown": "Body"},
        ),
        (
            "propose",
            {
                "capture_ids": [CAPTURE_A],
                "title": "Title",
                "markdown": "x" * (MAX_REVIEW_MARKDOWN_BYTES + 1),
            },
        ),
        ("show", {"proposal_id": "proposal_invalid"}),
        (
            "propose",
            {
                "capture_ids": [CAPTURE_A],
                "title": "Title",
                "markdown": "Body",
                "target_page_id": "target_page_42c7f07e-d26e-4e1f-8184-b1d60f40ce68",
            },
        ),
        ("approve", {"proposal_id": PROPOSAL, "review_token": "short"}),
        ("reject", {"proposal_id": PROPOSAL, "review_token": DIGEST, "extra": True}),
    ],
)
def test_validator_rejects_invalid_or_oversized_requests(
    operation: str, arguments: dict[str, object]
) -> None:
    with pytest.raises(ReviewPublicationError, match="invalid_arguments"):
        validate_review_arguments(operation, arguments)


def test_shared_service_runs_multi_source_create_inspect_and_decide(tmp_path: Path) -> None:
    tasks = open_local_engine(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Projects",))
    )
    space_id = tasks.spaces.spaces()[0].space_id
    source_ids = [
        tasks.capture.accept(
            TextPayload(text), delivery_id=f"review.service.source.{index}", space_id=space_id
        ).capture_id
        for index, text in enumerate(("First evidence", "Second evidence"))
    ]
    service = ReviewPublicationService(tasks.review)
    proposed = service.propose(
        {
            "capture_ids": source_ids,
            "title": "Combined",
            "markdown": "Combined body\n",
            "idempotency_key": "service-proposal",
        }
    )
    shown = service.show({"proposal_id": proposed["proposal_id"]})
    assert shown["capture_ids"] == tuple(source_ids)
    assert shown["selected_capture_ids"] == tuple(source_ids)
    assert shown["markdown"] == "Combined body"
    approved = service.approve(
        {
            "proposal_id": proposed["proposal_id"],
            "review_token": shown["review_token"],
            "idempotency_key": "service-approve",
        }
    )
    assert approved["status"] == "approved"
    assert approved["page_id"] == proposed["page_id"]
    listed = service.list({"space_id": space_id, "status": "approved"})
    proposals = cast(list[dict[str, object]], listed["proposals"])
    assert [row["proposal_id"] for row in proposals] == [proposed["proposal_id"]]


def test_target_page_uses_canonical_page_identity() -> None:
    validate_review_arguments(
        "propose",
        {
            "capture_ids": [CAPTURE_A],
            "title": "Updated",
            "markdown": "Updated body",
            "target_page_id": PAGE,
        },
    )


def test_terminal_conflict_does_not_reserve_a_fresh_decision_retry_key(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Projects",))
    )
    space_id = tasks.spaces.spaces()[0].space_id
    first, second = (
        tasks.capture.accept(
            TextPayload(f"Source {index}"),
            delivery_id=f"review.service.terminal.source.{index}",
            space_id=space_id,
        ).capture_id
        for index in range(2)
    )
    service = ReviewPublicationService(tasks.review)
    first_proposal = service.propose(
        {
            "capture_ids": [first],
            "title": "First",
            "markdown": "First body",
            "idempotency_key": "terminal-first-proposal",
        }
    )
    first_shown = service.show({"proposal_id": first_proposal["proposal_id"]})
    service.approve(
        {
            "proposal_id": first_proposal["proposal_id"],
            "review_token": first_shown["review_token"],
            "idempotency_key": "terminal-first-decision",
        }
    )

    with pytest.raises(ReviewPublicationError, match="terminal_decision"):
        service.approve(
            {
                "proposal_id": first_proposal["proposal_id"],
                "review_token": first_shown["review_token"],
                "idempotency_key": "fresh-decision-key",
            }
        )

    second_proposal = service.propose(
        {
            "capture_ids": [second],
            "title": "Second",
            "markdown": "Second body",
            "idempotency_key": "terminal-second-proposal",
        }
    )
    second_shown = service.show({"proposal_id": second_proposal["proposal_id"]})
    decided = service.approve(
        {
            "proposal_id": second_proposal["proposal_id"],
            "review_token": second_shown["review_token"],
            "idempotency_key": "fresh-decision-key",
        }
    )

    assert decided["proposal_id"] == second_proposal["proposal_id"]
