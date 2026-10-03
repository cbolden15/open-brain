from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.engine import (
    CaptureFault,
    InjectedFault,
    PublicJobRevisionSink,
    SourceRevisionDelivery,
    SourceRevisionDeliveryReceipt,
    open_local_engine,
)
from open_brain_engine.engine.capture import CaptureTasks
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingError,
    SharingInspectRequest,
    SharingPreviewRequest,
    SharingRevokeRequest,
)
from open_brain_engine.engine.source_lifecycle_contracts import (
    SourceInspectRequest,
    SourceWithdrawRequest,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, RecordReadRequest, T03Error
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from open_brain_connectors.runtime.live_common import LiveSourceError
from packages.app.tests.integration.engine.test_sharing_surfaces import _external
from packages.collector.tests.integration.test_saved_markdown import _adapter
from packages.collector.tests.integration.test_saved_markdown_publication import (
    _approve_current,
    _collector,
    _terminal_snapshot,
)


def test_normalization_upgrade_preserves_pending_v1_and_requires_new_v2_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "saved.md").write_text("# Upgrade source\nSame retained body\n", encoding="utf-8")
    now = [100]
    tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    candidate, = _adapter(selected).dry_run().candidates
    assert candidate.identity is not None and candidate.intake is not None
    current = candidate.intake
    assert current.observation is not None
    legacy_identity = replace(
        candidate.identity, normalization_version="saved-markdown-continuous.v1",
    )
    legacy = replace(
        current, key=replace(current.key, revision_id=legacy_identity.revision_id),
        observation=replace(
            current.observation, normalization_version=legacy_identity.normalization_version,
        ),
    )
    assert legacy_identity.item_id == candidate.identity.item_id
    assert legacy_identity.revision_id != candidate.identity.revision_id
    assert legacy_identity.delivery_id != candidate.identity.delivery_id
    submit = PublicJobRevisionSink.submit

    def lost_response(
        self: PublicJobRevisionSink, delivery: SourceRevisionDelivery,
    ) -> SourceRevisionDeliveryReceipt:
        submit(self, delivery)
        raise RuntimeError("synthetic v1 lost response")

    with monkeypatch.context() as patch:
        patch.setattr(PublicJobRevisionSink, "submit", lost_response)
        with pytest.raises(RuntimeError, match="synthetic v1 lost response"):
            sink.submit(legacy)
    before = _terminal_snapshot(tasks)
    assert len(before) == 1
    old_envelope = json.loads(cast(bytes, before[0][1]))
    assert (
        old_envelope["observation"]["normalization_version"]
        == legacy_identity.normalization_version
    )
    tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    with pytest.raises(LiveSourceError, match="collector_custody_stale"):
        sink.submit(current)
    assert _terminal_snapshot(tasks) == before
    old_receipt = sink.submit(legacy)
    assert old_receipt.source_receipt is not None
    assert _terminal_snapshot(tasks)[0][1] == before[0][1]
    source_id = old_receipt.source_receipt.source_id
    assert source_id is not None
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    old_copy = _approve_current(tasks, source_id, owner, "normalization-v1")
    assert old_copy.copy_capture_id is not None and tasks.sources is not None
    source = tasks.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    external = _external(source.destination_brain_id, source.issuer_epoch, "openai")
    old_read = RecordReadRequest(
        record_id=old_copy.copy_capture_id, expected_revision_id=old_copy.copy_capture_id,
    )
    assert tasks.retrieval.read_record(old_read, authority=external).to_wire()["content"]
    controller.enable(
        source_id="synthetic-publication", selection=_adapter(selected).selection,
        interval_seconds=60,
    )
    assert controller.sync_due(
        source_id="synthetic-publication", runtime=runtime, capture_sink=sink,
    ).captured_count == 1
    advanced = tasks.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    assert advanced.head_capture_id != source.head_capture_id
    with pytest.raises(T03Error, match="not_found"):
        tasks.retrieval.read_record(old_read, authority=external)
    with pytest.raises(T03Error, match="not_found"):
        tasks.retrieval.read_record(
            RecordReadRequest(
                record_id=advanced.head_capture_id, expected_revision_id=advanced.head_capture_id,
            ),
            authority=external,
        )
    assert tasks.history is not None
    assert tasks.history.read_history(old_read, authority=owner).to_wire()["content"]
    new_copy = _approve_current(tasks, source_id, owner, "normalization-v2")
    assert new_copy.copy_capture_id is not None
    assert new_copy.copy_capture_id != old_copy.copy_capture_id
    assert tasks.retrieval.read_record(
        RecordReadRequest(
            record_id=new_copy.copy_capture_id, expected_revision_id=new_copy.copy_capture_id,
        ),
        authority=external,
    ).to_wire()["content"]


