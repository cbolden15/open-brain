"""Owner-repair engine core: ledger state machine, replay, and projection refresh."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.access_contracts import validate_stored_privacy_decision
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import DecisionOutcome, ProposalDraft, TextPayload, privacy_repairs
from open_brain_engine.engine.contracts import LocalEngineContext
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import (
    open_local_database,
    open_local_database_read_only,
)
from open_brain_engine.engine.privacy_projection import project_retained_privacy_evidence
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.providers.base import ProviderMode

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
OWNER_ACTOR_ID = "actor_00000000-0000-4000-8000-000000000101"

ROLE_CLAIM: dict[str, Any] = {
    "actor_id": OWNER_ACTOR_ID,
    "capabilities": ["owner"],
    "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000102",
    "role_id": "role_00000000-0000-4000-8000-000000000103",
    "tenant_id": TENANT_ID,
}

SEARCH_COLUMNS = (
    "effective_tier, effective_cloud, effective_external_egress, "
    "invalid_evidence_reason, invalid_evidence_sha256, "
    "applied_repair_id, applied_repair_sequence"
)


def _privacy(
    tier: PrivacyTier,
    *,
    cloud: bool = False,
    external_egress: bool = False,
    confirmation_ref: str | None = None,
    reason: PrivacyReason | None = None,
) -> PrivacyDecision:
    reasons = {
        PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
        PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
        PrivacyTier.SECRET: PrivacyReason.SECRET_DETECTED,
    }
    personal_reason = (
        PrivacyReason.PERSONAL_CONFIRMED
        if confirmation_ref is not None
        else PrivacyReason.PERSONAL_LOCAL_ONLY
    )
    return PrivacyDecision.create(
        tier=tier,
        reason=reason or reasons.get(tier, personal_reason),
        policy_version="privacy-v1",
        authority=Authority(cloud=cloud, external_egress=external_egress),
        confirmation_ref=confirmation_ref,
    )


def _owner_authority(*, principal: str = OWNER_ACTOR_ID, owner: bool = True) -> EffectiveAuthority:
    return EffectiveAuthority(principal, "session", frozenset(), None, owner=owner)


def _fixed_clock(*, offset_seconds: int = 0) -> Callable[[], datetime]:
    base = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    return lambda: base + timedelta(seconds=offset_seconds)


def _read_only(profile: LocalEngineContext) -> sqlite3.Connection:
    return open_local_database_read_only(profile)


def _repair_request(
    target_kind: str,
    target_id: str,
    invalid_evidence_sha256: str,
    *,
    operation_id: str,
    replacement: PrivacyDecision | Mapping[str, object] | None = None,
    supersedes_repair_id: str | None = None,
) -> privacy_repairs.PrivacyRepairRequest:
    return privacy_repairs.PrivacyRepairRequest(
        target_kind=target_kind,
        target_id=target_id,
        invalid_evidence_sha256=invalid_evidence_sha256,
        replacement=replacement or _privacy(PrivacyTier.WORK, cloud=True, external_egress=True),
        operation_id=operation_id,
        supersedes_repair_id=supersedes_repair_id,
    )


class _RepairBrain:
    """A schema-nine Brain with one invalid member and one valid member."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        corrupt_values: dict[str, str | None] | None = None,
        corrupt_valid: bool = False,
        clock_offset: int = 0,
    ) -> None:
        self.root = tmp_path / "brain"
        self.root.mkdir(mode=0o700, parents=True)
        # The engine expects the app bootstrap's private state layout to exist.
        (self.root / ".open-brain").mkdir(mode=0o700)
        self.profile = LocalEngineContext(
            root=self.root,
            root_identity=_root_identity(self.root),
            tenant_id=TENANT_ID,
            owner_actor_id=OWNER_ACTOR_ID,
            owner_role_claim=ROLE_CLAIM,
            provider_mode=ProviderMode.NONE,
            starter_spaces=("Notes",),
        )
        engine = BrainEngine.open(self.profile, clock=_fixed_clock(offset_seconds=clock_offset))
        space_id = engine.inbox.spaces()[0].space_id
        self.valid = engine.capture.accept(
            TextPayload("valid personal evidence"),
            delivery_id="repair.valid",
            space_id=space_id,
        )
        self.broken = engine.capture.accept(
            TextPayload("retained evidence that will go missing"),
            delivery_id="repair.broken",
            space_id=space_id,
        )
        proposal = engine.review.propose(
            (self.valid.capture_id, self.broken.capture_id),
            (ProposalDraft("Repair page", "Repair body"),),
            delivery_id="repair.proposal",
        )[0]
        decision = engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="repair.decision",
            expected_review_digest=proposal.review_digest,
        )
        assert decision.page_id is not None
        self.page_id: str = decision.page_id
        # Corrupt retained evidence after the fact, then force the revision
        # projections to re-register from the corrupted retained values.
        corrupt_values = dict(corrupt_values or {self.broken.capture_id: None})
        if corrupt_valid:
            # Both members fail with the identical missing-evidence digest, so a
            # repair bound to one capture can never authorize the other's row.
            corrupt_values[self.valid.capture_id] = None
        connection = open_local_database(
            self.profile, clock=_fixed_clock(offset_seconds=clock_offset)
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not corrupt_valid:
                connection.execute(
                    "UPDATE captures SET privacy_json=? WHERE capture_id=?",
                    (
                        json.dumps(_privacy(PrivacyTier.PERSONAL).to_dict()),
                        self.valid.capture_id,
                    ),
                )
            for capture_id, value in corrupt_values.items():
                connection.execute(
                    "UPDATE captures SET privacy_json=? WHERE capture_id=?",
                    (value, capture_id),
                )
            connection.execute("DELETE FROM source_revision_privacy")
            connection.execute("DELETE FROM canonical_revision_privacy")
            connection.execute("COMMIT")
        finally:
            connection.close()
        # Re-register through the engine-owned paths so the invalid evidence
        # projects with its append-only markers, then rebuild the live search
        # rows from that corrupted evidence.
        from open_brain_engine.engine.source_store import (
            register_completed_captures,
            register_publication_members,
        )

        connection = open_local_database(
            self.profile, clock=_fixed_clock(offset_seconds=clock_offset)
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            register_completed_captures(connection, self.profile)
            register_publication_members(connection, self.profile)
            connection.execute("COMMIT")
        finally:
            connection.close()
        from open_brain_engine.engine.reconciliation import rederive_live_search_projection

        rederive_live_search_projection(engine)
        self.engine = engine
        with _read_only(self.profile) as connection:
            self.broken_digest: str = _digest_of(
                connection, "source_revision", self.broken.capture_id
            )
            self.page_revision: str = str(connection.execute(
                "SELECT revision_id FROM canonical_revision_members WHERE page_id=?",
                (self.page_id,),
            ).fetchone()[0])
            self.page_digest = _digest_of(
                connection, "canonical_revision", self.page_revision
            )
        self.base_generation = self.generation()

    def repair(
        self,
        request: privacy_repairs.PrivacyRepairRequest,
        *,
        authority: EffectiveAuthority | None = None,
    ) -> privacy_repairs.PrivacyRepairReceipt:
        task = self.engine.tasks.privacy_repair
        assert task is not None
        return task.repair_privacy(
            request, authority=authority or _owner_authority()
        )

    def search_row(self, result_id: str) -> tuple[object, ...]:
        with _read_only(self.profile) as connection:
            return tuple(
                connection.execute(
                    f"SELECT {SEARCH_COLUMNS} FROM search_documents WHERE result_id=?",
                    (result_id,),
                ).fetchone()
            )

    def ledger_rows(self) -> list[sqlite3.Row]:
        with _read_only(self.profile) as connection:
            return connection.execute(
                "SELECT repair_id, repair_sequence, target_kind, target_id, "
                "invalid_evidence_sha256, owner_actor_id, replacement_privacy_json, "
                "operation_id, request_sha256, issuer_epoch, receipt_json, recorded_at, "
                "supersedes_repair_id FROM privacy_repair_ledger ORDER BY repair_sequence"
            ).fetchall()

    def generation(self) -> int:
        with _read_only(self.profile) as connection:
            return int(connection.execute(
                "SELECT retrieval_generation FROM engine_generations WHERE singleton=1"
            ).fetchone()[0])

    def retained_state(self) -> dict[str, Any]:
        with _read_only(self.profile) as connection:
            return {
                "captures": connection.execute(
                    "SELECT capture_id, privacy_json FROM captures ORDER BY capture_id"
                ).fetchall(),
                "source_revision_privacy": connection.execute(
                    "SELECT capture_id, effective_privacy_json FROM source_revision_privacy "
                    "ORDER BY capture_id"
                ).fetchall(),
                "canonical_revision_privacy": connection.execute(
                    "SELECT revision_id, effective_privacy_json FROM canonical_revision_privacy "
                    "ORDER BY revision_id"
                ).fetchall(),
                "markers": connection.execute(
                    "SELECT target_kind, target_id, invalid_reason, invalid_evidence_sha256 "
                    "FROM privacy_invalid_evidence ORDER BY target_kind, target_id"
                ).fetchall(),
            }


def _root_identity(root: Path) -> tuple[int, int]:
    stat = root.stat()
    return (stat.st_dev, stat.st_ino)


def _digest_of(
    connection: sqlite3.Connection, target_kind: str, target_id: str
) -> str:
    return str(connection.execute(
        "SELECT invalid_evidence_sha256 FROM privacy_invalid_evidence "
        "WHERE target_kind=? AND target_id=?",
        (target_kind, target_id),
    ).fetchone()[0])


def _digest_of_source_missing() -> str:
    return project_retained_privacy_evidence([None]).invalid_evidence_sha256 or ""


def _insert_orphan_source_target(brain: _RepairBrain) -> str:
    capture_id = "capture_orphan_source_revision"
    connection = open_local_database(brain.profile, clock=_fixed_clock())
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO captures (delivery_id, request_sha256, capture_id, "
            "accepted_receipt_id, payload_family, payload_json, search_text, file_bytes, "
            "source_origin, source_reference, space_id, intent, capture_why, action, title, "
            "accepted_at, stage, source_path, canonical_path, auto_proposal_id, "
            "auto_proposal_receipt_id, auto_decision_id, auto_decision_receipt_id, page_id, "
            "publication_id, publication_path, enrichment_state, actor_id, role_claim_json, "
            "privacy_json, provenance_json, submission_path) "
            "SELECT 'repair.orphan', request_sha256, ?, 'receipt_repair_orphan', "
            "payload_family, payload_json, search_text, file_bytes, source_origin, "
            "'synthetic:orphan', space_id, intent, capture_why, action, title, accepted_at, "
            "0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, enrichment_state, "
            "actor_id, role_claim_json, NULL, provenance_json, NULL FROM captures "
            "WHERE capture_id=?",
            (capture_id, brain.broken.capture_id),
        )
        connection.execute(
            "INSERT INTO privacy_invalid_evidence "
            "(target_kind, target_id, invalid_reason, invalid_evidence_sha256) "
            "VALUES ('source_revision', ?, 'missing', ?)",
            (capture_id, _digest_of_source_missing()),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return capture_id


def _insert_ledger_receipt(
    connection: sqlite3.Connection,
    *,
    request: privacy_repairs.PrivacyRepairRequest,
    repair_id: str,
    sequence: int,
    issuer_epoch: int = 1,
    stored_request_sha256: str | None = None,
    receipt_replacement: PrivacyDecision | None = None,
) -> privacy_repairs.PrivacyRepairReceipt:
    replacement = validate_stored_privacy_decision(request.replacement)
    request_sha256 = stored_request_sha256 or privacy_repairs.privacy_repair_request_sha256(
        request
    )
    receipt = privacy_repairs.PrivacyRepairReceipt(
        repair_id=repair_id,
        repair_sequence=sequence,
        target_kind=request.target_kind,
        target_id=request.target_id,
        invalid_evidence_sha256=request.invalid_evidence_sha256,
        owner_actor_id=OWNER_ACTOR_ID,
        issuer_epoch=issuer_epoch,
        replacement=receipt_replacement or replacement,
        operation_id=request.operation_id,
        request_sha256=request_sha256,
        supersedes_repair_id=request.supersedes_repair_id,
        recorded_at="2026-09-20T12:00:00.000000Z",
    )
    connection.execute(
        "INSERT INTO privacy_repair_ledger (repair_id, repair_sequence, target_kind, "
        "target_id, invalid_evidence_sha256, owner_actor_id, replacement_privacy_json, "
        "operation_id, request_sha256, issuer_epoch, receipt_json, recorded_at, "
        "supersedes_repair_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            receipt.repair_id,
            receipt.repair_sequence,
            receipt.target_kind,
            receipt.target_id,
            receipt.invalid_evidence_sha256,
            receipt.owner_actor_id,
            json.dumps(replacement.to_dict(), sort_keys=True, separators=(",", ":")),
            receipt.operation_id,
            request_sha256,
            issuer_epoch,
            receipt.encode(),
            receipt.recorded_at,
            receipt.supersedes_repair_id,
        ),
    )
    return receipt


def test_owner_authority_gates_the_task_before_any_target_lookup(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    request = _repair_request(
        "source_revision", brain.broken.capture_id, brain.broken_digest,
        operation_id="privacy-repair.authority.1",
    )
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="owner_required"):
        brain.repair(request, authority=_owner_authority(principal="actor_stranger", owner=True))
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="owner_required"):
        brain.repair(
            request, authority=_owner_authority(principal=OWNER_ACTOR_ID, owner=False)
        )
    assert brain.ledger_rows() == []
    assert brain.generation() == brain.base_generation


def test_repair_request_rejects_engine_owned_fields() -> None:
    with pytest.raises(TypeError):
        privacy_repairs.PrivacyRepairRequest(
            target_kind="source_revision",
            target_id="capture-1",
            invalid_evidence_sha256="a" * 64,
            replacement=_privacy(PrivacyTier.WORK),
            operation_id="privacy-repair.fields",
            owner_actor_id="actor_attacker",  # type: ignore[call-arg]
        )


def test_malformed_request_values_fail_closed_before_writes(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    for target_kind in ("search_document", "engine", ""):
        with pytest.raises(privacy_repairs.PrivacyRepairError, match="invalid_request"):
            brain.repair(
                _repair_request(
                    target_kind, brain.broken.capture_id, brain.broken_digest,
                    operation_id="privacy-repair.kind",
                )
            )
    for operation_id in ("", "-leading", "space id", "x" * 256 + "y"):
        with pytest.raises(privacy_repairs.PrivacyRepairError, match="invalid_request"):
            brain.repair(
                _repair_request(
                    "source_revision", brain.broken.capture_id, brain.broken_digest,
                    operation_id=operation_id,
                )
            )
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="invalid_request"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, "not-a-digest",
                operation_id="privacy-repair.digest",
            )
        )
    bad_replacement = {
        **_privacy(PrivacyTier.SECRET).to_dict(),
        "authority": {"cloud": True, "external_egress": False},
    }
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="invalid_request"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.secret",
                replacement=bad_replacement,
            )
        )
    assert brain.ledger_rows() == []
    assert brain.generation() == brain.base_generation


