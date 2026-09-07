"""Opaque, role-distinct identifiers scoped by an explicit Brain ID."""

from __future__ import annotations

import base64
import re
import secrets
from types import MappingProxyType
from typing import Final, Literal

type IdentifierRole = Literal[
    "artifact",
    "brain",
    "commit",
    "decision",
    "delivery",
    "effect",
    "grant",
    "job",
    "key",
    "node",
    "nonce",
    "principal",
    "proposal",
    "purge",
    "receipt",
    "record",
    "revision",
    "stop_proof",
    "transfer",
]

IDENTIFIER_ROLES: Final = MappingProxyType(
    {
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
)
_TOKEN = re.compile(r"[a-z2-7]{26}")


def generate_identifier(role: IdentifierRole, *, brain_id: str | None = None) -> str:
    """Generate a random 128-bit identifier in the role's Brain namespace."""
    if role != "brain":
        _validate_brain_scope(brain_id)
    elif brain_id is not None:
        raise ValueError("a Brain identifier cannot be scoped by another Brain")
    token = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()
    return f"{IDENTIFIER_ROLES[role]}_{token}"


def validate_identifier(
    value: str,
    *,
    role: IdentifierRole,
    brain_id: str | None = None,
) -> str:
    """Validate role encoding and require an explicit Brain scope where applicable."""
    if role != "brain":
        _validate_brain_scope(brain_id)
    elif brain_id is not None:
        raise ValueError("a Brain identifier cannot be scoped by another Brain")
    prefix, separator, token = value.partition("_")
    if separator != "_" or prefix != IDENTIFIER_ROLES[role] or _TOKEN.fullmatch(token) is None:
        raise ValueError(f"invalid {role} identifier")
    return value


def _validate_brain_scope(brain_id: str | None) -> None:
    if brain_id is None:
        raise ValueError("Brain-scoped identifiers require brain_id")
    validate_identifier(brain_id, role="brain")