@pytest.mark.parametrize("failure", ["journal_commit", "index_update", "lost_response"])
def test_collector_uncertain_source_preserves_exact_envelope_and_portable_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    path = selected / "saved.md"
    path.write_text("# Frozen collector source\nExact synthetic body 漢字\n", encoding="utf-8")
    now = [100]
    tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    assert isinstance(tasks.capture, CaptureTasks)
    engine = tasks.capture._engine
    controller.enable(
        source_id="synthetic-publication", selection=_adapter(selected).selection,
        interval_seconds=60,
    )
    submit = PublicJobRevisionSink.submit
    submitted: list[bytes] = []

    def uncertain(
        self: PublicJobRevisionSink, delivery: SourceRevisionDelivery
    ) -> SourceRevisionDeliveryReceipt:
        submitted.append(delivery.custody_bytes())
        if len(submitted) == 1 and failure != "lost_response":
            engine._faults.add(
                CaptureFault.AFTER_JOURNAL_COMMIT
                if failure == "journal_commit" else CaptureFault.AFTER_INDEX_UPDATE
            )
        result = submit(self, delivery)
        if len(submitted) == 1 and failure == "lost_response":
            raise RuntimeError("synthetic collector lost terminal response")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(PublicJobRevisionSink, "submit", uncertain)
        if failure == "lost_response":
            with pytest.raises(RuntimeError, match="synthetic collector lost terminal response"):
                controller.sync_due(
                    source_id="synthetic-publication", runtime=runtime, capture_sink=sink
                )
        else:
            try:
                result = controller.sync_due(
                    source_id="synthetic-publication", runtime=runtime, capture_sink=sink
                )
            except InjectedFault:
                pass
            else:
                assert result.outcome.value == "deferred"
        assert len(submitted) == 1
        assert controller.custody_status("synthetic-publication")["retained_items"] == 1
        before = _terminal_snapshot(tasks)
        assert len(before) == 1 and bytes(cast(bytes, before[0][1])) == submitted[0]
        if failure != "lost_response":
            refused = tmp_path / "pending-export"
            with pytest.raises(ValueError, match="ingestion_pending"):
                tasks.portability.export(refused, export_id="export_" + str(uuid4()))
            assert not refused.exists()
        else:
            # Source admission completed. Collector still retains uncertain sender custody.
            assert before[0][2] is not None
        path.write_text("# Newer filesystem bytes\nNot the pending source\n", encoding="utf-8")
        tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
        recovered = controller.sync_due(
            source_id="synthetic-publication", runtime=runtime, capture_sink=sink
        )
        assert recovered.outcome.value == "completed"
        assert len(submitted) == 2 and submitted[0] == submitted[1]
        assert controller.custody_status("synthetic-publication")["retained_items"] == 1
    terminal = _terminal_snapshot(tasks)
    assert len(terminal) == 1 and terminal[0][1] == before[0][1]
    source_receipt = json.loads(cast(str, terminal[0][2]))["source_receipt"]
    capture_id = source_receipt["capture_id"]
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    export, imported, again = (tmp_path / name for name in ("export", "imported", "again"))
    assert tasks.portability.export(export, export_id="export_" + str(uuid4())).schema_version == 8
    first = validated_portable_snapshot(export)
    import_id = "import_" + str(uuid4())
    assert tasks.portability.import_clean(export, imported, import_id=import_id).schema_version == 8
    assert tasks.portability.import_clean(export, imported, import_id=import_id).duplicate
    reopened = open_local_engine(compile_single_user_local(imported))
    reopened.portability.rebuild_index()
    assert reopened.history is not None
    read = reopened.history.read_history(
        RecordReadRequest(record_id=capture_id, expected_revision_id=capture_id), authority=owner
    ).to_wire()
    assert "Exact synthetic body 漢字" in cast(
        str, cast(dict[str, object], read["content"])["text"]
    )
    with open_local_database_read_only(reopened.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM managed_consents").fetchone()[0] == 0
    reopened.portability.export(again, export_id="export_" + str(uuid4()))
    second = validated_portable_snapshot(again)
    assert {
        name: value for name, value in first.files.items() if name != "portable-manifest.json"
    } == {
        name: value for name, value in second.files.items() if name != "portable-manifest.json"
    }


@pytest.mark.parametrize(
    "state", ["active", "superseded", "withdrawn", "revoked", "rejected", "undecided"]
)
def test_collector_copy_custody_lifecycle_portable_roundtrip(
    tmp_path: Path, state: str
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    path = selected / "saved.md"
    text = "# Collector retained publication\nExact copy body 漢字\n"
    path.write_text(
        text + "## Why Saved\nowner-before-break\n\n---\nowner-after-break\n"
        "```\n```\u00a0\n## Still fenced\nowner-in-fence\n```\nowner-after-fence\n"
        "## [Why Saved][owner]\nowner-reference\n\n[owner]: https://example.test\n"
        "## <em>Why Saved</em>\nowner-wrapper\n"
        "## Why<!-- label --> Saved\nowner-comment\n"
        "## Why<br>Saved\nowner-break\n"
        "## ![**Why Saved**](https://example.test/image)\nowner-image-alt\n",
        encoding="utf-8",
    )
    now = [100]
    tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    controller.enable(
        source_id="synthetic-publication", selection=_adapter(selected).selection,
        interval_seconds=60,
    )
    assert controller.sync_due(
        source_id="synthetic-publication", runtime=runtime, capture_sink=sink
    ).captured_count == 1
    source_id = json.loads(cast(str, _terminal_snapshot(tasks)[0][2]))[
        "source_receipt"
    ]["source_id"]
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    assert tasks.sources is not None and tasks.sharing is not None
    source = tasks.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    request = SharingPreviewRequest(
        operation_id="sharing.preview.collector.primary",
        source_id=source_id,
        expected_head=source.head_capture_id,
        expected_head_version=source.head_version,
        expected_lifecycle_version=source.lifecycle_version,
        expected_route_version=source.route_version,
        brain_id=source.destination_brain_id,
        issuer_epoch=source.issuer_epoch,
        provider_ids=("openai",),
    )
    preview = tasks.sharing.preview(request, authority=owner)
    assert preview.text == text
    assert "owner-" not in preview.text
    undecided = tasks.sharing.preview(
        replace(request, operation_id="sharing.preview.collector.undecided"), authority=owner
    )
    rejected_preview = tasks.sharing.preview(
        replace(request, operation_id="sharing.preview.collector.rejected"), authority=owner
    )
    decision = SharingDecisionRequest(
        operation_id="sharing.approve.collector.primary",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=source.destination_brain_id,
        issuer_epoch=source.issuer_epoch,
        destination_brain_id=source.destination_brain_id,
        expected_decision_version=0,
        decision="reject" if state == "rejected" else "approve",
    )
    rejected_request = replace(
        decision,
        operation_id="sharing.reject.collector.secondary",
        preview_id=rejected_preview.preview_id,
        preview_sha256=rejected_preview.preview_sha256,
        decision="reject",
    )
    rejected = tasks.sharing.decide(rejected_request, authority=owner)
    assert rejected.copy_capture_id is None
    receipt = None
    frozen = None
    revoked_request = None
    revoked = None
    if state != "undecided":
        if state != "rejected":
            assert isinstance(tasks.capture, CaptureTasks)
            tasks.capture._engine._faults.add(CaptureFault.AFTER_INDEX_UPDATE)
        receipt = tasks.sharing.decide(decision, authority=owner)
        if state != "rejected":
            assert receipt.state == "pending" and receipt.copy_capture_id is None
            with open_local_database_read_only(tasks.profile) as connection:
                frozen = tuple(connection.execute(
                    "SELECT copy_delivery_id,copy_submission_sha256,copy_submission_bytes "
                    "FROM sharing_decisions WHERE approval_id=?", (receipt.approval_id,)
                ).fetchone())
            refused = tmp_path / "pending-copy-export"
            with pytest.raises(ValueError, match="ingestion_pending"):
                tasks.portability.export(refused, export_id="export_" + str(uuid4()))
            assert not refused.exists()
    if state == "superseded":
        path.write_text("# New collector version\nStill local only\n", encoding="utf-8")
        now[0] = 160
        assert controller.sync_due(
            source_id="synthetic-publication", runtime=runtime, capture_sink=sink
        ).captured_count == 1
    elif state == "withdrawn":
        tasks.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.collector.pending-copy",
                source_id=source_id,
                expected_head=source.head_capture_id,
                expected_lifecycle_version=source.lifecycle_version,
                brain_id=source.destination_brain_id,
                issuer_epoch=source.issuer_epoch,
                reason_code="owner_choice",
            ), authority=owner,
        )
    elif state == "revoked":
        assert receipt is not None
        revoked_request = SharingRevokeRequest(
            operation_id="sharing.revoke.collector.pending-copy",
            approval_id=receipt.approval_id,
            expected_approval_version=1,
            brain_id=source.destination_brain_id,
            issuer_epoch=source.issuer_epoch,
            destination_brain_id=source.destination_brain_id,
            reason="owner_choice",
        )
        revoked = tasks.sharing.revoke(revoked_request, authority=owner)
    # Actual startup reconciles the retained copy, never a newly inferred approval.
    tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    assert tasks.sharing is not None
    if receipt is not None:
        receipt = tasks.sharing.decide(decision, authority=owner)
        assert tasks.sharing.decide(decision, authority=owner) == receipt
        if frozen is not None:
            with open_local_database_read_only(tasks.profile) as connection:
                assert tuple(connection.execute(
                    "SELECT copy_delivery_id,copy_submission_sha256,copy_submission_bytes "
                    "FROM sharing_decisions WHERE approval_id=?", (receipt.approval_id,)
                ).fetchone()) == frozen
            assert receipt.copy_capture_id is not None
            assert receipt.state == ("captured" if state == "active" else "history_only")
    export, imported, again = (tmp_path / name for name in ("export", "imported", "again"))
    assert tasks.portability.export(export, export_id="export_" + str(uuid4())).schema_version == 8
    first = validated_portable_snapshot(export)
    import_id = "import_" + str(uuid4())
    tasks.portability.import_clean(export, imported, import_id=import_id)
    assert tasks.portability.import_clean(export, imported, import_id=import_id).duplicate
    reopened = open_local_engine(compile_single_user_local(imported))
    reopened.portability.rebuild_index()
    assert reopened.sharing is not None and reopened.history is not None
    assert reopened.sharing.decide(rejected_request, authority=owner) == rejected
    if receipt is not None:
        assert reopened.sharing.decide(decision, authority=owner) == receipt
    if revoked_request is not None:
        assert reopened.sharing.revoke(revoked_request, authority=owner) == revoked
    retained_preview = reopened.sharing.inspect(
        SharingInspectRequest(subject_id=undecided.preview_id), authority=owner
    )
    assert retained_preview.preview.text == preview.text and retained_preview.decision is None
    with pytest.raises(SharingError, match="preview_expired"):
        reopened.sharing.decide(
            replace(
                decision, operation_id="sharing.approve.imported-undecided",
                preview_id=undecided.preview_id, preview_sha256=undecided.preview_sha256,
            ), authority=owner,
        )
    external = _external(source.destination_brain_id, source.issuer_epoch, "openai")
    originals = [source.head_capture_id]
    if state == "superseded":
        assert reopened.sources is not None
        originals.append(reopened.sources.inspect(
            SourceInspectRequest(source_id=source_id), authority=owner
        ).head_capture_id)
    for original in originals:
        read = reopened.history.read_history(
            RecordReadRequest(record_id=original, expected_revision_id=original), authority=owner
        ).to_wire()
        assert cast(dict[str, object], read["content"])["text"]
    if receipt is not None and receipt.copy_capture_id is not None:
        reading = RecordReadRequest(
            record_id=receipt.copy_capture_id, expected_revision_id=receipt.copy_capture_id
        )
        history = reopened.history.read_history(reading, authority=owner).to_wire()
        assert cast(dict[str, object], history["content"])["text"] == preview.text
        if state == "active":
            allowed = reopened.retrieval.read_record(reading, authority=external).to_wire()
            assert cast(dict[str, object], allowed["content"])["text"] == preview.text
        else:
            with pytest.raises(T03Error, match="not_found"):
                reopened.retrieval.read_record(reading, authority=external)
    with open_local_database_read_only(reopened.profile) as connection:
        assert connection.execute("SELECT count(*) FROM managed_consents").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == int(
            state not in {"rejected", "undecided"}
        )
    reopened.portability.export(again, export_id="export_" + str(uuid4()))
    second = validated_portable_snapshot(again)
    assert {
        name: value for name, value in first.files.items() if name != "portable-manifest.json"
    } == {
        name: value for name, value in second.files.items() if name != "portable-manifest.json"
    }
