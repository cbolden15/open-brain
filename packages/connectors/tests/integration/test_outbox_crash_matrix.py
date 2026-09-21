"""The O6 crash matrix: every boundary from enqueue through body cleanup.

Each scenario proves that the committed O2 store, O3 drain, O4 transports,
and O5 owner operations preserve exactly one of the three gate outcomes:
exactly-once canonical acceptance against a real disposable Brain (counted
as capture rows through ``SyntheticTransport``), a recoverable quarantined
item (body intact, owner retry or conversion still possible), or an explicit
metadata-only terminal record.

Crash injection uses the store's documented ``_write_hooks`` seam with a
non-OSError sentinel; restarts re-open the ``OutboxStore`` (and, where a
delivery replays, the disposable Brain) in a fresh process through the probe
scripts written to ``tmp_path``. Every assertion about on-disk state reads
the item files directly.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, EngineTaskSet

from open_brain.profile import compile_single_user_local
from open_brain_connectors.outbox import store as outbox_store
from open_brain_connectors.outbox.contracts import (
    DeliveryEnvelope,
    TerminalReceipt,
    TerminalReceiptStatus,
)
from open_brain_connectors.outbox.drain import (
    DRAIN_LEASE_NAME,
    DeliveryFailure,
    DrainResult,
    run_drain_cycle,
)
from open_brain_connectors.outbox.owner_ops import (
    OwnerOperationError,
    owner_convert,
    owner_discard,
    owner_retry,
)
from open_brain_connectors.outbox.store import (
    TERMINAL_METADATA_HEADROOM_BYTES,
    EnqueueResult,
    OutboxItemState,
    OutboxStore,
)
from open_brain_connectors.outbox.transport import (
    SyntheticStartupPolicy,
    SyntheticTransport,
)

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
BRAIN_ID = derive_brain_id(TENANT_ID)
PRINCIPAL_ID = "crash-matrix-principal"
ALLOWED_TIERS = frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL})
PAYLOAD_TEXT = "Synthetic crash matrix body"
ENQUEUED_AT = "2026-09-21T12:00:00Z"
NOW = datetime(2026, 9, 21, 12, 30, 0, tzinfo=UTC)
RETRY_AT = "2026-09-21T12:45:00Z"
CONVERT_AT = "2026-09-21T12:50:00Z"
CONVERT_NOW = datetime(2026, 9, 21, 12, 50, 0, tzinfo=UTC)
MAX_BYTES = 1024 * 1024
WRITE_HOOK_STAGES = (
    "after_temp_write",
    "after_temp_fsync",
    "after_rename",
    "after_directory_fsync",
)

_STORE_PROBE_SOURCE = """\
import json
import sys
from pathlib import Path

from open_brain_connectors.outbox.contracts import DeliveryEnvelope
from open_brain_connectors.outbox.store import OutboxStore

store = OutboxStore(Path(sys.argv[1]), max_items=int(sys.argv[2]), max_bytes=int(sys.argv[3]))
report = {}
if len(sys.argv) > 4:
    envelope = DeliveryEnvelope.from_dict(json.loads(Path(sys.argv[4]).read_bytes()))
    report["enqueue"] = store.enqueue(envelope).value
report["scan"] = [
    {
        "delivery_id": item.delivery_id,
        "state": item.state.value,
        "size_bytes": item.size_bytes,
    }
    for item in store.scan()
]
status = store.status()
report["status"] = {
    "items": status.items,
    "queued_items": status.queued_items,
    "quarantined_items": status.quarantined_items,
    "terminal_items": status.terminal_items,
    "corrupt_items": status.corrupt_items,
    "corrupt_size_bytes": status.corrupt_size_bytes,
    "size_bytes": status.size_bytes,
}
sys.stdout.write(json.dumps(report, sort_keys=True) + "\\n")
"""

_DRAIN_PROBE_SOURCE = """\
import json
import sqlite3
import sys
from pathlib import Path

from open_brain.profile import compile_single_user_local
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine

from open_brain_connectors.outbox.drain import run_drain_cycle
from open_brain_connectors.outbox.store import OutboxStore
from open_brain_connectors.outbox.transport import (
    SyntheticStartupPolicy,
    SyntheticTransport,
)

