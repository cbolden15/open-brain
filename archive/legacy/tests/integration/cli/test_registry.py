from __future__ import annotations

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli._registry import CommandSpec
from open_brain_legacy.cli.config import show_registry


def test_registry_normalizes_policy_and_returns_nonzero_when_degraded() -> None:
    commands = (
        CommandSpec("status", "Status."),
        CommandSpec("query", "Query."),
    )

    healthy = show_registry(commands=commands, degraded=False)
    degraded = show_registry(commands=commands, degraded=True)

    assert healthy.exit_code is ExitCode.SUCCESS
    assert degraded.exit_code is ExitCode.FAILURE
    assert degraded.envelope == {
        "command": "registry",
        "commands": ["query", "status"],
        "policy": {
            "configuration": "explicit-only",
            "network": "disabled",
            "output": "redacted",
        },
        "status": "degraded",
    }
