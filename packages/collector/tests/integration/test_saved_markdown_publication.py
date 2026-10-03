from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine.contracts import EngineTaskSet
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionReceipt,
    SharingDecisionRequest,
    SharingPreviewRequest,
    SharingRevokeRequest,
)
from open_brain_engine.engine.source_lifecycle_contracts import (
    SourceInspectRequest,
    SourceWithdrawRequest,
)
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    T03Error,
)

from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorStateStore,
    EngineRevisionSink,
)
from open_brain_collector.saved_markdown import SavedMarkdownCollectorRuntime
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_markdown import SavedMarkdownRootAdapter
from packages.app.tests.integration.engine.test_sharing_surfaces import _external
from packages.collector.tests.integration.test_saved_markdown import _adapter, _revision_sink


def _collector(
    tmp_path: Path, selected: Path, now: list[int]
) -> tuple[EngineTaskSet, EngineRevisionSink, SavedMarkdownCollectorRuntime, CollectorController]:
    tasks, sink = _revision_sink(tmp_path)
    runtime = SavedMarkdownCollectorRuntime(
        _adapter(selected), store=PrivateJsonStore(tmp_path / "scan")
    )
    controller = CollectorController(
        CollectorStateStore(tmp_path / "state.json"),
        clock=lambda: now[0],
        brain_root=tmp_path / "brain",
    )
    return tasks, sink, runtime, controller


def _terminal_snapshot(tasks: EngineTaskSet) -> tuple[tuple[object, ...], ...]:
    with open_local_database_read_only(tasks.profile) as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT delivery_id,envelope_bytes,receipt_json "
                "FROM managed_source_deliveries ORDER BY delivery_id"
            )
        )


def test_publication_collects_sixty_files_through_restart_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    for index in range(60):
        (selected / f"item-{index:03}.md").write_text(
            f"---\nowner: synthetic\n---\n# Publication item {index}\n"
            f"Full synthetic body {index} 漢字\n\n# Why Saved\nprivate motivation {index}\n",
            encoding="utf-8",
        )
    original = SavedMarkdownRootAdapter._candidate
    visited: list[str] = []

    def observe(
        self: SavedMarkdownRootAdapter, root: Path, path: Path, relative: str
    ) -> object:
        visited.append(relative)
        return original(self, root, path, relative)

    monkeypatch.setattr(SavedMarkdownRootAdapter, "_candidate", observe)
    now = [100]
    tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    controller.enable(
        source_id="synthetic-publication",
        selection=_adapter(selected).selection,
        interval_seconds=60,
    )
    for page_index, expected_count in enumerate((25, 25, 10)):
        now[0] = 100 + page_index * 60
        result = controller.sync_due(
            source_id="synthetic-publication", runtime=runtime, capture_sink=sink
        )
        assert result.outcome.value == "completed"
        assert result.captured_count == expected_count
        assert result.duplicate_count == result.quarantined_count == 0
        assert (result.next_cursor is None) is (page_index == 2)
        assert result.next_run_epoch == now[0] + 60
        assert controller.custody_status("synthetic-publication")["retained_items"] == 0
        state = CollectorStateStore(tmp_path / "state.json").load()
        entry = cast(dict[str, object], cast(dict[str, object], state["sources"])[
            "synthetic-publication"
        ])
        admitted = sum((25, 25, 10)[: page_index + 1])
        assert entry["active_run"] is None
        assert entry["next_cursor"] == result.next_cursor
        assert len(cast(dict[str, object], entry["committed_capture_ids"])) == admitted
        assert len(cast(dict[str, object], entry["committed_revisions"])) == admitted
        assert len(cast(dict[str, object], entry["committed_digests"])) == admitted
        receipts = _terminal_snapshot(tasks)
        assert len(receipts) == admitted
        assert all(row[2] is not None for row in receipts)
        if page_index == 0:
            # Reopen every durable object, not only the scan iterator.
            tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    assert len(visited) == len(set(visited)) == 60
    assert "item-059.md" in visited
    assert runtime.last_scan is not None and runtime.last_scan.complete
    assert runtime.absence_candidates == ()
    before = _terminal_snapshot(tasks)
    source_ids: set[str] = set()
    capture_ids: set[str] = set()
    late_source: str | None = None
    for _, envelope, receipt_json in before:
        delivery = json.loads(cast(bytes, envelope))
        receipt = json.loads(cast(str, receipt_json))
        source = receipt["source_receipt"]
        source_ids.add(source["source_id"])
        capture_ids.add(source["capture_id"])
        capture = delivery["submission"]["capture"]
        if capture["title"] == "Publication item 59":
            late_source = source["source_id"]
        assert capture["privacy"]["authority"] == {
            "cloud": False, "external_egress": False
        }
        assert "private motivation" not in json.dumps(capture["payload"])
        assert "owner: synthetic" not in json.dumps(capture["payload"])
    assert len(source_ids) == len(capture_ids) == 60
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 60
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 60
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0
    # Exercise actual publication for the final page, not only the first25 admissions.
    assert late_source is not None
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    approved = _approve_current(tasks, late_source, owner, "late-page")
    assert tasks.sources is not None and approved.copy_capture_id is not None
    late = tasks.sources.inspect(SourceInspectRequest(source_id=late_source), authority=owner)
    external = _external(late.destination_brain_id, late.issuer_epoch, "openai")
    published = tasks.retrieval.read_record(
        RecordReadRequest(
            record_id=approved.copy_capture_id, expected_revision_id=approved.copy_capture_id
        ), authority=external
    ).to_wire()
    published_content = cast(dict[str, object], published["content"])
    assert "Full synthetic body 59 漢字" in cast(str, published_content["text"])
    with pytest.raises(T03Error, match="not_found"):
        tasks.retrieval.read_record(
            RecordReadRequest(
                record_id=late.head_capture_id, expected_revision_id=late.head_capture_id
            ), authority=external
        )
    # A fresh unchanged scan may reread bodies, but creates no new engine custody.
    for page_index, expected_count in enumerate((25, 25, 10)):
        now[0] = 280 + page_index * 60
        result = controller.sync_due(
            source_id="synthetic-publication", runtime=runtime, capture_sink=sink
        )
        assert result.captured_count == result.quarantined_count == 0
        assert result.duplicate_count == expected_count
        assert (result.next_cursor is None) is (page_index == 2)
        assert controller.custody_status("synthetic-publication")["retained_items"] == 0
    assert _terminal_snapshot(tasks) == before
    assert runtime.last_scan is not None and runtime.last_scan.complete
    assert runtime.absence_candidates == ()


