"""Independent C0 grammar checks derived from the accepted facet specification.

These tests prove contract admission, not persistence or authorization effects.
"""

from __future__ import annotations

import inspect
import json
from copy import deepcopy
from typing import Any, cast, get_type_hints

import pytest
from open_brain_engine.engine.facet_contracts import (
    OPERATION_METHODS,
    REQUEST_TYPES,
    RESPONSE_TYPES,
    FacetError,
    parse_wire,
    request_from_wire,
    response_from_wire,
    response_to_wire,
    semantic_request_sha256,
)

DIMENSION = "facetdim_00000000-0000-4000-8000-000000000001"
VALUE_A = "facetval_00000000-0000-4000-8000-000000000002"
VALUE_B = "facetval_00000000-0000-4000-8000-000000000003"
QUERY = "facetquery_00000000-0000-4000-8000-000000000004"
EVENT = "facetevent_00000000-0000-4000-8000-000000000005"
CAPTURE = "capture_00000000-0000-4000-8000-000000000006"
SOURCE = "source_00000000-0000-4000-8000-000000000007"
PAGE = "page_00000000-0000-4000-8000-000000000008"
SPACE = "space_00000000-0000-4000-8000-000000000009"


def _create(**overrides: Any) -> dict[str, Any]:
    return {
        "dto_version": 1,
        "operation_id": "synthetic-create",
        "expected_version": 0,
        "name": "Project",
        **overrides,
    }


def _selector(**overrides: Any) -> dict[str, Any]:
    return {"all_value_ids": [VALUE_A], **overrides}


def _patch(**overrides: Any) -> dict[str, Any]:
    return {
        "dto_version": 1,
        "operation_id": "synthetic-patch",
        "expected_version": 0,
        "record_id": CAPTURE,
        "expected_revision_id": CAPTURE,
        "add_value_ids": [VALUE_A],
        "remove_value_ids": [],
        **overrides,
    }


def _proposal(**overrides: Any) -> dict[str, Any]:
    return {
        "dto_version": 1,
        "operation_id": "synthetic-proposal",
        "query_id": QUERY,
        "expected_query_version": 1,
        "target_page_id": None,
        "expected_page_revision_id": None,
        "destination_space_id": SPACE,
        "title": "Synthetic page",
        "markdown": "Synthetic content.",
        **overrides,
    }


def _receipt(**overrides: Any) -> dict[str, Any]:
    return {
        "dto_version": 1,
        "status": "applied",
        "operation_id": "synthetic-create",
        "subject_id": DIMENSION,
        "version": 1,
        "event_id": EVENT,
        **overrides,
    }


def _reject_request(operation: str, body: dict[str, Any]) -> None:
    with pytest.raises(FacetError) as error:
        request_from_wire(operation, body)
    assert error.value.code in {"invalid_arguments", "limit_exceeded"}


@pytest.mark.parametrize(
    "token",
    [
        "true",
        "false",
        "1.0",
        "1e0",
        "1E+0",
        "-1",
        "9007199254740992",
        "NaN",
        "Infinity",
        "-Infinity",
    ],
)
def test_raw_integer_token_admission(token: str) -> None:
    raw = '{"dto_version":1,"limit":' + token + "}"
    with pytest.raises(FacetError):
        request_from_wire("facet.dimension.list", parse_wire(raw))


@pytest.mark.parametrize(
    "raw",
    [
        '{"dto_version":1,"dto_version":1}',
        '{"dto_version":1,"selector":{"all_value_ids":[],"all_value_ids":[]}}',
        '{"dto_version":1,"selector":{"all_value_ids":[],"space_ids":[{"x":1,"x":2}]}}',
        b'{"dto_version":1,"name":"\xff"}',
    ],
)
def test_json_rejects_duplicate_keys_at_every_depth_and_invalid_unicode(raw: str | bytes) -> None:
    with pytest.raises(FacetError):
        parse_wire(raw)