def test_missing_target_and_missing_marker_are_rejected(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="not_found"):
        brain.repair(
            _repair_request(
                "source_revision", "capture_missing", brain.broken_digest,
                operation_id="privacy-repair.absent",
            )
        )
    # Retained evidence that went malformed after its last registration is
    # genuinely invalid, but no append-only marker was ever written for that
    # digest: there is no exact marker to repair against.
    connection = open_local_database(brain.profile, clock=_fixed_clock())
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE captures SET privacy_json='not json' WHERE capture_id=?",
            (brain.valid.capture_id,),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    unmarked_digest = project_retained_privacy_evidence(["not json"]).invalid_evidence_sha256
    assert unmarked_digest is not None
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="not_found"):
        brain.repair(
            _repair_request(
                "source_revision", brain.valid.capture_id, unmarked_digest,
                operation_id="privacy-repair.no-marker",
            )
        )
    assert brain.ledger_rows() == []


def test_valid_base_and_stale_digest_are_rejected(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="evidence_mismatch"):
        brain.repair(
            _repair_request(
                "source_revision", brain.valid.capture_id, "0" * 64,
                operation_id="privacy-repair.valid-base",
            )
        )
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="evidence_mismatch"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, "0" * 64,
                operation_id="privacy-repair.stale-digest",
            )
        )
    assert brain.ledger_rows() == []
    assert brain.generation() == brain.base_generation


