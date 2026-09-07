from __future__ import annotations

import math

import pytest
from open_brain_engine.protocol import (
    AUTHORIZATION_POLICY,
    CLOCK_POLICY,
    CRYPTO_PROFILE,
    IDENTIFIER_ROLES,
    RESOURCE_LIMITS,
    SEMANTIC_OPERATIONS,
    PrincipalKeyCustodian,
    RootKeyCustodian,
    UserPresenceProvider,
    canonical_json_bytes,
    canonical_sha256,
    generate_identifier,
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
        "destroy",
        "restart",
        "rotate",
        "unlock",
    } <= set(vars(RootKeyCustodian)["__protocol_attrs__"])
    assert {"create", "destroy", "load_public_key", "rotate", "sign"} <= set(
        vars(PrincipalKeyCustodian)["__protocol_attrs__"]
    )
    assert {"challenge"} <= set(vars(UserPresenceProvider)["__protocol_attrs__"])