def test_request_admission_rejects_json_escaped_surrogates() -> None:
    raw = (
        '{"dto_version":1,"operation_id":"synthetic-create","expected_version":0,"name":"\\ud800"}'
    )
    with pytest.raises(FacetError):
        request_from_wire("facet.dimension.create", parse_wire(raw))


@pytest.mark.parametrize(
    "field", ["authority", "owner", "path", "sql", "space_scope", "session_id", "request_id"]
)
def test_request_cannot_accept_transport_or_authority_fields(field: str) -> None:
    _reject_request("facet.dimension.create", _create(**{field: "injected"}))


@pytest.mark.parametrize("name", ["", "   ", "x" * 121, "A\x00B", "A\x7fB", "A\x85B"])
def test_catalogue_names_reject_blank_long_and_control_characters(name: str) -> None:
    _reject_request("facet.dimension.create", _create(name=name))


def test_name_normalization_precedes_semantic_hashing_without_casefolding_display() -> None:
    first = request_from_wire("facet.dimension.create", _create(name="  Cafe\u0301  "))
    second = request_from_wire("facet.dimension.create", _create(name="Caf\u00e9"))
    assert first.to_wire()["name"] == "Caf\u00e9"
    assert semantic_request_sha256("facet.dimension.create", first) == semantic_request_sha256(
        "facet.dimension.create", second
    )
    lower = request_from_wire("facet.dimension.create", _create(name="caf\u00e9"))
    assert lower.to_wire()["name"] == "caf\u00e9"
    assert semantic_request_sha256("facet.dimension.create", first) != semantic_request_sha256(
        "facet.dimension.create", lower
    )


@pytest.mark.parametrize("operation_id", ["", "  ", "x" * 129, "a\x00b", "a\x7fb"])
def test_operation_ids_are_bounded_nonblank_and_control_free(operation_id: str) -> None:
    _reject_request("facet.dimension.create", _create(operation_id=operation_id))


@pytest.mark.parametrize(
    "bad_id",
    [
        "facetdim_00000000-0000-1000-8000-000000000001",
        "facetdim_00000000-0000-4000-0000-000000000001",
        "facetdim_00000000-0000-4000-c000-000000000001",
        "facetdim_00000000-0000-4000-8000-00000000000g",
        VALUE_A,
    ],
)
def test_new_facet_ids_require_correct_prefix_uuid_version_and_variant(bad_id: str) -> None:
    _reject_request(
        "facet.dimension.archive",
        {
            "dto_version": 1,
            "operation_id": "synthetic-archive",
            "expected_version": 1,
            "dimension_id": bad_id,
        },
    )


@pytest.mark.parametrize("expected_version", [True, 0.0, -1, 1, 2**53])
def test_create_requires_integer_zero_expected_version(expected_version: object) -> None:
    _reject_request("facet.dimension.create", _create(expected_version=expected_version))


def test_request_defaults_and_selector_set_normalization_are_hash_stable() -> None:
    sparse = {"dto_version": 1, "selector": _selector(all_value_ids=[VALUE_B, VALUE_A, VALUE_A])}
    explicit = {
        "dto_version": 1,
        "limit": 50,
        "cursor": None,
        "selector": {
            "all_value_ids": [VALUE_A, VALUE_B],
            "space_ids": [],
            "payload_families": [],
            "record_types": ["canonical", "source"],
        },
    }
    first = request_from_wire("facet.query.page", sparse)
    second = request_from_wire("facet.query.page", explicit)
    assert first.to_wire() == second.to_wire()
    assert semantic_request_sha256("facet.query.page", first) == semantic_request_sha256(
        "facet.query.page", second
    )


def test_selector_limit_counts_distinct_values_after_normalization() -> None:
    request = request_from_wire(
        "facet.query.page",
        {
            "dto_version": 1,
            "selector": _selector(all_value_ids=[VALUE_A] * 40),
        },
    )
    assert request.to_wire()["selector"]["all_value_ids"] == [VALUE_A]
    ids = [f"facetval_00000000-0000-4000-8000-{number:012x}" for number in range(1, 34)]
    _reject_request(
        "facet.query.page", {"dto_version": 1, "selector": _selector(all_value_ids=ids)}
    )


