"""Owner-only operational access to the durable capture-ingestion journal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from open_brain_engine.storage.locks import LockBusyError

from .consent_contracts import EgressMode
from .contracts import IngestionDrainResult, IngestionStatus, IngestionSummary
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine


class JournalOperationError(ValueError):
    """Closed, content-free failure for owner journal operations."""

    def __init__(self, code: str) -> None:
        if code not in {
            "discard_confirmation_required",
            "invalid_request",
            "not_quarantined",
            "operation_unavailable",
            "owner_required",
            "writer_busy",
        }:
            raise ValueError("invalid journal operation error")
        super().__init__(code)


class JournalTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def status(
        self, *, authority: EffectiveAuthority, limit: int = 100
    ) -> tuple[IngestionStatus, ...]:
        self._require_owner(authority)
        return self._engine.ingestion.status(limit=limit)

    def summary(self, *, authority: EffectiveAuthority) -> IngestionSummary:
        self._require_owner(authority)
        return self._engine.ingestion.summary()

    def drain(self, *, authority: EffectiveAuthority) -> IngestionDrainResult:
        self._require_owner(authority)
        try:
            with self._engine._writer_lease.acquire_shared_writer():
                self._engine._recover_captures_locked()
                receipts = self._engine.ingestion.drain_locked()
        except LockBusyError:
            raise JournalOperationError("writer_busy") from None
        return IngestionDrainResult(materialized_count=len(receipts), receipts=receipts)

    def retry(self, delivery_id: str, *, authority: EffectiveAuthority) -> None:
        self._require_owner(authority)
        try:
            self._engine.ingestion.retry(delivery_id)
        except ValueError as error:
            raise JournalOperationError("not_quarantined") from error

    def discard(self, delivery_id: str, *, reason: str, authority: EffectiveAuthority) -> None:
        self._require_owner(authority)
        try:
            self._engine.ingestion.discard(delivery_id, reason=reason)
        except ValueError as error:
            raise JournalOperationError("not_quarantined") from error

    def _require_owner(self, authority: EffectiveAuthority) -> None:
        profile = self._engine.profile
        if (
            not isinstance(authority, EffectiveAuthority)
            or not authority.owner
            or authority.principal_id != profile.owner_actor_id
            or authority.egress_mode is not EgressMode.OWNER_LOCAL
        ):
            raise JournalOperationError("owner_required")


__all__ = ["JournalOperationError", "JournalTasks"]