def test_first_repair_appends_and_refreshes_projection_exactly(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    before_state = brain.retained_state()
    before_generation = brain.generation()
    replacement = _privacy(PrivacyTier.WORK, cloud=True, external_egress=True)

    receipt = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.first", replacement=replacement,
        )
    )

    assert receipt.repair_sequence == 1
    assert receipt.target_kind == "source_revision"
    assert receipt.target_id == brain.broken.capture_id
    assert receipt.issuer_epoch == 1
    assert receipt.recorded_at.endswith("Z")
    rows = brain.ledger_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row[1] == 1
    assert row[5] == OWNER_ACTOR_ID
    assert row[9] == 1
    assert row[12] is None
    stored_receipt = json.loads(row[10])
    assert stored_receipt["repair_id"] == receipt.repair_id
    # The source search row carries the replacement tier and authority while
    # keeping the original invalid lineage.
    assert brain.search_row(brain.broken.capture_id) == (
        "work", 1, 1, "missing", brain.broken_digest,
        receipt.repair_id, 1,
    )
    assert brain.generation() == before_generation + 1
    assert brain.retained_state() == before_state


def test_exact_replay_returns_stored_receipt_without_side_effects(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    request = _repair_request(
        "source_revision", brain.broken.capture_id, brain.broken_digest,
        operation_id="privacy-repair.replay",
    )
    first = brain.repair(request)
    generation = brain.generation()
    rows = brain.ledger_rows()

    replayed = brain.repair(request)

    assert replayed == first
    assert brain.ledger_rows() == rows
    assert brain.generation() == generation


def test_same_operation_id_with_different_request_conflicts(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.conflict",
            replacement=_privacy(PrivacyTier.WORK),
        )
    )
    rows = brain.ledger_rows()
    generation = brain.generation()
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="operation_conflict"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.conflict",
                replacement=_privacy(PrivacyTier.PERSONAL),
            )
        )
    assert brain.ledger_rows() == rows
    assert brain.generation() == generation


