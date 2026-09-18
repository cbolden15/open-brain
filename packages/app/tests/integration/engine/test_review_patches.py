from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    DecisionOutcome,
    EngineTaskSet,
    PatchDraft,
    PatchOperation,
    ProposalDraft,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.engine.materializer import materialize_portable_root
from open_brain_engine.portable import validate_portable_root
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from open_brain.services.review_publication import ReviewPublicationError, ReviewPublicationService


def _tasks(root: Path) -> EngineTaskSet:
    return open_local_engine(compile_single_user_local(root, starter_spaces=("Projects",)))


def _approved_page(root: Path) -> tuple[EngineTaskSet, str, str, Path]:
    tasks = _tasks(root)
    space_id = tasks.spaces.spaces()[0].space_id
    capture_id = tasks.capture.accept(
        TextPayload("seed evidence"), delivery_id="patch.seed", space_id=space_id
    ).capture_id
    proposal = tasks.review.propose(
        (capture_id,),
        (ProposalDraft("Recurring note", "Original body\n"),),
        delivery_id="patch.seed",
    )[0]
    shown = tasks.review.show(proposal.proposal_id)
    tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="patch.seed.approve",
        expected_review_digest=shown.review_digest,
    )
    assert proposal.page_id is not None
    path = next((root / "content").rglob(f"{proposal.page_id}.md"))
    return tasks, space_id, proposal.page_id, path


def _source(tasks: EngineTaskSet, space_id: str, key: str) -> str:
    return tasks.capture.accept(
        TextPayload(f"evidence {key}"), delivery_id=f"patch.source.{key}", space_id=space_id
    ).capture_id


def _append_patch(page_id: str, path: Path, text: str) -> PatchDraft:
    content = path.read_bytes()
    # Page-body offsets are measured against the UTF-8 body, not frontmatter.
    body = "Original body\n"
    return PatchDraft(
        target_page_id=page_id,
        expected_page_sha256=sha256(content).hexdigest(),
        operations=(PatchOperation(len(body.encode()), len(body.encode()), text),),
    )


def test_patch_is_bound_shown_as_diff_and_applied_with_idempotent_retries(tmp_path: Path) -> None:
    tasks, space_id, page_id, path = _approved_page(tmp_path / "brain")
    source_id = _source(tasks, space_id, "one")
    draft = _append_patch(page_id, path, "Slack update\n")

    first = tasks.review.propose((source_id,), (draft,), delivery_id="patch.propose")
    retried = tasks.review.propose((source_id,), (draft,), delivery_id="patch.propose")
    assert first[0].proposal_id == retried[0].proposal_id
    assert first[0].draft_type == "patch"

    shown = tasks.review.show(first[0].proposal_id)
    assert shown.patch == draft
    assert "+Slack update" in (shown.patch_diff or "")
    approved = tasks.review.decide(
        first[0].proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="patch.approve",
        expected_review_digest=shown.review_digest,
    )
    duplicate = tasks.review.decide(
        first[0].proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="patch.approve",
        expected_review_digest=shown.review_digest,
    )
    assert approved.publication_id == duplicate.publication_id and duplicate.duplicate is True
    assert "Original body\nSlack update\n" in path.read_text()


def test_patch_rejects_overlaps_and_stale_target_without_writing(tmp_path: Path) -> None:
    tasks, space_id, page_id, path = _approved_page(tmp_path / "brain")
    with pytest.raises(ValueError, match="overlapping"):
        PatchDraft(
            page_id,
            sha256(path.read_bytes()).hexdigest(),
            (PatchOperation(0, 2, "a"), PatchOperation(1, 3, "b")),
        )

    source_id = _source(tasks, space_id, "stale")
    stale = _append_patch(page_id, path, "stale update\n")
    proposal = tasks.review.propose((source_id,), (stale,), delivery_id="patch.stale")[0]
    path.write_bytes(path.read_bytes() + b"owner edit\n")
    with pytest.raises(ValueError, match="canonical page revision conflict"):
        tasks.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="patch.stale.approve",
            expected_review_digest=tasks.review.show(proposal.proposal_id).review_digest,
        )
    assert b"stale update" not in path.read_bytes()


def test_patch_edit_uses_explicit_replacement_body_and_export_retains_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks, space_id, page_id, path = _approved_page(root)
    source_id = _source(tasks, space_id, "edited")
    service = ReviewPublicationService(tasks.review)
    patch = _append_patch(page_id, path, "draft update\n")
    proposed = service.propose(
        {"capture_ids": [source_id], "patch": patch.to_dict(), "idempotency_key": "patch.service"}
    )
    shown = service.show({"proposal_id": proposed["proposal_id"]})
    with pytest.raises(ReviewPublicationError, match="invalid_arguments"):
        service.edit_and_approve(
            {
                "proposal_id": proposed["proposal_id"],
                "review_token": cast(str, shown["review_token"]),
                "markdown": "bad",
            }
        )
    service.edit_and_approve(
        {
            "proposal_id": proposed["proposal_id"],
            "review_token": cast(str, shown["review_token"]),
            "replacement_body": "Owner-edited replacement\n",
            "idempotency_key": "patch.edit",
        }
    )
    assert "Owner-edited replacement" in path.read_text()
    exported = tmp_path / "exported"
    tasks.portability.export(exported, export_id="export_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd4")
    assert cast(int, validate_portable_root(exported)["schema_version"]) >= 3
    assert any((exported / "history/review-bindings").rglob("*.json"))
    snapshot = validated_portable_snapshot(exported)
    materialized = materialize_portable_root(
        exported, snapshot=snapshot, expected_root_identity=snapshot.root_identity
    )
    assert materialized.profile.root == exported
