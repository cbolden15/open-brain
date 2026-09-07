from __future__ import annotations

import importlib
import json
import socket
from pathlib import Path
from typing import cast

import pytest

import open_brain.services.local_bootstrap as bootstrap_module
from open_brain.local_data import LocalDataError
from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _private_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    return home


def test_local_help_and_version_are_root_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(("--help",), environment={}) == 0
    assert "daemonless" in capsys.readouterr().out
    assert run_cli(("--version",), environment={}) == 0
    assert capsys.readouterr().out == "open-brain 0.1.0\n"


@pytest.mark.parametrize(
    ("platform_name", "expected_relative"),
    (
        ("darwin", "Library/Application Support/open-brain/brain"),
        ("linux", ".local/share/open-brain/brain"),
    ),
)
def test_local_init_creates_exact_default_once_without_daemon_or_environment_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    expected_relative: str,
) -> None:
    home = _private_home(tmp_path)
    forbidden_root = tmp_path / "must-not-be-used"

    def reject_listener(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("default bootstrap must not open a listener")

    monkeypatch.setattr(socket.socket, "bind", reject_listener)
    environment = {"HOME": str(home), "OPEN_BRAIN_ROOT": str(forbidden_root)}

    assert (
        run_cli(
            ("--json", "init"),
            environment=environment,
            platform_name=platform_name,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    first = cast(dict[str, object], json.loads(capsys.readouterr().out))
    brain_root = home / expected_relative
    identity = (brain_root / "brain.toml").read_bytes()

    assert first == {
        "application_encryption": False,
        "brain_count": 1,
        "daemon_running": False,
        "profile": "local",
        "state_schema_version": 1,
        "status": "initialized",
        "storage": "sqlite",
    }
    assert (brain_root / ".open-brain/state/phase1.sqlite3").is_file()
    assert not forbidden_root.exists()

    assert (
        run_cli(
            ("--json", "init"),
            environment=environment,
            platform_name=platform_name,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    second = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert second["status"] == "already_initialized"
    assert (brain_root / "brain.toml").read_bytes() == identity


def test_local_init_absolute_override_names_brain_root_directly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _private_home(tmp_path)
    brain_root = home / "selected"

    assert (
        run_cli(
            ("--json", "--data-dir", str(brain_root), "init"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "initialized"
    assert (brain_root / "brain.toml").is_file()
    assert not (brain_root / "brain").exists()


def test_local_init_redacts_private_data_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _private_home(tmp_path)

    assert (
        run_cli(
            ("--json", "--data-dir", "relative", "init"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    assert json.loads(capsys.readouterr().out) == {
        "error": {
            "code": "private_data_directory_unavailable",
            "message": "Open Brain could not use the private data directory.",
        },
        "status": "failed",
    }


def test_identity_revalidation_runs_inside_profile_before_first_identity_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _private_home(tmp_path)
    root = home / "brain"
    original = compile_single_user_local

    def compile_after_replacement(
        selected_root: Path,
        *,
        validate_before_identity_write: object,
    ) -> object:
        selected_root.rename(home / "pinned")
        selected_root.mkdir(mode=0o700)
        assert callable(validate_before_identity_write)
        validate_before_identity_write()
        return original(selected_root)

    monkeypatch.setattr(
        "open_brain.services.local_bootstrap.compile_single_user_local",
        compile_after_replacement,
    )
    selection = importlib.import_module("open_brain.local_data").select_local_root(
        data_dir=str(root),
        environment={"HOME": str(home)},
        platform_name="linux",
    )

    with pytest.raises(LocalDataError, match="changed"):
        bootstrap_module.initialize_local_brain(selection, filesystem_type_probe=_filesystem)
    assert not (root / "brain.toml").exists()
    assert not (home / "pinned/brain.toml").exists()


def test_sqlite_revalidation_runs_at_engine_write_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _private_home(tmp_path)
    root = home / "brain"

    def open_after_replacement(
        _profile: object, *, validate_before_write: object
    ) -> object:
        root.rename(home / "pinned")
        root.mkdir(mode=0o700)
        assert callable(validate_before_write)
        validate_before_write()
        raise AssertionError("unreachable")

    monkeypatch.setattr(bootstrap_module, "open_local_engine", open_after_replacement)
    selection = importlib.import_module("open_brain.local_data").select_local_root(
        data_dir=str(root),
        environment={"HOME": str(home)},
        platform_name="linux",
    )

    with pytest.raises(LocalDataError, match="changed"):
        bootstrap_module.initialize_local_brain(selection, filesystem_type_probe=_filesystem)
    assert not (root / ".open-brain/state/phase1.sqlite3").exists()
    assert not (home / "pinned/.open-brain/state/phase1.sqlite3").exists()
