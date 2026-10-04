"""Format feasibility only: local fixtures are not independent durability proof."""

from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import (
    PublicJobRevisionSink,
    SourceRevisionDelivery,
    SourceRevisionDeliveryReceipt,
    open_local_engine,
)
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.source_lifecycle_contracts import SourceInspectRequest
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.collector.tests.integration.test_saved_markdown import (
    _intake,
    _retained_source_snapshot,
    _revision_sink,
    _root,
    _withdraw_saved_source,
)


@pytest.mark.parametrize("observed", [False, True], ids=["delivery-v1", "delivery-v2"])
def test_portable_recovers_three_revisions_and_withdrawal_without_primary_or_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed: bool,
) -> None:
    selected = _root(tmp_path)
    tasks, sink = _revision_sink(tmp_path)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    deliveries: list[tuple[SourceRevisionDelivery, SourceRevisionDeliveryReceipt]] = []
    submit = PublicJobRevisionSink.submit

    def retain_terminal(
        self: PublicJobRevisionSink, delivery: SourceRevisionDelivery,
    ) -> SourceRevisionDeliveryReceipt:
        result = submit(self, delivery)
        assert result.source_receipt is not None and result.outcome == "captured"
        deliveries.append((delivery, result))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(PublicJobRevisionSink, "submit", retain_terminal)
        for revision in range(3):
            (selected / "saved.md").write_text(
                f"# synthetic revision {revision}\nExact retained body {revision} 漢字\n",
                encoding="utf-8",
            )
            intake = _intake(selected)
            sink.submit(intake if observed else replace(intake, observation=None))
    assert len(deliveries) == 3
    assert {delivery.dto_version for delivery, _ in deliveries} == {2 if observed else 1}
    withdrawal = _withdraw_saved_source(tasks, owner)
    assert tasks.sources is not None
    inspection = tasks.sources.inspect(
        SourceInspectRequest(source_id=withdrawal.source_id), authority=owner,
    )
    retained = _retained_source_snapshot(tasks, owner)
    terminal = tuple((delivery.custody_bytes(), receipt) for delivery, receipt in deliveries)
    archive = tmp_path / "recovery-record"
    tasks.portability.export(archive, export_id="export_" + str(uuid4()))
    original_snapshot = validated_portable_snapshot(archive)
    assert original_snapshot.manifest["schema_version"] == 8

    # Keep synthetic inputs recoverable but remove every normal primary/sender
    # path before recovery. Import must consume only the exported format.
    unavailable = tmp_path / "unavailable"
    unavailable.mkdir()
    for path in (tasks.profile.root, selected, tmp_path / "revisions"):
        assert path.exists()
        path.rename(unavailable / path.name)
        assert not path.exists()
    del tasks, sink
    restored_root = tmp_path / "restored"
    # Existing public import requires a live caller root. This synthetic blank
    # caller supplies tooling only, not the unavailable primary's data or keys.
    recovery_tool = open_local_engine(compile_single_user_local(tmp_path / "recovery-tool"))
    import_id = "import_" + str(uuid4())
    assert recovery_tool.portability.import_clean(
        archive, restored_root, import_id=import_id,
    ).schema_version == 8
    assert recovery_tool.portability.import_clean(
        archive, restored_root, import_id=import_id,
    ).duplicate
    restored = open_local_engine(compile_single_user_local(restored_root))
    assert restored.sources is not None
    assert restored.sources.inspect(
        SourceInspectRequest(source_id=withdrawal.source_id), authority=owner,
    ) == inspection
    assert asdict(restored.sources.withdraw(withdrawal, authority=owner)) == (
        inspection.withdrawal_receipt
    )
    assert _retained_source_snapshot(restored, owner)["history_reads"] == (
        retained["history_reads"]
    )
    for (delivery, _), (envelope, receipt) in zip(deliveries, terminal, strict=True):
        assert delivery.custody_bytes() == envelope
        restored_sink = restored.sources.public_revision_sink(delivery.binding)
        assert restored_sink.lookup_receipt(delivery) == receipt
        assert restored_sink.submit(delivery) == receipt
    with open_local_database_read_only(restored.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 3
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 3
        )
        assert (
            connection.execute("SELECT count(*) FROM source_lifecycle_operations").fetchone()[0]
            == 1
        )
    again = tmp_path / "reexport"
    restored.portability.export(again, export_id="export_" + str(uuid4()))
    restored_snapshot = validated_portable_snapshot(again)
    assert {
        name: data for name, data in original_snapshot.files.items()
        if name != "portable-manifest.json"
    } == {
        name: data for name, data in restored_snapshot.files.items()
        if name != "portable-manifest.json"
    }
