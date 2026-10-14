"""Owner-decided entity candidates and an independent proposal attention queue."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import cast

from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain_collector.capture_contracts import digest
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore

Request = dict[str, object]
AssignmentPlan = Callable[[Request, str], tuple[Request, ...]]


def _owner(authority: EffectiveAuthority) -> None:
    if not isinstance(authority, EffectiveAuthority) or not authority.owner:
        raise LiveSourceError("candidate_owner_required")


def _text(value: object) -> None:
    if type(value) is not str or not value or len(value) > 512:
        raise LiveSourceError("candidate_invalid_request")


def _time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result
    except ValueError, TypeError:
        raise LiveSourceError("attention_invalid_time") from None


def _assignments(requests: tuple[Request, ...], value_id: str) -> list[Request]:
    if type(requests) is not tuple or len(requests) > 1000:
        raise LiveSourceError("candidate_invalid_assignment_plan")
    result = []
    for request in requests:
        if (
            type(request) is not dict
            or not {
                "operation_id",
                "record_id",
                "expected_revision_id",
                "expected_version",
                "add_value_ids",
                "remove_value_ids",
            }
            <= set(request)
            or request["add_value_ids"] != [value_id]
            or request["remove_value_ids"] != []
            or type(request["expected_version"]) is not int
            or request["expected_version"] < 0
        ):
            raise LiveSourceError("candidate_invalid_assignment_plan")
        for key in ("operation_id", "record_id", "expected_revision_id"):
            _text(request[key])
        result.append(cast(Request, json.loads(bounded_json(request))))
    if len({str(r["operation_id"]) for r in result}) != len(result):
        raise LiveSourceError("candidate_invalid_assignment_plan")
    return result


class CaptureReviewStore:
    def __init__(
        self, store: PrivateJsonStore, *, max_candidates: int = 1000, max_attention: int = 1000
    ) -> None:
        if any(type(n) is not int or not 1 <= n <= 10_000 for n in (max_candidates, max_attention)):
            raise LiveSourceError("candidate_invalid_limits")
        self.store = store
        self.max_candidates = max_candidates
        self.max_attention = max_attention

    def _load(self) -> dict[str, object]:
        value = self.store.read("capture-review-v1.json")
        if value is None:
            return {"schema_version": 1, "candidates": {}, "attention": {}}
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "candidates", "attention"}
            or value["schema_version"] != 1
            or not isinstance(value["candidates"], dict)
            or not isinstance(value["attention"], dict)
        ):
            raise LiveSourceError("candidate_invalid_state")
        return cast(dict[str, object], value)

    def _save(self, state: dict[str, object]) -> None:
        self.store.write("capture-review-v1.json", state)

    def discover(
        self,
        name: str,
        dimension_id: str,
        workspace_id: str,
        evidence_capture_ids: tuple[str, ...],
        *,
        possible_value_ids: tuple[str, ...] = (),
        priority: int = 0,
    ) -> str:
        for value in (name, dimension_id, workspace_id):
            _text(value)
        if (
            type(priority) is not int
            or not 0 <= priority <= 100
            or type(evidence_capture_ids) is not tuple
            or not evidence_capture_ids
            or type(possible_value_ids) is not tuple
        ):
            raise LiveSourceError("candidate_invalid_request")
        for value in (*evidence_capture_ids, *possible_value_ids):
            _text(value)
        candidate_id = "candidate:" + digest([workspace_id, dimension_id, name])
        with self.store.lock("capture-review"):
            state = self._load()
            candidates = cast(dict[str, Request], state["candidates"])
            if candidate_id not in candidates:
                if len(candidates) >= self.max_candidates:
                    raise LiveSourceError("candidate_queue_full")
                candidates[candidate_id] = {
                    "candidate_id": candidate_id,
                    "name": name,
                    "dimension_id": dimension_id,
                    "workspace_id": workspace_id,
                    "evidence_capture_ids": [],
                    "possible_value_ids": [],
                    "priority": priority,
                    "state": "pending",
                    "decision": None,
                    "assignment_requests": [],
                    "assigned": [],
                    "value_id": None,
                    "creation_request": None,
                    "deferred_until": None,
                }
            candidate = candidates[candidate_id]
            previous_evidence = set(cast(list[str], candidate["evidence_capture_ids"]))
            for key, values in (
                ("evidence_capture_ids", evidence_capture_ids),
                ("possible_value_ids", possible_value_ids),
            ):
                candidate[key] = sorted(set(cast(list[str], candidate[key])) | set(values))
            if candidate["state"] == "resolved" and set(evidence_capture_ids) - previous_evidence:
                candidate["state"] = "backfill_pending"
            candidate["priority"] = max(cast(int, candidate["priority"]), priority)
            self._save(state)
        return candidate_id

    def candidates(self) -> tuple[Request, ...]:
        with self.store.lock("capture-review"):
            return tuple(cast(dict[str, Request], self._load()["candidates"]).values())

    def decide(
        self,
        candidate_id: str,
        action: str,
        *,
        authority: EffectiveAuthority,
        value_id: str | None = None,
        deferred_until: str | None = None,
        creation_request: Request | None = None,
        assignment_requests: tuple[Request, ...] = (),
    ) -> Request:
        _owner(authority)
        if action not in {"map", "create", "reject", "defer"}:
            raise LiveSourceError("candidate_invalid_decision")
        if action == "defer":
            if deferred_until is None:
                raise LiveSourceError("candidate_invalid_decision")
            _time(deferred_until)
        with self.store.lock("capture-review"):
            state = self._load()
            candidate = cast(dict[str, Request], state["candidates"])[candidate_id]
            if candidate["decision"] not in {None, "defer"}:
                raise LiveSourceError("candidate_already_decided")
            if action == "map":
                _text(value_id)
                candidate["assignment_requests"] = _assignments(
                    assignment_requests, cast(str, value_id)
                )
                candidate["value_id"] = value_id
            if action == "create":
                if (
                    not isinstance(creation_request, dict)
                    or creation_request.get("dimension_id") != candidate["dimension_id"]
                    or creation_request.get("name") != candidate["name"]
                    or type(creation_request.get("expected_dimension_version")) is not int
                ):
                    raise LiveSourceError("candidate_invalid_creation_plan")
                _text(creation_request.get("operation_id"))
                candidate["creation_request"] = json.loads(bounded_json(creation_request))
            candidate.update(decision=action, state=action, deferred_until=deferred_until)
            self._save(state)
            return candidate

    def apply(
        self,
        candidate_id: str,
        *,
        authority: EffectiveAuthority,
        create_value: Callable[[Request], str],
        assign: Callable[[Request], None],
        assignment_plan: AssignmentPlan | None = None,
    ) -> Request:
        _owner(authority)
        with self.store.lock("capture-review"):
            state = self._load()
            candidate = cast(dict[str, Request], state["candidates"])[candidate_id]
            if candidate["decision"] not in {"map", "create"}:
                raise LiveSourceError("candidate_not_approved")
            if candidate["value_id"] is None:
                value = create_value(json.loads(bounded_json(candidate["creation_request"])))
                _text(value)
                candidate["value_id"] = value
                self._save(state)
            if not candidate["assignment_requests"]:
                if assignment_plan is None:
                    raise LiveSourceError("candidate_assignment_plan_required")
                candidate["assignment_requests"] = _assignments(
                    assignment_plan(
                        json.loads(bounded_json(candidate)), cast(str, candidate["value_id"])
                    ),
                    cast(str, candidate["value_id"]),
                )
                self._save(state)
            for request in cast(list[Request], candidate["assignment_requests"]):
                operation_id = cast(str, request["operation_id"])
                if operation_id in cast(list[str], candidate["assigned"]):
                    continue
                assign(json.loads(bounded_json(request)))
                cast(list[str], candidate["assigned"]).append(operation_id)
                self._save(state)
            planned = {
                cast(str, request["record_id"])
                for request in cast(list[Request], candidate["assignment_requests"])
            }
            candidate["state"] = (
                "backfill_pending"
                if set(cast(list[str], candidate["evidence_capture_ids"])) - planned
                else "resolved"
            )
            self._save(state)
            return candidate

    def replan(
        self, candidate_id: str, requests: tuple[Request, ...], *, authority: EffectiveAuthority
    ) -> Request:
        _owner(authority)
        with self.store.lock("capture-review"):
            state = self._load()
            candidate = cast(dict[str, Request], state["candidates"])[candidate_id]
            if candidate["decision"] not in {"map", "create"} or candidate["value_id"] is None:
                raise LiveSourceError("candidate_not_approved")
            completed = [
                request
                for request in cast(list[Request], candidate["assignment_requests"])
                if request["operation_id"] in cast(list[str], candidate["assigned"])
            ]
            completed_records = {request["record_id"] for request in completed}
            candidate["assignment_requests"] = completed + [
                request
                for request in _assignments(requests, cast(str, candidate["value_id"]))
                if request["record_id"] not in completed_records
            ]
            candidate["state"] = candidate["decision"]
            self._save(state)
            return candidate

    def admit_attention(
        self, proposal_id: str, created_at: str, *, priority: int = 0, kind: str = "routine"
    ) -> bool:
        _text(proposal_id)
        date = _time(created_at)
        if kind not in {"routine", "commitment", "contradiction"} or type(priority) is not int:
            raise LiveSourceError("attention_invalid_request")
        with self.store.lock("capture-review"):
            state = self._load()
            attention = cast(dict[str, Request], state["attention"])
            if proposal_id in attention:
                return False
            week = date.isocalendar()[:2]
            if (
                sum(
                    _time(cast(str, v["created_at"])).isocalendar()[:2] == week
                    for v in attention.values()
                )
                >= 10
            ):
                raise LiveSourceError("attention_weekly_limit")
            if len(attention) >= self.max_attention:
                raise LiveSourceError("attention_queue_full")
            attention[proposal_id] = {
                "proposal_id": proposal_id,
                "created_at": created_at,
                "priority": priority,
                "kind": kind,
                "deferred_until": None,
                "dismissed": False,
            }
            self._save(state)
            return True

    def defer_attention(
        self, proposal_id: str, until: str, *, authority: EffectiveAuthority
    ) -> None:
        _owner(authority)
        _time(until)
        self._attention_update(proposal_id, "deferred_until", until)

    def dismiss_attention(self, proposal_id: str, *, authority: EffectiveAuthority) -> None:
        _owner(authority)
        self._attention_update(proposal_id, "dismissed", True)

    def _attention_update(self, proposal_id: str, key: str, value: object) -> None:
        with self.store.lock("capture-review"):
            state = self._load()
            cast(dict[str, Request], state["attention"])[proposal_id][key] = value
            self._save(state)

    def attention(self, now: str, status: Callable[[str], str]) -> tuple[Request, ...]:
        date = _time(now)
        with self.store.lock("capture-review"):
            attention = cast(dict[str, Request], self._load()["attention"])
            results = []
            for proposal_id, entry in attention.items():
                current_status = status(proposal_id)
                if current_status in {"approved", "rejected"} or entry["dismissed"]:
                    continue
                deferred = entry["deferred_until"]
                if deferred is not None and _time(cast(str, deferred)) > date:
                    continue
                if entry["kind"] == "routine" and date - _time(
                    cast(str, entry["created_at"])
                ) >= timedelta(days=30):
                    continue
                results.append({**entry, "status": current_status})
            return tuple(sorted(results, key=lambda v: -cast(int, v["priority"])))
