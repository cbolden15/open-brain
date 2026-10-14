"""Portable11 commitments to the complete immutable development evidence graph."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

MAX_DEVELOPMENT_DEPENDENCIES = 1024


def development_closure(
    capture_ids: Sequence[str], lookup: Callable[[str], Mapping[str, Any] | None],
) -> list[dict[str, Any]]:
    """Commit present receipts and absent receipts; reject cycles and oversized graphs."""
    commitments: dict[str, str | None] = {}
    active: set[str] = set()
    pending = [(identity, False) for identity in reversed(capture_ids)]
    while pending:
        identity, leaving = pending.pop()
        if leaving:
            active.remove(identity)
            continue
        if identity in active:
            raise ValueError("development evidence cycle")
        if identity in commitments:
            continue
        if len(commitments) >= MAX_DEVELOPMENT_DEPENDENCIES:
            raise ValueError("development evidence dependency limit")
        row = lookup(identity)
        commitments[identity] = None if row is None else sha256(canonical(dict(row))).hexdigest()
        if row is None:
            continue
        receipt = json.loads(row["receipt_json"])
        active.add(identity)
        pending.append((identity, True))
        dependencies = (
            receipt["supporting_capture_ids"] + receipt["supports_development_ids"]
            + receipt["supersedes_development_ids"]
        )
        pending.extend((dependency, False) for dependency in reversed(dependencies))
    return [
        {"capture_id": identity, "evidence_sha256": digest}
        for identity, digest in sorted(commitments.items())
    ]
