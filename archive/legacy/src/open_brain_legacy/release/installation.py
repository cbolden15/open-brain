"""Generic installation-plan inputs; this module never installs a service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.models import SchedulerPlatform
from open_brain_legacy.operations.render import render_manifest
from open_brain_legacy.operations.scheduler import RenderedManifest


class InstallationPlanError(ValueError):
    """A generic service-installation plan is not confined to its manifest."""


class InstallationPlatform(StrEnum):
    """The only scheduler platforms for which a plan may be constructed."""

    LAUNCHD = "launchd"
    SYSTEMD = "systemd"


_JOB_ID = re.compile(r"JOB-[0-9]{3}")
_DESTINATIONS = {
    InstallationPlatform.LAUNCHD: "<LAUNCHD_AGENT_DIRECTORY>",
    InstallationPlatform.SYSTEMD: "<SYSTEMD_UNIT_DIRECTORY>",
}


@dataclass(frozen=True, slots=True)
class InstallationPlan:
    """A generic manifest destination, with no filesystem or installer capability."""

    platform: InstallationPlatform
    job_id: str
    label: str
    manifest_name: str
    destination: str
    rendered_manifest: RenderedManifest


def generic_installation_plan(
    *, platform: InstallationPlatform, job_id: str
) -> InstallationPlan:
    """Create one validated generic plan for a catalog job and scheduler platform."""
    _validate_platform(platform)
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise InstallationPlanError("invalid-installation-job-id")
    job = get_job(job_id)
    scheduler_platform = SchedulerPlatform(platform.value)
    if scheduler_platform not in job.allowed_platforms:
        raise InstallationPlanError("installation-platform-not-allowed-for-job")
    label = _label(platform, job_id)
    return InstallationPlan(
        platform=platform,
        job_id=job_id,
        label=label,
        manifest_name=f"{label}.{_extension(platform)}",
        destination=_DESTINATIONS[platform],
        rendered_manifest=render_manifest(job, scheduler_platform),
    )


def validate_installation_plan(value: InstallationPlan) -> None:
    """Validate plan confinement without writing a manifest or invoking a scheduler."""
    if type(value) is not InstallationPlan:
        raise InstallationPlanError("invalid-installation-plan")
    _validate_platform(value.platform)
    expected = generic_installation_plan(platform=value.platform, job_id=value.job_id)
    if value != expected:
        raise InstallationPlanError("installation-plan-confinement-mismatch")


def _label(platform: InstallationPlatform, job_id: str) -> str:
    suffix = job_id.lower()
    if platform is InstallationPlatform.LAUNCHD:
        return f"org.open-brain.{suffix}"
    return f"open-brain-{suffix}"


def _extension(platform: InstallationPlatform) -> str:
    return "plist" if platform is InstallationPlatform.LAUNCHD else "service"


def _validate_platform(value: object) -> None:
    if type(value) is not InstallationPlatform:
        raise InstallationPlanError("invalid-installation-platform")
