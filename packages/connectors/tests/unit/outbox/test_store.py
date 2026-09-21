from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier

import open_brain_connectors.outbox.store as outbox_store
from open_brain_connectors.outbox import DeliveryEnvelope, TerminalReceipt, TerminalReceiptStatus
from open_brain_connectors.outbox.contracts import OutboxContractError, verify_terminal_receipt
from open_brain_connectors.outbox.stdio_transport import MAX_DOCUMENT_BYTES
from open_brain_connectors.outbox.store import (
    DEFAULT_MAX_ITEM_BYTES,
    MAX_ITEM_MARGIN_BYTES,
    MAX_REQUEST_DOCUMENT_BYTES,
    TERMINAL_METADATA_HEADROOM_BYTES,
    EnqueueResult,
    OutboxItemState,
    OutboxStore,
    OutboxStoreError,
)

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
BRAIN_ID = derive_brain_id(TENANT_ID)
OTHER_BRAIN_ID = derive_brain_id("tenant_00000000-0000-4000-8000-000000000000")
PRINCIPAL_ID = "synthetic-destination-principal"
PAYLOAD_TEXT = "Synthetic destination-bound outbox capture"
ENQUEUED_AT = "2026-09-21T12:00:00Z"
TERMINAL_AT = "2026-09-21T12:30:00Z"


class _Crash(Exception):
    """Sentinel crash raised from an injected write hook."""


def _envelope(
    *,
    delivery_id: str = "delivery.store-001",
    payload_text: str = PAYLOAD_TEXT,
    enqueued_at: str = ENQUEUED_AT,
) -> DeliveryEnvelope:
    return DeliveryEnvelope.create(
        destination_brain_id=BRAIN_ID,
        expected_issuer_epoch=7,
        tenant_id=TENANT_ID,
        principal_id=PRINCIPAL_ID,
        delivery_id=delivery_id,
        requested_tier=PrivacyTier.WORK,
        policy_ref="policy.synthetic-v1",
        payload={"family": "text", "text": payload_text},
        enqueued_at=enqueued_at,
        retry_age_limit_seconds=86400,
        retry_attempt_limit=8,
    )


def _receipt(envelope: DeliveryEnvelope) -> TerminalReceipt:
    return TerminalReceipt(
        status=TerminalReceiptStatus.ACCEPTED,
        brain_id=envelope.destination_brain_id,
        issuer_epoch=envelope.expected_issuer_epoch,
        delivery_id=envelope.delivery_id,
        request_digest=envelope.request_digest,
        final_admitted_tier=envelope.requested_tier,
    )


def _store(tmp_path: Path, *, max_items: int = 32, max_bytes: int = 1024 * 1024) -> OutboxStore:
    return OutboxStore(tmp_path / "outbox", max_items=max_items, max_bytes=max_bytes)


def _item_files(directory: Path) -> list[str]:
    return sorted(entry.name for entry in os.scandir(directory) if not entry.name.startswith("."))


def _crash_hook() -> outbox_store._WriteHook:
    def hook(target: Path) -> None:
        raise _Crash(str(target))

    return hook


def _json_object(raw: bytes) -> object:
    return json.loads(raw.decode("utf-8"))


def _json_dict(raw: bytes) -> dict[str, object]:
    return cast(dict[str, object], _json_object(raw))


