from __future__ import annotations

import stat
import subprocess
from hashlib import sha256
from pathlib import Path

from open_brain_legacy._compat.open_brain.integrations.ports import (
    HookEmitResult,
    HookInstallRequest,
    HookInstallStatus,
    HookKind,
    HookSignalStatus,
    PostCommitSignal,
)
from open_brain_legacy.integrations.hooks import (
    HookCompatibilityAction,
    HookCompatibilityDisposition,
    RepositoryHookCapability,
    TemporaryHookPlanner,
    deliver_post_commit_signal,
    run_post_commit_hook,
)


class _RecordingSignalPort:
    def __init__(self) -> None:
        self.signals: list[PostCommitSignal] = []

    def emit(self, signal: PostCommitSignal) -> HookEmitResult:
        self.signals.append(signal)
        return HookEmitResult(
            signal_id=signal.signal_id,
            status=HookSignalStatus.EMITTED,
        )


class _FailingSignalPort:
    def emit(self, signal: PostCommitSignal) -> HookEmitResult:
        raise RuntimeError("synthetic signal failure")


def test_hook_001_signal_delivery_is_metadata_only_and_fail_zero() -> None:
    port = _RecordingSignalPort()
    revision_id = "a" * 40

    emitted = deliver_post_commit_signal(
        repository_id="repository_fixture",
        revision_id=revision_id,
        signal_port=port,
    )
    failed = deliver_post_commit_signal(
        repository_id="repository_fixture",
        revision_id=revision_id,
        signal_port=_FailingSignalPort(),
    )

    assert emitted.status is HookSignalStatus.EMITTED
    assert failed.status is HookSignalStatus.FAILED
    assert port.signals == [
        PostCommitSignal(
            signal_id=emitted.signal_id,
            repository_id="repository_fixture",
            revision_id=revision_id,
        )
    ]
    assert all(
        len(value) <= 128
        for value in (
            port.signals[0].signal_id,
            port.signals[0].repository_id,
            port.signals[0].revision_id,
        )
    )
    assert run_post_commit_hook(
        repository_id="repository_fixture",
        revision_id=revision_id,
        signal_port=_FailingSignalPort(),
    ) == 0


def test_hook_001_bound_capability_installs_once_and_keeps_commits_fail_zero(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "synthetic-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    capability = RepositoryHookCapability.bind(
        repository_id="repository_fixture",
        repository_root=repository,
    )
    planner = TemporaryHookPlanner(capability=capability)
    request = HookInstallRequest(
        repository_id="repository_fixture",
        hook_kind=HookKind.POST_COMMIT,
        dry_run=False,
    )

    first = planner.install(request)
    second = planner.install(request)

    hook = repository / ".git" / "hooks" / "post-commit"
    assert first.status is HookInstallStatus.INSTALLED
    assert second.status is HookInstallStatus.ALREADY_INSTALLED
    assert hook.read_text() == planner.plan(request).template
    assert hook.stat().st_mode & stat.S_IXUSR
    assert "OLD/" not in hook.read_text()
    assert "sync-project-hooks.sh" not in hook.read_text()
    assert str(repository) not in hook.read_text()

    (repository / "synthetic.txt").write_text("synthetic content\n")
    subprocess.run(
        ["git", "add", "synthetic.txt"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Synthetic User",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "synthetic commit",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    revision_id = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    port = _RecordingSignalPort()

    assert run_post_commit_hook(
        repository_id="repository_fixture",
        revision_id=revision_id,
        signal_port=port,
    ) == 0
    assert port.signals[0].revision_id == revision_id


def test_hook_001_capability_denies_out_of_root_symlink_target(tmp_path: Path) -> None:
    repository = tmp_path / "synthetic-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    capability = RepositoryHookCapability.bind(
        repository_id="repository_fixture",
        repository_root=repository,
    )
    planner = TemporaryHookPlanner(capability=capability)
    hooks_directory = repository / ".git" / "hooks"
    hooks_directory.rename(repository / ".git" / "bound-hooks")
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    hooks_directory.symlink_to(outside_directory, target_is_directory=True)

    result = planner.install(
        HookInstallRequest(
            repository_id="repository_fixture",
            hook_kind=HookKind.POST_COMMIT,
            dry_run=False,
        )
    )

    assert result.status is HookInstallStatus.BLOCKED
    assert not (outside_directory / "post-commit").exists()
    assert str(repository) not in repr(capability)


def test_hook_001_capability_denies_unsafe_existing_hook(tmp_path: Path) -> None:
    repository = tmp_path / "synthetic-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    capability = RepositoryHookCapability.bind(
        repository_id="repository_fixture",
        repository_root=repository,
    )
    hook = repository / ".git" / "hooks" / "post-commit"
    hook.write_text("synthetic existing hook\n")

    result = TemporaryHookPlanner(capability=capability).install(
        HookInstallRequest(
            repository_id="repository_fixture",
            hook_kind=HookKind.POST_COMMIT,
            dry_run=False,
        )
    )

    assert result.status is HookInstallStatus.BLOCKED
    assert hook.read_text() == "synthetic existing hook\n"


