from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.capture.models import DistillationWorkItem
from open_brain_engine.core.models import (
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.core.policy import classify_privacy
from open_brain_engine.core.ports import EventRecord, RedactionReceipt

from open_brain_legacy.ledger.models import LedgerRoute, LedgerTaxonomy, LedgerValidationError
from open_brain_legacy.ledger.scan import scan_distillation_work_item, scan_source_root


def _taxonomy() -> LedgerTaxonomy:
    return LedgerTaxonomy.create(
        version="synthetic-v1",
        routes=(
            LedgerRoute.create(
                path_prefix=("professional",),
                topic_id="work-notes",
                topic_label="Work notes",
                privacy_tier=PrivacyTier.WORK,
            ),
            LedgerRoute.create(
                path_prefix=("professional", "research"),
                topic_id="research",
                topic_label="Research",
                privacy_tier=PrivacyTier.WORK,
            ),
        ),
    )


def _event(
    *, transcript: str | None = None, origin: ContentOrigin = ContentOrigin.THIRD_PARTY
) -> EventRecord:
    payload = {
        "text": "synthetic extracted text",
        "transcript": transcript,
        "capture_why": "Keep this for the research backlog",
        "capture_source": CaptureSource.CLI.value,
        "source_type": SourceType.WEB.value,
        "content_kind": ContentKind.ARTICLE.value,
        "provenance": {
            "source_ref": "https://example.test/synthetic-source",
            "content_origin": origin.value,
            "owner_context": CaptureWhyOrigin.OWNER_AUTHORED.value,
        },
    }
    output_digest = EventRecord.output_digest_sha256(payload)
    return EventRecord.create(
        event_id="evt_synthetic",
        stream_id="cap_" + "a" * 64,
        event_type="capture.extracted",
        occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
        privacy_decision=classify_privacy(PrivacyTier.WORK, policy_version="policy-v1"),
        payload=payload,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256="b" * 64,
            output_digest_sha256=output_digest,
            policy_version="redaction-v1",
        ),
    )


def _item(event: EventRecord) -> DistillationWorkItem:
    return DistillationWorkItem.create(
        capture_id=event.stream_id,
        event_id=event.event_id,
        redacted_event_digest_sha256=sha256(event.canonical_bytes()).hexdigest(),
    )


ScanBindingMutation = Callable[
    [DistillationWorkItem, EventRecord], tuple[DistillationWorkItem, EventRecord]
]


def _mutate_canonical_event_digest(
    item: DistillationWorkItem, event: EventRecord
) -> tuple[DistillationWorkItem, EventRecord]:
    return item, replace(event, occurred_at=event.occurred_at + timedelta(seconds=1))


def _mutate_payload_receipt_binding(
    item: DistillationWorkItem, event: EventRecord
) -> tuple[DistillationWorkItem, EventRecord]:
    mutated_event = replace(event, payload={**event.payload, "text": "mutated synthetic text"})
    rebound_item = replace(
        item,
        redacted_event_digest_sha256=sha256(mutated_event.canonical_bytes()).hexdigest(),
    )
    return rebound_item, mutated_event


