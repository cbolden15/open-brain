"""Synthetic C0 wire/protocol checks, without a Brain or production registration."""

from __future__ import annotations

import inspect
import json
from dataclasses import FrozenInstanceError
from hashlib import sha256
from typing import Any

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.facet_contracts import (
    OPERATION_METHODS,
    PUBLICATION_OPERATION,
    REQUEST_TYPES,
    REQUIRED_CAPABILITIES,
    RESPONSE_BYTE_LIMITS,
    RESPONSE_TYPES,
    AssignmentsPatchRequest,
    DimensionCreateRequest,
    DimensionsListRequest,
    FacetError,
    FacetHistorySubject,
    FacetPublicationTasks,
    FacetSelector,
    FacetSubject,
    FacetTasks,
    FacetVaultTasks,
    QueryCreateRequest,
    QueryPageRequest,
    parse_wire,
    request_from_wire,
    response_from_wire,
    response_to_wire,
    semantic_request_sha256,
    semantic_request_value,
    validate_wire,
)
from open_brain_engine.engine.facet_validation import CONTRACT
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

UUID = "00000000-0000-4000-8000-000000000001"
OTHER_UUID = "00000000-0000-4000-8000-000000000002"
DIMENSION = f"facetdim_{UUID}"
VALUE = f"facetval_{UUID}"
OTHER_VALUE = f"facetval_{OTHER_UUID}"
QUERY = f"facetquery_{UUID}"
SOURCE = f"source_{UUID}"
CAPTURE = f"capture_{UUID}"
PAGE = f"page_{UUID}"
REVISION = f"revision_{UUID}"
EVENT = f"facetevent_{UUID}"
TIME = "2026-10-13T12:00:00.000000Z"
SUBJECT = {"kind": "source", "id": SOURCE}
SELECTOR = {"all_value_ids": [VALUE]}
NORMALIZED_SELECTOR = {
    "all_value_ids": [VALUE],
    "space_ids": [],
    "payload_families": [],
    "record_types": ["canonical", "source"],
}
WRITE: dict[str, object] = {"operation_id": "synthetic-operation-1", "expected_version": 1}


def requests() -> dict[str, dict[str, Any]]:
    return {
        "facet.dimension.list": {},
        "facet.dimension.create": {**WRITE, "expected_version": 0, "name": "Project"},
        "facet.dimension.rename": {**WRITE, "dimension_id": DIMENSION, "name": "Projects"},
        "facet.dimension.archive": {**WRITE, "dimension_id": DIMENSION},
        "facet.value.list": {"dimension_id": DIMENSION},
        "facet.value.create": {
            **WRITE,
            "expected_version": 0,
            "dimension_id": DIMENSION,
            "expected_dimension_version": 1,
            "name": "Apollo",
        },
        "facet.value.rename": {**WRITE, "value_id": VALUE, "name": "Apollo II"},
        "facet.value.archive": {**WRITE, "value_id": VALUE},
        "facet.assignment.get": {"record_id": CAPTURE},
        "facet.assignment.patch": {
            **WRITE,
            "record_id": CAPTURE,
            "expected_revision_id": CAPTURE,
            "add_value_ids": [VALUE],
        },
        "facet.query.list": {},
        "facet.query.get": {"query_id": QUERY},
        "facet.query.create": {
            **WRITE,
            "expected_version": 0,
            "name": "Apollo",
            "selector": SELECTOR,
        },
        "facet.query.update": {**WRITE, "query_id": QUERY, "name": "Apollo", "selector": SELECTOR},
        "facet.query.archive": {**WRITE, "query_id": QUERY},
        "facet.query.attach": {
            **WRITE,
            "page_id": PAGE,
            "expected_revision_id": REVISION,
            "query_id": QUERY,
            "expected_query_version": 1,
        },
        "facet.query.attachment.get": {"page_id": PAGE},
        "facet.query.page": {"selector": SELECTOR},
        "facet.history.list": {"subject": SUBJECT},
        "facet.history.state": {"subject": SUBJECT, "as_of_sequence": 0},
        PUBLICATION_OPERATION: {
            "operation_id": "synthetic-review-1",
            "query_id": QUERY,
            "expected_query_version": 1,
            "target_page_id": None,
            "expected_page_revision_id": None,
            "destination_space_id": f"space_{UUID}",
            "title": "Apollo",
            "markdown": "# Apollo\n",
        },
        "facet.vault.preview": {"workspace_id": f"workspace_{UUID}", "record_id": CAPTURE},
        "facet.vault.apply": {"preview_id": "synthetic-preview-1", "operation_id": "apply-1"},
    }


