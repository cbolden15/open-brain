from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.core.ports import EventRecord, RedactionReceipt

from open_brain_legacy.ledger.models import LedgerScanRecord, LedgerValidationError
from open_brain_legacy.ledger.scan import scan_distillation_work_item, scan_source_root
from open_brain_legacy.ledger.stage import StageDisposition, stage_manifest_entry, stage_scan_record

from .test_scan import _event, _item, _taxonomy


def _record(
    *,
    text: str = "synthetic extracted text",
    transcript: str | None = None,
    event_id: str = "evt_synthetic",
    source_locator: PurePosixPath | None = None,
) -> LedgerScanRecord:
    base_event = _event(transcript=transcript)
    payload = {**base_event.payload, "text": text}
    event = EventRecord.create(
        event_id=event_id,
        stream_id=base_event.stream_id,
        event_type=base_event.event_type,
        occurred_at=base_event.occurred_at,
        privacy_decision=base_event.privacy_decision,
        payload=payload,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256=base_event.redaction_receipt.source_digest_sha256,
            output_digest_sha256=EventRecord.output_digest_sha256(payload),
            policy_version=base_event.redaction_receipt.policy_version,
            findings=base_event.redaction_receipt.findings,
        ),
    )
    return scan_distillation_work_item(
        item=_item(event),
        event=event,
        taxonomy=_taxonomy(),
        source_locator=source_locator or PurePosixPath("professional/research/note"),
    )


def test_stage_is_transcript_free_and_binds_a_second_projection_receipt() -> None:
    transcript_canary = "TRANSCRIPT_CANARY_MUST_NOT_REACH_LEDGER"
    stage = stage_scan_record(record=_record(transcript=transcript_canary), taxonomy=_taxonomy())

    assert transcript_canary not in stage.canonical_bytes().decode("utf-8")
    assert transcript_canary not in stage.prompt_context().decode("utf-8")
    assert stage.redaction_receipt.source_digest_sha256 == stage.binding.event_digest_sha256
    assert stage.redaction_receipt.output_digest_sha256 == stage.projection_digest_sha256
    assert stage.upstream_redaction_receipt == stage.binding.upstream_redaction_receipt


def test_each_concurrent_record_gets_its_own_immutable_stage() -> None:
    first_canary = "FIRST_RECORD_TEXT_CANARY"
    second_canary = "SECOND_RECORD_TEXT_CANARY"
    first = _record(text=first_canary, event_id="evt_first")
    second = _record(
        text=second_canary,
        event_id="evt_second",
        source_locator=PurePosixPath("professional/note-two"),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stages = tuple(
            pool.map(
                lambda item: stage_scan_record(record=item, taxonomy=_taxonomy()), (first, second)
            )
        )

    assert stages[0].binding.record_id != stages[1].binding.record_id
    first_stage = stages[0].canonical_bytes().decode("utf-8")
    second_stage = stages[1].canonical_bytes().decode("utf-8")
    first_context = stages[0].prompt_context().decode("utf-8")
    second_context = stages[1].prompt_context().decode("utf-8")
    assert first_canary in first_stage
    assert first_canary in first_context
    assert first_canary not in second_stage
    assert first_canary not in second_context
    assert second_canary in second_stage
    assert second_canary in second_context
    assert second_canary not in first_stage
    assert second_canary not in first_context
    assert str(stages[0].binding.source_locator) not in first_context
    assert str(stages[1].binding.source_locator) not in second_context
    assert stages[0].binding.provenance.source_ref not in first_context
    assert stages[1].binding.provenance.source_ref not in second_context


def test_stage_rejects_mutated_record_taxonomy_or_stage_digest() -> None:
    record = _record()
    with pytest.raises(LedgerValidationError):
        stage_scan_record(record=replace(record, taxonomy_version="changed"), taxonomy=_taxonomy())

    stage = stage_scan_record(record=record, taxonomy=_taxonomy())
    with pytest.raises(LedgerValidationError):
        replace(stage, staged_text="changed").prompt_context()


def test_manifest_stage_is_receipt_bound_transcript_free_and_idempotent(tmp_path: Path) -> None:
    transcript_canary = "TRANSCRIPT_CANARY_MUST_NOT_REACH_SCRATCH"
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "page.md").write_text(
        "Synthetic body\n"
        "<!-- open-brain:transcript -->\n"
        f"{transcript_canary}\n"
        "<!-- /open-brain:transcript -->\n"
    )
    manifest = scan_source_root(root=source_root)
    scratch_root = tmp_path / "scratch"

    first = stage_manifest_entry(
        manifest=manifest,
        key=manifest.entries[0].key,
        source_root=source_root,
        scratch_root=scratch_root,
    )
    second = stage_manifest_entry(
        manifest=manifest,
        key=manifest.entries[0].key,
        source_root=source_root,
        scratch_root=scratch_root,
    )

    assert first == second
    assert first.disposition is StageDisposition.STAGED
    assert first.relative_path is not None
    staged = (scratch_root / first.relative_path).read_text()
    assert "Synthetic body" in staged
    assert transcript_canary not in staged
    assert manifest.manifest_id in staged
    assert manifest.entries[0].content_digest_sha256 in staged


def test_manifest_stage_reports_missing_key_without_writing(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "page.md").write_text("Synthetic body\n")
    scratch_root = tmp_path / "scratch"

    result = stage_manifest_entry(
        manifest=scan_source_root(root=source_root),
        key="source_" + "0" * 64,
        source_root=source_root,
        scratch_root=scratch_root,
    )

    assert result.disposition is StageDisposition.MISSING
    assert result.relative_path is None
    assert not scratch_root.exists()
