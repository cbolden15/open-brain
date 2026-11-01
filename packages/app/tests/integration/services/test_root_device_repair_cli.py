"""A recorded root whose volume was renumbered (same folder, new st_dev) keeps working."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import BrainEngine, CaptureAction, TextPayload

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from packages.app.tests.unit.engine.test_source_migration import (
    _legacy_store_with_managed_workspace,
)

_STATE = ".open-brain/state/phase1.sqlite3"


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _shift_device(brain: Path, table: str) -> None:
    """Simulate macOS renumbering the volume: the folder and inode are unchanged."""
    connection = sqlite3.connect(brain / _STATE)
    with connection:
        changed = connection.execute(
            f"UPDATE {table} SET device=CAST(CAST(device AS INTEGER)+1 AS TEXT)"  # noqa: S608
        ).rowcount
    connection.close()
    assert changed == 1


def _export(
    brain: Path, destination: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    code = run_cli(
        ("export", str(destination), "--verify", "--json", "--data-dir", str(brain)),
        filesystem_type_probe=_filesystem,
    )
    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert code == 0, payload
    return payload


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def test_current_store_exports_after_the_workspace_volume_is_renumbered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    engine = BrainEngine.open(profile)
    engine.capture.accept(
        TextPayload("Managed body"),
        delivery_id="managed.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    workspace = _private_dir(tmp_path / "workspace")
    engine.managed_workspace.setup(str(workspace), operation_id="managed.setup")
    _shift_device(profile.root, "managed_workspaces")

    exported = _export(profile.root, _private_dir(tmp_path / "out") / "export", capsys)

    assert exported["verification"] == "verified"


def test_legacy_store_migrates_and_exports_after_the_workspace_volume_is_renumbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile, _workspace = _legacy_store_with_managed_workspace(tmp_path, monkeypatch)
    _shift_device(profile.root, "managed_workspaces")

    exported = _export(profile.root, _private_dir(tmp_path / "out") / "export", capsys)

    assert exported["verification"] == "verified"
    assert exported["captures"] == 1


def test_markdown_reimport_works_after_the_import_root_volume_is_renumbered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _private_dir(tmp_path / "home")
    environment = {"HOME": str(home)}
    brain = home / ".local/share/open-brain/brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nrenumbered-volume-token", encoding="utf-8")

    def import_vault(*extra: str) -> dict[str, object]:
        code = run_cli(
            ("import", str(vault), "--json", *extra),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
        assert code == 0, payload
        return payload

    assert import_vault("--yes")["imported"] == 1
    _shift_device(brain, "markdown_import_roots")

    assert import_vault()["unchanged"] == 1
