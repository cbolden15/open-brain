from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "docs" / "architecture" / "m0-contract-manifest.json"
EXPECTED_DECISIONS = {
    f"M0-ADR-{number:04d}": f"docs/architecture/decisions/{name}"
    for number, name in enumerate(
        (
            "0001-brain-protocol-v1.md",
            "0002-brain-and-record-identity.md",
            "0003-idempotency-and-conflict-semantics.md",
            "0004-capability-authorization.md",
            "0005-information-flow-labels.md",
            "0006-provenance-closed-erasure.md",
            "0007-single-sequencer-fencing.md",
            "0008-rebuildable-projections.md",
            "0009-brainpack-v2.md",
        ),
        start=1,
    )
}
SHA256 = re.compile(r"[0-9a-f]{64}")
EXPECTED_MANIFEST_FIELDS = {
    "schema_version",
    "bundle_id",
    "status",
    "accepted_date",
    "hash_algorithm",
    "byte_contract",
    "decisions",
}


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_manifest(text: str) -> dict[str, Any]:
    value = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    if not isinstance(value, dict):
        raise TypeError("M0 contract manifest must be an object")
    return cast(dict[str, Any], value)


def _manifest() -> dict[str, Any]:
    return _load_manifest(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_m0_manifest_rejects_duplicate_json_keys() -> None:
    duplicate = '{"decision":{"source_sha256":"first","source_sha256":"second"}}'

    with pytest.raises(ValueError, match="duplicate JSON key: source_sha256"):
        _load_manifest(duplicate)


def test_m0_manifest_binds_exact_public_adr_bytes() -> None:
    manifest = _manifest()

    assert set(manifest) == EXPECTED_MANIFEST_FIELDS
    assert manifest["schema_version"] == 1
    assert manifest["bundle_id"] == "open-brain-m0-architecture-decisions"
    assert manifest["status"] == "accepted"
    assert manifest["accepted_date"] == "2026-09-04"
    assert manifest["hash_algorithm"] == "sha256"
    assert manifest["byte_contract"] == "exact raw UTF-8 Git blob bytes"

    decisions = manifest["decisions"]
    assert isinstance(decisions, list)
    assert len(decisions) == len(EXPECTED_DECISIONS)
    assert [
        (item["decision_id"], item["public_path"])
        for item in decisions
        if isinstance(item, dict)
    ] == list(EXPECTED_DECISIONS.items())

    for item in decisions:
        assert isinstance(item, dict)
        assert set(item) == {
            "decision_id",
            "public_path",
            "source_sha256",
            "public_sha256",
        }
        public_path = ROOT / item["public_path"]
        digest = hashlib.sha256(public_path.read_bytes()).hexdigest()
        assert SHA256.fullmatch(item["source_sha256"])
        assert digest == item["source_sha256"] == item["public_sha256"]


def test_m0_public_lineage_contains_no_private_location() -> None:
    public_text = json.dumps(_manifest(), ensure_ascii=False) + "".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in EXPECTED_DECISIONS.values()
    )
    lowered = public_text.casefold()

    for forbidden in ("/users/", "source_repository", "source_path"):
        assert forbidden not in lowered