def test_open_creates_owner_directory_with_restricted_mode(tmp_path: Path) -> None:
    directory = tmp_path / "outbox"
    OutboxStore(directory, max_items=4, max_bytes=4096)

    assert directory.is_dir()
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_enqueue_persists_canonical_envelope_and_returns_queued(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()

    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    item_path = store.directory / "delivery.store-001.json"
    assert item_path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(item_path.stat().st_mode) == 0o600
    reparsed = DeliveryEnvelope.from_dict(_json_object(item_path.read_bytes()))
    assert reparsed == envelope
    status = store.status()
    assert status.queued_items == 1
    assert status.queued_size_bytes == item_path.stat().st_size


def test_duplicate_enqueue_of_identical_envelope_returns_already_queued(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED

    assert store.enqueue(_envelope()) is EnqueueResult.ALREADY_QUEUED
    assert _item_files(store.directory) == ["delivery.store-001.json"]
    assert store.status().queued_items == 1


def test_conflicting_envelope_under_same_delivery_id_writes_nothing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED
    before = (store.directory / "delivery.store-001.json").read_bytes()

    conflicting = _envelope(payload_text="Different synthetic body")

    assert store.enqueue(conflicting) is EnqueueResult.DELIVERY_CONFLICT
    assert (store.directory / "delivery.store-001.json").read_bytes() == before


def test_conflicting_enqueue_over_corrupt_item_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (store.directory / "delivery.store-001.json").write_bytes(b"not an envelope")

    assert store.enqueue(_envelope()) is EnqueueResult.DELIVERY_CONFLICT
    assert (store.directory / "delivery.store-001.json").read_bytes() == b"not an envelope"


def test_outbox_full_at_item_cap_writes_no_temp_file(tmp_path: Path) -> None:
    store = _store(tmp_path, max_items=1)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED

    assert store.enqueue(_envelope(delivery_id="delivery.store-002")) is EnqueueResult.OUTBOX_FULL

    assert _item_files(store.directory) == ["delivery.store-001.json"]
    assert not any(name.endswith(".tmp") for name in os.listdir(store.directory))
    assert store.status().queued_items == 1


def test_byte_cap_admits_exactly_size_plus_headroom_and_refuses_one_byte_less(
    tmp_path: Path,
) -> None:
    probe = _store(tmp_path)
    assert probe.enqueue(_envelope()) is EnqueueResult.QUEUED
    item_size = (probe.directory / "delivery.store-001.json").stat().st_size

    exact = _store(
        tmp_path / "exact", max_items=8, max_bytes=item_size + TERMINAL_METADATA_HEADROOM_BYTES
    )
    assert exact.enqueue(_envelope()) is EnqueueResult.QUEUED

    tight = _store(
        tmp_path / "tight",
        max_items=8,
        max_bytes=item_size + TERMINAL_METADATA_HEADROOM_BYTES - 1,
    )
    assert tight.enqueue(_envelope()) is EnqueueResult.OUTBOX_FULL
    assert _item_files(tight.directory) == []


def test_item_count_capacity_includes_quarantined_and_terminal_records(tmp_path: Path) -> None:
    quarantined = _store(tmp_path / "quarantined", max_items=1)
    assert quarantined.enqueue(_envelope()) is EnqueueResult.QUEUED
    quarantined.quarantine("delivery.store-001", reason="synthetic-quarantine")
    assert (
        quarantined.enqueue(_envelope(delivery_id="delivery.store-002"))
        is EnqueueResult.OUTBOX_FULL
    )

    discarded = _store(tmp_path / "discarded", max_items=1)
    assert discarded.enqueue(_envelope()) is EnqueueResult.QUEUED
    discarded.terminal_discard("delivery.store-001", discarded_at=TERMINAL_AT)
    assert (
        discarded.enqueue(_envelope(delivery_id="delivery.store-002")) is EnqueueResult.OUTBOX_FULL
    )

    terminal = _store(tmp_path / "terminal", max_items=1)
    envelope = _envelope()
    assert terminal.enqueue(envelope) is EnqueueResult.QUEUED
    terminal.mark_terminal("delivery.store-001", _receipt(envelope), terminal_at=TERMINAL_AT)
    assert (
        terminal.enqueue(_envelope(delivery_id="delivery.store-002")) is EnqueueResult.OUTBOX_FULL
    )


@pytest.mark.parametrize(
    "stage",
    ["after_temp_write", "after_temp_fsync", "after_rename", "after_directory_fsync"],
)
def test_crash_seam_leaves_no_partial_item_at_each_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(outbox_store, "_write_hooks", {stage: _crash_hook()})

    with pytest.raises(_Crash):
        store.enqueue(_envelope())

    item_path = store.directory / "delivery.store-001.json"
    if stage in {"after_temp_write", "after_temp_fsync"}:
        # The crash happened before the rename: no item exists and a rescan
        # (fresh process view of the same directory) reports nothing queued.
        assert not item_path.exists()
        assert OutboxStore(store.directory, max_items=8, max_bytes=65536).status().items == 0
    else:
        # The rename completed: the item is complete on disk and a fresh
        # store recovers a readable, parseable item even if the caller never
        # observed the queued result.
        recovered = OutboxStore(store.directory, max_items=8, max_bytes=65536)
        entries = recovered.scan()
        assert len(entries) == 1
        assert entries[0].state is OutboxItemState.QUEUED
        assert DeliveryEnvelope.from_dict(_json_object(item_path.read_bytes()))


def test_mark_terminal_crash_before_rename_leaves_envelope_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    monkeypatch.setattr(outbox_store, "_write_hooks", {"after_temp_write": _crash_hook()})

    with pytest.raises(_Crash):
        store.mark_terminal("delivery.store-001", _receipt(envelope), terminal_at=TERMINAL_AT)

    item_path = store.directory / "delivery.store-001.json"
    assert DeliveryEnvelope.from_dict(_json_object(item_path.read_bytes())) == envelope
    recovered = OutboxStore(store.directory, max_items=8, max_bytes=65536)
    assert [entry.state for entry in recovered.scan()] == [OutboxItemState.QUEUED]


def test_record_attempt_rewrites_the_envelope_with_attempt_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    item = store.record_attempt(
        "delivery.store-001",
        attempted_at="2026-09-21T12:05:00Z",
        result="admission_busy",
    )

    assert item.state is OutboxItemState.QUEUED
    reparsed = DeliveryEnvelope.from_dict(
        _json_object((store.directory / "delivery.store-001.json").read_bytes())
    )
    assert reparsed.attempts == 1
    assert reparsed.last_attempt_at == "2026-09-21T12:05:00Z"
    assert reparsed.last_attempt_result == "admission_busy"
    assert reparsed.request_digest == envelope.request_digest


def test_quarantine_sets_reason_and_retains_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    item = store.quarantine("delivery.store-001", reason="age_exhausted")

    assert item.state is OutboxItemState.QUARANTINED
    assert item.quarantine_reason == "age_exhausted"
    reparsed = DeliveryEnvelope.from_dict(
        _json_object((store.directory / "delivery.store-001.json").read_bytes())
    )
    assert reparsed.quarantine_reason == "age_exhausted"
    assert reparsed.payload is not None


def test_mark_terminal_replaces_body_with_receipt_terminal_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    receipt = _receipt(envelope)

    item = store.mark_terminal("delivery.store-001", receipt, terminal_at=TERMINAL_AT)

    assert item.state is OutboxItemState.TERMINAL
    item_path = store.directory / "delivery.store-001.json"
    raw = item_path.read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") not in raw
    assert b'"payload"' not in raw
    record = _json_dict(raw)
    assert record["record_version"] == "outbox.terminal.v1"
    assert record["terminal_kind"] == "receipt"
    assert record["receipt"] == receipt.to_dict()
    assert record["delivery_id"] == envelope.delivery_id
    assert record["destination_brain_id"] == envelope.destination_brain_id
    assert record["issuer_epoch"] == envelope.expected_issuer_epoch
    assert record["tenant_id"] == envelope.tenant_id
    assert record["principal_id"] == envelope.principal_id
    assert record["request_digest"] == envelope.request_digest
    assert record["enqueued_at"] == envelope.enqueued_at
    assert record["attempts"] == 0
    assert record["last_attempt_at"] is None
    assert record["terminal_at"] == TERMINAL_AT
    assert "payload" not in record
    status = store.status()
    assert status.terminal_items == 1
    assert status.terminal_size_bytes == item_path.stat().st_size


def test_mark_terminal_refuses_unverified_receipt_and_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    before = (store.directory / "delivery.store-001.json").read_bytes()
    wrong_brain = TerminalReceipt(
        status=TerminalReceiptStatus.ACCEPTED,
        brain_id=OTHER_BRAIN_ID,
        issuer_epoch=envelope.expected_issuer_epoch,
        delivery_id=envelope.delivery_id,
        request_digest=envelope.request_digest,
        final_admitted_tier=envelope.requested_tier,
    )

    with pytest.raises(OutboxContractError):
        store.mark_terminal("delivery.store-001", wrong_brain, terminal_at=TERMINAL_AT)

    assert (store.directory / "delivery.store-001.json").read_bytes() == before
    assert store.status().queued_items == 1


def test_terminal_discard_replaces_body_with_discard_terminal_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    item = store.terminal_discard("delivery.store-001", discarded_at=TERMINAL_AT)

    assert item.state is OutboxItemState.TERMINAL
    raw = (store.directory / "delivery.store-001.json").read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") not in raw
    assert b'"payload"' not in raw
    record = _json_dict(raw)
    assert record["record_version"] == "outbox.terminal.v1"
    assert record["terminal_kind"] == "discard"
    assert record["receipt"] is None
    assert record["delivery_id"] == envelope.delivery_id
    assert record["destination_brain_id"] == envelope.destination_brain_id
    assert record["issuer_epoch"] == envelope.expected_issuer_epoch
    assert record["tenant_id"] == envelope.tenant_id
    assert record["principal_id"] == envelope.principal_id
    assert record["request_digest"] == envelope.request_digest
    assert record["enqueued_at"] == envelope.enqueued_at
    assert record["terminal_at"] == TERMINAL_AT
    assert "payload" not in record


@pytest.mark.parametrize("tamper", ["digest", "missing_key"])
def test_tampered_terminal_record_surfaces_as_corrupt(tmp_path: Path, tamper: str) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    store.mark_terminal("delivery.store-001", _receipt(envelope), terminal_at=TERMINAL_AT)

    item_path = store.directory / "delivery.store-001.json"
    value = _json_dict(item_path.read_bytes())
    if tamper == "digest":
        value["request_digest"] = "b" * 64
    else:
        del value["terminal_at"]
    item_path.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    entries = OutboxStore(store.directory, max_items=8, max_bytes=65536).scan()
    assert [entry.state for entry in entries] == [OutboxItemState.CORRUPT]
    assert store.status().corrupt_items == 1


def test_corrupt_item_file_surfaced_metadata_only_and_never_deleted(tmp_path: Path) -> None:
    store = _store(tmp_path, max_items=2)
    (store.directory / "delivery.corrupt-001.json").write_bytes(b"{not json")

    entries = store.scan()
    status = store.status()

    assert [entry.state for entry in entries] == [OutboxItemState.CORRUPT]
    assert entries[0].delivery_id == "delivery.corrupt-001"
    assert entries[0].size_bytes == len(b"{not json")
    assert status.corrupt_items == 1
    assert (store.directory / "delivery.corrupt-001.json").read_bytes() == b"{not json"
    # A corrupt item consumes capacity: one slot left of two.
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED
    assert store.enqueue(_envelope(delivery_id="delivery.store-002")) is EnqueueResult.OUTBOX_FULL


def test_envelope_delivery_id_must_match_file_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED
    hijacked = store.directory / "delivery.other.json"
    hijacked.write_bytes((store.directory / "delivery.store-001.json").read_bytes())

    entries = store.scan()

    assert sorted(entry.state for entry in entries) == [
        OutboxItemState.CORRUPT,
        OutboxItemState.QUEUED,
    ]
    assert entries[0].delivery_id == "delivery.other"


def test_terminal_items_reject_every_transition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    store.mark_terminal("delivery.store-001", _receipt(envelope), terminal_at=TERMINAL_AT)

    with pytest.raises(OutboxStoreError):
        store.record_attempt(
            "delivery.store-001", attempted_at="2026-09-21T12:05:00Z", result="rate_limited"
        )
    with pytest.raises(OutboxStoreError):
        store.quarantine("delivery.store-001", reason="age_exhausted")
    with pytest.raises(OutboxStoreError):
        store.mark_terminal("delivery.store-001", _receipt(envelope), terminal_at=TERMINAL_AT)
    with pytest.raises(OutboxStoreError):
        store.terminal_discard("delivery.store-001", discarded_at=TERMINAL_AT)


@pytest.mark.parametrize("operation", ["record_attempt", "quarantine", "mark_terminal", "discard"])
@pytest.mark.parametrize("problem", ["missing", "corrupt"])
def test_transitions_refuse_missing_and_corrupt_items(
    tmp_path: Path, operation: str, problem: str
) -> None:
    store = _store(tmp_path)
    receipt = _receipt(_envelope())
    if problem == "corrupt":
        (store.directory / "delivery.store-001.json").write_bytes(b"garbage")
        target = "delivery.store-001"
    else:
        target = "delivery.store-404"

    with pytest.raises(OutboxStoreError):
        if operation == "record_attempt":
            store.record_attempt(target, attempted_at="2026-09-21T12:05:00Z", result="ok")
        elif operation == "quarantine":
            store.quarantine(target, reason="age_exhausted")
        elif operation == "mark_terminal":
            store.mark_terminal(target, receipt, terminal_at=TERMINAL_AT)
        else:
            store.terminal_discard(target, discarded_at=TERMINAL_AT)


def test_status_reports_counts_and_bytes_by_state_without_payload(tmp_path: Path) -> None:
    store = _store(tmp_path, max_items=8, max_bytes=65536)
    queued = _envelope()
    assert store.enqueue(queued) is EnqueueResult.QUEUED
    quarantined = _envelope(delivery_id="delivery.store-002", payload_text="Second body")
    assert store.enqueue(quarantined) is EnqueueResult.QUEUED
    store.quarantine("delivery.store-002", reason="age_exhausted")
    terminal = _envelope(delivery_id="delivery.store-003", payload_text="Third body")
    assert store.enqueue(terminal) is EnqueueResult.QUEUED
    store.mark_terminal("delivery.store-003", _receipt(terminal), terminal_at=TERMINAL_AT)
    discarded = _envelope(delivery_id="delivery.store-004", payload_text="Fourth body")
    assert store.enqueue(discarded) is EnqueueResult.QUEUED
    store.terminal_discard("delivery.store-004", discarded_at=TERMINAL_AT)
    (store.directory / "delivery.corrupt-001.json").write_bytes(b"junk")

    status = store.status()

    assert status.max_items == 8
    assert status.queued_items == 1
    assert status.quarantined_items == 1
    assert status.terminal_items == 2
    assert status.corrupt_items == 1
    assert status.items == 5
    assert status.size_bytes == sum(
        entry.stat().st_size
        for entry in os.scandir(store.directory)
        if not entry.name.startswith(".")
    )
    assert PAYLOAD_TEXT not in repr(status)


def test_scan_order_is_deterministic_by_delivery_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for suffix in ("003", "001", "002"):
        assert store.enqueue(_envelope(delivery_id=f"delivery.store-{suffix}")) is (
            EnqueueResult.QUEUED
        )

    assert [entry.delivery_id for entry in store.scan()] == [
        "delivery.store-001",
        "delivery.store-002",
        "delivery.store-003",
    ]


def test_recovery_scan_after_reopen_reports_queued_items(tmp_path: Path) -> None:
    first = _store(tmp_path)
    assert first.enqueue(_envelope()) is EnqueueResult.QUEUED

    reopened = OutboxStore(first.directory, max_items=8, max_bytes=65536)

    entries = reopened.scan()
    assert [entry.delivery_id for entry in entries] == ["delivery.store-001"]
    assert entries[0].state is OutboxItemState.QUEUED
    assert entries[0].enqueued_at == ENQUEUED_AT


def test_unsafe_delivery_ids_are_refused_at_enqueue(tmp_path: Path) -> None:
    store = _store(tmp_path)

    for unsafe in ("../escape", ".hidden", "sub/dir"):
        envelope = _envelope(delivery_id=unsafe)
        with pytest.raises(OutboxStoreError):
            store.enqueue(envelope)

    assert _item_files(store.directory) == []


def test_invalid_capacity_configuration_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OutboxStoreError):
        OutboxStore(tmp_path / "outbox-a", max_items=0, max_bytes=1024)
    with pytest.raises(OutboxStoreError):
        OutboxStore(tmp_path / "outbox-b", max_items=4, max_bytes=0)
    with pytest.raises(OutboxStoreError):
        OutboxStore(tmp_path / "outbox-c", max_items=4, max_bytes=-1)
    with pytest.raises(OutboxStoreError):
        OutboxStore(tmp_path / "outbox-d", max_items=4, max_bytes=1024, max_item_bytes=0)


def test_item_limit_default_derives_from_the_shared_request_document_cap() -> None:
    assert MAX_DOCUMENT_BYTES == MAX_REQUEST_DOCUMENT_BYTES
    assert DEFAULT_MAX_ITEM_BYTES == MAX_REQUEST_DOCUMENT_BYTES - MAX_ITEM_MARGIN_BYTES
    assert DEFAULT_MAX_ITEM_BYTES < MAX_REQUEST_DOCUMENT_BYTES


def _limit_sized_envelope(*, delivery_id: str, extra: int = 0) -> DeliveryEnvelope:
    """One envelope whose serialized document is exactly the default item limit plus ``extra``.

    Text is capped at 65,536 characters by the engine payload contract, so the
    padding uses four-byte UTF-8 characters to reach the byte limit exactly.
    """
    probe = _envelope(delivery_id=delivery_id, payload_text="x")
    fixed_overhead = len(outbox_store._canonical_bytes(probe.to_dict())) - 1
    target = DEFAULT_MAX_ITEM_BYTES + extra - fixed_overhead
    wide, narrow = divmod(target, 4)
    assert wide + narrow <= 65_536
    return _envelope(delivery_id=delivery_id, payload_text="\U0010ffff" * wide + "x" * narrow)


def test_item_at_the_size_limit_enqueues(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _limit_sized_envelope(delivery_id="delivery.store-limit-001")

    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    assert (store.directory / "delivery.store-limit-001.json").stat().st_size == (
        DEFAULT_MAX_ITEM_BYTES
    )


def test_item_over_the_size_limit_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        outbox_store,
        "_write_hooks",
        {
            stage: _crash_hook()
            for stage in (
                "after_temp_write",
                "after_temp_fsync",
                "after_rename",
                "after_directory_fsync",
            )
        },
    )
    oversized = _limit_sized_envelope(delivery_id="delivery.store-limit-001", extra=1)

    assert store.enqueue(oversized) is EnqueueResult.ITEM_TOO_LARGE

    assert _item_files(store.directory) == []
    assert os.listdir(store.directory) == []


def test_verify_terminal_receipt_still_binds_against_the_original_envelope(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    receipt = _receipt(envelope)

    store.mark_terminal("delivery.store-001", receipt, terminal_at=TERMINAL_AT)

    assert verify_terminal_receipt(envelope, receipt) == receipt