@pytest.mark.parametrize(
    "body",
    [
        {"dto_version": 1},
        {"dto_version": 1, "selector": _selector(all_value_ids=[])},
        {"dto_version": 1, "query_id": QUERY},
        {"dto_version": 1, "expected_query_version": 1},
        {"dto_version": 1, "selector": _selector(), "query_id": QUERY, "expected_query_version": 1},
        {"dto_version": 1, "selector": _selector(), "expected_query_version": 1},
        {"dto_version": 1, "query_id": QUERY, "expected_query_version": 0},
    ],
)
def test_query_page_requires_exactly_one_complete_query_form(body: dict[str, Any]) -> None:
    _reject_request("facet.query.page", body)


def test_requests_detach_and_freeze_nested_selector_and_emitted_values() -> None:
    selector = _selector()
    request = request_from_wire("facet.query.page", {"dto_version": 1, "selector": selector})
    selector["all_value_ids"].append(VALUE_B)
    assert request.to_wire()["selector"]["all_value_ids"] == [VALUE_A]
    emitted = request.to_wire()
    emitted["selector"]["all_value_ids"].append(VALUE_B)
    assert request.to_wire()["selector"]["all_value_ids"] == [VALUE_A]
    with pytest.raises((AttributeError, TypeError)):
        cast(Any, request).selector["all_value_ids"].append(VALUE_B)


@pytest.mark.parametrize(
    "changes",
    [
        {"add_value_ids": [], "remove_value_ids": []},
        {"add_value_ids": [VALUE_A], "remove_value_ids": [VALUE_A]},
        {"add_value_ids": [VALUE_A], "expected_revision_id": None},
        {"record_id": SOURCE},
    ],
)
def test_assignment_patch_is_nonempty_disjoint_and_anchored(changes: dict[str, Any]) -> None:
    _reject_request("facet.assignment.patch", _patch(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": None},
        {"status": "unchanged"},
        {"version": True},
        {"subject_id": QUERY},
        {"extra": "private"},
    ],
)
def test_mutation_receipts_enforce_event_status_and_operation_subject(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(FacetError):
        response_from_wire("facet.dimension.create", _receipt(**changes))


def test_receipt_round_trip_preserves_exact_logical_success() -> None:
    body = _receipt()
    response = response_from_wire("facet.dimension.create", body)
    assert response_to_wire("facet.dimension.create", response) == body
    with pytest.raises(FacetError):
        response_to_wire(
            "facet.dimension.create", {k: v for k, v in body.items() if k != "event_id"}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"next_cursor": "cursor", "complete": True},
        {"next_cursor": None, "complete": False},
        {"complete": "true"},
        {"total_count": 0},
        {"items": [{"dimension_id": DIMENSION}]},
    ],
)
def test_list_responses_are_closed_and_have_consistent_continuation(
    changes: dict[str, Any],
) -> None:
    body = {
        "dto_version": 1,
        "status": "ok",
        "items": [],
        "next_cursor": None,
        "complete": True,
        **changes,
    }
    with pytest.raises(FacetError):
        response_from_wire("facet.dimension.list", body)


@pytest.mark.parametrize(
    "changes",
    [
        {"destination_space_id": None},
        {"target_page_id": PAGE},
        {"expected_page_revision_id": "revision_00000000-0000-5000-8000-000000000010"},
        {
            "target_page_id": PAGE,
            "expected_page_revision_id": "revision_00000000-0000-5000-8000-000000000010",
        },
        {"title": ""},
        {"title": "x" * 201},
        {"markdown": "\u00e9" * 32769},
        {"capture_ids": [CAPTURE]},
        {"authority": {"owner": True}},
    ],
)
def test_publication_request_preserves_create_update_and_source_mode_boundaries(
    changes: dict[str, Any],
) -> None:
    _reject_request("review.propose_from_facet_query", _proposal(**changes))


