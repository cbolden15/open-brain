from __future__ import annotations

import json

import pytest

from open_brain.services import local_native_entrypoint


def test_source_self_check_fails_without_touching_local_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert local_native_entrypoint.main(("__open-brain-self-check",)) == 1
    output = json.loads(capsys.readouterr().out)

    assert output == {
        "daemon_running": False,
        "frozen": False,
        "profile": "local",
        "status": "failed",
        "version": "0.1.0",
    }


def test_native_entrypoint_routes_normal_commands_to_local_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_run_cli(arguments: tuple[str, ...]) -> int:
        observed.append(arguments)
        return 7

    monkeypatch.setattr(local_native_entrypoint, "run_cli", fake_run_cli)

    assert local_native_entrypoint.main(("--version",)) == 7
    assert observed == [("--version",)]
