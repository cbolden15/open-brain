"""Historical receipt truth cannot bypass current collector page eligibility."""

from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.storage.locks import FileLease, LockBusyError

from open_brain_collector.lifecycle import CollectorController, CollectorStateStore
from open_brain_collector.saved_markdown import SavedMarkdownCollectorRuntime
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from packages.collector.tests.integration.test_saved_markdown import _adapter, _intake
from packages.collector.tests.integration.test_saved_markdown_historical import _adopt_saved


def test_genuine_file_reversion_creates_new_revision_not_old_baseline(tmp_path: Path) -> None:
    engine, sink, _, baseline = _adopt_saved(tmp_path)
    path = tmp_path / "selected/saved.md"
    original = path.read_bytes()
    adapter = _adapter(path.parent)
    now = [100]
    store = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(store, clock=lambda: now[0], brain_root=engine.profile.root)
    controller.enable(source_id="synthetic", selection=adapter.selection, interval_seconds=60)
    runtime = SavedMarkdownCollectorRuntime(adapter, store=PrivateJsonStore(tmp_path / "scan"))
    path.write_text("# genuine successor\nnew body\n")
    assert (
        controller.sync_due(
            source_id="synthetic", runtime=runtime, capture_sink=sink
        ).captured_count
        == 1
    )
    path.write_bytes(original)
    now[0] = 160
    result = controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    assert result.captured_count == 1 and result.duplicate_count == 0
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM logical_sources").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT revision_key FROM source_revisions WHERE capture_id=?",
                (baseline.submission.expected_head,),
            ).fetchone()[0]
            is None
        )
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 0
    with pytest.raises(T03Error, match="revision_changed"):
        sink.lookup_baseline(_intake(path.parent), selection_generation="selected.v1")


def test_reverted_file_cannot_revive_withdrawn_successor(tmp_path: Path) -> None:
    engine, sink, _, _ = _adopt_saved(tmp_path)
    path = tmp_path / "selected/saved.md"
    original = path.read_bytes()
    path.write_text("# successor\nnew body\n")
    accepted = sink.submit(_intake(path.parent))
    assert accepted.source_receipt is not None
    binding, revision_sink = sink._revision_capability(_intake(path.parent))
    head = revision_sink.inspect_head()
    assert head.source_id is not None and head.capture_id is not None
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.synthetic.revert",
            source_id=head.source_id,
            expected_head=head.capture_id,
            expected_lifecycle_version=head.lifecycle_version,
            brain_id=binding.destination_brain_id,
            issuer_epoch=binding.issuer_epoch,
            reason_code="owner_choice",
        ),
        authority=owner,
    )
    path.write_bytes(original)
    with pytest.raises(T03Error, match="revision_changed"):
        sink.lookup_baseline(
            _intake(path.parent), selection_generation="selected.v1", allow_successor=True
        )
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 2


def test_baseline_controller_checkpoints_without_new_capture_and_retains_custody(
    tmp_path: Path,
) -> None:
    engine, sink, _, baseline = _adopt_saved(tmp_path)
    adapter = _adapter(tmp_path / "selected")
    runtime = SavedMarkdownCollectorRuntime(adapter, store=PrivateJsonStore(tmp_path / "scan"))
    store = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(store, clock=lambda: 100, brain_root=engine.profile.root)
    controller.enable(source_id="synthetic", selection=adapter.selection, interval_seconds=60)
    result = controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    assert result.duplicate_count == 1 and result.captured_count == 0
    assert controller.custody_status("synthetic")["retained_items"] == 1
    entry = cast(dict[str, object], cast(dict[str, object], store.load()["sources"])["synthetic"])
    assert entry["active_run"] is None and entry["last_success_epoch"] == 100
    known = cast(dict[str, dict[str, object]], runtime._load()["known"])
    assert len(known) == 1
    assert next(iter(known.values()))["head"] == baseline.submission.expected_head
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 0
        )
    # Cancellation after acceptance cannot drop this unprotected baseline body.
    controller.disable("synthetic")
    assert controller.custody_status("synthetic")["retained_items"] == 1


def test_stale_baseline_between_item_lookup_and_page_commit_refuses_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sink, _, _ = _adopt_saved(tmp_path)
    adapter = _adapter(tmp_path / "selected")
    runtime = SavedMarkdownCollectorRuntime(adapter, store=PrivateJsonStore(tmp_path / "scan"))
    store = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(store, clock=lambda: 100, brain_root=engine.profile.root)
    controller.enable(source_id="synthetic", selection=adapter.selection, interval_seconds=60)
    lookup = sink.lookup_baseline
    calls = 0

    def lookup_and_advance(
        intake: SourceRecordIntake,
        *,
        selection_generation: str,
        allow_successor: bool = False,
        expected_capture_id: str | None = None,
    ) -> object:
        nonlocal calls
        result = lookup(
            intake,
            selection_generation=selection_generation,
            allow_successor=allow_successor,
            expected_capture_id=expected_capture_id,
        )
        calls += 1
        if calls == 1:
            with _historical_transaction(engine.profile, lambda: None) as connection:
                connection.execute("UPDATE logical_sources SET route_version=route_version+1")
        return result

    monkeypatch.setattr(sink, "lookup_baseline", lookup_and_advance)
    with pytest.raises(T03Error, match="revision_changed"):
        controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    entry = cast(dict[str, object], cast(dict[str, object], store.load()["sources"])["synthetic"])
    assert entry["active_run"] is not None and entry["last_success_epoch"] is None
    assert runtime._load()["page"] is not None
    assert controller.custody_status("synthetic")["retained_items"] == 1


def test_runtime_acknowledgement_holds_canonical_writer_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sink, _, _ = _adopt_saved(tmp_path)
    adapter = _adapter(tmp_path / "selected")
    runtime = SavedMarkdownCollectorRuntime(adapter)
    controller = CollectorController(
        CollectorStateStore(tmp_path / "state.json"),
        clock=lambda: 100,
        brain_root=engine.profile.root,
    )
    controller.enable(source_id="synthetic", selection=adapter.selection, interval_seconds=60)
    contender = FileLease(
        engine.profile.root / ".open-brain",
        "synthetic-ack-contender",
        parent_root_identity=engine.profile.root_identity,
    )
    acknowledge = runtime.acknowledge_page
    reached = False

    def assert_fenced_ack() -> None:
        nonlocal reached
        reached = True
        with pytest.raises(LockBusyError), contender.acquire_shared_writer():
            pytest.fail("writer admitted during runtime acknowledgement")
        acknowledge()

    monkeypatch.setattr(runtime, "acknowledge_page", assert_fenced_ack)
    controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    assert reached