def test_publication_admits_new_page_and_fixed_destination_update() -> None:
    request_from_wire("review.propose_from_facet_query", _proposal())
    request_from_wire(
        "review.propose_from_facet_query",
        _proposal(
            target_page_id=PAGE,
            expected_page_revision_id="revision_00000000-0000-5000-8000-000000000010",
            destination_space_id=None,
        ),
    )


def test_publication_encoded_request_allowance_preserves_utf8_markdown_limit() -> None:
    body = _proposal(markdown="\u00e9" * 32768)
    raw = json.dumps(body)
    assert 65536 < len(raw.encode("utf-8")) < 1024 * 1024
    request_from_wire(
        "review.propose_from_facet_query",
        parse_wire(raw, operation="review.propose_from_facet_query"),
    )
    over_limit = raw + " " * (1024 * 1024 + 1 - len(raw.encode("utf-8")))
    with pytest.raises(FacetError):
        parse_wire(over_limit, operation="review.propose_from_facet_query")


def test_regular_encoded_request_byte_limit_is_inclusive() -> None:
    raw = '{"dto_version":1}'
    exact_limit = raw + " " * (65536 - len(raw.encode("utf-8")))
    request_from_wire("facet.dimension.list", parse_wire(exact_limit))
    with pytest.raises(FacetError):
        parse_wire(exact_limit + " ")


def test_response_serializer_reports_invalid_unicode_as_facet_error() -> None:
    with pytest.raises(FacetError):
        response_to_wire(
            "facet.dimension.list",
            {
                "dto_version": 1,
                "status": "ok",
                "complete": True,
                "next_cursor": None,
                "items": [
                    {"dimension_id": DIMENSION, "name": "\ud800", "archived": False, "version": 1}
                ],
            },
        )


@pytest.mark.parametrize(
    "operation,body",
    [
        (
            "facet.vault.preview",
            {
                "dto_version": 1,
                "workspace_id": "synthetic-workspace",
                "record_id": CAPTURE,
                "path": "/tmp/forbidden",
            },
        ),
        (
            "facet.vault.preview",
            {
                "dto_version": 1,
                "workspace_id": "synthetic-workspace",
                "record_id": CAPTURE,
                "add_value_ids": [VALUE_A],
            },
        ),
        (
            "facet.vault.apply",
            {
                "dto_version": 1,
                "preview_id": "synthetic-preview",
                "operation_id": "synthetic-apply",
                "expected_version": 0,
            },
        ),
        (
            "facet.vault.apply",
            {
                "dto_version": 1,
                "preview_id": "synthetic-preview",
                "operation_id": "synthetic-apply",
                "record_id": CAPTURE,
            },
        ),
    ],
)
def test_vault_requests_cannot_bypass_preview_custody_or_claim_paths(
    operation: str,
    body: dict[str, Any],
) -> None:
    _reject_request(operation, body)


def test_every_declared_wire_operation_has_typed_request_response_and_method() -> None:
    expected = {
        "facet.dimension.list",
        "facet.dimension.create",
        "facet.dimension.rename",
        "facet.dimension.archive",
        "facet.value.list",
        "facet.value.create",
        "facet.value.rename",
        "facet.value.archive",
        "facet.assignment.get",
        "facet.assignment.patch",
        "facet.query.list",
        "facet.query.get",
        "facet.query.create",
        "facet.query.update",
        "facet.query.archive",
        "facet.query.attach",
        "facet.query.attachment.get",
        "facet.query.page",
        "facet.history.list",
        "facet.history.state",
        "review.propose_from_facet_query",
        "facet.vault.preview",
        "facet.vault.apply",
    }
    assert set(REQUEST_TYPES) == expected
    assert set(RESPONSE_TYPES) == expected
    assert set(OPERATION_METHODS) == expected