profile = compile_single_user_local(Path(sys.argv[1]))
tasks = BrainEngine.open(profile).tasks
connection = sqlite3.connect(profile.root / ".open-brain/state/phase1.sqlite3")
row = connection.execute("SELECT brain_id, issuer_epoch FROM brain_identity").fetchone()
connection.close()
policy = SyntheticStartupPolicy(
    destination_brain_id=row[0],
    issuer_epoch=row[1],
    tenant_id=profile.tenant_id,
    principal_id="crash-matrix-principal",
    allowed_capture_tiers=frozenset(
        {PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL}
    ),
)
transport = SyntheticTransport(tasks, policy)
store = OutboxStore(Path(sys.argv[2]), max_items=int(sys.argv[3]), max_bytes=int(sys.argv[4]))
summary = run_drain_cycle(store, transport, max_batch_items=16, max_batch_bytes=1048576)
sys.stdout.write(
    json.dumps(
        {
            "result": summary.result.value,
            "accepted": summary.accepted,
            "duplicate": summary.duplicate,
            "delivery_attempts": summary.delivery_attempts,
        }
    )
    + "\\n")
"""

_LEASE_HOLDER_SOURCE = """\
import json
import sys
import time
from pathlib import Path

from open_brain_connectors.outbox.contracts import (
    DeliveryEnvelope,
    TerminalReceipt,
    TerminalReceiptStatus,
)
from open_brain_connectors.outbox.drain import run_drain_cycle
from open_brain_connectors.outbox.store import OutboxStore

ready = Path(sys.argv[1])
hold_seconds = float(sys.argv[2])
store = OutboxStore(Path(sys.argv[3]), max_items=int(sys.argv[4]), max_bytes=int(sys.argv[5]))


def holder(envelope):
    ready.write_text("held", encoding="utf-8")
    time.sleep(hold_seconds)
    return TerminalReceipt(
        status=TerminalReceiptStatus.ACCEPTED,
        brain_id=envelope.destination_brain_id,
        issuer_epoch=envelope.expected_issuer_epoch,
        delivery_id=envelope.delivery_id,
        request_digest=envelope.request_digest,
        final_admitted_tier=envelope.requested_tier,
    )


