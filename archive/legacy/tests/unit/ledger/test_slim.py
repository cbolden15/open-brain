from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.storage.filesystem import atomic_write_new
from open_brain_engine.storage.frontmatter import AtomicMarkdownReader, AtomicMarkdownSink

from open_brain_legacy.ledger.service import LedgerService
from open_brain_legacy.ledger.slim import (
    AtomicSourceViewSuccessorReader,
    AtomicSourceViewSuccessorStore,
    LedgerSlimService,
    LedgerSourceView,
    SlimError,
    SourceViewReceipt,
)
from open_brain_legacy.ledger.store import LedgerRowIdentity, SqliteLedgerStore

from .test_store import _prepared, _resolver, _stage


class _GraceVerifier:
    def elapsed(self, source_view: LedgerSourceView, *, now: datetime) -> bool:
        return now >= source_view.created_at + timedelta(days=7)


class _Archive:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._bytes: bytes | None = None

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        self._events.append("archive-write")
        self._bytes = source_view.canonical_bytes()
        return SourceViewReceipt.create(
            record_id=source_view.version_id,
            digest_sha256=sha256(self._bytes).hexdigest(),
        )

    def read(self, receipt: SourceViewReceipt) -> bytes | None:
        self._events.append("archive-verify")
        return self._bytes


class _SuccessorStore:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.views: list[LedgerSourceView] = []
        self._bytes: bytes | None = None

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        self._events.append("successor-write")
        self.views.append(source_view)
        payload = source_view.canonical_bytes()
        self._bytes = payload
        return SourceViewReceipt.create(
            record_id=source_view.version_id,
            digest_sha256=sha256(payload).hexdigest(),
        )

    def read(self, receipt: SourceViewReceipt) -> bytes | None:
        self._events.append("successor-read")
        return self._bytes


class _IndependentSuccessorReader:
    def __init__(self, events: list[str], writer: _SuccessorStore) -> None:
        self._events = events
        self._writer = writer

    def read(self, receipt: SourceViewReceipt) -> bytes | None:
        self._events.append("successor-read")
        return self._writer._bytes


class _LoggingAtomicSuccessorStore:
    def __init__(self, events: list[str], *, root: Path) -> None:
        self._events = events
        self._store = AtomicSourceViewSuccessorStore(root=root)
        self.views: list[LedgerSourceView] = []

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        self._events.append("successor-write")
        self.views.append(source_view)
        return self._store.write_if_absent(source_view)


class _WrongBytesSuccessorStore:
    def __init__(self, events: list[str], *, root: Path) -> None:
        self._events = events
        self._root = root

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        self._events.append("successor-write")
        payload = source_view.canonical_bytes()
        digest = sha256(payload).hexdigest()
        atomic_write_new(
            root=self._root,
            relative=PurePosixPath("source-views", digest[:2], digest + ".json"),
            data=b"synthetic wrong successor bytes",
        )
        return SourceViewReceipt.create(
            record_id=source_view.version_id,
            digest_sha256=digest,
        )


class _ReadExceptionSuccessorStore:
    def __init__(self, events: list[str], *, root: Path) -> None:
        self._events = events
        self._root = root
        self._store = AtomicSourceViewSuccessorStore(root=root)

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        self._events.append("successor-write")
        receipt = self._store.write_if_absent(source_view)
        moved_root = self._root.with_name(self._root.name + "-moved")
        self._root.rename(moved_root)
        self._root.write_bytes(b"synthetic unsafe successor root")
        return receipt


class _ArchiveFailure:
    def __init__(self, events: list[str], *, scenario: str) -> None:
        self._events = events
        self._scenario = scenario
        self._bytes: bytes | None = None

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        self._events.append("archive-write")
        if self._scenario == "write-exception":
            raise RuntimeError("synthetic archive write failure")
        self._bytes = source_view.canonical_bytes()
        return SourceViewReceipt.create(
            record_id=(
                "source_view_forged"
                if self._scenario == "wrong-receipt-id"
                else source_view.version_id
            ),
            digest_sha256=(
                "0" * 64
                if self._scenario == "wrong-receipt-digest"
                else sha256(self._bytes).hexdigest()
            ),
        )

    def read(self, receipt: SourceViewReceipt) -> bytes | None:
        self._events.append("archive-verify")
        if self._scenario == "read-exception":
            raise RuntimeError("synthetic archive read failure")
        if self._scenario == "read-mismatch":
            return b"synthetic mismatched archive bytes"
        return self._bytes


class _FailingSuccessorStore:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.persisted: list[LedgerSourceView] = []

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        self._events.append("successor-write")
        raise RuntimeError("synthetic successor failure")


