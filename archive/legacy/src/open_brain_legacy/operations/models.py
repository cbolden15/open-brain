from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from open_brain_engine.engine import LockScope


class OperationsValidationError(ValueError):
    """An operations value violates the public scheduler contract."""


class ExitClass(IntEnum):
    SUCCESS = 0
    LOCK_HELD = 75
    CONFIGURATION = 78


class HostRole(StrEnum):
    INGRESS = "ingress"
    PROBE = "probe"
    WRITER = "writer"
    SERVICE = "service"


class DeploymentTarget(StrEnum):
    EDGE_OPERATOR = "edge-operator"
    CANONICAL_WRITER = "canonical-writer"
    INGRESS_NODE = "ingress-node"


class SchedulerPlatform(StrEnum):
    LAUNCHD = "launchd"
    SYSTEMD = "systemd"


class WriterScope(StrEnum):
    NONE = "none"
    CAPTURE_INGRESS = "capture-ingress"
    INDEX = "index"
    CONTENT = "content"
    STATE = "state"
    BACKUP = "backup"


class TriggerKind(StrEnum):
    INTERVAL = "interval"
    CALENDAR = "calendar"
    CALENDAR_INTERVAL = "calendar-interval"
    KEEPALIVE = "keepalive"


class RetryPolicy(StrEnum):
    NEVER = "never"
    ON_FAILURE = "on-failure"


class OutputPolicy(StrEnum):
    METADATA_ONLY = "metadata-only"
    REDACTED_REPORT = "redacted-report"


class JobState(StrEnum):
    ENABLED = "enabled"
    MANUAL = "manual"
    DISABLED = "disabled"


