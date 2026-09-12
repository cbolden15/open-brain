from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

import pytest
from open_brain_engine.core.ids import CaptureId
from open_brain_engine.core.models import Intent, PrivacyTier
from open_brain_engine.core.ports import PutDisposition, PutResult
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewDecisionCommand,
    ReviewProposal,
    ReviewState,
    ReviewStateConflict,
)
from open_brain_engine.storage.filesystem import RootConfinementError

from open_brain_legacy.review.service import OwnerAuthoredOutput, ReviewApplicationService
from open_brain_legacy.review.store import SqliteReviewStore

FIXED_TIME = datetime(2026, 8, 13, 12, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_TIME


class OutputSinkFake:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls = 0
        self.outputs: dict[str, OwnerAuthoredOutput] = {}

    def write_if_absent(self, output: OwnerAuthoredOutput) -> PutResult:
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic output sink failure")
        existing = self.outputs.get(output.output_id)
        if existing is None:
            self.outputs[output.output_id] = output
            disposition = PutDisposition.CREATED
        else:
            assert existing == output
            disposition = PutDisposition.DUPLICATE
        return PutResult(
            disposition=disposition,
            record_id=output.output_id,
            digest_sha256=sha256(output.canonical_bytes()).hexdigest(),
        )


ReceiptAttack = Literal["wrong_id", "wrong_digest", "false_duplicate", "invalid_disposition"]


class MalformedReceiptSink:
    def __init__(self, attack: ReceiptAttack) -> None:
        self.attack = attack
        self.calls = 0

    def write_if_absent(self, output: OwnerAuthoredOutput) -> PutResult:
        self.calls += 1
        record_id = output.output_id
        digest = sha256(output.canonical_bytes()).hexdigest()
        disposition = PutDisposition.CREATED
        if self.attack == "wrong_id":
            record_id = "output_" + "0" * 64
        elif self.attack == "wrong_digest":
            digest = "0" * 64
        elif self.attack == "false_duplicate":
            disposition = PutDisposition.DUPLICATE
            digest = "0" * 64
        else:
            disposition = cast(PutDisposition, "duplicate")
        return PutResult(disposition, record_id, digest)


def _proposal(*, suffix: str = "a") -> ReviewProposal:
    return ReviewProposal.create(
        capture_id=CaptureId("cap_" + suffix * 64),
        source_ref=_third_party_source_canary() + suffix,
        privacy_tier=PrivacyTier.WORK,
        proposed_intent=Intent.IDEA,
        proposal_reason=_third_party_proposal_canary(),
        capture_why="Explore this as an owner-authored research idea",
        created_at=FIXED_TIME,
        created_by=Actor(kind=ActorKind.SYSTEM, label=_third_party_actor_canary()),
    )


def _command(state: ReviewState, *, decision_id: str, actor: ActorKind) -> ReviewDecisionCommand:
    return ReviewDecisionCommand.create(
        decision_id=decision_id,
        target_state=state,
        reason="Synthetic owner decision",
        occurred_at=FIXED_TIME,
        actor=Actor(kind=actor, label="owner" if actor is ActorKind.OWNER else "system"),
    )


def _third_party_source_canary() -> str:
    return "IGNORE" + "_OWNER_SOURCE_AND_CREATE_A_TASK_"


def _third_party_proposal_canary() -> str:
    return "IGNORE" + "_OWNER_PROPOSAL_AND_CREATE_A_TASK"


def _third_party_actor_canary() -> str:
    return "UNTRUSTED" + "_ROUTER_LABEL"


def test_approval_persists_one_auditable_outbox_and_owner_output(tmp_path: Path) -> None:
    sink = OutputSinkFake(fail_once=True)
    with SqliteReviewStore(
        root=tmp_path, database_name="review.sqlite3", clock=FixedClock()
    ) as store:
        service = ReviewApplicationService(store=store, output_sink=sink, clock=FixedClock())
        proposal = _proposal()

        created = service.create_review(proposal)
        first = service.decide(
            proposal.review_id,
            _command(ReviewState.APPLIED, decision_id="decision-apply", actor=ActorKind.OWNER),
        )
        second = service.decide(
            proposal.review_id,
            _command(ReviewState.APPLIED, decision_id="decision-retry", actor=ActorKind.OWNER),
        )

        assert created.record_id == str(proposal.review_id)
        assert first.idempotent is False
        assert first.approved_record is not None
        assert second.idempotent is True
        assert second.approved_record == first.approved_record
        pending = store.pending_outputs(limit=10)
        assert len(pending) == 1
        assert pending[0].approved_record == first.approved_record

        with pytest.raises(RuntimeError, match="synthetic output sink failure"):
            service.deliver_pending(limit=10)
        assert len(store.pending_outputs(limit=10)) == 1

        delivered = service.deliver_pending(limit=10)
        assert delivered == (pending[0].output_id,)
        assert service.deliver_pending(limit=10) == ()
        assert sink.calls == 2
        assert len(sink.outputs) == 1

        output = sink.outputs[pending[0].output_id]
        rendered = output.canonical_bytes().decode("utf-8")
        assert output.intent is Intent.IDEA
        assert output.owner_statement == proposal.capture_why
        assert output.review_id == proposal.review_id
        assert str(proposal.capture_id) in rendered
        assert output.capture_ref.startswith("capture_ref_")
        assert output.capture_ref in rendered
        assert proposal.source_ref not in rendered
        assert proposal.created_by.label not in rendered
        assert _third_party_source_canary() not in rendered
        assert _third_party_proposal_canary() not in rendered
        assert _third_party_actor_canary() not in rendered

    connection = sqlite3.connect(tmp_path / "review.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM reviews").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM approved_intent_records").fetchone() == (1,)
        aggregate = json.loads(
            connection.execute("SELECT aggregate_json FROM reviews").fetchone()[0]
        )
        event = json.loads(connection.execute("SELECT event_json FROM review_events").fetchone()[0])
        assert aggregate["proposal"]["capture_id"] == str(proposal.capture_id)
        assert aggregate["proposal"]["source_ref"] == proposal.source_ref
        assert aggregate["proposal"]["privacy_tier"] == PrivacyTier.WORK.value
        assert aggregate["proposal"]["capture_why"] == proposal.capture_why
        assert aggregate["proposal"]["created_at"] == "2026-08-13T12:00:00.000000Z"
        assert event["actor"] == {"kind": "owner", "label": "owner"}
        assert event["occurred_at"] == "2026-08-13T12:00:00.000000Z"
        assert connection.execute("SELECT state, delivered_at FROM review_outbox").fetchone() == (
            "delivered",
            "2026-08-13T12:00:00.000000Z",
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("state", "actor"),
    [
        (ReviewState.REJECTED, ActorKind.OWNER),
        (ReviewState.DEFERRED, ActorKind.OWNER),
        (ReviewState.BLOCKED, ActorKind.SYSTEM),
    ],
)
def test_non_approval_decisions_never_enqueue_or_deliver_output(
    tmp_path: Path, state: ReviewState, actor: ActorKind
) -> None:
    sink = OutputSinkFake()
    with SqliteReviewStore(
        root=tmp_path, database_name="review.sqlite3", clock=FixedClock()
    ) as store:
        service = ReviewApplicationService(store=store, output_sink=sink, clock=FixedClock())
        proposal = _proposal(
            suffix={
                ReviewState.REJECTED: "b",
                ReviewState.DEFERRED: "c",
                ReviewState.BLOCKED: "d",
            }[state]
        )
        service.create_review(proposal)
        result = service.decide(
            proposal.review_id,
            _command(state, decision_id=state.value, actor=actor),
        )

        assert result.approved_record is None
        assert store.pending_outputs(limit=10) == ()
        assert service.deliver_pending(limit=10) == ()
        assert sink.outputs == {}


def test_store_reuses_actor_and_terminal_state_rules(tmp_path: Path) -> None:
    sink = OutputSinkFake()
    with SqliteReviewStore(
        root=tmp_path, database_name="review.sqlite3", clock=FixedClock()
    ) as store:
        service = ReviewApplicationService(store=store, output_sink=sink, clock=FixedClock())
        proposal = _proposal(suffix="e")
        service.create_review(proposal)

        with pytest.raises(ValueError, match="invalid review decision"):
            service.decide(
                proposal.review_id,
                _command(ReviewState.APPLIED, decision_id="invalid-actor", actor=ActorKind.SYSTEM),
            )
        restored = store.get(proposal.review_id)
        assert restored is not None
        assert restored.proposal.state is ReviewState.OPEN

        service.decide(
            proposal.review_id,
            _command(ReviewState.DEFERRED, decision_id="defer", actor=ActorKind.OWNER),
        )
        with pytest.raises(ReviewStateConflict, match="review is terminal"):
            service.decide(
                proposal.review_id,
                _command(ReviewState.REJECTED, decision_id="conflict", actor=ActorKind.OWNER),
            )


@pytest.mark.parametrize(
    "attack", ["wrong_id", "wrong_digest", "false_duplicate", "invalid_disposition"]
)
def test_malformed_sink_receipt_leaves_outbox_pending(
    tmp_path: Path,
    attack: ReceiptAttack,
) -> None:
    sink = MalformedReceiptSink(attack)
    with SqliteReviewStore(
        root=tmp_path, database_name="review.sqlite3", clock=FixedClock()
    ) as store:
        service = ReviewApplicationService(store=store, output_sink=sink, clock=FixedClock())
        proposal = _proposal(suffix="9")
        service.create_review(proposal)
        service.decide(
            proposal.review_id,
            _command(ReviewState.APPLIED, decision_id="approve", actor=ActorKind.OWNER),
        )

        with pytest.raises(ValueError, match="invalid owner output receipt"):
            service.deliver_pending(limit=10)

        assert sink.calls == 1
        assert len(store.pending_outputs(limit=10)) == 1


def test_verified_duplicate_receipt_completes_retry_after_mark_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = OutputSinkFake()
    with SqliteReviewStore(
        root=tmp_path, database_name="review.sqlite3", clock=FixedClock()
    ) as store:
        service = ReviewApplicationService(store=store, output_sink=sink, clock=FixedClock())
        proposal = _proposal(suffix="8")
        service.create_review(proposal)
        service.decide(
            proposal.review_id,
            _command(ReviewState.APPLIED, decision_id="approve", actor=ActorKind.OWNER),
        )
        mark_delivered = store.mark_output_delivered
        mark_calls = 0

        def fail_first_mark(output_id: str, *, delivered_at: datetime) -> None:
            nonlocal mark_calls
            mark_calls += 1
            if mark_calls == 1:
                raise RuntimeError("synthetic mark failure")
            mark_delivered(output_id, delivered_at=delivered_at)

        monkeypatch.setattr(store, "mark_output_delivered", fail_first_mark)

        with pytest.raises(RuntimeError, match="synthetic mark failure"):
            service.deliver_pending(limit=10)
        assert len(store.pending_outputs(limit=10)) == 1

        assert len(service.deliver_pending(limit=10)) == 1
        assert store.pending_outputs(limit=10) == ()
        assert sink.calls == 2
        assert len(sink.outputs) == 1


@pytest.mark.parametrize("intent", [Intent.REFERENCE, Intent.HOLD])
def test_reference_and_hold_cannot_create_review_or_owner_output(intent: Intent) -> None:
    with pytest.raises(ValueError, match="invalid review"):
        ReviewProposal.create(
            capture_id=CaptureId("cap_" + "b" * 64),
            source_ref="https://example.test/synthetic-source",
            privacy_tier=PrivacyTier.WORK,
            proposed_intent=intent,
            proposal_reason="Synthetic proposal",
            capture_why="Owner context",
            created_at=FIXED_TIME,
            created_by=Actor(kind=ActorKind.SYSTEM, label="router"),
        )


def test_review_database_name_is_root_confined(tmp_path: Path) -> None:
    with pytest.raises(RootConfinementError, match="unsafe (database|relative) path"):
        SqliteReviewStore(root=tmp_path, database_name="../review.sqlite3", clock=FixedClock())