@pytest.mark.parametrize("operation,arguments", requests().items())
def test_every_operation_parses_freezes_and_round_trips(
    operation: str, arguments: dict[str, Any]
) -> None:
    value = {"dto_version": 1, **arguments}
    request = request_from_wire(operation, parse_wire(json.dumps(value)))
    assert type(request) is REQUEST_TYPES[operation]
    assert request.operation == operation
    assert request_from_wire(operation, request.to_wire()) == request
    assert semantic_request_value(operation, request) == {
        "method": operation,
        "arguments": request.to_wire(),
    }
    with pytest.raises(FrozenInstanceError):
        request.dto_version = 2  # type: ignore[misc]


def test_defaults_are_expanded_before_semantic_hashing() -> None:
    compact = {"dto_version": 1, "selector": {"all_value_ids": [OTHER_VALUE, VALUE, VALUE]}}
    expanded = {
        "dto_version": 1,
        "limit": 50,
        "cursor": None,
        "selector": {**NORMALIZED_SELECTOR, "all_value_ids": [VALUE, OTHER_VALUE]},
    }
    assert request_from_wire("facet.query.page", compact).to_wire() == expanded
    assert semantic_request_sha256("facet.query.page", compact) == semantic_request_sha256(
        "facet.query.page", expanded
    )
    assert DimensionsListRequest().to_wire() == {
        "dto_version": 1,
        "limit": 50,
        "cursor": None,
        "include_archived": False,
    }
    assert request_from_wire(
        "facet.history.list", {"dto_version": 1, "subject": SUBJECT}
    ).to_wire() == {
        "dto_version": 1,
        "subject": SUBJECT,
        "limit": 50,
        "cursor": None,
        "as_of_sequence": None,
    }


def test_name_normalization_and_hash_bindings() -> None:
    compact = {
        "dto_version": 1,
        "operation_id": "create-1",
        "expected_version": 0,
        "name": "  Cafe\u0301  ",
    }
    request = request_from_wire("facet.dimension.create", compact)
    assert request.to_wire()["name"] == "Caf\u00e9"
    expected = sha256(
        portable_canonical_json_bytes(
            {
                "method": "facet.dimension.create",
                "arguments": request.to_wire(),
            }
        )
    ).hexdigest()
    assert request.request_sha256() == expected
    changed = {**request.to_wire(), "operation_id": "create-2"}
    assert semantic_request_sha256(request.operation, changed) != expected
    assert compact["name"] == "  Cafe\u0301  "


def test_nested_request_values_are_owned_and_deeply_frozen() -> None:
    selector = {"all_value_ids": [VALUE]}
    request = QueryCreateRequest(
        operation_id="q-1", expected_version=0, name="Apollo", selector=selector
    )
    selector["all_value_ids"].append(OTHER_VALUE)
    assert request.to_wire()["selector"] == NORMALIZED_SELECTOR
    with pytest.raises(TypeError):
        request.selector["all_value_ids"] = [OTHER_VALUE]  # type: ignore[index]
    detached = request.to_wire()
    detached["selector"]["all_value_ids"].append(OTHER_VALUE)
    assert request.to_wire()["selector"] == NORMALIZED_SELECTOR
    typed = FacetSelector(all_value_ids=[OTHER_VALUE, VALUE, VALUE])
    assert typed.all_value_ids == (VALUE, OTHER_VALUE)
    assert QueryPageRequest(selector=typed).to_wire()["selector"] == typed.to_wire()
    assert FacetSubject("source", SOURCE).to_wire() == SUBJECT
    assert FacetHistorySubject("dimension", DIMENSION).kind == "dimension"


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        '{"dto_version":1,"dto_version":1}',
        '{"x":{"nested":1,"nested":1}}',
        '{"dto_version":NaN}',
        '{"dto_version":Infinity}',
        '{"dto_version":-Infinity}',
        '{"dto_version":1.0}',
        '{"dto_version":1e0}',
        '{"dto_version":1E+0}',
        '{"dto_version":9007199254740992}',
        '{"dto_version":-9007199254740992}',
        '{"dto_version":' + "9" * 10_000 + "}",
        "\ud800",
    ],
)
def test_strict_json_parser_rejects_malicious_tokens(raw: str | bytes) -> None:
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        parse_wire(raw)


