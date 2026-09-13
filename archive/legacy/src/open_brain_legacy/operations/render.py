from __future__ import annotations

import plistlib
import re

from .models import JobSpec, JobState, RetryPolicy, TriggerKind
from .scheduler import (
    DEFAULT_MANIFEST_REFERENCES,
    ManifestReferences,
    RenderedManifest,
    SchedulerPlatform,
    requires_systemd_timer,
)


class ManifestValidationError(ValueError):
    """A rendered manifest differs from its immutable public job contract."""


_SYSTEMD_UNQUOTED_ARGUMENT = re.compile(r"[A-Za-z0-9_./:=,+@-]+")


def render_launchd(
    job: JobSpec,
    references: ManifestReferences = DEFAULT_MANIFEST_REFERENCES,
) -> str:
    _ensure_platform_allowed(job, SchedulerPlatform.LAUNCHD)
    payload: dict[str, object] = {
        "Disabled": job.state is not JobState.ENABLED,
        "EnvironmentVariables": _environment(job),
        "Label": _label(job, references),
        "ProcessType": "Background",
        "ProgramArguments": list(job.command),
        "RunAtLoad": job.trigger.run_at_load,
        "StandardErrorPath": _log_path(job, references, "stderr"),
        "StandardOutPath": _log_path(job, references, "stdout"),
        "Umask": 0o077,
        "WorkingDirectory": references.working_directory,
    }
    if job.retry is RetryPolicy.ON_FAILURE:
        payload["ThrottleInterval"] = 30
    if job.trigger.kind is TriggerKind.INTERVAL:
        if job.trigger.interval_seconds is None:
            raise ValueError("interval trigger is incomplete")
        payload["StartInterval"] = job.trigger.interval_seconds
    elif job.trigger.kind is TriggerKind.CALENDAR:
        if job.trigger.hour is None or job.trigger.minute is None:
            raise ValueError("calendar trigger is incomplete")
        calendar: dict[str, int] = {
            "Hour": job.trigger.hour,
            "Minute": job.trigger.minute,
        }
        if job.trigger.weekday is not None:
            calendar["Weekday"] = job.trigger.weekday
        payload["StartCalendarInterval"] = calendar
    else:
        payload["KeepAlive"] = True
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")


def render_systemd_service(
    job: JobSpec,
    references: ManifestReferences = DEFAULT_MANIFEST_REFERENCES,
) -> str:
    _ensure_platform_allowed(job, SchedulerPlatform.SYSTEMD)
    unit = [
        "[Unit]",
        f"Description=Open Brain {job.id}",
        f"X-OpenBrain-State={job.state.value}",
    ]
    if job.trigger.kind is TriggerKind.KEEPALIVE and job.host_role.value == "ingress":
        unit.extend(("Wants=network-online.target", "After=network-online.target"))

    service = [
        "",
        "[Service]",
        "Type=simple" if job.trigger.kind is TriggerKind.KEEPALIVE else "Type=oneshot",
        f"ExecStart={_systemd_command(job.command)}",
        f"WorkingDirectory={references.working_directory}",
        "UMask=0077",
        f"TimeoutStartSec={job.timeout_seconds}",
    ]
    service.extend(f"Environment={name}={value}" for name, value in _environment(job).items())
    service.extend(
        (
            f"StandardOutput=append:{_log_path(job, references, 'stdout')}",
            f"StandardError=append:{_log_path(job, references, 'stderr')}",
            "Restart=on-failure" if job.retry is RetryPolicy.ON_FAILURE else "Restart=no",
        )
    )
    return "\n".join((*unit, *service, ""))


def render_systemd_timer(
    job: JobSpec,
    references: ManifestReferences = DEFAULT_MANIFEST_REFERENCES,
) -> str:
    _ensure_platform_allowed(job, SchedulerPlatform.SYSTEMD)
    if not requires_systemd_timer(job):
        raise ValueError("systemd timer requires an interval, calendar, or timer trigger")
    label = _label(job, references)
    lines = [
        "[Unit]",
        f"Description=Open Brain timer {job.id}",
        f"X-OpenBrain-State={job.state.value}",
        "",
        "[Timer]",
    ]
    if job.trigger.kind is TriggerKind.CALENDAR:
        if job.trigger.hour is None or job.trigger.minute is None:
            raise ValueError("calendar trigger is incomplete")
        calendar = f"*-*-* {job.trigger.hour:02d}:{job.trigger.minute:02d}:00"
        if job.trigger.weekday is not None:
            calendar = f"{_systemd_weekday(job.trigger.weekday)} {calendar}"
        lines.append(f"OnCalendar={calendar}")
    elif job.trigger.kind is TriggerKind.CALENDAR_INTERVAL:
        if job.trigger.interval_seconds is None:
            raise ValueError("calendar interval trigger is incomplete")
        lines.append(f"OnCalendar={_systemd_calendar_interval(job.trigger.interval_seconds)}")
    else:
        if job.trigger.interval_seconds is None:
            raise ValueError("timer trigger is incomplete")
        boot_seconds = 0 if job.trigger.run_at_load else job.trigger.interval_seconds
        lines.extend(
            (
                f"OnBootSec={boot_seconds}",
                f"OnUnitActiveSec={job.trigger.interval_seconds}s",
            )
        )
    lines.extend(
        (
            f"Persistent={'true' if job.trigger.persistent else 'false'}",
            f"Unit={label}.service",
            "",
        )
    )
    return "\n".join(lines)


