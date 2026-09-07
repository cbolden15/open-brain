from __future__ import annotations

import importlib
import importlib.metadata
import json
import socket
import subprocess
import sys
import time
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
    help_output = capsys.readouterr().out
    assert "daemonless" in help_output
    for command in ("capture", "search", "export", "status", "doctor"):
        assert command in help_output
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
            ("init", "--json"),
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
            ("init", "--data-dir", str(brain_root), "--json"),
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


def test_exact_local_data_journey_bootstraps_without_init_or_background_runtime(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    forbidden_root = tmp_path / "must-not-be-used"
    environment = {"HOME": str(home), "OPEN_BRAIN_ROOT": str(forbidden_root)}
    token = "open-brain-five-minute-acceptance"

    def reject_listener(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("default commands must not open a listener")

    monkeypatch.setattr(socket.socket, "bind", reject_listener)

    assert (
        run_cli(
            ("capture", token, "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capture = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert capture["status"] == "captured"
    assert isinstance(capture["capture_id"], str)
    assert token not in json.dumps(capture)

    brain_root = home / ".local/share/open-brain/brain"
    assert (brain_root / "brain.toml").is_file()
    assert (brain_root / ".open-brain/state/phase1.sqlite3").is_file()
    assert not forbidden_root.exists()

    assert (
        run_cli(
            ("search", token),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert token in capsys.readouterr().out

    assert (
        run_cli(
            ("status", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    before_export = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert before_export == {
        "application_encryption": False,
        "brain_count": 1,
        "daemon_running": False,
        "portable_export": "absent",
        "profile": "local",
        "storage": "sqlite",
    }

    destination = tmp_path / "brain-export"
    assert (
        run_cli(
            ("export", str(destination), "--verify", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    exported = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert exported["status"] == "exported"
    assert exported["verification"] == "verified"
    assert (destination / "portable-manifest.json").is_file()
    assert any(
        token.encode("utf-8") in path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    )
    assert not any(".open-brain" in path.parts for path in destination.rglob("*"))
    assert not any(path.suffix in {".sqlite", ".sqlite3"} for path in destination.rglob("*"))

    assert (
        run_cli(
            ("status", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    after_export = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert after_export == before_export | {"portable_export": "verified"}

    for check in (
        "private-data-directory",
        "no-background-runtime",
        "base-dependency-closure",
    ):
        assert (
            run_cli(
                ("doctor", "--check", check),
                environment=environment,
                platform_name="linux",
                filesystem_type_probe=_filesystem,
            )
            == 0
        )
        assert capsys.readouterr().out == f"{check}: ok\n"

    assert not (brain_root / ".open-brain/run/control.sock").exists()


def test_local_search_json_is_bounded_and_export_failure_is_redacted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _private_home(tmp_path)
    environment = {"HOME": str(home)}
    token = "synthetic-local-search-token"

    assert (
        run_cli(
            ("--json", "capture", token),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        run_cli(
            ("search", token, "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    results = cast(list[dict[str, object]], payload["results"])
    assert payload["status"] == "ok"
    assert len(results) == 1
    assert results[0]["excerpt"] == token
    assert set(results[0]) == {
        "capture_id",
        "excerpt",
        "payload_family",
        "record_type",
        "result_id",
        "title",
        "trust",
    }

    destination = tmp_path / "conflicting-export"
    destination.mkdir()
    assert (
        run_cli(
            ("export", str(destination), "--verify", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    failure = capsys.readouterr().out
    assert json.loads(failure) == {
        "error": {
            "code": "local_operation_failed",
            "message": "Open Brain could not complete the local command.",
        },
        "status": "failed",
    }
    assert str(destination) not in failure
    assert token not in failure


def test_local_doctor_rejects_a_background_runtime_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _private_home(tmp_path)
    environment = {"HOME": str(home)}
    assert (
        run_cli(
            ("init",),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capsys.readouterr()
    marker = home / ".local/share/open-brain/brain/.open-brain/run/control.sock"
    marker.write_bytes(b"synthetic")
    marker.chmod(0o600)

    assert (
        run_cli(
            ("doctor", "--check", "no-background-runtime", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {
        "check": "no-background-runtime",
        "status": "failed",
    }

    token = "synthetic-runtime-conflict-private-text"
    assert (
        run_cli(
            ("capture", token, "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    failure = capsys.readouterr().out
    assert json.loads(failure) == {
        "error": {
            "code": "local_operation_failed",
            "message": "Open Brain could not complete the local command.",
        },
        "status": "failed",
    }
    assert token not in failure


@pytest.mark.parametrize(
    ("poisoned_distribution", "poisoned_requirement"),
    (
        ("open-brain", "starlette>=0.48,<1"),
        ("open-brain-engine", "cryptography>=50,<51"),
        ("rfc8785", "keyring>=25.6,<26"),
    ),
)
def test_local_dependency_doctor_rejects_an_unconditional_secure_node_dependency(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    poisoned_distribution: str,
    poisoned_requirement: str,
) -> None:
    home = _private_home(tmp_path)

    expected = {
        "open-brain": ["open-brain-engine==0.1.0"],
        "open-brain-engine": ["rfc8785<0.2,>=0.1.4"],
        "rfc8785": [],
    }

    def poisoned_requirements(distribution: str) -> list[str]:
        requirements = list(expected[distribution])
        if distribution == poisoned_distribution:
            requirements.append(poisoned_requirement)
        return requirements

    monkeypatch.setattr(
        importlib.metadata,
        "requires",
        poisoned_requirements,
    )

    assert (
        run_cli(
            ("doctor", "--check", "base-dependency-closure", "--json"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {
        "check": "base-dependency-closure",
        "status": "failed",
    }


@pytest.mark.parametrize(
    ("arguments", "json_output", "private_values"),
    (
        (("private-command",), False, ("private-command",)),
        (("capture",), False, ()),
        (
            ("capture", "synthetic-private-text", "synthetic-extra"),
            False,
            ("synthetic-private-text", "synthetic-extra"),
        ),
        (
            ("--json", "capture", "synthetic-private-text", "synthetic-extra"),
            True,
            ("synthetic-private-text", "synthetic-extra"),
        ),
        (
            ("capture", "synthetic-private-text", "synthetic-extra", "--json"),
            True,
            ("synthetic-private-text", "synthetic-extra"),
        ),
        (
            ("doctor", "--check", "synthetic-private-check", "--json"),
            True,
            ("synthetic-private-check",),
        ),
        (
            ("export", "/synthetic/private/export", "synthetic-extra"),
            False,
            ("/synthetic/private/export", "synthetic-extra"),
        ),
    ),
)
def test_local_usage_failures_are_bounded_and_redacted(
    arguments: tuple[str, ...],
    json_output: bool,
    private_values: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(arguments, environment={}) == 2
    output = capsys.readouterr()
    if json_output:
        assert output.err == ""
        assert json.loads(output.out) == {
            "error": {
                "code": "invalid_command",
                "message": "Open Brain could not parse the command.",
            },
            "status": "failed",
        }
    else:
        assert output.out == ""
        assert output.err == "Open Brain could not parse the command.\n"
    for private_value in private_values:
        assert private_value not in output.out
        assert private_value not in output.err


def test_local_status_observes_daemon_authority_and_capture_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _private_home(tmp_path)
    environment = {"HOME": str(home)}
    assert (
        run_cli(
            ("init",),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capsys.readouterr()
    brain_root = home / ".local/share/open-brain/brain"
    ready = tmp_path / "daemon-authority-ready"
    program = """
from pathlib import Path
import sys

from open_brain.profile import open_existing_single_user_local
from open_brain_engine.engine import acquire_daemon_authority

profile = open_existing_single_user_local(Path(sys.argv[1]))
with acquire_daemon_authority(profile):
    Path(sys.argv[2]).write_text("ready", encoding="ascii")
    sys.stdin.read(1)
"""
    holder = subprocess.Popen(
        (sys.executable, "-c", program, str(brain_root), str(ready)),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.is_file() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        error = (
            holder.stderr.read()
            if not ready.is_file() and holder.poll() is not None and holder.stderr is not None
            else "daemon authority holder did not become ready"
        )
        assert ready.is_file(), error

        assert (
            run_cli(
                ("status", "--json"),
                environment=environment,
                platform_name="linux",
                filesystem_type_probe=_filesystem,
            )
            == 0
        )
        status = cast(dict[str, object], json.loads(capsys.readouterr().out))
        assert status["daemon_running"] is True

        token = "synthetic-daemon-conflict-private-text"
        assert (
            run_cli(
                ("capture", token, "--json"),
                environment=environment,
                platform_name="linux",
                filesystem_type_probe=_filesystem,
            )
            == 78
        )
        failure = capsys.readouterr().out
        assert json.loads(failure) == {
            "error": {
                "code": "local_operation_failed",
                "message": "Open Brain could not complete the local command.",
            },
            "status": "failed",
        }
        assert token not in failure
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=5)

    assert holder.returncode == 0
