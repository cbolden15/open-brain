"""Scheduled-application specifications for configured integrations."""

from __future__ import annotations

from dataclasses import replace

from open_brain_legacy._compat.open_brain.integrations.ports import Capability, ProviderSyncRequest
from open_brain_legacy._compat.open_brain.integrations.ui import UiBindConfig
from open_brain_legacy.integrations.life_os import LifePlanRequest, LifeResetRequest

from .catalog import get_job
from .models import JobSpec


def compose_lifeos_midday_job(request: LifePlanRequest) -> JobSpec:
    """Compose JOB-017 from a validated LifeOS request without executing it."""
    if not isinstance(request, LifePlanRequest):
        raise ValueError("invalid LifeOS midday request")
    return _configured_job(
        "JOB-017",
        (
            "open-brain",
            "lifeos",
            "nudge",
            "midday",
            f"--date={request.plan_date.isoformat()}",
            "--json",
        ),
    )


def compose_lifeos_plan_job(request: LifePlanRequest) -> JobSpec:
    """Compose JOB-018 without serializing review candidate identifiers."""
    if not isinstance(request, LifePlanRequest):
        raise ValueError("invalid LifeOS plan request")
    return _configured_job(
        "JOB-018",
        (
            "open-brain",
            "lifeos",
            "plan",
            f"--date={request.plan_date.isoformat()}",
            "--generic-titles",
            "--json",
        ),
    )


def compose_lifeos_reset_job(request: LifeResetRequest) -> JobSpec:
    """Compose JOB-019 for one validated date-derived plan."""
    if not isinstance(request, LifeResetRequest):
        raise ValueError("invalid LifeOS reset request")
    return _configured_job(
        "JOB-019",
        (
            "open-brain",
            "lifeos",
            "reset",
            f"--date={request.plan_date.isoformat()}",
            "--json",
        ),
    )


def compose_message_extract_job(request: ProviderSyncRequest) -> JobSpec:
    """Compose JOB-020 with an immutable review-proposal gate."""
    command = ["open-brain", "messages", "extract"]
    _append_message_request(command, request)
    command.extend(("--json", "--review-actions"))
    return _configured_job("JOB-020", tuple(command))


def compose_message_sync_job(request: ProviderSyncRequest) -> JobSpec:
    """Compose JOB-021 from opaque cursor references without executing a provider."""
    command = ["open-brain", "messages", "sync"]
    _append_message_request(command, request)
    command.append("--json")
    return _configured_job("JOB-021", tuple(command))


def compose_ui_job(config: UiBindConfig) -> JobSpec:
    """Compose enabled JOB-026 from a validated private-default UI bind."""
    if not isinstance(config, UiBindConfig):
        raise ValueError("invalid UI bind configuration")
    return _configured_job(
        "JOB-026",
        (
            "open-brain",
            "ui",
            "serve",
            f"--bind={config.host}",
            f"--port={config.port}",
        ),
    )


def _append_message_request(command: list[str], request: ProviderSyncRequest) -> None:
    if (
        not isinstance(request, ProviderSyncRequest)
        or request.capability is not Capability.MESSAGING
    ):
        raise ValueError("invalid messaging job request")
    command.append(f"--resource-ref={request.resource_ref}")
    if request.cursor_ref is not None:
        command.append(f"--cursor-ref={request.cursor_ref}")
    if request.dry_run:
        command.append("--dry-run")


def _configured_job(job_id: str, command: tuple[str, ...]) -> JobSpec:
    return replace(get_job(job_id), command=command)