summary = run_drain_cycle(store, holder, max_batch_items=16, max_batch_bytes=1048576)
sys.stdout.write(
    json.dumps({"result": summary.result.value, "accepted": summary.accepted}) + "\\n"
)
"""


class _Crash(Exception):
    """Sentinel crash raised from an injected write hook (never an OSError)."""


def _brain(tmp_path: Path, name: str = "brain") -> tuple[EngineTaskSet, str, int, str, Path]:
    """One disposable real Brain; returns tasks, identity, epoch, tenant, root."""
    profile = compile_single_user_local(tmp_path / name)
    tasks = BrainEngine.open(profile).tasks
    connection = sqlite3.connect(profile.root / ".open-brain/state/phase1.sqlite3")
    try:
        row = connection.execute("SELECT brain_id, issuer_epoch FROM brain_identity").fetchone()
    finally:
        connection.close()
    assert row is not None
    return tasks, str(row[0]), int(row[1]), profile.tenant_id, profile.root


def _capture_rows(brain_root: Path, delivery_id: str | None = None) -> int:
    """Count real capture rows in a disposable Brain, optionally per delivery."""
    connection = sqlite3.connect(brain_root / ".open-brain/state/phase1.sqlite3")
    try:
        if delivery_id is None:
            row = connection.execute("SELECT COUNT(*) FROM captures").fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) FROM captures WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _policy(
    brain_id: str, issuer_epoch: int, tenant_id: str, *, principal_id: str = PRINCIPAL_ID
) -> SyntheticStartupPolicy:
    return SyntheticStartupPolicy(
        destination_brain_id=brain_id,
        issuer_epoch=issuer_epoch,
        tenant_id=tenant_id,
        principal_id=principal_id,
        allowed_capture_tiers=ALLOWED_TIERS,
    )


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(
    *,
    destination_brain_id: str = BRAIN_ID,
    issuer_epoch: int = 7,
    tenant_id: str = TENANT_ID,
    delivery_id: str = "delivery.matrix-001",
    payload_text: str = PAYLOAD_TEXT,
    enqueued_at: str = ENQUEUED_AT,
    retry_age_limit_seconds: int = 86400,
    retry_attempt_limit: int = 8,
    attempts: int = 0,
) -> DeliveryEnvelope:
    envelope = DeliveryEnvelope.create(
        destination_brain_id=destination_brain_id,
        expected_issuer_epoch=issuer_epoch,
        tenant_id=tenant_id,
        principal_id=PRINCIPAL_ID,
        delivery_id=delivery_id,
        requested_tier=PrivacyTier.WORK,
        policy_ref="policy.synthetic-v1",
        payload={"family": "text", "text": payload_text},
        enqueued_at=enqueued_at,
        retry_age_limit_seconds=retry_age_limit_seconds,
        retry_attempt_limit=retry_attempt_limit,
    )
    if attempts:
        values = envelope.to_dict()
        values["attempts"] = attempts
        values["last_attempt_at"] = "2026-09-21T12:10:00Z"
        values["last_attempt_result"] = "rate_limited"
        envelope = DeliveryEnvelope.from_dict(values)
    return envelope


def _store(
    tmp_path: Path, name: str = "outbox", *, max_items: int = 64, max_bytes: int = MAX_BYTES
) -> OutboxStore:
    return OutboxStore(tmp_path / name, max_items=max_items, max_bytes=max_bytes)


def _clock(at: datetime) -> Callable[[], datetime]:
    def tick() -> datetime:
        return at

    return tick


def _receipt(envelope: DeliveryEnvelope) -> TerminalReceipt:
    return TerminalReceipt(
        status=TerminalReceiptStatus.ACCEPTED,
        brain_id=envelope.destination_brain_id,
        issuer_epoch=envelope.expected_issuer_epoch,
        delivery_id=envelope.delivery_id,
        request_digest=envelope.request_digest,
        final_admitted_tier=envelope.requested_tier,
    )


def _crash_at(monkeypatch: pytest.MonkeyPatch, *stages: str) -> None:
    def hook(target: Path) -> None:
        raise _Crash(str(target))

    monkeypatch.setattr(outbox_store, "_write_hooks", {stage: hook for stage in stages})


def _item_path(store: OutboxStore, delivery_id: str) -> Path:
    return store.directory / f"{delivery_id}.json"


def _item_json(raw: bytes) -> dict[str, object]:
    return cast(dict[str, object], json.loads(raw.decode("utf-8")))


def _read_envelope(store: OutboxStore, delivery_id: str) -> DeliveryEnvelope:
    return DeliveryEnvelope.from_dict(_item_json(_item_path(store, delivery_id).read_bytes()))


def _visible_files(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if not name.startswith("."))


def _temp_files(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if name.endswith(".tmp"))


def _write_probes(tmp_path: Path) -> tuple[Path, Path]:
    store_probe = tmp_path / "o6_store_probe.py"
    store_probe.write_text(_STORE_PROBE_SOURCE, encoding="utf-8")
    drain_probe = tmp_path / "o6_drain_probe.py"
    drain_probe.write_text(_DRAIN_PROBE_SOURCE, encoding="utf-8")
    return store_probe, drain_probe


def _run_probe(script: Path, *args: object) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-I", str(script), *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert completed.stdout is not None
    return cast(dict[str, object], json.loads(completed.stdout))


def _probe_status(report: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], report["status"])


def _probe_scan(report: dict[str, object]) -> dict[str, str]:
    entries = cast(list[dict[str, object]], report["scan"])
    return {str(entry["delivery_id"]): str(entry["state"]) for entry in entries}


def _enqueue_file(tmp_path: Path, envelope: DeliveryEnvelope) -> Path:
    path = tmp_path / f"{envelope.delivery_id}.envelope.json"
    path.write_bytes(json.dumps(envelope.to_dict()).encode("utf-8"))
    return path


@pytest.mark.parametrize("stage", WRITE_HOOK_STAGES)
def test_scenario_01_enqueue_crash_at_each_write_hook_stage_then_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    store = _store(tmp_path)
    envelope = _envelope(enqueued_at=_stamp())
    _crash_at(monkeypatch, stage)
    with pytest.raises(_Crash):
        store.enqueue(envelope)

    store_probe, _ = _write_probes(tmp_path)
    restarted = _run_probe(store_probe, store.directory, 8, 65536)
    if stage in {"after_temp_write", "after_temp_fsync"}:
        # Crash before the rename: no item exists and a fresh process agrees.
        assert _visible_files(store.directory) == []
        assert _probe_scan(restarted) == {}
        assert _probe_status(restarted)["items"] == 0
    else:
        # Crash after the rename: a complete item is on disk even though the
        # caller never observed the queued result.
        assert _visible_files(store.directory) == ["delivery.matrix-001.json"]
        assert _read_envelope(store, envelope.delivery_id) == envelope
        assert _probe_status(restarted)["items"] == 1
        assert _probe_status(restarted)["queued_items"] == 1

    recovered = _run_probe(
        store_probe, store.directory, 8, 65536, _enqueue_file(tmp_path, envelope)
    )
    expected = "queued" if stage in {"after_temp_write", "after_temp_fsync"} else "already_queued"
    assert recovered["enqueue"] == expected
    final = _run_probe(store_probe, store.directory, 8, 65536)
    assert _probe_status(final)["items"] == 1
    assert _probe_status(final)["queued_items"] == 1
    assert _probe_scan(final) == {"delivery.matrix-001": "queued"}


def test_scenario_02_drain_crash_after_acceptance_before_terminal_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, brain_id, epoch, tenant_id, brain_root = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    store = _store(tmp_path)
    envelope = _envelope(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        tenant_id=tenant_id,
        enqueued_at=_stamp(),
    )
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    _crash_at(monkeypatch, "after_temp_write")
    with pytest.raises(_Crash):
        run_drain_cycle(store, transport, max_batch_items=4, max_batch_bytes=MAX_BYTES)

    # The destination accepted exactly once and the body survived the crash.
    assert _capture_rows(brain_root) == 1
    crashed_raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in crashed_raw
    assert not (store.directory / DRAIN_LEASE_NAME).exists()

    # Restart in a fresh process: the replay returns duplicate...
    store_probe, drain_probe = _write_probes(tmp_path)
    replayed = _run_probe(drain_probe, brain_root, store.directory, 8, 65536)
    assert replayed["result"] == "completed"
    assert replayed["duplicate"] == 1
    assert replayed["accepted"] == 0
    assert replayed["delivery_attempts"] == 1

    # ...the body is removed only after the verified duplicate receipt, and
    # the destination still holds exactly one capture row.
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") not in raw
    record = _item_json(raw)
    assert record["record_version"] == "outbox.terminal.v1"
    assert record["terminal_kind"] == "receipt"
    assert record["receipt"] is not None
    receipt = cast(dict[str, object], record["receipt"])
    assert receipt["status"] == "duplicate"
    assert _capture_rows(brain_root) == 1
    assert _probe_scan(_run_probe(store_probe, store.directory, 8, 65536)) == {
        "delivery.matrix-001": "terminal"
    }


def test_scenario_03_transport_timeout_retains_body_then_later_drain_succeeds(
    tmp_path: Path,
) -> None:
    tasks, brain_id, epoch, tenant_id, brain_root = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    store = _store(tmp_path)
    envelope = _envelope(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        tenant_id=tenant_id,
        enqueued_at=_stamp(),
    )
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    def timing_out(item: DeliveryEnvelope) -> TerminalReceipt | DeliveryFailure:
        raise TimeoutError("synthetic transport timeout")

    first = run_drain_cycle(store, timing_out, max_batch_items=4, max_batch_bytes=MAX_BYTES)
    assert first.transport_errors == 1
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    value = _item_json(raw)
    assert value["attempts"] == 1
    assert value["last_attempt_result"] == "transport_error"
    assert value["quarantine_reason"] is None
    assert _capture_rows(brain_root) == 0

    second = run_drain_cycle(store, transport, max_batch_items=4, max_batch_bytes=MAX_BYTES)
    assert second.accepted == 1
    assert _capture_rows(brain_root) == 1
    final_raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") not in final_raw
    assert _item_json(final_raw)["terminal_kind"] == "receipt"


def test_scenario_04_duplicate_reply_returns_the_same_accepted_receipt_twice(
    tmp_path: Path,
) -> None:
    tasks, brain_id, epoch, tenant_id, brain_root = _brain(tmp_path)
    real = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    store = _store(tmp_path)
    envelope = _envelope(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        tenant_id=tenant_id,
        enqueued_at=_stamp(),
    )
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    replied: list[TerminalReceipt] = []

    def replying_twice(item: DeliveryEnvelope) -> TerminalReceipt | DeliveryFailure:
        if not replied:
            # First send: the destination commits, then the reply is lost to
            # a transient refusal; the retry hands back the same accepted
            # receipt, so the transport replies with it twice in total.
            outcome = real(item)
            assert isinstance(outcome, TerminalReceipt)
            replied.append(outcome)
            return DeliveryFailure(code="admission_busy", retryable=True)
        return replied[0]

    first = run_drain_cycle(store, replying_twice, max_batch_items=4, max_batch_bytes=MAX_BYTES)
    assert first.retried == 1
    assert _capture_rows(brain_root) == 1
    retained = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in retained

    second = run_drain_cycle(store, replying_twice, max_batch_items=4, max_batch_bytes=MAX_BYTES)
    assert second.accepted == 1
    assert second.duplicate == 0
    assert _capture_rows(brain_root) == 1
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    record = _item_json(raw)
    assert record["terminal_kind"] == "receipt"
    assert record["receipt"] == replied[0].to_dict()
    assert PAYLOAD_TEXT.encode("utf-8") not in raw


def test_scenario_05_mismatched_brain_receipt_quarantines_with_no_destination_capture(
    tmp_path: Path,
) -> None:
    destination_tasks, dest_id, dest_epoch, dest_tenant, dest_root = _brain(tmp_path, "destination")
    wrong_tasks, wrong_id, wrong_epoch, wrong_tenant, wrong_root = _brain(tmp_path, "wrong-brain")
    assert wrong_id != dest_id
    wrong_transport = SyntheticTransport(wrong_tasks, _policy(wrong_id, wrong_epoch, wrong_tenant))
    store = _store(tmp_path)
    envelope = _envelope(
        destination_brain_id=dest_id,
        issuer_epoch=dest_epoch,
        tenant_id=dest_tenant,
        enqueued_at=_stamp(),
    )
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    def delivered_to_the_wrong_brain(item: DeliveryEnvelope) -> TerminalReceipt | DeliveryFailure:
        return wrong_transport(item)

    summary = run_drain_cycle(
        store, delivered_to_the_wrong_brain, max_batch_items=4, max_batch_bytes=MAX_BYTES
    )
    assert summary.quarantined_receipt_mismatch == 1
    assert summary.accepted == 0
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    assert _item_json(raw)["quarantine_reason"] == "receipt_mismatch"
    assert _capture_rows(dest_root) == 0
    assert _capture_rows(wrong_root) == 1
    assert store.status().quarantined_items == 1


def test_scenario_06_stale_epoch_receipt_quarantines_with_no_capture(
    tmp_path: Path,
) -> None:
    tasks, brain_id, _epoch, tenant_id, brain_root = _brain(tmp_path)
    store = _store(tmp_path)
    # The client expects epoch 7; a stale reply from epoch 6 must not remove
    # the body even though the Brain ID matches.
    envelope = _envelope(
        destination_brain_id=brain_id,
        issuer_epoch=7,
        tenant_id=tenant_id,
        enqueued_at=_stamp(),
    )
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    stale = TerminalReceipt(
        status=TerminalReceiptStatus.ACCEPTED,
        brain_id=brain_id,
        issuer_epoch=6,
        delivery_id=envelope.delivery_id,
        request_digest=envelope.request_digest,
        final_admitted_tier=envelope.requested_tier,
    )

    def replying_with_a_stale_epoch(item: DeliveryEnvelope) -> TerminalReceipt:
        return stale

    summary = run_drain_cycle(
        store, replying_with_a_stale_epoch, max_batch_items=4, max_batch_bytes=MAX_BYTES
    )
    assert summary.quarantined_receipt_mismatch == 1
    assert summary.accepted == 0
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    assert _item_json(raw)["quarantine_reason"] == "receipt_mismatch"
    assert _capture_rows(brain_root) == 0
    assert store.status().quarantined_items == 1


def test_scenario_07_partial_write_truncated_item_surfaces_corrupt_after_restart(
    tmp_path: Path,
) -> None:
    tasks, brain_id, epoch, tenant_id, brain_root = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    store = _store(tmp_path, max_items=2)
    good = _envelope(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        tenant_id=tenant_id,
        delivery_id="delivery.matrix-good",
        enqueued_at=_stamp(),
    )
    assert store.enqueue(good) is EnqueueResult.QUEUED
    complete = _item_path(store, "delivery.matrix-good").read_bytes()
    truncated = complete[: len(complete) // 2]
    partial = store.directory / "delivery.matrix-partial.json"
    partial.write_bytes(truncated)

    store_probe, _ = _write_probes(tmp_path)
    restarted = _run_probe(store_probe, store.directory, 2, 65536)
    assert _probe_scan(restarted) == {
        "delivery.matrix-good": "queued",
        "delivery.matrix-partial": "corrupt",
    }
    status = _probe_status(restarted)
    assert status["corrupt_items"] == 1
    assert status["corrupt_size_bytes"] == len(truncated)

    # The corrupt item counts against capacity: the box is full at two items.
    extra = _envelope(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        tenant_id=tenant_id,
        delivery_id="delivery.matrix-extra",
        enqueued_at=_stamp(),
    )
    refused = _run_probe(store_probe, store.directory, 2, 65536, _enqueue_file(tmp_path, extra))
    assert refused["enqueue"] == "outbox_full"

    # It also never blocks the healthy item.
    summary = run_drain_cycle(store, transport, max_batch_items=4, max_batch_bytes=MAX_BYTES)
    assert summary.accepted == 1
    assert _capture_rows(brain_root) == 1
    assert partial.read_bytes() == truncated
    assert store.status().corrupt_items == 1


def test_scenario_08_second_drain_during_a_held_lease_returns_busy(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    before = _item_path(store, envelope.delivery_id).read_bytes()
    ready = tmp_path / "lease-held"
    holder_script = tmp_path / "o6_lease_holder.py"
    holder_script.write_text(_LEASE_HOLDER_SOURCE, encoding="utf-8")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(holder_script),
            str(ready),
            "5",
            str(store.directory),
            "8",
            "65536",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "lease holder never acquired the lease"

        def accepting(item: DeliveryEnvelope) -> TerminalReceipt:
            return _receipt(item)

        busy = run_drain_cycle(store, accepting, max_batch_items=4, max_batch_bytes=MAX_BYTES)
        assert busy.result is DrainResult.DRAIN_BUSY
        assert busy.admitted_items == 0
        assert busy.delivery_attempts == 0
        assert _item_path(store, envelope.delivery_id).read_bytes() == before
        assert (store.directory / DRAIN_LEASE_NAME).exists()
    finally:
        stdout, stderr = holder.communicate(timeout=60)
    assert holder.returncode == 0, stderr
    holder_report = cast(dict[str, object], json.loads(stdout or "{}"))
    assert holder_report["accepted"] == 1
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") not in raw
    assert _item_json(raw)["terminal_kind"] == "receipt"
    assert not (store.directory / DRAIN_LEASE_NAME).exists()


def test_scenario_09_queue_exhaustion_returns_outbox_full_with_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Item cap: exactly max_items admitted, the next enqueue is refused.
    item_capped = _store(tmp_path, "item-capped", max_items=1)
    assert item_capped.enqueue(_envelope(delivery_id="delivery.matrix-001")) is (
        EnqueueResult.QUEUED
    )
    # Byte cap: exactly max_bytes minus headroom admitted, the next refused.
    sizing = _store(tmp_path, "sizing")
    assert sizing.enqueue(_envelope(delivery_id="delivery.matrix-001")) is EnqueueResult.QUEUED
    item_size = _item_path(sizing, "delivery.matrix-001").stat().st_size
    byte_capped = _store(
        tmp_path, "byte-capped", max_items=8, max_bytes=item_size + TERMINAL_METADATA_HEADROOM_BYTES
    )
    assert byte_capped.enqueue(_envelope(delivery_id="delivery.matrix-001")) is (
        EnqueueResult.QUEUED
    )
    snapshots = {
        store.directory: {
            name: (store.directory / name).read_bytes() for name in _visible_files(store.directory)
        }
        for store in (item_capped, byte_capped)
    }

    # A crash hook armed during a refused enqueue must never fire: capacity
    # is decided before any write begins.
    fired: list[str] = []

    def hook(target: Path) -> None:
        fired.append(str(target))
        raise _Crash(str(target))

    monkeypatch.setattr(outbox_store, "_write_hooks", {stage: hook for stage in WRITE_HOOK_STAGES})
    assert item_capped.enqueue(_envelope(delivery_id="delivery.matrix-002")) is (
        EnqueueResult.OUTBOX_FULL
    )
    assert byte_capped.enqueue(_envelope(delivery_id="delivery.matrix-002")) is (
        EnqueueResult.OUTBOX_FULL
    )
    assert fired == []
    for store in (item_capped, byte_capped):
        assert _temp_files(store.directory) == []
        assert _visible_files(store.directory) == ["delivery.matrix-001.json"]
        assert snapshots[store.directory] == {
            name: (store.directory / name).read_bytes() for name in _visible_files(store.directory)
        }
        assert store.status().items == 1


def test_scenario_10_age_exhaustion_quarantines_before_any_attempt(tmp_path: Path) -> None:
    tasks, brain_id, epoch, tenant_id, brain_root = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    seen: list[str] = []

    def observing(item: DeliveryEnvelope) -> TerminalReceipt | DeliveryFailure:
        seen.append(item.delivery_id)
        return transport(item)

    store = _store(tmp_path)
    envelope = _envelope(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        tenant_id=tenant_id,
        retry_age_limit_seconds=1800,
    )
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    summary = run_drain_cycle(
        store, observing, max_batch_items=4, max_batch_bytes=MAX_BYTES, clock=_clock(NOW)
    )
    assert summary.quarantined_age_exhausted == 1
    assert summary.delivery_attempts == 0
    assert seen == []
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    assert _item_json(raw)["quarantine_reason"] == "age_exhausted"
    assert _capture_rows(brain_root) == 0


def test_scenario_11_attempt_exhaustion_quarantines_before_any_attempt(
    tmp_path: Path,
) -> None:
    tasks, brain_id, epoch, tenant_id, brain_root = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    seen: list[str] = []

    def observing(item: DeliveryEnvelope) -> TerminalReceipt | DeliveryFailure:
        seen.append(item.delivery_id)
        return transport(item)

    store = _store(tmp_path)
    envelope = _envelope(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        tenant_id=tenant_id,
        retry_attempt_limit=2,
        attempts=2,
    )
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    summary = run_drain_cycle(
        store, observing, max_batch_items=4, max_batch_bytes=MAX_BYTES, clock=_clock(NOW)
    )
    assert summary.quarantined_attempts_exhausted == 1
    assert summary.delivery_attempts == 0
    assert seen == []
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    assert _item_json(raw)["quarantine_reason"] == "attempts_exhausted"
    assert _capture_rows(brain_root) == 0


def test_scenario_12_owner_retry_of_an_age_exhausted_item_drains_successfully(
    tmp_path: Path,
) -> None:
    tasks, brain_id, epoch, tenant_id, brain_root = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    store = _store(tmp_path)
    envelope = _envelope(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        tenant_id=tenant_id,
        retry_age_limit_seconds=1800,
    )
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    first = run_drain_cycle(
        store, transport, max_batch_items=4, max_batch_bytes=MAX_BYTES, clock=_clock(NOW)
    )
    assert first.quarantined_age_exhausted == 1
    assert _capture_rows(brain_root) == 0

    item = owner_retry(store, envelope.delivery_id, now=RETRY_AT)
    assert item.state is OutboxItemState.QUEUED

    second = run_drain_cycle(
        store,
        transport,
        max_batch_items=4,
        max_batch_bytes=MAX_BYTES,
        clock=_clock(datetime(2026, 9, 21, 12, 45, 0, tzinfo=UTC)),
    )
    assert second.accepted == 1
    assert _capture_rows(brain_root) == 1
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") not in raw
    record = _item_json(raw)
    assert record["terminal_kind"] == "receipt"
    assert record["delivery_id"] == envelope.delivery_id


def test_scenario_13_destination_conversion_drains_to_a_second_brain_with_lineage(
    tmp_path: Path,
) -> None:
    origin_tasks, origin_id, origin_epoch, origin_tenant, origin_root = _brain(
        tmp_path, "origin-brain"
    )
    origin_transport = SyntheticTransport(
        origin_tasks, _policy(origin_id, origin_epoch, origin_tenant)
    )
    # The destination-bound digest binds the tenant, and a fresh Brain's ID
    # is derived from its tenant, so a second Brain that can verify a
    # converted envelope shares the original identity file (same tenant and
    # Brain ID) while keeping its own capture state. The conversion rebinds
    # the epoch, which the synthetic authority controls directly.
    target_root = tmp_path / "target-brain"
    target_root.mkdir(parents=True)
    (target_root / "brain.toml").write_bytes((origin_root / "brain.toml").read_bytes())
    converted_epoch = origin_epoch + 1
    target_tasks, target_id, _target_epoch, target_tenant, target_state_root = _brain(
        tmp_path, "target-brain"
    )
    assert target_id == origin_id
    assert target_tenant == origin_tenant
    target_transport = SyntheticTransport(
        target_tasks, _policy(origin_id, converted_epoch, target_tenant)
    )

    store = _store(tmp_path)
    envelope = _envelope(
        destination_brain_id=origin_id,
        issuer_epoch=origin_epoch,
        tenant_id=origin_tenant,
        retry_age_limit_seconds=1800,
    )
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    first = run_drain_cycle(
        store, origin_transport, max_batch_items=4, max_batch_bytes=MAX_BYTES, clock=_clock(NOW)
    )
    assert first.quarantined_age_exhausted == 1

    conversion = owner_convert(
        store,
        envelope.delivery_id,
        destination_brain_id=origin_id,
        issuer_epoch=converted_epoch,
        now=CONVERT_AT,
    )
    assert conversion.result is EnqueueResult.QUEUED
    assert conversion.new_delivery_id != envelope.delivery_id
    converted = _read_envelope(store, conversion.new_delivery_id)
    assert converted.lineage_delivery_id == envelope.delivery_id
    assert converted.expected_issuer_epoch == converted_epoch
    assert converted.request_digest != envelope.request_digest

    second = run_drain_cycle(
        store,
        target_transport,
        max_batch_items=4,
        max_batch_bytes=MAX_BYTES,
        clock=_clock(CONVERT_NOW),
    )
    assert second.accepted == 1
    assert _capture_rows(target_state_root) == 1
    assert _capture_rows(target_state_root, conversion.new_delivery_id) == 1
    assert _capture_rows(origin_root) == 0

    # The original stays quarantined with its body intact.
    survivor = _read_envelope(store, envelope.delivery_id)
    assert survivor.quarantine_reason == "age_exhausted"
    assert PAYLOAD_TEXT.encode("utf-8") in _item_path(store, envelope.delivery_id).read_bytes()
    terminal_raw = _item_path(store, conversion.new_delivery_id).read_bytes()
    terminal = _item_json(terminal_raw)
    assert terminal["terminal_kind"] == "receipt"
    assert terminal["delivery_id"] == conversion.new_delivery_id
    converted_receipt = cast(dict[str, object], terminal["receipt"])
    assert converted_receipt["issuer_epoch"] == converted_epoch
    assert PAYLOAD_TEXT.encode("utf-8") not in terminal_raw


def test_scenario_14_owner_confirmed_discard_leaves_a_metadata_only_record(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    envelope = _envelope(retry_attempt_limit=2, attempts=2)
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    seen: list[str] = []

    def observing(item: DeliveryEnvelope) -> TerminalReceipt:
        seen.append(item.delivery_id)
        return _receipt(item)

    exhausted = run_drain_cycle(
        store, observing, max_batch_items=4, max_batch_bytes=MAX_BYTES, clock=_clock(NOW)
    )
    assert exhausted.quarantined_attempts_exhausted == 1
    assert seen == []

    with pytest.raises(OwnerOperationError):
        owner_discard(store, envelope.delivery_id, confirm=False, now=RETRY_AT)
    assert _read_envelope(store, envelope.delivery_id).payload is not None

    item = owner_discard(store, envelope.delivery_id, confirm=True, now=RETRY_AT)
    assert item.state is OutboxItemState.TERMINAL
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    record = _item_json(raw)
    assert record["record_version"] == "outbox.terminal.v1"
    assert record["terminal_kind"] == "discard"
    assert record["receipt"] is None
    assert record["request_digest"] == envelope.request_digest
    for name in _visible_files(store.directory):
        assert PAYLOAD_TEXT.encode("utf-8") not in (store.directory / name).read_bytes()
    assert store.status().terminal_items == 1


@pytest.mark.parametrize("stage", WRITE_HOOK_STAGES)
def test_scenario_15_full_capacity_crash_at_each_write_hook_stage_during_final_admitted_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    sizing = _store(tmp_path, "sizing")
    assert sizing.enqueue(_envelope(delivery_id="delivery.cap-001")) is EnqueueResult.QUEUED
    item_size = _item_path(sizing, "delivery.cap-001").stat().st_size
    max_bytes = 3 * item_size + TERMINAL_METADATA_HEADROOM_BYTES
    store = _store(tmp_path, "capped", max_items=3, max_bytes=max_bytes)
    originals: dict[str, bytes] = {}
    for suffix in ("001", "002"):
        delivery_id = f"delivery.cap-{suffix}"
        assert store.enqueue(_envelope(delivery_id=delivery_id)) is EnqueueResult.QUEUED
        originals[delivery_id] = _item_path(store, delivery_id).read_bytes()
    final = _envelope(delivery_id="delivery.cap-003")
    overflow = _envelope(delivery_id="delivery.cap-004")

    _crash_at(monkeypatch, stage)
    with pytest.raises(_Crash):
        store.enqueue(final)

    store_probe, _ = _write_probes(tmp_path)
    restarted = _run_probe(store_probe, store.directory, 3, max_bytes)
    before_rename = stage in WRITE_HOOK_STAGES[:2]
    assert _probe_status(restarted)["items"] == (2 if before_rename else 3)
    if before_rename:
        # Crash before the rename: the final enqueue never reached the disk;
        # a fresh process still admits it under the same caps.
        assert _probe_scan(restarted) == {
            "delivery.cap-001": "queued",
            "delivery.cap-002": "queued",
        }
        recovered = _run_probe(
            store_probe, store.directory, 3, max_bytes, _enqueue_file(tmp_path, final)
        )
        assert recovered["enqueue"] == "queued"
    else:
        # Crash after the rename: the complete item is on disk, and the
        # restart reports it queued without any overwrite of the originals.
        assert _probe_scan(restarted) == {
            "delivery.cap-001": "queued",
            "delivery.cap-002": "queued",
            "delivery.cap-003": "queued",
        }
        assert _read_envelope(store, final.delivery_id) == final
        final_before = _item_path(store, final.delivery_id).read_bytes()
        recovered = _run_probe(
            store_probe, store.directory, 3, max_bytes, _enqueue_file(tmp_path, final)
        )
        assert recovered["enqueue"] == "already_queued"
        assert _item_path(store, final.delivery_id).read_bytes() == final_before

    # No eviction or overwrite of any admitted item, and no false queued
    # result: the caps still refuse the next enqueue after restart.
    for delivery_id, raw in originals.items():
        assert _item_path(store, delivery_id).read_bytes() == raw
    refused = _run_probe(
        store_probe, store.directory, 3, max_bytes, _enqueue_file(tmp_path, overflow)
    )
    assert refused["enqueue"] == "outbox_full"
    final_state = _run_probe(store_probe, store.directory, 3, max_bytes)
    assert _probe_status(final_state)["items"] == 3
    assert set(_probe_scan(final_state)) == {
        "delivery.cap-001",
        "delivery.cap-002",
        "delivery.cap-003",
    }
    for delivery_id, raw in originals.items():
        assert _item_path(store, delivery_id).read_bytes() == raw
