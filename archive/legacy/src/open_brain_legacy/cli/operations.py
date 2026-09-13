"""Dependency-injected, metadata-only adapters for operator services."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from open_brain_engine.core.models import PrivacyTier

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
from open_brain_legacy.operations.runlog import RunMetadata
from open_brain_legacy.production.retention import RetentionReport


@dataclass(frozen=True, slots=True)
class OperationsCliResult:
    """A deterministic public result without service or filesystem details."""

    exit_code: ExitCode
    envelope: dict[str, object]

    def to_json(self) -> str:
        """Serialize the metadata-only envelope for automation callers."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


class RetentionService(Protocol):
    """Effect boundary supplied by CLI composition."""

    def retain(self, *, dry_run: bool) -> RetentionReport: ...


def run_retention(
    *, service: RetentionService, dry_run: bool = True
) -> OperationsCliResult:
    """Preview retention by default and expose only its safe candidate manifest."""
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("invalid dry-run flag")
        report = service.retain(dry_run=dry_run)
        if not isinstance(report, RetentionReport) or (dry_run and report.removed_count):
            raise ValueError("invalid retention response")
    except Exception:
        return _failed("retention", "retention_operation_failed")
    return OperationsCliResult(
        ExitCode.SUCCESS,
        {
            "candidate_count": report.candidate_count,
            "command": "retention",
            "dry_run": dry_run,
            "manifest_digest": report.manifest_digest,
            "protected_count": report.protected_count,
            "removed_count": report.removed_count,
            "replayed": report.replayed,
            "status": "planned" if dry_run else "applied",
        },
    )


def _failed(command: str, code: str) -> OperationsCliResult:
    return OperationsCliResult(
        ExitCode.FAILURE,
        {"command": command, "error": redacted_error(code), "status": "failed"},
    )


class CronReader(Protocol):
    """Read bounded run metadata from an injected run-log service."""

    def reports(self, *, window_seconds: int) -> tuple[RunMetadata, ...]: ...


def show_cron(
    *,
    reader: CronReader,
    action: str = "report",
    window_seconds: int = 86_400,
) -> OperationsCliResult:
    """Return redacted reports for a bounded recent run window."""
    if not isinstance(action, str) or action != "report":
        return _failed("cron", "cron_unknown_action")
    if (
        not isinstance(window_seconds, int)
        or isinstance(window_seconds, bool)
        or not 1 <= window_seconds <= 604_800
    ):
        return _failed("cron", "cron_invalid_window")
    try:
        reports = reader.reports(window_seconds=window_seconds)
        if not isinstance(reports, tuple) or any(
            not isinstance(report, RunMetadata) for report in reports
        ):
            raise ValueError("invalid cron reports")
    except Exception:
        return _failed("cron", "cron_operation_failed")
    ordered = sorted(reports, key=lambda report: (report.finished_at, report.job_id))
    return OperationsCliResult(
        ExitCode.SUCCESS,
        {
            "command": "cron",
            "run_count": len(ordered),
            "runs": [report.to_dict() for report in ordered],
            "status": "reported",
            "window_seconds": window_seconds,
        },
    )


class DigestOutputMode(StrEnum):
    """Output modes the digest service may render without returning content here."""

    JSON = "json"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class DigestReport:
    """Allow-listed digest metadata returned by the typed digest service."""

    event_count: int
    output_mode: DigestOutputMode
    redacted_count: int
    replayed: bool
    tier: PrivacyTier

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (self.event_count, self.redacted_count)
            )
            or self.redacted_count > self.event_count
            or not isinstance(self.output_mode, DigestOutputMode)
            or not isinstance(self.replayed, bool)
            or not isinstance(self.tier, PrivacyTier)
        ):
            raise ValueError("invalid digest report")


class DigestService(Protocol):
    """Render a digest through the runtime's explicitly injected service."""

    def render(self, *, tier: PrivacyTier, output_mode: DigestOutputMode) -> DigestReport: ...


def render_digest(
    *,
    service: DigestService,
    tier: PrivacyTier | str,
    output_mode: DigestOutputMode | str = DigestOutputMode.JSON,
) -> OperationsCliResult:
    """Request a tiered digest while retaining only redacted count metadata."""
    try:
        requested_tier = PrivacyTier(tier)
        requested_mode = DigestOutputMode(output_mode)
        report = service.render(tier=requested_tier, output_mode=requested_mode)
        if (
            not isinstance(report, DigestReport)
            or report.tier is not requested_tier
            or report.output_mode is not requested_mode
        ):
            raise ValueError("invalid digest response")
    except Exception:
        return _failed("digest", "digest_operation_failed")
    return OperationsCliResult(
        ExitCode.SUCCESS,
        {
            "command": "digest",
            "event_count": report.event_count,
            "output_mode": report.output_mode.value,
            "redacted_count": report.redacted_count,
            "replayed": report.replayed,
            "status": "rendered",
            "tier": report.tier.value,
        },
    )


class OkfAction(StrEnum):
    """The closed set of confined Open Knowledge Format operations."""

    CHECK = "check"
    EXPORT = "export"


@dataclass(frozen=True, slots=True)
class OkfReport:
    """Schema-only OKF outcome without root, path, or record content."""

    action: OkfAction
    record_count: int
    replayed: bool
    schema_version: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.action, OkfAction)
            or not isinstance(self.record_count, int)
            or isinstance(self.record_count, bool)
            or self.record_count < 0
            or not isinstance(self.replayed, bool)
            or not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version < 1
        ):
            raise ValueError("invalid OKF report")


class OkfService(Protocol):
    """Run a confined OKF service supplied by CLI composition."""

    def run(self, *, action: OkfAction) -> OkfReport: ...


def run_okf(*, service: OkfService, action: OkfAction | str) -> OperationsCliResult:
    """Delegate a closed OKF action and serialize only schema metadata."""
    try:
        requested_action = OkfAction(action)
    except (TypeError, ValueError):
        return _failed("okf", "okf_unknown_action")
    try:
        report = service.run(action=requested_action)
        if not isinstance(report, OkfReport) or report.action is not requested_action:
            raise ValueError("invalid OKF response")
    except Exception:
        return _failed("okf", "okf_operation_failed")
    return OperationsCliResult(
        ExitCode.SUCCESS,
        {
            "action": report.action.value,
            "command": "okf",
            "record_count": report.record_count,
            "replayed": report.replayed,
            "schema_version": report.schema_version,
            "status": "checked" if report.action is OkfAction.CHECK else "exported",
        },
    )
