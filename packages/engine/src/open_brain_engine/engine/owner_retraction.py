"""Durable local-owner preview/commit with atomic journal and approval evidence."""

from __future__ import annotations

import secrets
from datetime import timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from .capture_lifecycle import _journal
from .capture_lifecycle_contracts import LifecycleError, canonical_bytes, decode_canonical, digest
from .normalization import _timestamp
from .owner_retraction_contracts import (
    RetractionError,
    approval_record,
    impact_value,
    lifecycle_request,
    make_receipt,
    operation_identifier,
    preview_identifier,
)
from .paging import read_snapshot
from .retraction_impact import RetractionImpact, _rows_for, inspect_retraction
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .capture_lifecycle_journal import LifecycleJournal
    from .local import BrainEngine


def _impact(impact: RetractionImpact) -> dict[str, object]:
    return impact_value(
        {
            "target": {"kind": impact.target_kind, "id": impact.target_id},
            "capture_ids": list(impact.capture_ids),
            "membership_version": impact.membership_version,
            "membership_sha256": impact.membership_sha256,
            "items": [
                {"kind": item.kind, "id": item.id, "effect": item.effect} for item in impact.items
            ],
            "counts": dict(impact.counts),
            "preimage_sha256": impact.preimage_sha256,
        }
    )


def _public_receipt(receipt: dict[str, object]) -> dict[str, object]:
    return {
        "dto_version": 1,
        "status": "committed",
        "operation_id": receipt["operation_id"],
        "receipt_id": receipt["receipt_id"],
        "counts": receipt["counts"],
        "history_retained": True,
    }