def test_supersession_replaces_the_active_repair(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    first = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.first.head", replacement=_privacy(PrivacyTier.WORK),
        )
    )
    second = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.second.head",
            replacement=_privacy(PrivacyTier.PUBLIC, cloud=True, external_egress=True),
            supersedes_repair_id=first.repair_id,
        )
    )
    assert second.repair_sequence == 2
    assert second.supersedes_repair_id == first.repair_id
    assert [row[1] for row in brain.ledger_rows()] == [1, 2]
    assert brain.search_row(brain.broken.capture_id) == (
        "public", 1, 1, "missing", brain.broken_digest,
        second.repair_id, 2,
    )
    # The chain stays inspectable: superseded identity and successor identity.
    assert brain.ledger_rows()[0][0] == first.repair_id


def test_supersession_rejects_stale_head_branch_and_missing_reference(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    first = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.chain.first",
        )
    )
    head = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.chain.head",
            replacement=_privacy(PrivacyTier.WORK, cloud=True),
            supersedes_repair_id=first.repair_id,
        )
    )
    rows = brain.ledger_rows()
    generation = brain.generation()
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="supersession_invalid"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.chain.stale",
                supersedes_repair_id=first.repair_id,
            )
        )
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="supersession_invalid"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.chain.headless",
            )
        )
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="supersession_invalid"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.chain.absent",
                supersedes_repair_id="repair_missing",
            )
        )
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="supersession_invalid"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.chain.self",
                supersedes_repair_id=head.repair_id + "-same",
            )
        )
    assert brain.ledger_rows() == rows
    assert brain.generation() == generation


