from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from open_brain_engine.protocol import canonical_json_bytes, load_signature_vectors


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
