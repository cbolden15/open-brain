"""Capture reservation, durable replay, and capture task implementation."""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    ContentOrigin,
    PrivacyDecision,
    PrivacyTier,
    narrowest_tier,
)
from open_brain_engine.providers.base import EnrichmentState
from open_brain_engine.storage import watermarks
from open_brain_engine.storage.locks import WriterQueueFullError
from open_brain_engine.storage.markdown import render_markdown

from .contracts import (
    BoundaryClassifier,
    CaptureAction,
    CaptureAdmissionError,
    CaptureAdmissionResult,
    CaptureFault,
    CaptureReceipt,
    CaptureSubmission,
    CaptureSubmissionPath,
    DecisionOutcome,
    EnrichmentRequest,
    EnrichmentUnavailable,
    FilePayload,
    JournalEnvelope,
    Payload,
    ProposalRecord,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    ReferencePayload,
    TextPayload,
    _LocalEngineOperations,
    project_public_capture_receipt,
)
from .markdown_import import capture_projection_is_active, capture_submission_is_reserved
from .normalization import (
    _dated_path,
    _decision_record,
    _new_id,
    _payload_dict,
    _portable_id,
    _privacy,
    _publication_record,
    _receipt,
    _role_claim,
    _space_row,
    _timestamp,
    _trust,
)
from .portability_ports import portable_write_port
from .privacy_projection import narrow_retained_privacy_decision

if TYPE_CHECKING:
    from .local import BrainEngine


