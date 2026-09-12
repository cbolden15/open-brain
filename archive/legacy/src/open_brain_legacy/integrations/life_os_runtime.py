"""Root-confined persistent runtime capabilities for LifeOS planning jobs."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import overload

from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    StorageError,
    atomic_replace,
    read_confined,
)

from open_brain_legacy._compat.open_brain.integrations.config import IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.ports import Capability

from .life_os import (
    CalendarBlockPort,
    CalendarBlockRequest,
    CalendarBlockWriteDisposition,
    CalendarBlockWriteResult,
    CalendarWriteApproval,
    LifeOSIntegration,
    LifePlan,
    LifePlanDisposition,
    LifePlanPort,
    LifePlanRequest,
    LifePlanResult,
    LifePlanStore,
    LifeResetDisposition,
    LifeResetRequest,
    LifeResetResult,
    ReviewGatedActionCandidate,
)

_STATE_RELATIVE = PurePosixPath("runtime/life_os/planning-runtime.json")
_OPAQUE_STATE_PREFIX = "lifeos_runtime_"


class LifeOSRuntimeOperation(StrEnum):
    """Date-keyed persisted LifeOS runtime operations."""

    MIDDAY = "midday"
    PLAN = "plan"
    RESET = "reset"


class LifeOSRuntimeDisposition(StrEnum):
    """Crash-safe persistence outcomes for LifeOS runtime requests."""

    STAGED = "staged"
    DUPLICATE = "duplicate"


class LifeOSRuntimeStateError(StorageError):
    """Persisted LifeOS runtime state is malformed or semantically invalid."""


@dataclass(frozen=True, slots=True)
class LifeOSRuntimeInvocation:
    """One public-safe runtime invocation with no private candidate identifiers."""

    operation: LifeOSRuntimeOperation
    plan_date: date
    plan_key: str
    state_ref: str
    disposition: LifeOSRuntimeDisposition
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation, LifeOSRuntimeOperation)
            or not isinstance(self.plan_date, date)
            or not isinstance(self.plan_key, str)
            or not self.plan_key.startswith("life_plan_")
            or not isinstance(self.state_ref, str)
            or not self.state_ref.startswith(_OPAQUE_STATE_PREFIX)
            or not isinstance(self.disposition, LifeOSRuntimeDisposition)
            or not isinstance(self.command, tuple)
            or any(not isinstance(argument, str) for argument in self.command)
        ):
            raise ValueError("invalid LifeOS runtime invocation")


@dataclass(frozen=True, slots=True)
class _RuntimeState:
    midday: dict[str, LifePlanRequest]
    plan: dict[str, LifePlanRequest]
    reset: dict[str, LifeResetRequest]


@dataclass(slots=True)
class PersistentLifePlanStore:
    """Root-confined durable LifeOS plan store."""

    runtime: LifeOSPlanningRuntime

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, LifeOSPlanningRuntime):
            raise ValueError("invalid LifeOS runtime store")

    def put(self, plan: LifePlan) -> LifePlanDisposition:
        if not isinstance(plan, LifePlan) or plan.plan_key != _plan_key(plan.plan_date):
            raise ValueError("invalid LifeOS plan")
        request = LifePlanRequest(
            plan_date=plan.plan_date,
            action_candidates=plan.action_candidates,
        )
        state, payload_exists = self.runtime._read_state()
        key = plan.plan_date.isoformat()
        current = state.plan.get(key)
        if current is not None:
            if current == request:
                return LifePlanDisposition.DUPLICATE
            raise ValueError("conflicting LifeOS plan replay")
        state.plan[key] = request
        self.runtime._write_state(state, payload_exists=payload_exists)
        return LifePlanDisposition.PLANNED

    def get(self, plan_key: str) -> LifePlan | None:
        if not isinstance(plan_key, str) or not plan_key.startswith("life_plan_"):
            raise ValueError("invalid LifeOS plan key")
        state, _ = self.runtime._read_state()
        for plan_date_key in sorted(state.plan):
            request = state.plan[plan_date_key]
            if _plan_key(request.plan_date) == plan_key:
                return LifePlan(
                    plan_key=plan_key,
                    plan_date=request.plan_date,
                    action_candidates=request.action_candidates,
                )
        return None

    def reset(self, plan_key: str) -> LifeResetDisposition:
        if not isinstance(plan_key, str) or not plan_key.startswith("life_plan_"):
            raise ValueError("invalid LifeOS plan key")
        state, payload_exists = self.runtime._read_state()
        existing_key: str | None = None
        for plan_date_key in sorted(state.plan):
            request = state.plan[plan_date_key]
            if _plan_key(request.plan_date) == plan_key:
                existing_key = plan_date_key
                break
        if existing_key is None:
            return LifeResetDisposition.DUPLICATE
        del state.plan[existing_key]
        self.runtime._write_state(state, payload_exists=payload_exists)
        return LifeResetDisposition.RESET


class LifeOSPlanningRuntime:
    """One root-bound, local-only runtime state store for LifeOS planning jobs."""

    def __init__(
        self,
        *,
        root: Path,
        planner: LifePlanPort | None = None,
        calendar_writer: CalendarBlockPort | None = None,
    ) -> None:
        self._root = _validated_root(root)
        self._store = PersistentLifePlanStore(self)
        self._planner = (
            planner
            if planner is not None
            else LifeOSIntegration(
                config=IntegrationConfig(live_adapters=frozenset({Capability.LIFE_OS})),
                store=self._store,
            )
        )
        self._calendar_writer = calendar_writer

    @classmethod
    def bind(
        cls,
        *,
        root: Path,
        planner: LifePlanPort | None = None,
        calendar_writer: CalendarBlockPort | None = None,
    ) -> LifeOSPlanningRuntime:
        return cls(root=root, planner=planner, calendar_writer=calendar_writer)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def planner(self) -> LifePlanPort | None:
        return self._planner

    @property
    def calendar_writer(self) -> CalendarBlockPort | None:
        return self._calendar_writer

    @property
    def store(self) -> LifePlanStore:
        return self._store

    def execute_plan(self, request: LifePlanRequest) -> LifePlanResult:
        planner = self._planner
        if planner is None:
            raise ValueError("invalid LifeOS planner")
        return planner.plan(request)

    def execute_reset(self, request: LifeResetRequest) -> LifeResetResult:
        planner = self._planner
        if planner is None:
            raise ValueError("invalid LifeOS planner")
        return planner.reset(request)

    def write_calendar(
        self,
        request: CalendarBlockRequest,
        *,
        approval: CalendarWriteApproval | None = None,
        writer: CalendarBlockPort | None = None,
    ) -> CalendarBlockWriteResult:
        calendar_writer = writer if writer is not None else self._calendar_writer
        if calendar_writer is None or approval is None:
            return CalendarBlockWriteResult(
                block_key=request.block_key,
                disposition=CalendarBlockWriteDisposition.DISABLED,
            )
        integration = LifeOSIntegration(
            config=IntegrationConfig(
                live_adapters=frozenset({Capability.LIFE_OS}),
                lifeos_external_writes_enabled=True,
            ),
            store=self._store,
        )
        return integration.write_calendar(
            request,
            writer=calendar_writer,
            approval=approval,
        )

    def midday(self, request: LifePlanRequest) -> LifeOSRuntimeInvocation:
        return self._stage_plan_like(
            operation=LifeOSRuntimeOperation.MIDDAY,
            request=request,
            command=(
                "open-brain",
                "lifeos",
                "nudge",
                "midday",
                f"--date={request.plan_date.isoformat()}",
                "--json",
            ),
        )

    def plan(self, request: LifePlanRequest) -> LifeOSRuntimeInvocation:
        return self._stage_plan_like(
            operation=LifeOSRuntimeOperation.PLAN,
            request=request,
            command=(
                "open-brain",
                "lifeos",
                "plan",
                f"--date={request.plan_date.isoformat()}",
                "--generic-titles",
                "--json",
            ),
        )

    @overload
    def reset(self, request: LifeResetRequest) -> LifeOSRuntimeInvocation: ...

    @overload
    def reset(self, request: str) -> LifeResetDisposition: ...

    def reset(
        self,
        request: LifeResetRequest | str,
    ) -> LifeOSRuntimeInvocation | LifeResetDisposition:
        if isinstance(request, str):
            return self._store.reset(request)
        if not isinstance(request, LifeResetRequest):
            raise ValueError("invalid LifeOS reset request")
        state, payload_exists = self._read_state()
        plan_date = request.plan_date
        plan_key = _plan_key(plan_date)
        key = plan_date.isoformat()
        current = state.reset.get(key)
        if current is not None:
            if current == request:
                disposition = LifeOSRuntimeDisposition.DUPLICATE
            else:
                raise ValueError("conflicting LifeOS runtime reset replay")
        else:
            state.reset[key] = request
            self._write_state(state, payload_exists=payload_exists)
            disposition = LifeOSRuntimeDisposition.STAGED
        return LifeOSRuntimeInvocation(
            operation=LifeOSRuntimeOperation.RESET,
            plan_date=plan_date,
            plan_key=plan_key,
            state_ref=_state_ref(LifeOSRuntimeOperation.RESET, plan_date),
            disposition=disposition,
            command=(
                "open-brain",
                "lifeos",
                "reset",
                f"--date={plan_date.isoformat()}",
                "--json",
            ),
        )

    def load(
        self,
        *,
        operation: LifeOSRuntimeOperation,
        plan_date: date,
    ) -> LifePlanRequest | LifeResetRequest | None:
        if not isinstance(operation, LifeOSRuntimeOperation) or not isinstance(plan_date, date):
            raise ValueError("invalid LifeOS runtime lookup")
        state, _ = self._read_state()
        key = plan_date.isoformat()
        if operation is LifeOSRuntimeOperation.MIDDAY:
            return state.midday.get(key)
        if operation is LifeOSRuntimeOperation.PLAN:
            return state.plan.get(key)
        return state.reset.get(key)

    def _stage_plan_like(
        self,
        *,
        operation: LifeOSRuntimeOperation,
        request: LifePlanRequest,
        command: tuple[str, ...],
    ) -> LifeOSRuntimeInvocation:
        if not isinstance(request, LifePlanRequest):
            raise ValueError("invalid LifeOS plan request")
        state, payload_exists = self._read_state()
        plan_date = request.plan_date
        key = plan_date.isoformat()
        plan_key = _plan_key(plan_date)
        destination = state.midday if operation is LifeOSRuntimeOperation.MIDDAY else state.plan
        current = destination.get(key)
        if current is not None:
            if current == request:
                disposition = LifeOSRuntimeDisposition.DUPLICATE
            else:
                raise ValueError(f"conflicting LifeOS runtime {operation.value} replay")
        else:
            destination[key] = request
            self._write_state(state, payload_exists=payload_exists)
            disposition = LifeOSRuntimeDisposition.STAGED
        return LifeOSRuntimeInvocation(
            operation=operation,
            plan_date=plan_date,
            plan_key=plan_key,
            state_ref=_state_ref(operation, plan_date),
            disposition=disposition,
            command=command,
        )

    def _read_state(self) -> tuple[_RuntimeState, bool]:
        payload = read_confined(root=self._root, relative=_STATE_RELATIVE)
        if payload is None:
            return _RuntimeState(midday={}, plan={}, reset={}), False
        return _state_from_bytes(payload), True

    def _write_state(self, state: _RuntimeState, *, payload_exists: bool) -> None:
        atomic_replace(
            root=self._root,
            relative=_STATE_RELATIVE,
            data=_state_to_bytes(state),
            require_existing=payload_exists,
        )


PersistentLifeOSRuntime = LifeOSPlanningRuntime
LifeOSRuntimeCapability = LifeOSPlanningRuntime


def _validated_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise RootConfinementError("unsafe storage root")
    try:
        metadata = root.lstat()
    except OSError:
        raise RootConfinementError("unsafe storage root") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RootConfinementError("unsafe storage root")
    return root


def _state_ref(operation: LifeOSRuntimeOperation, plan_date: date) -> str:
    digest = sha256(f"{operation.value}:{plan_date.isoformat()}".encode("ascii")).hexdigest()
    return f"{_OPAQUE_STATE_PREFIX}{operation.value}_{digest}"


def _plan_key(plan_date: date) -> str:
    digest = sha256(plan_date.isoformat().encode("ascii")).hexdigest()
    return "life_plan_" + digest


def _state_to_bytes(state: _RuntimeState) -> bytes:
    value = {
        "schema_version": 1,
        "midday": _plan_mapping_to_dict(state.midday, generic_titles=False),
        "plan": _plan_mapping_to_dict(state.plan, generic_titles=True),
        "reset": _reset_mapping_to_dict(state.reset),
    }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _plan_mapping_to_dict(
    requests: dict[str, LifePlanRequest],
    *,
    generic_titles: bool,
) -> dict[str, object]:
    return {
        key: {
            "action_candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "review_id": candidate.review_id,
                }
                for candidate in request.action_candidates
            ],
            "generic_titles": generic_titles,
            "plan_date": request.plan_date.isoformat(),
            "plan_key": _plan_key(request.plan_date),
        }
        for key, request in sorted(requests.items())
    }


def _reset_mapping_to_dict(requests: dict[str, LifeResetRequest]) -> dict[str, object]:
    return {
        key: {
            "plan_date": request.plan_date.isoformat(),
            "plan_key": _plan_key(request.plan_date),
        }
        for key, request in sorted(requests.items())
    }


def _state_from_bytes(payload: bytes) -> _RuntimeState:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise LifeOSRuntimeStateError("invalid LifeOS runtime state") from None
    if not isinstance(value, dict) or set(value) != {"schema_version", "midday", "plan", "reset"}:
        raise LifeOSRuntimeStateError("invalid LifeOS runtime state")
    if value["schema_version"] != 1:
        raise LifeOSRuntimeStateError("invalid LifeOS runtime state")
    try:
        midday = _plan_mapping_from_dict(value["midday"], generic_titles=False)
        plan = _plan_mapping_from_dict(value["plan"], generic_titles=True)
        reset = _reset_mapping_from_dict(value["reset"])
    except (TypeError, ValueError, KeyError):
        raise LifeOSRuntimeStateError("invalid LifeOS runtime state") from None
    state = _RuntimeState(midday=midday, plan=plan, reset=reset)
    if _state_to_bytes(state) != payload:
        raise LifeOSRuntimeStateError("invalid LifeOS runtime state")
    return state


def _plan_mapping_from_dict(
    value: object,
    *,
    generic_titles: bool,
) -> dict[str, LifePlanRequest]:
    if not isinstance(value, dict):
        raise ValueError
    result: dict[str, LifePlanRequest] = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValueError
        if set(entry) != {"action_candidates", "generic_titles", "plan_date", "plan_key"}:
            raise ValueError
        if entry["generic_titles"] is not generic_titles:
            raise ValueError
        plan_date = _date_from_string(entry["plan_date"])
        if key != plan_date.isoformat() or entry["plan_key"] != _plan_key(plan_date):
            raise ValueError
        candidates_value = entry["action_candidates"]
        if not isinstance(candidates_value, list):
            raise ValueError
        candidates = tuple(
            _candidate_from_dict(candidate_value) for candidate_value in candidates_value
        )
        request = LifePlanRequest(plan_date=plan_date, action_candidates=candidates)
        result[key] = request
    return result


def _reset_mapping_from_dict(value: object) -> dict[str, LifeResetRequest]:
    if not isinstance(value, dict):
        raise ValueError
    result: dict[str, LifeResetRequest] = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValueError
        if set(entry) != {"plan_date", "plan_key"}:
            raise ValueError
        plan_date = _date_from_string(entry["plan_date"])
        if key != plan_date.isoformat() or entry["plan_key"] != _plan_key(plan_date):
            raise ValueError
        result[key] = LifeResetRequest(plan_date=plan_date)
    return result


def _candidate_from_dict(value: object) -> ReviewGatedActionCandidate:
    if not isinstance(value, dict) or set(value) != {"candidate_id", "review_id"}:
        raise ValueError
    return ReviewGatedActionCandidate(
        candidate_id=value["candidate_id"],
        review_id=value["review_id"],
    )


def _date_from_string(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError
    return date.fromisoformat(value)
