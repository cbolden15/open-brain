"""Owner-authorized lifecycle tasks over real capture materialization and reads.

Public tasks retain owner quick-text semantics. Consumer orchestration uses
private steps that revalidate accepted approval and bound session authority.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.storage.filesystem import read_confined

from .capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleError,
    LifecycleRequest,
    SegmentBinding,
    canonical_bytes,
    decode_canonical,
)
from .capture_lifecycle_journal import LifecycleJournal, snapshot_token
from .capture_recovery import CaptureRecoveryPlan, CaptureReservationIdentity
from .consent_contracts import EgressMode
from .consumer_journal import LifecycleJournalV2, consumer_items
from .consumer_segments import ConsumerSegmentEnvelope
from .contracts import (
    CaptureAction,
    CaptureFault,
    CaptureSubmission,
    CaptureSubmissionPath,
    JournalEnvelope,
    TextPayload,
)
from .normalization import _timestamp
from .recovery_journal import RecoveryBaseline
from .t03_contracts import EffectiveAuthority, SessionOrigin

if TYPE_CHECKING:
    from .local import BrainEngine
    from .retraction_impact import RetractionImpact


@dataclass(frozen=True, slots=True)
class LifecycleSegment:
    envelope: JournalEnvelope
    identities: CaptureReservationIdentity

    def __post_init__(self) -> None:
        if type(self.envelope) is not JournalEnvelope:
            raise LifecycleError("invalid_arguments")
        submission = self.envelope.submission
        if (
            type(self.identities) is not CaptureReservationIdentity
            or self.identities.canonical
            or submission.submission_path is not CaptureSubmissionPath.OWNER
            or submission.action is not CaptureAction.QUICK
            or not isinstance(submission.payload, TextPayload)
        ):
            raise LifecycleError("invalid_arguments")

    @property
    def body(self) -> bytes:
        return cast(TextPayload, self.envelope.submission.payload).text.encode("utf-8")

    def to_bytes(self) -> bytes:
        return canonical_bytes(
            {
                "contract": "engine.lifecycle-segment.v1",
                "capture": self.envelope.to_dict(),
                "reservation": asdict(self.identities),
            }
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> LifecycleSegment:
        value = decode_canonical(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"contract", "capture", "reservation"}
            or value["contract"] != "engine.lifecycle-segment.v1"
            or not isinstance(value["reservation"], dict)
        ):
            raise LifecycleError("invalid_evidence")
        try:
            result = cls(
                JournalEnvelope.from_bytes(canonical_bytes(value["capture"])),
                CaptureReservationIdentity(**value["reservation"]),
            )
            if result.to_bytes() != raw:
                raise LifecycleError("invalid_evidence")
            return result
        except ValueError, TypeError:
            raise LifecycleError("invalid_evidence") from None

    @property
    def binding(self) -> SegmentBinding:
        return SegmentBinding(
            self.identities.capture_id,
            sha256(self.body).hexdigest(),
            sha256(self.to_bytes()).hexdigest(),
        )


@dataclass(slots=True)
class _ReadSnapshot:
    connection: sqlite3.Connection
    journal: LifecycleJournal | None = None
    token: tuple[int, int] | None = None


_READ_SNAPSHOT: ContextVar[_ReadSnapshot | None] = ContextVar(
    "lifecycle_read_snapshot", default=None
)


@contextmanager
def lifecycle_read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    """Verify the journal once for every candidate checked in one read snapshot.

    Each record is still decided on its own against that verified state. The journal is
    reused only while ``snapshot_token`` is unchanged: a commit by this or any other
    connection discards it, and the next check verifies the journal again.
    """
    token = _READ_SNAPSHOT.set(_ReadSnapshot(connection))
    try:
        yield
    finally:
        _READ_SNAPSHOT.reset(token)


def open_lifecycle_journal(connection: sqlite3.Connection) -> LifecycleJournal:
    """Dispatch only from admitted engine coordinates and the checked journal identity."""
    snapshot = _READ_SNAPSHOT.get()
    if snapshot is None or snapshot.connection is not connection or not connection.in_transaction:
        return _open_lifecycle_journal(connection)
    token = snapshot_token(connection)
    if snapshot.journal is None or snapshot.token != token:
        journal = _open_lifecycle_journal(connection)
        journal._snapshot = (token, None)
        snapshot.journal, snapshot.token = journal, token
    return snapshot.journal


def _open_lifecycle_journal(connection: sqlite3.Connection) -> LifecycleJournal:
    identity = connection.execute(
        "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise LifecycleError("invalid_evidence")
    journal_type = (
        LifecycleJournalV2
        if connection.execute("PRAGMA user_version").fetchone()[0] >= 20
        else LifecycleJournal
    )
    return journal_type.for_engine(connection, brain_id=identity[0], issuer_epoch=identity[1])


def _journal(connection: sqlite3.Connection) -> LifecycleJournal:
    return open_lifecycle_journal(connection)



def _require_legacy_request(request: LifecycleRequest) -> None:
    if consumer_items(request) is not None:
        raise LifecycleError("invalid_arguments")


def _require_consumer_operation(
    engine: BrainEngine,
    journal: LifecycleJournal,
    request: LifecycleRequest,
    *,
    session_id: str | None = None,
) -> None:
    """Every mutable consumer step requires accepted engine-owned authority."""
    from .consumer_intake import _check_scope, _load

    if not isinstance(journal, LifecycleJournalV2):
        raise LifecycleError("invalid_evidence")
    prepared = _load(journal, request.operation_id)
    if prepared is None or prepared.request.canonical_bytes != request.canonical_bytes:
        raise LifecycleError("invalid_evidence")
    prepared.validate_profile(engine.profile)
    if request.issuer_epoch != journal.issuer_epoch:
        raise LifecycleError("revision_changed")
    engine.consumer_imports._active(prepared)
    registered = _check_scope(journal, prepared)
    if session_id is not None:
        if session_id != prepared.value()["session_id"]:
            raise LifecycleError("operation_conflict")
        archive = journal.connection.execute(
            "SELECT scope_id FROM consumer_archive_bindings "
            "WHERE provider=? AND archive_sha256=?",
            (prepared.plan.provider, prepared.plan.archive_sha256),
        ).fetchone()
        if not registered or archive is None or archive[0] != prepared.scope["scope_id"]:
            raise LifecycleError("invalid_evidence")

def decode_lifecycle_segment(
    journal: LifecycleJournal, operation_id: str, raw: bytes
) -> LifecycleSegment | ConsumerSegmentEnvelope:
    """The typed accepted request selects the decoder; envelope shape grants nothing."""
    state = journal._state()
    request = state.request(operation_id)
    if consumer_items(request) is None:
        return LifecycleSegment.from_bytes(raw)
    if not isinstance(journal, LifecycleJournalV2):
        raise LifecycleError("invalid_evidence")
    segment = ConsumerSegmentEnvelope.from_bytes(raw)
    manifest = state.intended_groups(operation_id).get(segment.conversation_key)
    if (
        manifest is None
        or manifest.revision_sha256 != segment.conversation_revision_sha256
        or segment.binding not in manifest.segments
    ):
        raise LifecycleError("invalid_evidence")
    return segment


def _segments(journal: LifecycleJournal) -> Iterator[LifecycleSegment | ConsumerSegmentEnvelope]:
    state = journal._state()
    for operation, group, capture in state.custodied:
        binding = next(
            item for item in state.staged[operation, group].segments if item.capture_id == capture
        )
        segment = decode_lifecycle_segment(journal, operation, state.blobs[binding.request_sha256])
        if segment.binding != binding:
            raise LifecycleError("invalid_evidence")
        yield segment


def require_unreserved_lifecycle_delivery(connection: sqlite3.Connection, delivery_id: str) -> None:
    """Ordinary ingestion cannot impersonate an internal staged segment."""
    if connection.execute("PRAGMA user_version").fetchone()[0] < 18:
        return
    owned = not connection.in_transaction
    try:
        if owned:
            connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] >= 20 and connection.execute(
            "SELECT 1 FROM consumer_segment_bindings b JOIN consumer_intent_objects o "
            "ON o.sha256=b.envelope_sha256 WHERE "
            "json_extract(CAST(o.object_bytes AS TEXT),'$.capture.submission.delivery_id')=? "
            "LIMIT 1",
            (delivery_id,),
        ).fetchone():
            raise LifecycleError("operation_conflict")
        if any(
            item.envelope.submission.delivery_id == delivery_id
            for item in _segments(_journal(connection))
        ):
            raise LifecycleError("operation_conflict")
    finally:
        if owned:
            connection.execute("ROLLBACK")


def _matching_row(
    row: sqlite3.Row, segment: LifecycleSegment | ConsumerSegmentEnvelope, journal: LifecycleJournal
) -> None:
    from .capture_replay import require_matching_capture

    plan = CaptureRecoveryPlan(
        RecoveryBaseline(journal.brain_id, journal.issuer_epoch, "0" * 64),
        segment.envelope,
        segment.identities,
    )
    try:
        require_matching_capture(row, plan)
    except ValueError:
        raise LifecycleError("operation_conflict") from None


def require_lifecycle_materialization(
    engine: BrainEngine,
    submission: CaptureSubmission,
    admitted_privacy: PrivacyDecision,
    identities: CaptureReservationIdentity,
) -> None:
    with closing(engine._store.connect()) as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        journal = _journal(connection)
        expected = JournalEnvelope(submission, admitted_privacy)
        if not any(
            item.envelope == expected and item.identities == identities
            for item in _segments(journal)
        ):
            raise LifecycleError("invalid_evidence")


class CaptureLifecycleTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def _owner(self, authority: EffectiveAuthority) -> None:
        if (
            type(authority) is not EffectiveAuthority
            or not authority.owner
            or authority.principal_id != self._engine.profile.owner_actor_id
            or authority.egress_mode is not EgressMode.OWNER_LOCAL
            or authority.session_origin is not SessionOrigin.LOCAL_OWNER
        ):
            raise LifecycleError("invalid_arguments")
        self._engine._assert_root()
        if authority.brain_id is not None:
            with closing(self._engine._store.connect()) as connection:
                identity = connection.execute(
                    "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
                ).fetchone()
                if identity is None or tuple(identity) != (
                    authority.brain_id,
                    authority.issuer_epoch,
                ):
                    raise LifecycleError("revision_changed")

    @contextmanager
    def _transaction(self, authority: EffectiveAuthority) -> Iterator[LifecycleJournal]:
        self._owner(authority)
        with self._engine._writer_lease_bounded(), self._engine._store.transaction() as connection:
            journal = _journal(connection)
            if authority.brain_id is not None and (
                authority.brain_id != journal.brain_id
                or authority.issuer_epoch != journal.issuer_epoch
            ):
                raise LifecycleError("revision_changed")
            yield journal

    def prepare_segment(
        self, submission: CaptureSubmission, *, authority: EffectiveAuthority
    ) -> LifecycleSegment:
        self._owner(authority)
        envelope = self._engine._prepare_journal_submission(submission)
        return LifecycleSegment(
            envelope,
            CaptureReservationIdentity.allocate(
                canonical=False, accepted_at=_timestamp(self._engine._clock())
            ),
        )

    def begin(
        self, request: LifecycleRequest, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        _require_legacy_request(request)
        with self._transaction(authority) as journal:
            if request.kind != "consumer_import":
                raise LifecycleError("invalid_arguments")
            prior = journal._state().requests.get(request.operation_id)
            if prior is not None:
                return journal.reserve(request)
            groups = request.value()["payload"]
            assert isinstance(groups, dict)
            raw_groups = groups.get("groups")
            if set(groups) != {"groups"} or not isinstance(raw_groups, list) or not raw_groups:
                raise LifecycleError("invalid_arguments")
            parsed = [GroupIntent.from_value(item) for item in raw_groups]
            ids = [item.capture_id for group in parsed for item in group.segments]
            if len(set(ids)) != len(ids):
                raise LifecycleError("invalid_arguments")
            for capture in ids:
                if journal.connection.execute(
                    "SELECT 1 FROM captures WHERE capture_id=?", (capture,)
                ).fetchone():
                    raise LifecycleError("operation_conflict")
            journal.reserve(request)
            for group in parsed:
                journal.stage_group(request.operation_id, group)
            return journal.status(request.operation_id)

    def stage_segment(
        self,
        request: LifecycleRequest,
        group_key: str,
        segment: LifecycleSegment | ConsumerSegmentEnvelope,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        _require_legacy_request(request)
        return self._stage_segment(request, group_key, segment, authority=authority)

    def _stage_consumer_segment(
        self,
        request: LifecycleRequest,
        group_key: str,
        segment: ConsumerSegmentEnvelope,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        if consumer_items(request) is None:
            raise LifecycleError("invalid_arguments")
        self._engine.consumer_imports._owner(authority)
        return self._stage_segment(request, group_key, segment, authority=authority)

    def _stage_segment(
        self,
        request: LifecycleRequest,
        group_key: str,
        segment: LifecycleSegment | ConsumerSegmentEnvelope,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        self._owner(authority)
        engine = self._engine
        with engine._writer_lease_bounded():
            with engine._store.transaction() as connection:
                journal = _journal(connection)
                if consumer_items(request) is not None:
                    _require_consumer_operation(engine, journal, request)
                result = journal.reserve(request)
                if result["status"] in {"committed", "halted"}:
                    return result
                state = journal._state()
                manifest = state.staged.get((request.operation_id, group_key))
                if manifest is None or segment.binding not in manifest.segments:
                    raise LifecycleError("invalid_arguments")
                if decode_lifecycle_segment(journal, request.operation_id, segment.to_bytes()) != (
                    segment
                ):
                    raise LifecycleError("invalid_evidence")
                if (request.operation_id, group_key) in state.completed:
                    if state.blobs.get(segment.binding.request_sha256) != segment.to_bytes():
                        raise LifecycleError("invalid_evidence")
                    return result
                current = engine._prepare_journal_submission(
                    segment.envelope.submission, _lifecycle=True
                )
                if current != segment.envelope:
                    raise LifecycleError("revision_changed")
                engine._refuse_on_storage_watermark()
                if isinstance(segment, ConsumerSegmentEnvelope):
                    from .consumer_sources import require_consumer_source_preimages

                    require_consumer_source_preimages(connection, (segment,))
                delivery = segment.envelope.submission.delivery_id
                existing = connection.execute(
                    "SELECT * FROM captures WHERE delivery_id=?", (delivery,)
                ).fetchone()
                if existing is not None:
                    _matching_row(existing, segment, journal)
                if connection.execute(
                    "SELECT 1 FROM capture_ingestion_items WHERE delivery_id=?", (delivery,)
                ).fetchone():
                    raise LifecycleError("operation_conflict")
                for prior in _segments(journal):
                    if prior.envelope.submission.delivery_id == delivery and prior != segment:
                        raise LifecycleError("operation_conflict")
                journal.custody_segment(
                    request.operation_id,
                    group_key,
                    segment.identities.capture_id,
                    body=segment.body,
                    envelope=segment.to_bytes(),
                )
            if isinstance(segment, ConsumerSegmentEnvelope):
                engine._fault(CaptureFault.AFTER_CONSUMER_CUSTODY)
            engine._materialize_capture_locked(
                segment.envelope.submission,
                admitted_privacy=segment.envelope.admitted_privacy,
                lifecycle_identity=segment.identities,
            )
            return self.status(request.operation_id, authority=authority)

    def recover_pending(
        self, request: LifecycleRequest, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        """Resume retained segment custody with original IDs; activation stays explicit."""
        _require_legacy_request(request)
        with self._transaction(authority) as journal:
            result = journal.reserve(request)
            if result["status"] in {"committed", "halted"}:
                return result
            state = journal._state()
            retained = [
                (group, decode_lifecycle_segment(
                    journal, operation, state.blobs[binding.request_sha256]
                ))
                for (operation, group), manifest in state.staged.items()
                if operation == request.operation_id
                for binding in manifest.segments
                if (operation, group, binding.capture_id) in state.custodied
            ]
        for group, segment in retained:
            self.stage_segment(request, group, segment, authority=authority)
        return self.status(request.operation_id, authority=authority)

    def activate_group(
        self,
        request: LifecycleRequest,
        group_key: str,
        *,
        session_id: str,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        _require_legacy_request(request)
        with self._transaction(authority) as journal:
            return self._activate_group_in_transaction(
                journal, request, group_key, session_id=session_id
            )

    def _activate_group_in_transaction(
        self, journal: LifecycleJournal, request: LifecycleRequest, group_key: str,
        *, session_id: str
    ) -> dict[str, object]:
        """Compose checked scope/archive admission with the same activation commit."""
        engine = self._engine
        if consumer_items(request) is not None:
            _require_consumer_operation(engine, journal, request, session_id=session_id)
        journal.reserve(request)
        state = journal._state()
        if (request.operation_id, group_key) in state.completed:
            if state.sessions.get(request.operation_id) != session_id:
                raise LifecycleError("operation_conflict")
            return journal.status(request.operation_id)
        manifest = state.staged.get((request.operation_id, group_key))
        if manifest is None:
            raise LifecycleError("operation_not_found")
        rows = []
        consumer_segments: list[ConsumerSegmentEnvelope] = []
        for binding in manifest.segments:
            if (request.operation_id, group_key, binding.capture_id) not in state.custodied:
                raise LifecycleError("operation_pending")
            segment = decode_lifecycle_segment(
                journal, request.operation_id, state.blobs[binding.request_sha256]
            )
            if segment.binding != binding:
                raise LifecycleError("invalid_evidence")
            row = journal.connection.execute(
                "SELECT * FROM captures WHERE capture_id=?", (binding.capture_id,)
            ).fetchone()
            if row is None or row["stage"] != 3:
                raise LifecycleError("operation_pending")
            _matching_row(row, segment, journal)
            current = engine._prepare_journal_submission(
                segment.envelope.submission, _lifecycle=True
            )
            if current != segment.envelope:
                raise LifecycleError("revision_changed")
            raw = read_confined(
                root=engine.profile.root, relative=row["source_path"],
                expected_root_identity=engine.profile.root_identity,
            )
            from open_brain_engine.core.ids import portable_canonical_json_bytes

            if raw != portable_canonical_json_bytes(engine._capture_record(row)):
                raise LifecycleError("invalid_evidence")
            rows.append(row)
            if isinstance(segment, ConsumerSegmentEnvelope):
                consumer_segments.append(segment)
        previous = state.revisions.get((group_key, manifest.expected_revision_sha256 or ""))
        if consumer_segments:
            from .consumer_sources import activate_consumer_sources

            activate_consumer_sources(
                journal.connection, consumer_segments,
                previous_capture_ids=(
                    tuple(item.capture_id for item in previous.segments)
                    if previous is not None else ()
                ),
            )
        journal.activate_group(request.operation_id, group_key, session_id=session_id)
        if previous is not None:
            for item in previous.segments:
                journal.connection.execute(
                    "DELETE FROM search_documents WHERE capture_id=?", (item.capture_id,)
                )
        for row in rows:
            changed = journal.connection.execute(
                "UPDATE logical_sources SET historical_only=0 WHERE source_id IN "
                "(SELECT source_id FROM source_revisions WHERE capture_id=?)",
                (row["capture_id"],),
            ).rowcount
            if changed != 1:
                raise LifecycleError("invalid_evidence")
            current_row = journal.connection.execute(
                "SELECT * FROM captures WHERE capture_id=?", (row["capture_id"],)
            ).fetchone()
            engine._upsert_source_search(journal.connection, current_row)
        self._advance(journal)
        return journal.status(request.operation_id)

    @staticmethod
    def _advance(journal: LifecycleJournal) -> None:
        journal.connection.execute(
            "UPDATE engine_generations SET retrieval_generation=retrieval_generation+1,"
            "vector_generation=vector_generation+1"
        )

    def finish(
        self, request: LifecycleRequest, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        _require_legacy_request(request)
        with self._transaction(authority) as journal:
            journal.reserve(request)
            return journal.finish(request.operation_id)

    def halt(
        self, request: LifecycleRequest, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        _require_legacy_request(request)
        with self._transaction(authority) as journal:
            prior = journal._state().requests.get(request.operation_id)
            if prior is None:
                raise LifecycleError("operation_not_found")
            if prior.canonical_bytes != request.canonical_bytes:
                raise LifecycleError("operation_conflict")
            return journal.halt(request.operation_id)

    def retract(
        self, request: LifecycleRequest, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        with self._transaction(authority) as journal:
            return self._retract_in_transaction(journal, request)

    def _retract_in_transaction(
        self, journal: LifecycleJournal, request: LifecycleRequest
    ) -> dict[str, object]:
        if request.kind == "capture_retract":
            capture = request.payload.get("capture_id")
            if (
                not isinstance(capture, str)
                or journal.connection.execute(
                    "SELECT 1 FROM source_revisions WHERE capture_id=?", (capture,)
                ).fetchone()
                is None
            ):
                raise LifecycleError("operation_not_found")
        elif request.kind != "session_retract":
            raise LifecycleError("invalid_arguments")
        before = journal.eligibility_generation
        result = journal.retract(request)
        if journal.eligibility_generation != before:
            self._advance(journal)
            if journal.connection.execute("PRAGMA user_version").fetchone()[0] >= 19:
                # Current readers enforce the authoritative marker before using
                # projections. The decision/receipt never waits on row cleanup.
                return result
            from .capture_lifecycle_policy import lifecycle_permits_capture

            for row in list(
                journal.connection.execute("SELECT DISTINCT capture_id FROM search_documents")
            ):
                if not lifecycle_permits_capture(journal.connection, row[0]):
                    journal.connection.execute(
                        "DELETE FROM search_documents WHERE capture_id=?", (row[0],)
                    )
        return result

    def status(self, operation_id: str, *, authority: EffectiveAuthority) -> dict[str, object]:
        self._owner(authority)
        with closing(self._engine._store.connect()) as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            return _journal(connection).status(operation_id)

    def preview_retraction(
        self,
        *,
        authority: EffectiveAuthority,
        capture_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        from .owner_retraction import OwnerRetractionTasks

        return OwnerRetractionTasks(self._engine).preview(
            authority=authority, capture_id=capture_id, session_id=session_id
        )

    def preview_retraction_page(
        self,
        preview_id: str,
        *,
        authority: EffectiveAuthority,
        item_page: int = 0,
    ) -> dict[str, object]:
        from .owner_retraction import OwnerRetractionTasks

        return OwnerRetractionTasks(self._engine).preview_page(
            preview_id, authority=authority, item_page=item_page
        )

    def commit_retraction(
        self,
        preview_id: str,
        operation_id: str,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        from .owner_retraction import OwnerRetractionTasks

        return OwnerRetractionTasks(self._engine).commit(
            preview_id, operation_id, authority=authority
        )

    def retraction_status(
        self,
        operation_id: str,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        from .owner_retraction import OwnerRetractionTasks

        return OwnerRetractionTasks(self._engine).status(operation_id, authority=authority)

    def session_snapshot(
        self, session_id: str, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        self._owner(authority)
        with closing(self._engine._store.connect()) as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            return _journal(connection).session_snapshot(session_id)

    def inspect_retraction(
        self,
        *,
        authority: EffectiveAuthority,
        capture_id: str | None = None,
        session_id: str | None = None,
        expected_preimage_sha256: str | None = None,
    ) -> RetractionImpact:
        """Inspect exact body-free effects; this grants no mutation authority."""
        from .paging import read_snapshot
        from .retraction_impact import inspect_retraction

        self._owner(authority)
        # A whole impact set can cross space boundaries. This internal seam has
        # no partial/suppressed-result contract for a space-limited owner.
        if authority.space_ids is not None:
            raise LifecycleError("invalid_arguments")
        with read_snapshot(self._engine) as connection:
            journal = _journal(connection)
            if authority.brain_id is not None and (
                authority.brain_id != journal.brain_id
                or authority.issuer_epoch != journal.issuer_epoch
            ):
                raise LifecycleError("revision_changed")
            return inspect_retraction(
                journal,
                capture_id=capture_id,
                session_id=session_id,
                expected_preimage_sha256=expected_preimage_sha256,
            )
