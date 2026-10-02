from __future__ import annotations

import json
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import (
    CaptureFault,
    CaptureSubmission,
    ContentOrigin,
    InjectedFault,
    PrivacyDecision,
    PublicJobCaptureContext,
    PublicJobRevisionSink,
    SourceInspectRequest,
    SourceRevisionDelivery,
    SourceRevisionDeliveryReceipt,
    SourceWithdrawRequest,
    open_local_engine,
)
from open_brain_engine.engine.contracts import EngineTaskSet
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    T03Error,
)

from open_brain.profile import compile_single_user_local
from open_brain_collector.custody import intake_dict, intake_digest, intake_from_dict
from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorStateStore,
    EngineRevisionSink,
    MemoryCaptureSink,
)
from open_brain_collector.saved_markdown import SavedMarkdownCollectorRuntime
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
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


@pytest.mark.parametrize("failure", ["pending", "lost_response"])
def test_observation_survives_custody_restart_exact_retry_and_terminal_engine_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    root = _root(tmp_path)
    raw = "---\r\nowner: synthetic\r\n---\r\n# Cafe\u0301\r\nsynthetic body\r\n".encode()
    (root / "saved.md").write_bytes(raw)
    intake = _intake(root)
    assert intake.observation is not None
    observation = intake.observation.value()
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
            raise RuntimeError("synthetic lost response")
        return result

    monkeypatch.setattr(PublicJobRevisionSink, "submit", fail_once)
    if failure == "pending":
        assert controller.sync_due(source_id="synthetic", runtime=runtime,
                                   capture_sink=sink).outcome.value == "deferred"
    else:
        with pytest.raises(RuntimeError, match="synthetic lost response"):
            controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    receipt_ids = cast(list[str], controller.custody_status("synthetic")["receipt_ids"])
    staged = controller._custody.intake(receipt_ids[0])
    assert staged == intake
    assert intake_dict(staged)["observation"] == observation
    revisions = PrivateJsonStore(tmp_path / "revisions")
    pending = cast(dict[str, object], revisions.read(revisions.names("saved-revision-")[0]))
    envelope = cast(dict[str, object], pending["envelope"])
    assert envelope["dto_version"] == 2
    assert envelope["observation"] == observation
    (root / "saved.md").write_text("# Newer filesystem revision\n", encoding="utf-8")
    restarted_tasks, restarted_sink = _revision_sink(tmp_path)
    restarted = CollectorController(CollectorStateStore(state_path), clock=lambda: 100,
                                    brain_root=tmp_path / "brain")
    restarted_runtime = SavedMarkdownCollectorRuntime(_adapter(root),
                                                      store=PrivateJsonStore(tmp_path / "scan"))
    replay = restarted.sync_due(source_id="synthetic", runtime=restarted_runtime,
                                capture_sink=restarted_sink)
    assert replay.outcome.value == "completed"
    assert submitted[0] == submitted[1]
    assert restarted.custody_status("synthetic")["retained_items"] == 0
    with open_local_database_read_only(restarted_tasks.profile) as connection:
        rows = connection.execute(
            "SELECT envelope_bytes,receipt_json FROM managed_source_deliveries"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["receipt_json"] is not None
        assert bytes(rows[0]["envelope_bytes"]) == submitted[0]
        retained = json.loads(rows[0]["envelope_bytes"])
        assert retained["observation"] == observation
        capture = retained["submission"]["capture"]
        assert observation["admitted_payload_sha256"] == sha256(
            portable_canonical_json_bytes(capture["payload"])).hexdigest()
        assert observation["privacy_policy_sha256"] == sha256(
            portable_canonical_json_bytes(capture["privacy"])).hexdigest()
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1


def test_completed_historical_receipt_replays_after_head_advance_without_head_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    tasks, sink = _revision_sink(tmp_path)
    original_intake = _intake(root)
    original = PublicJobRevisionSink.submit
    submitted: list[bytes] = []

    def lose_once(self: PublicJobRevisionSink, delivery: SourceRevisionDelivery
                  ) -> SourceRevisionDeliveryReceipt:
        submitted.append(delivery.custody_bytes())
        result = original(self, delivery)
        if len(submitted) == 1:
            raise RuntimeError("synthetic lost response")
        return result

    monkeypatch.setattr(PublicJobRevisionSink, "submit", lose_once)
    with pytest.raises(RuntimeError, match="synthetic lost response"):
        sink.submit(original_intake)
    (root / "saved.md").write_text("# Later accepted head\nsynthetic body\n", encoding="utf-8")
    assert tasks.sources is not None
    other_sink = EngineRevisionSink(tasks.sources.public_revision_sink,
        tasks.capture.public_job_sink(_context(tasks.profile)),
        store=PrivateJsonStore(tmp_path / "other-revisions"))
    advanced = other_sink.submit(_intake(root))
    assert advanced.source_receipt is not None
    later_head = advanced.source_receipt.capture_id
    assert later_head is not None
    _, restarted_sink = _revision_sink(tmp_path)

    def no_head_read(self: PublicJobRevisionSink) -> None:
        raise AssertionError("historical receipt verification must use immutable engine evidence")

    monkeypatch.setattr(PublicJobRevisionSink, "inspect_head", no_head_read)
    replay = restarted_sink.submit(original_intake)
    assert submitted[0] == submitted[2]
    assert replay.source_receipt is not None
    assert replay.source_receipt.capture_id != later_head
    assert replay.source_receipt.source_id == advanced.source_receipt.source_id
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT head_capture_id FROM logical_sources").fetchone()[0] == (
            later_head
        )
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 2


