from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.git_sync_runtime import (
    GitCommand,
    GitCommandResult,
    GitRepositoryBinding,
    GitRepositoryKind,
    GitSyncBatch,
    GitSyncEffectCapability,
    GitSyncPlanner,
    GitSyncRuntimeApplication,
    PlannedGitSyncExecutor,
    SharedWriterAuthority,
)
from open_brain_legacy.operations.replay_journal import SqliteReplayJournal
from open_brain_legacy.operations.writer_jobs import (
    JobRunDisposition,
    WriterJobError,
    run_writer_job,
)
from tests.unit.storage._factories import FixedClock

_PUSH_TARGET = "git@example.test:owner/repository.git"


def _push_digest(target: str = _PUSH_TARGET) -> str:
    return sha256(target.encode("utf-8")).hexdigest()


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


@dataclass
class _RepoState:
    dirty: bool = False
    ahead_count: int = 0
    behind_count: int = 0
    has_upstream: bool = True
    conflicted: bool = False
    top_level: Path | None = None
    push_target: str = _PUSH_TARGET
    fail_once_on_push: bool = False


class FakeGitRunner:
    def __init__(self, repos: dict[Path, _RepoState]) -> None:
        self._repos = repos
        self.commands: list[GitCommand] = []

    def run(self, command: GitCommand) -> GitCommandResult:
        self.commands.append(command)
        state = self._repos.get(command.cwd)
        assert state is not None, f"unexpected cwd: {command.cwd}"
        argv = command.argv
        assert argv[:3] == ("git", "-c", "core.askPass=")
        action = argv[3:]
        if action == ("rev-parse", "--show-toplevel"):
            top_level = state.top_level or command.cwd
            return GitCommandResult(0, stdout=(str(top_level) + "\n").encode("utf-8"))
        if action == ("status", "--porcelain=v1", "--branch"):
            return GitCommandResult(0, stdout=_status_bytes(state))
        if action == ("remote", "get-url", "--push", "origin"):
            return GitCommandResult(0, stdout=(state.push_target + "\n").encode("utf-8"))
        if action == ("add", "--all"):
            return GitCommandResult(0)
        if action[:2] == ("commit", "--message"):
            if state.conflicted or not state.dirty:
                return GitCommandResult(1, stderr=b"commit refused\n")
            state.dirty = False
            if state.has_upstream:
                state.ahead_count += 1
            return GitCommandResult(0)
        if action == ("push",):
            if state.conflicted:
                return GitCommandResult(1, stderr=b"push refused\n")
            state.ahead_count = 0
            if state.fail_once_on_push:
                state.fail_once_on_push = False
                raise RuntimeError("synthetic interruption after git push")
            return GitCommandResult(0)
        raise AssertionError(f"unexpected argv: {argv!r}")


def _status_bytes(state: _RepoState) -> bytes:
    line = "## main"
    if state.has_upstream:
        line += "...origin/main"
        markers: list[str] = []
        if state.ahead_count:
            markers.append(f"ahead {state.ahead_count}")
        if state.behind_count:
            markers.append(f"behind {state.behind_count}")
        if markers:
            line += " [" + ", ".join(markers) + "]"
    lines = [line]
    if state.conflicted:
        lines.append("UU conflicted.txt")
    elif state.dirty:
        lines.append(" M dirty.txt")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True)
    return path


def _planner(
    *,
    work_root: Path,
    dev_root: Path,
    personal_root: Path | None = None,
    repositories: tuple[GitRepositoryBinding, ...],
    runner: FakeGitRunner,
) -> GitSyncPlanner:
    return GitSyncPlanner(
        work_root=work_root,
        dev_root=dev_root,
        personal_root=personal_root,
        repositories=repositories,
        runner=runner,
    )


