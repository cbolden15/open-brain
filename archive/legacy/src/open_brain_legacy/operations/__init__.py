"""Pure operations contracts and scheduler manifest rendering."""

from open_brain_engine.engine import LockScope

from .catalog import JOB_CATALOG, JOBS_BY_ID, get_job
from .models import (
    DeploymentTarget,
    ExitClass,
    HostRole,
    JobSpec,
    JobState,
    OperationsValidationError,
    OutputPolicy,
    RetryPolicy,
    SchedulerPlatform,
    TriggerKind,
    TriggerSpec,
    WriterScope,
)
from .render import (
    ManifestValidationError,
    render_launchd,
    render_manifest,
    render_systemd_service,
    render_systemd_timer,
    validate_rendered_manifest,
)
from .runlog import RunErrorClass, RunMetadata, RunOutcome, classify_exit_code
from .scheduler import (
    DEFAULT_MANIFEST_REFERENCES,
    EXPECTED_JOB_IDS,
    JobCatalogValidationError,
    ManifestReferences,
    RenderedManifest,
    validate_job_catalog,
)

__all__ = [
    "DEFAULT_MANIFEST_REFERENCES",
    "DeploymentTarget",
    "EXPECTED_JOB_IDS",
    "ExitClass",
    "HostRole",
    "JOBS_BY_ID",
    "JOB_CATALOG",
    "JobCatalogValidationError",
    "JobSpec",
    "JobState",
    "LockScope",
    "ManifestReferences",
    "ManifestValidationError",
    "OperationsValidationError",
    "OutputPolicy",
    "RenderedManifest",
    "RetryPolicy",
    "RunMetadata",
    "RunErrorClass",
    "RunOutcome",
    "SchedulerPlatform",
    "TriggerKind",
    "TriggerSpec",
    "WriterScope",
    "classify_exit_code",
    "get_job",
    "render_launchd",
    "render_manifest",
    "render_systemd_service",
    "render_systemd_timer",
    "validate_job_catalog",
    "validate_rendered_manifest",
]