@pytest.mark.parametrize("value", [True, -1, 1.0, 2**53, "1"])
def test_versions_are_exact_safe_nonnegative_integer_values(value: object) -> None:
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        request_from_wire(
            "facet.assignment.patch",
            {
                "dto_version": 1,
                "operation_id": "p-1",
                "expected_version": value,
                "record_id": CAPTURE,
                "expected_revision_id": CAPTURE,
                "add_value_ids": [VALUE],
            },
        )


@pytest.mark.parametrize("name", ["", " \t ", "x\n", "x\x7f", "x\u0085", "x" * 121, "\ud800"])
def test_names_reject_blank_controls_surrogates_and_overflow(name: str) -> None:
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        DimensionCreateRequest(operation_id="create-1", expected_version=0, name=name)


@pytest.mark.parametrize("extra", ["authority", "owner", "space_ids", "path", "sql"])
def test_trusted_authority_and_scope_never_enter_wire_arguments(extra: str) -> None:
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        request_from_wire("facet.dimension.list", {"dto_version": 1, extra: None})


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"add_value_ids": [], "remove_value_ids": []},
        {"add_value_ids": [VALUE], "remove_value_ids": [VALUE]},
        {"add_value_ids": ["facetval_00000000-0000-1000-8000-000000000001"]},
    ],
)
def test_assignment_patch_rejects_empty_overlap_and_non_v4_facet_ids(patch: dict[str, Any]) -> None:
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        request_from_wire(
            "facet.assignment.patch",
            {
                "dto_version": 1,
                **WRITE,
                "record_id": CAPTURE,
                "expected_revision_id": CAPTURE,
                **patch,
            },
        )


def test_assignment_patch_normalizes_duplicate_ids_and_checks_head_identity() -> None:
    patch = AssignmentsPatchRequest(
        operation_id="p-1",
        expected_version=0,
        record_id=CAPTURE,
        expected_revision_id=CAPTURE,
        add_value_ids=[VALUE, VALUE],
    )
    assert patch.add_value_ids == (VALUE,)
    assert patch.remove_value_ids == ()
    with pytest.raises(FacetError):
        AssignmentsPatchRequest(
            operation_id="p-2",
            expected_version=0,
            record_id=CAPTURE,
            expected_revision_id=REVISION,
            add_value_ids=[VALUE],
        )


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"selector": SELECTOR, "query_id": QUERY, "expected_query_version": 1},
        {"query_id": QUERY},
        {"selector": SELECTOR, "expected_query_version": 1},
        {"selector": None},
        {"selector": {"all_value_ids": []}},
    ],
)
def test_query_page_requires_one_complete_query_form(query: dict[str, Any]) -> None:
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        request_from_wire("facet.query.page", {"dto_version": 1, **query})


def test_saved_query_form_and_publication_create_update_rules() -> None:
    request = QueryPageRequest(query_id=QUERY, expected_query_version=1)
    assert "selector" not in request.to_wire()
    create = {"dto_version": 1, **requests()[PUBLICATION_OPERATION]}
    assert request_from_wire(PUBLICATION_OPERATION, create).to_wire() == create
    update = {
        **create,
        "target_page_id": PAGE,
        "expected_page_revision_id": REVISION,
        "destination_space_id": None,
    }
    assert request_from_wire(PUBLICATION_OPERATION, update).to_wire() == update
    for bad in [
        {**create, "expected_page_revision_id": REVISION},
        {**update, "destination_space_id": f"space_{UUID}"},
        {**create, "markdown": "é" * 32769},
    ]:
        with pytest.raises(FacetError, match="^invalid_arguments$"):
            request_from_wire(PUBLICATION_OPERATION, bad)


def test_parser_byte_bounds_include_raw_whitespace_and_publication_exception() -> None:
    ordinary = " " * 65536 + "{}"
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        parse_wire(ordinary)
    assert parse_wire(ordinary, operation=PUBLICATION_OPERATION) == {}
    with pytest.raises(FacetError):
        parse_wire(" " * 1048576 + "{}", operation=PUBLICATION_OPERATION)
    with pytest.raises(FacetError):
        parse_wire("é" * 32769)


