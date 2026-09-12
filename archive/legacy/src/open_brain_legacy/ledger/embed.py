"""Deterministic local embeddings for typed ledger claims."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import replace
from hashlib import sha256

from .index import ClaimRecord

_TOKEN = re.compile(r"[a-z0-9]+")


def embed_text(text: str, *, dimensions: int) -> tuple[float, ...]:
    if (
        not isinstance(text, str)
        or not text
        or not isinstance(dimensions, int)
        or isinstance(dimensions, bool)
        or not 8 <= dimensions <= 1024
    ):
        raise ValueError("invalid local embedding input")
    tokens = _TOKEN.findall(unicodedata.normalize("NFC", text).casefold())
    if not tokens:
        raise ValueError("invalid local embedding input")
    values = [0.0] * dimensions
    for token in tokens:
        digest = sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        values[index] += 1.0 if digest[4] & 1 else -1.0
    magnitude = math.sqrt(sum(component * component for component in values))
    if magnitude == 0:
        raise ValueError("invalid local embedding input")
    return tuple(component / magnitude for component in values)


def embed_claims(
    claims: tuple[ClaimRecord, ...], *, dimensions: int
) -> tuple[ClaimRecord, ...]:
    if not isinstance(claims, tuple) or any(
        not isinstance(claim, ClaimRecord) for claim in claims
    ):
        raise ValueError("invalid claims")
    embedded: list[ClaimRecord] = []
    for claim in claims:
        claim.validate()
        value = replace(claim, embedding=embed_text(claim.text, dimensions=dimensions))
        value.validate()
        embedded.append(value)
    return tuple(embedded)
