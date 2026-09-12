"""Provider-neutral messaging sync with review-only personal intent routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from open_brain_engine.core.ids import capture_id_for, review_id_for
from open_brain_engine.core.models import Intent

from open_brain_legacy._compat.open_brain.integrations.config import IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.ports import (
    Capability,
    ProviderSyncRequest,
    ProviderSyncResult,
    ReviewBoundWriter,
    ReviewDisposition,
    ReviewWriteKind,
    ReviewWriteRequest,
    ReviewWriteResult,
    SyncStatus,
)

_OPAQUE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class MessageConfidence(StrEnum):
    """Provider-neutral confidence bands for message-derived candidates."""

    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class MessageCandidate:
    """Opaque personal-message references; raw message content stays provider-side."""

    message_ref: str
    content_ref: str
    confidence: MessageConfidence

    def __post_init__(self) -> None:
        if (
            _OPAQUE_ID_PATTERN.fullmatch(self.message_ref) is None
            or _OPAQUE_ID_PATTERN.fullmatch(self.content_ref) is None
            or not isinstance(self.confidence, MessageConfidence)
        ):
            raise ValueError("invalid message candidate")


@dataclass(frozen=True, slots=True)
class MessageBatch:
    """One cursor-bound provider page containing opaque candidate references."""

    resource_ref: str
    cursor_ref: str | None
    next_cursor_ref: str
    candidates: tuple[MessageCandidate, ...]

    def __post_init__(self) -> None:
        if (
            _OPAQUE_ID_PATTERN.fullmatch(self.resource_ref) is None
            or (
                self.cursor_ref is not None
                and _OPAQUE_ID_PATTERN.fullmatch(self.cursor_ref) is None
            )
            or _OPAQUE_ID_PATTERN.fullmatch(self.next_cursor_ref) is None
            or not isinstance(self.candidates, tuple)
            or any(not isinstance(candidate, MessageCandidate) for candidate in self.candidates)
        ):
            raise ValueError("invalid message batch")


class MessageSource(Protocol):
    """Read one opaque, cursor-bound page from an injected messaging provider."""

    def fetch(self, request: ProviderSyncRequest) -> MessageBatch: ...


class MessagingCursorStore(Protocol):
    """Persist only opaque messaging cursors with compare-and-swap semantics."""

    def current_cursor(self, resource_ref: str) -> str | None: ...

    def cursor_was_processed(self, resource_ref: str, cursor_ref: str | None) -> bool: ...

    def advance_cursor(
        self,
        resource_ref: str,
        *,
        expected_cursor: str | None,
        next_cursor: str,
    ) -> bool: ...


@dataclass(slots=True)
class MessagingIntegration:
    """Queue message-derived candidates for review without applying personal actions."""

    source: MessageSource
    cursors: MessagingCursorStore
    reviews: ReviewBoundWriter
    config: IntegrationConfig = IntegrationConfig()

    def sync(self, request: ProviderSyncRequest) -> ProviderSyncResult:
        if (
            not isinstance(request, ProviderSyncRequest)
            or request.capability is not Capability.MESSAGING
        ):
            raise ValueError("invalid messaging sync request")
        if not self.config.live_adapter_enabled(Capability.MESSAGING):
            return _result(status=SyncStatus.UNSUPPORTED)

        try:
            current_cursor = self.cursors.current_cursor(request.resource_ref)
            _validate_optional_cursor(current_cursor)
        except Exception:
            return _result(status=SyncStatus.RETRYABLE)
        if request.cursor_ref != current_cursor:
            try:
                processed = self.cursors.cursor_was_processed(
                    request.resource_ref,
                    request.cursor_ref,
                )
                if type(processed) is not bool:
                    raise TypeError("invalid processed cursor result")
            except Exception:
                return _result(status=SyncStatus.RETRYABLE, next_cursor=current_cursor)
            return _result(
                status=SyncStatus.COMPLETED if processed else SyncStatus.RETRYABLE,
                next_cursor=current_cursor,
            )

        try:
            batch = self.source.fetch(request)
            if not isinstance(batch, MessageBatch):
                raise TypeError("invalid messaging provider response")
            batch = MessageBatch(
                resource_ref=batch.resource_ref,
                cursor_ref=batch.cursor_ref,
                next_cursor_ref=batch.next_cursor_ref,
                candidates=tuple(
                    MessageCandidate(
                        message_ref=candidate.message_ref,
                        content_ref=candidate.content_ref,
                        confidence=candidate.confidence,
                    )
                    for candidate in batch.candidates
                ),
            )
            if (
                batch.resource_ref != request.resource_ref
                or batch.cursor_ref != request.cursor_ref
            ):
                raise ValueError("messaging provider cursor mismatch")
        except Exception:
            return _result(status=SyncStatus.RETRYABLE, next_cursor=current_cursor)
        if request.dry_run:
            return _result(
                status=SyncStatus.DRY_RUN,
                created=len(batch.candidates),
                next_cursor=batch.next_cursor_ref,
            )

        created = 0
        try:
            for candidate in batch.candidates:
                review_request = _proposal_request(request.resource_ref, candidate)
                review_result = self.reviews.submit(review_request)
                _validate_review_result(review_request, review_result)
                if review_result.disposition is ReviewDisposition.BLOCKED:
                    return _result(status=SyncStatus.RETRYABLE, next_cursor=current_cursor)
                if review_result.disposition is ReviewDisposition.QUEUED:
                    created += 1
        except Exception:
            return _result(status=SyncStatus.RETRYABLE, next_cursor=current_cursor)

        try:
            advanced = self.cursors.advance_cursor(
                request.resource_ref,
                expected_cursor=current_cursor,
                next_cursor=batch.next_cursor_ref,
            )
        except Exception:
            return _result(status=SyncStatus.RETRYABLE, next_cursor=current_cursor)
        if not advanced:
            return _result(status=SyncStatus.RETRYABLE, next_cursor=current_cursor)
        return _result(
            status=SyncStatus.COMPLETED,
            created=created,
            next_cursor=batch.next_cursor_ref,
        )


def _proposal_request(resource_ref: str, candidate: MessageCandidate) -> ReviewWriteRequest:
    digest = sha256(
        f"{resource_ref}\0{candidate.message_ref}\0{candidate.confidence.value}".encode()
    ).hexdigest()
    capture_id = capture_id_for(
        {
            "identity_version": 1,
            "source": "messaging",
            "content_ref": candidate.content_ref,
        }
    )
    return ReviewWriteRequest(
        request_id=f"message_proposal_{digest}",
        kind=ReviewWriteKind.PROPOSAL,
        content_ref=candidate.content_ref,
        review_id=str(review_id_for(capture_id, Intent.ACTION_CANDIDATE.value)),
    )


def _validate_review_result(
    request: ReviewWriteRequest,
    result: ReviewWriteResult,
) -> None:
    if not isinstance(result, ReviewWriteResult):
        raise TypeError("invalid review writer response")
    validated = ReviewWriteResult(
        request_id=result.request_id,
        disposition=result.disposition,
        review_id=result.review_id,
    )
    if validated.request_id != request.request_id or validated.review_id != request.review_id:
        raise ValueError("review writer response mismatch")


def _validate_optional_cursor(cursor: str | None) -> None:
    if cursor is not None and _OPAQUE_ID_PATTERN.fullmatch(cursor) is None:
        raise ValueError("invalid stored messaging cursor")


def _result(
    *,
    status: SyncStatus,
    created: int = 0,
    next_cursor: str | None = None,
) -> ProviderSyncResult:
    return ProviderSyncResult(
        capability=Capability.MESSAGING,
        status=status,
        created=created,
        updated=0,
        removed=0,
        next_cursor_ref=next_cursor,
    )