def test_git_sync_planner_builds_deterministic_batch_and_uses_direct_argv(
    tmp_path: Path,
) -> None:
    work_root = _mkdir(tmp_path / "work")
    dev_root = _mkdir(tmp_path / "dev")
    work_repo = _mkdir(work_root / "alpha")
    dev_repo = _mkdir(dev_root / "beta")
    runner = FakeGitRunner(
        {
            work_repo: _RepoState(dirty=True, has_upstream=True),
            dev_repo: _RepoState(dirty=False, ahead_count=2, has_upstream=True),
        }
    )
    planner = _planner(
        work_root=work_root,
        dev_root=dev_root,
        repositories=(
            GitRepositoryBinding(
                repo_id="dev_repo",
                kind=GitRepositoryKind.DEV,
                relative_path=PurePosixPath("beta"),
                record_id="dev_sync",
                digest_sha256="b" * 64,
                push_target_digest_sha256=_push_digest(),
            ),
            GitRepositoryBinding(
                repo_id="work_repo",
                kind=GitRepositoryKind.WORK,
                relative_path=PurePosixPath("alpha"),
                record_id="work_sync",
                digest_sha256="a" * 64,
                push_target_digest_sha256=_push_digest(),
            ),
        ),
        runner=runner,
    )

    batch = planner.plan_batch()

    assert [repository.repo_id for repository in batch.repositories] == [
        "dev_repo",
        "work_repo",
    ]
    assert batch.repositories[0].requires_commit is False
    assert batch.repositories[0].requires_push is True
    assert batch.repositories[0].personal is False
    assert batch.repositories[1].requires_commit is True
    assert batch.repositories[1].requires_push is True
    assert [command.argv for command in runner.commands] == [
        ("git", "-c", "core.askPass=", "rev-parse", "--show-toplevel"),
        ("git", "-c", "core.askPass=", "status", "--porcelain=v1", "--branch"),
        ("git", "-c", "core.askPass=", "rev-parse", "--show-toplevel"),
        ("git", "-c", "core.askPass=", "status", "--porcelain=v1", "--branch"),
    ]


def test_git_sync_planner_refuses_nonmatching_physical_repository_root(
    tmp_path: Path,
) -> None:
    work_root = _mkdir(tmp_path / "work")
    dev_root = _mkdir(tmp_path / "dev")
    repo = _mkdir(work_root / "alpha")
    other = _mkdir(work_root / "elsewhere")
    runner = FakeGitRunner({repo: _RepoState(top_level=other)})
    planner = _planner(
        work_root=work_root,
        dev_root=dev_root,
        repositories=(
            GitRepositoryBinding(
                repo_id="work_repo",
                kind=GitRepositoryKind.WORK,
                relative_path=PurePosixPath("alpha"),
                record_id="work_sync",
                digest_sha256="a" * 64,
                push_target_digest_sha256=_push_digest(),
            ),
        ),
        runner=runner,
    )

    with pytest.raises(WriterJobError, match="git repository root validation failed"):
        planner.plan_batch()


def test_git_sync_runtime_commits_personal_state_locally_without_push(tmp_path: Path) -> None:
    work_root = _mkdir(tmp_path / "work")
    dev_root = _mkdir(tmp_path / "dev")
    personal_root = _mkdir(tmp_path / "personal")
    personal_repo = _mkdir(personal_root / "notes")
    runner = FakeGitRunner({personal_repo: _RepoState(dirty=True, has_upstream=True)})
    planner = _planner(
        work_root=work_root,
        dev_root=dev_root,
        personal_root=personal_root,
        repositories=(
            GitRepositoryBinding(
                repo_id="personal_repo",
                kind=GitRepositoryKind.PERSONAL,
                relative_path=PurePosixPath("notes"),
                record_id="personal_sync",
                digest_sha256="a" * 64,
            ),
        ),
        runner=runner,
    )
    batch = planner.plan_batch()
    capability = GitSyncEffectCapability(
        root=tmp_path,
        batch=batch,
        executor=PlannedGitSyncExecutor(planner=planner, runner=runner),
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )

    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        result = run_writer_job(
            job_id="JOB-015",
            root=tmp_path,
            replay_key="git-sync-personal",
            journal=journal,
            application=GitSyncRuntimeApplication(batch),
            effect_capability=capability,
            lease=RecordingLease(),
            personal_local_only=True,
        )

    assert result.disposition is JobRunDisposition.APPLIED
    assert batch.repositories[0].personal is True
    assert batch.repositories[0].requires_commit is True
    assert batch.repositories[0].requires_push is False
    assert sum(command.argv[3] == "commit" for command in runner.commands) == 1
    assert all(command.argv[3] != "push" for command in runner.commands)


