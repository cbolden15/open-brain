from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .models import (
    DeploymentTarget,
    JobSpec,
    OperationsValidationError,
    TriggerKind,
)
from .models import SchedulerPlatform as SchedulerPlatform


class JobCatalogValidationError(OperationsValidationError):
    """The public catalog does not contain the closed scheduled-job allocation."""


_PLACEHOLDER_PATTERN = re.compile(r"<[A-Z][A-Z0-9_]*>")
_LABEL_PREFIX_PATTERN = re.compile(r"[a-z][a-z0-9.-]{0,63}")
EXPECTED_JOB_IDS = tuple(f"JOB-{number:03d}" for number in range(1, 31))
EXPECTED_JOB_DEPLOYMENTS: Mapping[
    str, tuple[DeploymentTarget, frozenset[SchedulerPlatform]]
] = MappingProxyType(
    {
        **{
            f"JOB-{number:03d}": (
                DeploymentTarget.EDGE_OPERATOR,
                frozenset({SchedulerPlatform.LAUNCHD}),
            )
            for number in range(1, 10)
        },
        **{
            f"JOB-{number:03d}": (
                DeploymentTarget.CANONICAL_WRITER,
                frozenset({SchedulerPlatform.LAUNCHD}),
            )
            for number in range(10, 28)
        },
        **{
            f"JOB-{number:03d}": (
                DeploymentTarget.INGRESS_NODE,
                frozenset({SchedulerPlatform.SYSTEMD}),
            )
            for number in range(28, 31)
        },
    }
)


@dataclass(frozen=True, slots=True)
class ManifestReferences:
    label_prefix: str = "org.open-brain"
    working_directory: str = "<WORKING_DIRECTORY>"
    log_directory: str = "<LOG_DIRECTORY>"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.label_prefix, str)
            or _LABEL_PREFIX_PATTERN.fullmatch(self.label_prefix) is None
        ):
            raise OperationsValidationError("invalid manifest label prefix")
        for reference in (self.working_directory, self.log_directory):
            if not isinstance(reference, str) or _PLACEHOLDER_PATTERN.fullmatch(reference) is None:
                raise OperationsValidationError("manifest paths must be generic placeholders")


@dataclass(frozen=True, slots=True)
class RenderedManifest:
    platform: SchedulerPlatform
    job_id: str
    service: str
    timer: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.platform, SchedulerPlatform):
            raise OperationsValidationError("invalid scheduler platform")
        if self.job_id not in EXPECTED_JOB_IDS:
            raise OperationsValidationError("invalid rendered job id")
        if not isinstance(self.service, str) or not self.service:
            raise OperationsValidationError("invalid rendered service manifest")
        if self.timer is not None and (not isinstance(self.timer, str) or not self.timer):
            raise OperationsValidationError("invalid rendered timer manifest")


DEFAULT_MANIFEST_REFERENCES = ManifestReferences()


def validate_job_catalog(jobs: tuple[JobSpec, ...]) -> tuple[JobSpec, ...]:
    if not isinstance(jobs, tuple) or any(not isinstance(job, JobSpec) for job in jobs):
        raise JobCatalogValidationError("job catalog must be an immutable JobSpec tuple")
    ids = tuple(job.id for job in jobs)
    if ids != EXPECTED_JOB_IDS or len(set(ids)) != len(EXPECTED_JOB_IDS):
        raise JobCatalogValidationError(
            "job catalog must contain JOB-001 through JOB-030 exactly once"
        )
    deployments = tuple(
        (job.deployment_target, job.allowed_platforms) for job in jobs
    )
    expected_deployments = tuple(EXPECTED_JOB_DEPLOYMENTS[job_id] for job_id in ids)
    if deployments != expected_deployments:
        raise JobCatalogValidationError(
            "job catalog does not match the fixed deployment mapping"
        )
    writer_targets = {
        job.deployment_target for job in jobs if job.host_role.value == "writer"
    }
    if writer_targets != {DeploymentTarget.CANONICAL_WRITER}:
        raise JobCatalogValidationError(
            "job catalog must have exactly one canonical writer target"
        )
    return jobs


def requires_systemd_timer(job: JobSpec) -> bool:
    return job.trigger.kind is not TriggerKind.KEEPALIVE