def render_manifest(
    job: JobSpec,
    platform: SchedulerPlatform | str,
    references: ManifestReferences = DEFAULT_MANIFEST_REFERENCES,
) -> RenderedManifest:
    try:
        scheduler_platform = SchedulerPlatform(platform)
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported scheduler platform") from error
    _ensure_platform_allowed(job, scheduler_platform)
    if scheduler_platform is SchedulerPlatform.LAUNCHD:
        return RenderedManifest(
            platform=scheduler_platform,
            job_id=job.id,
            service=render_launchd(job, references),
            timer=None,
        )
    timer = render_systemd_timer(job, references) if requires_systemd_timer(job) else None
    return RenderedManifest(
        platform=scheduler_platform,
        job_id=job.id,
        service=render_systemd_service(job, references),
        timer=timer,
    )


def validate_rendered_manifest(
    job: JobSpec,
    rendered: RenderedManifest,
    references: ManifestReferences = DEFAULT_MANIFEST_REFERENCES,
) -> None:
    if not isinstance(rendered, RenderedManifest):
        raise ManifestValidationError("rendered manifest does not match the job contract")
    expected = render_manifest(job, rendered.platform, references)
    if rendered != expected:
        raise ManifestValidationError("rendered manifest does not match the job contract")


def _environment(job: JobSpec) -> dict[str, str]:
    environment = {
        "OPEN_BRAIN_DEPLOYMENT_TARGET": job.deployment_target.value,
        "OPEN_BRAIN_ENV_REFS": ",".join(job.env_refs),
        "OPEN_BRAIN_HOST_ROLE": job.host_role.value,
        "OPEN_BRAIN_JOB_ID": job.id,
        "OPEN_BRAIN_JOB_STATE": job.state.value,
        "OPEN_BRAIN_LOCK_SCOPE": job.lock_scope.value,
        "OPEN_BRAIN_OUTPUT_POLICY": job.output_policy.value,
        "OPEN_BRAIN_RETRY_POLICY": job.retry.value,
        "OPEN_BRAIN_RUN_AT_LOAD": str(job.trigger.run_at_load).lower(),
        "OPEN_BRAIN_SCHEDULER_PLATFORMS": ",".join(
            sorted(platform.value for platform in job.allowed_platforms)
        ),
        "OPEN_BRAIN_TIMEOUT_SECONDS": str(job.timeout_seconds),
        "OPEN_BRAIN_TRIGGER_KIND": job.trigger.kind.value,
        "OPEN_BRAIN_TRIGGER_PERSISTENT": str(job.trigger.persistent).lower(),
        "OPEN_BRAIN_WRITER_SCOPE": job.writer_scope.value,
    }
    environment.update({reference: f"<{reference}>" for reference in job.env_refs})
    return environment


def _label(job: JobSpec, references: ManifestReferences) -> str:
    return f"{references.label_prefix}.{job.id.lower()}"


def _log_path(job: JobSpec, references: ManifestReferences, stream: str) -> str:
    return f"{references.log_directory}/{job.id}.{stream}.log"


def _systemd_command(command: tuple[str, ...]) -> str:
    return " ".join(_systemd_argument(argument) for argument in command)


def _systemd_argument(argument: str) -> str:
    if _SYSTEMD_UNQUOTED_ARGUMENT.fullmatch(argument) is not None:
        return argument
    escaped = (
        argument.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _systemd_weekday(weekday: int) -> str:
    return ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[weekday]


def _systemd_calendar_interval(interval_seconds: int) -> str:
    if interval_seconds % 60 != 0:
        raise ValueError("calendar interval must use whole minutes")
    minutes = interval_seconds // 60
    if not 1 <= minutes <= 60 or 60 % minutes != 0:
        raise ValueError("calendar interval must divide one hour")
    minute_field = "00" if minutes == 60 else f"0/{minutes}"
    return f"*-*-* *:{minute_field}:00"


def _ensure_platform_allowed(job: JobSpec, platform: SchedulerPlatform) -> None:
    if platform not in job.allowed_platforms:
        raise ManifestValidationError(
            f"scheduler platform {platform.value} is not allowed for {job.id}"
        )
