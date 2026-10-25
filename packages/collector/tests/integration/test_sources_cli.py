from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast

import pytest

from open_brain.profile import compile_single_user_local
from open_brain_collector import sources_cli


def _cli(argv: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = sources_cli.main(argv)
    return code, cast(dict[str, object], json.loads(output.getvalue()))


def test_resources_resource_type_defaults_to_drive_file_and_forwards_drive_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Manager:
        def __init__(self, *_args: object) -> None:
            pass

        def dispatch(self, operation: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((operation, arguments))
            return {"resources": [], "next_cursor": None}

    monkeypatch.setattr(sources_cli, "LiveSourceManager", Manager)
    base = [
        "--state",
        str(tmp_path / "state.json"),
        "--brain-root",
        str(tmp_path / "brain"),
        "--foreground",
        "resources",
        "--connection-id",
        "account:abc",
    ]
    assert _cli([*base, "--provider", "google_drive"]) == (
        0,
        {"resources": [], "next_cursor": None},
    )
    folders = [*base, "--provider", "google_drive", "--resource-type", "drive_folder"]
    assert _cli([*folders, "--cursor", "page-2"])[0] == 0
    assert _cli([*base, "--provider", "gmail"])[0] == 0
    assert calls == [
        (
            "sources.resources",
            {
                "provider": "google_drive",
                "connection_id": "account:abc",
                "resource_type": "drive_file",
            },
        ),
        (
            "sources.resources",
            {
                "provider": "google_drive",
                "connection_id": "account:abc",
                "cursor": "page-2",
                "resource_type": "drive_folder",
            },
        ),
        ("sources.resources", {"provider": "gmail", "connection_id": "account:abc"}),
    ]
    # drive_account is a fixed id, never a listing kind: argparse refuses it.
    with pytest.raises(SystemExit):
        sources_cli.main([*base, "--provider", "google_drive", "--resource-type", "drive_account"])
    assert len(calls) == 3


def test_resources_resource_type_is_rejected_for_other_providers(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    state = brain / "collector" / "state.json"
    code, result = _cli(
        [
            "--state",
            str(state),
            "--brain-root",
            str(brain),
            "--foreground",
            "resources",
            "--provider",
            "gmail",
            "--connection-id",
            "account:abc",
            "--resource-type",
            "drive_folder",
        ]
    )
    assert (code, result) == (
        78,
        {"outcome": "failed", "failure_code": "source_invalid_arguments"},
    )
