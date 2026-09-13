"""Confined immutable renderers for synthesis pages and derived claim views."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.core.ports import (
    PutDisposition,
    RedactedMarkdownDocument,
    RedactionReceipt,
)
from open_brain_engine.storage.frontmatter import (
    AtomicMarkdownReader,
    AtomicMarkdownSink,
    markdown_relative_path,
    rendered_markdown_bytes,
)

from .index import ClaimRecord, ClaimStatus
from .synthesis_store import DurableSynthesisRecord


class RenderDisposition(StrEnum):
    CREATED = "created"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class RenderResult:
    disposition: RenderDisposition
    relative_path: PurePosixPath
    digest_sha256: str
    document: RedactedMarkdownDocument


@dataclass(frozen=True, slots=True)
class ClaimViewResult:
    current: RenderResult
    archive: RenderResult


class _Renderer:
    def __init__(self, *, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("invalid ledger render root")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        self._sink = AtomicMarkdownSink(root=root)
        self._reader = AtomicMarkdownReader(root=root)

    def _persist(self, document: RedactedMarkdownDocument) -> RenderResult:
        payload = rendered_markdown_bytes(document)
        digest = sha256(payload).hexdigest()
        receipt = self._sink.write_if_absent(document)
        if (
            receipt.record_id != document.document_id
            or receipt.digest_sha256 != digest
            or receipt.disposition
            not in {PutDisposition.CREATED, PutDisposition.DUPLICATE}
            or self._reader.read_back(document.document_id) != payload
        ):
            raise ValueError("ledger render verification failed")
        return RenderResult(
            disposition=(
                RenderDisposition.CREATED
                if receipt.disposition is PutDisposition.CREATED
                else RenderDisposition.UNCHANGED
            ),
            relative_path=markdown_relative_path(document.document_id),
            digest_sha256=digest,
            document=document,
        )


class SynthesisRenderer(_Renderer):
    def render(self, record: DurableSynthesisRecord) -> RenderResult:
        if not isinstance(record, DurableSynthesisRecord):
            raise ValueError("invalid durable synthesis render input")
        document = record.document
        if (
            RedactedMarkdownDocument.from_canonical_bytes(document.canonical_bytes()) != document
            or sha256(document.canonical_bytes()).hexdigest()
            != record.document_digest_sha256
        ):
            raise ValueError("invalid durable synthesis render input")
        return self._persist(document)


class ClaimViewRenderer(_Renderer):
    """Write content-addressed projections and never remove prior view generations."""

    def render(
        self,
        *,
        claims: tuple[ClaimRecord, ...],
        privacy: PrivacyDecision,
    ) -> ClaimViewResult:
        if not isinstance(claims, tuple) or any(
            not isinstance(claim, ClaimRecord) for claim in claims
        ):
            raise ValueError("invalid claim render input")
        if not isinstance(privacy, PrivacyDecision):
            raise ValueError("invalid claim render privacy")
        ordered = tuple(sorted(claims, key=lambda claim: claim.claim_id))
        for claim in ordered:
            claim.validate()
        current = tuple(claim for claim in ordered if claim.status is not ClaimStatus.RETIRED)
        archive = tuple(claim for claim in ordered if claim.status is ClaimStatus.RETIRED)
        return ClaimViewResult(
            current=self._persist(_claim_document("current", current, privacy=privacy)),
            archive=self._persist(_claim_document("archive", archive, privacy=privacy)),
        )


def _claim_document(
    view: str,
    claims: tuple[ClaimRecord, ...],
    *,
    privacy: PrivacyDecision,
) -> RedactedMarkdownDocument:
    identity = {
        "claims": [
            {
                "citation_ids": [citation.citation_id for citation in claim.citations],
                "claim_id": claim.claim_id,
                "status": claim.status.value,
            }
            for claim in claims
        ],
        "view": view,
    }
    source_digest = sha256(canonical_json_bytes(identity)).hexdigest()
    document_id = "ledger_" + view + "_" + source_digest
    frontmatter = {
        "claim_ids": [claim.claim_id for claim in claims],
        "derived": True,
        "view": view,
    }
    lines = [f"# Ledger {view}", ""]
    for claim in claims:
        citations = "".join(
            f" [{citation.citation_id}](<{citation.destination}>)"
            for citation in claim.citations
        )
        lines.append(
            f"- `{claim.claim_id}` [{claim.status.value}] {claim.text}{citations}"
        )
    body = "\n".join(lines).rstrip() + "\n"
    output_digest = RedactedMarkdownDocument.output_digest_sha256(frontmatter, body)
    return RedactedMarkdownDocument.create(
        document_id=document_id,
        logical_key=document_id,
        privacy_decision=privacy,
        frontmatter=frontmatter,
        body=body,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256=source_digest,
            output_digest_sha256=output_digest,
            policy_version="ledger-derived-view-v1",
        ),
    )
