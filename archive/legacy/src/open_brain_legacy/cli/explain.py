"""Static no-network policy explanation for public CLI callers."""

from __future__ import annotations

import json
from dataclasses import dataclass

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error


@dataclass(frozen=True, slots=True)
class ExplainCliResult:
    """A deterministic policy explanation result."""

    exit_code: ExitCode
    envelope: dict[str, object]

    def to_json(self) -> str:
        """Serialize the stable automation response."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


def explain_policy(policy: str) -> ExplainCliResult:
    """Explain only the closed no-network policy and fail closed otherwise."""
    if policy != "no-network":
        return ExplainCliResult(
            exit_code=ExitCode.FAILURE,
            envelope={
                "command": "explain",
                "error": redacted_error("policy_explanation_unavailable"),
                "status": "failed",
            },
        )
    return ExplainCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "command": "explain",
            "network_access": "denied",
            "policy": "no-network",
            "status": "ok",
        },
    )
