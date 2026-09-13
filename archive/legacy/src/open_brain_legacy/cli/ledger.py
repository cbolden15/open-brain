"""Thin, deterministic public adapters for confined ledger scan and stage operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from open_brain_engine.core.models import PrivacyDecision

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
from open_brain_legacy.ledger.age import age_claims
from open_brain_legacy.ledger.embed import embed_claims
from open_brain_legacy.ledger.index import ClaimInput, ClaimRecord, index_claims
from open_brain_legacy.ledger.reconcile import ReconcileDisposition, ReconcileResult
from open_brain_legacy.ledger.reinforce import rank_claims, reinforce_claims
from open_brain_legacy.ledger.render import ClaimViewResult, RenderResult
from open_brain_legacy.ledger.requarantine import RequarantineDisposition, RequarantineResult
from open_brain_legacy.ledger.scan import LedgerSourceManifest, scan_source_root
from open_brain_legacy.ledger.service import ApplyResult, PreparedLedgerApply
from open_brain_legacy.ledger.slim import PreparedSlim, SlimError, SlimResult
from open_brain_legacy.ledger.stage import (
    LedgerStage,
    ManifestStageResult,
    StageDisposition,
    stage_manifest_entry,
)
from open_brain_legacy.ledger.synthesis import (
    PreparedSynthesis,
    SynthesisError,
    SynthesisOutcome,
    SynthesisRequest,
)


class LedgerApplier(Protocol):
    def apply(self, *, stage: LedgerStage, prepared: PreparedLedgerApply) -> ApplyResult: ...


class LedgerReconcileService(Protocol):
    def reconcile(self, *, prepared: PreparedLedgerApply) -> ReconcileResult: ...


class LedgerSlimmer(Protocol):
    def prepare(
        self,
        *,
        source_view: object,
        row_identity: object,
        now: datetime,
    ) -> SlimResult: ...


class LedgerRequarantineReplayer(Protocol):
    def replay(
        self, *, limit: int, dry_run: bool
    ) -> tuple[RequarantineResult, ...]: ...


class LedgerSynthesisService(Protocol):
    def apply(
        self, *, request: SynthesisRequest, privacy: PrivacyDecision
    ) -> SynthesisOutcome: ...


class LedgerClaimRenderer(Protocol):
    def render(
        self,
        *,
        claims: tuple[ClaimRecord, ...],
        privacy: PrivacyDecision,
    ) -> ClaimViewResult: ...


@dataclass(frozen=True, slots=True)
class LedgerCliResult:
    """A serializable public result that deliberately excludes filesystem contents."""

    exit_code: ExitCode
    envelope: dict[str, object]
    value: object | None = None

    def to_json(self) -> str:
        """Serialize a stable response for automation callers."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


def scan(*, root: Path) -> LedgerCliResult:
    """Scan a confined source root and return only stable manifest metadata."""
    try:
        manifest = scan_source_root(root=root)
    except Exception:
        return _failed("ledger.scan")
    return LedgerCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "command": "ledger.scan",
            "entry_count": len(manifest.entries),
            "manifest_digest_sha256": manifest.manifest_digest_sha256,
            "manifest_id": manifest.manifest_id,
            "status": "ok",
        },
        value=manifest,
    )


def stage(
    *,
    manifest: LedgerSourceManifest,
    key: str,
    source_root: Path,
    scratch_root: Path,
    dry_run: bool,
) -> LedgerCliResult:
    """Create one transcript-free stage or return a safe missing-key outcome."""
    if dry_run:
        return _dry_run("ledger.stage")
    try:
        result = stage_manifest_entry(
            manifest=manifest,
            key=key,
            source_root=source_root,
            scratch_root=scratch_root,
        )
    except Exception:
        return _failed("ledger.stage")
    if result.disposition is StageDisposition.MISSING:
        return LedgerCliResult(
            exit_code=ExitCode.FAILURE,
            envelope={"command": "ledger.stage", "status": "missing"},
            value=result,
        )
    return _staged(result)


def apply(
    *,
    service: LedgerApplier,
    stage: LedgerStage,
    prepared: PreparedLedgerApply,
    dry_run: bool,
) -> LedgerCliResult:
    """Apply prepared ledger output through the typed replay-safe service."""
    if dry_run:
        return _dry_run("ledger.apply")
    try:
        result = service.apply(stage=stage, prepared=prepared)
        if not isinstance(result, ApplyResult):
            raise ValueError("invalid apply result")
    except Exception:
        return _failed("ledger.apply")
    return LedgerCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={"command": "ledger.apply", "status": result.status},
        value=result,
    )


def reconcile(
    *,
    service: LedgerReconcileService,
    prepared: PreparedLedgerApply,
    dry_run: bool,
) -> LedgerCliResult:
    """Reconcile one prepared ledger apply through the typed recovery service."""
    if dry_run:
        return _dry_run("ledger.reconcile")
    try:
        result = service.reconcile(prepared=prepared)
        if not isinstance(result, ReconcileResult):
            raise ValueError("invalid reconcile result")
    except Exception:
        return _failed("ledger.reconcile")
    if result.disposition is ReconcileDisposition.CONFLICT:
        return LedgerCliResult(
            exit_code=ExitCode.FAILURE,
            envelope={
                "command": "ledger.reconcile",
                "disposition": result.disposition.value,
                "status": "conflict",
            },
            value=result,
        )
    return LedgerCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "command": "ledger.reconcile",
            "disposition": result.disposition.value,
            "status": "ok",
        },
        value=result,
    )


