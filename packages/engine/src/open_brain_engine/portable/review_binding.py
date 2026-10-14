"""Versioned review-binding values and structural validation."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v1 import (
    PortableValidationError,
    _digest,
    _identifier,
    _object,
    _require_exact_keys,
    _required_list,
    _timestamp,
    _validate_role_binding,
)

_BINDING_KEYS = {
    "actor_id",
    "expected_page_sha256",
    "expected_publication_id",
    "operation",
    "page_id",
    "proposal_id",
    "provenance",
    "recorded_at",
    "review_digest",
    "role_claim",
    "schema_version",
    "selected_capture_ids",
    "source_states",
    "tenant_id",
}
_SOURCE_STATE_KEYS = {"capture_id", "route_id", "sha256", "space_id"}
_FACET_KEYS = {
    "selection_kind", "destination_space_id", "query_id", "query_version",
    "query_selection_version", "selector", "attachment_version", "facet_source_states",
}
_SELECTOR_KEYS = {"all_value_ids", "space_ids", "payload_families", "record_types"}
_FACET_SOURCE_KEYS = {"source_id", "capture_id", "head_version", "memberships"}
_PATCH_KEYS = {"base_body", "base_body_sha256", "operations", "target_page_sha256"}
_PATCH_OPERATION_KEYS = {"start_byte", "end_byte", "replacement"}
_MAX_SOURCES = 32
_MAX_PATCH_OPERATIONS = 16
_MAX_PATCH_REPLACEMENT_BYTES = 16 * 1024
_MAX_PATCH_TOTAL_BYTES = 64 * 1024


def review_binding_digest(proposal: Mapping[str, object], binding: Mapping[str, object]) -> str:
    """Bind exact proposal bytes to the target and frozen source state."""
    if not isinstance(proposal, Mapping) or not isinstance(binding, Mapping):
        raise ValueError("review binding digest input is invalid")
    binding_without_digest = dict(binding)
    binding_without_digest.pop("review_digest", None)
    try:
        payload = portable_canonical_json_bytes(
            {"binding": binding_without_digest, "proposal": dict(proposal)}
        )
    except (TypeError, ValueError) as error:
        raise ValueError("review binding digest input is invalid") from error
    return sha256(payload).hexdigest()


def _capture_ids(value: object, label: str) -> list[str]:
    values = _required_list({"value": value}, "value", label)
    if not 1 <= len(values) <= _MAX_SOURCES:
        raise PortableValidationError(f"{label} is malformed")
    capture_ids = [_identifier(item, "capture", label) for item in values]
    if len(capture_ids) != len(set(capture_ids)):
        raise PortableValidationError(f"{label} is malformed")
    return capture_ids


def validate_review_binding(binding: object) -> dict[str, object]:
    """Validate one closed, bounded v3, v4, or v5 review binding."""
    value = _object(binding, "review binding")
    version = value.get("schema_version")
    expected_keys = (
        _BINDING_KEYS if version == 3 else
        _BINDING_KEYS | {"patch"} if version == 4 else
        _BINDING_KEYS | _FACET_KEYS
    )
    _require_exact_keys(value, expected_keys, "review binding")
    if type(version) is not int or version not in {3, 4, 5}:
        raise PortableValidationError("review binding schema version is malformed")
    tenant_id = _identifier(value.get("tenant_id"), "tenant", "review binding")
    _validate_role_binding(value, tenant_id, "review binding")
    _identifier(value.get("proposal_id"), "proposal", "review binding")
    _identifier(value.get("page_id"), "page", "review binding")
    _timestamp(value.get("recorded_at"), "review binding")
    _digest(value.get("review_digest"), "review binding")

    operation = value.get("operation")
    expected_publication_id = value.get("expected_publication_id")
    expected_page_sha256 = value.get("expected_page_sha256")
    if operation == "create":
        if expected_publication_id is not None or expected_page_sha256 is not None:
            raise PortableValidationError("review create predecessor is malformed")
    elif operation == "update":
        _identifier(expected_publication_id, "publication", "review binding predecessor")
        _digest(expected_page_sha256, "review binding predecessor")
    else:
        raise PortableValidationError("review binding operation is malformed")

    if version == 4:
        if operation != "update":
            raise PortableValidationError("review patch operation is malformed")
        patch = _object(value.get("patch"), "review patch")
        _require_exact_keys(patch, _PATCH_KEYS, "review patch")
        _digest(patch.get("base_body_sha256"), "review patch")
        base_body = patch.get("base_body")
        if (
            not isinstance(base_body, str)
            or len(base_body.encode("utf-8")) > _MAX_PATCH_TOTAL_BYTES
            or sha256(base_body.encode("utf-8")).hexdigest() != patch["base_body_sha256"]
        ):
            raise PortableValidationError("review patch base is malformed")
        if patch.get("target_page_sha256") != expected_page_sha256:
            raise PortableValidationError("review patch target is malformed")
        operations = _required_list(patch, "operations", "review patch")
        if not 1 <= len(operations) <= _MAX_PATCH_OPERATIONS:
            raise PortableValidationError("review patch operations are malformed")
        prior_end = -1
        replacement_bytes = 0
        for raw_operation in operations:
            operation_value = _object(raw_operation, "review patch operation")
            _require_exact_keys(operation_value, _PATCH_OPERATION_KEYS, "review patch operation")
            start = operation_value.get("start_byte")
            end = operation_value.get("end_byte")
            replacement = operation_value.get("replacement")
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start <= end <= _MAX_PATCH_TOTAL_BYTES
                or start < prior_end
                or not isinstance(replacement, str)
                or "\x00" in replacement
            ):
                raise PortableValidationError("review patch operation is malformed")
            size = len(replacement.encode("utf-8"))
            if size > _MAX_PATCH_REPLACEMENT_BYTES:
                raise PortableValidationError("review patch operation is malformed")
            replacement_bytes += size
            prior_end = end
        if replacement_bytes > _MAX_PATCH_TOTAL_BYTES:
            raise PortableValidationError("review patch replacement is malformed")

    selected = _capture_ids(value.get("selected_capture_ids"), "selected captures")
    provenance = _capture_ids(value.get("provenance"), "review provenance")
    if any(capture_id not in provenance for capture_id in selected):
        raise PortableValidationError("review selected captures are not provenance")

    if version == 5 and operation == "create" and selected != provenance:
        raise PortableValidationError("facet create provenance differs from selection")

    raw_states = _required_list(value, "source_states", "review binding")
    if len(raw_states) != len(provenance):
        raise PortableValidationError("review source states are malformed")
    states: list[dict[str, object]] = []
    space_id: str | None = None
    for index, raw_state in enumerate(raw_states):
        state = _object(raw_state, "review source state")
        _require_exact_keys(state, _SOURCE_STATE_KEYS, "review source state")
        capture_id = _identifier(state.get("capture_id"), "capture", "review source state")
        if capture_id != provenance[index]:
            raise PortableValidationError("review source state order is malformed")
        _digest(state.get("sha256"), "review source state")
        raw_space_id = state.get("space_id")
        current_space_id = (
            None if version == 5 and raw_space_id is None else
            _identifier(raw_space_id, "space", "review source state")
        )
        if version != 5:
            if space_id is None:
                space_id = current_space_id
            elif current_space_id != space_id:
                raise PortableValidationError("review source spaces are mixed")
        route_id = state.get("route_id")
        if route_id is not None:
            _identifier(route_id, "route", "review source state")
        states.append(state)
    if version == 5:
        _validate_facet_binding(value, selected)
    result = dict(value)
    result["selected_capture_ids"] = selected
    result["provenance"] = provenance
    result["source_states"] = states
    return result


def _version(value: object, *, minimum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise PortableValidationError(f"{label} is malformed")
    return value


def _sorted_unique_ids(
    value: object, prefix: str, label: str, *, minimum: int = 0, maximum: int = _MAX_SOURCES
) -> list[str]:
    values = _required_list({"value": value}, "value", label)
    if not minimum <= len(values) <= maximum:
        raise PortableValidationError(f"{label} is malformed")
    ids = [_identifier(item, prefix, label) for item in values]
    if ids != sorted(set(ids)):
        raise PortableValidationError(f"{label} is malformed")
    return ids


def _validate_facet_binding(value: dict[str, object], selected: list[str]) -> None:
    if value["selection_kind"] != "facet":
        raise PortableValidationError("review facet selection is malformed")
    _identifier(value["destination_space_id"], "space", "review facet destination")
    _identifier(value["query_id"], "facetquery", "review facet query")
    query_version = _version(value["query_version"], minimum=1, label="review facet query")
    selection_version = _version(
        value["query_selection_version"], minimum=1, label="review facet selection"
    )
    if selection_version > query_version:
        raise PortableValidationError("review facet selection is malformed")
    attachment_version = _version(
        value["attachment_version"], minimum=0, label="review facet attachment"
    )
    if (attachment_version == 0) != (value["operation"] == "create"):
        raise PortableValidationError("review facet attachment is malformed")
    selector = _object(value["selector"], "review facet selector")
    _require_exact_keys(selector, _SELECTOR_KEYS, "review facet selector")
    selected_values = _sorted_unique_ids(
        selector["all_value_ids"], "facetval", "review facet values", minimum=1
    )
    _sorted_unique_ids(selector["space_ids"], "space", "review facet spaces", maximum=256)
    record_types: list[str] = []
    for key, allowed in (
        ("record_types", {"canonical", "source"}),
        ("payload_families", {"text", "event", "measurement", "reference_or_file"}),
    ):
        options = _required_list(selector, key, "review facet selector")
        if any(type(item) is not str for item in options):
            raise PortableValidationError("review facet selector is malformed")
        string_options = [str(item) for item in options]
        if string_options != sorted(set(string_options)) or any(
            item not in allowed for item in string_options
        ):
            raise PortableValidationError("review facet selector is malformed")
        if key == "record_types":
            record_types = string_options
    if record_types and "source" not in record_types:
        raise PortableValidationError("review facet selector excludes sources")
    raw_states = _required_list(value, "facet_source_states", "review facet binding")
    if len(raw_states) != len(selected):
        raise PortableValidationError("review facet source states are malformed")
    source_ids: set[str] = set()
    for selected_id, raw_state in zip(selected, raw_states, strict=True):
        state = _object(raw_state, "review facet source state")
        _require_exact_keys(state, _FACET_SOURCE_KEYS, "review facet source state")
        source_id = _identifier(state["source_id"], "source", "review facet source state")
        if source_id in source_ids or state["capture_id"] != selected_id:
            raise PortableValidationError("review facet source states are malformed")
        source_ids.add(source_id)
        _version(state["head_version"], minimum=1, label="review facet head")
        memberships = _required_list(state, "memberships", "review facet source state")
        if len(memberships) != len(selected_values):
            raise PortableValidationError("review facet memberships are malformed")
        for expected_id, raw_membership in zip(selected_values, memberships, strict=True):
            membership = _object(raw_membership, "review facet membership")
            _require_exact_keys(
                membership, {"value_id", "membership_version"}, "review facet membership"
            )
            if membership["value_id"] != expected_id:
                raise PortableValidationError("review facet memberships are malformed")
            _version(
                membership["membership_version"], minimum=1, label="review facet membership"
            )


__all__ = ["review_binding_digest", "validate_review_binding"]
