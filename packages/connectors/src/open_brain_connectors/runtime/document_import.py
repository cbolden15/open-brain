"""Foreground document import into an explicitly selected, existing Brain."""

from __future__ import annotations

import hmac
import re
from pathlib import Path

from open_brain_engine.engine import (
    CaptureCustodyReceipt,
    LocalEngineContext,
    PrivacyDecision,
    PublicJobCaptureContext,
    ReferencePayload,
    open_local_engine,
)

from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.document_files import SelectedDocument
from open_brain_connectors.runtime.document_parser import MAX_CAPTURE_TEXT, REVISION_HEADER_PREFIX
from open_brain_connectors.runtime.local_document import LocalDocumentSourceAdapter
from open_brain_connectors.runtime.source_host import existing_source_profile


def document_privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": False},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "personal_local_only",
            "tier": "personal",
        }
    )


def document_preview(document: SelectedDocument, connection_id: str) -> dict[str, object]:
    if connection_id != document.connection_id:
        raise ConnectorContractError("document_preview_changed")
    adapter = LocalDocumentSourceAdapter()
    record = document.record
    selection = adapter.file_selection(
        connection_id=connection_id,
        document_id=record.document_id,
        file_kind=record.file_kind,
    )
    page = adapter.preview(selection, (record,), privacy=document_privacy())
    _revision_payload(record.revision_id, record.text, page.records[0].source_reference)
    return {
        **page.to_dict(),
        "status": "ready",
        "preview_id": document.preview_id,
        "extracted_text": record.text,
        "revision_id": record.revision_id,
        "history_retained_after_source_removal": True,
    }


def _revision_payload(revision_id: str, text: str, source_reference: str) -> ReferencePayload:
    # The adapter has already scanned all user text. This generated fingerprint
    # belongs in the raw capture, not in the user's text redaction check: a SHA
    # otherwise resembles a secret. Keep it in Portable history without changing
    # the stable source reference required for active-revision replacement.
    body = f"{REVISION_HEADER_PREFIX}{revision_id}\n\n{text}"
    if len(body) > MAX_CAPTURE_TEXT:
        raise ConnectorContractError("document_text_too_large")
    return ReferencePayload(url=source_reference, supplied_text=body)


def _existing_profile(root: Path) -> LocalEngineContext:
    try:
        return existing_source_profile(root)
    except ConnectorContractError as error:
        raise ConnectorContractError("document_brain_unavailable") from error


def import_document(
    document: SelectedDocument,
    *,
    connection_id: str,
    preview_id: str,
    brain_root: Path,
) -> dict[str, object]:
    if (
        connection_id != document.connection_id
        or re.fullmatch(
            r"[0-9a-f]{64}",
            preview_id,
        )
        is None
        or not hmac.compare_digest(
            document.preview_id,
            preview_id,
        )
    ):
        raise ConnectorContractError("document_preview_changed")
    adapter = LocalDocumentSourceAdapter()
    record = document.record
    selection = adapter.file_selection(
        connection_id=connection_id,
        document_id=record.document_id,
        file_kind=record.file_kind,
    )
    intake = adapter.intake(selection, record, privacy=document_privacy())
    payload = _revision_payload(record.revision_id, record.text, intake.source_reference)
    try:
        tasks = open_local_engine(_existing_profile(brain_root))
        actor = "actor_22222222-2222-4222-8222-222222222222"
        context = PublicJobCaptureContext.create(
            profile=tasks.profile,
            actor_id=actor,
            role_claim={
                "actor_id": actor,
                "tenant_id": tasks.profile.tenant_id,
                "role_id": "role_22222222-2222-4222-8222-222222222222",
                "role_claim_id": "role_claim_22222222-2222-4222-8222-222222222222",
                "capabilities": ["capture.accept"],
            },
        )
        receipt = tasks.capture.public_job_sink(context).submit(
            payload,
            delivery_id=intake.key.delivery_id(),
            source_origin="third_party",
            source_reference=intake.source_reference,
            provenance=intake.provenance(),
            privacy=intake.privacy,
            intent="reference",
            title=intake.title,
        )
    except ConnectorContractError:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise ConnectorContractError("document_import_failed") from error
    result: dict[str, object] = {
        "status": "queued"
        if isinstance(receipt, CaptureCustodyReceipt)
        else ("unchanged" if receipt.duplicate else "imported"),
        "source_reference": intake.source_reference,
        "document_id": record.document_id,
        "revision_id": record.revision_id,
        "history_retained_after_source_removal": True,
    }
    if isinstance(receipt, CaptureCustodyReceipt):
        result["ingestion_id"] = receipt.ingestion_id
    else:
        result["capture_id"] = receipt.capture_id
    return result
