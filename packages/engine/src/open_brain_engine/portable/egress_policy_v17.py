"""Portable17 ``ob.owner-egress-policy.v1`` evidence: canonical JSONL policy history.

The family is the complete retained transition history. Restore installs it and
makes its latest row the destination's active policy, so effective privacy
projected under the policy round-trips. Exported privacy projections stay the
policy-independent base; the policy overlay is re-derived from this history.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .v1 import PortableValidationError

OWNER_EGRESS_POLICY_CONTRACT = "ob.owner-egress-policy.v1"
OWNER_EGRESS_POLICY_PATH = "history/owner-egress-policy/owner-egress-policy.jsonl"
_ROW_KEYS = frozenset({"contract", "operation_id", "recorded_at", "tiers", "version"})
_TIERS = ("personal", "public", "work")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
_MAX_OPERATION_ID = 128


def _row(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _ROW_KEYS:
        raise ValueError("row keys")
    version, tiers = value["version"], value["tiers"]
    operation, recorded = value["operation_id"], value["recorded_at"]
    if (
        value["contract"] != OWNER_EGRESS_POLICY_CONTRACT
        or type(version) is not int
        or version < 1
        or type(tiers) is not list
        or any(type(tier) is not str or tier not in _TIERS for tier in tiers)
        or tiers != sorted(set(tiers))
        or type(operation) is not str
        or not 1 <= len(operation) <= _MAX_OPERATION_ID
        or type(recorded) is not str
        or _TIMESTAMP.fullmatch(recorded) is None
    ):
        raise ValueError("row value")
    return dict(value)


def validate_history_rows(rows: object) -> list[dict[str, object]]:
    """Exact typed rows with contiguous versions from one and unique operations."""
    if type(rows) is not list:
        raise PortableValidationError("Portable17 owner egress policy evidence invalid")
    try:
        validated = [_row(row) for row in rows]
    except ValueError as error:
        raise PortableValidationError(
            "Portable17 owner egress policy evidence invalid"
        ) from error
    if [row["version"] for row in validated] != list(range(1, len(validated) + 1)):
        raise PortableValidationError("Portable17 owner egress policy versions are not monotone")
    if len({row["operation_id"] for row in validated}) != len(validated):
        raise PortableValidationError("Portable17 owner egress policy operations repeat")
    return validated


def owner_egress_policy_sidecar(history: list[dict[str, object]]) -> bytes:
    rows = validate_history_rows(
        [{**row, "contract": OWNER_EGRESS_POLICY_CONTRACT} for row in history]
    )
    return b"".join(canonical(row) + b"\n" for row in rows)


def validate_owner_egress_policy_evidence(files: Mapping[str, bytes]) -> list[dict[str, object]]:
    """Decode the required family; every line is one canonical row."""
    try:
        raw = files[OWNER_EGRESS_POLICY_PATH]
    except KeyError:
        raise PortableValidationError(
            "Portable17 owner egress policy evidence missing"
        ) from None
    if raw and not raw.endswith(b"\n"):
        raise PortableValidationError("Portable17 owner egress policy evidence invalid")
    rows: list[object] = []
    for line in raw.split(b"\n")[:-1] if raw else []:
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise PortableValidationError(
                "Portable17 owner egress policy evidence invalid"
            ) from error
        if canonical(value) != line:
            raise PortableValidationError(
                "Portable17 owner egress policy evidence is not canonical"
            )
        rows.append(value)
    return validate_history_rows(rows)


__all__ = [
    "OWNER_EGRESS_POLICY_CONTRACT",
    "OWNER_EGRESS_POLICY_PATH",
    "owner_egress_policy_sidecar",
    "validate_history_rows",
    "validate_owner_egress_policy_evidence",
]
