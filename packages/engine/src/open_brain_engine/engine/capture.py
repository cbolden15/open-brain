"""Capture reservation, durable replay, and capture task implementation."""

from __future__ import annotations

import base64
import json
import sqlite3
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.providers.base import EnrichmentState
from open_brain_engine.storage.markdown import render_markdown

from .contracts import (
    CaptureAction,
    CaptureFault,
    CaptureReceipt,
    CaptureSubmission,
    DecisionOutcome,
    EnrichmentRequest,
    EnrichmentUnavailable,
    FilePayload,
    Payload,
    ProposalRecord,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
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

if TYPE_CHECKING:
    from .local import BrainEngine


class CaptureOperations(_LocalEngineOperations):
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
            )
        )

    def _submit_capture(self, submission: CaptureSubmission) -> CaptureReceipt:
        submission.validate_profile(self.profile)
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
                        portable_canonical_json_bytes(submission.privacy.to_dict()).decode("utf-8"),
                        portable_canonical_json_bytes(submission.provenance.to_dict()).decode(
                            "utf-8"
                        ),
                        submission.submission_path.value,
                    ),
                )
        if conflict is not None:
            self._quarantine(delivery_id, expected=conflict[0], actual=conflict[1])
            raise ValueError("conflicting delivery")
        if not duplicate:
            self._fault(CaptureFault.AFTER_CAPTURE_RESERVATION)
        row = self._capture_row(capture_id)
        self._process_capture(row)
        receipt = self._capture_receipt(capture_id)
        if receipt is None:
            raise RuntimeError("capture state unavailable")
        return project_public_capture_receipt(CaptureReceipt(
            capture_id=receipt.capture_id,
            payload_family=receipt.payload_family,
            state=receipt.state,
            enrichment_state=receipt.enrichment_state,
            space_id=receipt.space_id,
            canonical_path=receipt.canonical_path,
            duplicate=duplicate,
        ))

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
        public_job = submission_path == "public_job"
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
            "privacy": _stored_submission_value(row, "privacy_json") if public_job else _privacy(),
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
                "privacy": _privacy(),
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
        return CaptureReceipt(
            capture_id=cast(str, row["capture_id"]),
            payload_family=cast(str, row["payload_family"]),
            state=("published" if cast(str | None, row["canonical_path"]) is not None else "inbox"),
            enrichment_state=cast(str, row["enrichment_state"]),
            space_id=cast(str | None, row["space_id"]),
            canonical_path=cast(str | None, row["canonical_path"]),
        )


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
    ) -> CaptureReceipt:
        with self._engine._writer_lease.acquire_shared_writer():
            return self._engine._accept_capture(
                payload,
                delivery_id=delivery_id,
                action=action,
                space_id=space_id,
                intent=intent,
                capture_why=capture_why,
                title=title,
            )

    def submit(self, submission: CaptureSubmission) -> CaptureReceipt:
        with self._engine._writer_lease.acquire_shared_writer():
            return self._engine._submit_capture(submission)

    def public_job_sink(self, context: PublicJobCaptureContext) -> PublicJobCaptureSink:
        context.validate_profile(self._engine.profile)
        return PublicJobCaptureSink(self, context=context)

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
