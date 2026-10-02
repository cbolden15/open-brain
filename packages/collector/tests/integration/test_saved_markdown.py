from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    CaptureSubmission,
    ContentOrigin,
    PrivacyDecision,
    PublicJobCaptureContext,
    PublicJobRevisionSink,
    SourceRevisionDelivery,
    SourceRevisionDeliveryReceipt,
    open_local_engine,
)
from open_brain_engine.engine.contracts import EngineTaskSet
from open_brain_engine.engine.local_schema import open_local_database_read_only

from open_brain.profile import compile_single_user_local
from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorStateStore,
    EngineRevisionSink,
    MemoryCaptureSink,
)
from open_brain_collector.saved_markdown import SavedMarkdownCollectorRuntime
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_markdown import SavedMarkdownRootAdapter
from open_brain_connectors.runtime.source_intake import SourceRecordIntake


def test_saved_markdown_stable_item_predecessor_revision_and_revert(tmp_path: Path) -> None:
    root = _root(tmp_path)
    tasks, sink = _revision_sink(tmp_path)
    first = _intake(root)
    a = sink.submit(first)
    assert a.source_receipt is not None
    assert sink.submit(first).outcome == "duplicate"
    (root / "saved.md").write_text("# Changed saved item\nchanged body\n", encoding="utf-8")
    second = _intake(root)
    b = sink.submit(second)
    assert b.source_receipt is not None
    # Return to the exact initial bytes, not merely the normalized body.
    (root / "saved.md").write_text(
        "---\nowner: synthetic\n---\n# Third-party saved item\n\nbody\n\n# Why Saved\nprivate\n",
        encoding="utf-8",
    )
    c = sink.submit(_intake(root))
    assert c.source_receipt is not None
    assert first.key.external_id == second.key.external_id
    assert a.source_receipt.source_id == b.source_receipt.source_id == c.source_receipt.source_id
    assert len({a.source_receipt.capture_id, b.source_receipt.capture_id,
                c.source_receipt.capture_id}) == 3
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 3


@pytest.mark.parametrize("failure", ["lost_response", "pending", "wrong_destination"])
def test_revision_delivery_exact_envelope_pending_receipt_and_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    root = _root(tmp_path)
    tasks, sink = _revision_sink(tmp_path)
    runtime = SavedMarkdownCollectorRuntime(_adapter(root),
                                           store=PrivateJsonStore(tmp_path / "scan"))
    state_path = tmp_path / "state.json"
    controller = CollectorController(CollectorStateStore(state_path), clock=lambda: 100,
                                     brain_root=tmp_path / "brain")
    controller.enable(source_id="synthetic", selection=_adapter(root).selection,
                      interval_seconds=60)
    original = PublicJobRevisionSink.submit
    submitted: list[bytes] = []

    def fail_once(self: PublicJobRevisionSink, delivery: SourceRevisionDelivery
                  ) -> SourceRevisionDeliveryReceipt:
        submitted.append(delivery.custody_bytes())
        if len(submitted) == 1 and failure == "pending":
            return SourceRevisionDeliveryReceipt(delivery.delivery_id, delivery.envelope_sha256,
                delivery.binding.destination_brain_id, delivery.binding.issuer_epoch,
                None, "operation_pending")
        result = original(self, delivery)
        if len(submitted) == 1:
            if failure == "lost_response":
                raise RuntimeError("synthetic lost response")
            return replace(result, destination_brain_id="brn_wrong")
        return result

    monkeypatch.setattr(PublicJobRevisionSink, "submit", fail_once)
    if failure == "pending":
        assert controller.sync_due(source_id="synthetic", runtime=runtime,
                                   capture_sink=sink).outcome.value == "deferred"
    else:
        with pytest.raises(Exception, match="lost response|collector_invalid_custody_receipt"):
            controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    assert controller.custody_status("synthetic")["retained_items"] == 1
    retained = PrivateJsonStore(tmp_path / "revisions")
    assert cast(dict[str, object], retained.read(retained.names("saved-revision-")[0]))[
        "terminal"] is None
    (root / "saved.md").write_text("# newer filesystem bytes\n", encoding="utf-8")
    assert tasks.sources is not None
    restarted_sink = EngineRevisionSink(tasks.sources.public_revision_sink,
        tasks.capture.public_job_sink(_context(tasks.profile)), store=retained)
    restarted = CollectorController(CollectorStateStore(state_path), clock=lambda: 100,
                                    brain_root=tmp_path / "brain")
    replay = restarted.sync_due(source_id="synthetic", runtime=runtime,
                                capture_sink=restarted_sink)
    assert replay.outcome.value == "completed"
    assert submitted[0] == submitted[1]
    assert restarted.custody_status("synthetic")["retained_items"] == 0
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1


