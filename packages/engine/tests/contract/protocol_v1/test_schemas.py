from __future__ import annotations

import json
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from open_brain_engine.protocol import (
    ProtocolContractError,
    load_conformance_cases,
    load_schema,
    load_signature_vectors,
    request_binding_from_envelope,
    schema_catalog,
    validate_protocol_semantics,
)
from referencing import Registry, Resource

EXPECTED_SCHEMAS = {
    "changes",
    "cold-transfer-certificate",
    "commit-batch",
    "commit-result",
    "decision",
    "durable-job",
    "effect-receipt",
    "grant",
    "inspect",
    "node-epoch-certificate",
    "owner-key-certificate",
    "proposal",
    "provenance",
    "purge-transition",
    "query",
    "query-continuation",
    "query-evidence",
    "receipt",
    "record",
    "request-binding",
    "request-envelope",
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
            validate_protocol_semantics(name, cast(dict[str, object], value))
        for value in cases["invalid"][name]:
            if validator.is_valid(value):
                with pytest.raises(ProtocolContractError):
                    validate_protocol_semantics(name, cast(dict[str, object], value))


def test_principal_signature_vector_uses_the_exact_request_binding_schema() -> None:
    vector = load_signature_vectors()[0]
    binding = cast(dict[str, object], vector["request_binding"])
    envelope = cast(
        dict[str, object],
        load_conformance_cases()["valid"]["request-envelope"][0],
    )

    assert _validator("request-binding").is_valid(binding)
    assert request_binding_from_envelope(envelope) == binding


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

    grant = dict(cast(dict[str, object], load_conformance_cases()["valid"]["grant"][0]))
    grant["expires_at"] = "2026-09-06T12:10:01Z"
    assert _validator("grant").is_valid(grant)
    with pytest.raises(ProtocolContractError, match="timestamps"):
        validate_protocol_semantics("grant", grant)


def test_record_envelope_requires_authority_times_provenance_and_integrity() -> None:
    record = cast(dict[str, object], load_conformance_cases()["valid"]["record"][0])
    required = {
        "content_schema",
        "producer_principal_id",
        "origin_id",
        "captured_at",
        "observed_at",
        "provenance",
        "ciphertext_state",
        "ciphertext_digest",
    }

    assert required <= set(record)
    persisted = dict(record)
    persisted["ciphertext_state"] = "verified"
    persisted["ciphertext_digest"] = "b" * 64
    assert _validator("record").is_valid(persisted)
    for field in required:
        incomplete = dict(record)
        incomplete.pop(field)
        assert not _validator("record").is_valid(incomplete), field

    for state, digest in (
        ("pending", "b" * 64),
        ("verified", None),
        ("unknown_historical", "b" * 64),
    ):
        inconsistent = dict(record)
        inconsistent["ciphertext_state"] = state
        inconsistent["ciphertext_digest"] = digest
        assert not _validator("record").is_valid(inconsistent), state


def test_commit_accepts_only_pending_record_envelopes() -> None:
    batch = cast(dict[str, object], load_conformance_cases()["valid"]["commit-batch"][0])
    persisted_batch = dict(batch)
    items = cast(list[object], batch["items"])
    persisted = dict(cast(dict[str, object], items[0]))
    persisted["ciphertext_state"] = "verified"
    persisted["ciphertext_digest"] = "b" * 64
    persisted_batch["items"] = [persisted]

    assert _validator("record").is_valid(persisted)
    assert not _validator("commit-batch").is_valid(persisted_batch)


def test_processor_provenance_requires_identity_and_a_source() -> None:
    provenance = {
        "schema_version": 1,
        "brain_id": "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "source_record_ids": [],
        "source_revision_ids": [],
        "processor": None,
        "derivation": "processor",
    }

    assert not _validator("provenance").is_valid(provenance)


def test_commit_semantics_reject_a_structurally_valid_foreign_brain_item() -> None:
    batch = cast(dict[str, object], load_conformance_cases()["invalid"]["commit-batch"][0])

    assert _validator("commit-batch").is_valid(batch)
    with pytest.raises(ProtocolContractError, match="Brain boundary"):
        validate_protocol_semantics("commit-batch", batch)


def test_query_limit_counts_utf8_bytes_not_code_points() -> None:
    request = dict(cast(dict[str, object], load_conformance_cases()["valid"]["query"][0]))
    request["literal_text"] = "☃" * 1366

    assert len(cast(str, request["literal_text"])) < 4096
    assert len(cast(str, request["literal_text"]).encode("utf-8")) > 4096
    assert _validator("query").is_valid(request)
    with pytest.raises(ProtocolContractError, match="4096 UTF-8 bytes"):
        validate_protocol_semantics("query", request)


def test_commit_results_are_closed_typed_outcomes() -> None:
    cases = load_conformance_cases()["valid"]
    receipt = cast(dict[str, object], cases["receipt"][0])
    validator = _validator("commit-result")

    for kind in ("accepted", "replayed"):
        result = {"schema_version": 1, "kind": kind, "receipt": receipt}
        assert validator.is_valid(result)
        validate_protocol_semantics("commit-result", result)

    assert {
        (cast(dict[str, object], result)["kind"], cast(dict[str, object], result)["code"])
        for result in cases["commit-result"]
    } == {
        ("conflict", "digest_conflict"),
        ("conflict", "revision_conflict"),
        ("delivery_purged", "delivery_purged"),
    }


def test_changes_feed_accepts_every_typed_commit_identity() -> None:
    item_ids = {
        "record": "rec_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "proposal": "prp_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "revision": "rev_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "decision": "dec_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "purge_transition": "prg_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "effect_receipt": "rcp_aaaaaaaaaaaaaaaaaaaaaaaaaa",
    }

    for item_kind, item_id in item_ids.items():
        page = {
            "schema_version": 1,
            "kind": "page",
            "brain_id": "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "changes": [
                {
                    "cursor": "cur_v1_aaaaaaaaaaaaaaaa",
                    "commit_id": "cmt_aaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "item_kind": item_kind,
                    "item_id": item_id,
                    "change": "committed",
                }
            ],
            "next": None,
        }
        assert _validator("changes").is_valid(page), item_kind


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


