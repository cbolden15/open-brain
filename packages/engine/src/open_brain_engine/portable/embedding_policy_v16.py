"""Portable16 ``ob.embedding-policy.v1`` evidence: canonical JSONL owner policy history.

The family is retained transition evidence only. Restore installs it and always
leaves the destination's local embedding policy disabled.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .v1 import PortableValidationError

EMBEDDING_POLICY_CONTRACT = "ob.embedding-policy.v1"
EMBEDDING_POLICY_PATH = "history/embedding-policy/embedding-policy.jsonl"
_ROW_KEYS = frozenset({"action", "changed_at", "component_id", "contract", "version"})
_COMPONENT = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
_MAX_VERSION = 9007199254740991


def _row(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _ROW_KEYS:
        raise ValueError("row keys")
    version, action, component = value["version"], value["action"], value["component_id"]
    changed = value["changed_at"]
    if (
        value["contract"] != EMBEDDING_POLICY_CONTRACT
        or type(version) is not int
        or not 1 <= version <= _MAX_VERSION
        or action not in {"enable", "disable"}
        or (action == "disable" and component is not None)
        or (
            action == "enable"
            and (type(component) is not str or _COMPONENT.fullmatch(component) is None)
        )
        or type(changed) is not str
        or _TIMESTAMP.fullmatch(changed) is None
    ):
        raise ValueError("row value")
    return dict(value)


def validate_history_rows(rows: object) -> list[dict[str, object]]:
    """Exact typed rows with strictly increasing consent versions."""
    if type(rows) is not list:
        raise PortableValidationError("Portable16 embedding policy evidence invalid")
    try:
        validated = [_row(row) for row in rows]
    except ValueError as error:
        raise PortableValidationError("Portable16 embedding policy evidence invalid") from error
    versions = [row["version"] for row in validated]
    if versions != sorted(set(versions)):  # type: ignore[type-var]
        raise PortableValidationError("Portable16 embedding policy versions are not monotone")
    return validated


def embedding_policy_sidecar(history: list[dict[str, object]]) -> bytes:
    rows = validate_history_rows(
        [{**row, "contract": EMBEDDING_POLICY_CONTRACT} for row in history]
    )
    return b"".join(canonical(row) + b"\n" for row in rows)


def validate_embedding_policy_evidence(files: Mapping[str, bytes]) -> list[dict[str, object]]:
    """Decode the required family; every line is one canonical row."""
    try:
        raw = files[EMBEDDING_POLICY_PATH]
    except KeyError:
        raise PortableValidationError("Portable16 embedding policy evidence missing") from None
    if raw and not raw.endswith(b"\n"):
        raise PortableValidationError("Portable16 embedding policy evidence invalid")
    rows: list[object] = []
    for line in raw.split(b"\n")[:-1] if raw else []:
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise PortableValidationError("Portable16 embedding policy evidence invalid") from error
        if canonical(value) != line:
            raise PortableValidationError("Portable16 embedding policy evidence is not canonical")
        rows.append(value)
    return validate_history_rows(rows)