class DeliveryConflict(ValueError):
    """A submitted immutable delivery key was previously bound to different bytes."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("conflicting delivery")

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"__traceback__", "__cause__", "__context__", "__suppress_context__"}:
            return super().__setattr__(name, value)
        raise AttributeError("delivery conflict is immutable")


def _payload_body_length(payload: Payload) -> int:
    """The raw payload body bytes: text UTF-8 encoded, file bytes as-is."""
    if isinstance(payload, TextPayload):
        return len(payload.text.encode("utf-8"))
    if isinstance(payload, FilePayload):
        return len(payload.data)
    return 0


# Bounded writer wait for capture paths only. This is a small documented
# constant rather than an AdmissionLimits field: the milestone fixes the
# waiter cap and rate/concurrency bounds as configuration, while the wait
# deadline itself stays a local foreground-runtime constant.
_WRITER_WAIT_TIMEOUT_SECONDS = 5.0
_WRITER_POLL_INTERVAL_SECONDS = 0.01
_RATE_WINDOW = timedelta(seconds=60)


def _principal_key(tenant_id: str, actor_id: str) -> str:
    """One admission principal is tenant plus actor; owner paths share the owner key."""
    return f"{tenant_id}:{actor_id}"


def _boundary_scan_text(submission: CaptureSubmission) -> tuple[str, ...]:
    """The free-text surfaces one canonical-boundary rescan may classify."""
    values: list[str] = []
    if isinstance(submission.payload, TextPayload):
        values.append(submission.payload.text)
    if submission.title is not None:
        values.append(submission.title)
    return tuple(values)


def redaction_boundary_classifier(submission: CaptureSubmission) -> PrivacyTier | None:
    """Reuse the approved capture redaction policy as one boundary signal.

    Any finding of the deterministic work-tier redaction policy is a secret
    signal; no finding is no signal. The engine default is no classifier at
    all, so this runs only where an embedder injects it.
    """
    for value in _boundary_scan_text(submission):
        if has_redaction_finding(value):
            return PrivacyTier.SECRET
    return None


class CaptureOperations(_LocalEngineOperations):
    # Engine-owned admission gate state, initialized by BrainEngine; declared
    # here so typed gate arithmetic on the mixin resolves.
    _admission_gate_guard: threading.Lock
    _admission_rate_windows: dict[str, deque[datetime]]
    _active_admissions: int
    _storage_probe: Callable[[Path], watermarks.StorageUsage] | None
    _boundary_classifier: BoundaryClassifier | None

    def _accept_capture(
        self,
        payload: Payload,
        *,
        delivery_id: str,
        action: CaptureAction,
        space_id: str | None,
        intent: str | None,
        capture_why: str | None,
        title: str | None,
        privacy_tier: PrivacyTier | None = None,
    ) -> CaptureReceipt:
        return self._submit_capture(
            CaptureSubmission.for_local_owner(
                profile=self.profile,
                payload=payload,
                delivery_id=delivery_id,
                action=action,
                space_id=space_id,
                intent=intent,
                capture_why=capture_why,
                title=title,
                privacy_tier=privacy_tier,
            )
        )

    @contextmanager
    def _admit_before_writer(self, principal_key: str) -> Iterator[None]:
        """Per-principal rate and concurrent-admission gate for remote submissions.

        Raises ``CaptureAdmissionError(rate_limited)`` or
        ``CaptureAdmissionError(admission_busy)`` before the writer lease is
        touched, so a refusal leaves no capture row, blob, or search document.
        The sliding window and the counter are per process (the core is one
        foreground process) and recover in-process: the window ages out and
        the counter releases on success and on exceptions. Only admitted
        requests consume rate budget for their principal.
        """
        limits = self._admission_limits
        with self._admission_gate_guard:
            now = self._clock()
            window = self._admission_rate_windows.get(principal_key)
            if window is None:
                window = deque()
                self._admission_rate_windows[principal_key] = window
            else:
                boundary = now - _RATE_WINDOW
                while window and window[0] <= boundary:
                    window.popleft()
            if len(window) >= limits.requests_per_minute_per_principal:
                raise CaptureAdmissionError(CaptureAdmissionResult.RATE_LIMITED)
            if self._active_admissions >= limits.max_concurrent_admissions:
                raise CaptureAdmissionError(CaptureAdmissionResult.ADMISSION_BUSY)
            window.append(now)
            self._active_admissions += 1
        try:
            yield
        finally:
            with self._admission_gate_guard:
                self._active_admissions -= 1

    @contextmanager
    def _admit_submission(self, submission: CaptureSubmission) -> Iterator[None]:
        """Gate remote-originated submissions; owner paths skip rate and concurrency.

        Outcome 1 keeps existing no-flag local capture and Markdown import
        behaving exactly as today, so CaptureSubmissionPath.OWNER submissions
        are never rate limited or concurrency capped. Non-owner submissions
        (public job today, destination-bound clients later) pass the full
        per-principal gate before the writer lease.
        """
        if submission.submission_path is CaptureSubmissionPath.OWNER:
            yield
            return
        key = _principal_key(submission.tenant_id, submission.actor_id)
        with self._admit_before_writer(key):
            yield

    @contextmanager
    def _writer_lease_bounded(self) -> Iterator[None]:
        """Bounded writer lease for capture paths; a full queue becomes an admission result."""
        limits = self._admission_limits
        try:
            with self._writer_lease.acquire_shared_writer_bounded(
                max_waiters=limits.max_writer_waiters,
                timeout=_WRITER_WAIT_TIMEOUT_SECONDS,
                poll_interval=_WRITER_POLL_INTERVAL_SECONDS,
            ):
                yield
        except WriterQueueFullError:
            raise CaptureAdmissionError(CaptureAdmissionResult.WRITER_QUEUE_FULL) from None

    def _refuse_on_storage_watermark(self) -> None:
        """Classify Brain-root free storage before any write path runs.

        Every submission path (owner, public job, Markdown import) funnels
        through ``_submit_capture``, so one check before the reservation
        read, blob write, or SQLite transaction protects the Brain itself
        and leaves no capture row, revision, blob, search document, or
        receipt on refusal. The probe runs per submission, so free space
        returning above a watermark recovers without reopening the engine.
        """
        # The default resolves through the watermarks module attribute at
        # call time so a test conftest can pin one hermetic probe for the
        # whole suite; an injected engine probe always wins.
        probe = (
            self._storage_probe
            if self._storage_probe is not None
            else watermarks.probe_storage_usage
        )
        result = watermarks.classify_storage(probe(self.profile.root), self._admission_limits)
        if result is not None:
            raise CaptureAdmissionError(result)

    def _check_pre_materialization_admission(self, submission: CaptureSubmission) -> None:
        """Run the size and storage-watermark checks before any durable row exists.

        These are exactly the G2 size checks and the G4 storage check that
        ``_submit_capture`` enforces; the Markdown import path also calls this
        helper before it reserves an import revision, so an oversized or
        storage-refused note is refused before any reservation row is written.
        """
        limits = self._admission_limits
        body_length = _payload_body_length(submission.payload)
        envelope_length = (
            len(portable_canonical_json_bytes(submission.request_value())) + body_length
        )
        if envelope_length > limits.max_envelope_bytes:
            raise CaptureAdmissionError(CaptureAdmissionResult.ENVELOPE_TOO_LARGE)
        if body_length > limits.max_body_bytes:
            raise CaptureAdmissionError(CaptureAdmissionResult.BODY_TOO_LARGE)
        self._refuse_on_storage_watermark()

    def _check_static_capture_admission(self, submission: CaptureSubmission) -> None:
        """Validate bounded request bytes without consuming storage or writer state."""
        limits = self._admission_limits
        body_length = _payload_body_length(submission.payload)
        envelope_length = (
            len(portable_canonical_json_bytes(submission.request_value())) + body_length
        )
        if envelope_length > limits.max_envelope_bytes:
            raise CaptureAdmissionError(CaptureAdmissionResult.ENVELOPE_TOO_LARGE)
        if body_length > limits.max_body_bytes:
            raise CaptureAdmissionError(CaptureAdmissionResult.BODY_TOO_LARGE)

    def _admitted_privacy(self, submission: CaptureSubmission) -> PrivacyDecision:
        """Canonical-boundary rescan: the classifier may narrow the retained tier.

        ``None`` (the default) or a ``None`` signal keeps the submitted
        decision exactly; any other signal is folded through the G1
        ``narrowest_tier`` helper, so admission can only ever narrow. Every
        submission path (owner, public job, Markdown import) funnels through
        ``_submit_capture`` and is rescan-narrowed identically here.
        """
        classifier = self._boundary_classifier
        if classifier is None:
            return submission.privacy
        signal = classifier(submission)
        if signal is None:
            return submission.privacy
        return narrow_retained_privacy_decision(
            submission.privacy, narrowest_tier(submission.requested_tier, signal)
        )

    def _prepare_journal_submission(self, submission: CaptureSubmission) -> JournalEnvelope:
        """Perform all no-write capture admission before the journal transaction."""
        submission.validate_profile(self.profile)
        self._check_static_capture_admission(submission)
        admitted_privacy = self._admitted_privacy(submission)
        return JournalEnvelope(submission, admitted_privacy)

    def _submit_capture(self, submission: CaptureSubmission) -> CaptureReceipt:
        """Materialize a submission for legacy writer-held callers during Phase 2."""
        envelope = self._prepare_journal_submission(submission)
        self._refuse_on_storage_watermark()
        return self._materialize_capture_locked(
            submission, admitted_privacy=envelope.admitted_privacy
        )

    def _materialize_capture_locked(
        self,
        submission: CaptureSubmission,
        *,
        admitted_privacy: PrivacyDecision,
    ) -> CaptureReceipt:
        """Run the established capture stage machine under an already-held writer lease."""
        capture_submission_is_reserved(cast("BrainEngine", self), submission)
        payload = submission.payload
        delivery_id = submission.delivery_id
        action = submission.action
        space_id = submission.space_id
        intent = None if submission.intent is None else submission.intent.value
        capture_why = submission.capture_why
        title = submission.title
        if action is CaptureAction.CANONICAL_NOTE and not isinstance(payload, TextPayload):
            raise ValueError("canonical note requires owner text")
        payload_bytes = portable_canonical_json_bytes(payload.to_dict())
        source_origin = submission.durable_source_origin()
        source_reference = submission.source_reference
        request_sha = submission.request_sha256()
        duplicate = False
        conflict: tuple[str, str] | None = None
        with self._store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM captures WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if existing is not None:
                if cast(str, existing["request_sha256"]) != request_sha:
                    if connection.execute("PRAGMA user_version").fetchone()[
                        0
                    ] < 7 and _can_replace_public_source(submission, existing):
                        previous_capture_id = cast(str, existing["capture_id"])
                        capture_id = _new_id("capture")
                        accepted_at = _timestamp(self._clock())
                        connection.execute(
                            "DELETE FROM search_documents WHERE capture_id = ? OR result_id = ?",
                            (previous_capture_id, previous_capture_id),
                        )
                        connection.execute(
                            """
                            UPDATE captures
                            SET request_sha256 = ?, capture_id = ?, accepted_receipt_id = ?,
                                payload_family = ?, payload_json = ?, search_text = ?,
                                file_bytes = ?, source_origin = ?, source_reference = ?,
                                space_id = ?, intent = ?, capture_why = ?, action = ?,
                                title = ?, accepted_at = ?, stage = 0, source_path = NULL,
                                canonical_path = NULL, publication_path = NULL,
                                enrichment_state = 'pending_enrichment', actor_id = ?,
                                role_claim_json = ?, privacy_json = ?, provenance_json = ?,
                                submission_path = ?
                            WHERE delivery_id = ?
                            """,
                            (
                                request_sha,
                                capture_id,
                                _new_id("receipt"),
                                payload.family,
                                payload_bytes,
                                payload.search_text(),
                                payload.data if isinstance(payload, FilePayload) else None,
                                source_origin,
                                source_reference,
                                space_id,
                                intent,
                                capture_why,
                                action.value,
                                title,
                                accepted_at,
                                submission.actor_id,
                                portable_canonical_json_bytes(
                                    {
                                        "actor_id": submission.role_claim["actor_id"],
                                        "capabilities": list(
                                            cast(
                                                tuple[str, ...],
                                                submission.role_claim["capabilities"],
                                            )
                                        ),
                                        "role_claim_id": submission.role_claim["role_claim_id"],
                                        "role_id": submission.role_claim["role_id"],
                                        "tenant_id": submission.role_claim["tenant_id"],
                                    }
                                ).decode("utf-8"),
                                portable_canonical_json_bytes(admitted_privacy.to_dict()).decode(
                                    "utf-8"
                                ),
                                portable_canonical_json_bytes(
                                    submission.provenance.to_dict()
                                ).decode("utf-8"),
                                submission.submission_path.value,
                                delivery_id,
                            ),
                        )
                    else:
                        conflict = (cast(str, existing["request_sha256"]), request_sha)
                else:
                    duplicate = True
                    capture_id = cast(str, existing["capture_id"])
            else:
                if space_id is not None and _space_row(connection, space_id) is None:
                    raise ValueError("unknown space")
                if action is CaptureAction.CANONICAL_NOTE and space_id is None:
                    raise ValueError("canonical note requires a space")
                capture_id = _new_id("capture")
                accepted_at = _timestamp(self._clock())
                canonical = action is CaptureAction.CANONICAL_NOTE
                connection.execute(
                    """
                    INSERT INTO captures (
                        delivery_id, request_sha256, capture_id, accepted_receipt_id,
                        payload_family, payload_json, search_text, file_bytes,
                        source_origin, source_reference, space_id, intent, capture_why,
                        action, title, accepted_at, auto_proposal_id,
                        auto_proposal_receipt_id, auto_decision_id,
                        auto_decision_receipt_id, page_id, publication_id, actor_id,
                        role_claim_json, privacy_json, provenance_json, submission_path
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        delivery_id,
                        request_sha,
                        capture_id,
                        _new_id("receipt"),
                        payload.family,
                        payload_bytes,
                        payload.search_text(),
                        payload.data if isinstance(payload, FilePayload) else None,
                        source_origin,
                        source_reference,
                        space_id,
                        intent,
                        capture_why,
                        action.value,
                        title,
                        accepted_at,
                        _new_id("proposal") if canonical else None,
                        _new_id("receipt") if canonical else None,
                        _new_id("decision") if canonical else None,
                        _new_id("receipt") if canonical else None,
                        _new_id("page") if canonical else None,
                        _new_id("publication") if canonical else None,
                        submission.actor_id,
                        portable_canonical_json_bytes(
                            {
                                "actor_id": submission.role_claim["actor_id"],
                                "capabilities": list(
                                    cast(tuple[str, ...], submission.role_claim["capabilities"])
                                ),
                                "role_claim_id": submission.role_claim["role_claim_id"],
                                "role_id": submission.role_claim["role_id"],
                                "tenant_id": submission.role_claim["tenant_id"],
                            }
                        ).decode("utf-8"),
                        portable_canonical_json_bytes(admitted_privacy.to_dict()).decode("utf-8"),
                        portable_canonical_json_bytes(submission.provenance.to_dict()).decode(
                            "utf-8"
                        ),
                        submission.submission_path.value,
                    ),
                )
        if conflict is not None:
            self._quarantine(delivery_id, expected=conflict[0], actual=conflict[1])
            raise DeliveryConflict()
        if not duplicate:
            self._fault(CaptureFault.AFTER_CAPTURE_RESERVATION)
        row = self._capture_row(capture_id)
        self._process_capture(row)
        receipt = self._capture_receipt(capture_id)
        if receipt is None:
            raise RuntimeError("capture state unavailable")
        return project_public_capture_receipt(
            CaptureReceipt(
                capture_id=receipt.capture_id,
                payload_family=receipt.payload_family,
                state=receipt.state,
                enrichment_state=receipt.enrichment_state,
                space_id=receipt.space_id,
                canonical_path=receipt.canonical_path,
                duplicate=duplicate,
                requested_tier=submission.requested_tier,
                final_admitted_tier=admitted_privacy.tier,
                # Only the destination-bound path publishes its request and
                # authority binding; every other receipt keeps today's shape.
                delivery_id=(
                    delivery_id
                    if submission.submission_path is CaptureSubmissionPath.DESTINATION_BOUND
                    else None
                ),
                request_sha256=(
                    request_sha
                    if submission.submission_path is CaptureSubmissionPath.DESTINATION_BOUND
                    else None
                ),
                destination_brain_id=submission.destination_brain_id,
                issuer_epoch=submission.issuer_epoch,
            )
        )

    def _capture_row(self, capture_id: str) -> sqlite3.Row:
        _portable_id(capture_id, "capture")
        connection = self._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("unknown capture")
        return cast(sqlite3.Row, row)

    def _process_capture(self, supplied_row: sqlite3.Row) -> None:
        row = self._capture_row(cast(str, supplied_row["capture_id"]))
        connection = self._store.connect()
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] >= 7:
                intake = connection.execute(
                    "SELECT plan_json FROM source_intakes WHERE delivery_id=? "
                    "AND receipt_json IS NULL",
                    (row["delivery_id"],),
                ).fetchone()
                epoch = connection.execute(
                    "SELECT control_epoch FROM engine_generations"
                ).fetchone()[0]
                if intake is not None and json.loads(intake["plan_json"])["control_epoch"] != epoch:
                    from .t03_contracts import T03Error

                    raise T03Error("operation_pending")
        finally:
            connection.close()
        stage = cast(int, row["stage"])
        if stage < 1:
            if cast(bytes | None, row["file_bytes"]) is not None:
                portable_write_port(self).put_blob(cast(bytes, row["file_bytes"]))
                self._fault(CaptureFault.AFTER_BLOB_WRITE)
            source_path = _dated_path(
                "sources/captures", cast(str, row["accepted_at"]), cast(str, row["capture_id"])
            )
            portable_write_port(self).put_capture(
                portable_canonical_json_bytes(self._capture_record(row))
            )
            self._fault(CaptureFault.AFTER_SOURCE_WRITE)
            with self._store.transaction() as connection:
                connection.execute(
                    "UPDATE captures SET source_path = ?, stage = 1 WHERE capture_id = ?",
                    (source_path, row["capture_id"]),
                )
            row = self._capture_row(cast(str, row["capture_id"]))
            stage = 1
        if stage < 2:
            if cast(str, row["action"]) == CaptureAction.CANONICAL_NOTE.value:
                proposal_path, decision_path, canonical_path, publication_path = (
                    self._write_automatic_publication(row)
                )
                del proposal_path, decision_path
                with self._store.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE captures
                        SET canonical_path = ?, publication_path = ?, stage = 2
                        WHERE capture_id = ?
                        """,
                        (canonical_path, publication_path, row["capture_id"]),
                    )
            else:
                with self._store.transaction() as connection:
                    connection.execute(
                        "UPDATE captures SET stage = 2 WHERE capture_id = ?",
                        (row["capture_id"],),
                    )
            row = self._capture_row(cast(str, row["capture_id"]))
            stage = 2
        if stage < 3:
            with self._store.transaction() as connection:
                if capture_projection_is_active(
                    connection,
                    delivery_id=cast(str, row["delivery_id"]),
                    capture_id=cast(str, row["capture_id"]),
                ):
                    self._upsert_source_search(connection, row)
                if cast(str | None, row["canonical_path"]) is not None:
                    self._upsert_canonical_search(
                        connection,
                        result_id=cast(str, row["page_id"]),
                        capture_id=cast(str, row["capture_id"]),
                        payload_family=cast(str, row["payload_family"]),
                        space_id=cast(str, row["space_id"]),
                        title=self._capture_title(row),
                        body=cast(str, row["search_text"]),
                        canonical_path=cast(str, row["canonical_path"]),
                        updated_at=cast(str, row["accepted_at"]),
                    )
                connection.execute(
                    "UPDATE captures SET stage = 3 WHERE capture_id = ?", (row["capture_id"],)
                )
            self._fault(CaptureFault.AFTER_INDEX_UPDATE)

    def _capture_record(self, row: sqlite3.Row) -> dict[str, object]:
        payload = _payload_dict(row)
        payload_bytes = portable_canonical_json_bytes(payload)
        capture_id = cast(str, row["capture_id"])
        original: dict[str, object]
        if cast(bytes | None, row["file_bytes"]) is not None:
            original = {"blob_sha256": cast(str, payload["blob_sha256"]), "kind": "blob"}
            original_digest = cast(str, payload["blob_sha256"])
        else:
            original_digest = sha256(payload_bytes).hexdigest()
            original = {
                "bytes_base64": base64.b64encode(payload_bytes).decode("ascii"),
                "kind": "inline",
                "sha256": original_digest,
            }
        accepted_payload = {
            "capture_id": capture_id,
            "original_payload_sha256": original_digest,
            "payload_sha256": sha256(payload_bytes).hexdigest(),
        }
        receipts = [
            _receipt(
                "capture_accepted",
                cast(str, row["accepted_receipt_id"]),
                capture_id,
                cast(str, row["accepted_at"]),
                accepted_payload,
            )
        ]
        connection = self._store.connect()
        try:
            routes = tuple(
                connection.execute(
                    "SELECT * FROM route_operations WHERE capture_id = ? "
                    "ORDER BY recorded_at, delivery_id",
                    (capture_id,),
                )
            )
        finally:
            connection.close()
        receipts.extend(
            _receipt(
                "routing",
                cast(str, route["receipt_id"]),
                capture_id,
                cast(str, route["recorded_at"]),
                {"capture_id": capture_id, "space_id": cast(str, route["space_id"])},
            )
            for route in routes
        )
        origin = cast(str, row["source_origin"])
        submission_path = cast(str | None, row["submission_path"]) or "owner"
        # Destination-bound rows project from the stored submission exactly like
        # public-job rows: the durable record must never claim the owner actor
        # for a non-owner destination principal.
        public_job = submission_path in (
            CaptureSubmissionPath.PUBLIC_JOB.value,
            CaptureSubmissionPath.DESTINATION_BOUND.value,
        )
        stored_provenance = _stored_submission_value(row, "provenance_json") if public_job else {}
        provenance = (
            {
                "content_origin": (
                    "unknown" if stored_provenance["content_origin"] == "unknown" else "third_party"
                ),
                "owner_context": "automation_absent",
                "source_ref": row["source_reference"],
                "transformation_receipts": [],
            }
            if public_job
            else {
                "content_origin": "third_party" if origin == "third_party" else "owner_authored",
                "owner_context": (
                    "automation_absent" if origin == "third_party" else "owner_authored"
                ),
                "source_ref": row["source_reference"],
                "transformation_receipts": [],
            }
        )
        return {
            "accepted_at": row["accepted_at"],
            "actor_id": row["actor_id"] if public_job else self.profile.owner_actor_id,
            "capture_id": capture_id,
            "capture_why": row["capture_why"],
            "intent": row["intent"],
            "original_payload": original,
            "payload": payload,
            "payload_binding": {
                "kind": "inline",
                "payload_sha256": sha256(payload_bytes).hexdigest(),
            },
            "payload_schema_version": 1,
            "privacy": (
                _stored_submission_value(row, "privacy_json")
                if public_job
                else _owner_record_privacy(row)
            ),
            "provenance": provenance,
            "receipt_refs": receipts,
            "role_claim": (
                _stored_submission_value(row, "role_claim_json")
                if public_job
                else _role_claim(self.profile)
            ),
            "schema_version": 1,
            "source": {"origin": origin, "reference": row["source_reference"]},
            "space_id": row["space_id"],
            "tenant_id": self.profile.tenant_id,
            "trust": _trust(
                self.profile,
                cast(str, row["accepted_at"]),
                (
                    "unverified"
                    if public_job and provenance["content_origin"] == "unknown"
                    else "third_party"
                    if origin == "third_party"
                    else "owner"
                ),
                "captured source material" if origin == "third_party" else "owner supplied capture",
            ),
        }

    def _write_automatic_publication(self, row: sqlite3.Row) -> tuple[str, str, str, str]:
        page_bytes = self._canonical_page_bytes(row, trust="owner")
        proposal = self._proposal_record(
            row,
            proposal_id=cast(str, row["auto_proposal_id"]),
            receipt_id=cast(str, row["auto_proposal_receipt_id"]),
            proposed_bytes=page_bytes,
            proposed_kind="page_update",
            sibling_ids=(cast(str, row["auto_proposal_id"]),),
            supplied_reason="explicit canonical-note action",
            recorded_at=cast(str, row["accepted_at"]),
        )
        proposal_path = _dated_path(
            "history/proposals",
            cast(str, row["accepted_at"]),
            cast(str, row["auto_proposal_id"]),
        )
        portable_write_port(self).put_history("proposal", portable_canonical_json_bytes(proposal))
        self._fault(CaptureFault.AFTER_AUTOMATIC_PROPOSAL_WRITE)
        decision = _decision_record(
            profile=self.profile,
            proposal=proposal,
            decision_id=cast(str, row["auto_decision_id"]),
            outcome=DecisionOutcome.APPROVED,
            edited_bytes=None,
            recorded_at=cast(str, row["accepted_at"]),
        )
        decision_path = _dated_path(
            "history/decisions",
            cast(str, row["accepted_at"]),
            cast(str, row["auto_decision_id"]),
        )
        portable_write_port(self).put_history("decision", portable_canonical_json_bytes(decision))
        self._fault(CaptureFault.AFTER_AUTOMATIC_DECISION_WRITE)
        canonical_path = self._canonical_path(cast(str, row["space_id"]), cast(str, row["page_id"]))
        portable_write_port(self).put_page(canonical_path, page_bytes)
        self._fault(CaptureFault.AFTER_CANONICAL_PAGE_WRITE)
        publication = _publication_record(
            profile=self.profile,
            decision_id=cast(str, row["auto_decision_id"]),
            page_id=cast(str, row["page_id"]),
            publication_id=cast(str, row["publication_id"]),
            published_path=canonical_path,
            published_bytes=page_bytes,
            recorded_at=cast(str, row["accepted_at"]),
        )
        publication_path = _dated_path(
            "history/publications",
            cast(str, row["accepted_at"]),
            cast(str, row["publication_id"]),
        )
        portable_write_port(self).put_history(
            "publication", portable_canonical_json_bytes(publication)
        )
        self._fault(CaptureFault.AFTER_PUBLICATION_WRITE)
        return proposal_path, decision_path, canonical_path, publication_path

    def _capture_title(self, row: sqlite3.Row) -> str:
        supplied = cast(str | None, row["title"])
        if supplied is not None:
            return supplied
        first = next(
            (
                line.strip().lstrip("#").strip()
                for line in cast(str, row["search_text"]).splitlines()
                if line.strip()
            ),
            "Untitled note",
        )
        return first[:200]

    def _canonical_page_bytes(self, row: sqlite3.Row, *, trust: str) -> bytes:
        body = cast(str, row["search_text"])
        rendered = render_markdown(
            fields={
                "actor_id": self.profile.owner_actor_id,
                "modified_at": row["accepted_at"],
                "page_id": row["page_id"],
                # The page frontmatter carries the admitted decision from the
                # capture row, so page, row, and search agree even where
                # admission narrowed or the owner set an explicit tier. For a
                # no-flag capture this is byte-identical to the fixed dict.
                "privacy": _owner_record_privacy(row),
                "provenance": [row["capture_id"]],
                "role_claim": _role_claim(self.profile),
                "schema_version": 1,
                "space_id": row["space_id"],
                "status": "active",
                "tenant_id": self.profile.tenant_id,
                "title": self._capture_title(row),
                "trust": trust,
            },
            body=body if body.endswith("\n") else body + "\n",
        )
        return rendered.encode("utf-8")

    def _capture_receipt(self, capture_id: str) -> CaptureReceipt | None:
        connection = self._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        retained_tier = _retained_privacy_tier(row)
        return CaptureReceipt(
            capture_id=cast(str, row["capture_id"]),
            payload_family=cast(str, row["payload_family"]),
            state=("published" if cast(str | None, row["canonical_path"]) is not None else "inbox"),
            enrichment_state=cast(str, row["enrichment_state"]),
            space_id=cast(str | None, row["space_id"]),
            canonical_path=cast(str | None, row["canonical_path"]),
            requested_tier=retained_tier,
            final_admitted_tier=retained_tier,
        )

    def _receipt_for_delivery(self, delivery_id: str) -> CaptureReceipt | None:
        connection = self._store.connect()
        try:
            row = connection.execute(
                "SELECT capture_id FROM captures WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._capture_receipt(cast(str, row["capture_id"]))


def _retained_privacy_tier(row: sqlite3.Row) -> PrivacyTier:
    """The tier of the retained admitted decision; unreadable evidence is unknown."""
    raw = row["privacy_json"]
    if not isinstance(raw, str):
        return PrivacyTier.UNKNOWN
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return PrivacyTier.UNKNOWN
    if not isinstance(value, dict):
        return PrivacyTier.UNKNOWN
    try:
        return PrivacyTier(value["tier"])
    except KeyError, TypeError, ValueError:
        return PrivacyTier.UNKNOWN


def _owner_record_privacy(row: sqlite3.Row) -> dict[str, object]:
    """Owner capture records keep the fixed local decision unless admission narrowed it."""
    raw = row["privacy_json"]
    if isinstance(raw, str):
        try:
            stored = json.loads(raw)
        except json.JSONDecodeError:
            stored = None
        if isinstance(stored, dict) and stored != _privacy():
            return cast(dict[str, object], stored)
    return _privacy()


def _stored_submission_value(row: sqlite3.Row, column: str) -> dict[str, object]:
    raw = row[column]
    if not isinstance(raw, str):
        raise RuntimeError("public-job capture metadata is unavailable")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("public-job capture metadata is invalid") from error
    if not isinstance(value, dict):
        raise RuntimeError("public-job capture metadata is invalid")
    return cast(dict[str, object], value)


def _can_replace_public_source(submission: CaptureSubmission, existing: sqlite3.Row) -> bool:
    stored_source_reference = cast(str | None, existing["source_reference"])
    stored_provenance = _stored_submission_value(existing, "provenance_json")
    return (
        submission.submission_path is CaptureSubmissionPath.PUBLIC_JOB
        and submission.source_origin is ContentOrigin.THIRD_PARTY
        and cast(str | None, existing["submission_path"]) == CaptureSubmissionPath.PUBLIC_JOB.value
        and cast(str | None, existing["source_origin"]) == "third_party"
        and cast(str | None, existing["action"]) == CaptureAction.QUICK.value
        and cast(str | None, existing["space_id"]) is None
        and submission.action is CaptureAction.QUICK
        and submission.space_id is None
        and isinstance(submission.payload, ReferencePayload)
        and cast(str | None, existing["payload_family"]) == "reference_or_file"
        and stored_source_reference == submission.source_reference
        and stored_provenance == submission.provenance.to_dict()
    )


class CaptureTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def accept(
        self,
        payload: Payload,
        *,
        delivery_id: str,
        action: CaptureAction = CaptureAction.QUICK,
        space_id: str | None = None,
        intent: str | None = None,
        capture_why: str | None = None,
        title: str | None = None,
        privacy_tier: PrivacyTier | None = None,
    ) -> CaptureReceipt:
        engine = self._engine
        with engine._writer_lease.acquire_shared_writer():
            return engine._accept_capture(
                payload,
                delivery_id=delivery_id,
                action=action,
                space_id=space_id,
                intent=intent,
                capture_why=capture_why,
                title=title,
                privacy_tier=privacy_tier,
            )

    def submit(self, submission: CaptureSubmission) -> CaptureReceipt:
        engine = self._engine
        with engine._admit_submission(submission):
            # Phase 3 routes these entrypoints through durable enqueue. Until
            # then, retain the established owner and remote writer behavior.
            lease = (
                engine._writer_lease.acquire_shared_writer()
                if submission.submission_path is CaptureSubmissionPath.OWNER
                else engine._writer_lease_bounded()
            )
            with lease:
                return engine._submit_capture(submission)

    def public_job_sink(self, context: PublicJobCaptureContext) -> PublicJobCaptureSink:
        context.validate_profile(self._engine.profile)
        profile = self._engine.profile
        fingerprint = PublicJobCaptureSink.fingerprint_for(
            str(profile.root), profile.root_identity, profile.tenant_id
        )
        return PublicJobCaptureSink(
            self,
            context=context,
            brain_fingerprint=fingerprint,
        )

    def get(self, capture_id: str) -> CaptureReceipt | None:
        receipt = self._engine._capture_receipt(capture_id)
        return None if receipt is None else project_public_capture_receipt(receipt)

    def retry_enrichment(
        self,
        capture_id: str,
        *,
        delivery_id: str,
    ) -> tuple[ProposalRecord, ...]:
        with self._engine._writer_lease.acquire_shared_writer():
            row = self._engine._capture_row(capture_id)
            if cast(str, row["enrichment_state"]) == EnrichmentState.ENRICHED.value:
                proposal_set = self._engine._proposal_set_row(delivery_id)
                if cast(str, proposal_set["capture_id"]) != capture_id:
                    raise ValueError("conflicting enrichment delivery")
                return self._engine._list_proposals(
                    capture_id=capture_id,
                    status=None,
                    set_delivery_id=delivery_id,
                )
            provider = self._engine._enrichment_provider
            if provider is None:
                raise EnrichmentUnavailable("enrichment provider unavailable")
            request = EnrichmentRequest(
                capture_id=capture_id,
                payload_family=cast(str, row["payload_family"]),
                source_text=cast(str, row["search_text"]),
            )
            try:
                drafts = tuple(provider.enrich(request))
            except EnrichmentUnavailable:
                raise
            except Exception:
                raise EnrichmentUnavailable("enrichment provider unavailable") from None
            proposals = self._engine._propose(capture_id, drafts, delivery_id)
            with self._engine._store.transaction() as connection:
                connection.execute(
                    "UPDATE captures SET enrichment_state = ? WHERE capture_id = ?",
                    (EnrichmentState.ENRICHED.value, capture_id),
                )
            return proposals