def test_legacy_intake_custody_and_revision_delivery_shapes_remain_unchanged(
    tmp_path: Path,
) -> None:
    intake = replace(_intake(_root(tmp_path)), observation=None)
    expected = {"key": asdict(intake.key), "url": intake.url, "text": intake.text,
                "title": intake.title, "privacy": intake.privacy.to_dict()}
    assert intake_dict(intake) == expected
    assert bounded_json(intake_dict(intake)) == bounded_json(expected)
    assert intake_digest(intake) == sha256(bounded_json(expected)).hexdigest()
    assert intake_from_dict(expected) == intake
    tasks, sink = _revision_sink(tmp_path)
    assert sink.submit(intake).outcome == "captured"
    with open_local_database_read_only(tasks.profile) as connection:
        row = connection.execute("SELECT envelope_bytes FROM managed_source_deliveries").fetchone()
    assert set(json.loads(row[0])) == {"dto_version", "binding", "submission",
                                      "expected_lifecycle_version", "delivery_id"}
    assert json.loads(row[0])["dto_version"] == 1


@pytest.mark.parametrize("tamper", ["unknown", "missing", "payload", "privacy", "version"])
def test_observed_intake_custody_refuses_invalid_or_mismatched_observation(
    tmp_path: Path, tamper: str,
) -> None:
    value = intake_dict(_intake(_root(tmp_path)))
    observation = cast(dict[str, object], value["observation"])
    if tamper == "unknown":
        observation["extra"] = "synthetic"
    elif tamper == "missing":
        observation.pop("original_sha256")
    elif tamper == "payload":
        observation["admitted_payload_sha256"] = "f" * 64
    elif tamper == "privacy":
        observation["privacy_policy_sha256"] = "f" * 64
    else:
        observation["dto_version"] = True
    with pytest.raises(LiveSourceError, match="collector_invalid_custody"):
        intake_from_dict(value)


def test_saved_markdown_rename_is_new_item_and_only_proposes_old_item_absence(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    original_intake = _intake(root)
    tasks, sink = _revision_sink(tmp_path)
    runtime = SavedMarkdownCollectorRuntime(_adapter(root),
                                           store=PrivateJsonStore(tmp_path / "scan"))
    controller = CollectorController(CollectorStateStore(tmp_path / "state.json"),
                                     clock=lambda: 100, brain_root=tmp_path / "brain")
    controller.enable(source_id="synthetic", selection=_adapter(root).selection,
                      interval_seconds=60)
    assert controller.sync_due(source_id="synthetic", runtime=runtime,
                               capture_sink=sink).captured_count == 1
    (root / "saved.md").rename(root / "renamed.md")
    returned_intake = _intake(root)
    assert returned_intake.text == original_intake.text
    assert returned_intake.key.external_id != original_intake.key.external_id
    controller.sync_now("synthetic")
    assert controller.sync_due(source_id="synthetic", runtime=runtime,
                               capture_sink=sink).captured_count == 1
    candidates = runtime.absence_candidates
    assert len(candidates) == 1
    assert candidates[0].item_id == original_intake.key.external_id.removeprefix("item:")
    assert candidates[0].lifecycle_version == 0
    with open_local_database_read_only(tasks.profile) as connection:
        sources = connection.execute("SELECT source_id FROM logical_sources").fetchall()
        assert len(sources) == 2
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM source_lifecycle_operations"
        ).fetchone()[0] == 0
    assert tasks.sources is not None
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    for row in sources:
        inspection = tasks.sources.inspect(SourceInspectRequest(source_id=row[0]), authority=owner)
        assert inspection.lifecycle == "active" and inspection.withdrawal_receipt is None
    evidence = bounded_json(asdict(candidates[0]))
    assert original_intake.text.encode() not in evidence
    assert str(root).encode() not in evidence
    assert original_intake.url.encode() not in evidence