_JOB_ID_PATTERN = re.compile(r"JOB-[0-9]{3}")
_ENV_REF_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    kind: TriggerKind
    interval_seconds: int | None = None
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None
    run_at_load: bool = False
    persistent: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TriggerKind):
            raise OperationsValidationError("invalid trigger kind")
        if not isinstance(self.run_at_load, bool) or not isinstance(self.persistent, bool):
            raise OperationsValidationError("invalid trigger flags")

        if self.kind in {TriggerKind.INTERVAL, TriggerKind.CALENDAR_INTERVAL}:
            if (
                not isinstance(self.interval_seconds, int)
                or isinstance(self.interval_seconds, bool)
                or not 1 <= self.interval_seconds <= 86_400
                or any(value is not None for value in (self.hour, self.minute, self.weekday))
            ):
                raise OperationsValidationError("invalid interval trigger")
            if self.kind is TriggerKind.CALENDAR_INTERVAL and (
                self.interval_seconds % 60 != 0
                or not 1 <= self.interval_seconds // 60 <= 60
                or 60 % (self.interval_seconds // 60) != 0
                or self.run_at_load
            ):
                raise OperationsValidationError("invalid calendar interval trigger")
        elif self.kind is TriggerKind.CALENDAR:
            if (
                self.interval_seconds is not None
                or not isinstance(self.hour, int)
                or isinstance(self.hour, bool)
                or not 0 <= self.hour <= 23
                or not isinstance(self.minute, int)
                or isinstance(self.minute, bool)
                or not 0 <= self.minute <= 59
                or (
                    self.weekday is not None
                    and (
                        not isinstance(self.weekday, int)
                        or isinstance(self.weekday, bool)
                        or not 0 <= self.weekday <= 7
                    )
                )
            ):
                raise OperationsValidationError("invalid calendar trigger")
        elif any(
            value is not None
            for value in (self.interval_seconds, self.hour, self.minute, self.weekday)
        ):
            raise OperationsValidationError("invalid keepalive trigger")

        if self.persistent and self.kind not in {
            TriggerKind.CALENDAR,
            TriggerKind.CALENDAR_INTERVAL,
        }:
            raise OperationsValidationError("monotonic interval cannot be persistent")
        if self.kind is TriggerKind.CALENDAR_INTERVAL and not self.persistent:
            raise OperationsValidationError("calendar interval requires missed-run persistence")


@dataclass(frozen=True, slots=True)
class JobSpec:
    id: str
    command: tuple[str, ...]
    deployment_target: DeploymentTarget
    allowed_platforms: frozenset[SchedulerPlatform]
    host_role: HostRole
    trigger: TriggerSpec
    writer_scope: WriterScope
    lock_scope: LockScope
    timeout_seconds: int
    retry: RetryPolicy
    env_refs: tuple[str, ...]
    output_policy: OutputPolicy
    state: JobState

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or _JOB_ID_PATTERN.fullmatch(self.id) is None:
            raise OperationsValidationError("invalid job id")
        if (
            not isinstance(self.command, tuple)
            or len(self.command) < 2
            or self.command[0] != "open-brain"
            or any(
                not isinstance(argument, str)
                or not argument
                or any(marker in argument for marker in ("\x00", "\r", "\n"))
                for argument in self.command
            )
        ):
            raise OperationsValidationError("command must be a direct open-brain argv tuple")
        if not isinstance(self.deployment_target, DeploymentTarget):
            raise OperationsValidationError("invalid deployment target")
        if (
            not isinstance(self.allowed_platforms, frozenset)
            or not self.allowed_platforms
            or any(
                not isinstance(platform, SchedulerPlatform)
                for platform in self.allowed_platforms
            )
        ):
            raise OperationsValidationError("invalid allowed scheduler platforms")
        if not isinstance(self.host_role, HostRole):
            raise OperationsValidationError("invalid host role")
        if not isinstance(self.trigger, TriggerSpec):
            raise OperationsValidationError("invalid trigger")
        if not isinstance(self.writer_scope, WriterScope):
            raise OperationsValidationError("invalid writer scope")
        if not isinstance(self.lock_scope, LockScope):
            raise OperationsValidationError("invalid lock scope")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 86_400
        ):
            raise OperationsValidationError("invalid timeout")
        if not isinstance(self.retry, RetryPolicy):
            raise OperationsValidationError("invalid retry policy")
        if (
            not isinstance(self.env_refs, tuple)
            or len(set(self.env_refs)) != len(self.env_refs)
            or any(
                not isinstance(reference, str) or _ENV_REF_PATTERN.fullmatch(reference) is None
                for reference in self.env_refs
            )
        ):
            raise OperationsValidationError("invalid environment references")
        if not isinstance(self.output_policy, OutputPolicy):
            raise OperationsValidationError("invalid output policy")
        if not isinstance(self.state, JobState):
            raise OperationsValidationError("invalid job state")
        if self.trigger.persistent and self.allowed_platforms != frozenset(
            {SchedulerPlatform.SYSTEMD}
        ):
            raise OperationsValidationError(
                "missed-run persistence requires the systemd scheduler platform"
            )
        self._validate_ownership()

    def _validate_ownership(self) -> None:
        if self.host_role in {HostRole.PROBE, HostRole.SERVICE}:
            if self.writer_scope is not WriterScope.NONE or self.lock_scope is not LockScope.NONE:
                raise OperationsValidationError("non-writer role cannot own canonical writes")
            return
        if self.host_role is HostRole.INGRESS:
            if (
                self.writer_scope is not WriterScope.CAPTURE_INGRESS
                or self.lock_scope is not LockScope.INGRESS
            ):
                raise OperationsValidationError("ingress role must remain append-only")
            return
        expected_locks = {
            WriterScope.INDEX: {LockScope.INDEX, LockScope.SHARED_WRITER},
            WriterScope.CONTENT: {LockScope.SHARED_WRITER},
            WriterScope.STATE: {LockScope.SHARED_WRITER},
            WriterScope.BACKUP: {LockScope.BACKUP_PROFILE},
        }
        if self.writer_scope not in expected_locks:
            raise OperationsValidationError("writer role requires a canonical writer scope")
        if self.lock_scope not in expected_locks[self.writer_scope]:
            raise OperationsValidationError("writer scope requires its approved lock")
