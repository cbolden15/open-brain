from __future__ import annotations

import math
from dataclasses import fields
from inspect import signature

import pytest
from open_brain_engine.protocol import (
    AUTHORIZATION_POLICY,
    CLOCK_POLICY,
    CRYPTO_PROFILE,
    IDENTIFIER_ROLES,
    RESOURCE_LIMITS,
    SEMANTIC_OPERATIONS,
    CiphertextEnvelope,
    PrincipalKeyCustodian,
    ProtocolContractError,
    RootKeyCustodian,
    UserPresenceProvider,
    WrappedKeyEnvelope,
    canonical_json_bytes,
    canonical_sha256,
    decode_base64url,
    decode_protocol_json,
    generate_identifier,
    ledger_history_commitment,
    load_conformance_cases,
    validate_identifier,
)


def test_only_four_semantic_operations_are_frozen() -> None:
    assert SEMANTIC_OPERATIONS == ("commit", "query", "changes", "inspect")


def test_rfc8785_bytes_and_digest_are_canonical() -> None:
    value = {"z": [3, 2, 1], "é": "snowman ☃", "a": 1}

    encoded = canonical_json_bytes(value)

    assert encoded == '{"a":1,"z":[3,2,1],"é":"snowman ☃"}'.encode()
    assert canonical_sha256(value) == (
        "12eab2a345ec3ec39043a6a7b89aa0e7187ec9ca0b3fdd9f78f5d20c22e27063"
    )


def test_ledger_history_commitment_is_domain_separated_and_reproducible() -> None:
    receipt = load_conformance_cases()["valid"]["receipt"][0]

    assert ledger_history_commitment(receipt) == (
        "lhc_v1_PgQze80r54tk4H2UsGhmi4iGkLg-ovE42Duq8r80jAQ"
    )


