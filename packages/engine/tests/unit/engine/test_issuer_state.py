from __future__ import annotations

from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import ValidationError
from open_brain_engine.engine.issuer_state import (
    derive_legacy_bindings,
    legacy_binding_manifest_sha256,
    synthetic_cutover_manifest,
)
from open_brain_engine.portable.v4 import manifest_v4

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
CUTOVER_CREATED_AT = "1970-01-01T00:00:00Z"


def _cutover_export_id(files: dict[str, bytes], tenant_id: str) -> str:
    inventory = [
        {"path": path, "sha256": sha256(payload).hexdigest()}
        for path, payload in sorted(files.items())
    ]
    inventory_digest = sha256(portable_canonical_json_bytes(inventory)).hexdigest()
    return "export_" + str(
        uuid5(
            NAMESPACE_URL,
            "open-brain-cutover-manifest-v1:" + tenant_id + ":" + inventory_digest,
        )
    )


def test_synthetic_cutover_manifest_is_exact_and_deterministic() -> None:
    files = {
        "brain.toml": b"[brain]\n",
        "content/notes/one.md": b"one\n",
        "history/captures/1.json": b"{}\n",
        "sources/captures/a.jsonl": b'{"a":1}\n\n{"b":2}\n',
    }
    manifest = synthetic_cutover_manifest(files, tenant_id=TENANT_ID)

    assert manifest == manifest_v4(
        files,
        tenant_id=TENANT_ID,
        export_id=_cutover_export_id(files, TENANT_ID),
        created_at=CUTOVER_CREATED_AT,
    )
    assert manifest["created_at"] == CUTOVER_CREATED_AT
    assert synthetic_cutover_manifest(files, tenant_id=TENANT_ID) == manifest


def test_legacy_bindings_cover_every_declared_non_jsonl_file_once() -> None:
    files = {
        "brain.toml": b"[brain]\n",
        "content/notes/one.md": b"one\n",
        "sources/captures/a.jsonl": b'{"a":1}\n',
    }
    bindings = derive_legacy_bindings(files)

    assert list(bindings) == [
        ("brain.toml", None, sha256(b"[brain]\n").hexdigest()),
        ("content/notes/one.md", None, sha256(b"one\n").hexdigest()),
        ("sources/captures/a.jsonl", 0, sha256(b'{"a":1}').hexdigest()),
    ]


def test_legacy_bindings_cover_nonempty_jsonl_rows_by_zero_based_ordinal() -> None:
    files = {
        "sources/captures/a.jsonl": b'{"a":1}\n\n{"b":2}\n',
        "sources/captures/empty.jsonl": b"",
    }
    bindings = derive_legacy_bindings(files)

    assert list(bindings) == [
        ("sources/captures/a.jsonl", 0, sha256(b'{"a":1}').hexdigest()),
        ("sources/captures/a.jsonl", 2, sha256(b'{"b":2}').hexdigest()),
    ]


def test_legacy_binding_manifest_digest_covers_epoch_and_sorted_bindings() -> None:
    bindings = (
        ("brain.toml", None, sha256(b"[brain]\n").hexdigest()),
        ("sources/captures/a.jsonl", 0, sha256(b'{"a":1}').hexdigest()),
    )
    payload = [
        {
            "artifact_path": path,
            "issuer_epoch": 1,
            "jsonl_ordinal": ordinal,
            "payload_sha256": digest,
        }
        for path, ordinal, digest in bindings
    ]

    assert legacy_binding_manifest_sha256(bindings, issuer_epoch=1) == sha256(
        portable_canonical_json_bytes(payload)
    ).hexdigest()
    assert legacy_binding_manifest_sha256(bindings, issuer_epoch=1) != (
        legacy_binding_manifest_sha256(bindings, issuer_epoch=2)
    )


def test_validate_issuer_digest_requires_lowercase_hex_shape() -> None:
    from open_brain_engine.engine.issuer_state import validate_issuer_digest

    digest = sha256(b"evidence").hexdigest()

    assert validate_issuer_digest(digest) == digest
    for invalid in (
        digest.upper(),
        digest[:-1],
        "z" + digest[1:],
        "",
        64,
        None,
    ):
        with pytest.raises(ValidationError):
            validate_issuer_digest(invalid)
