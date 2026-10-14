"""Explicit owner resolution through existing engine facet operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine import open_local_engine

from open_brain_collector.capture_contracts import digest
from open_brain_collector.capture_review import CaptureReviewStore
from open_brain_collector.capture_workflow import owner_authority
from open_brain_collector.runner import _open_existing_collector_profile
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_storage import PrivateJsonStore


class CandidateResolver:
    def __init__(self, root: Path, brain: Path) -> None:
        self.review = CaptureReviewStore(PrivateJsonStore(root / "capture-review"))
        self.tasks = open_local_engine(_open_existing_collector_profile(brain))
        self.authority = owner_authority()

    def _candidate(self, identity: str) -> dict[str, object]:
        for candidate in self.review.candidates():
            if candidate["candidate_id"] == identity:
                return candidate
        raise LiveSourceError("candidate_not_found")

    def _catalog(self, dimension: str | None = None) -> list[dict[str, Any]]:
        assert self.tasks.facets is not None
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):
            response = cast(
                dict[str, Any],
                (
                    self.tasks.facets.dimensions_list(
                        fc.DimensionsListRequest(limit=50, cursor=cursor), authority=self.authority
                    )
                    if dimension is None
                    else self.tasks.facets.values_list(
                        fc.ValuesListRequest(dimension_id=dimension, limit=50, cursor=cursor),
                        authority=self.authority,
                    )
                ).to_wire(),
            )
            if response["status"] != "ok":
                raise LiveSourceError("candidate_catalog_unavailable")
            items.extend(response["items"])
            cursor = response["next_cursor"]
            if cursor is None:
                return items
        raise LiveSourceError("candidate_catalog_limit")

    def assignment_plan(
        self, candidate: dict[str, object], value: str
    ) -> tuple[dict[str, object], ...]:
        assert self.tasks.facets is not None
        requests = []
        for capture_id in cast(list[str], candidate["evidence_capture_ids"]):
            result = self.tasks.facets.assignments_get(
                fc.AssignmentsGetRequest(record_id=capture_id), authority=self.authority
            ).to_wire()
            if result["status"] != "ok":
                raise LiveSourceError("candidate_evidence_conflict")
            item = cast(dict[str, Any], result["item"])
            requests.append(
                {
                    "operation_id": "candidate-assign-"
                    + digest(
                        [
                            candidate["candidate_id"],
                            capture_id,
                            value,
                            item["revision_id"],
                            item["version"],
                        ]
                    ),
                    "dto_version": 1,
                    "record_id": capture_id,
                    "expected_revision_id": item["revision_id"],
                    "expected_version": item["version"],
                    "add_value_ids": [value],
                    "remove_value_ids": [],
                }
            )
        return tuple(requests)

    def decide(
        self,
        identity: str,
        action: str,
        *,
        value_id: str | None = None,
        deferred_until: str | None = None,
    ) -> dict[str, object]:
        candidate = self._candidate(identity)
        dimension = cast(str, candidate["dimension_id"])
        creation = None
        assignments: tuple[dict[str, object], ...] = ()
        if action == "map":
            if not any(value["value_id"] == value_id for value in self._catalog(dimension)):
                raise LiveSourceError("candidate_invalid_value")
            assert value_id is not None
            assignments = self.assignment_plan(candidate, value_id)
        elif action == "create":
            parent = next(
                (item for item in self._catalog() if item["dimension_id"] == dimension), None
            )
            if parent is None:
                raise LiveSourceError("candidate_invalid_dimension")
            creation = {
                "dto_version": 1,
                "operation_id": "candidate-create-" + digest(identity),
                "expected_version": 0,
                "dimension_id": dimension,
                "expected_dimension_version": parent["version"],
                "name": candidate["name"],
            }
        decision = self.review.decide(
            identity,
            action,
            authority=self.authority,
            value_id=value_id,
            deferred_until=deferred_until,
            creation_request=creation,
            assignment_requests=assignments,
        )
        return self.retry(identity) if action in {"map", "create"} else decision

    def retry(self, identity: str, *, replan: bool = False) -> dict[str, object]:
        candidate = self._candidate(identity)
        if replan:
            value = candidate["value_id"]
            if type(value) is not str:
                raise LiveSourceError("candidate_not_approved")
            self.review.replan(
                identity, self.assignment_plan(candidate, value), authority=self.authority
            )

        def create(request: dict[str, object]) -> str:
            assert self.tasks.facets is not None
            response = self.tasks.facets.value_create(
                cast(fc.ValueCreateRequest, fc.request_from_wire("facet.value.create", request)),
                authority=self.authority,
            ).to_wire()
            if response["status"] not in {"applied", "duplicate"}:
                raise LiveSourceError("candidate_creation_conflict")
            return cast(str, response["subject_id"])

        def assign(request: dict[str, object]) -> None:
            assert self.tasks.facets is not None
            response = self.tasks.facets.assignments_patch(
                cast(
                    fc.AssignmentsPatchRequest,
                    fc.request_from_wire("facet.assignment.patch", request),
                ),
                authority=self.authority,
            ).to_wire()
            if response["status"] not in {"applied", "duplicate", "unchanged"}:
                raise LiveSourceError("candidate_assignment_conflict")

        return self.review.apply(
            identity,
            authority=self.authority,
            create_value=create,
            assign=assign,
            assignment_plan=self.assignment_plan,
        )