def test_git_sync_runtime_refuses_unbound_push_target_before_push(tmp_path: Path) -> None:
    work_root = _mkdir(tmp_path / "work")
    dev_root = _mkdir(tmp_path / "dev")
    work_repo = _mkdir(work_root / "alpha")
    runner = FakeGitRunner(
        {
            work_repo: _RepoState(
                dirty=False,
                ahead_count=1,
                has_upstream=True,
                push_target="https://example.com/repo.git",
            )
        }
    )
    planner = _planner(
        work_root=work_root,
        dev_root=dev_root,
        repositories=(
            GitRepositoryBinding(
                repo_id="work_repo",
                kind=GitRepositoryKind.WORK,
                relative_path=PurePosixPath("alpha"),
                record_id="work_sync",
                digest_sha256="a" * 64,
                push_target_digest_sha256=_push_digest(
                    "git@example.test:owner/expected.git"
                ),
            ),
        ),
        runner=runner,
    )
    batch = planner.plan_batch()
    capability = GitSyncEffectCapability(
        root=tmp_path,
        batch=batch,
        executor=PlannedGitSyncExecutor(planner=planner, runner=runner),
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )

    with (
        SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal,
        pytest.raises(
            WriterJobError,
            match="git push target binding mismatch",
        ),
    ):
        run_writer_job(
            job_id="JOB-015",
            root=tmp_path,
            replay_key="git-sync-egress-refusal",
            journal=journal,
            application=GitSyncRuntimeApplication(batch),
            effect_capability=capability,
            lease=RecordingLease(),
        )

    argvs = [command.argv for command in runner.commands]
    assert ("git", "-c", "core.askPass=", "push") not in argvs


def test_git_sync_runtime_commits_pushes_and_replays(tmp_path: Path) -> None:
    work_root = _mkdir(tmp_path / "work")
    dev_root = _mkdir(tmp_path / "dev")
    work_repo = _mkdir(work_root / "alpha")
    dev_repo = _mkdir(dev_root / "beta")
    runner = FakeGitRunner(
        {
            work_repo: _RepoState(dirty=True, has_upstream=True),
            dev_repo: _RepoState(dirty=True, has_upstream=False),
        }
    )
    planner = _planner(
        work_root=work_root,
        dev_root=dev_root,
        repositories=(
            GitRepositoryBinding(
                repo_id="work_repo",
                kind=GitRepositoryKind.WORK,
                relative_path=PurePosixPath("alpha"),
                record_id="work_sync",
                digest_sha256="a" * 64,
                push_target_digest_sha256=_push_digest(),
            ),
            GitRepositoryBinding(
                repo_id="dev_repo",
                kind=GitRepositoryKind.DEV,
                relative_path=PurePosixPath("beta"),
                record_id="dev_sync",
                digest_sha256="b" * 64,
            ),
        ),
        runner=runner,
    )
    batch = planner.plan_batch()
    capability = GitSyncEffectCapability(
        root=tmp_path,
        batch=batch,
        executor=PlannedGitSyncExecutor(planner=planner, runner=runner),
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )
    lease = RecordingLease()
    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        first = run_writer_job(
            job_id="JOB-015",
            root=tmp_path,
            replay_key="git-sync-2026-08-17",
            journal=journal,
            application=GitSyncRuntimeApplication(batch),
            effect_capability=capability,
            lease=lease,
        )
        replay = run_writer_job(
            job_id="JOB-015",
            root=tmp_path,
            replay_key="git-sync-2026-08-17",
            journal=journal,
            application=GitSyncRuntimeApplication(batch),
            effect_capability=capability,
            lease=lease,
        )

    argvs = [command.argv for command in runner.commands]
    commit_calls = sum(
        command.argv[:4] == ("git", "-c", "core.askPass=", "commit")
        for command in runner.commands
    )
    push_calls = sum(
        command.argv == ("git", "-c", "core.askPass=", "push")
        for command in runner.commands
    )

    assert first.disposition is JobRunDisposition.APPLIED
    assert first.effect_count == 2
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert lease.scopes == [LockScope.SHARED_WRITER, LockScope.SHARED_WRITER]
    assert ("git", "-c", "core.askPass=", "add", "--all") in argvs
    assert ("git", "-c", "core.askPass=", "push") in argvs
    assert commit_calls == 2
    assert push_calls == 1