def test_response_immutability_required_nulls_and_operation_binding() -> None:
    value = {"status": "ok", "dto_version": 1, "items": [], "next_cursor": None, "complete": True}
    response = response_from_wire("facet.dimension.list", value)
    value["items"].append({"malicious": True})  # type: ignore[attr-defined]
    assert response.to_wire()["items"] == []
    with pytest.raises(FrozenInstanceError):
        response.encoded = "{}"  # type: ignore[misc]
    with pytest.raises((FrozenInstanceError, TypeError)):
        response.operation = "facet.value.list"  # type: ignore[misc]
    with pytest.raises(FacetError):
        response_to_wire("facet.value.list", response)
    with pytest.raises(FacetError):
        request_from_wire("facet.query.list", DimensionsListRequest())
    with pytest.raises(FacetError):
        response_from_wire(
            "facet.dimension.list",
            {
                "status": "ok",
                "dto_version": 1,
                "items": [],
                "complete": True,
            },
        )
    with pytest.raises(FacetError):
        response_from_wire(
            "facet.dimension.list",
            {
                "status": "ok",
                "dto_version": 1,
                "items": [],
                "complete": False,
                "next_cursor": None,
            },
        )


def test_mutation_receipt_applied_and_unchanged_semantics() -> None:
    applied = {
        "status": "applied",
        "dto_version": 1,
        "operation_id": "p-1",
        "subject_id": SOURCE,
        "version": 1,
        "event_id": EVENT,
    }
    unchanged = {**applied, "status": "unchanged", "event_id": None}
    assert response_to_wire("facet.assignment.patch", applied) == applied
    assert response_to_wire("facet.assignment.patch", unchanged) == unchanged
    with pytest.raises(FacetError):
        response_to_wire("facet.assignment.patch", {**applied, "event_id": None})
    with pytest.raises(FacetError):
        response_to_wire("facet.assignment.patch", {**unchanged, "event_id": EVENT})
    with pytest.raises(FacetError):
        response_to_wire("facet.assignment.patch", {**applied, "replayed": True})


def test_history_uses_projected_closed_rows_with_matching_identity() -> None:
    row = {
        "dimension_id": DIMENSION,
        "name": "Project",
        "archived": False,
        "version": 1,
        "created_at": TIME,
        "updated_at": TIME,
    }
    event = {
        "event_id": EVENT,
        "sequence": 1,
        "recorded_at": TIME,
        "actor_id": "synthetic-owner",
        "kind": "dimension.create",
        "changes": [
            {"entity": {"kind": "dimension", "id": DIMENSION}, "before": None, "after": row},
        ],
    }
    envelope = {
        "status": "ok",
        "dto_version": 1,
        "items": [event],
        "next_cursor": None,
        "complete": True,
    }
    assert response_to_wire("facet.history.list", envelope) == envelope
    row["name_key"] = "project"
    with pytest.raises(FacetError):
        response_to_wire("facet.history.list", envelope)
    del row["name_key"]
    row["dimension_id"] = f"facetdim_{OTHER_UUID}"
    with pytest.raises(FacetError):
        response_to_wire("facet.history.list", envelope)
    state = {
        "status": "ok",
        "dto_version": 1,
        "as_of_sequence": 0,
        "item": {"subject": SUBJECT, "version": 0, "memberships": [], "attachment": None},
    }
    assert response_to_wire("facet.history.state", state) == state
    assert (
        response_to_wire(
            "facet.query.attachment.get",
            {
                "status": "ok",
                "dto_version": 1,
                "item": None,
            },
        )["item"]
        is None
    )


