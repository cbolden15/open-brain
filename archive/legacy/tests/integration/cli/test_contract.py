from __future__ import annotations

import json

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli._registry import command_names
from open_brain_legacy.cli.main import main


@pytest.mark.parametrize("command", command_names())
def test_every_registered_command_fails_closed_when_its_adapter_is_unavailable(
    command: str, capsys: object
) -> None:
    assert main(["--json", command, "token=synthetic-secret"]) == ExitCode.FAILURE

    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output == {
        "command": command,
        "error": {
            "code": "command_adapter_unavailable",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "unavailable",
    }
    assert "synthetic-secret" not in json.dumps(output)


def test_unknown_command_returns_a_redacted_usage_envelope(capsys: object) -> None:
    assert main(["--json", "not-a-command", "token=synthetic-secret"]) == ExitCode.USAGE

    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output == {
        "error": {
            "code": "invalid_command",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "invalid",
    }