def test_temporary_hook_planning_is_dry_run_idempotent_and_owner_gated(
    tmp_path: Path,
) -> None:
    hooks_directory = tmp_path / ".git" / "hooks"
    hooks_directory.mkdir(parents=True)
    existing_hook = hooks_directory / "post-commit"
    existing_hook.write_text("synthetic existing hook\n")
    request = HookInstallRequest(
        repository_id="repository_fixture",
        hook_kind=HookKind.POST_COMMIT,
        dry_run=True,
    )
    planner = TemporaryHookPlanner()

    first = planner.install(request)
    second = planner.install(request)

    assert first.status is HookInstallStatus.PLANNED
    assert second == first
    assert first.to_dict() == {
        "repository_id": "repository_fixture",
        "hook_kind": "post_commit",
        "status": "planned",
    }
    assert existing_hook.read_text() == "synthetic existing hook\n"
    assert (
        planner.install(
            HookInstallRequest(
                repository_id="repository_fixture",
                hook_kind=HookKind.POST_COMMIT,
                dry_run=False,
            )
        ).status
        is HookInstallStatus.BLOCKED
    )

    for action, disposition in (
        (
            HookCompatibilityAction.RETAIN,
            HookCompatibilityDisposition.IMPLEMENTATION_READY,
        ),
        (
            HookCompatibilityAction.RETIRE,
            HookCompatibilityDisposition.RETIREMENT_BLOCKED,
        ),
    ):
        result = planner.compatibility(action)

        assert result.disposition is disposition


def test_temporary_hook_plan_is_bounded_deterministic_and_fail_zero(
    tmp_path: Path,
) -> None:
    hooks_directory = tmp_path / ".git" / "hooks"
    hooks_directory.mkdir(parents=True)
    existing_hook = hooks_directory / "post-commit"
    existing_hook.write_text("synthetic existing hook\n")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    request = HookInstallRequest(
        repository_id="repository_fixture",
        hook_kind=HookKind.POST_COMMIT,
        dry_run=True,
    )
    planner = TemporaryHookPlanner()

    first = planner.plan(request)
    second = planner.plan(request)

    assert second == first
    assert first.repository_id == "repository_fixture"
    assert first.hook_kind is HookKind.POST_COMMIT
    assert first.template == (
        "#!/bin/sh\n"
        'revision="$(git rev-parse --verify HEAD 2>/dev/null)" || exit 0\n'
        "python -m open_brain_legacy.integrations.hooks post-commit repository_fixture "
        '"$revision" </dev/null >/dev/null 2>&1 &\n'
        "exit 0\n"
    )
    assert first.template_digest == sha256(first.template.encode()).hexdigest()
    assert "OLD/" not in first.template
    assert "sync-project-hooks.sh" not in first.template
    assert before == {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
