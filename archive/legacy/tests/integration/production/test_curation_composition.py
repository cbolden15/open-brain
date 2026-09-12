from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import (
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    Intent,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.core.policy import classify_privacy
from open_brain_engine.core.ports import EventRecord, RedactionReceipt
from open_brain_engine.events.store import SqliteEventStore
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewProposal,
    ReviewState,
    capture_reference_for,
)
from open_brain_engine.storage.writer_record import write_canonical_writer_record

from open_brain_legacy._compat.open_brain.config import (
    AppConfig,
    LedgerConfig,
    LedgerRouteConfig,
    LedgerTaxonomyConfig,
    RetainedRoots,
)
from open_brain_legacy.cli.scheduled import ScheduledDispatchStatus
from open_brain_legacy.operations.writer_jobs import get_writer_job_spec
from open_brain_legacy.review.maintenance import (
    CurationClass,
    CurationTarget,
    predecessor_curation_taxonomy,
)
from open_brain_legacy.review.store import SqliteReviewStore
from open_brain_legacy.services.application import ConfiguredScheduledAdapters


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _config(tmp_path: Path) -> AppConfig:
    paths = {
        name: tmp_path / name
        for name in ("work", "personal", "capture", "saved", "state", "backup")
    }
    for path in paths.values():
        path.mkdir()
    taxonomy = LedgerTaxonomyConfig.create(
        version="production-v1",
        routes=(
            LedgerRouteConfig.create(
                path_prefix=("patterns",),
                topic_id="patterns",
                topic_label="Patterns",
                privacy_tier=PrivacyTier.WORK.value,
            ),
        ),
    )
    return AppConfig(
        roots=RetainedRoots(
            work=paths["work"],
            personal=paths["personal"],
            capture=paths["capture"],
            saved_content=paths["saved"],
            state=paths["state"],
        ),
        backup=paths["backup"],
        host_identity="synthetic-writer",
        ledger=LedgerConfig(taxonomy),
    )


def _event(capture_id: str) -> EventRecord:
    privacy = classify_privacy(PrivacyTier.WORK, policy_version="privacy-v1")
    payload = {
        "text": "Synthetic extracted text",
        "capture_why": "Keep this synthetic finding",
        "capture_source": CaptureSource.CLI.value,
        "source_type": SourceType.WEB.value,
        "content_kind": ContentKind.ARTICLE.value,
        "provenance": Provenance.create(
            source_ref="https://example.test/synthetic",
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ).to_dict(),
    }
    return EventRecord.create(
        event_id="evt_" + "a" * 32,
        stream_id=capture_id,
        event_type="capture.extracted",
        occurred_at=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
        privacy_decision=privacy,
        payload=payload,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256="b" * 64,
            output_digest_sha256=EventRecord.output_digest_sha256(payload),
            policy_version="redaction-v1",
        ),
    )


def test_production_curation_assembles_applies_replays_and_delivers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    clock = FixedClock()
    capture_id = "cap_" + "a" * 64
    proposal = ReviewProposal.create(
        capture_id=capture_id,
        source_ref=capture_reference_for(capture_id),
        privacy_tier=PrivacyTier.WORK,
        proposed_intent=Intent.IDEA,
        proposal_reason="Synthetic curation proposal",
        capture_why="Keep this synthetic finding",
        created_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        created_by=Actor(ActorKind.SYSTEM, "fixture"),
    )
    aggregate = ReviewAggregate.create(proposal)
    (config.state_root / "events").mkdir()
    with (
        SqliteEventStore(
            root=config.state_root / "events",
            database_name="events.sqlite3",
            clock=clock,
        ) as events,
        SqliteReviewStore(
            root=config.state_root,
            database_name="review/review.sqlite3",
            clock=clock,
        ) as reviews,
    ):
        payload = canonical_json_bytes(aggregate.to_dict())
        reviews.create_if_absent(aggregate, payload_digest=sha256(payload).hexdigest())
        reviews.register_curation_target(
            CurationTarget.create(
                review=aggregate,
                tier=PrivacyTier.WORK,
                category="patterns",
                slug="synthetic",
                title="Synthetic",
                classification_class=CurationClass.NEW_PAGE,
                occurred_at=datetime(2026, 8, 15, 9, 30, tzinfo=UTC),
                taxonomy=predecessor_curation_taxonomy(),
            )
        )
        decision = reviews.decide(
            proposal.review_id,
            ReviewDecisionCommand.create(
                decision_id="decision-synthetic",
                target_state=ReviewState.APPLIED,
                reason="Synthetic owner approval",
                occurred_at=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
                actor=Actor(ActorKind.OWNER, "owner"),
            ),
        )
        assert decision.approved_record is not None
        events.append(_event(str(proposal.capture_id)))
        write_canonical_writer_record(
            state_root=config.state_root,
            identity_id="synthetic-writer",
            generation=1,
            recorded_at=clock.now(),
        )

    adapters = ConfiguredScheduledAdapters(config, clock)
    first = adapters.dispatch_writer(get_writer_job_spec("JOB-012"))
    replay = adapters.dispatch_writer(get_writer_job_spec("JOB-012"))
    with SqliteReviewStore(
        root=config.state_root,
        database_name="review/review.sqlite3",
        clock=clock,
    ) as reviews:
        assert first.status is ScheduledDispatchStatus.COMPLETED
        assert replay.status is ScheduledDispatchStatus.COMPLETED
        assert reviews.pending_outputs(limit=10) == ()
    assert len(tuple(config.work_root.rglob("*.md"))) == 2


def test_production_curation_queues_incomplete_approved_binding_without_publish(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    clock = FixedClock()
    capture_id = "cap_" + "c" * 64
    proposal = ReviewProposal.create(
        capture_id=capture_id,
        source_ref=capture_reference_for(capture_id),
        privacy_tier=PrivacyTier.WORK,
        proposed_intent=Intent.ACTION_CANDIDATE,
        proposal_reason="Synthetic incomplete curation proposal",
        capture_why="Review this synthetic incomplete binding",
        created_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        created_by=Actor(ActorKind.SYSTEM, "fixture"),
    )
    aggregate = ReviewAggregate.create(proposal)
    (config.state_root / "events").mkdir()
    with (
        SqliteEventStore(
            root=config.state_root / "events",
            database_name="events.sqlite3",
            clock=clock,
        ),
        SqliteReviewStore(
            root=config.state_root,
            database_name="review/review.sqlite3",
            clock=clock,
        ) as reviews,
    ):
        payload = canonical_json_bytes(aggregate.to_dict())
        reviews.create_if_absent(aggregate, payload_digest=sha256(payload).hexdigest())
        reviews.decide(
            proposal.review_id,
            ReviewDecisionCommand.create(
                decision_id="decision-incomplete",
                target_state=ReviewState.APPLIED,
                reason="Synthetic owner approval",
                occurred_at=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
                actor=Actor(ActorKind.OWNER, "owner"),
            ),
        )
        write_canonical_writer_record(
            state_root=config.state_root,
            identity_id="synthetic-writer",
            generation=1,
            recorded_at=clock.now(),
        )

    result = ConfiguredScheduledAdapters(config, clock).dispatch_writer(
        get_writer_job_spec("JOB-012")
    )
    with SqliteReviewStore(
        root=config.state_root,
        database_name="review/review.sqlite3",
        clock=clock,
    ) as reviews:
        assert result.status is ScheduledDispatchStatus.COMPLETED
        assert reviews.pending_outputs(limit=10) == ()
    assert len(tuple((config.state_root / "curation-review").rglob("*.json"))) == 1
    assert tuple(config.work_root.rglob("*.md")) == ()
