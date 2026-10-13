"""C0 interface contract checks; real CLI/MCP and authorization arrive in A/E."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS
from packages.app.tests.facet_fake import FakeFacetEngine

DIMENSION = "facetdim_00000000-0000-4000-8000-000000000001"
EVENT = "facetevent_00000000-0000-4000-8000-000000000002"
CREATE = "facet.dimension.create"
AUTHORITY = EffectiveAuthority(
    "synthetic-owner", "synthetic-session", frozenset(), None, owner=True
)


def _request() -> fc.DimensionCreateRequest:
    request = fc.request_from_wire(
        CREATE,
        {
            "dto_version": 1,
            "operation_id": "fixture-create",
            "expected_version": 0,
            "name": "Project",
        },
    )
    assert isinstance(request, fc.DimensionCreateRequest)
    return request


def _receipt() -> dict[str, object]:
    return {
        "status": "applied",
        "dto_version": 1,
        "operation_id": "fixture-create",
        "subject_id": DIMENSION,
        "version": 1,
        "event_id": EVENT,
    }


def test_fake_satisfies_all_engine_protocols_without_a_brain() -> None:
    fake = FakeFacetEngine()
    facets: fc.FacetTasks = fake
    publication: fc.FacetPublicationTasks = fake
    vault: fc.FacetVaultTasks = fake
    assert facets is fake
    assert publication is fake
    assert vault is fake
    for method in fc.OPERATION_METHODS.values():
        signature = inspect.signature(getattr(fake, method))
        assert signature.parameters["authority"].kind is inspect.Parameter.KEYWORD_ONLY


def test_parser_fake_serializer_preserves_exact_configured_result_and_authority() -> None:
    fake = FakeFacetEngine()
    receipt = _receipt()
    fake.configure(CREATE, receipt, expected_request=_request())
    receipt["version"] = 999  # Caller-owned values cannot mutate scripted outcomes.
    tasks: fc.FacetTasks = fake
    response = tasks.dimension_create(_request(), authority=AUTHORITY)
    assert fc.response_to_wire(CREATE, response) == _receipt()
    assert fake.calls[0].authority is AUTHORITY
    assert fake.calls[0].request.to_wire() == _request().to_wire()
    fake.assert_exhausted()
    with pytest.raises(AssertionError, match="Unconfigured facet call"):
        tasks.dimension_create(_request(), authority=AUTHORITY)


def test_fake_requires_explicit_retry_and_noop_receipts() -> None:
    fake = FakeFacetEngine()
    applied = _receipt()
    for receipt in (applied, applied):
        fake.configure(CREATE, receipt)
    results = [
        fc.response_to_wire(CREATE, fake.dimension_create(_request(), authority=AUTHORITY))
        for _ in range(2)
    ]
    rename = "facet.dimension.rename"
    unchanged = applied | {"status": "unchanged", "operation_id": "fixture-noop", "event_id": None}
    request = fc.request_from_wire(
        rename,
        {
            "dto_version": 1,
            "operation_id": "fixture-noop",
            "expected_version": 1,
            "dimension_id": DIMENSION,
            "name": "Project",
        },
    )
    assert isinstance(request, fc.DimensionRenameRequest)
    fake.configure(rename, unchanged, expected_request=request)
    result = fake.dimension_rename(request, authority=AUTHORITY)
    assert results == [applied, applied]
    assert fc.response_to_wire(rename, result) == unchanged
    assert len(fake.calls) == 3
    fake.assert_exhausted()


def test_fake_propagates_configured_domain_error_without_default_success() -> None:
    fake = FakeFacetEngine()
    fake.configure(CREATE, fc.FacetError("version_conflict"))
    with pytest.raises(fc.FacetError) as caught:
        fake.dimension_create(_request(), authority=AUTHORITY)
    assert caught.value.code == "version_conflict"
    assert len(fake.calls) == 1
    fake.assert_exhausted()


def test_fake_rejects_unknown_operations_invalid_outcomes_and_wrong_request_types() -> None:
    fake = FakeFacetEngine()
    with pytest.raises(fc.FacetError):
        fake.configure("facet.unknown", _receipt())
    with pytest.raises(fc.FacetError):
        fake.configure(CREATE, {"status": "applied"})
    wrong: Any = fc.request_from_wire("facet.dimension.list", {"dto_version": 1})
    with pytest.raises(fc.FacetError):
        fake.dimension_create(wrong, authority=AUTHORITY)
    assert not fake.calls


def test_request_mismatch_does_not_consume_a_scripted_outcome() -> None:
    fake = FakeFacetEngine()
    expected = _request().to_wire() | {"name": "Different"}
    fake.configure(CREATE, _receipt(), expected_request=expected)
    with pytest.raises(AssertionError, match="Unexpected facet request"):
        fake.dimension_create(_request(), authority=AUTHORITY)
    with pytest.raises(AssertionError, match="Unused facet outcomes"):
        fake.assert_exhausted()


def test_fake_list_and_error_paths_use_real_closed_contracts() -> None:
    fake = FakeFacetEngine()
    operation = "facet.dimension.list"
    wire = {"dto_version": 1}
    response = {
        "status": "ok",
        "dto_version": 1,
        "items": [],
        "next_cursor": None,
        "complete": True,
    }
    fake.configure(operation, response, expected_request=wire)
    request = fc.request_from_wire(operation, fc.parse_wire(b'{"dto_version":1}'))
    assert isinstance(request, fc.DimensionsListRequest)
    result = fake.dimensions_list(request, authority=AUTHORITY)
    assert fc.response_to_wire(operation, result) == response
    assert fake.calls[0].request.to_wire() == wire | {
        "limit": 50,
        "cursor": None,
        "include_archived": False,
    }
    with pytest.raises(fc.FacetError):
        fc.request_from_wire(operation, wire | {"owner": True})
    fake.assert_exhausted()


def test_c0_does_not_advertise_unimplemented_facet_tools() -> None:
    names = {str(tool["name"]) for tool in MCP_REGISTERED_TOOLS}
    assert not any("facet" in name for name in names)