@pytest.mark.parametrize("value", ({1: "non-string key"}, math.nan, math.inf, 2**60))
def test_canonicalization_rejects_values_outside_rfc8785(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json_bytes(value)


def test_generated_identifiers_are_role_distinct_opaque_and_brain_scoped() -> None:
    brain_id = generate_identifier("brain")
    record_id = generate_identifier("record", brain_id=brain_id)
    receipt_id = generate_identifier("receipt", brain_id=brain_id)

    assert validate_identifier(brain_id, role="brain") == brain_id
    assert validate_identifier(record_id, role="record", brain_id=brain_id) == record_id
    assert validate_identifier(receipt_id, role="receipt", brain_id=brain_id) == receipt_id
    assert record_id.partition("_")[0] != receipt_id.partition("_")[0]
    assert len(record_id.partition("_")[2]) == 26
    assert "sha256" not in record_id
    with pytest.raises(ValueError):
        validate_identifier(record_id, role="receipt", brain_id=brain_id)
    with pytest.raises(ValueError):
        validate_identifier(record_id, role="record")


def test_identifier_role_catalog_covers_semantic_and_operational_values() -> None:
    assert IDENTIFIER_ROLES == {
        "artifact": "art",
        "brain": "brn",
        "commit": "cmt",
        "decision": "dec",
        "delivery": "dlv",
        "effect": "eff",
        "grant": "grt",
        "job": "job",
        "key": "key",
        "node": "nod",
        "nonce": "non",
        "operation": "opn",
        "principal": "pri",
        "proposal": "prp",
        "purge": "prg",
        "receipt": "rcp",
        "record": "rec",
        "revision": "rev",
        "stop_proof": "stp",
        "transfer": "xfr",
    }


def test_resource_and_clock_bounds_are_explicit_and_apply_to_owners() -> None:
    assert RESOURCE_LIMITS.labels_per_record == 16
    assert RESOURCE_LIMITS.active_label_sets_per_brain == 256
    assert RESOURCE_LIMITS.authorized_query_fanout_per_round == 32
    assert RESOURCE_LIMITS.records_per_label_set == 50_000
    assert RESOURCE_LIMITS.commit_batch_items == 128
    assert RESOURCE_LIMITS.blob_staging_bytes == 64 * 1024 * 1024
    assert RESOURCE_LIMITS.concurrent_requests_per_brain == 32
    assert RESOURCE_LIMITS.concurrent_requests_per_principal == 8
    assert RESOURCE_LIMITS.retained_nonces_per_brain == 100_000
    assert RESOURCE_LIMITS.retained_nonces_per_principal == 10_000
    assert RESOURCE_LIMITS.query_text_bytes == 4_096
    assert RESOURCE_LIMITS.query_top_k == 100
    assert RESOURCE_LIMITS.query_pages_per_grant == 256
    assert RESOURCE_LIMITS.reciprocal_rank_fusion_constant == 60
    assert RESOURCE_LIMITS.durable_job_attempts == 8
    assert RESOURCE_LIMITS.owner_exempt is False
    assert CLOCK_POLICY.owner_session_seconds == 900
    assert CLOCK_POLICY.step_up_freshness_seconds == 60
    assert CLOCK_POLICY.grant_minimum_seconds == 30
    assert CLOCK_POLICY.grant_maximum_seconds == 900
    assert CLOCK_POLICY.utc_skew_allowance_seconds == 300


def test_crypto_and_authorization_profiles_freeze_security_boundaries() -> None:
    assert CRYPTO_PROFILE.payload_aead == "AES-256-GCM"
    assert CRYPTO_PROFILE.payload_nonce_bytes == 12
    assert CRYPTO_PROFILE.data_key_bytes == 32
    assert CRYPTO_PROFILE.data_key_wrap == "AES-256-KW"
    assert CRYPTO_PROFILE.argon2id_memory_kib == 65_536
    assert CRYPTO_PROFILE.argon2id_iterations == 3
    assert CRYPTO_PROFILE.argon2id_parallelism == 4
    assert CRYPTO_PROFILE.argon2id_salt_bytes == 16
    assert CRYPTO_PROFILE.argon2id_tag_bytes == 32
    assert CRYPTO_PROFILE.sqlcipher_compatibility == 4
    assert CRYPTO_PROFILE.sqlcipher_kdf_iterations == 256_000
    assert AUTHORIZATION_POLICY.principal_signature == "Ed25519"
    assert AUTHORIZATION_POLICY.bearer_grants is False
    assert AUTHORIZATION_POLICY.nonce_store == "separate-encrypted-operational-store"
    assert AUTHORIZATION_POLICY.reserve_nonce_before_operation is True


def test_key_custody_ports_are_separate_public_boundaries() -> None:
    custody_ports = {
        RootKeyCustodian.__name__,
        PrincipalKeyCustodian.__name__,
        UserPresenceProvider.__name__,
    }
    assert custody_ports == {
        "PrincipalKeyCustodian",
        "RootKeyCustodian",
        "UserPresenceProvider",
    }
    assert {
        "bootstrap",
        "decrypt",
        "derive",
        "destroy",
        "encrypt",
        "generate_data_key",
        "public_key",
        "restart",
        "rotate",
        "sign",
        "unlock",
        "unwrap_key",
        "wrap_key",
    } <= set(vars(RootKeyCustodian)["__protocol_attrs__"])
    assert {"create", "destroy", "load", "rotate", "sign"} <= set(
        vars(PrincipalKeyCustodian)["__protocol_attrs__"]
    )
    assert {"challenge"} <= set(vars(UserPresenceProvider)["__protocol_attrs__"])


def test_key_custody_envelopes_and_aead_inputs_are_exact() -> None:
    assert tuple(field.name for field in fields(CiphertextEnvelope)) == (
        "crypto_version",
        "data_key_identifier",
        "nonce",
        "associated_data_digest",
        "ciphertext_digest",
        "ciphertext",
    )
    assert tuple(field.name for field in fields(WrappedKeyEnvelope)) == (
        "crypto_version",
        "wrapping_key_identifier",
        "data_key_identifier",
        "wrapped_key",
    )
    assert tuple(signature(RootKeyCustodian.decrypt).parameters) == (
        "self",
        "key",
        "envelope",
        "associated_data",
    )
    assert tuple(signature(RootKeyCustodian.rotate).parameters) == (
        "self",
        "key",
        "presence",
    )


@pytest.mark.parametrize(
    "payload",
    (
        b'{"outer":{"key":1,"key":2}}',
        b'{"\\u006bey":1,"key":2}',
        b'{"number":NaN}',
        b'{"number":Infinity}',
        b'\xff',
    ),
)
def test_protocol_json_decoder_rejects_ambiguous_or_non_i_json(payload: bytes) -> None:
    with pytest.raises((UnicodeDecodeError, ValueError)):
        decode_protocol_json(payload)


def test_protocol_json_decoder_accepts_canonicalizable_utf8() -> None:
    assert decode_protocol_json(b'{"snowman":"\xe2\x98\x83","number":1}') == {
        "number": 1,
        "snowman": "☃",
    }


@pytest.mark.parametrize("value", ("A", "_" * 42, "a=" * 22, "+" * 43))
def test_base64url_decoder_enforces_canonical_exact_length(value: str) -> None:
    with pytest.raises(ProtocolContractError):
        decode_base64url(value, expected_bytes=32, label="public key")
