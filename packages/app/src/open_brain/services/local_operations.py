"""Shared default-product operations and public representations for CLI and MCP."""

from __future__ import annotations

from hashlib import sha256

from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine import (
    CaptureReceipt,
    CaptureTask,
    EngineTaskSet,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    ReconciliationTask,
    RetrievalResult,
    RetrievalTask,
    TextPayload,
)
from open_brain_engine.storage.locks import LockBusyError
from open_brain_engine.storage.sqlite import is_database_busy

_MCP_IDENTITY = "cf350566-c33d-49ab-bef7-e0d760171ae1"


def mcp_capture_sink(tasks: EngineTaskSet) -> PublicJobCaptureSink:
    actor = "actor_" + _MCP_IDENTITY
    context = PublicJobCaptureContext.create(
        profile=tasks.profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_" + _MCP_IDENTITY,
            "role_id": "role_" + _MCP_IDENTITY,
            "tenant_id": tasks.profile.tenant_id,
        },
    )
    return tasks.capture.public_job_sink(context)


def capture_text(
    capture: CaptureTask | PublicJobCaptureSink, text: str, *, delivery_id: str
) -> CaptureReceipt:
    payload = TextPayload(text)
    if isinstance(capture, PublicJobCaptureSink):
        # Bind exact input bytes as well as the engine-normalized payload on replay.
        source = "urn:open-brain:mcp:" + sha256(text.encode("utf-8")).hexdigest()
        return capture.submit(
            payload,
            delivery_id=delivery_id,
            source_origin=ContentOrigin.UNKNOWN,
            source_reference=source,
            provenance=Provenance.create(
                source_ref=source,
                content_origin=ContentOrigin.UNKNOWN,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            ),
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.PERSONAL,
                reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
                policy_version="privacy-v1",
                authority=Authority(cloud=False, external_egress=False),
            ),
        )
    return capture.accept(payload, delivery_id=delivery_id)


def search_brain(
    retrieval: RetrievalTask, reconciliation: ReconciliationTask, query: str, *, limit: int
) -> tuple[RetrievalResult, ...]:
    reconciliation.reconcile()
    return retrieval.search(query, limit=limit)


def capture_result(receipt: CaptureReceipt) -> dict[str, object]:
    return {
        "capture_id": receipt.capture_id,
        "duplicate": receipt.duplicate,
        "payload_family": receipt.payload_family,
        "state": receipt.state,
        "status": "captured",
    }


def search_result(results: tuple[RetrievalResult, ...]) -> dict[str, object]:
    return {
        "results": [
            {
                "capture_id": result.capture_id,
                "excerpt": result.excerpt,
                "payload_family": result.payload_family,
                "record_type": result.record_type,
                "result_id": result.result_id,
                "source_origin": result.provenance.source_origin,
                "title": result.title,
                "trust": result.trust,
                "explanation": result.explanation,
            }
            for result in results
        ],
        "status": "ok",
    }


def database_is_busy(error: BaseException) -> bool:
    """Recognize typed contention through redacting storage exception wrappers."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LockBusyError):
            return True
        if is_database_busy(current):
            return True
        current = current.__cause__ or current.__context__
    return False
