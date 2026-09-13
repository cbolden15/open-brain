"""Provider-neutral, review-gated LifeOS planning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from open_brain_engine.core.models import Intent
from open_brain_engine.review.models import ApprovedIntentRecord

from open_brain_legacy._compat.open_brain.integrations.config import IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.ports import Capability

_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class LifePlanDisposition(StrEnum):
    """Structural planning outcomes."""

    DISABLED = "disabled"
    PLANNED = "planned"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class ReviewGatedActionCandidate:
    """An action reference that cannot be mistaken for an applied task."""

    candidate_id: str
    review_id: str

    def __post_init__(self) -> None:
        if not _is_opaque_id(self.candidate_id) or not _is_opaque_id(self.review_id):
            raise ValueError("invalid review-gated action candidate")

    @property
    def requires_review(self) -> bool:
        return True


class CalendarBlockWriteDisposition(StrEnum):
    """Structural outcomes for an explicitly authorized calendar write."""

    DISABLED = "disabled"
    BLOCKED = "blocked"
    WRITTEN = "written"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class CalendarBlockRequest:
    """An opaque calendar block bound to a reviewed action candidate."""

    block_key: str
    plan_key: str
    action_candidate: ReviewGatedActionCandidate

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.block_key)
            or not _is_opaque_id(self.plan_key)
            or not isinstance(self.action_candidate, ReviewGatedActionCandidate)
        ):
            raise ValueError("invalid calendar block request")


@dataclass(frozen=True, slots=True)
class CalendarBlockWriteResult:
    """Provider-neutral calendar result containing no event payload."""

    block_key: str
    disposition: CalendarBlockWriteDisposition

    def __post_init__(self) -> None:
        if not _is_opaque_id(self.block_key) or not isinstance(
            self.disposition, CalendarBlockWriteDisposition
        ):
            raise ValueError("invalid calendar block result")


@dataclass(frozen=True, slots=True)
class CalendarWriteApproval:
    """Exact plan/candidate binding backed by an owner-approved intent record."""

    plan_key: str
    candidate_id: str
    review_id: str
    approved_record: ApprovedIntentRecord

    @classmethod
    def from_record(
        cls,
        *,
        plan_key: str,
        record: ApprovedIntentRecord,
    ) -> CalendarWriteApproval:
        if not isinstance(record, ApprovedIntentRecord):
            raise ValueError("invalid calendar write approval")
        return cls(
            plan_key=plan_key,
            candidate_id=record.record_id,
            review_id=str(record.review_id),
            approved_record=record,
        )

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.plan_key)
            or not _is_opaque_id(self.candidate_id)
            or not _is_opaque_id(self.review_id)
            or not isinstance(self.approved_record, ApprovedIntentRecord)
            or self.approved_record.intent is not Intent.ACTION_CANDIDATE
            or self.candidate_id != self.approved_record.record_id
            or self.review_id != str(self.approved_record.review_id)
        ):
            raise ValueError("invalid calendar write approval")


class CalendarBlockPort(Protocol):
    """Separately injected calendar write boundary."""

    def write(self, request: CalendarBlockRequest) -> CalendarBlockWriteResult: ...


class InMemoryCalendarBlockWriter:
    """Synthetic idempotent calendar writer for contract tests."""

    def __init__(self) -> None:
        self._requests: dict[str, CalendarBlockRequest] = {}

    @property
    def requests(self) -> tuple[CalendarBlockRequest, ...]:
        return tuple(self._requests[key] for key in sorted(self._requests))

    def write(self, request: CalendarBlockRequest) -> CalendarBlockWriteResult:
        if not isinstance(request, CalendarBlockRequest):
            raise ValueError("invalid calendar block request")
        existing = self._requests.get(request.block_key)
        if existing is None:
            self._requests[request.block_key] = request
            disposition = CalendarBlockWriteDisposition.WRITTEN
        elif existing == request:
            disposition = CalendarBlockWriteDisposition.DUPLICATE
        else:
            raise ValueError("conflicting calendar block replay")
        return CalendarBlockWriteResult(request.block_key, disposition)


@dataclass(frozen=True, slots=True)
class LifePlanRequest:
    """A structural day plan containing review-bound action references only."""

    plan_date: date
    action_candidates: tuple[ReviewGatedActionCandidate, ...] = ()

    def __post_init__(self) -> None:
        candidate_ids = tuple(candidate.candidate_id for candidate in self.action_candidates)
        if (
            not isinstance(self.plan_date, date)
            or not isinstance(self.action_candidates, tuple)
            or any(
                not isinstance(candidate, ReviewGatedActionCandidate)
                for candidate in self.action_candidates
            )
            or len(set(candidate_ids)) != len(candidate_ids)
        ):
            raise ValueError("invalid LifeOS plan request")


@dataclass(frozen=True, slots=True)
class LifePlan:
    """Persisted structural plan state with no task or calendar payload."""

    plan_key: str
    plan_date: date
    action_candidates: tuple[ReviewGatedActionCandidate, ...]


@dataclass(frozen=True, slots=True)
class LifePlanResult:
    """A plan outcome; disabled planning has no persisted plan."""

    disposition: LifePlanDisposition
    plan: LifePlan | None


class LifeResetDisposition(StrEnum):
    """Structural reset outcomes."""

    DISABLED = "disabled"
    RESET = "reset"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class LifeResetRequest:
    """Reset one date-derived LifeOS plan."""

    plan_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.plan_date, date):
            raise ValueError("invalid LifeOS reset request")


@dataclass(frozen=True, slots=True)
class LifeResetResult:
    """A reset outcome with only its opaque plan key."""

    plan_key: str
    disposition: LifeResetDisposition


class LifePlanPort(Protocol):
    """Provider-neutral LifeOS planning boundary."""

    def plan(self, request: LifePlanRequest) -> LifePlanResult: ...

    def reset(self, request: LifeResetRequest) -> LifeResetResult: ...


class LifePlanStore(Protocol):
    """Atomic idempotency boundary for local plan state."""

    def put(self, plan: LifePlan) -> LifePlanDisposition: ...

    def get(self, plan_key: str) -> LifePlan | None: ...

    def reset(self, plan_key: str) -> LifeResetDisposition: ...


class InMemoryLifePlanStore:
    """Synthetic plan state used by focused integration tests."""

    def __init__(self) -> None:
        self._plans: dict[str, LifePlan] = {}

    @property
    def plans(self) -> tuple[LifePlan, ...]:
        return tuple(self._plans[key] for key in sorted(self._plans))

    def put(self, plan: LifePlan) -> LifePlanDisposition:
        existing = self._plans.get(plan.plan_key)
        if existing is None:
            self._plans[plan.plan_key] = plan
            return LifePlanDisposition.PLANNED
        if existing == plan:
            return LifePlanDisposition.DUPLICATE
        raise ValueError("conflicting LifeOS plan replay")

    def get(self, plan_key: str) -> LifePlan | None:
        return self._plans.get(plan_key)

    def reset(self, plan_key: str) -> LifeResetDisposition:
        if self._plans.pop(plan_key, None) is None:
            return LifeResetDisposition.DUPLICATE
        return LifeResetDisposition.RESET


class LifeOSIntegration:
    """Disabled-by-default planner that never invokes task or calendar writers."""

    def __init__(
        self,
        *,
        config: IntegrationConfig | None = None,
        store: LifePlanStore | None = None,
    ) -> None:
        if config is not None and not isinstance(config, IntegrationConfig):
            raise ValueError("invalid LifeOS integration configuration")
        self._config = config or IntegrationConfig()
        self._store = store if store is not None else InMemoryLifePlanStore()

    def plan(self, request: LifePlanRequest) -> LifePlanResult:
        if not isinstance(request, LifePlanRequest):
            raise ValueError("invalid LifeOS plan request")
        if not self._config.live_adapter_enabled(Capability.LIFE_OS):
            return LifePlanResult(LifePlanDisposition.DISABLED, None)

        plan = LifePlan(
            plan_key=_plan_key(request.plan_date),
            plan_date=request.plan_date,
            action_candidates=request.action_candidates,
        )
        return LifePlanResult(self._store.put(plan), plan)

    def reset(self, request: LifeResetRequest) -> LifeResetResult:
        if not isinstance(request, LifeResetRequest):
            raise ValueError("invalid LifeOS reset request")
        plan_key = _plan_key(request.plan_date)
        if not self._config.live_adapter_enabled(Capability.LIFE_OS):
            return LifeResetResult(plan_key, LifeResetDisposition.DISABLED)
        return LifeResetResult(plan_key, self._store.reset(plan_key))

    def write_calendar(
        self,
        request: CalendarBlockRequest,
        *,
        writer: CalendarBlockPort,
        approval: CalendarWriteApproval | None = None,
    ) -> CalendarBlockWriteResult:
        if not isinstance(request, CalendarBlockRequest):
            raise ValueError("invalid calendar block request")
        if not self._config.external_writes_enabled(Capability.LIFE_OS):
            return CalendarBlockWriteResult(
                request.block_key,
                CalendarBlockWriteDisposition.DISABLED,
            )
        plan = self._store.get(request.plan_key)
        if (
            plan is None
            or request.action_candidate not in plan.action_candidates
            or not isinstance(approval, CalendarWriteApproval)
            or approval.plan_key != request.plan_key
            or approval.candidate_id != request.action_candidate.candidate_id
            or approval.review_id != request.action_candidate.review_id
        ):
            return CalendarBlockWriteResult(
                request.block_key,
                CalendarBlockWriteDisposition.BLOCKED,
            )
        result = writer.write(request)
        if (
            not isinstance(result, CalendarBlockWriteResult)
            or result.block_key != request.block_key
        ):
            raise ValueError("invalid calendar block result")
        return result


def _plan_key(plan_date: date) -> str:
    digest = sha256(plan_date.isoformat().encode("ascii")).hexdigest()
    return "life_plan_" + digest


def _is_opaque_id(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None