def _stored_approval(journal: LifecycleJournal, operation_id: str) -> dict[str, object] | None:
    row = journal.connection.execute(
        "SELECT * FROM retraction_approvals WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        request = decode_canonical(bytes(row["request_bytes"]))
        receipt = decode_canonical(bytes(row["receipt_bytes"]))
        record = approval_record(request, receipt)
        if digest(request) != row["request_sha256"] or digest(receipt) != row["receipt_sha256"]:
            raise RetractionError("invalid_evidence")
        request = cast(dict[str, object], record["request"])
        receipt = cast(dict[str, object], record["receipt"])
        expected = lifecycle_request(request)
        if (
            expected.operation_id != operation_id
            or journal._state().requests.get(operation_id) != expected
        ):
            raise RetractionError("invalid_evidence")
        status = journal.status(operation_id)
        if (
            status["status"] != "committed"
            or status["receipt_sha256"] != receipt["journal_receipt_sha256"]
        ):
            raise RetractionError("invalid_evidence")
        return record
    except LifecycleError, ValueError, TypeError, KeyError:
        raise RetractionError("invalid_evidence") from None


def _insert_approval(
    journal: LifecycleJournal, request: dict[str, object], receipt: dict[str, object]
) -> None:
    approval_record(request, receipt)
    journal.connection.execute(
        "INSERT INTO retraction_approvals VALUES(?,?,?,?,?)",
        (
            request["operation_id"],
            canonical_bytes(request),
            digest(request),
            canonical_bytes(receipt),
            digest(receipt),
        ),
    )


class OwnerRetractionTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def _owner(self, authority: EffectiveAuthority) -> None:
        try:
            self._engine.lifecycle._owner(authority)
        except LifecycleError as error:
            raise RetractionError(
                "unsupported_capability" if error.code == "invalid_arguments" else error.code
            ) from None
        if authority.space_ids is not None:
            raise RetractionError("unsupported_capability")

    @staticmethod
    def _current(journal: LifecycleJournal, authority: EffectiveAuthority) -> None:
        if journal.connection.execute("PRAGMA user_version").fetchone()[0] not in {19, 20, 21}:
            raise RetractionError("operation_pending")
        if authority.brain_id is not None and (
            authority.brain_id != journal.brain_id or authority.issuer_epoch != journal.issuer_epoch
        ):
            raise RetractionError("revision_changed")

    def _inspect(
        self, journal: LifecycleJournal, target: dict[str, object], expected: str | None = None
    ) -> RetractionImpact:
        try:
            impact = inspect_retraction(
                journal,
                capture_id=cast(str, target["id"]) if target["kind"] == "capture" else None,
                session_id=cast(str, target["id"]) if target["kind"] == "session" else None,
                expected_preimage_sha256=expected,
            )
        except LifecycleError as error:
            raise RetractionError(
                "not_found" if error.code == "operation_not_found" else error.code
            ) from None
        # There is no public v1 item identity for an approved, not-yet-created copy.
        # Other unrelated pending approvals do not change this selected impact.
        if (
            next(
                _rows_for(
                    journal.connection,
                    "SELECT 1 FROM sharing_decisions d JOIN sharing_previews p USING(preview_id) "
                    "WHERE p.original_capture_id IN ({ids}) AND d.decision='approve' "
                    "AND d.copy_capture_id IS NULL LIMIT 1",
                    impact.freshness_capture_ids,
                ),
                None,
            )
            is not None
        ):
            raise RetractionError("operation_pending")
        return impact

    def preview(
        self,
        *,
        authority: EffectiveAuthority,
        capture_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        self._owner(authority)
        if (capture_id is None) == (session_id is None):
            raise RetractionError("invalid_arguments")
        target: dict[str, object] = {
            "kind": "capture" if capture_id is not None else "session",
            "id": capture_id if capture_id is not None else session_id,
        }
        with self._engine.lifecycle._transaction(authority) as journal:
            self._current(journal, authority)
            impact = self._inspect(journal, target)
            now = self._engine._clock()
            created, expires = _timestamp(now), _timestamp(now + timedelta(hours=1))
            journal.connection.execute(
                "DELETE FROM retraction_previews WHERE expires_at<=?", (created,)
            )
            live = journal.connection.execute(
                "SELECT count(*) FROM retraction_previews WHERE principal_id=?",
                (authority.principal_id,),
            ).fetchone()[0]
            if live >= 16:
                raise RetractionError("operation_pending")
            identity = "retprev_" + secrets.token_urlsafe(32)
            value = _impact(impact)
            journal.connection.execute(
                "INSERT INTO retraction_previews VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    authority.principal_id,
                    journal.brain_id,
                    journal.issuer_epoch,
                    created,
                    expires,
                    canonical_bytes(value),
                    digest(value),
                ),
            )
            return self._page(impact, identity, expires, 0)

    @staticmethod
    def _page(
        impact: RetractionImpact, identity: str, expires: str, item_page: int
    ) -> dict[str, object]:
        value = impact.page(item_page)
        value.update(status="preview", preview_id=identity, expires_at=expires)
        return value

    def _preview(
        self, journal: LifecycleJournal, identity: str, authority: EffectiveAuthority
    ) -> tuple[RetractionImpact, str]:
        row = journal.connection.execute(
            "SELECT * FROM retraction_previews WHERE preview_id=? AND principal_id=?",
            (identity, authority.principal_id),
        ).fetchone()
        if row is None:
            raise RetractionError("preview_not_found")
        if (row["brain_id"], row["issuer_epoch"]) != (journal.brain_id, journal.issuer_epoch):
            raise RetractionError("revision_changed")
        now = _timestamp(self._engine._clock())
        if not row["created_at"] <= now < row["expires_at"]:
            raise RetractionError("preview_expired")
        try:
            retained = impact_value(decode_canonical(bytes(row["impact_bytes"])))
            if digest(retained) != row["impact_sha256"]:
                raise RetractionError("invalid_evidence")
            impact = self._inspect(
                journal,
                cast(dict[str, object], retained["target"]),
                cast(str, retained["preimage_sha256"]),
            )
            if _impact(impact) != retained:
                raise RetractionError("revision_changed")
            return impact, row["expires_at"]
        except LifecycleError:
            raise RetractionError("invalid_evidence") from None

    def preview_page(
        self, preview_id: str, *, authority: EffectiveAuthority, item_page: int = 0
    ) -> dict[str, object]:
        self._owner(authority)
        preview_identifier(preview_id)
        with read_snapshot(self._engine) as connection:
            journal = _journal(connection)
            self._current(journal, authority)
            impact, expires = self._preview(journal, preview_id, authority)
            try:
                return self._page(impact, preview_id, expires, item_page)
            except LifecycleError:
                raise RetractionError("invalid_arguments") from None

    def commit(
        self, preview_id: str, operation_id: str, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        self._owner(authority)
        preview_identifier(preview_id)
        operation_identifier(operation_id)
        binding = sha256(preview_id.encode("ascii")).hexdigest()
        with self._engine.lifecycle._transaction(authority) as journal:
            self._current(journal, authority)
            prior = _stored_approval(journal, operation_id)
            if prior is not None:
                if cast(dict[str, object], prior["request"])["preview_binding_sha256"] != binding:
                    raise RetractionError("operation_conflict")
                return _public_receipt(cast(dict[str, object], prior["receipt"]))
            if operation_id in journal._state().requests:
                raise RetractionError("operation_conflict")
            impact, _ = self._preview(journal, preview_id, authority)
            request: dict[str, object] = {
                "contract": "ob.owner-retraction-approval.v1",
                "operation_id": operation_id,
                "brain_id": journal.brain_id,
                "issuer_epoch": journal.issuer_epoch,
                "principal_id": authority.principal_id,
                "preview_binding_sha256": binding,
                "impact": _impact(impact),
            }
            expected = lifecycle_request(request)
            request["lifecycle_request_sha256"] = expected.request_sha256
            status = self._engine.lifecycle._retract_in_transaction(journal, expected)
            receipt = make_receipt(request, cast(str, status["receipt_sha256"]))
            _insert_approval(journal, request, receipt)
            journal.connection.execute(
                "DELETE FROM retraction_previews WHERE preview_id=?", (preview_id,)
            )
            return _public_receipt(receipt)

    def status(self, operation_id: str, *, authority: EffectiveAuthority) -> dict[str, object]:
        self._owner(authority)
        operation_identifier(operation_id)
        with read_snapshot(self._engine) as connection:
            journal = _journal(connection)
            self._current(journal, authority)
            record = _stored_approval(journal, operation_id)
            if record is None:
                raise RetractionError("operation_not_found")
            return _public_receipt(cast(dict[str, object], record["receipt"]))
