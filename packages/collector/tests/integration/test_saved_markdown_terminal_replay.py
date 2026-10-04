"""Collector replay classification cannot fabricate engine acceptance."""

import json
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    PublicJobRevisionSink,
    SourceRevisionDelivery,
    SourceRevisionDeliveryReceipt,
)

from open_brain_collector.lifecycle import SavedMarkdownDelivery
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from packages.collector.tests.integration.test_saved_markdown import _intake, _revision_sink, _root


def test_terminal_replay_uses_exact_engine_receipt_and_retained_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, sink = _revision_sink(tmp_path)
    intake = _intake(root)
    first = sink.submit(intake)
    assert isinstance(first, SavedMarkdownDelivery)
    submitted: list[bytes] = []
    verified: list[SourceRevisionDeliveryReceipt] = []
    original_submit = PublicJobRevisionSink.submit
    original_verify = PublicJobRevisionSink.verify_receipt

    def submit(
        self: PublicJobRevisionSink, delivery: SourceRevisionDelivery
    ) -> SourceRevisionDeliveryReceipt:
        submitted.append(delivery.custody_bytes())
        return original_submit(self, delivery)

    def verify(
        self: PublicJobRevisionSink,
        delivery: SourceRevisionDelivery,
        receipt: SourceRevisionDeliveryReceipt,
    ) -> None:
        original_verify(self, delivery, receipt)
        verified.append(receipt)

    monkeypatch.setattr(PublicJobRevisionSink, "submit", submit)
    monkeypatch.setattr(PublicJobRevisionSink, "verify_receipt", verify)
    _, restarted = _revision_sink(tmp_path)
    replay = restarted.submit(intake)
    assert replay.outcome == "duplicate" and replay.replayed
    assert replay.receipt == first.receipt
    assert replay.receipt.outcome == "captured"
    assert submitted == [first.delivery.custody_bytes()]
    assert verified == [first.receipt]
    store = PrivateJsonStore(tmp_path / "revisions")
    retained = store.read(store.names("saved-revision-")[0])
    assert isinstance(retained, dict)
    assert retained["envelope"] == json.loads(first.delivery.custody_bytes())


def test_new_revision_preserves_all_previous_terminal_envelopes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _, sink = _revision_sink(tmp_path)
    deliveries = [sink.submit(_intake(root)).delivery]
    for index in range(2):
        (root / "saved.md").write_text(f"# changed version {index}\nbody {index}\n")
        deliveries.append(sink.submit(_intake(root)).delivery)
    store = PrivateJsonStore(tmp_path / "revisions")
    retained = [store.read(name) for name in store.names("saved-revision-")]
    assert len(retained) == 3
    for delivery in deliveries:
        assert any(
            isinstance(item, dict) and item["envelope"] == json.loads(delivery.custody_bytes())
            for item in retained
        )


def test_terminal_body_capacity_refuses_new_version_without_eviction(tmp_path: Path) -> None:
    root = _root(tmp_path)
    tasks, sink = _revision_sink(tmp_path)
    sink.submit(_intake(root))
    store = PrivateJsonStore(tmp_path / "revisions")
    cache_name = store.names("saved-revision-")[0]
    retained = store.read(cache_name)
    assert isinstance(retained, dict)
    # Synthetic occupied terminal storage counts even though nothing is pending.
    for index in range(255):
        store.write(f"saved-revision-archive-synthetic-{index:03}.json", retained)
    before = {name: store.read(name) for name in store.names("saved-revision-")}
    (root / "saved.md").write_text("# new version\nnew body\n")
    with pytest.raises(LiveSourceError, match="collector_custody_full"):
        sink.submit(_intake(root))
    assert {name: store.read(name) for name in store.names("saved-revision-")} == before
    from open_brain_engine.engine.local_schema import open_local_database_read_only

    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1


def test_compacted_terminal_cache_refuses_without_fabricated_replay(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _, sink = _revision_sink(tmp_path)
    intake = _intake(root)
    first = sink.submit(intake)
    store = PrivateJsonStore(tmp_path / "revisions")
    name = store.names("saved-revision-")[0]
    retained = store.read(name)
    assert isinstance(retained, dict)
    retained["envelope"] = {"delivery_id": first.delivery.delivery_id}
    store.write(name, retained)
    with pytest.raises(LiveSourceError, match="collector_invalid_custody"):
        sink.submit(intake)
    assert store.read(name) == retained
