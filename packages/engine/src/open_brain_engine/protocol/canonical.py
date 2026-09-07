"""RFC 8785 canonical bytes used by Brain Protocol v1 digests."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, cast

import rfc8785

LEDGER_HISTORY_COMMITMENT_PREFIX = "lhc_v1_"
_LEDGER_HISTORY_COMMITMENT_DOMAIN = b"open-brain-ledger-head-v1\0"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_non_finite_number(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def decode_protocol_json(payload: bytes) -> object:
    """Decode strict UTF-8 I-JSON while preserving duplicate-name rejection."""
    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite_number,
    )
    canonical_json_bytes(value)
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Return the one RFC 8785 encoding accepted by protocol v1."""
    return rfc8785.dumps(cast(Any, value))


def canonical_sha256(value: object) -> str:
    """Hash protocol data only after RFC 8785 canonicalization."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def ledger_history_commitment(receipt: object) -> str:
    """Commit to a complete signed receipt without serializing its commit digest."""
    digest = hashlib.sha256(
        _LEDGER_HISTORY_COMMITMENT_DOMAIN + canonical_json_bytes(receipt)
    ).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{LEDGER_HISTORY_COMMITMENT_PREFIX}{encoded}"