@pytest.mark.parametrize("action", ["pause", "disable"])
def test_saved_markdown_cancellation_ack_retains_uncertain_envelope_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
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

    def accepted_pending(self: PublicJobRevisionSink, delivery: SourceRevisionDelivery
                         ) -> SourceRevisionDeliveryReceipt:
        submitted.append(delivery.custody_bytes())
        original(self, delivery)
        return SourceRevisionDeliveryReceipt(delivery.delivery_id, delivery.envelope_sha256,
            delivery.binding.destination_brain_id, delivery.binding.issuer_epoch,
            None, "operation_pending")

    monkeypatch.setattr(PublicJobRevisionSink, "submit", accepted_pending)
    assert controller.sync_due(source_id="synthetic", runtime=runtime,
                               capture_sink=sink).outcome.value == "deferred"
    revisions = PrivateJsonStore(tmp_path / "revisions")
    name = revisions.names("saved-revision-")[0]
    before = revisions.read(name)
    assert isinstance(before, dict) and before["terminal"] is None
    acknowledged = getattr(controller, action)("synthetic")
    assert acknowledged.status == ("paused" if action == "pause" else "disabled")
    assert revisions.read(name) == before
    (root / "saved.md").write_text("# Changed after cancellation acknowledgement\n",
                                   encoding="utf-8")
    restarted_tasks, restarted_sink = _revision_sink(tmp_path)
    restarted = CollectorController(CollectorStateStore(state_path), clock=lambda: 160,
                                    brain_root=tmp_path / "brain")
    result = restarted.sync_due(source_id="synthetic",
        runtime=SavedMarkdownCollectorRuntime(_adapter(root),
                                              store=PrivateJsonStore(tmp_path / "scan")),
        capture_sink=restarted_sink)
    assert result.outcome.value == ("deferred" if action == "pause" else "skipped")
    assert result.captured_count == 0 and len(submitted) == 1
    assert revisions.read(name) == before
    with open_local_database_read_only(restarted_tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1


@pytest.mark.parametrize("fault_name", ["AFTER_CAPTURE_RESERVATION", "AFTER_SOURCE_WRITE"])
def test_saved_markdown_fenced_terminal_quarantine_retains_exact_custody(
    tmp_path: Path, fault_name: str,
) -> None:
    root = _root(tmp_path)
    intake = _intake(root)
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile, faults={CaptureFault[fault_name]})
    assert tasks.sources is not None
    sink = EngineRevisionSink(tasks.sources.public_revision_sink,
        tasks.capture.public_job_sink(_context(tasks.profile)),
        store=PrivateJsonStore(tmp_path / "revisions"))
    runtime = SavedMarkdownCollectorRuntime(_adapter(root),
                                           store=PrivateJsonStore(tmp_path / "scan"))
    state_path = tmp_path / "state.json"
    controller = CollectorController(CollectorStateStore(state_path), clock=lambda: 100,
                                     brain_root=tmp_path / "brain")
    controller.enable(source_id="synthetic", selection=_adapter(root).selection,
                      interval_seconds=60)
    with pytest.raises(InjectedFault):
        controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    assert tasks.sources.fence_intake(expected_epoch=0, authority=owner) == 1
    restarted_tasks, restarted_sink = _revision_sink(tmp_path)
    restarted = CollectorController(CollectorStateStore(state_path), clock=lambda: 100,
                                    brain_root=tmp_path / "brain")
    result = restarted.sync_due(source_id="synthetic", runtime=runtime,
                               capture_sink=restarted_sink)
    assert result.quarantined_count == 1 and result.captured_count == result.duplicate_count == 0
    assert restarted.custody_status("synthetic")["retained_items"] == 1
    revisions = PrivateJsonStore(tmp_path / "revisions")
    name = revisions.names("saved-revision-")[0]
    retained = revisions.read(name)
    assert isinstance(retained, dict)
    terminal = retained["terminal"]
    assert isinstance(terminal, dict) and terminal["outcome"] == "quarantined"
    assert terminal["control_epoch"] == 1 and terminal["custody_id"] is not None
    envelope = retained["envelope"]
    assert isinstance(envelope, dict) and "submission" in envelope and "observation" in envelope
    with pytest.raises(T03Error, match="source_revision_conflict"):
        restarted_sink.submit(intake)
    assert revisions.read(name) == retained
    with open_local_database_read_only(restarted_tasks.profile) as connection:
        assert connection.execute(
            "SELECT count(*) FROM source_revisions WHERE revision_key IS NOT NULL"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM source_namespaces").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM logical_sources WHERE historical_only=0"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM source_quarantine").fetchone()[0] == 1
    assert restarted_tasks.retrieval.search("body") == ()


@pytest.mark.parametrize("field", ["source_id", "capture_id", "control_epoch", "history_only"])
def test_saved_markdown_refuses_wrong_nested_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    root = _root(tmp_path)
    _, sink = _revision_sink(tmp_path)
    original = PublicJobRevisionSink.submit

    def wrong(self: PublicJobRevisionSink, delivery: SourceRevisionDelivery
              ) -> SourceRevisionDeliveryReceipt:
        result = original(self, delivery)
        receipt = result.source_receipt
        assert receipt is not None
        if field == "source_id":
            receipt = replace(receipt, source_id="source_00000000-0000-4000-8000-000000000099")
        elif field == "capture_id":
            receipt = replace(receipt, capture_id="capture_" + "f" * 64)
        elif field == "control_epoch":
            receipt = replace(receipt, control_epoch=receipt.control_epoch + 1)
        else:
            receipt = replace(receipt, outcome="history_only")
        return replace(result, source_receipt=receipt, outcome=receipt.outcome)

    monkeypatch.setattr(PublicJobRevisionSink, "submit", wrong)
    with pytest.raises(LiveSourceError, match="collector_invalid_custody_receipt"):
        sink.submit(_intake(root))
    revisions = PrivateJsonStore(tmp_path / "revisions")
    retained = revisions.read(revisions.names("saved-revision-")[0])
    assert isinstance(retained, dict) and retained["terminal"] is None


def test_changed_saved_markdown_return_after_owner_withdrawal_stays_retired(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    tasks, sink = _revision_sink(tmp_path)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    now = [100]
    state_path = tmp_path / "state.json"
    runtime = SavedMarkdownCollectorRuntime(
        _adapter(root), store=PrivateJsonStore(tmp_path / "scan")
    )
    controller = CollectorController(
        CollectorStateStore(state_path), clock=lambda: now[0], brain_root=tmp_path / "brain"
    )
    controller.enable(source_id="synthetic", selection=_adapter(root).selection,
                      interval_seconds=60)
    assert controller.sync_due(source_id="synthetic", runtime=runtime,
                               capture_sink=sink).captured_count == 1
    (root / "saved.md").write_text("# Retained second revision\nsynthetic body\n",
                                   encoding="utf-8")
    now[0] = 160
    assert controller.sync_due(source_id="synthetic", runtime=runtime,
                               capture_sink=sink).captured_count == 1
    request = _withdraw_saved_source(tasks, owner)
    retained = _retained_source_snapshot(tasks, owner)
    assert len(cast(tuple[object, ...], retained["captures"])) == 2
    assert len(cast(tuple[object, ...], retained["source_revisions"])) == 2

    (root / "saved.md").unlink()
    now[0] = 220
    assert controller.sync_due(source_id="synthetic", runtime=runtime,
                               capture_sink=sink).captured_count == 0
    assert len(runtime.absence_candidates) == 1
    (root / "saved.md").write_text("# Returned changed revision\nnew synthetic body\n",
                                   encoding="utf-8")
    restarted_tasks, restarted_sink = _revision_sink(tmp_path)
    restarted_runtime = SavedMarkdownCollectorRuntime(
        _adapter(root), store=PrivateJsonStore(tmp_path / "scan")
    )
    restarted = CollectorController(
        CollectorStateStore(state_path), clock=lambda: 280, brain_root=tmp_path / "brain"
    )
    try:
        result = restarted.sync_due(source_id="synthetic", runtime=restarted_runtime,
                                    capture_sink=restarted_sink)
    except T03Error as error:
        assert error.code in {"revision_changed", "source_revision_conflict"}
    else:
        assert result.captured_count == result.duplicate_count == 0
    assert restarted_runtime.absence_candidates == ()
    assert _retained_source_snapshot(restarted_tasks, owner) == retained
    # Retained refusal custody must not poison the next ordinary engine startup.
    recovered_tasks = open_local_engine(tasks.profile)
    assert _retained_source_snapshot(recovered_tasks, owner) == retained
    assert recovered_tasks.sources is not None
    assert recovered_tasks.sources.withdraw(request, authority=owner).lifecycle == "retired"


@pytest.mark.parametrize("uncertain", ["pending", "lost_response"])
def test_uncertain_saved_markdown_delivery_after_owner_withdrawal_stays_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: str,
) -> None:
    root = _root(tmp_path)
    tasks, sink = _revision_sink(tmp_path)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    now = [100]
    state_path = tmp_path / "state.json"
    runtime = SavedMarkdownCollectorRuntime(
        _adapter(root), store=PrivateJsonStore(tmp_path / "scan")
    )
    controller = CollectorController(
        CollectorStateStore(state_path), clock=lambda: now[0], brain_root=tmp_path / "brain"
    )
    controller.enable(source_id="synthetic", selection=_adapter(root).selection,
                      interval_seconds=60)
    assert controller.sync_due(source_id="synthetic", runtime=runtime,
                               capture_sink=sink).captured_count == 1
    (root / "saved.md").write_text("# Uncertain revision\nretained synthetic body\n",
                                   encoding="utf-8")
    original = PublicJobRevisionSink.submit
    submitted: list[bytes] = []

    def uncertain_once(self: PublicJobRevisionSink, delivery: SourceRevisionDelivery
                       ) -> SourceRevisionDeliveryReceipt:
        submitted.append(delivery.custody_bytes())
        if len(submitted) == 1 and uncertain == "pending":
            return SourceRevisionDeliveryReceipt(
                delivery.delivery_id, delivery.envelope_sha256,
                delivery.binding.destination_brain_id, delivery.binding.issuer_epoch,
                None, "operation_pending",
            )
        result = original(self, delivery)
        if len(submitted) == 1:
            raise RuntimeError("synthetic lost response")
        return result

    monkeypatch.setattr(PublicJobRevisionSink, "submit", uncertain_once)
    now[0] = 160
    if uncertain == "pending":
        assert controller.sync_due(source_id="synthetic", runtime=runtime,
                                   capture_sink=sink).outcome.value == "deferred"
    else:
        with pytest.raises(RuntimeError, match="synthetic lost response"):
            controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    assert controller.custody_status("synthetic")["retained_items"] == 1
    request = _withdraw_saved_source(tasks, owner)
    retained = _retained_source_snapshot(tasks, owner)
    expected_count = 1 if uncertain == "pending" else 2
    assert len(cast(tuple[object, ...], retained["captures"])) == expected_count
    assert len(cast(tuple[object, ...], retained["source_revisions"])) == expected_count
    (root / "saved.md").unlink()
    (root / "saved.md").write_text("# Changed during withdrawal\nnew filesystem body\n",
                                   encoding="utf-8")
    restarted_tasks, restarted_sink = _revision_sink(tmp_path)
    restarted = CollectorController(
        CollectorStateStore(state_path), clock=lambda: 160, brain_root=tmp_path / "brain"
    )
    restarted_runtime = SavedMarkdownCollectorRuntime(
        _adapter(root), store=PrivateJsonStore(tmp_path / "scan")
    )
    try:
        result = restarted.sync_due(source_id="synthetic", runtime=restarted_runtime,
                                    capture_sink=restarted_sink)
    except T03Error as error:
        assert uncertain == "pending"
        assert error.code in {"revision_changed", "source_revision_conflict"}
    else:
        if uncertain == "lost_response":
            assert result.outcome.value == "completed"
            assert restarted.custody_status("synthetic")["retained_items"] == 0
        else:
            assert result.captured_count == result.duplicate_count == 0
    assert submitted[0] == submitted[1]
    assert _retained_source_snapshot(restarted_tasks, owner) == retained
    recovered_tasks = open_local_engine(tasks.profile)
    assert _retained_source_snapshot(recovered_tasks, owner) == retained
    assert recovered_tasks.sources is not None
    assert recovered_tasks.sources.withdraw(request, authority=owner).lifecycle == "retired"


def _withdraw_saved_source(
    tasks: EngineTaskSet, owner: EffectiveAuthority,
) -> SourceWithdrawRequest:
    with open_local_database_read_only(tasks.profile) as connection:
        sources = connection.execute("SELECT source_id FROM logical_sources").fetchall()
    assert len(sources) == 1
    assert tasks.sources is not None
    inspection = tasks.sources.inspect(SourceInspectRequest(source_id=sources[0][0]),
                                       authority=owner)
    request = SourceWithdrawRequest(
        operation_id="withdraw.synthetic-saved-markdown",
        source_id=inspection.source_id,
        expected_head=inspection.head_capture_id,
        expected_lifecycle_version=inspection.lifecycle_version,
        brain_id=inspection.destination_brain_id,
        issuer_epoch=inspection.issuer_epoch,
        reason_code="owner_choice",
    )
    receipt = tasks.sources.withdraw(request, authority=owner)
    assert receipt.lifecycle == "retired"
    assert receipt.lifecycle_version == inspection.lifecycle_version + 1
    withdrawn = tasks.sources.inspect(SourceInspectRequest(source_id=inspection.source_id),
                                      authority=owner)
    assert withdrawn.head_capture_id == inspection.head_capture_id
    assert withdrawn.head_version == inspection.head_version
    assert withdrawn.lifecycle == "retired"
    assert withdrawn.availability == "missing"
    assert withdrawn.lifecycle_version == receipt.lifecycle_version
    assert withdrawn.withdrawal_receipt is not None
    assert withdrawn.withdrawal_receipt["receipt_sha256"] == receipt.receipt_sha256
    return request


def _retained_source_snapshot(tasks: EngineTaskSet, owner: EffectiveAuthority) -> dict[str, object]:
    retained: dict[str, object] = {}
    with open_local_database_read_only(tasks.profile) as connection:
        for table in ("captures", "source_revisions", "logical_sources", "source_namespaces",
                      "source_lifecycle_state", "source_lifecycle_operations"):
            retained[table] = tuple(tuple(row) for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ))
        revisions = connection.execute(
            "SELECT capture_id,source_path,source_bytes FROM source_revisions ORDER BY sequence"
        ).fetchall()
    files = tuple((row["source_path"], bytes(row["source_bytes"]),
                   (tasks.profile.root / row["source_path"]).read_bytes()) for row in revisions)
    assert all(expected == actual for _, expected, actual in files)
    retained["files"] = files
    assert tasks.history is not None
    assert tasks.retrieval.search("synthetic") == ()
    for row in revisions:
        assert tasks.retrieval.fetch(row["capture_id"]) is None
    listing = tasks.history.list_history(
        HistoryListRequest(record_id=revisions[0]["capture_id"], limit=25), authority=owner
    ).to_wire()
    assert listing["complete"]
    retained["history"] = listing
    reads = []
    for row in revisions:
        response = tasks.history.read_history(
            RecordReadRequest(record_id=revisions[0]["capture_id"],
                              expected_revision_id=row["capture_id"], target_bytes=4096),
            authority=owner,
        ).to_wire()
        assert response["complete"]
        reads.append(response)
    retained["history_reads"] = tuple(reads)
    return retained


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