def test_full_scan_continues_beyond_25_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    for index in range(60):
        (root / f"item-{index:03}.md").write_text(f"# item {index}\nbody\n", encoding="utf-8")
    store = PrivateJsonStore(tmp_path / "scan")
    adapter = _adapter(root)
    original = SavedMarkdownRootAdapter._candidate
    visited: list[str] = []

    def observe(self: SavedMarkdownRootAdapter, selected: Path, path: Path,
                relative: str) -> object:
        visited.append(relative)
        return original(self, selected, path, relative)

    monkeypatch.setattr(SavedMarkdownRootAdapter, "_candidate", observe)
    runtime = SavedMarkdownCollectorRuntime(adapter, store=store)
    first = runtime.fetch_page(adapter.selection, None)
    assert len(first.intakes) == 25
    assert first.next_cursor is not None
    runtime.acknowledge_page()
    runtime = SavedMarkdownCollectorRuntime(_adapter(root), store=store)
    second = runtime.fetch_page(adapter.selection, first.next_cursor)
    assert len(second.intakes) == 25
    runtime.acknowledge_page()
    third = runtime.fetch_page(adapter.selection, second.next_cursor)
    assert len(third.intakes) == 10
    assert third.next_cursor is None
    assert runtime.last_scan is not None and runtime.last_scan.complete
    assert len(visited) == len(set(visited)) == 60


@pytest.mark.parametrize("failure", ["concurrent_edit", "membership", "enumeration", "policy",
                                     "refused_present"])
def test_full_scan_failed_or_refused_presence_never_proves_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    root = _root(tmp_path)
    for index in range(30):
        (root / f"item-{index:03}.md").write_text(f"# item {index}\n", encoding="utf-8")
    adapter = _adapter(root)
    runtime = SavedMarkdownCollectorRuntime(adapter, store=PrivateJsonStore(tmp_path / "scan"))
    _, sink = _revision_sink(tmp_path)
    intake = _intake(root, name="saved.md")
    receipt = sink.submit(intake)
    runtime.record_terminal(intake, receipt)
    first = runtime.fetch_page(adapter.selection, None)
    runtime.acknowledge_page()
    if failure == "concurrent_edit":
        (root / "item-000.md").write_text("# concurrent changed\n", encoding="utf-8")
    elif failure == "membership":
        (root / "new.md").write_text("# new\n", encoding="utf-8")
    elif failure == "enumeration":
        import os

        monkeypatch.setattr(os, "scandir", lambda *args: (_ for _ in ()).throw(OSError()))
    elif failure == "policy":
        adapter = SavedMarkdownRootAdapter(root, destination_identity="destination:synthetic",
            accepted_source_identity="source:synthetic",
            source_reference="https://saved.example.test/library",
            privacy=replace(intake.privacy, policy_version="policy-v2"))
        runtime = SavedMarkdownCollectorRuntime(adapter, store=PrivateJsonStore(tmp_path / "scan"))
    else:
        (root / "saved.md").write_text("---\nmalformed frontmatter\n", encoding="utf-8")
    cursor = first.next_cursor
    while cursor is not None:
        page = runtime.fetch_page(adapter.selection, cursor)
        runtime.acknowledge_page()
        cursor = page.next_cursor
    assert runtime.absence_candidates == ()
    assert runtime.last_scan is not None
    assert runtime.last_scan.complete is (failure == "refused_present")


