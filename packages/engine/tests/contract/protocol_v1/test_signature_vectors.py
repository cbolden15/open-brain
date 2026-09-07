from __future__ import annotations

import base64
import hashlib
from typing import cast

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from open_brain_engine.protocol import (
    ProtocolContractError,
    canonical_json_bytes,
    decode_base64url,
    ledger_history_commitment,
    load_conformance_cases,
    load_signature_vectors,
    signed_payload_bytes,
    validate_cold_transfer_pair,
    validate_protocol_semantics,
)

SIGNATURE_FIELDS = {
    "cold-transfer-certificate": "owner_signature",
    "grant": "issuer_signature",
    "node-epoch-certificate": "owner_signature",
    "owner-key-certificate": "owner_signature",
    "receipt": "node_signature",
    "sequencer-stop-proof": "node_signature",
}


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _public_key(private_key: Ed25519PrivateKey) -> str:
    return _base64url(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def _sign_document(
    contract: str,
    document: dict[str, object],
    private_key: Ed25519PrivateKey,
) -> dict[str, object]:
    signed = dict(document)
    signature_field = SIGNATURE_FIELDS[contract]
    signed[signature_field] = ""
    signed[signature_field] = _base64url(
        private_key.sign(signed_payload_bytes(contract, signed))
    )
    return signed


def _temporal_receipt(
    *,
    genesis_valid_from: str,
    genesis_retired_at: str | None,
    successor_valid_from: str | None,
    node_issued_at: str,
) -> dict[str, object]:
    owner_private = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    successor_private = Ed25519PrivateKey.from_private_bytes(bytes(range(65, 97)))
    node_private = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
    owner = _sign_document(
        "owner-key-certificate",
        {
            "schema_version": 1,
            "brain_id": "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "owner_key_id": "key_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "owner_public_key": _public_key(owner_private),
            "owner_epoch": 1,
            "previous_owner_key_id": None,
            "valid_from": genesis_valid_from,
            "retired_at": genesis_retired_at,
            "certifying_owner_key_id": "key_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
        owner_private,
    )
    owner_history = [owner]
    certifier_private = owner_private
    certifier_key_id = "key_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    if successor_valid_from is not None:
        successor = _sign_document(
            "owner-key-certificate",
            {
                "schema_version": 1,
                "brain_id": "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa",
                "owner_key_id": "key_cccccccccccccccccccccccccc",
                "owner_public_key": _public_key(successor_private),
                "owner_epoch": 2,
                "previous_owner_key_id": "key_aaaaaaaaaaaaaaaaaaaaaaaaaa",
                "valid_from": successor_valid_from,
                "retired_at": None,
                "certifying_owner_key_id": "key_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            },
            owner_private,
        )
        owner_history.append(successor)
        certifier_private = successor_private
        certifier_key_id = "key_cccccccccccccccccccccccccc"

    node_certificate = _sign_document(
        "node-epoch-certificate",
        {
            "schema_version": 1,
            "brain_id": "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "node_id": "nod_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "node_key_id": "key_bbbbbbbbbbbbbbbbbbbbbbbbbb",
            "node_public_key": _public_key(node_private),
            "sequencer_epoch": 1,
            "prior_ledger_head": None,
            "owner_key_id": certifier_key_id,
            "issued_at": node_issued_at,
        },
        certifier_private,
    )
    return _sign_document(
        "receipt",
        {
            "schema_version": 1,
            "receipt_id": "rcp_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "brain_id": "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "commit_id": "cmt_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "delivery_id": "dlv_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            "cursor": "cur_v1_aaaaaaaaaaaaaaaa",
            "commit_digest": "a" * 64,
            "issuer_epoch": 1,
            "policy_digest": "a" * 64,
            "sequencer_epoch": 1,
            "node_key_id": "key_bbbbbbbbbbbbbbbbbbbbbbbbbb",
            "node_epoch_certificate": node_certificate,
            "owner_key_history": owner_history,
            "issued_at": node_issued_at,
        },
        node_private,
    )


def _verify_temporal_receipt_signatures(receipt: dict[str, object]) -> None:
    owner_keys: dict[object, Ed25519PublicKey] = {}
    history = cast(list[dict[str, object]], receipt["owner_key_history"])
    for owner in history:
        owner_key = Ed25519PublicKey.from_public_bytes(
            decode_base64url(
                owner["owner_public_key"],
                expected_bytes=32,
                label="owner_public_key",
            )
        )
        certifier = owner_key if owner["owner_epoch"] == 1 else owner_keys[
            owner["certifying_owner_key_id"]
        ]
        certifier.verify(
            decode_base64url(
                owner["owner_signature"],
                expected_bytes=64,
                label="owner_signature",
            ),
            signed_payload_bytes("owner-key-certificate", owner),
        )
        owner_keys[owner["owner_key_id"]] = owner_key

    node = cast(dict[str, object], receipt["node_epoch_certificate"])
    owner_keys[node["owner_key_id"]].verify(
        decode_base64url(
            node["owner_signature"],
            expected_bytes=64,
            label="owner_signature",
        ),
        signed_payload_bytes("node-epoch-certificate", node),
    )
    node_key = Ed25519PublicKey.from_public_bytes(
        decode_base64url(
            node["node_public_key"],
            expected_bytes=32,
            label="node_public_key",
        )
    )
    node_key.verify(
        decode_base64url(
            receipt["node_signature"],
            expected_bytes=64,
            label="node_signature",
        ),
        signed_payload_bytes("receipt", receipt),
    )


def test_language_neutral_principal_signature_vector_verifies() -> None:
    vector = load_signature_vectors()[0]
    binding = vector["request_binding"]
    canonical = canonical_json_bytes(binding)

    assert canonical.decode() == vector["canonical_request_binding"]
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(vector["principal_public_key"] + "==")
    )
    public_key.verify(
        base64.urlsafe_b64decode(vector["principal_signature"] + "=="),
        canonical,
    )


def test_signature_vector_rejects_another_principal_and_changed_binding() -> None:
    vector = load_signature_vectors()[0]
    signature = base64.urlsafe_b64decode(vector["principal_signature"] + "==")
    canonical = canonical_json_bytes(vector["request_binding"])
    other_principal = Ed25519PrivateKey.from_private_bytes(bytes(range(2, 34))).public_key()

    with pytest.raises(InvalidSignature):
        other_principal.verify(signature, canonical)

    changed_binding = dict(vector["request_binding"])
    changed_binding["body_digest"] = "b" * 64
    original_principal = Ed25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(vector["principal_public_key"] + "==")
    )
    with pytest.raises(InvalidSignature):
        original_principal.verify(signature, canonical_json_bytes(changed_binding))


def test_every_signed_contract_has_a_reproducible_language_neutral_vector() -> None:
    vectors = load_signature_vectors()[1:]
    cases = load_conformance_cases()["valid"]

    assert {vector["contract"] for vector in vectors} == set(SIGNATURE_FIELDS)
    for vector in vectors:
        contract = cast(str, vector["contract"])
        document = cast(dict[str, object], vector["document"])
        canonical = signed_payload_bytes(contract, document)
        signature_field = SIGNATURE_FIELDS[contract]

        assert hashlib.sha256(canonical).hexdigest() == vector["signed_payload_sha256"]
        assert document in cases[contract]
        validate_protocol_semantics(contract, document)
        public_key = Ed25519PublicKey.from_public_bytes(
            decode_base64url(
                vector["verification_public_key"],
                expected_bytes=32,
                label="verification_public_key",
            )
        )
        public_key.verify(
            decode_base64url(document[signature_field], expected_bytes=64, label=signature_field),
            canonical,
        )


def test_receipt_chain_anchors_node_signature_to_pinned_owner_fingerprint() -> None:
    vectors = {vector["case"]: vector for vector in load_signature_vectors()}
    owner = cast(dict[str, object], vectors["owner-key-genesis-certificate-v1"]["document"])
    node = cast(dict[str, object], vectors["owner-signed-node-epoch-certificate-v1"]["document"])
    receipt = cast(dict[str, object], vectors["node-signed-receipt-chain-v1"]["document"])
    owner_public_key = decode_base64url(
        owner["owner_public_key"], expected_bytes=32, label="owner_public_key"
    )

    assert hashlib.sha256(owner_public_key).hexdigest() == vectors[
        "owner-key-genesis-certificate-v1"
    ]["pinned_public_key_sha256"]
    assert receipt["owner_key_history"] == [owner]
    assert receipt["node_epoch_certificate"] == node

    owner_verifier = Ed25519PublicKey.from_public_bytes(owner_public_key)
    owner_verifier.verify(
        decode_base64url(owner["owner_signature"], expected_bytes=64, label="owner_signature"),
        signed_payload_bytes("owner-key-certificate", owner),
    )
    owner_verifier.verify(
        decode_base64url(node["owner_signature"], expected_bytes=64, label="owner_signature"),
        signed_payload_bytes("node-epoch-certificate", node),
    )
    node_verifier = Ed25519PublicKey.from_public_bytes(
        decode_base64url(node["node_public_key"], expected_bytes=32, label="node_public_key")
    )
    node_verifier.verify(
        decode_base64url(receipt["node_signature"], expected_bytes=64, label="node_signature"),
        signed_payload_bytes("receipt", receipt),
    )

    wrong_key = dict(receipt)
    wrong_key["node_key_id"] = "key_cccccccccccccccccccccccccc"
    with pytest.raises(ProtocolContractError, match="key IDs"):
        validate_protocol_semantics("receipt", wrong_key)


@pytest.mark.parametrize(
    ("genesis_retired_at", "successor_valid_from", "node_issued_at"),
    (
        pytest.param("2026-09-06T12:30:00Z", None, "2026-09-06T12:00:00Z", id="valid-from"),
        pytest.param(
            "2026-09-06T12:30:00Z",
            "2026-09-06T12:30:00Z",
            "2026-09-06T12:30:00Z",
            id="adjacent-rotation",
        ),
        pytest.param(
            "2026-09-06T12:30:00Z",
            "2026-09-06T12:45:00Z",
            "2026-09-06T12:45:00Z",
            id="rotation-gap",
        ),
    ),
)
def test_receipt_owner_certification_accepts_half_open_interval_boundaries(
    genesis_retired_at: str,
    successor_valid_from: str | None,
    node_issued_at: str,
) -> None:
    receipt = _temporal_receipt(
        genesis_valid_from="2026-09-06T12:00:00Z",
        genesis_retired_at=genesis_retired_at,
        successor_valid_from=successor_valid_from,
        node_issued_at=node_issued_at,
    )

    _verify_temporal_receipt_signatures(receipt)
    validate_protocol_semantics("receipt", receipt)


@pytest.mark.parametrize(
    (
        "genesis_valid_from",
        "genesis_retired_at",
        "successor_valid_from",
        "node_issued_at",
        "message",
    ),
    (
        pytest.param(
            "2026-09-06T12:10:00Z",
            None,
            None,
            "2026-09-06T12:09:59Z",
            "predates its certifying owner",
            id="node-before-owner-validity",
        ),
        pytest.param(
            "2026-09-06T12:00:00Z",
            "2026-09-06T12:30:00Z",
            None,
            "2026-09-06T12:30:00Z",
            "outside its certifying owner",
            id="node-at-owner-retirement",
        ),
        pytest.param(
            "2026-09-06T12:00:00Z",
            "2026-09-06T12:30:00Z",
            None,
            "2026-09-06T12:30:01Z",
            "outside its certifying owner",
            id="node-after-owner-retirement",
        ),
        pytest.param(
            "2026-09-06T12:00:00Z",
            "2026-09-06T12:30:00Z",
            "2026-09-06T11:59:59Z",
            "2026-09-06T12:31:00Z",
            "valid_from values must strictly increase",
            id="backdated-rotation",
        ),
        pytest.param(
            "2026-09-06T12:00:00Z",
            None,
            "2026-09-06T12:30:00Z",
            "2026-09-06T12:30:00Z",
            "must be retired",
            id="active-predecessor",
        ),
        pytest.param(
            "2026-09-06T12:00:00Z",
            "2026-09-06T13:00:00Z",
            "2026-09-06T12:30:00Z",
            "2026-09-06T12:30:00Z",
            "must not overlap",
            id="overlapping-owner-intervals",
        ),
    ),
)
def test_receipt_owner_certification_rejects_validly_signed_invalid_chronology(
    genesis_valid_from: str,
    genesis_retired_at: str | None,
    successor_valid_from: str | None,
    node_issued_at: str,
    message: str,
) -> None:
    receipt = _temporal_receipt(
        genesis_valid_from=genesis_valid_from,
        genesis_retired_at=genesis_retired_at,
        successor_valid_from=successor_valid_from,
        node_issued_at=node_issued_at,
    )

    _verify_temporal_receipt_signatures(receipt)
    with pytest.raises(ProtocolContractError, match=message):
        validate_protocol_semantics("receipt", receipt)


def test_cold_transfer_is_bound_to_the_node_stop_proof() -> None:
    vectors = {vector["case"]: vector for vector in load_signature_vectors()}
    receipt = cast(dict[str, object], vectors["node-signed-receipt-chain-v1"]["document"])
    transfer = cast(dict[str, object], vectors["owner-signed-cold-transfer-v1"]["document"])
    stop = cast(dict[str, object], vectors["node-signed-sequencer-stop-proof-v1"]["document"])

    expected_head = {"history_commitment": ledger_history_commitment(receipt)}
    assert transfer["prior_ledger_head"] == expected_head
    assert stop["last_ledger_head"] == expected_head
    validate_cold_transfer_pair(transfer, stop)

    changed_stop = dict(stop)
    changed_stop["epoch"] = 2
    with pytest.raises(ProtocolContractError, match="previous_epoch"):
        validate_cold_transfer_pair(transfer, changed_stop)
