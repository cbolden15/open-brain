"""CLI/MCP helper preparation checks; real handler wiring waits for E and P."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.services.facets import (
    FacetAppAdapter,
    add_facet_parsers,
    facet_cli_from_namespace,
    read_facet_selector_file,
)
from open_brain.services.mcp_protocol import encoded_tool_response_size
from packages.app.tests.facet_fake import FakeFacetEngine

WIRE_CASES = Path(__file__).resolve().parents[4] / "engine/tests/fixtures/facets/wire-cases.json"
CASES = json.loads(WIRE_CASES.read_text())["cases"]


def _case(operation: str) -> dict[str, Any]:
    return cast(dict[str, Any], next(case for case in CASES if case["operation"] == operation))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    add_facet_parsers(parser.add_subparsers(dest="command", required=True))
    return parser


def test_cli_create_and_selector_file_use_closed_engine_requests() -> None:
    parser = _parser()
    parsed = parser.parse_args(
        [
            "facet",
            "dimension",
            "create",
            "Project",
            "--expected-version",
            "0",
            "--operation-id",
            "cli-one",
        ]
    )
    operation, request = facet_cli_from_namespace(parsed)
    assert operation == "facet.dimension.create"
    assert request["operation_id"] == "cli-one"
    assert request["name"] == "Project"

    selector = b'{"all_value_ids":["facetval_00000000-0000-4000-8000-000000000001"]}'
    parsed = parser.parse_args(["facet", "query", "run", "--selector-file", "-"])
    operation, request = facet_cli_from_namespace(parsed, selector_text=selector)
    assert operation == "facet.query.page"
    assert cast(dict[str, object], request["selector"])["record_types"] == ["canonical", "source"]
    with pytest.raises(fc.FacetError, match="invalid_arguments"):
        facet_cli_from_namespace(parsed, selector_text=b'{"owner":true}')


def test_selector_file_is_bounded_and_vault_preview_uses_named_record_flag(
    tmp_path: Path,
) -> None:
    selector = b'{"all_value_ids":["facetval_00000000-0000-4000-8000-000000000001"]}'
    path = tmp_path / "synthetic-selector.json"
    path.write_bytes(selector)
    assert read_facet_selector_file(str(path)) == selector
    assert read_facet_selector_file("-", stdin=io.BytesIO(selector)) == selector
    path.write_bytes(b" " * (fc.MAX_REQUEST_BYTES + 1))
    with pytest.raises(fc.FacetError, match="invalid_arguments"):
        read_facet_selector_file(str(path))
    parsed = _parser().parse_args(
        ["facet", "vault", "preview", "--workspace-id", "workspace-1", "--record-id", "record-1"]
    )
    assert parsed.record_id == "record-1"


def test_direct_mcp_call_rejects_without_grant_then_charges_wrapped_receipt() -> None:
    fake = FakeFacetEngine()
    case = _case("facet.dimension.create")
    ungranted = FacetAppAdapter(fake, EffectiveAuthority("synthetic", "session", frozenset(), None))
    with pytest.raises(fc.FacetError, match="unsupported_capability"):
        ungranted.invoke_mcp("brain_facet_dimension_create", case["request"])
    assert not fake.calls

    fake.configure(case["operation"], case["response"])
    granted = FacetAppAdapter(
        fake, EffectiveAuthority("synthetic", "session", frozenset({"facet-catalog-write"}), None)
    )
    assert "brain_facet_dimension_create" in {row["name"] for row in granted.tool_definitions()}
    result = granted.invoke_mcp(
        "brain_facet_dimension_create",
        case["request"],
        encoded_size=lambda value: encoded_tool_response_size(7, value),
    )
    assert result == case["response"]
    assert granted.budget.response_bytes["write"] == encoded_tool_response_size(7, result)
    assert len(fake.calls) == 1


def test_owner_vault_is_cli_only() -> None:
    fake = FakeFacetEngine()
    case = _case("facet.vault.preview")
    fake.configure(case["operation"], case["response"])
    app = FacetAppAdapter(
        fake,
        EffectiveAuthority("synthetic", "session", frozenset(), None, owner=True),
        vault=fake,
    )
    assert "facet.vault.preview" in app.available_operations(transport="cli")
    assert "facet.vault.preview" not in app.available_operations()
    with pytest.raises(fc.FacetError, match="unsupported_capability"):
        app.invoke_mcp("brain_facet_vault_preview", case["request"])
    assert not fake.calls
    assert app.invoke(case["operation"], case["request"], transport="cli") == case["response"]
