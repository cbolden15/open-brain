"""RFC 8785 canonical bytes used by Brain Protocol v1 digests."""

from __future__ import annotations

import hashlib
from typing import Any, cast

import rfc8785


def canonical_json_bytes(value: object) -> bytes:
    """Return the one RFC 8785 encoding accepted by protocol v1."""
    return rfc8785.dumps(cast(Any, value))


def canonical_sha256(value: object) -> str:
    """Hash protocol data only after RFC 8785 canonicalization."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