def _approve_current(
    tasks: EngineTaskSet, source_id: str, owner: EffectiveAuthority, operation: str
) -> SharingDecisionReceipt:
    assert tasks.sources is not None and tasks.sharing is not None
    source = tasks.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    request = SharingPreviewRequest(
        operation_id="sharing.preview." + operation,
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
    assert "private motivation" not in preview.text and "owner: synthetic" not in preview.text
    assert tasks.sharing.preview(request, authority=owner) == preview
    decision = SharingDecisionRequest(
        operation_id="sharing.approve." + operation,
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=source.destination_brain_id,
        issuer_epoch=source.issuer_epoch,
        destination_brain_id=source.destination_brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    receipt = tasks.sharing.decide(decision, authority=owner)
    assert receipt.copy_capture_id is not None
    assert tasks.sharing.decide(decision, authority=owner) == receipt
    return receipt


@pytest.mark.parametrize("returned", ["same", "changed"])
def test_publication_version_approval_withdrawal_retains_history(
    tmp_path: Path, returned: str
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    path = selected / "saved.md"

    def content(version: int) -> str:
        return (
            f"---\nowner: synthetic\n---\n# Publication version {version}\n"
            f"Full retained body {version} 漢字\n\n# Why Saved\nprivate motivation\n"
        )

    path.write_text(content(1), encoding="utf-8")
    now = [100]
    tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    controller.enable(
        source_id="synthetic-publication", selection=_adapter(selected).selection,
        interval_seconds=60,
    )
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    originals: list[str] = []
    copies: list[str] = []
    approvals: list[SharingDecisionReceipt] = []
    tails: list[RecordReadRequest] = []
    source_id = ""
    for version in (1, 2, 3):
        path.write_text(content(version), encoding="utf-8")
        now[0] = 100 + (version - 1) * 60
        run = controller.sync_due(
            source_id="synthetic-publication", runtime=runtime, capture_sink=sink
        )
        assert run.captured_count == 1 and run.quarantined_count == 0
        assert tasks.sources is not None
        if version == 1:
            source_id = json.loads(cast(str, _terminal_snapshot(tasks)[0][2]))[
                "source_receipt"
            ]["source_id"]
        source = tasks.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
        assert source.head_version == version and source.lifecycle == "active"
        originals.append(source.head_capture_id)
        external = _external(source.destination_brain_id, source.issuer_epoch, "openai")
        for capture_id in originals + copies:
            refusal = "revision_changed" if capture_id in originals[:-1] else "not_found"
            with pytest.raises(T03Error, match=refusal):
                tasks.retrieval.read_record(
                    RecordReadRequest(record_id=capture_id, expected_revision_id=capture_id),
                    authority=external,
                )
        if version > 1:
            receipt = _approve_current(tasks, source_id, owner, str(version))
            approvals.append(receipt)
            assert receipt.copy_capture_id is not None
            copies.append(receipt.copy_capture_id)
            reading = RecordReadRequest(
                record_id=receipt.copy_capture_id,
                expected_revision_id=receipt.copy_capture_id,
                target_bytes=8,
            )
            first = tasks.retrieval.read_record(reading, authority=external).to_wire()
            assert first["next_cursor"] is not None
            tail = replace(reading, cursor=cast(str, first["next_cursor"]))
            tails.append(tail)
            assert tasks.retrieval.read_record(tail, authority=external).to_wire()[
                "start_byte"
            ] == first["end_byte"]
            with pytest.raises(T03Error, match="not_found"):
                tasks.retrieval.read_record(
                    reading,
                    authority=_external(source.destination_brain_id, source.issuer_epoch, "gemini"),
                )
    assert len(set(originals)) == 3 and len(set(copies)) == 2
    path.unlink()
    now[0] = 280
    absent = controller.sync_due(
        source_id="synthetic-publication", runtime=runtime, capture_sink=sink
    )
    assert absent.captured_count == 0
    assert runtime.last_scan is not None and runtime.last_scan.complete
    assert len(runtime.absence_candidates) == 1
    candidate = runtime.absence_candidates[0]
    assert candidate.source_id == source_id and candidate.expected_head == originals[-1]
    assert tasks.sources is not None
    source = tasks.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    assert source.lifecycle == "active"  # Complete absence is not automatic withdrawal.
    withdrawal = SourceWithdrawRequest(
        operation_id="withdraw.synthetic.publication",
        source_id=source_id,
        expected_head=source.head_capture_id,
        expected_lifecycle_version=source.lifecycle_version,
        brain_id=source.destination_brain_id,
        issuer_epoch=source.issuer_epoch,
        reason_code="owner_confirmed_absence",
        absence_evidence_digest=candidate.evidence_sha256,
    )
    withdrawn = tasks.sources.withdraw(withdrawal, authority=owner)
    assert withdrawn.lifecycle == "retired"
    assert tasks.sharing is not None
    revoke = SharingRevokeRequest(
        operation_id="sharing.revoke.synthetic-publication",
        approval_id=approvals[-1].approval_id,
        expected_approval_version=1,
        brain_id=source.destination_brain_id,
        issuer_epoch=source.issuer_epoch,
        destination_brain_id=source.destination_brain_id,
        reason="owner_choice",
    )
    revoked = tasks.sharing.revoke(revoke, authority=owner)
    assert tasks.sharing.revoke(revoke, authority=owner) == revoked
    before = _terminal_snapshot(tasks)
    path.write_text(content(3 if returned == "same" else 4), encoding="utf-8")
    now[0] = 340
    tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    try:
        result = controller.sync_due(
            source_id="synthetic-publication", runtime=runtime, capture_sink=sink
        )
    except T03Error as error:
        assert error.code in {"revision_changed", "source_revision_conflict"}
    else:
        assert result.captured_count == 0
    assert runtime.absence_candidates == ()
    assert _terminal_snapshot(tasks) == before
    tasks.portability.rebuild_index()
    assert tasks.sources is not None and tasks.history is not None and tasks.sharing is not None
    assert tasks.sources.withdraw(withdrawal, authority=owner) == withdrawn
    assert tasks.sharing.revoke(revoke, authority=owner) == revoked
    assert tasks.sources.inspect(
        SourceInspectRequest(source_id=source_id), authority=owner
    ).lifecycle == "retired"
    external_history = replace(external, capabilities=frozenset({"history-read", "content-read"}))
    for capture_id in originals + copies:
        reading = RecordReadRequest(record_id=capture_id, expected_revision_id=capture_id)
        retained = tasks.history.read_history(reading, authority=owner).to_wire()
        retained_content = cast(dict[str, object], retained["content"])
        assert "Full retained body" in cast(str, retained_content["text"])
        with pytest.raises(T03Error, match="not_found"):
            tasks.retrieval.read_record(reading, authority=external)
        with pytest.raises(T03Error, match="not_found"):
            tasks.history.read_history(reading, authority=external_history)
    listing = tasks.history.list_history(
        HistoryListRequest(record_id=originals[0]), authority=owner
    ).to_wire()
    entries = cast(list[dict[str, object]], listing["entries"])
    assert {row["revision_id"] for row in entries} == set(originals)
    for tail in tails:
        with pytest.raises(T03Error, match="not_found|cursor_stale"):
            tasks.retrieval.read_record(tail, authority=external)
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 5
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 5
        assert {
            row[0] for row in connection.execute(
                "SELECT capture_id FROM source_revisions WHERE source_id=?", (source_id,)
            )
        } == set(originals)
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 2
