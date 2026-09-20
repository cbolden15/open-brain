from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    CaptureAction,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.profile import compile_single_user_local


def test_reconciliation_updates_retrieval_and_space_name_without_rewriting_owner_markdown(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    space = tasks.inbox.create_space("Studio", delivery_id="reconcile.space")
    capture = tasks.capture.accept(
        TextPayload("Original canonical body\n"),
        delivery_id="reconcile.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    page = next((root / "content" / "spaces").rglob("page_*.md"))
    original_page = page.read_bytes()
    parsed_page = parse_markdown(original_page)
    page.write_bytes(
        render_markdown(
            fields={
                **parsed_page.fields,
                "modified_at": "2026-09-01T12:30:00Z",
                "title": "Edited title",
            },
            body="Edited owner Markdown body\n",
        ).encode("utf-8")
    )
    space_file = next((root / "content" / "spaces").rglob("_space.md"))
    original_space = space_file.read_bytes()
    parsed_space = parse_markdown(original_space)
    space_file.write_bytes(
        render_markdown(fields={**parsed_space.fields, "name": "Renamed Studio"}, body="").encode(
            "utf-8"
        )
    )
    expected_page = page.read_bytes()
    expected_space = space_file.read_bytes()
    receipt = tasks.reconciliation.reconcile()

    refreshed = tasks.retrieval.search("Edited owner Markdown")[0]
    renamed = tasks.inbox.spaces()[0]

    assert receipt.status == "reconciled"
    assert receipt.page_updates == 1
    assert receipt.space_updates == 1
    assert refreshed.result_id != capture.capture_id
    assert refreshed.title == "Edited title"
    assert renamed.space_id == space.space_id
    assert renamed.slug == space.slug
    assert renamed.name == "Renamed Studio"
    assert page.read_bytes() == expected_page
    assert space_file.read_bytes() == expected_space


def test_reconciliation_repairs_stored_trust_but_rejects_edited_trust(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root, starter_spaces=("Notes",)))
    space = tasks.inbox.spaces()[0]
    capture = tasks.capture.accept(
        TextPayload("Durable trust canary"),
        delivery_id="reconcile.trust.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    page = next((root / "content/spaces").rglob("page_*.md"))
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.execute(
            "UPDATE search_documents SET trust = 'unverified' "
            "WHERE capture_id = ? AND record_type = 'canonical'",
            (capture.capture_id,),
        )

    repaired = tasks.reconciliation.reconcile()
    result = tasks.retrieval.search("Durable trust canary", record_type="canonical")[0]

    assert repaired.page_updates == 1
    assert result.trust == "owner"

    parsed = parse_markdown(page.read_bytes())
    page.write_bytes(
        render_markdown(
            fields={**parsed.fields, "trust": "unverified"},
            body=parsed.body,
        ).encode("utf-8")
    )

    with pytest.raises(ValueError, match="canonical"):
        tasks.reconciliation.reconcile()
    assert tasks.retrieval.search("Durable trust canary", record_type="canonical")[0].trust == (
        "owner"
    )


def test_reconciliation_rebuilds_repaired_search_drift_from_the_active_repair(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
    from open_brain_engine.engine import DecisionOutcome, ProposalDraft
    from open_brain_engine.engine.local import BrainEngine
    from open_brain_engine.engine.local_schema import open_local_database
    from open_brain_engine.engine.privacy_migration import _verify_privacy_projections
    from open_brain_engine.engine.privacy_repairs import PrivacyRepairRequest
    from open_brain_engine.engine.reconciliation import rederive_live_search_projection
    from open_brain_engine.engine.source_store import (
        register_completed_captures,
        register_publication_members,
    )
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

    def privacy(tier: PrivacyTier, *, cloud: bool, external_egress: bool) -> PrivacyDecision:
        reason = (
            PrivacyReason.POLICY_WORK if tier is PrivacyTier.WORK else PrivacyReason.POLICY_PUBLIC
        )
        return PrivacyDecision.create(
            tier=tier,
            reason=reason,
            policy_version="privacy-v1",
            authority=Authority(cloud=cloud, external_egress=external_egress),
        )

    root = tmp_path / "brain"
    profile = compile_single_user_local(root, starter_spaces=("Notes",))
    engine = BrainEngine.open(profile, clock=lambda: datetime.now(UTC))
    space_id = engine.inbox.spaces()[0].space_id
    valid = engine.capture.accept(
        TextPayload("valid reconciliation evidence"),
        delivery_id="repair.valid",
        space_id=space_id,
    )
    broken = engine.capture.accept(
        TextPayload("evidence reconciliation will repair"),
        delivery_id="repair.broken",
        space_id=space_id,
    )
    proposal = engine.review.propose(
        (valid.capture_id, broken.capture_id),
        (ProposalDraft("Drift page", "Drift body"),),
        delivery_id="repair.proposal",
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="repair.decision",
        expected_review_digest=proposal.review_digest,
    )
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE captures SET privacy_json=NULL WHERE capture_id=?",
            (broken.capture_id,),
        )
        connection.execute("DELETE FROM source_revision_privacy")
        connection.execute("DELETE FROM canonical_revision_privacy")
        connection.execute("COMMIT")
    finally:
        connection.close()
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        register_completed_captures(connection, profile)
        register_publication_members(connection, profile)
        connection.execute("COMMIT")
        broken_digest = connection.execute(
            "SELECT invalid_evidence_sha256 FROM privacy_invalid_evidence "
            "WHERE target_kind='source_revision' AND target_id=?",
            (broken.capture_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    rederive_live_search_projection(engine)
    authority = EffectiveAuthority(
        profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    repair_task = engine.tasks.privacy_repair
    assert repair_task is not None
    first = repair_task.repair_privacy(
        PrivacyRepairRequest(
            target_kind="source_revision",
            target_id=broken.capture_id,
            invalid_evidence_sha256=broken_digest,
            replacement=privacy(PrivacyTier.WORK, cloud=True, external_egress=True),
            operation_id="privacy-repair.drift.first",
        ),
        authority=authority,
    )
    second = repair_task.repair_privacy(
        PrivacyRepairRequest(
            target_kind="source_revision",
            target_id=broken.capture_id,
            invalid_evidence_sha256=broken_digest,
            replacement=privacy(PrivacyTier.PUBLIC, cloud=True, external_egress=True),
            operation_id="privacy-repair.drift.head",
            supersedes_repair_id=first.repair_id,
        ),
        authority=authority,
    )
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Shape-legal drift: the superseded repair is a real ledger row bound to
        # this capture and digest, so the row passes every schema-nine trigger
        # while disagreeing with the active chain's resolution.
        connection.execute(
            "UPDATE search_documents SET effective_tier='work', effective_cloud=0, "
            "effective_external_egress=0, applied_repair_id=?, applied_repair_sequence=? "
            "WHERE result_id=?",
            (first.repair_id, first.repair_sequence, broken.capture_id),
        )
        durable_before = tuple(connection.execute(
            "SELECT repair_id, repair_sequence, replacement_privacy_json, receipt_json "
            "FROM privacy_repair_ledger ORDER BY repair_sequence"
        ))
        markers_before = tuple(connection.execute(
            "SELECT target_kind, target_id, invalid_reason, invalid_evidence_sha256 "
            "FROM privacy_invalid_evidence ORDER BY target_kind, target_id"
        ))
        connection.execute("COMMIT")
    finally:
        connection.close()

    # Detection: the migration verifier rejects the drifted derived state.
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(T03Error, match="operation_pending"):
            _verify_privacy_projections(connection)
        connection.execute("ROLLBACK")
    finally:
        connection.close()

    rederive_live_search_projection(engine)

    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        assert tuple(connection.execute(
            "SELECT effective_tier, effective_cloud, effective_external_egress, "
            "invalid_evidence_reason, invalid_evidence_sha256, applied_repair_id, "
            "applied_repair_sequence FROM search_documents WHERE result_id=?",
            (broken.capture_id,),
        ).fetchone()) == (
            "public", 1, 1, "missing", broken_digest, second.repair_id, 2,
        )
        # Only derived search state changed: the ledger and append-only markers
        # are byte-identical to the pre-drift snapshot.
        assert tuple(connection.execute(
            "SELECT repair_id, repair_sequence, replacement_privacy_json, receipt_json "
            "FROM privacy_repair_ledger ORDER BY repair_sequence"
        )) == durable_before
        assert tuple(connection.execute(
            "SELECT target_kind, target_id, invalid_reason, invalid_evidence_sha256 "
            "FROM privacy_invalid_evidence ORDER BY target_kind, target_id"
        )) == markers_before
        _verify_privacy_projections(connection)
    finally:
        connection.close()
