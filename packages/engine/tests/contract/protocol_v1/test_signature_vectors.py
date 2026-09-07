from __future__ import annotations

import base64
import hashlib
from typing import cast

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from open_brain_engine.protocol import (
    ProtocolContractError,
    canonical_json_bytes,
    decode_base64url,
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


def test_cold_transfer_is_bound_to_the_node_stop_proof() -> None:
    vectors = {vector["case"]: vector for vector in load_signature_vectors()}
    transfer = cast(dict[str, object], vectors["owner-signed-cold-transfer-v1"]["document"])
    stop = cast(dict[str, object], vectors["node-signed-sequencer-stop-proof-v1"]["document"])

    validate_cold_transfer_pair(transfer, stop)

    changed_stop = dict(stop)
    changed_stop["epoch"] = 2
    with pytest.raises(ProtocolContractError, match="previous_epoch"):
        validate_cold_transfer_pair(transfer, changed_stop)