def slim(
    *,
    service: LedgerSlimmer,
    source_view: object,
    row_identity: object,
    now: datetime,
    dry_run: bool,
) -> LedgerCliResult:
    """Delegate archive-first slimming to the typed replay-safe service."""
    if dry_run:
        return _dry_run("ledger.slim")
    try:
        result = service.prepare(
            source_view=source_view,
            row_identity=row_identity,
            now=now,
        )
        if not isinstance(result, SlimResult):
            raise ValueError("invalid slim result")
    except Exception:
        return _failed("ledger.slim")
    if isinstance(result.error, SlimError):
        return LedgerCliResult(
            exit_code=ExitCode.FAILURE,
            envelope={
                "command": "ledger.slim",
                "reason": result.error.value,
                "status": "rejected",
            },
            value=result,
        )
    if not isinstance(result.prepared, PreparedSlim):
        return _failed("ledger.slim")
    return LedgerCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={"command": "ledger.slim", "status": "slimmed"},
        value=result,
    )


def requarantine(
    *,
    service: LedgerRequarantineReplayer,
    limit: int,
    dry_run: bool,
) -> LedgerCliResult:
    """Replay bounded quarantine entries through the typed durable service."""
    try:
        results = service.replay(limit=limit, dry_run=dry_run)
        if not isinstance(results, tuple) or any(
            not isinstance(result, RequarantineResult) for result in results
        ):
            raise ValueError("invalid requarantine result")
    except Exception:
        return _failed("ledger.requarantine")
    return LedgerCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "command": "ledger.requarantine",
            "dry_run": dry_run,
            "held_count": sum(
                result.disposition is RequarantineDisposition.HELD for result in results
            ),
            "restored_count": sum(
                result.disposition is RequarantineDisposition.RESTORED for result in results
            ),
            "status": "dry_run" if dry_run else "replayed",
        },
        value=results,
    )


def synthesis(
    *,
    service: LedgerSynthesisService,
    store: object,
    renderer: object,
    request: SynthesisRequest,
    privacy: PrivacyDecision,
    dry_run: bool,
) -> LedgerCliResult:
    """Persist and render one citation-bound synthesis through typed services."""
    if dry_run:
        return _dry_run("ledger.synthesis")
    try:
        outcome = service.apply(request=request, privacy=privacy)
        if not isinstance(outcome, SynthesisOutcome):
            raise ValueError("invalid synthesis outcome")
        if isinstance(outcome.error, SynthesisError):
            return LedgerCliResult(
                exit_code=ExitCode.FAILURE,
                envelope={
                    "attempts": outcome.attempts,
                    "command": "ledger.synthesis",
                    "reason": outcome.error.value,
                    "status": "rejected",
                },
                value=outcome,
            )
        if not isinstance(outcome.prepared, PreparedSynthesis):
            raise ValueError("invalid prepared synthesis")
        get = getattr(store, "get", None)
        render = getattr(renderer, "render", None)
        if not callable(get) or not callable(render):
            raise ValueError("invalid synthesis services")
        record = get(outcome.prepared.request.request_id)
        if record is None:
            raise ValueError("synthesis record unavailable")
        rendered = render(record)
        if not isinstance(rendered, RenderResult):
            raise ValueError("invalid synthesis render")
    except Exception:
        return _failed("ledger.synthesis")
    return LedgerCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "attempts": outcome.attempts,
            "command": "ledger.synthesis",
            "request_id": outcome.prepared.request.request_id,
            "status": "synthesized",
        },
        value=(outcome, rendered),
    )


def claim_lifecycle(
    *,
    inputs: tuple[ClaimInput, ...],
    renderer: LedgerClaimRenderer,
    privacy: PrivacyDecision,
    query: str,
    now: datetime,
    aging_after: timedelta,
    retire_after: timedelta,
    dimensions: int,
    similarity_threshold: float,
    limit: int,
    dry_run: bool,
) -> LedgerCliResult:
    """Compose typed claim operations and persist only immutable derived views."""
    try:
        indexed = index_claims(inputs)
        embedded = embed_claims(indexed, dimensions=dimensions)
        reinforced = reinforce_claims(
            embedded,
            reinforced_at=now,
            similarity_threshold=similarity_threshold,
        )
        aged = age_claims(
            reinforced,
            now=now,
            aging_after=aging_after,
            retire_after=retire_after,
        )
        ranked = rank_claims(query=query, claims=aged, limit=limit)
        views = None if dry_run else renderer.render(claims=aged, privacy=privacy)
        if views is not None and not isinstance(views, ClaimViewResult):
            raise ValueError("invalid claim views")
    except Exception:
        return _failed("ledger.lifecycle")
    return LedgerCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "claim_count": len(aged),
            "command": "ledger.lifecycle",
            "dry_run": dry_run,
            "ranked_claim_ids": [claim.claim_id for claim in ranked],
            "status": "dry_run" if dry_run else "rendered",
        },
        value=views,
    )


def _staged(result: ManifestStageResult) -> LedgerCliResult:
    if (
        result.disposition is not StageDisposition.STAGED
        or result.staged_digest_sha256 is None
        or result.relative_path is None
    ):
        return _failed("ledger.stage")
    return LedgerCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "command": "ledger.stage",
            "staged_digest_sha256": result.staged_digest_sha256,
            "status": "staged",
        },
        value=result,
    )


def _dry_run(command: str) -> LedgerCliResult:
    return LedgerCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={"command": command, "dry_run": True, "status": "dry_run"},
    )


def _failed(command: str) -> LedgerCliResult:
    return LedgerCliResult(
        exit_code=ExitCode.FAILURE,
        envelope={
            "command": command,
            "error": redacted_error("ledger_operation_failed"),
            "status": "failed",
        },
    )
