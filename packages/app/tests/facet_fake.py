"""Explicit scripted facet engine for adapter tests; never a production fallback.

Each call consumes one configured outcome. Configure retries separately so an accidental
second write cannot pass by receiving an invented success. The fake records trusted authority
but does not simulate real-engine authorization, persistence, or idempotency.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TypeVar

from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

_Response = TypeVar("_Response", bound=fc.FacetResponse)


@dataclass(frozen=True)
class FacetCall:
    operation: str
    request: fc.FacetRequest
    authority: EffectiveAuthority


@dataclass(frozen=True)
class _Outcome:
    result: fc.FacetResponse | fc.FacetError
    expected_request: fc.FacetRequest | None


class FakeFacetEngine:
    """Protocol-conforming scripted outcomes, using the real engine wire contracts."""

    def __init__(self) -> None:
        self._outcomes: dict[str, deque[_Outcome]] = defaultdict(deque)
        self._calls: list[FacetCall] = []

    @property
    def calls(self) -> tuple[FacetCall, ...]:
        return tuple(self._calls)

    def configure(
        self,
        operation: str,
        outcome: object,
        *,
        expected_request: object | None = None,
    ) -> None:
        if operation not in fc.REQUEST_TYPES:
            raise fc.FacetError("unsupported_capability")
        request = (
            None
            if expected_request is None
            else fc.request_from_wire(
                operation,
                expected_request.to_wire()
                if isinstance(expected_request, fc.FacetRequest)
                else expected_request,
            )
        )
        result = (
            outcome
            if isinstance(outcome, fc.FacetError)
            else fc.response_from_wire(operation, outcome)
        )
        self._outcomes[operation].append(_Outcome(result, request))

    def assert_exhausted(self) -> None:
        pending = {operation: len(queue) for operation, queue in self._outcomes.items() if queue}
        assert not pending, f"Unused facet outcomes: {pending}"

    def _call(
        self,
        operation: str,
        request: fc.FacetRequest,
        authority: EffectiveAuthority,
        response_type: type[_Response],
    ) -> _Response:
        if not isinstance(request, fc.REQUEST_TYPES[operation]):
            raise fc.FacetError("invalid_arguments")
        normalized = fc.request_from_wire(operation, request.to_wire())
        self._calls.append(FacetCall(operation, normalized, authority))
        queue = self._outcomes[operation]
        assert queue, f"Unconfigured facet call: {operation}"
        outcome = queue[0]
        if outcome.expected_request is not None:
            assert normalized.to_wire() == outcome.expected_request.to_wire(), (
                f"Unexpected facet request: {operation}"
            )
        queue.popleft()
        if isinstance(outcome.result, fc.FacetError):
            raise outcome.result
        assert isinstance(outcome.result, response_type)
        return outcome.result

    def dimensions_list(
        self, request: fc.DimensionsListRequest, *, authority: EffectiveAuthority
    ) -> fc.DimensionsListResponse:
        return self._call("facet.dimension.list", request, authority, fc.DimensionsListResponse)

    def dimension_create(
        self, request: fc.DimensionCreateRequest, *, authority: EffectiveAuthority
    ) -> fc.DimensionCreateResponse:
        return self._call("facet.dimension.create", request, authority, fc.DimensionCreateResponse)

    def dimension_rename(
        self, request: fc.DimensionRenameRequest, *, authority: EffectiveAuthority
    ) -> fc.DimensionRenameResponse:
        return self._call("facet.dimension.rename", request, authority, fc.DimensionRenameResponse)

    def dimension_archive(
        self, request: fc.DimensionArchiveRequest, *, authority: EffectiveAuthority
    ) -> fc.DimensionArchiveResponse:
        return self._call(
            "facet.dimension.archive", request, authority, fc.DimensionArchiveResponse
        )

    def values_list(
        self, request: fc.ValuesListRequest, *, authority: EffectiveAuthority
    ) -> fc.ValuesListResponse:
        return self._call("facet.value.list", request, authority, fc.ValuesListResponse)

    def value_create(
        self, request: fc.ValueCreateRequest, *, authority: EffectiveAuthority
    ) -> fc.ValueCreateResponse:
        return self._call("facet.value.create", request, authority, fc.ValueCreateResponse)

    def value_rename(
        self, request: fc.ValueRenameRequest, *, authority: EffectiveAuthority
    ) -> fc.ValueRenameResponse:
        return self._call("facet.value.rename", request, authority, fc.ValueRenameResponse)

    def value_archive(
        self, request: fc.ValueArchiveRequest, *, authority: EffectiveAuthority
    ) -> fc.ValueArchiveResponse:
        return self._call("facet.value.archive", request, authority, fc.ValueArchiveResponse)

    def assignments_get(
        self, request: fc.AssignmentsGetRequest, *, authority: EffectiveAuthority
    ) -> fc.AssignmentsGetResponse:
        return self._call("facet.assignment.get", request, authority, fc.AssignmentsGetResponse)

    def assignments_patch(
        self, request: fc.AssignmentsPatchRequest, *, authority: EffectiveAuthority
    ) -> fc.AssignmentsPatchResponse:
        return self._call("facet.assignment.patch", request, authority, fc.AssignmentsPatchResponse)

    def queries_list(
        self, request: fc.QueriesListRequest, *, authority: EffectiveAuthority
    ) -> fc.QueriesListResponse:
        return self._call("facet.query.list", request, authority, fc.QueriesListResponse)

    def query_get(
        self, request: fc.QueryGetRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryGetResponse:
        return self._call("facet.query.get", request, authority, fc.QueryGetResponse)

    def query_create(
        self, request: fc.QueryCreateRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryCreateResponse:
        return self._call("facet.query.create", request, authority, fc.QueryCreateResponse)

    def query_update(
        self, request: fc.QueryUpdateRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryUpdateResponse:
        return self._call("facet.query.update", request, authority, fc.QueryUpdateResponse)

    def query_archive(
        self, request: fc.QueryArchiveRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryArchiveResponse:
        return self._call("facet.query.archive", request, authority, fc.QueryArchiveResponse)

    def query_attach(
        self, request: fc.QueryAttachRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryAttachResponse:
        return self._call("facet.query.attach", request, authority, fc.QueryAttachResponse)

    def query_attachment_get(
        self, request: fc.QueryAttachmentGetRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryAttachmentGetResponse:
        return self._call(
            "facet.query.attachment.get", request, authority, fc.QueryAttachmentGetResponse
        )

    def query_page(
        self, request: fc.QueryPageRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryPageResponse:
        return self._call("facet.query.page", request, authority, fc.QueryPageResponse)

    def history_list(
        self, request: fc.HistoryListRequest, *, authority: EffectiveAuthority
    ) -> fc.HistoryListResponse:
        return self._call("facet.history.list", request, authority, fc.HistoryListResponse)

    def history_state(
        self, request: fc.HistoryStateRequest, *, authority: EffectiveAuthority
    ) -> fc.HistoryStateResponse:
        return self._call("facet.history.state", request, authority, fc.HistoryStateResponse)

    def propose_from_facet_query(
        self, request: fc.ProposeFromFacetQueryRequest, *, authority: EffectiveAuthority
    ) -> fc.ProposeFromFacetQueryResponse:
        return self._call(
            "review.propose_from_facet_query", request, authority, fc.ProposeFromFacetQueryResponse
        )

    def vault_preview(
        self, request: fc.VaultPreviewRequest, *, authority: EffectiveAuthority
    ) -> fc.VaultPreviewResponse:
        return self._call("facet.vault.preview", request, authority, fc.VaultPreviewResponse)

    def vault_apply(
        self, request: fc.VaultApplyRequest, *, authority: EffectiveAuthority
    ) -> fc.VaultApplyResponse:
        return self._call("facet.vault.apply", request, authority, fc.VaultApplyResponse)