def test_schema_public_maps_and_protocol_signatures_do_not_drift() -> None:
    assert set(requests()) == set(REQUEST_TYPES) == set(RESPONSE_TYPES) == set(OPERATION_METHODS)
    assert set(REQUEST_TYPES) == set(REQUIRED_CAPABILITIES) == set(RESPONSE_BYTE_LIMITS)
    for operation, metadata in CONTRACT["operations"].items():
        assert REQUEST_TYPES[operation].__name__ == metadata["request"]
        assert RESPONSE_TYPES[operation].__name__ == metadata["response"]
        assert OPERATION_METHODS[operation] == metadata["method"]
        assert REQUIRED_CAPABILITIES[operation] == tuple(CONTRACT["grants"][operation])
        protocol = (
            FacetPublicationTasks
            if operation == PUBLICATION_OPERATION
            else FacetVaultTasks
            if operation.startswith("facet.vault.")
            else FacetTasks
        )
        signature = inspect.signature(getattr(protocol, metadata["method"]))
        assert list(signature.parameters) == ["self", "request", "authority"]
        assert signature.parameters["authority"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["authority"].annotation == "EffectiveAuthority"
    authority = EffectiveAuthority(
        principal_id="synthetic-owner",
        session_id="s-1",
        capabilities=frozenset(),
        space_ids=None,
        owner=True,
    )
    assert authority.owner
    assert "facet-read" not in authority.capabilities
    for code in CONTRACT["definitions"]["error"]["properties"]["code"]["enum"]:
        assert validate_wire("error", FacetError(code).to_wire())["code"] == code
    with pytest.raises(ValueError, match="invalid error code"):
        FacetError("raw_private_path")


def test_receipts_bind_the_operation_affected_entity_and_selectors_are_normalized() -> None:
    receipt = {
        "status": "applied",
        "dto_version": 1,
        "operation_id": "create-1",
        "subject_id": QUERY,
        "version": 1,
        "event_id": EVENT,
    }
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        response_from_wire("facet.dimension.create", receipt)
    saved_query = {
        "query_id": QUERY,
        "name": "Apollo",
        "selector": {
            **NORMALIZED_SELECTOR,
            "all_value_ids": [OTHER_VALUE, VALUE],
        },
        "archived": False,
        "version": 1,
        "selection_version": 1,
    }
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        response_from_wire(
            "facet.query.get", {"dto_version": 1, "status": "ok", "item": saved_query}
        )
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        request_from_wire("facet.dimension.list", parse_wire("[" * 2000 + "]" * 2000))


# Reusable producer/adapter examples: coordinator freezes these values as fixture bytes.
REQUEST_EXAMPLES: dict[str, dict[str, Any]] = {
    operation: {"dto_version": 1, **arguments} for operation, arguments in requests().items()
}
_DIMENSION = {"dimension_id": DIMENSION, "name": "Project", "archived": False, "version": 1}
_VALUE = {
    "value_id": VALUE,
    "dimension_id": DIMENSION,
    "name": "Apollo",
    "archived": False,
    "effective_archived": False,
    "version": 1,
}
_QUERY = {
    "query_id": QUERY,
    "name": "Apollo",
    "selector": NORMALIZED_SELECTOR,
    "archived": False,
    "version": 1,
    "selection_version": 1,
}
_ATTACHMENT = {"page_id": PAGE, "query_id": QUERY, "version": 1}
_MEMBERSHIP = {"value_id": VALUE, "membership_version": 1}
_SUMMARY = {
    "record_id": CAPTURE,
    "record_type": "source",
    "revision_id": CAPTURE,
    "source_id": SOURCE,
    "payload_family": "text",
    "space_id": None,
    "title": "Apollo",
    "excerpt": "Synthetic source evidence.",
    "trust": "owner",
    "provenance": {
        "representative_capture_id": CAPTURE,
        "capture_ids": [CAPTURE],
        "source_origin": "owner_authored",
    },
    "source_update_available": False,
}
_HISTORY_ENTRY = {
    "event_id": EVENT,
    "sequence": 1,
    "recorded_at": TIME,
    "actor_id": "synthetic-owner",
    "kind": "assignment.patch",
    "changes": [
        {
            "entity": {"kind": "subject", "id": SUBJECT},
            "before": None,
            "after": {"subject_kind": "source", "subject_id": SOURCE, "version": 1},
        },
        {
            "entity": {
                "kind": "assignment",
                "id": {"subject_kind": "source", "subject_id": SOURCE, "value_id": VALUE},
            },
            "before": None,
            "after": {
                "subject_kind": "source",
                "subject_id": SOURCE,
                "value_id": VALUE,
                "present": True,
                "membership_version": 1,
                "last_event_sequence": 1,
            },
        },
    ],
}
_RECEIPT = {
    "status": "applied",
    "operation_id": "synthetic-operation-1",
    "subject_id": SOURCE,
    "version": 2,
    "event_id": EVENT,
}
RESPONSE_EXAMPLES: dict[str, dict[str, Any]] = {
    "facet.dimension.list": {
        "status": "ok",
        "dto_version": 1,
        "items": [_DIMENSION],
        "next_cursor": None,
        "complete": True,
    },
    "facet.dimension.create": {**_RECEIPT, "dto_version": 1, "subject_id": DIMENSION, "version": 1},
    "facet.dimension.rename": {**_RECEIPT, "dto_version": 1, "subject_id": DIMENSION},
    "facet.dimension.archive": {**_RECEIPT, "dto_version": 1, "subject_id": DIMENSION},
    "facet.value.list": {
        "status": "ok",
        "dto_version": 1,
        "items": [_VALUE],
        "next_cursor": None,
        "complete": True,
    },
    "facet.value.create": {**_RECEIPT, "dto_version": 1, "subject_id": VALUE, "version": 1},
    "facet.value.rename": {**_RECEIPT, "dto_version": 1, "subject_id": VALUE},
    "facet.value.archive": {**_RECEIPT, "dto_version": 1, "subject_id": VALUE},
    "facet.assignment.get": {
        "status": "ok",
        "dto_version": 1,
        "item": {
            "subject": SUBJECT,
            "record_id": CAPTURE,
            "revision_id": CAPTURE,
            "version": 1,
            "memberships": [_MEMBERSHIP],
        },
    },
    "facet.assignment.patch": {**_RECEIPT, "dto_version": 1},
    "facet.query.list": {
        "status": "ok",
        "dto_version": 1,
        "items": [_QUERY],
        "next_cursor": None,
        "complete": True,
    },
    "facet.query.get": {"status": "ok", "dto_version": 1, "item": _QUERY},
    "facet.query.create": {**_RECEIPT, "dto_version": 1, "subject_id": QUERY, "version": 1},
    "facet.query.update": {**_RECEIPT, "dto_version": 1, "subject_id": QUERY},
    "facet.query.archive": {**_RECEIPT, "dto_version": 1, "subject_id": QUERY},
    "facet.query.attach": {**_RECEIPT, "dto_version": 1, "subject_id": PAGE},
    "facet.query.attachment.get": {"status": "ok", "dto_version": 1, "item": _ATTACHMENT},
    "facet.query.page": {
        "status": "ok",
        "dto_version": 1,
        "items": [{"subject": SUBJECT, "record": _SUMMARY, "classification_version": 1}],
        "next_cursor": None,
        "complete": True,
    },
    "facet.history.list": {
        "status": "ok",
        "dto_version": 1,
        "items": [_HISTORY_ENTRY],
        "next_cursor": None,
        "complete": True,
    },
    "facet.history.state": {
        "status": "ok",
        "dto_version": 1,
        "as_of_sequence": 0,
        "item": {"subject": SUBJECT, "version": 0, "memberships": [], "attachment": None},
    },
    PUBLICATION_OPERATION: {
        "status": "proposed",
        "dto_version": 1,
        "effective_idempotency_key": "synthetic-review-1",
        "proposal_id": f"proposal_{UUID}",
        "page_id": PAGE,
        "space_id": f"space_{UUID}",
        "target_page_id": None,
        "operation": "create",
        "proposal_status": "pending",
    },
    "facet.vault.preview": {
        "status": "ok",
        "dto_version": 1,
        "preview_id": "synthetic-preview-1",
        "subject": SUBJECT,
        "expected_version": 1,
        "add_value_ids": [VALUE],
        "remove_value_ids": [],
        "conflicts": [],
        "expires_at": "2026-10-13T12:15:00.000000Z",
    },
    "facet.vault.apply": {
        "status": "ok",
        "dto_version": 1,
        "receipt": {**_RECEIPT, "operation_id": "apply-1"},
        "projection_status": "current",
    },
}


@pytest.mark.parametrize("operation,response", RESPONSE_EXAMPLES.items())
def test_reusable_response_examples_validate_and_round_trip(
    operation: str, response: dict[str, Any]
) -> None:
    parsed = response_from_wire(operation, response)
    assert type(parsed) is RESPONSE_TYPES[operation]
    assert parsed.to_wire() == response
    assert portable_canonical_json_bytes(parsed.to_wire()).decode() == parsed.encoded


def test_reusable_example_maps_cover_every_public_boundary() -> None:
    assert set(REQUEST_EXAMPLES) == set(RESPONSE_EXAMPLES) == set(OPERATION_METHODS)


def test_encoded_input_bound_precedes_duplicate_normalization() -> None:
    oversized: dict[str, Any] = {"dto_version": 1, "selector": {"all_value_ids": [VALUE] * 2000}}
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        request_from_wire("facet.query.page", oversized)
    with pytest.raises(FacetError, match="^invalid_arguments$"):
        QueryPageRequest(selector=oversized["selector"])