def test_git_sync_runtime_refuses_conflicted_state_before_mutation(
    tmp_path: Path,
) -> None:
    work_root = _mkdir(tmp_path / "work")
    dev_root = _mkdir(tmp_path / "dev")
    work_repo = _mkdir(work_root / "alpha")
    runner = FakeGitRunner({work_repo: _RepoState(conflicted=True)})
    planner = _planner(
        work_root=work_root,
        dev_root=dev_root,
        repositories=(
            GitRepositoryBinding(
                repo_id="work_repo",
                kind=GitRepositoryKind.WORK,
                relative_path=PurePosixPath("alpha"),
                record_id="work_sync",
                digest_sha256="a" * 64,
                push_target_digest_sha256=_push_digest(),
            ),
        ),
        runner=runner,
    )
    batch = planner.plan_batch()
    capability = GitSyncEffectCapability(
        root=tmp_path,
        batch=batch,
        executor=PlannedGitSyncExecutor(planner=planner, runner=runner),
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )

    with (
        SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal,
        pytest.raises(WriterJobError, match="git sync conflict state"),
    ):
        run_writer_job(
            job_id="JOB-015",
            root=tmp_path,
            replay_key="git-sync-conflict",
            journal=journal,
            application=GitSyncRuntimeApplication(batch),
            effect_capability=capability,
            lease=RecordingLease(),
        )

    assert all(command.argv[3] not in {"add", "commit", "push"} for command in runner.commands)


def test_git_sync_runtime_recovers_reserved_receipt_after_push_interruption(
    tmp_path: Path,
) -> None:
    work_root = _mkdir(tmp_path / "work")
    dev_root = _mkdir(tmp_path / "dev")
    work_repo = _mkdir(work_root / "alpha")
    runner = FakeGitRunner(
        {
            work_repo: _RepoState(
                dirty=True,
                has_upstream=True,
                fail_once_on_push=True,
            ),
        }
    )
    planner = _planner(
        work_root=work_root,
        dev_root=dev_root,
        repositories=(
            GitRepositoryBinding(
                repo_id="work_repo",
                kind=GitRepositoryKind.WORK,
                relative_path=PurePosixPath("alpha"),
                record_id="work_sync",
                digest_sha256="a" * 64,
                push_target_digest_sha256=_push_digest(),
            ),
        ),
        runner=runner,
    )
    batch = planner.plan_batch()
    capability = GitSyncEffectCapability(
        root=tmp_path,
        batch=batch,
        executor=PlannedGitSyncExecutor(planner=planner, runner=runner),
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )

    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            run_writer_job(
                job_id="JOB-015",
                root=tmp_path,
                replay_key="git-sync-crash-retry",
                journal=journal,
                application=GitSyncRuntimeApplication(batch),
                effect_capability=capability,
                lease=RecordingLease(),
            )
        recovered = run_writer_job(
            job_id="JOB-015",
            root=tmp_path,
            replay_key="git-sync-crash-retry",
            journal=journal,
            application=GitSyncRuntimeApplication(batch),
            effect_capability=capability,
            lease=RecordingLease(),
        )

    commit_calls = sum(
        command.argv[:4] == ("git", "-c", "core.askPass=", "commit")
        for command in runner.commands
    )
    push_calls = sum(
        command.argv == ("git", "-c", "core.askPass=", "push")
        for command in runner.commands
    )

    assert recovered.disposition is JobRunDisposition.REPLAYED
    assert commit_calls == 1
    assert push_calls == 1


def test_git_sync_runtime_rejects_non_shared_writer_authority(tmp_path: Path) -> None:
    batch = GitSyncBatch()

    with pytest.raises(WriterJobError, match="shared writer authority mismatch"):
        SharedWriterAuthority(LockScope.INDEX)

    assert batch.repositories == ()
