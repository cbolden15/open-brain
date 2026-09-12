from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, cast

import pytest
from open_brain_engine.capture.models import DistillationWorkItem
from open_brain_engine.core.ids import canonical_json_bytes, review_id_for
from open_brain_engine.core.models import (
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    Intent,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.core.policy import IntentPolicyReason, classify_privacy
from open_brain_engine.core.ports import EventRecord, PutDisposition, PutResult, RedactionReceipt
from open_brain_engine.review.models import ReviewAggregate, ReviewProposal, ReviewState
from open_brain_engine.storage.frontmatter import (
    AtomicMarkdownReader,
    AtomicMarkdownSink,
    markdown_relative_path,
)

from open_brain_legacy.ledger.merge import TrustedCitation
from open_brain_legacy.ledger.models import LedgerRoute, LedgerTaxonomy
from open_brain_legacy.ledger.sanitize import LedgerSection, sanitize_leaf
from open_brain_legacy.ledger.scan import scan_distillation_work_item
from open_brain_legacy.ledger.service import (
    ApplyResult,
    CaptureCitationResolver,
    LedgerService,
    PreparedLedgerApply,
)
from open_brain_legacy.ledger.stage import LedgerStage, stage_scan_record
from open_brain_legacy.ledger.store import SqliteLedgerStore
from open_brain_legacy.review.routing import (
    IntentRoutingDestination,
    IntentRoutingError,
    IntentRoutingStatus,
    Phase4IntentRouter,
)
from open_brain_legacy.review.service import OwnerAuthoredOutput, ReviewApplicationService
from open_brain_legacy.review.store import SqliteReviewStore

FIXED_TIME = datetime(2026, 8, 13, 12, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_TIME


class OutputSinkSpy:
    def __init__(self) -> None:
        self.calls = 0

    def write_if_absent(self, output: OwnerAuthoredOutput) -> PutResult:
        self.calls += 1
        return PutResult(
            PutDisposition.CREATED,
            output.output_id,
            sha256(output.canonical_bytes()).hexdigest(),
        )


class LedgerApplySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[LedgerStage, PreparedLedgerApply]] = []

    def apply(self, *, stage: LedgerStage, prepared: PreparedLedgerApply) -> ApplyResult:
        self.calls.append((stage, prepared))
        return ApplyResult(status="applied")


CreationReceiptAttack = Literal["wrong_digest", "wrong_id", "invalid_disposition"]


class NoOpReviewCreationBoundary:
    def __init__(self, attack: CreationReceiptAttack) -> None:
        self.attack = attack
        self.calls = 0

    def create_review(self, proposal: ReviewProposal) -> PutResult:
        self.calls += 1
        payload = canonical_json_bytes(ReviewAggregate.create(proposal).to_dict())
        digest = sha256(payload).hexdigest()
        record_id = str(proposal.review_id)
        disposition = PutDisposition.CREATED
        if self.attack == "wrong_digest":
            digest = "0" * 64
        elif self.attack == "wrong_id":
            record_id = "review_" + "0" * 64
        else:
            disposition = cast(PutDisposition, "created")
        return PutResult(disposition, record_id, digest)


def _capture(*, tier: PrivacyTier = PrivacyTier.WORK, suffix: str = "a") -> CaptureEnvelope:
    source_ref = f"https://example.test/synthetic-{suffix}"
    return CaptureEnvelope.create(
        source_type=SourceType.WEB,
        content_kind=ContentKind.ARTICLE,
        source_url=source_ref,
        title=None,
        shared_text=f"Synthetic third-party content {suffix}",
        captured_at=FIXED_TIME,
        capture_why="Keep this for owner-directed research",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.CLI,
        provenance=Provenance.create(
            source_ref=source_ref,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=classify_privacy(tier, policy_version="policy-v1"),
    )


def _taxonomy(*, route_tier: PrivacyTier | None = PrivacyTier.WORK) -> LedgerTaxonomy:
    return LedgerTaxonomy.create(
        version="synthetic-v1",
        routes=(
            LedgerRoute.create(
                path_prefix=("professional", "research"),
                topic_id="research",
                topic_label="Research",
                privacy_tier=route_tier,
            ),
        ),
    )


def _stage(
    capture: CaptureEnvelope, *, route_tier: PrivacyTier | None = PrivacyTier.WORK
) -> LedgerStage:
    payload = {
        "text": "Synthetic extracted reference",
        "capture_why": capture.capture_why,
        "capture_source": capture.capture_source.value,
        "source_type": capture.source_type.value,
        "content_kind": capture.content_kind.value,
        "provenance": capture.provenance.to_dict(),
    }
    event = EventRecord.create(
        event_id="evt_" + str(capture.capture_id)[4:20],
        stream_id=capture.capture_id,
        event_type="capture.extracted",
        occurred_at=capture.captured_at,
        privacy_decision=capture.privacy_decision,
        payload=payload,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256="b" * 64,
            output_digest_sha256=EventRecord.output_digest_sha256(payload),
            policy_version="redaction-v1",
        ),
    )
    item = DistillationWorkItem.create(
        capture_id=capture.capture_id,
        event_id=event.event_id,
        redacted_event_digest_sha256=sha256(event.canonical_bytes()).hexdigest(),
    )
    record = scan_distillation_work_item(
        item=item,
        event=event,
        taxonomy=_taxonomy(route_tier=route_tier),
        source_locator=PurePosixPath("professional/research/note"),
    )
    return stage_scan_record(record=record, taxonomy=_taxonomy(route_tier=route_tier))


def _ledger_service(tmp_path: Path, stage: LedgerStage) -> tuple[LedgerService, SqliteLedgerStore]:
    store = SqliteLedgerStore(root=tmp_path / "private-ledger")
    markdown_root = tmp_path / "markdown"
    markdown_root.mkdir(mode=0o700)
    citation_id = "cite-synthetic"
    resolver = CaptureCitationResolver(
        citations={
            (str(stage.binding.capture_id), stage.binding.event_id): TrustedCitation.create(
                citation_id=citation_id,
                destination=markdown_relative_path(f"capture_ref_{citation_id}").as_posix(),
            )
        }
    )
    return (
        LedgerService(
            store=store,
            citations=resolver,
            sink=AtomicMarkdownSink(root=markdown_root),
            reader=AtomicMarkdownReader(root=markdown_root),
        ),
        store,
    )


def _prepared(service: LedgerService, stage: LedgerStage) -> PreparedLedgerApply:
    result = sanitize_leaf(
        item_id="synthetic-reference",
        section=LedgerSection.SUMMARY,
        text="Synthetic cited knowledge",
    )
    assert result.leaf is not None
    return service.prepare(stage=stage, section=LedgerSection.SUMMARY, leaf=result.leaf)


@pytest.mark.parametrize("intent", [Intent.IDEA, Intent.ACTION_CANDIDATE])
def test_reviewable_intents_persist_one_open_review_without_ledger_or_output(
    tmp_path: Path, intent: Intent
) -> None:
    capture = _capture(suffix=intent.value)
    ledger = LedgerApplySpy()
    output = OutputSinkSpy()
    database = tmp_path / "reviews.sqlite3"
    with SqliteReviewStore(root=tmp_path, database_name=database.name, clock=FixedClock()) as store:
        reviews = ReviewApplicationService(store=store, output_sink=output, clock=FixedClock())
        router = Phase4IntentRouter(ledger=ledger, reviews=reviews, clock=FixedClock())

        first = router.route(
            capture=capture,
            proposed_intent=intent.value,
            proposal_reason="Synthetic proposal",
        )
        replay = router.route(
            capture=capture,
            proposed_intent=intent.value,
            proposal_reason="Synthetic proposal",
        )

        expected_id = review_id_for(capture.capture_id, intent.value)
        aggregate = store.get(expected_id)
        assert first == replay
        assert first.status is IntentRoutingStatus.REVIEW_OPEN
        assert first.destination is IntentRoutingDestination.REVIEW
        assert first.intent is intent
        assert first.review_id == expected_id
        assert aggregate is not None
        assert aggregate.proposal.state is ReviewState.OPEN
        assert aggregate.events == ()
        assert store.pending_outputs(limit=10) == ()
        assert ledger.calls == []
        assert output.calls == 0

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT count(*) FROM reviews").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize("attack", ["wrong_digest", "wrong_id", "invalid_disposition"])
def test_unbound_review_creation_receipt_cannot_report_review_open(
    attack: CreationReceiptAttack,
) -> None:
    capture = _capture(suffix=attack)
    ledger = LedgerApplySpy()
    reviews = NoOpReviewCreationBoundary(attack)
    router = Phase4IntentRouter(ledger=ledger, reviews=reviews, clock=FixedClock())

    with pytest.raises(IntentRoutingError, match="review persistence failed"):
        router.route(
            capture=capture,
            proposed_intent=Intent.IDEA.value,
            proposal_reason="Synthetic proposal",
        )

    assert reviews.calls == 1
    assert ledger.calls == []


@pytest.mark.parametrize(
    ("capture_tier", "route_tier", "destination"),
    [
        (PrivacyTier.PERSONAL, PrivacyTier.WORK, IntentRoutingDestination.WORK),
        (PrivacyTier.WORK, PrivacyTier.PERSONAL, IntentRoutingDestination.PERSONAL),
    ],
)
def test_reference_destination_comes_from_the_stage_used_by_the_apply_boundary(
    tmp_path: Path,
    capture_tier: PrivacyTier,
    route_tier: PrivacyTier,
    destination: IntentRoutingDestination,
) -> None:
    capture = _capture(tier=capture_tier, suffix=route_tier.value)
    stage = _stage(capture, route_tier=route_tier)
    preparing_service, _ = _ledger_service(tmp_path, stage)
    prepared = _prepared(preparing_service, stage)
    ledger = LedgerApplySpy()
    output = OutputSinkSpy()
    with SqliteReviewStore(
        root=tmp_path, database_name="reviews.sqlite3", clock=FixedClock()
    ) as review_store:
        reviews = ReviewApplicationService(
            store=review_store, output_sink=output, clock=FixedClock()
        )
        router = Phase4IntentRouter(ledger=ledger, reviews=reviews, clock=FixedClock())

        first = router.route(
            capture=capture,
            proposed_intent=Intent.REFERENCE.value,
            proposal_reason="Synthetic reference",
            stage=stage,
            prepared=prepared,
        )
        assert first.status is IntentRoutingStatus.REFERENCE_APPLIED
        assert first.destination is destination
        assert first.intent is Intent.REFERENCE
        assert ledger.calls == [(stage, prepared)]
        assert review_store.pending_outputs(limit=10) == ()
        assert review_store.get(review_id_for(capture.capture_id, Intent.IDEA.value)) is None
        assert output.calls == 0


def test_reference_rejects_route_without_a_native_parity_destination_before_apply(
    tmp_path: Path,
) -> None:
    route_tier = PrivacyTier.PUBLIC
    capture = _capture(suffix=route_tier.value)
    stage = _stage(capture, route_tier=route_tier)
    preparing_service, _ = _ledger_service(tmp_path, stage)
    prepared = _prepared(preparing_service, stage)
    ledger = LedgerApplySpy()
    with SqliteReviewStore(
        root=tmp_path, database_name="reviews.sqlite3", clock=FixedClock()
    ) as store:
        reviews = ReviewApplicationService(
            store=store, output_sink=OutputSinkSpy(), clock=FixedClock()
        )
        router = Phase4IntentRouter(ledger=ledger, reviews=reviews, clock=FixedClock())

        with pytest.raises(IntentRoutingError, match="reference destination unavailable"):
            router.route(
                capture=capture,
                proposed_intent=Intent.REFERENCE.value,
                proposal_reason="Synthetic reference",
                stage=stage,
                prepared=prepared,
            )

    assert ledger.calls == []


def test_work_reference_result_matches_the_persisted_native_ledger_apply(
    tmp_path: Path,
) -> None:
    capture = _capture()
    stage = _stage(capture)
    ledger, ledger_store = _ledger_service(tmp_path, stage)
    prepared = _prepared(ledger, stage)
    with SqliteReviewStore(
        root=tmp_path, database_name="reviews.sqlite3", clock=FixedClock()
    ) as review_store:
        reviews = ReviewApplicationService(
            store=review_store, output_sink=OutputSinkSpy(), clock=FixedClock()
        )
        result = Phase4IntentRouter(
            ledger=ledger, reviews=reviews, clock=FixedClock()
        ).route(
            capture=capture,
            proposed_intent=Intent.REFERENCE.value,
            proposal_reason="Synthetic reference",
            stage=stage,
            prepared=prepared,
        )

    assert result.destination is IntentRoutingDestination.WORK
    assert ledger_store.record_count() == 1
    assert ledger_store.published_document_set(stage.stage_digest_sha256) is not None


@pytest.mark.parametrize(
    ("tier", "proposal", "reason"),
    [
        (PrivacyTier.WORK, Intent.HOLD.value, IntentPolicyReason.PROPOSAL_ACCEPTED),
        (PrivacyTier.SECRET, Intent.REFERENCE.value, IntentPolicyReason.PRIVACY_HOLD),
        (PrivacyTier.WORK, "invalid", IntentPolicyReason.INVALID_PROPOSAL),
        (PrivacyTier.WORK, None, IntentPolicyReason.INVALID_PROPOSAL),
    ],
)
def test_hold_routes_call_no_ledger_review_or_output_seam(
    tmp_path: Path,
    tier: PrivacyTier,
    proposal: str | None,
    reason: IntentPolicyReason,
) -> None:
    capture = _capture(tier=tier, suffix=reason.value)
    ledger = LedgerApplySpy()
    output = OutputSinkSpy()
    with SqliteReviewStore(
        root=tmp_path, database_name="reviews.sqlite3", clock=FixedClock()
    ) as store:
        reviews = ReviewApplicationService(store=store, output_sink=output, clock=FixedClock())
        router = Phase4IntentRouter(ledger=ledger, reviews=reviews, clock=FixedClock())

        result = router.route(
            capture=capture,
            proposed_intent=proposal,
            proposal_reason="Synthetic proposal",
        )

        assert result.status is IntentRoutingStatus.HELD
        assert result.destination is IntentRoutingDestination.HOLD
        assert result.intent is Intent.HOLD
        assert result.reason is reason
        assert result.review_id is None
        assert ledger.calls == []
        assert store.get(review_id_for(capture.capture_id, Intent.IDEA.value)) is None
        assert store.pending_outputs(limit=10) == ()
        assert output.calls == 0


@pytest.mark.parametrize("missing", ["stage", "prepared"])
def test_reference_requires_both_stage_and_prepared_item(tmp_path: Path, missing: str) -> None:
    capture = _capture()
    stage = _stage(capture)
    preparing_service, _ = _ledger_service(tmp_path, stage)
    prepared = _prepared(preparing_service, stage)
    ledger = LedgerApplySpy()
    output = OutputSinkSpy()
    with SqliteReviewStore(
        root=tmp_path, database_name="reviews.sqlite3", clock=FixedClock()
    ) as store:
        reviews = ReviewApplicationService(store=store, output_sink=output, clock=FixedClock())
        router = Phase4IntentRouter(ledger=ledger, reviews=reviews, clock=FixedClock())

        with pytest.raises(IntentRoutingError, match="capture ledger binding mismatch"):
            router.route(
                capture=capture,
                proposed_intent=Intent.REFERENCE.value,
                proposal_reason="Synthetic reference",
                stage=None if missing == "stage" else stage,
                prepared=None if missing == "prepared" else prepared,
            )

        assert ledger.calls == []
        assert store.pending_outputs(limit=10) == ()
        assert output.calls == 0


def test_reference_rejects_stage_bound_to_another_capture_before_apply(tmp_path: Path) -> None:
    capture = _capture(suffix="target")
    other = _capture(suffix="other")
    stage = _stage(other)
    preparing_service, _ = _ledger_service(tmp_path, stage)
    prepared = _prepared(preparing_service, stage)
    ledger = LedgerApplySpy()
    output = OutputSinkSpy()
    with SqliteReviewStore(
        root=tmp_path, database_name="reviews.sqlite3", clock=FixedClock()
    ) as store:
        reviews = ReviewApplicationService(store=store, output_sink=output, clock=FixedClock())
        router = Phase4IntentRouter(ledger=ledger, reviews=reviews, clock=FixedClock())

        with pytest.raises(IntentRoutingError, match="capture ledger binding mismatch"):
            router.route(
                capture=capture,
                proposed_intent=Intent.REFERENCE.value,
                proposal_reason="Synthetic reference",
                stage=stage,
                prepared=prepared,
            )

        assert ledger.calls == []
        assert store.pending_outputs(limit=10) == ()
        assert output.calls == 0