def test_facets_protocol_requires_keyword_only_trusted_authority() -> None:
    from open_brain_engine.engine.facet_contracts import (
        FacetPublicationTasks,
        FacetTasks,
        FacetVaultTasks,
    )
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    for operation, method in OPERATION_METHODS.items():
        protocol = (
            FacetPublicationTasks
            if operation.startswith("review.")
            else FacetVaultTasks
            if operation.startswith("facet.vault.")
            else FacetTasks
        )
        callable_method = getattr(protocol, method)
        signature = inspect.signature(callable_method)
        assert signature.parameters["authority"].kind is inspect.Parameter.KEYWORD_ONLY
        hints = get_type_hints(callable_method)
        assert hints["authority"] is EffectiveAuthority
        assert hints["request"] is REQUEST_TYPES[operation]
        assert hints["return"] is RESPONSE_TYPES[operation]


def test_history_source_state_cannot_carry_page_attachment() -> None:
    body: dict[str, Any] = {
        "dto_version": 1,
        "status": "ok",
        "as_of_sequence": 1,
        "item": {
            "subject": {"kind": "source", "id": SOURCE},
            "version": 0,
            "memberships": [],
            "attachment": {"page_id": PAGE, "query_id": QUERY, "version": 1},
        },
    }
    with pytest.raises(FacetError):
        response_from_wire("facet.history.state", body)


def test_assignment_read_rejects_duplicate_memberships_and_negative_versions() -> None:
    item: dict[str, Any] = {
        "subject": {"kind": "source", "id": SOURCE},
        "record_id": CAPTURE,
        "revision_id": CAPTURE,
        "version": 1,
        "memberships": [{"value_id": VALUE_A, "membership_version": 1}],
    }
    for change in ("duplicate", "negative"):
        candidate = deepcopy(item)
        if change == "duplicate":
            candidate["memberships"].append(deepcopy(candidate["memberships"][0]))
        else:
            candidate["memberships"][0]["membership_version"] = -1
        with pytest.raises(FacetError):
            response_from_wire(
                "facet.assignment.get",
                {
                    "dto_version": 1,
                    "status": "ok",
                    "item": candidate,
                },
            )


def test_query_selector_rejects_unknown_nested_keys() -> None:
    _reject_request(
        "facet.query.page",
        {
            "dto_version": 1,
            "selector": _selector(authority={"space_ids": [SPACE]}),
        },
    )


@pytest.mark.parametrize("selected_ids", [[VALUE_B, VALUE_A], [VALUE_A, VALUE_A]])
def test_saved_query_responses_require_canonical_sorted_unique_selector(
    selected_ids: list[str],
) -> None:
    body: dict[str, Any] = {
        "dto_version": 1,
        "status": "ok",
        "item": {
            "query_id": QUERY,
            "name": "Synthetic query",
            "archived": False,
            "version": 1,
            "selection_version": 1,
            "selector": {
                "all_value_ids": selected_ids,
                "space_ids": [],
                "payload_families": [],
                "record_types": ["canonical", "source"],
            },
        },
    }
    with pytest.raises(FacetError):
        response_from_wire("facet.query.get", body)


def test_value_response_cannot_hide_its_own_archived_state() -> None:
    with pytest.raises(FacetError):
        response_from_wire(
            "facet.value.list",
            {
                "dto_version": 1,
                "status": "ok",
                "complete": True,
                "next_cursor": None,
                "items": [
                    {
                        "value_id": VALUE_A,
                        "dimension_id": DIMENSION,
                        "name": "Synthetic",
                        "archived": True,
                        "effective_archived": False,
                        "version": 1,
                    }
                ],
            },
        )


def test_json_boundary_accepts_canonical_integer_tokens() -> None:
    body = {"dto_version": 1, "limit": 100, "cursor": None, "include_archived": False}
    assert request_from_wire("facet.dimension.list", parse_wire(json.dumps(body))).to_wire() == body