class _NoOpSuccessorStore:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.ram_bytes: bytes | None = None

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        self._events.append("successor-write")
        payload = source_view.canonical_bytes()
        self.ram_bytes = payload
        return SourceViewReceipt.create(
            record_id=source_view.version_id,
            digest_sha256=sha256(payload).hexdigest(),
        )

    def read(self, receipt: SourceViewReceipt) -> bytes | None:
        self._events.append("successor-read")
        return self.ram_bytes


def _applied_row(tmp_path: Path) -> tuple[SqliteLedgerStore, LedgerRowIdentity, str]:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path)
    service = LedgerService(store=store, citations=_resolver(stage))
    prepared = _prepared(service, stage)
    store.journal(prepared)
    store.prepare(prepared)
    markdown_root = tmp_path / "markdown"
    markdown_root.mkdir(mode=0o700)
    sink = AtomicMarkdownSink(root=markdown_root)
    reader = AtomicMarkdownReader(root=markdown_root)
    receipts = tuple(
        sink.write_if_absent(document)
        for document in (prepared.capture_document, prepared.ledger_document)
    )
    store.finalize(prepared, reader=reader, receipts=receipts)
    identity = store.applied_row_identity(stage.stage_digest_sha256)
    assert identity is not None
    return store, identity, prepared.source_id


def _source_view(*, source_id: str, now: datetime) -> LedgerSourceView:
    return LedgerSourceView.create(
        source_id=source_id,
        created_at=now - timedelta(days=10),
        content=b"synthetic derived content",
        transcript=b"runtime-assembled transcript canary",
    )