def test_supersession_rejects_cross_target_references(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    canonical = brain.repair(
        _repair_request(
            "canonical_revision", brain.page_revision, brain.page_digest,
            operation_id="privacy-repair.cross.first",
        )
    )
    # A source-revision target cannot supersede a canonical repair even though
    # both targets bind the same underlying retained evidence.
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="supersession_invalid"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.cross.kind",
                supersedes_repair_id=canonical.repair_id,
            )
        )


def test_source_repair_feeds_dependent_canonical_aggregation(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    receipt = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.aggregate",
            replacement=_privacy(PrivacyTier.WORK),
        )
    )
    # Valid member is personal (no egress); repaired member is work without
    # egress: the aggregate heals to the most restrictive valid tier with
    # intersected authority and no applied-repair lineage.
    assert brain.search_row(brain.page_id) == (
        "personal", 0, 0, None, None, None, None,
    )
    assert receipt.repair_id


def test_direct_canonical_repair_applies_only_while_base_remains_invalid(
    tmp_path: Path,
) -> None:
    brain = _RepairBrain(tmp_path)
    receipt = brain.repair(
        _repair_request(
            "canonical_revision", brain.page_revision, brain.page_digest,
            operation_id="privacy-repair.canonical.direct",
            replacement=_privacy(PrivacyTier.WORK, cloud=True, external_egress=True),
        )
    )
    assert brain.search_row(brain.page_id) == (
        "work", 1, 1, "missing", brain.page_digest, receipt.repair_id, 1,
    )
    assert brain.search_row(brain.broken.capture_id)[0] == "unknown"

    # Once the member repair heals the canonical aggregate, a direct canonical
    # repair against the stale invalid base is no longer eligible.
    member = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.canonical.member",
        )
    )
    assert member.repair_sequence == 2
    assert brain.search_row(brain.page_id)[:1] == ("personal",)
    rows = brain.ledger_rows()
    generation = brain.generation()
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="evidence_mismatch"):
        brain.repair(
            _repair_request(
                "canonical_revision", brain.page_revision, brain.page_digest,
                operation_id="privacy-repair.canonical.stale",
            )
        )
    assert brain.ledger_rows() == rows
    assert brain.generation() == generation


def test_multiple_unrelated_invalid_records_repair_independently(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    # The fixture already holds two unrelated invalid targets: the broken
    # source revision and the page revision aggregating it.
    first = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.multi.1",
        )
    )
    assert first.repair_sequence == 1
    # The unrelated invalid canonical page heals through member aggregation
    # rather than staying invalid, while the valid member row never changes.
    assert brain.search_row(brain.valid.capture_id)[0] == "personal"


