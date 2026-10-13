"""C0 grammar/fixture assertions; no claim of runtime facet acceptance."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from open_brain_engine.engine.facet_evidence import (
    EVIDENCE_SCHEMA,
    SIDECAR_PATHS,
    FacetEvidenceError,
    canonical_evidence_bytes,
    request_sha256,
    validate_evidence,
    validate_portable_facet_sidecars,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/facets"
REPO = FIXTURES.parents[4]
MANIFEST: dict[str, Any] = json.loads((FIXTURES / "manifest.json").read_text())


def _read(path: str) -> Any:
    return json.loads((FIXTURES / path).read_bytes())


def _assert_fixture(schema: str, value: Any) -> Any:
    validate_evidence(schema, value)
    if schema == "portable_assertion":
        return validate_portable_facet_sidecars(
            value["events"], value["operations"], value["state"],
        )
    return value


@pytest.mark.parametrize("fixture", MANIFEST["fixtures"], ids=lambda row: row["path"])
def test_golden_fixture_outcomes_and_canonical_bytes(fixture: dict[str, Any]) -> None:
    raw = (FIXTURES / fixture["path"]).read_bytes()
    value = json.loads(raw)
    assert sha256(raw).hexdigest() == fixture["sha256"]
    assert canonical_evidence_bytes(value) == raw
    if fixture["outcome"] == "accept":
        _assert_fixture(fixture["schema"], value)
        grammar = {**EVIDENCE_SCHEMA, "$ref": "#/$defs/" + fixture["schema"]}
        Draft202012Validator(grammar).validate(value)
    else:
        with pytest.raises(FacetEvidenceError) as error:
            _assert_fixture(fixture["schema"], value)
        assert error.value.code == fixture["error_code"]


def test_draft_schema_references_and_manifest_ownership() -> None:
    Draft202012Validator.check_schema(EVIDENCE_SCHEMA)
    for name in EVIDENCE_SCHEMA["$defs"]:
        # Resolving every ref catches misspelled refs even in currently empty sidecars.
        def walk(rule: Any) -> None:
            if isinstance(rule, dict):
                if "$ref" in rule:
                    assert rule["$ref"].startswith("#/$defs/")
                    assert rule["$ref"].removeprefix("#/$defs/") in EVIDENCE_SCHEMA["$defs"]
                for child in rule.values():
                    walk(child)
            elif isinstance(rule, list):
                for child in rule:
                    walk(child)
        walk(EVIDENCE_SCHEMA["$defs"][name])
    assert MANIFEST["contract_revision"] == EVIDENCE_SCHEMA["x-contract-revision"]
    assert MANIFEST["status"] == "draft-unregistered"
    for path, digest in MANIFEST["schemas"].items():
        assert sha256((REPO / path).read_bytes()).hexdigest() == digest
    assert set(MANIFEST["acceptance_ownership"]) == {f"F{i:02d}" for i in range(1, 23)}
    for task in MANIFEST["acceptance_ownership"].values():
        assert task["producer"] in {"E", "A", "P", "X", "V", "I"}
        assert task["independent_test_owner"] in {"T", "I"}
        assert task["inputs"] and task["declared_outputs"] and task["verification_command"]
    assert set(SIDECAR_PATHS.values()) == {
        "history/facets/events-v1.json", "history/facets/operations-v1.json",
        "content/facets/state-v1.json",
    }


def test_retry_noop_and_remove_restore_assertions() -> None:
    kit = _read("positive/portable-replay.json")
    original = _read("positive/retry-original-receipt.json")
    retained = next(operation for operation in kit["operations"]["operations"]
                    if operation["operation_id"] == original["operation_id"])
    assert canonical_evidence_bytes(original) == canonical_evidence_bytes(retained)
    assert original["receipt_json"]["version"] == 1
    assert kit["state"]["subjects"][0]["version"] == 4
    assert kit["state"]["assignments"][0]["membership_version"] == 3
    assert kit["state"]["assignments"][0]["present"] is True
    noop = _read("positive/archived-existing-membership-noop.json")
    assert noop["event_id"] is None
    assert noop["receipt_json"]["version"] == kit["state"]["subjects"][0]["version"]
    assert not any(event["operation_id"] == noop["operation_id"]
                   for event in kit["events"]["events"])
    assert kit["state"]["dimensions"][0]["archived"] is True
    assert len(kit["state"]["assignments"]) == 2
    rename = _read("positive/query-display-rename.json")["changes"][0]
    assert rename["after"]["selection_version"] == rename["before"]["selection_version"]
    update = _read("positive/query-selector-update.json")["changes"][0]
    assert update["after"]["selection_version"] == update["before"]["selection_version"] + 1


@pytest.mark.parametrize("field,mutation", [
    ("name", "Tampered request name"), ("expected_version", 1),
])
def test_rehashed_operation_arguments_must_match_event(field: str, mutation: object) -> None:
    kit = _read("positive/portable-replay.json")
    operation = next(row for row in kit["operations"]["operations"]
                     if row["request_json"]["method"] == "facet.dimension.create")
    arguments = operation["request_json"]["arguments"]
    arguments[field] = mutation
    if field == "expected_version":
        # A create expectation is structurally invalid before replay admission.
        with pytest.raises(FacetEvidenceError):
            request_sha256(operation["request_json"]["method"], arguments)
        return
    operation["request_sha256"] = request_sha256(operation["request_json"]["method"], arguments)
    with pytest.raises(FacetEvidenceError, match="operation_event_mismatch"):
        validate_portable_facet_sidecars(kit["events"], kit["operations"], kit["state"])


def test_binding_uses_explicit_destination_and_selected_memberships_only() -> None:
    binding = _read("positive/review-create-cross-space.json")
    assert binding["source_states"][0]["space_id"] is None
    assert binding["source_states"][1]["space_id"] != binding["destination_space_id"]
    validate_evidence("review_binding_v5", binding)
    bad = deepcopy(binding)
    bad["facet_source_states"][0]["memberships"].append({
        "value_id": "facetval_00000000-0000-4000-8000-000000000999", "membership_version": 1,
    })
    with pytest.raises(FacetEvidenceError):
        validate_evidence("review_binding_v5", bad)
    for field in ("retrieval_generation", "dimension_name", "workspace_id"):
        bad = {**binding, field: "synthetic"}
        with pytest.raises(FacetEvidenceError):
            validate_evidence("review_binding_v5", bad)


def test_primitive_boundaries_and_empty_ledger() -> None:
    for schema, value in [("operation_id", " \t"), ("name", " Name "),
                          ("name", "e\u0301"), ("facetevent_id", "facetevent_invalid"),
                          ("timestamp", "2026-02-30T12:00:00Z")]:
        with pytest.raises(FacetEvidenceError):
            validate_evidence(schema, value)
    kit = _read("positive/portable-replay.json")
    empty_events = {**kit["events"], "events": []}
    empty_operations = {**kit["operations"], "operations": []}
    empty_state = {key: [] if isinstance(value, list) else value
                   for key, value in kit["state"].items()}
    projected = validate_portable_facet_sidecars(empty_events, empty_operations, empty_state)
    assert projected == empty_state
    with pytest.raises(FacetEvidenceError, match="unknown_evidence_schema"):
        validate_evidence("unknown", {})