def test_archive_and_successor_are_verified_before_atomic_slim_finalization(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    store, row_identity, source_id = _applied_row(tmp_path / "private")
    source_view = _source_view(source_id=source_id, now=now)
    events: list[str] = []
    successor_root = tmp_path / "successors"
    successor_store = _LoggingAtomicSuccessorStore(events, root=successor_root)
    service = LedgerSlimService(
        store=store,
        archive=_Archive(events),
        successor_store=successor_store,
        successor_reader=AtomicSourceViewSuccessorReader(root=successor_root),
        grace_verifier=_GraceVerifier(),
    )

    result = service.prepare(source_view=source_view, row_identity=row_identity, now=now)

    assert result.prepared is not None
    assert result.error is None
    assert events == [
        "archive-write",
        "archive-verify",
        "successor-write",
    ]
    assert successor_store.views[0].transcript is None
    durable = store.slim_state(row_identity)
    assert durable is not None
    assert durable.slimmed is True
    assert durable.archive_digest_sha256 == result.prepared.archive_digest_sha256
    assert durable.successor_digest_sha256 == result.prepared.successor_digest_sha256


def test_forged_row_identity_cannot_authorize_slim(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    store, row_identity, source_id = _applied_row(tmp_path / "private")
    forged = LedgerRowIdentity(
        stage_digest_sha256=row_identity.stage_digest_sha256,
        row_digest_sha256="0" * 64,
    )
    events: list[str] = []
    successor_store = _SuccessorStore(events)
    service = LedgerSlimService(
        store=store,
        archive=_Archive(events),
        successor_store=successor_store,
        successor_reader=AtomicSourceViewSuccessorReader(root=tmp_path / "successors"),
        grace_verifier=_GraceVerifier(),
    )

    result = service.prepare(
        source_view=_source_view(source_id=source_id, now=now),
        row_identity=forged,
        now=now,
    )

    assert result.prepared is None
    assert events == []
    assert store.slim_state(row_identity) is not None
    assert store.slim_state(row_identity).slimmed is False  # type: ignore[union-attr]


def test_slim_finalization_replay_is_idempotent_without_rewriting(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    store, row_identity, source_id = _applied_row(tmp_path / "private")
    source_view = _source_view(source_id=source_id, now=now)
    events: list[str] = []
    successor_root = tmp_path / "successors"
    successor_store = _LoggingAtomicSuccessorStore(events, root=successor_root)
    service = LedgerSlimService(
        store=store,
        archive=_Archive(events),
        successor_store=successor_store,
        successor_reader=AtomicSourceViewSuccessorReader(root=successor_root),
        grace_verifier=_GraceVerifier(),
    )

    first = service.prepare(source_view=source_view, row_identity=row_identity, now=now)
    second = service.prepare(source_view=source_view, row_identity=row_identity, now=now)

    assert first == second
    assert events == [
        "archive-write",
        "archive-verify",
        "successor-write",
    ]


@pytest.mark.parametrize(
    ("scenario", "expected_error", "expected_events"),
    (
        ("write-exception", SlimError.ARCHIVE_FAILED, ["archive-write"]),
        ("wrong-receipt-id", SlimError.ARCHIVE_MISMATCH, ["archive-write"]),
        ("wrong-receipt-digest", SlimError.ARCHIVE_MISMATCH, ["archive-write"]),
        (
            "read-exception",
            SlimError.ARCHIVE_MISMATCH,
            ["archive-write", "archive-verify"],
        ),
        ("read-mismatch", SlimError.ARCHIVE_MISMATCH, ["archive-write", "archive-verify"]),
    ),
)
def test_archive_failures_leave_the_source_view_and_durable_row_unchanged(
    tmp_path: Path,
    scenario: str,
    expected_error: SlimError,
    expected_events: list[str],
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    store, row_identity, source_id = _applied_row(tmp_path / "private")
    source_view = _source_view(source_id=source_id, now=now)
    original_source_bytes = source_view.canonical_bytes()
    original_row = store.slim_state(row_identity)
    events: list[str] = []
    successor_store = _SuccessorStore(events)
    service = LedgerSlimService(
        store=store,
        archive=_ArchiveFailure(events, scenario=scenario),
        successor_store=successor_store,
        successor_reader=AtomicSourceViewSuccessorReader(root=tmp_path / "successors"),
        grace_verifier=_GraceVerifier(),
    )

    result = service.prepare(source_view=source_view, row_identity=row_identity, now=now)

    assert result.prepared is None
    assert result.error is expected_error
    assert events == expected_events
    assert successor_store.views == []
    assert source_view.canonical_bytes() == original_source_bytes
    assert source_view.transcript is not None
    assert store.slim_state(row_identity) == original_row
    assert original_row is not None
    assert original_row.slimmed is False


def test_successor_failure_leaves_the_original_view_and_durable_row_unchanged(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    store, row_identity, source_id = _applied_row(tmp_path / "private")
    source_view = _source_view(source_id=source_id, now=now)
    original_source_bytes = source_view.canonical_bytes()
    original_row = store.slim_state(row_identity)
    events: list[str] = []
    successor_store = _FailingSuccessorStore(events)
    service = LedgerSlimService(
        store=store,
        archive=_Archive(events),
        successor_store=successor_store,
        successor_reader=AtomicSourceViewSuccessorReader(root=tmp_path / "successors"),
        grace_verifier=_GraceVerifier(),
    )

    result = service.prepare(source_view=source_view, row_identity=row_identity, now=now)

    assert result.prepared is None
    assert result.error is SlimError.SUCCESSOR_FAILED
    assert events == ["archive-write", "archive-verify", "successor-write"]
    assert successor_store.persisted == []
    assert source_view.canonical_bytes() == original_source_bytes
    assert source_view.transcript is not None
    assert store.slim_state(row_identity) == original_row
    assert original_row is not None
    assert original_row.slimmed is False


def test_noop_successor_store_cannot_finalize_from_a_perfect_receipt(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    store, row_identity, source_id = _applied_row(tmp_path / "private")
    source_view = _source_view(source_id=source_id, now=now)
    original_source_bytes = source_view.canonical_bytes()
    original_row = store.slim_state(row_identity)
    events: list[str] = []
    successor_store = _NoOpSuccessorStore(events)
    service = LedgerSlimService(
        store=store,
        archive=_Archive(events),
        successor_store=successor_store,
        successor_reader=AtomicSourceViewSuccessorReader(root=tmp_path / "successors"),
        grace_verifier=_GraceVerifier(),
    )

    result = service.prepare(source_view=source_view, row_identity=row_identity, now=now)

    assert result.prepared is None
    assert result.error is SlimError.SUCCESSOR_FAILED
    assert events == ["archive-write", "archive-verify", "successor-write"]
    assert successor_store.ram_bytes is not None
    assert source_view.canonical_bytes() == original_source_bytes
    assert store.slim_state(row_identity) == original_row
    assert original_row is not None
    assert original_row.slimmed is False


def test_successor_writer_cannot_be_used_as_its_own_reader(tmp_path: Path) -> None:
    store, _, _ = _applied_row(tmp_path / "private")
    events: list[str] = []
    successor_store = _NoOpSuccessorStore(events)

    with pytest.raises(ValueError, match="approved root-confined reader required"):
        LedgerSlimService(
            store=store,
            archive=_Archive(events),
            successor_store=successor_store,
            successor_reader=successor_store,
            grace_verifier=_GraceVerifier(),
        )

    assert events == []


def test_distinct_ram_writer_and_reader_are_rejected_before_state_work(tmp_path: Path) -> None:
    store = SqliteLedgerStore(root=tmp_path / "private")
    events: list[str] = []
    successor_store = _SuccessorStore(events)

    with pytest.raises(ValueError, match="approved root-confined reader required"):
        LedgerSlimService(
            store=store,
            archive=_Archive(events),
            successor_store=successor_store,
            successor_reader=_IndependentSuccessorReader(events, successor_store),
            grace_verifier=_GraceVerifier(),
        )

    assert events == []
    assert store.record_count() == 0
    assert store.inflight_count() == 0


@pytest.mark.parametrize("scenario", ("wrong-bytes", "read-exception"))
def test_successor_readback_failure_leaves_the_row_and_source_unchanged(
    tmp_path: Path,
    scenario: str,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    store, row_identity, source_id = _applied_row(tmp_path / "private")
    source_view = _source_view(source_id=source_id, now=now)
    original_source_bytes = source_view.canonical_bytes()
    original_row = store.slim_state(row_identity)
    events: list[str] = []
    successor_root = tmp_path / "successors"
    successor_reader = AtomicSourceViewSuccessorReader(root=successor_root)
    successor_store = (
        _WrongBytesSuccessorStore(events, root=successor_root)
        if scenario == "wrong-bytes"
        else _ReadExceptionSuccessorStore(events, root=successor_root)
    )
    service = LedgerSlimService(
        store=store,
        archive=_Archive(events),
        successor_store=successor_store,
        successor_reader=successor_reader,
        grace_verifier=_GraceVerifier(),
    )

    result = service.prepare(source_view=source_view, row_identity=row_identity, now=now)

    assert result.prepared is None
    assert result.error is SlimError.SUCCESSOR_FAILED
    assert events == [
        "archive-write",
        "archive-verify",
        "successor-write",
    ]
    assert source_view.canonical_bytes() == original_source_bytes
    assert source_view.transcript is not None
    assert store.slim_state(row_identity) == original_row
    assert original_row is not None
    assert original_row.slimmed is False


def test_atomic_successor_store_reads_back_exact_root_confined_bytes(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    store, row_identity, source_id = _applied_row(tmp_path / "private")
    source_view = _source_view(source_id=source_id, now=now)
    events: list[str] = []
    successor_root = tmp_path / "successors"
    successor_store = AtomicSourceViewSuccessorStore(root=successor_root)
    successor_reader = AtomicSourceViewSuccessorReader(root=successor_root)
    service = LedgerSlimService(
        store=store,
        archive=_Archive(events),
        successor_store=successor_store,
        successor_reader=successor_reader,
        grace_verifier=_GraceVerifier(),
    )

    result = service.prepare(source_view=source_view, row_identity=row_identity, now=now)

    assert result.prepared is not None
    receipt = SourceViewReceipt.create(
        record_id=result.prepared.successor.version_id,
        digest_sha256=result.prepared.successor_digest_sha256,
    )
    assert successor_reader.read(receipt) == result.prepared.successor.canonical_bytes()
    durable = store.slim_state(row_identity)
    assert durable is not None
    assert durable.slimmed is True


@pytest.mark.parametrize("authorization_failure", ("missing-row", "forged-row", "forged-source"))
def test_missing_or_forged_durable_authority_never_starts_archive_or_slim(
    tmp_path: Path,
    authorization_failure: str,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    store, row_identity, source_id = _applied_row(tmp_path / "private")
    original_row = store.slim_state(row_identity)
    selected_identity = row_identity
    selected_source_id = source_id
    if authorization_failure == "missing-row":
        selected_identity = LedgerRowIdentity(
            stage_digest_sha256="f" * 64,
            row_digest_sha256="e" * 64,
        )
    elif authorization_failure == "forged-row":
        selected_identity = LedgerRowIdentity(
            stage_digest_sha256=row_identity.stage_digest_sha256,
            row_digest_sha256="0" * 64,
        )
    else:
        selected_source_id = "source-forged"
    source_view = _source_view(source_id=selected_source_id, now=now)
    original_source_bytes = source_view.canonical_bytes()
    events: list[str] = []
    successor_store = _SuccessorStore(events)
    service = LedgerSlimService(
        store=store,
        archive=_Archive(events),
        successor_store=successor_store,
        successor_reader=AtomicSourceViewSuccessorReader(root=tmp_path / "successors"),
        grace_verifier=_GraceVerifier(),
    )

    result = service.prepare(
        source_view=source_view,
        row_identity=selected_identity,
        now=now,
    )

    assert result.prepared is None
    assert result.error is SlimError.CITATIONS_UNVERIFIED
    assert events == []
    assert source_view.canonical_bytes() == original_source_bytes
    assert source_view.transcript is not None
    assert store.slim_state(row_identity) == original_row
    assert original_row is not None
    assert original_row.slimmed is False
