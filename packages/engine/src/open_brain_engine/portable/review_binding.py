"""Portable Brain v3 review-binding values and structural validation."""

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
_MAX_SOURCES = 32


def review_binding_digest(
    proposal: Mapping[str, object], binding: Mapping[str, object]
) -> str:
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
    capture_ids = [
        _identifier(item, "capture", label)
        for item in values
    ]
    if len(capture_ids) != len(set(capture_ids)):
        raise PortableValidationError(f"{label} is malformed")
    return capture_ids


def validate_review_binding(binding: object) -> dict[str, object]:
    """Validate one closed, bounded Portable Brain v3 review binding."""
    value = _object(binding, "review binding")
    _require_exact_keys(value, _BINDING_KEYS, "review binding")
    if value.get("schema_version") != 3:
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

    selected = _capture_ids(value.get("selected_capture_ids"), "selected captures")
    provenance = _capture_ids(value.get("provenance"), "review provenance")
    if any(capture_id not in provenance for capture_id in selected):
        raise PortableValidationError("review selected captures are not provenance")

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
        current_space_id = _identifier(state.get("space_id"), "space", "review source state")
        if space_id is None:
            space_id = current_space_id
        elif current_space_id != space_id:
            raise PortableValidationError("review source spaces are mixed")
        route_id = state.get("route_id")
        if route_id is not None:
            _identifier(route_id, "route", "review source state")
        states.append(state)
    result = dict(value)
    result["selected_capture_ids"] = selected
    result["provenance"] = provenance
    result["source_states"] = states
    return result


__all__ = ["review_binding_digest", "validate_review_binding"]
