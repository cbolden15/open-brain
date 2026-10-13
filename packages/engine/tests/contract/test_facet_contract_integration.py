"""Cross-check the independently authored wire and evidence contracts."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.facet_evidence import canonical_evidence_bytes, request_sha256

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/facets"
OPERATIONS = json.loads((FIXTURES / "history/facets/operations-v1.json").read_bytes())["operations"]
EVENTS = json.loads((FIXTURES / "history/facets/events-v1.json").read_bytes())["events"]
WIRE_CASES = json.loads((FIXTURES / "wire-cases.json").read_bytes())["cases"]


@pytest.mark.parametrize("case", WIRE_CASES, ids=lambda item: item["id"])
def test_shared_wire_examples_have_exact_canonical_round_trips(case: dict[str, Any]) -> None:
    operation = case["operation"]
    if case["outcome"] == "reject":
        with pytest.raises(fc.FacetError) as caught:
            fc.request_from_wire(operation, fc.parse_wire(case["raw_request"], operation=operation))
        assert caught.value.code == case["error_code"]
        return
    raw = canonical_evidence_bytes(case["request"])
    request = fc.request_from_wire(operation, fc.parse_wire(raw, operation=operation))
    assert canonical_evidence_bytes(request.to_wire()) == raw
    response = fc.response_from_wire(operation, case["response"])
    assert canonical_evidence_bytes(response.to_wire()) == canonical_evidence_bytes(
        case["response"]
    )


def test_shared_wire_manifest_hashes_and_operation_coverage() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    for entry in manifest["wire_fixtures"]:
        raw = (FIXTURES / entry["path"]).read_bytes()
        assert sha256(raw).hexdigest() == entry["sha256"]
        assert canonical_evidence_bytes(json.loads(raw)) == raw
    assert {case["operation"] for case in WIRE_CASES if case["outcome"] == "accept"} == set(
        fc.OPERATION_METHODS
    )


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda item: item["operation_id"])
def test_semantic_operations_share_wire_requests_receipts_and_hashes(
    operation: dict[str, Any],
) -> None:
    semantic = operation["request_json"]
    method, arguments = semantic["method"], semantic["arguments"]
    assert request_sha256(method, arguments) == operation["request_sha256"]
    if method == "facet.query.attach.publication":
        # Only the publication stage machine can admit this semantic request.
        with pytest.raises(fc.FacetError):
            fc.request_from_wire(method, arguments)
        return
    request = fc.request_from_wire(method, arguments)
    assert canonical_evidence_bytes(request.to_wire()) == canonical_evidence_bytes(arguments)
    assert fc.semantic_request_sha256(method, request) == operation["request_sha256"]
    response = fc.response_from_wire(method, operation["receipt_json"])
    assert canonical_evidence_bytes(response.to_wire()) == canonical_evidence_bytes(
        operation["receipt_json"]
    )


def test_authorized_history_projection_uses_the_same_event_vocabulary() -> None:
    fields = {"event_id", "sequence", "recorded_at", "actor_id", "kind", "changes"}
    projection = [{key: value for key, value in event.items() if key in fields} for event in EVENTS]
    payload = {
        "status": "ok",
        "dto_version": 1,
        "items": projection,
        "next_cursor": None,
        "complete": True,
    }
    result = fc.response_to_wire("facet.history.list", payload)
    assert canonical_evidence_bytes(result) == canonical_evidence_bytes(payload)