def test_bodyless_inspect_rejects_payload_and_commit_digest_metadata() -> None:
    leaked = cast(dict[str, object], load_conformance_cases()["invalid"]["inspect"][0])

    assert not _validator("inspect").is_valid(leaked)


def test_failed_job_requires_a_bounded_attempt_and_error_code() -> None:
    failed = cast(dict[str, object], load_conformance_cases()["invalid"]["durable-job"][0])

    assert not _validator("durable-job").is_valid(failed)


@pytest.mark.parametrize(
    ("entity_kind", "entity_id"),
    (
        ("record", "rev_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("proposal", "rec_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("revision", "dec_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("decision", "prp_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("effect_receipt", "eff_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("purge", "rec_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("operation", "job_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("job", "opn_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("receipt", "cmt_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
    ),
)
def test_inspect_entity_kind_requires_the_matching_identifier_role(
    entity_kind: str,
    entity_id: str,
) -> None:
    request = dict(cast(dict[str, object], load_conformance_cases()["valid"]["inspect"][0]))
    request["entity_kind"] = entity_kind
    request["entity_id"] = entity_id

    assert not _validator("inspect").is_valid(request)


def test_typed_resource_failure_is_actionable() -> None:
    failure = cast(
        dict[str, object],
        load_conformance_cases()["valid"]["resource-limit-failure"][0],
    )
    assert set(failure) == {
        "accounting_scope",
        "code",
        "corrective_action",
        "limit",
        "message",
        "observed",
        "principal_id",
        "retry_after_ms",
        "retry_safe",
        "schema_version",
    }