@pytest.mark.parametrize("field", [
    "dto_version", "delivery_id", "expected_lifecycle_version",
    "binding.destination_brain_id", "binding.issuer_epoch", "binding.root_fingerprint",
    "binding.accepted_source_id", "binding.namespace.external_id",
    "binding.namespace.connector_name", "binding.namespace.connection_id",
    "binding.namespace.resource_id", "submission.dto_version",
    "submission.expected_head", "submission.expected_control_epoch",
    "submission.revision_key", "submission.canonical_sha256", "submission.ordering",
    "submission.namespace.external_id", "submission.delivery_id",
    "submission.namespace.connector_name", "submission.namespace.connection_id",
    "submission.namespace.resource_id", "submission.file_bytes_base64",
    "submission.capture.payload", "submission.capture.privacy", "submission.capture.title",
    "submission.capture.provenance", "observation.original_sha256",
    "submission.capture.actor_id", "submission.capture.role_claim", "submission.capture.tenant_id",
    "observation.dto_version",
    "observation.transformed_sha256", "observation.normalization_version",
    "observation.privacy_policy_version", "observation.privacy_policy_sha256",
    "observation.admitted_payload_sha256",
])
def test_revision_pending_envelope_tamper_and_changed_retry_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
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
    target = envelope
    components = field.split(".")
    for component in components[:-1]:
        target = cast(dict[str, object], target[component])
    key = components[-1]
    previous = target[key]
    if type(previous) is int:
        target[key] = previous + 1
    elif previous is None:
        target[key] = "capture_00000000-0000-4000-8000-000000000099"
    elif isinstance(previous, dict):
        target[key] = {"tampered": "synthetic"}
    else:
        target[key] = "synthetic-tampered"
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