def test_complete_scan_proposes_metadata_absence_and_known_return_clears_it(tmp_path: Path) -> None:
    root = _root(tmp_path)
    adapter = _adapter(root)
    runtime = SavedMarkdownCollectorRuntime(adapter, store=PrivateJsonStore(tmp_path / "scan"))
    _, sink = _revision_sink(tmp_path)
    intake = _intake(root)
    runtime.record_terminal(intake, sink.submit(intake))
    first = runtime.fetch_page(adapter.selection, None)
    assert first.next_cursor is None
    runtime.acknowledge_page()
    (root / "saved.md").unlink()
    runtime.fetch_page(adapter.selection, None)
    candidates = runtime.absence_candidates
    assert len(candidates) == 1
    assert candidates[0].item_id == intake.key.external_id.removeprefix("item:")
    assert len(candidates[0].evidence_sha256) == 64
    runtime.acknowledge_page()
    (root / "saved.md").write_text("---\nmalformed but present\n", encoding="utf-8")
    runtime.fetch_page(adapter.selection, None)
    assert runtime.absence_candidates == ()


def test_revision_pending_envelope_tamper_and_changed_retry_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, sink = _revision_sink(tmp_path)
    original = _intake(root)

    def pending(self: PublicJobRevisionSink, delivery: SourceRevisionDelivery
                ) -> SourceRevisionDeliveryReceipt:
        return SourceRevisionDeliveryReceipt(delivery.delivery_id, delivery.envelope_sha256,
            delivery.binding.destination_brain_id, delivery.binding.issuer_epoch,
            None, "operation_pending")

    monkeypatch.setattr(PublicJobRevisionSink, "submit", pending)
    assert sink.submit(original).outcome == "operation_pending"
    (root / "saved.md").write_text("# changed before retry\n", encoding="utf-8")
    with pytest.raises(LiveSourceError, match="collector_custody_stale"):
        sink.submit(_intake(root))
    store = PrivateJsonStore(tmp_path / "revisions")
    name = store.names("saved-revision-")[0]
    saved = cast(dict[str, object], store.read(name))
    envelope = cast(dict[str, object], saved["envelope"])
    envelope["expected_lifecycle_version"] = 99
    store.write(name, saved)
    with pytest.raises(LiveSourceError, match="collector_custody_stale"):
        sink.submit(original)


def test_scan_budget_continuation_root_change_and_cursor_tamper_refuse(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for index in range(30):
        (root / f"item-{index:03}.md").write_text("# body\n", encoding="utf-8")
    adapter = _adapter(root)
    epoch = adapter.begin_epoch()
    bounded = adapter.scan_page(epoch, max_entries=1)
    assert bounded.complete is False and bounded.candidates == ()
    timed = adapter.scan_page(bounded.epoch, max_seconds=1e-9)
    assert timed.complete is False and timed.candidates == ()
    resumed = adapter.scan_page(timed.epoch)
    assert len(resumed.candidates) == 25
    runtime = SavedMarkdownCollectorRuntime(adapter, store=PrivateJsonStore(tmp_path / "scan"))
    first = runtime.fetch_page(adapter.selection, None)
    runtime.acknowledge_page()
    assert first.next_cursor is not None
    from open_brain_connectors.runtime.connectors import ConnectorContractError

    with pytest.raises(ConnectorContractError, match="invalid saved markdown continuation"):
        runtime.fetch_page(adapter.selection, first.next_cursor.split(":")[0] + ":forged")
    root.rename(tmp_path / "old-root")
    root.mkdir()
    page = runtime.fetch_page(adapter.selection, first.next_cursor)
    assert page.next_cursor is None
    assert runtime.last_scan is not None and not runtime.last_scan.complete
    assert runtime.absence_candidates == ()


def _intake(root: Path, *, name: str | None = None) -> SourceRecordIntake:
    adapter = _adapter(root)
    candidate = (adapter._candidate(root, root / name, name) if name is not None
                 else adapter.dry_run().candidates[0])
    assert candidate.intake is not None
    return candidate.intake


def _revision_sink(tmp_path: Path) -> tuple[EngineTaskSet, EngineRevisionSink]:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    assert tasks.sources is not None
    return tasks, EngineRevisionSink(tasks.sources.public_revision_sink,
        tasks.capture.public_job_sink(_context(tasks.profile)),
        store=PrivateJsonStore(tmp_path / "revisions"))


def test_saved_markdown_public_job_admission_is_local_only_and_third_party(tmp_path: Path) -> None:
    adapter = _adapter(_root(tmp_path))
    candidate = adapter.dry_run().candidates[0]
    assert candidate.intake is not None
    intake = candidate.intake
    brain = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(brain))
    context = _context(tasks.profile)
    submission = CaptureSubmission.for_public_job(
        context=context,
        payload=intake.payload(),
        delivery_id=intake.key.delivery_id(),
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=intake.source_reference,
        provenance=intake.provenance(),
        privacy=intake.privacy,
        intent="reference",
        title=intake.title,
    )

    receipt = tasks.capture.submit(submission)

    assert receipt.requested_tier.value == "public"
    assert receipt.final_admitted_tier.value == "public"
    assert submission.provenance.content_origin is ContentOrigin.THIRD_PARTY
    assert submission.privacy.authority.cloud is False
    assert submission.privacy.authority.external_egress is False
    assert len(intake.text.encode("utf-8")) <= 65_536


