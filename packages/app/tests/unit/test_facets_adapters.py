"""F15 adapter checks against the explicit C0 scripted engine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.services.facets import (
    FacetAppAdapter,
    FacetSessionBudget,
    add_facet_grant_flags,
    facet_cli_exit_code,
    facet_error_result,
    facet_grants_from_namespace,
    facet_mcp_operation,
)
from open_brain.services.t03_adapters import T03SessionBudget
from packages.app.tests.facet_fake import FakeFacetEngine

WIRE_CASES = Path(__file__).resolve().parents[3] / "engine/tests/fixtures/facets/wire-cases.json"
CASES = json.loads(WIRE_CASES.read_text())["cases"]
ACCEPTED = {case["operation"]: case for case in CASES if case["outcome"] == "accept"}
GRANTS: dict[str, frozenset[str]] = {
    "facet.dimension.list": frozenset({"facet-read"}),
    "facet.dimension.create": frozenset({"facet-catalog-write"}),
    "facet.dimension.rename": frozenset({"facet-catalog-write"}),
    "facet.dimension.archive": frozenset({"facet-catalog-write"}),
    "facet.value.list": frozenset({"facet-read"}),
    "facet.value.create": frozenset({"facet-catalog-write"}),
    "facet.value.rename": frozenset({"facet-catalog-write"}),
    "facet.value.archive": frozenset({"facet-catalog-write"}),
    "facet.assignment.get": frozenset({"facet-read"}),
    "facet.assignment.patch": frozenset({"facet-assign"}),
    "facet.query.list": frozenset({"facet-read"}),
    "facet.query.get": frozenset({"facet-read"}),
    "facet.query.create": frozenset({"facet-query-write"}),
    "facet.query.update": frozenset({"facet-query-write"}),
    "facet.query.archive": frozenset({"facet-query-write"}),
    "facet.query.attach": frozenset({"facet-query-write"}),
    "facet.query.attachment.get": frozenset({"facet-read"}),
    "facet.query.page": frozenset({"facet-read", "search"}),
    "facet.history.list": frozenset({"facet-read", "history-read"}),
    "facet.history.state": frozenset({"facet-read", "history-read"}),
    "review.propose_from_facet_query": frozenset({"facet-read", "search", "review-propose"}),
}


def authority(*grants: str, owner: bool = False) -> EffectiveAuthority:
    return EffectiveAuthority(
        "synthetic-principal", "synthetic-session", frozenset(grants), None, owner=owner
    )


def adapter(fake: FakeFacetEngine, auth: EffectiveAuthority) -> FacetAppAdapter:
    return FacetAppAdapter(fake, auth, publication=fake, vault=fake)


@pytest.mark.parametrize("operation", tuple(ACCEPTED))
def test_every_operation_maps_to_c0_protocol_and_serializer(operation: str) -> None:
    fake = FakeFacetEngine()
    case = ACCEPTED[operation]
    fake.configure(operation, case["response"], expected_request=case["request"])
    app = adapter(fake, authority(owner=True))
    transport = "cli" if operation.startswith("facet.vault.") else "mcp"
    assert operation in app.available_operations(transport=transport)
    result = app.invoke(operation, case["request"], transport=transport)
    assert result == case["response"]
    assert len(fake.calls) == 1
    assert fake.calls[0].authority is app.authority
    assert fake.calls[0].request.to_wire() == case["request"]
    fake.assert_exhausted()


@pytest.mark.parametrize("operation,required", tuple(GRANTS.items()))
def test_default_off_and_each_missing_grant_reject_before_effects(
    operation: str, required: frozenset[str]
) -> None:
    fake = FakeFacetEngine()
    arguments = ACCEPTED[operation]["request"]
    for missing in required:
        app = adapter(fake, authority(*(required - {missing})))
        assert operation not in app.available_operations()
        with pytest.raises(fc.FacetError, match="unsupported_capability"):
            app.invoke(operation, arguments)
    app = adapter(fake, authority(*required))
    assert operation in app.available_operations()
    assert not fake.calls


def test_tool_discovery_never_exposes_vault_and_requires_callable() -> None:
    fake = FakeFacetEngine()
    app = FacetAppAdapter(fake, authority(owner=True), publication=None, vault=fake)
    names = {row["name"] for row in app.tool_definitions()}
    assert "brain_facet_vault_preview" not in names
    assert "brain_review_propose_from_facet_query" not in names
    assert "review.propose_from_facet_query" not in app.available_operations()
    assert set(app.available_operations(transport="cli")) == set(fc.OPERATION_METHODS) - {
        "review.propose_from_facet_query"
    }
    assert facet_mcp_operation("brain_facet_vault_apply") is None


def test_closed_parser_rejects_wire_ambiguity_before_admission() -> None:
    fake = FakeFacetEngine()
    app = adapter(fake, authority("facet-read"))
    for case in CASES:
        if case["outcome"] != "reject":
            continue
        with pytest.raises(fc.FacetError, match="invalid_arguments"):
            app.invoke(case["operation"], case["raw_request"])
    assert not fake.calls
    assert not app.budget.calls


def test_admitted_failures_and_retries_each_charge_call_budget() -> None:
    fake = FakeFacetEngine()
    case = ACCEPTED["facet.dimension.create"]
    fake.configure(case["operation"], fc.FacetError("version_conflict"))
    fake.configure(case["operation"], case["response"])
    fake.configure(case["operation"], case["response"])
    app = adapter(fake, authority("facet-catalog-write"))
    with pytest.raises(fc.FacetError, match="version_conflict"):
        app.invoke(case["operation"], case["request"])
    assert app.budget.calls["write"] == 1
    assert app.budget.response_bytes.get("write", 0) == 0
    for _ in range(2):
        assert app.invoke(case["operation"], case["request"]) == case["response"]
    assert app.budget.calls["write"] == 3
    assert len(fake.calls) == 3
    fake.assert_exhausted()


def test_call_limit_and_receipt_reservation_reject_before_write() -> None:
    fake = FakeFacetEngine()
    case = ACCEPTED["facet.dimension.create"]
    budget = FacetSessionBudget(calls={"write": 500})
    app = FacetAppAdapter(fake, authority("facet-catalog-write"), budget=budget)
    with pytest.raises(fc.FacetError, match="limit_exceeded"):
        app.invoke(case["operation"], case["request"])
    assert not fake.calls

    budget.calls["write"] = 0
    with pytest.raises(fc.FacetError, match="response_too_large"):
        app.invoke(case["operation"], case["request"], maximum_response_bytes=100)
    assert budget.calls["write"] == 1
    assert not fake.calls
    budget.response_bytes["write"] = 16 * 1024 * 1024 - 100
    with pytest.raises(fc.FacetError, match="limit_exceeded"):
        app.invoke(case["operation"], case["request"])
    assert budget.calls["write"] == 2
    assert not fake.calls


def test_transport_wrapping_is_charged_and_single_reads_never_truncated() -> None:
    fake = FakeFacetEngine()
    case = ACCEPTED["facet.assignment.get"]
    fake.configure(case["operation"], case["response"])
    app = adapter(fake, authority("facet-read"))
    with pytest.raises(fc.FacetError, match="response_too_large"):
        app.invoke(
            case["operation"],
            case["request"],
            maximum_response_bytes=200,
            encoded_size=lambda _result: 201,
        )
    assert app.budget.calls["metadata"] == 1
    assert app.budget.response_bytes.get("metadata", 0) == 0
    assert len(fake.calls) == 1


def test_error_projection_and_exit_codes_are_closed() -> None:
    error = fc.FacetError("operation_pending")
    assert facet_error_result(error) == {
        "status": "error",
        "dto_version": 1,
        "code": "operation_pending",
    }
    assert facet_cli_exit_code(error) == 75
    assert facet_cli_exit_code(fc.FacetError("version_conflict")) == 1
    assert facet_cli_exit_code(fc.FacetError("invalid_arguments"), grammar=True) == 2


def test_mcp_discovery_schemas_accept_c0_wire_requests() -> None:
    fake = FakeFacetEngine()
    app = adapter(fake, authority(owner=True))
    for definition in app.tool_definitions():
        name = definition["name"]
        operation = facet_mcp_operation(cast(str, name))
        assert operation is not None
        schema = definition["inputSchema"]
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(ACCEPTED[operation]["request"])


def test_agent_setup_flags_are_independent_and_default_off() -> None:
    parser = argparse.ArgumentParser()
    add_facet_grant_flags(parser)
    assert facet_grants_from_namespace(parser.parse_args([])) == frozenset()
    assert facet_grants_from_namespace(
        parser.parse_args(["--allow-facet-assign", "--allow-facet-read"])
    ) == frozenset({"facet-assign", "facet-read"})


def test_query_and_history_charge_shared_negotiated_budget() -> None:
    fake = FakeFacetEngine()
    negotiated = T03SessionBudget()
    budget = FacetSessionBudget(negotiated=negotiated)
    app = FacetAppAdapter(fake, authority("facet-read", "search", "history-read"), budget=budget)
    for operation in ("facet.query.page", "facet.history.list"):
        case = ACCEPTED[operation]
        fake.configure(operation, case["response"])
        app.invoke(operation, case["request"])
    assert negotiated.content_calls == 1
    assert negotiated.history_calls == 1
    assert negotiated.content_bytes == budget.response_bytes["search"]
    assert negotiated.history_bytes == budget.response_bytes["history"]


def test_request_transport_bound_and_review_quota_stop_before_handlers() -> None:
    fake = FakeFacetEngine()
    read = ACCEPTED["facet.dimension.list"]
    app = adapter(fake, authority("facet-read"))
    with pytest.raises(fc.FacetError, match="invalid_arguments"):
        app.invoke(read["operation"], read["request"], encoded_request_size=65_537)
    assert not app.budget.calls
    assert not fake.calls

    review = ACCEPTED["review.propose_from_facet_query"]
    budget = FacetSessionBudget(calls={"review": 100})
    app = FacetAppAdapter(
        fake,
        authority("facet-read", "search", "review-propose"),
        publication=fake,
        budget=budget,
    )
    with pytest.raises(fc.FacetError, match="limit_exceeded"):
        app.invoke(review["operation"], review["request"])
    assert not fake.calls


def test_invalid_mapping_is_a_closed_error_without_quota_charge() -> None:
    fake = FakeFacetEngine()
    app = adapter(fake, authority("facet-read"))
    with pytest.raises(fc.FacetError, match="invalid_arguments"):
        app.invoke("facet.dimension.list", {"dto_version": object()})
    assert not app.budget.calls
    assert not fake.calls
