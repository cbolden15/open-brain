import argparse
import json
import tomllib
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine import __version__

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli._registry import SCHEDULED_ROUTES
from open_brain_legacy.cli.main import build_parser, main
from open_brain_legacy.operations.models import ExitClass

ROOT = Path(__file__).resolve().parents[5]
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "phase0" / "public_cli.json"


def _fixture() -> dict[str, object]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("public CLI fixture must be an object")
    return cast(dict[str, object], value)


def _parser_surface() -> dict[str, object]:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    global_options = sorted(
        option
        for action in parser._actions
        if not isinstance(action, argparse._SubParsersAction)
        for option in action.option_strings
    )
    return {
        "command_families": sorted(subparsers.choices),
        "global_options": global_options,
    }


def _scheduled_surface() -> list[dict[str, object]]:
    return [
        {
            "job_id": route.job_id,
            "path": list(route.path),
            "options": sorted(route.options),
            "adapter": route.adapter,
        }
        for route in SCHEDULED_ROUTES
    ]


def _packaging_surface() -> dict[str, object]:
    packaging = tomllib.loads((ROOT / "packages/app/pyproject.toml").read_text())
    project = packaging["project"]
    return {
        "distribution": project["name"],
        "version": project["version"],
        "entry_points": project.get("scripts", {}),
    }


def _exit_surface() -> dict[str, dict[str, int]]:
    return {
        "interactive": {
            "success": int(ExitCode.SUCCESS),
            "failure": int(ExitCode.FAILURE),
            "usage": int(ExitCode.USAGE),
            "deferred": int(ExitCode.DEFERRED),
        },
        "scheduled": {
            "success": int(ExitClass.SUCCESS),
            "failure": int(ExitCode.FAILURE),
            "lock_held": int(ExitClass.LOCK_HELD),
            "configuration": int(ExitClass.CONFIGURATION),
        },
    }


def test_phase0_fixture_matches_current_public_cli_surface() -> None:
    fixture = _fixture()

    assert fixture["parser"] == _parser_surface()
    assert fixture["scheduled_routes"] == _scheduled_surface()
    assert fixture["packaging"] == _packaging_surface()
    assert fixture["exit_classes"] == _exit_surface()
    assert fixture["version_behavior"] == {
        "argv": ["--version"],
        "exit_code": 0,
        "output": f"open-brain {__version__}\n",
    }


def test_phase0_version_and_help_are_no_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    version_behavior = _fixture()["version_behavior"]
    assert isinstance(version_behavior, dict)

    assert main(["--version"]) == ExitCode.SUCCESS
    assert capsys.readouterr().out == version_behavior["output"]
    assert main(["--help"]) == ExitCode.SUCCESS