def test_scan_uses_the_trusted_longest_prefix_not_model_text() -> None:
    event = _event()

    record = scan_distillation_work_item(
        item=_item(event),
        event=event,
        taxonomy=_taxonomy(),
        source_locator=PurePosixPath("professional/research/note"),
    )

    assert record.topic_id == "research"
    assert record.topic_label == "Research"
    assert record.source_locator == PurePosixPath("professional/research/note")


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda item, event: (replace(item, schema_version=2), event),
            id="work-item-schema-version",
        ),
        pytest.param(
            lambda item, event: (replace(item, capture_id="cap_" + "c" * 64), event),
            id="work-item-capture-id",
        ),
        pytest.param(
            lambda item, event: (replace(item, event_id="evt_changed"), event),
            id="work-item-event-id",
        ),
        pytest.param(
            lambda item, event: (
                replace(item, redacted_event_digest_sha256="d" * 64),
                event,
            ),
            id="work-item-event-digest",
        ),
        pytest.param(_mutate_canonical_event_digest, id="canonical-event-digest"),
        pytest.param(_mutate_payload_receipt_binding, id="payload-receipt-binding"),
        pytest.param(
            lambda item, event: (
                item,
                replace(
                    event,
                    redaction_receipt=RedactionReceipt.create(
                        source_digest_sha256="c" * 64,
                        output_digest_sha256=event.redaction_receipt.output_digest_sha256,
                        policy_version="redaction-v1",
                    ),
                ),
            ),
            id="upstream-receipt",
        ),
    ],
)
def test_scan_rejects_each_compound_binding_mutation(mutation: ScanBindingMutation) -> None:
    event = _event()
    item, mutated_event = mutation(_item(event), event)

    with pytest.raises(LedgerValidationError):
        scan_distillation_work_item(
            item=item,
            event=mutated_event,
            taxonomy=_taxonomy(),
            source_locator=PurePosixPath("professional/research/note"),
        )


def test_scan_retains_normalized_provenance_in_its_canonical_binding() -> None:
    event = _event()

    record = scan_distillation_work_item(
        item=_item(event),
        event=event,
        taxonomy=_taxonomy(),
        source_locator=PurePosixPath("professional/research/note"),
    )

    assert record.capture_why == "Keep this for the research backlog"
    assert record.capture_source is CaptureSource.CLI
    assert record.source_type is SourceType.WEB
    assert record.content_kind is ContentKind.ARTICLE
    assert record.provenance == Provenance.create(
        source_ref="https://example.test/synthetic-source",
        content_origin=ContentOrigin.THIRD_PARTY,
        owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
    )
    with pytest.raises(LedgerValidationError):
        replace(record, capture_why="different").validate()


def test_unknown_and_ineligible_routes_hold_without_a_topic() -> None:
    event = _event(origin=ContentOrigin.OWNER_AUTHORED)
    unmatched = scan_distillation_work_item(
        item=_item(event),
        event=event,
        taxonomy=_taxonomy(),
        source_locator=PurePosixPath("unmatched/note"),
    )
    ineligible = scan_distillation_work_item(
        item=_item(event),
        event=event,
        taxonomy=_taxonomy(),
        source_locator=PurePosixPath("professional/research/note"),
    )

    assert unmatched.topic_id is None
    assert unmatched.topic_label is None
    assert unmatched.privacy_decision.tier.value == "unknown"
    assert unmatched.privacy_decision.authority.cloud is False
    assert unmatched.privacy_decision.authority.external_egress is False
    assert ineligible.topic_id is None
    assert ineligible.topic_label is None


@pytest.mark.parametrize(
    "path",
    [
        PurePosixPath("/absolute/path"),
        PurePosixPath("professional/../escape"),
        PurePosixPath("."),
    ],
)
def test_scan_rejects_unsafe_trusted_paths(path: PurePosixPath) -> None:
    event = _event()

    with pytest.raises(LedgerValidationError):
        scan_distillation_work_item(
            item=_item(event), event=event, taxonomy=_taxonomy(), source_locator=path
        )


def test_source_root_scan_is_confined_sorted_and_stably_bound(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "topic").mkdir()
    (source_root / "topic" / "second.md").write_text("Second synthetic page\n")
    (source_root / "first.md").write_text("First synthetic page\n")
    (source_root / "ignored.txt").write_text("Not a ledger source\n")

    first = scan_source_root(root=source_root)
    second = scan_source_root(root=source_root)

    assert first == second
    assert first.manifest_id == "manifest_" + first.manifest_digest_sha256
    assert [entry.source_locator.as_posix() for entry in first.entries] == [
        "first.md",
        "topic/second.md",
    ]
    assert len({entry.key for entry in first.entries}) == 2
    first.validate()


def test_source_root_scan_rejects_symlink_escape(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Synthetic outside page\n")
    (source_root / "escape.md").symlink_to(outside)

    with pytest.raises(LedgerValidationError, match="confined"):
        scan_source_root(root=source_root)