def test_saved_markdown_replays_exact_staged_intake_after_lost_response(
    tmp_path: Path,
) -> None:
    adapter = _adapter(_root(tmp_path))
    runtime = SavedMarkdownCollectorRuntime(adapter)
    controller = CollectorController(
        CollectorStateStore(tmp_path / "state.json"), clock=lambda: 100
    )
    controller.enable(
        source_id="saved-markdown.synthetic",
        selection=adapter.selection,
        interval_seconds=60,
    )
    sink = _LostResponseSink()

    with pytest.raises(RuntimeError, match="synthetic lost response"):
        controller.sync_due(
            source_id="saved-markdown.synthetic", runtime=runtime, capture_sink=sink
        )
    receipt_ids = cast(
        list[str], controller.custody_status("saved-markdown.synthetic")["receipt_ids"]
    )
    receipt_id = receipt_ids[0]
    retained = controller._custody.intake(receipt_id)

    restarted = CollectorController(CollectorStateStore(tmp_path / "state.json"), clock=lambda: 100)
    replay = restarted.sync_due(
        source_id="saved-markdown.synthetic", runtime=runtime, capture_sink=sink
    )

    assert replay.outcome.value == "completed"
    assert sink.intakes == [retained, retained]
    assert sink.intakes[0].key.delivery_id() == sink.intakes[1].key.delivery_id()


def test_saved_markdown_refusals_do_not_enter_collector_custody(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "secret.md").write_text("# secret\napi_key = synthetic\n", encoding="utf-8")
    adapter = _adapter(root)
    runtime = SavedMarkdownCollectorRuntime(adapter)
    controller = CollectorController(
        CollectorStateStore(tmp_path / "state.json"), clock=lambda: 100
    )
    controller.enable(
        source_id="saved-markdown.synthetic", selection=adapter.selection, interval_seconds=60
    )

    result = controller.sync_due(
        source_id="saved-markdown.synthetic", runtime=runtime, capture_sink=MemoryCaptureSink()
    )

    assert result.captured_count == 1
    assert runtime.last_scan is not None
    assert [item.refusal_code for item in runtime.last_scan.candidates if not item.accepted] == [
        "saved_markdown_secret_bearing"
    ]
    assert controller.custody_status("saved-markdown.synthetic")["retained_items"] == 0


class _LostResponseSink:
    def __init__(self) -> None:
        self.intakes: list[SourceRecordIntake] = []

    def submit(self, intake: SourceRecordIntake) -> None:
        self.intakes.append(intake)
        if len(self.intakes) == 1:
            raise RuntimeError("synthetic lost response")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "selected"
    root.mkdir()
    (root / "saved.md").write_text(
        "---\nowner: synthetic\n---\n# Third-party saved item\n\nbody\n\n# Why Saved\nprivate\n",
        encoding="utf-8",
    )
    return root


def _adapter(root: Path) -> SavedMarkdownRootAdapter:
    return SavedMarkdownRootAdapter(
        root,
        destination_identity="destination:synthetic",
        accepted_source_identity="source:synthetic",
        source_reference="https://saved.example.test/library",
        privacy=PrivacyDecision.from_dict(
            {
                "authority": {"cloud": False, "external_egress": False},
                "confirmation_ref": None,
                "policy_version": "policy-v1",
                "reason": "policy_public",
                "tier": "public",
            }
        ),
    )


def _context(profile: object) -> PublicJobCaptureContext:
    from open_brain_engine.engine import LocalEngineContext

    assert isinstance(profile, LocalEngineContext)
    actor = "actor_00000000-0000-4000-8000-000000000701"
    return PublicJobCaptureContext.create(
        profile=profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000702",
            "role_id": "role_00000000-0000-4000-8000-000000000703",
            "tenant_id": profile.tenant_id,
        },
    )