def test_active_repair_is_stable_across_restart_and_independent_of_clock(
    tmp_path: Path,
) -> None:
    brain_a = _RepairBrain(tmp_path / "a", clock_offset=0)
    brain_b = _RepairBrain(tmp_path / "b", clock_offset=987654)
    for brain, suffix in ((brain_a, "a"), (brain_b, "b")):
        first = brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id=f"privacy-repair.stability.{suffix}",
                replacement=_privacy(PrivacyTier.WORK),
            )
        )
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id=f"privacy-repair.stability.{suffix}.head",
                replacement=_privacy(PrivacyTier.PUBLIC, cloud=True, external_egress=True),
                supersedes_repair_id=first.repair_id,
            )
        )
    row_a = brain_a.search_row(brain_a.broken.capture_id)
    row_b = brain_b.search_row(brain_b.broken.capture_id)
    assert row_a[0:5] == row_b[0:5]
    assert row_a[6] == row_b[6] == 2
    # Restart: reopen the engine and rederive; the active repair survives.
    engine = BrainEngine.open(brain_a.profile, clock=_fixed_clock(offset_seconds=5))
    from open_brain_engine.engine.reconciliation import rederive_live_search_projection

    rederive_live_search_projection(engine)
    assert brain_a.search_row(brain_a.broken.capture_id) == row_a


def test_issuer_epoch_is_stamped_from_stored_identity_and_tampering_fails_closed(
    tmp_path: Path,
) -> None:
    brain = _RepairBrain(tmp_path)
    brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.issuer.first",
        )
    )
    # Invent issuer state the identity never authorized: a fabricated migration
    # marker that agrees with no manifest digest.
    connection = open_local_database(brain.profile, clock=_fixed_clock())
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO issuer_migration_marker VALUES ("
            "1, x'74616d7065726564', ?, 'brain_tampered', 1, 2, ?, "
            "'2026-01-01T00:00:00.000000Z')",
            ("0" * 64, "0" * 64),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    rows = brain.ledger_rows()
    generation = brain.generation()
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="issuer_mismatch"):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.issuer.tampered",
                supersedes_repair_id=rows[0][0],
            )
        )
    assert brain.ledger_rows() == rows
    assert brain.generation() == generation


def test_faults_before_and_after_append_roll_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = _RepairBrain(tmp_path)
    before_state = brain.retained_state()
    before_generation = brain.generation()
    before_row = brain.search_row(brain.broken.capture_id)

    class Crash(RuntimeError):
        pass

    original_append = privacy_repairs._append_repair_row
    original_refresh = privacy_repairs._refresh_affected_search_rows

    def crash_append(*args: object, **kwargs: object) -> None:
        raise Crash

    def crash_refresh(*args: object, **kwargs: object) -> None:
        raise Crash

    def crash_generation(*args: object, **kwargs: object) -> None:
        raise Crash

    # Fault before the append: nothing is written.
    monkeypatch.setattr(privacy_repairs, "_append_repair_row", crash_append)
    with pytest.raises(Crash):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.fault.before",
            )
        )
    assert brain.ledger_rows() == []
    assert brain.generation() == before_generation

    monkeypatch.setattr(privacy_repairs, "_append_repair_row", original_append)
    # Fault after the append but before the projection refresh: the append rolls back.
    monkeypatch.setattr(privacy_repairs, "_refresh_affected_search_rows", crash_refresh)
    with pytest.raises(Crash):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.fault.after-append",
            )
        )
    assert brain.ledger_rows() == []

    # Fault after the refresh but before the generation advance.
    monkeypatch.setattr(privacy_repairs, "_refresh_affected_search_rows", original_refresh)
    monkeypatch.setattr(privacy_repairs, "_advance_retrieval_generation", crash_generation)
    with pytest.raises(Crash):
        brain.repair(
            _repair_request(
                "source_revision", brain.broken.capture_id, brain.broken_digest,
                operation_id="privacy-repair.fault.after-refresh",
            )
        )
    assert brain.ledger_rows() == []
    assert brain.generation() == before_generation
    assert brain.search_row(brain.broken.capture_id) == before_row
    assert brain.retained_state() == before_state
    assert json.dumps(
        [
            list(row)
            for row in brain.retained_state()["markers"]
        ]
    ) == json.dumps([list(row) for row in before_state["markers"]])


