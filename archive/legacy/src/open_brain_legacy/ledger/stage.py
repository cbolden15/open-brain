"""Transcript-free, one-record ledger staging values."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.ports import RedactionReceipt
from open_brain_engine.storage.filesystem import atomic_write_new, read_confined

from .models import LedgerScanRecord, LedgerTaxonomy, LedgerValidationError
from .scan import LedgerSourceManifest

_STAGE_POLICY_VERSION = "ledger-stage-v1"
_TRANSCRIPT_BLOCK = re.compile(
    r"<!-- open-brain:transcript -->.*?<!-- /open-brain:transcript -->\s*",
    re.DOTALL,
)


class StageDisposition(StrEnum):
    STAGED = "staged"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class ManifestStageResult:
    disposition: StageDisposition
    key: str
    relative_path: PurePosixPath | None
    staged_digest_sha256: str | None


def stage_manifest_entry(
    *,
    manifest: LedgerSourceManifest,
    key: str,
    source_root: Path,
    scratch_root: Path,
) -> ManifestStageResult:
    """Write one immutable, transcript-free scratch input bound to a scan manifest."""
    if not isinstance(manifest, LedgerSourceManifest) or not isinstance(key, str):
        raise LedgerValidationError("invalid ledger manifest stage input")
    manifest.validate()
    entry = manifest.entry_for(key)
    if entry is None:
        return ManifestStageResult(StageDisposition.MISSING, key, None, None)
    if (
        not isinstance(source_root, Path)
        or not source_root.is_absolute()
        or not isinstance(scratch_root, Path)
        or not scratch_root.is_absolute()
        or source_root == scratch_root
    ):
        raise LedgerValidationError("ledger manifest stage is not confined")
    try:
        source_bytes = read_confined(root=source_root, relative=entry.source_locator)
    except Exception:
        raise LedgerValidationError("ledger manifest source unavailable") from None
    if (
        source_bytes is None
        or len(source_bytes) != entry.size_bytes
        or sha256(source_bytes).hexdigest() != entry.content_digest_sha256
    ):
        raise LedgerValidationError("ledger manifest source binding mismatch")
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise LedgerValidationError("invalid ledger manifest source") from None
    if source_text.count("<!-- open-brain:transcript -->") != source_text.count(
        "<!-- /open-brain:transcript -->"
    ):
        raise LedgerValidationError("invalid ledger transcript boundary")
    staged_body = _TRANSCRIPT_BLOCK.sub("", source_text).strip() + "\n"
    body_digest = sha256(staged_body.encode("utf-8")).hexdigest()
    output = (
        "---\n"
        f"manifest_id: {manifest.manifest_id}\n"
        f"manifest_digest_sha256: {manifest.manifest_digest_sha256}\n"
        f"source_key: {entry.key}\n"
        f"source_digest_sha256: {entry.content_digest_sha256}\n"
        f"staged_digest_sha256: {body_digest}\n"
        "---\n\n"
        + staged_body
    ).encode("utf-8")
    relative = PurePosixPath("staged", entry.key, "input.md")
    try:
        scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(scratch_root, 0o700)
        atomic_write_new(root=scratch_root, relative=relative, data=output)
    except Exception:
        raise LedgerValidationError("ledger manifest stage persistence failed") from None
    return ManifestStageResult(
        StageDisposition.STAGED,
        entry.key,
        relative,
        sha256(output).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class LedgerStage:
    """One immutable prompt-facing projection of exactly one verified scan record."""

    binding: LedgerScanRecord
    upstream_redaction_receipt: RedactionReceipt
    redaction_receipt: RedactionReceipt
    staged_text: str
    projection_digest_sha256: str
    stage_digest_sha256: str

    @classmethod
    def create(cls, *, binding: LedgerScanRecord) -> LedgerStage:
        if not isinstance(binding, LedgerScanRecord):
            raise LedgerValidationError("invalid ledger stage binding")
        binding.validate()
        context = _prompt_context(binding)
        projection_digest = sha256(context).hexdigest()
        projection_receipt = RedactionReceipt.create(
            source_digest_sha256=binding.event_digest_sha256,
            output_digest_sha256=projection_digest,
            policy_version=_STAGE_POLICY_VERSION,
        )
        value = cls(
            binding=binding,
            upstream_redaction_receipt=binding.upstream_redaction_receipt,
            redaction_receipt=projection_receipt,
            staged_text=binding.redacted_text,
            projection_digest_sha256=projection_digest,
            stage_digest_sha256="",
        )
        return cls(
            binding=value.binding,
            upstream_redaction_receipt=value.upstream_redaction_receipt,
            redaction_receipt=value.redaction_receipt,
            staged_text=value.staged_text,
            projection_digest_sha256=value.projection_digest_sha256,
            stage_digest_sha256=value._expected_stage_digest(),
        )

    def prompt_context(self) -> bytes:
        self.validate()
        return _prompt_context(self.binding)

    def _digest_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "upstream_redaction_receipt": self.upstream_redaction_receipt.to_dict(),
            "redaction_receipt": self.redaction_receipt.to_dict(),
            "staged_text": self.staged_text,
            "projection_digest_sha256": self.projection_digest_sha256,
        }

    def _expected_stage_digest(self) -> str:
        return sha256(canonical_json_bytes(self._digest_dict())).hexdigest()

    def validate(self) -> None:
        self.binding.validate()
        context = _prompt_context(self.binding)
        if (
            self.staged_text != self.binding.redacted_text
            or self.upstream_redaction_receipt != self.binding.upstream_redaction_receipt
            or self.projection_digest_sha256 != sha256(context).hexdigest()
            or self.redaction_receipt.source_digest_sha256 != self.binding.event_digest_sha256
            or self.redaction_receipt.output_digest_sha256 != self.projection_digest_sha256
            or self.redaction_receipt.policy_version != _STAGE_POLICY_VERSION
            or self.stage_digest_sha256 != self._expected_stage_digest()
        ):
            raise LedgerValidationError("ledger stage binding mismatch")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {**self._digest_dict(), "stage_digest_sha256": self.stage_digest_sha256}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def stage_scan_record(*, record: LedgerScanRecord, taxonomy: LedgerTaxonomy) -> LedgerStage:
    """Create a transcript-free stage only when the injected taxonomy still matches."""
    if not isinstance(record, LedgerScanRecord) or not isinstance(taxonomy, LedgerTaxonomy):
        raise LedgerValidationError("invalid ledger stage input")
    record.validate()
    expected_route = taxonomy.route_for(record.source_locator)
    if record.taxonomy_version != taxonomy.version or record.route != expected_route:
        raise LedgerValidationError("ledger taxonomy binding mismatch")
    return LedgerStage.create(binding=record)


def _prompt_context(binding: LedgerScanRecord) -> bytes:
    """Canonical projection deliberately excludes transcript, locators, and raw references."""
    return canonical_json_bytes(
        {
            "text": binding.redacted_text,
            "capture_why": binding.capture_why,
            "captured_at": binding.captured_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "capture_source": binding.capture_source.value,
            "source_type": binding.source_type.value,
            "content_kind": binding.content_kind.value,
            "provenance": {
                "content_origin": binding.provenance.content_origin.value,
                "owner_context": binding.provenance.owner_context.value,
            },
            "topic_id": binding.topic_id,
            "topic_label": binding.topic_label,
        }
    )
