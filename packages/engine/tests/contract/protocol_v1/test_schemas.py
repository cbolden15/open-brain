from __future__ import annotations

import json
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from open_brain_engine.protocol import load_conformance_cases, load_schema, schema_catalog
from referencing import Registry, Resource

EXPECTED_SCHEMAS = {
    "changes",
    "cold-transfer-certificate",
    "commit-batch",
    "decision",
    "durable-job",
    "grant",
    "inspect",
    "provenance",
    "purge-transition",
    "query",
    "query-continuation",
    "query-evidence",
    "receipt",
    "record",
    "request-binding",
    "resource-limit-failure",
    "revision",
    "security-audit-event",
    "sequencer-stop-proof",
}
FORMAT_CHECKER = FormatChecker()


def _validator(name: str) -> Draft202012Validator:
    schemas = {schema_name: load_schema(schema_name) for schema_name in schema_catalog()}
    registry = Registry().with_resources(
        (str(schema["$id"]), Resource.from_contents(schema)) for schema in schemas.values()
    )
    return Draft202012Validator(schemas[name], registry=registry, format_checker=FORMAT_CHECKER)


def test_schema_catalog_is_versioned_complete_and_self_consistent() -> None:
    assert set(schema_catalog()) == EXPECTED_SCHEMAS | {"common"}
    for name in schema_catalog():
        schema = load_schema(name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"] == f"urn:open-brain:protocol:v1:{name}"
        Draft202012Validator.check_schema(schema)


def test_every_frozen_schema_has_valid_and_negative_conformance_cases() -> None:
    cases = load_conformance_cases()
    assert set(cases) == {"valid", "invalid"}
    for disposition in ("valid", "invalid"):
        assert set(cases[disposition]) == EXPECTED_SCHEMAS
        assert all(cases[disposition][name] for name in EXPECTED_SCHEMAS)

    for name in sorted(EXPECTED_SCHEMAS):
        validator = _validator(name)
        for value in cases["valid"][name]:
            errors = sorted(validator.iter_errors(value), key=str)
            assert not errors, f"{name}: {[error.message for error in errors]}"
        for value in cases["invalid"][name]:
            assert not validator.is_valid(value), f"negative {name} case was accepted"


def test_query_continuation_exposes_only_an_opaque_protected_token() -> None:
    valid = cast(dict[str, object], load_conformance_cases()["valid"]["query-continuation"][0])
    assert set(valid) == {"schema_version", "token"}
    assert json.dumps(valid).casefold().find("shard") == -1
    assert json.dumps(valid).casefold().find("score") == -1
    assert json.dumps(valid).casefold().find("record") == -1


@pytest.mark.parametrize("forbidden", ("grant", "payload", "digest", "body", "path"))
def test_redacted_security_event_rejects_sensitive_fields(forbidden: str) -> None:
    event = dict(
        cast(
            dict[str, object],
            load_conformance_cases()["valid"]["security-audit-event"][0],
        )
    )
    event[forbidden] = "sensitive"
    assert not _validator("security-audit-event").is_valid(event)


def test_grant_requires_proof_of_possession_binding_and_bounded_ttl() -> None:
    grant = dict(cast(dict[str, object], load_conformance_cases()["valid"]["grant"][0]))
    grant.pop("principal_public_key")
    assert not _validator("grant").is_valid(grant)

    grant = dict(cast(dict[str, object], load_conformance_cases()["valid"]["grant"][0]))
    grant["ttl_seconds"] = 901
    assert not _validator("grant").is_valid(grant)


def test_inspect_not_found_has_no_presence_or_body_oracle() -> None:
    cases = load_conformance_cases()["valid"]["inspect"]
    not_found = next(
        cast(dict[str, object], value)
        for value in cases
        if isinstance(value, dict) and value.get("kind") == "not_found"
    )
    assert not_found == {
        "code": "not_found",
        "kind": "not_found",
        "message": "Entity was not found.",
        "retry_safe": True,
        "schema_version": 1,
    }


def test_typed_resource_failure_is_actionable() -> None:
    failure = cast(
        dict[str, object],
        load_conformance_cases()["valid"]["resource-limit-failure"][0],
    )
    assert set(failure) == {
        "code",
        "corrective_action",
        "limit",
        "message",
        "observed",
        "retry_after_ms",
        "retry_safe",
        "schema_version",
    }
