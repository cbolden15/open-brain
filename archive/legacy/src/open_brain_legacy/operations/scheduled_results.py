"""Metadata-only scheduled dispatch result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import ExitClass


class ScheduledDispatchStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ScheduledDispatchResult:
    job_id: str
    exit_code: int
    status: ScheduledDispatchStatus

    def __post_init__(self) -> None:
        if (
            isinstance(self.exit_code, int)
            and isinstance(self.status, ScheduledDispatchStatus)
            and (self.status is ScheduledDispatchStatus.COMPLETED) != (self.exit_code == 0)
        ):
            raise ValueError("scheduled status and exit disagree")
        if (
            not isinstance(self.job_id, str)
            or not self.job_id.startswith("JOB-")
            or type(self.exit_code) is not int
            or self.exit_code
            not in {0, 1, int(ExitClass.LOCK_HELD), int(ExitClass.CONFIGURATION)}
            or not isinstance(self.status, ScheduledDispatchStatus)
        ):
            raise ValueError("invalid scheduled dispatch result")

    @classmethod
    def completed(cls, job_id: str) -> ScheduledDispatchResult:
        return cls(job_id, 0, ScheduledDispatchStatus.COMPLETED)

    @classmethod
    def failed(cls, job_id: str) -> ScheduledDispatchResult:
        return cls(job_id, 1, ScheduledDispatchStatus.FAILED)

    @classmethod
    def unavailable(cls, job_id: str) -> ScheduledDispatchResult:
        return cls(job_id, 1, ScheduledDispatchStatus.UNAVAILABLE)

    @classmethod
    def lock_held(cls, job_id: str) -> ScheduledDispatchResult:
        return cls(job_id, int(ExitClass.LOCK_HELD), ScheduledDispatchStatus.FAILED)

    @classmethod
    def configuration(cls, job_id: str) -> ScheduledDispatchResult:
        return cls(job_id, int(ExitClass.CONFIGURATION), ScheduledDispatchStatus.FAILED)
