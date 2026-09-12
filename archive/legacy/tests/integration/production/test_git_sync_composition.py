from __future__ import annotations

import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes

from open_brain_legacy.operations.git_sync_runtime import GitCommand, GitRepositoryKind
from open_brain_legacy.production.git_sync import (
    GitInventoryError,
    SubprocessGitCommandRunner,
    load_private_git_inventory,
)


def _inventory_bytes(tmp_path: Path) -> bytes:
    return canonical_json_bytes(
        {
            "version": 1,
            "home_root": str(tmp_path / "home"),
            "dev_root": str(tmp_path / "dev"),
            "repositories": [
                {
                    "repo_id": "work_brain",
                    "kind": "work",
                    "relative_path": ".",
                    "record_id": "work_brain_sync",
                    "digest_sha256": "a" * 64,
                    "push_target_digest_sha256": None,
                },
                {
                    "repo_id": "personal_brain",
                    "kind": "personal",
                    "relative_path": ".",
                    "record_id": "personal_brain_sync",
                    "digest_sha256": "b" * 64,
                    "push_target_digest_sha256": None,
                },
            ],
        }
    )


def _write_inventory(tmp_path: Path, payload: bytes | None = None) -> Path:
    path = tmp_path / "git-inventory.json"
    path.write_bytes(_inventory_bytes(tmp_path) if payload is None else payload)
    path.chmod(0o600)
    return path


def test_private_git_inventory_is_canonical_owner_only_and_explicit(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    (tmp_path / "dev").mkdir()
    inventory = load_private_git_inventory(_write_inventory(tmp_path))

    assert inventory.home_root == tmp_path / "home"
    assert inventory.dev_root == tmp_path / "dev"
    assert [binding.kind for binding in inventory.repositories] == [
        GitRepositoryKind.WORK,
        GitRepositoryKind.PERSONAL,
    ]
    assert inventory.digest_sha256 == sha256(_inventory_bytes(tmp_path)).hexdigest()


def test_private_git_inventory_rejects_noncanonical_or_readable_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "home").mkdir()
    (tmp_path / "dev").mkdir()
    path = _write_inventory(tmp_path, b'{"version":1}')

    with pytest.raises(GitInventoryError, match="invalid private Git inventory"):
        load_private_git_inventory(path)

    path.write_bytes(_inventory_bytes(tmp_path))
    path.chmod(0o644)
    with pytest.raises(GitInventoryError, match="private Git inventory unavailable"):
        load_private_git_inventory(path)


def test_private_git_inventory_forbids_personal_push_authority(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    (tmp_path / "dev").mkdir()
    value = {
        "version": 1,
        "home_root": str(tmp_path / "home"),
        "dev_root": str(tmp_path / "dev"),
        "repositories": [
            {
                "repo_id": "personal_brain",
                "kind": "personal",
                "relative_path": ".",
                "record_id": "personal_brain_sync",
                "digest_sha256": "a" * 64,
                "push_target_digest_sha256": "b" * 64,
            }
        ],
    }

    with pytest.raises(GitInventoryError, match="invalid private Git inventory"):
        load_private_git_inventory(_write_inventory(tmp_path, canonical_json_bytes(value)))


def test_subprocess_git_runner_executes_only_closed_git_schema(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "repos"
    repo = root / "repo"
    home.mkdir()
    repo.mkdir(parents=True)
    subprocess.run(
        ("/usr/bin/git", "init", "--quiet"),
        cwd=repo,
        check=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    runner = SubprocessGitCommandRunner(
        home_root=home,
        allowed_roots=(root,),
        git_executable=Path("/usr/bin/git"),
    )

    status = runner.run(
        GitCommand(
            cwd=repo,
            argv=("git", "-c", "core.askPass=", "status", "--porcelain=v1", "--branch"),
        )
    )

    assert status.returncode == 0
    assert status.stdout.startswith(b"## ")
    assert status.stderr == b""
    with pytest.raises(GitInventoryError, match="Git command refused"):
        runner.run(
            GitCommand(
                cwd=repo,
                argv=("git", "-c", "core.askPass=", "config", "--list"),
            )
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(GitInventoryError, match="Git command refused"):
        runner.run(
            GitCommand(
                cwd=outside,
                argv=("git", "-c", "core.askPass=", "status", "--porcelain=v1", "--branch"),
            )
        )
    assert not os.environ.get("GIT_TERMINAL_PROMPT")


def test_subprocess_git_runner_allows_only_bounded_revision_history(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "repos"
    repo = root / "repo"
    home.mkdir()
    repo.mkdir(parents=True)
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    subprocess.run(("/usr/bin/git", "init", "--quiet"), cwd=repo, check=True, env=environment)
    (repo / "fixture.txt").write_text("synthetic\n")
    subprocess.run(("/usr/bin/git", "add", "fixture.txt"), cwd=repo, check=True, env=environment)
    subprocess.run(
        (
            "/usr/bin/git",
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.test",
            "commit",
            "--quiet",
            "--message=synthetic fixture",
        ),
        cwd=repo,
        check=True,
        env=environment,
    )
    runner = SubprocessGitCommandRunner(
        home_root=home,
        allowed_roots=(root,),
        git_executable=Path("/usr/bin/git"),
    )
    action = (
        "log",
        "--since=2000-01-01T00:00:00Z",
        "--until=2099-01-01T00:00:00Z",
        "--format=%H",
        "--max-count=100",
        "--no-merges",
    )

    history = runner.run(
        GitCommand(cwd=repo, argv=("git", "-c", "core.askPass=", *action))
    )

    assert history.returncode == 0
    assert len(history.stdout.strip()) == 40
    with pytest.raises(GitInventoryError, match="Git command refused"):
        runner.run(
            GitCommand(
                cwd=repo,
                argv=(
                    "git",
                    "-c",
                    "core.askPass=",
                    "log",
                    "--format=%B",
                ),
            )
        )
