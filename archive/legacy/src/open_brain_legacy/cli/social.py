"""Typed compatibility status for the native social-learning implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error


class SocialCompatibilityAction(StrEnum):
    """Compatibility actions retained for the predecessor-facing CLI."""

    RETAIN = "retain"
    RETIRE = "retire"


@dataclass(frozen=True, slots=True)
class SocialCliResult:
    """Deterministic defer metadata with no executable compatibility path."""

    exit_code: ExitCode
    envelope: dict[str, object]

    def to_json(self) -> str:
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


def compatibility(
    *,
    action: SocialCompatibilityAction,
    dry_run: bool,
) -> SocialCliResult:
    """Keep the native implementation and refuse retirement during stabilization."""
    if not isinstance(action, SocialCompatibilityAction) or not isinstance(dry_run, bool):
        return SocialCliResult(
            exit_code=ExitCode.USAGE,
            envelope={
                "command": "social.compatibility",
                "error": redacted_error("invalid_social_compatibility_request"),
                "status": "invalid",
            },
        )
    if action is SocialCompatibilityAction.RETIRE:
        return SocialCliResult(
            exit_code=ExitCode.FAILURE,
            envelope={
                "action": action.value,
                "command": "social.compatibility",
                "dry_run": dry_run,
                "error": redacted_error("predecessor_retirement_forbidden"),
                "status": "blocked",
            },
        )
    return SocialCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "action": action.value,
            "command": "social.compatibility",
            "disposition": "open-brain-live",
            "dry_run": dry_run,
            "status": "implementation-ready",
        },
    )