def test_source_repair_must_target_the_row_capture_in_search_binding(
    tmp_path: Path,
) -> None:
    brain = _RepairBrain(tmp_path, corrupt_valid=True)
    assert brain.broken_digest == _digest_of_source_missing()
    receipt = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.binding",
        )
    )
    connection = open_local_database(brain.profile, clock=_fixed_clock())
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE search_documents SET applied_repair_id=?, applied_repair_sequence=? "
                "WHERE result_id=?",
                (receipt.repair_id, receipt.repair_sequence, brain.valid.capture_id),
            )
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_repaired_secret_stays_local_only(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    secret = PrivacyDecision.create(
        tier=PrivacyTier.SECRET,
        reason=PrivacyReason.SECRET_DETECTED,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )
    receipt = brain.repair(
        _repair_request(
            "source_revision", brain.broken.capture_id, brain.broken_digest,
            operation_id="privacy-repair.secret-local",
            replacement=secret,
        )
    )
    assert brain.search_row(brain.broken.capture_id) == (
        "secret", 0, 0, "missing", brain.broken_digest, receipt.repair_id, 1,
    )


def test_request_digest_binds_decision_triple_and_supersession_only() -> None:
    replacement = _privacy(PrivacyTier.WORK)
    base_request = privacy_repairs.PrivacyRepairRequest(
        target_kind="source_revision",
        target_id="capture-1",
        invalid_evidence_sha256="a" * 64,
        replacement=replacement,
        operation_id="privacy-repair.digest.a",
    )
    same_request_other_operation = privacy_repairs.PrivacyRepairRequest(
        target_kind="source_revision",
        target_id="capture-1",
        invalid_evidence_sha256="a" * 64,
        replacement=replacement,
        operation_id="privacy-repair.digest.b",
    )
    assert privacy_repairs.privacy_repair_request_sha256(base_request) == (
        privacy_repairs.privacy_repair_request_sha256(same_request_other_operation)
    )
    for mutated in (
        privacy_repairs.PrivacyRepairRequest(
            target_kind="canonical_revision",
            target_id="capture-1",
            invalid_evidence_sha256="a" * 64,
            replacement=replacement,
            operation_id="privacy-repair.digest.a",
        ),
        privacy_repairs.PrivacyRepairRequest(
            target_kind="source_revision",
            target_id="capture-2",
            invalid_evidence_sha256="a" * 64,
            replacement=replacement,
            operation_id="privacy-repair.digest.a",
        ),
        privacy_repairs.PrivacyRepairRequest(
            target_kind="source_revision",
            target_id="capture-1",
            invalid_evidence_sha256="b" * 64,
            replacement=replacement,
            operation_id="privacy-repair.digest.a",
        ),
        privacy_repairs.PrivacyRepairRequest(
            target_kind="source_revision",
            target_id="capture-1",
            invalid_evidence_sha256="a" * 64,
            replacement=_privacy(PrivacyTier.WORK, cloud=True),
            operation_id="privacy-repair.digest.a",
        ),
        privacy_repairs.PrivacyRepairRequest(
            target_kind="source_revision",
            target_id="capture-1",
            invalid_evidence_sha256="a" * 64,
            replacement=replacement,
            operation_id="privacy-repair.digest.a",
            supersedes_repair_id="repair_1",
        ),
    ):
        assert privacy_repairs.privacy_repair_request_sha256(mutated) != (
            privacy_repairs.privacy_repair_request_sha256(base_request)
        )


def test_source_repair_requires_an_immutable_source_revision(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    capture_id = _insert_orphan_source_target(brain)
    generation = brain.generation()

    with pytest.raises(privacy_repairs.PrivacyRepairError, match="not_found"):
        brain.repair(
            _repair_request(
                "source_revision",
                capture_id,
                _digest_of_source_missing(),
                operation_id="privacy-repair.orphan.admission",
            )
        )

    assert brain.ledger_rows() == []
    assert brain.generation() == generation
    with _read_only(brain.profile) as connection:
        assert connection.execute(
            "SELECT count(*) FROM source_revisions WHERE capture_id=?", (capture_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM search_documents WHERE capture_id=?", (capture_id,)
        ).fetchone()[0] == 0


def test_full_audit_rejects_a_repair_for_an_orphan_source_target(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    capture_id = _insert_orphan_source_target(brain)
    request = _repair_request(
        "source_revision",
        capture_id,
        _digest_of_source_missing(),
        operation_id="privacy-repair.orphan.audit",
    )
    connection = open_local_database(brain.profile, clock=_fixed_clock())
    try:
        connection.execute("BEGIN IMMEDIATE")
        _insert_ledger_receipt(
            connection, request=request, repair_id="repair_orphan", sequence=1
        )
        with pytest.raises(T03Error, match="operation_pending"):
            privacy_repairs.audit_privacy_repair_ledger(connection)
        connection.execute("ROLLBACK")
    finally:
        connection.close()


@pytest.mark.parametrize("position", ["active", "superseded"])
@pytest.mark.parametrize(
    "corruption", ["wrong_epoch", "wrong_request_digest", "receipt_disagreement"]
)
def test_all_live_consumers_reject_corrupt_active_and_superseded_rows(
    tmp_path: Path, position: str, corruption: str
) -> None:
    brain = _RepairBrain(tmp_path)
    first_request = _repair_request(
        "source_revision",
        brain.broken.capture_id,
        brain.broken_digest,
        operation_id=f"privacy-repair.corrupt.{position}.{corruption}.first",
        replacement=_privacy(PrivacyTier.WORK, cloud=True),
    )
    bad_epoch = 999 if corruption == "wrong_epoch" else 1
    bad_digest = "f" * 64 if corruption == "wrong_request_digest" else None
    receipt_replacement = (
        _privacy(PrivacyTier.PUBLIC, cloud=True, external_egress=True)
        if corruption == "receipt_disagreement"
        else None
    )
    connection = open_local_database(brain.profile, clock=_fixed_clock())
    try:
        connection.execute("BEGIN IMMEDIATE")
        first = _insert_ledger_receipt(
            connection,
            request=first_request,
            repair_id=f"repair_corrupt_{position}_{corruption}",
            sequence=1,
            issuer_epoch=bad_epoch,
            stored_request_sha256=bad_digest,
            receipt_replacement=receipt_replacement,
        )
        head = first
        if position == "superseded":
            second_request = _repair_request(
                "source_revision",
                brain.broken.capture_id,
                brain.broken_digest,
                operation_id=f"privacy-repair.corrupt.{position}.{corruption}.second",
                replacement=_privacy(PrivacyTier.PUBLIC, cloud=True),
                supersedes_repair_id=first.repair_id,
            )
            head = _insert_ledger_receipt(
                connection,
                request=second_request,
                repair_id=f"repair_head_{corruption}",
                sequence=2,
            )
        connection.execute("COMMIT")
    finally:
        connection.close()

    rows = brain.ledger_rows()
    generation = brain.generation()
    with _read_only(brain.profile) as connection:
        with pytest.raises(privacy_repairs.PrivacyRepairError, match="ledger_corrupt"):
            privacy_repairs.resolve_search_privacy(
                connection,
                result_id=brain.broken.capture_id,
                capture_id=brain.broken.capture_id,
                record_type="source",
            )
        with pytest.raises(T03Error, match="operation_pending"):
            privacy_repairs.audit_privacy_repair_ledger(connection)
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="ledger_corrupt"):
        brain.repair(first_request)
    with pytest.raises(privacy_repairs.PrivacyRepairError, match="ledger_corrupt"):
        brain.repair(
            _repair_request(
                "source_revision",
                brain.broken.capture_id,
                brain.broken_digest,
                operation_id=f"privacy-repair.corrupt.{position}.{corruption}.append",
                supersedes_repair_id=head.repair_id,
            )
        )
    assert brain.ledger_rows() == rows
    assert brain.generation() == generation


def test_canonical_membership_conflict_stays_typed_with_and_without_direct_repair(
    tmp_path: Path,
) -> None:
    brain = _RepairBrain(tmp_path)

    with _read_only(brain.profile) as connection:
        base, applied = privacy_repairs.resolve_search_privacy(
            connection,
            result_id=brain.page_id,
            capture_id=brain.broken.capture_id,
            record_type="canonical",
        )
    assert base.invalid_reason is not None
    assert applied is None

    receipt = brain.repair(
        _repair_request(
            "canonical_revision",
            brain.page_revision,
            brain.page_digest,
            operation_id="privacy-repair.membership.direct",
        )
    )
    with _read_only(brain.profile) as connection:
        base, applied = privacy_repairs.resolve_search_privacy(
            connection,
            result_id=brain.page_id,
            capture_id=brain.broken.capture_id,
            record_type="canonical",
    )
    assert base.invalid_reason is not None
    assert applied == (receipt.repair_id, receipt.repair_sequence)


def test_writer_contention_is_bounded_and_writes_nothing(tmp_path: Path) -> None:
    brain = _RepairBrain(tmp_path)
    request = _repair_request(
        "source_revision",
        brain.broken.capture_id,
        brain.broken_digest,
        operation_id="privacy-repair.writer-contention",
    )
    generation = brain.generation()

    with (
        brain.engine._writer_lease.acquire_shared_writer(),
        pytest.raises(privacy_repairs.PrivacyRepairError, match="operation_pending"),
    ):
        brain.repair(request)

    assert brain.ledger_rows() == []
    assert brain.generation() == generation
