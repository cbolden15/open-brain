"""Metadata-only status serializer for public CLI callers."""

from __future__ import annotations

import json
from dataclasses import dataclass

from open_brain_legacy.operations.status import StatusResult


@dataclass(frozen=True, slots=True)
class StatusCliResult:
    """A deterministic status envelope with the typed service exit code."""

    exit_code: int
    envelope: dict[str, object]

    def to_json(self) -> str:
        """Serialize metadata with stable key ordering."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


def show_status(*, result: StatusResult) -> StatusCliResult:
    """Serialize allow-listed status metadata without discovering runtime state."""
    return StatusCliResult(
        exit_code=result.exit_code,
        envelope={
            "command": "status",
            "metrics": [metric.to_dict() for metric in result.metrics],
            "schema_version": result.schema_version,
            "status": result.outcome.value,
            "strict": result.strict,
        },
    )
