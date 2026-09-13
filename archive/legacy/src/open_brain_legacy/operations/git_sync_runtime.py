from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.engine import LockScope
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    atomic_write_new,
    read_confined,
)

from .writer_jobs import (
    EffectCommand,
    EffectReceipt,
    EffectRecord,
    PreparedEffect,
    ScheduledEffect,
    WriterJobError,
    WriterJobInvocation,
)

_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_RELATIVE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RECEIPT_FIELDS = frozenset(
    {
        "version",
        "job_id",
        "replay_key",
        "request_digest_sha256",
        "effect",
        "effect_digest_sha256",
        "records",
        "review_item_ids",
        "approval_bindings",
    }
)
_RECORD_FIELDS = frozenset({"record_id", "digest_sha256", "approval"})
_POINTER_FIELDS = frozenset({"version", "effect_digest_sha256"})
_MAX_RECEIPT_BYTES = 24 * 1024
_MAX_POINTER_BYTES = 4 * 1024
_STATUS_PREFIX = "## "
_UNMERGED_PREFIXES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
_SCP_PUSH_TARGET = re.compile(
    r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+"
)


@dataclass(frozen=True, slots=True)
class SharedWriterAuthority:
    scope: LockScope

    def __post_init__(self) -> None:
        if self.scope is not LockScope.SHARED_WRITER:
            raise WriterJobError("shared writer authority mismatch")


class GitRepositoryKind(StrEnum):
    WORK = "work"
    PERSONAL = "personal"
    DEV = "dev"


@dataclass(frozen=True, slots=True)
class GitRepositoryBinding:
    repo_id: str
    kind: GitRepositoryKind
    relative_path: PurePosixPath
    record_id: str
    digest_sha256: str
    push_target_digest_sha256: str | None = None

    def __post_init__(self) -> None:
        root_binding = self.relative_path == PurePosixPath(".")
        if (
            not isinstance(self.repo_id, str)
            or _OPAQUE_ID.fullmatch(self.repo_id) is None
            or not isinstance(self.kind, GitRepositoryKind)
            or not isinstance(self.relative_path, PurePosixPath)
            or self.relative_path.is_absolute()
            or not root_binding
            and (
                not self.relative_path.parts
                or any(
                part in {"", ".", ".."} or _SAFE_RELATIVE_PART.fullmatch(part) is None
                for part in self.relative_path.parts
                )
            )
            or not isinstance(self.record_id, str)
            or _OPAQUE_ID.fullmatch(self.record_id) is None
            or not isinstance(self.digest_sha256, str)
            or _SHA256.fullmatch(self.digest_sha256) is None
            or (
                self.push_target_digest_sha256 is not None
                and (
                    not isinstance(self.push_target_digest_sha256, str)
                    or _SHA256.fullmatch(self.push_target_digest_sha256) is None
                )
            )
            or (
                self.kind is GitRepositoryKind.PERSONAL
                and self.push_target_digest_sha256 is not None
            )
        ):
            raise WriterJobError("invalid git sync repository binding")


@dataclass(frozen=True, slots=True)
class GitRepositorySyncPlan:
    repo_id: str
    kind: GitRepositoryKind
    record_id: str
    digest_sha256: str
    requires_commit: bool = False
    requires_push: bool = False
    personal: bool = False
    conflicted: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repo_id, str)
            or _OPAQUE_ID.fullmatch(self.repo_id) is None
            or not isinstance(self.kind, GitRepositoryKind)
            or not isinstance(self.record_id, str)
            or _OPAQUE_ID.fullmatch(self.record_id) is None
            or not isinstance(self.digest_sha256, str)
            or _SHA256.fullmatch(self.digest_sha256) is None
            or type(self.requires_commit) is not bool
            or type(self.requires_push) is not bool
            or type(self.personal) is not bool
            or type(self.conflicted) is not bool
            or (self.personal and self.requires_push)
        ):
            raise WriterJobError("invalid git sync repository plan")

    @property
    def requires_action(self) -> bool:
        return self.requires_commit or self.requires_push

    def to_effect_record(self) -> EffectRecord:
        return EffectRecord(record_id=self.record_id, digest_sha256=self.digest_sha256)


@dataclass(frozen=True, slots=True)
class GitSyncBatch:
    repositories: tuple[GitRepositorySyncPlan, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repositories, tuple)
            or any(not isinstance(item, GitRepositorySyncPlan) for item in self.repositories)
            or len({item.repo_id for item in self.repositories}) != len(self.repositories)
            or len({item.record_id for item in self.repositories}) != len(self.repositories)
        ):
            raise WriterJobError("invalid git sync batch")


@dataclass(frozen=True, slots=True)
class GitCommand:
    cwd: Path
    argv: tuple[str, ...]
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cwd, Path)
            or not self.cwd.is_absolute()
            or not isinstance(self.argv, tuple)
            or not self.argv
            or any(
                not isinstance(value, str) or not value or "\x00" in value
                for value in self.argv
            )
            or not isinstance(self.timeout_seconds, int | float)
            or isinstance(self.timeout_seconds, bool)
            or not 0 < float(self.timeout_seconds) <= 30.0
        ):
            raise WriterJobError("invalid git sync command")


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.returncode, int)
            or isinstance(self.returncode, bool)
            or not isinstance(self.stdout, bytes)
            or not isinstance(self.stderr, bytes)
        ):
            raise WriterJobError("invalid git sync command result")


class GitCommandRunner(Protocol):
    def run(self, command: GitCommand) -> GitCommandResult: ...


@dataclass(frozen=True, slots=True)
class _ObservedGitStatus:
    dirty: bool
    conflicted: bool
    has_upstream: bool
    ahead_count: int
    behind_count: int

    @property
    def requires_commit(self) -> bool:
        return self.dirty

    @property
    def requires_push(self) -> bool:
        return self.ahead_count > 0 or (self.dirty and self.has_upstream)

    @property
    def sync_conflicted(self) -> bool:
        return self.conflicted or self.behind_count > 0


class GitSyncPlanner:
    def __init__(
        self,
        *,
        work_root: Path,
        dev_root: Path,
        personal_root: Path | None = None,
        repositories: tuple[GitRepositoryBinding, ...],
        runner: GitCommandRunner,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._work_root = _validated_root(work_root)
        self._dev_root = _validated_root(dev_root)
        if (
            not isinstance(repositories, tuple)
            or any(not isinstance(item, GitRepositoryBinding) for item in repositories)
            or len({item.repo_id for item in repositories}) != len(repositories)
            or len({item.record_id for item in repositories}) != len(repositories)
            or not isinstance(timeout_seconds, int | float)
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= 30.0
        ):
            raise WriterJobError("invalid git sync planner")
        self._personal_root: Path | None
        if any(
            repository.kind is GitRepositoryKind.PERSONAL
            for repository in repositories
        ):
            if personal_root is None:
                raise WriterJobError("git runtime root unavailable")
            self._personal_root = _validated_root(personal_root)
        else:
            self._personal_root = None
        self._repositories = {
            repository.repo_id: repository
            for repository in sorted(repositories, key=lambda item: (item.kind.value, item.repo_id))
        }
        self._runner = runner
        self._timeout_seconds = float(timeout_seconds)

    def plan_batch(self) -> GitSyncBatch:
        return GitSyncBatch(
            repositories=tuple(
                self.plan_repository(repository_id)
                for repository_id in self._repositories
            )
        )

    def plan_repository(self, repository_id: str) -> GitRepositorySyncPlan:
        repository = self._repositories.get(repository_id)
        if repository is None:
            raise WriterJobError("unknown git sync repository")
        root = self.repository_root(repository_id)
        status = self._status(root)
        return GitRepositorySyncPlan(
            repo_id=repository.repo_id,
            kind=repository.kind,
            record_id=repository.record_id,
            digest_sha256=repository.digest_sha256,
            requires_commit=status.requires_commit,
            requires_push=(
                status.requires_push
                and repository.push_target_digest_sha256 is not None
            ),
            personal=repository.kind is GitRepositoryKind.PERSONAL,
            conflicted=status.sync_conflicted,
        )

    def repository_root(self, repository_id: str) -> Path:
        repository = self._repositories.get(repository_id)
        if repository is None:
            raise WriterJobError("unknown git sync repository")
        base_root = {
            GitRepositoryKind.WORK: self._work_root,
            GitRepositoryKind.DEV: self._dev_root,
            GitRepositoryKind.PERSONAL: self._personal_root,
        }[repository.kind]
        if base_root is None:
            raise WriterJobError("git runtime root unavailable")
        candidate = _resolve_repository_root(base_root, repository.relative_path)
        reported_root = self._git_stdout(candidate, "rev-parse", "--show-toplevel")
        try:
            physical_reported = Path(reported_root).resolve(strict=True)
        except (OSError, RuntimeError):
            raise WriterJobError("git repository root validation failed") from None
        if physical_reported != candidate:
            raise WriterJobError("git repository root validation failed")
        return candidate

    def push_target_digest(self, repository_id: str) -> str | None:
        repository = self._repositories.get(repository_id)
        if repository is None:
            raise WriterJobError("unknown git sync repository")
        return repository.push_target_digest_sha256

    def _status(self, root: Path) -> _ObservedGitStatus:
        payload = self._git_stdout(root, "status", "--porcelain=v1", "--branch")
        lines = payload.splitlines()
        if not lines or not lines[0].startswith(_STATUS_PREFIX):
            raise WriterJobError("git status plan unavailable")
        branch = lines[0]
        ahead = _count_status_marker(branch, "ahead ")
        behind = _count_status_marker(branch, "behind ")
        has_upstream = "..." in branch
        conflicted = "diverged" in branch or any(_is_conflict_line(line) for line in lines[1:])
        dirty = any(bool(line.strip()) for line in lines[1:])
        return _ObservedGitStatus(
            dirty=dirty,
            conflicted=conflicted,
            has_upstream=has_upstream,
            ahead_count=ahead,
            behind_count=behind,
        )

    def _git_stdout(self, cwd: Path, *args: str) -> str:
        result = self._runner.run(
            GitCommand(
                cwd=cwd,
                argv=("git", "-c", "core.askPass=", *args),
                timeout_seconds=self._timeout_seconds,
            )
        )
        if result.returncode != 0:
            raise WriterJobError("git status plan unavailable")
        try:
            return result.stdout.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise WriterJobError("git status plan unavailable") from None


class GitSyncExecutor(Protocol):
    def has_commit(
        self,
        repository: GitRepositorySyncPlan,
        authority: SharedWriterAuthority,
    ) -> bool: ...

    def commit(
        self,
        repository: GitRepositorySyncPlan,
        authority: SharedWriterAuthority,
    ) -> None: ...

    def has_push(
        self,
        repository: GitRepositorySyncPlan,
        authority: SharedWriterAuthority,
    ) -> bool: ...

    def push(
        self,
        repository: GitRepositorySyncPlan,
        authority: SharedWriterAuthority,
    ) -> None: ...


class PlannedGitSyncExecutor:
    def __init__(
        self,
        *,
        planner: GitSyncPlanner,
        runner: GitCommandRunner,
        timeout_seconds: float = 5.0,
    ) -> None:
        if (
            not isinstance(timeout_seconds, int | float)
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= 30.0
        ):
            raise WriterJobError("invalid git sync executor")
        self._planner = planner
        self._runner = runner
        self._timeout_seconds = float(timeout_seconds)

    def has_commit(
        self,
        repository: GitRepositorySyncPlan,
        authority: SharedWriterAuthority,
    ) -> bool:
        _require_shared_writer(authority)
        status = self._planner.plan_repository(repository.repo_id)
        return not status.requires_commit and not status.conflicted

    def commit(
        self,
        repository: GitRepositorySyncPlan,
        authority: SharedWriterAuthority,
    ) -> None:
        _require_shared_writer(authority)
        status = self._planner.plan_repository(repository.repo_id)
        if status.conflicted:
            raise WriterJobError("git sync conflict state requires operator resolution")
        if not status.requires_commit:
            return
        root = self._planner.repository_root(repository.repo_id)
        self._run(root, "add", "--all")
        self._run(
            root,
            "commit",
            "--message",
            f"open-brain sync {repository.record_id} {repository.digest_sha256[:12]}",
        )
        if not self.has_commit(repository, authority):
            raise WriterJobError("git sync commit verification failed")

    def has_push(
        self,
        repository: GitRepositorySyncPlan,
        authority: SharedWriterAuthority,
    ) -> bool:
        _require_shared_writer(authority)
        status = self._planner.plan_repository(repository.repo_id)
        return not status.requires_push and not status.conflicted

    def push(
        self,
        repository: GitRepositorySyncPlan,
        authority: SharedWriterAuthority,
    ) -> None:
        _require_shared_writer(authority)
        status = self._planner.plan_repository(repository.repo_id)
        if status.conflicted:
            raise WriterJobError("git sync conflict state requires operator resolution")
        if status.requires_commit:
            raise WriterJobError("git sync commit verification failed")
        if not status.requires_push:
            return
        root = self._planner.repository_root(repository.repo_id)
        target = self._git_stdout(root, "remote", "get-url", "--push", "origin")
        expected_target_digest = self._planner.push_target_digest(repository.repo_id)
        _validate_push_target(target, expected_target_digest)
        self._run(root, "push")
        if not self.has_push(repository, authority):
            raise WriterJobError("git sync push verification failed")

    def _run(self, cwd: Path, *args: str) -> None:
        result = self._runner.run(
            GitCommand(
                cwd=cwd,
                argv=("git", "-c", "core.askPass=", *args),
                timeout_seconds=self._timeout_seconds,
            )
        )
        if result.returncode != 0:
            raise WriterJobError("git sync command failed")

    def _git_stdout(self, cwd: Path, *args: str) -> str:
        result = self._runner.run(
            GitCommand(
                cwd=cwd,
                argv=("git", "-c", "core.askPass=", *args),
                timeout_seconds=self._timeout_seconds,
            )
        )
        if result.returncode != 0:
            raise WriterJobError("git sync command failed")
        try:
            return result.stdout.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise WriterJobError("git sync command failed") from None


@dataclass(frozen=True, slots=True)
class GitSyncRuntimeApplication:
    batch: GitSyncBatch

    def __post_init__(self) -> None:
        if not isinstance(self.batch, GitSyncBatch):
            raise WriterJobError("invalid git sync runtime application")

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        if (
            not isinstance(invocation, WriterJobInvocation)
            or invocation.job_id != "JOB-015"
            or invocation.effect is not ScheduledEffect.LOCAL_GIT_SYNC
            or invocation.approved_records
            or invocation.approval_bindings
        ):
            raise WriterJobError("invalid git sync runtime invocation")
        if any(repository.conflicted for repository in self.batch.repositories):
            raise WriterJobError("git sync conflict state requires operator resolution")
        if invocation.personal_local_only and any(
            repository.personal and repository.requires_push
            for repository in self.batch.repositories
        ):
            raise WriterJobError("personal Git state must remain local-only")
        return _prepared_effect(self.batch)


class GitSyncEffectCapability:
    effect = ScheduledEffect.LOCAL_GIT_SYNC
    local_only = True
    dry_run = False

    def __init__(
        self,
        *,
        root: Path,
        batch: GitSyncBatch,
        executor: GitSyncExecutor,
        authority: SharedWriterAuthority,
    ) -> None:
        if (
            not isinstance(root, Path)
            or not isinstance(batch, GitSyncBatch)
            or not isinstance(authority, SharedWriterAuthority)
        ):
            raise WriterJobError("invalid git sync effect capability")
        self.root = root
        self._batch = batch
        self._executor = executor
        self._authority = authority

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        reservation, _pointer = _paths(job_id, replay_key)
        payload = read_confined(root=self.root, relative=reservation)
        if payload is None:
            return None
        return _receipt_from_bytes(payload)

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        self._validate_command(command)
        receipt = EffectReceipt.from_command(command)
        reservation, _pointer = _paths(command.job_id, command.replay_key)
        try:
            atomic_write_new(root=self.root, relative=reservation, data=_receipt_bytes(receipt))
        except DuplicateConflictError:
            raise WriterJobError("git sync effect reservation conflict") from None
        recovered = self.recover(command.job_id, command.replay_key)
        if recovered != receipt:
            raise WriterJobError("git sync effect reservation conflict")
        return receipt

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self._validate_command(command)
        expected = EffectReceipt.from_command(command)
        if receipt != expected or self.recover(command.job_id, command.replay_key) != receipt:
            raise WriterJobError("git sync effect reservation conflict")
        for repository in self._batch.repositories:
            if not repository.requires_action:
                continue
            if repository.requires_commit and not self._executor.has_commit(
                repository,
                self._authority,
            ):
                self._executor.commit(repository, self._authority)
            if repository.requires_push and not self._executor.has_push(
                repository,
                self._authority,
            ):
                self._executor.push(repository, self._authority)
        _reservation, pointer = _paths(command.job_id, command.replay_key)
        try:
            atomic_write_new(
                root=self.root,
                relative=pointer,
                data=canonical_json_bytes(
                    {"version": 1, "effect_digest_sha256": receipt.effect_digest_sha256}
                ),
            )
        except DuplicateConflictError:
            raise WriterJobError("git sync applied pointer conflict") from None

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        if not isinstance(receipt, EffectReceipt) or receipt.effect is not self.effect:
            raise WriterJobError("invalid git sync effect receipt")
        _reservation, pointer = _paths(receipt.job_id, receipt.replay_key)
        payload = read_confined(root=self.root, relative=pointer)
        if payload is None:
            return None
        effect_digest = _pointer_from_bytes(payload)
        if effect_digest != receipt.effect_digest_sha256:
            raise WriterJobError("git sync applied pointer conflict")
        for repository in self._batch.repositories:
            if repository.requires_commit and not self._executor.has_commit(
                repository,
                self._authority,
            ):
                raise WriterJobError("git sync durable read-back conflict")
            if repository.requires_push and not self._executor.has_push(
                repository,
                self._authority,
            ):
                raise WriterJobError("git sync durable read-back conflict")
        return _prepared_effect(self._batch)

    def _validate_command(self, command: EffectCommand) -> None:
        if (
            not isinstance(command, EffectCommand)
            or command.job_id != "JOB-015"
            or command.prepared != _prepared_effect(self._batch)
        ):
            raise WriterJobError("invalid git sync effect command")


def _prepared_effect(batch: GitSyncBatch) -> PreparedEffect:
    records = tuple(
        repository.to_effect_record()
        for repository in batch.repositories
        if repository.requires_action
    )
    return PreparedEffect(effect=ScheduledEffect.LOCAL_GIT_SYNC, records=records)


def _paths(job_id: str, replay_key: str) -> tuple[PurePosixPath, PurePosixPath]:
    identity = sha256(
        canonical_json_bytes({"job_id": job_id, "replay_key": replay_key})
    ).hexdigest()
    base = PurePosixPath("operations/effects/git-sync") / identity
    return base.with_suffix(".json"), base.with_suffix(".applied.json")


def _receipt_bytes(receipt: EffectReceipt) -> bytes:
    return canonical_json_bytes(
        {
            "version": 1,
            "job_id": receipt.job_id,
            "replay_key": receipt.replay_key,
            "request_digest_sha256": receipt.request_digest_sha256,
            "effect": receipt.effect.value,
            "effect_digest_sha256": receipt.effect_digest_sha256,
            "records": [record.to_dict() for record in receipt.records],
            "review_item_ids": list(receipt.review_item_ids),
            "approval_bindings": [],
        }
    )


def _receipt_from_bytes(payload: bytes) -> EffectReceipt:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_RECEIPT_BYTES:
        raise WriterJobError("invalid git sync effect receipt")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict or frozenset(value) != _RECEIPT_FIELDS or value["version"] != 1:
            raise WriterJobError("invalid git sync effect receipt")
        raw_records = value["records"]
        if (
            type(raw_records) is not list
            or value["review_item_ids"] != []
            or value["approval_bindings"] != []
        ):
            raise WriterJobError("invalid git sync effect receipt")
        records = tuple(
            EffectRecord(
                record_id=record["record_id"],
                digest_sha256=record["digest_sha256"],
            )
            for record in raw_records
            if (
                type(record) is dict
                and frozenset(record) == _RECORD_FIELDS
                and record["approval"] is None
            )
        )
        if len(records) != len(raw_records):
            raise WriterJobError("invalid git sync effect receipt")
        receipt = EffectReceipt(
            job_id=value["job_id"],
            replay_key=value["replay_key"],
            request_digest_sha256=value["request_digest_sha256"],
            effect=ScheduledEffect(value["effect"]),
            effect_digest_sha256=value["effect_digest_sha256"],
            records=records,
            review_item_ids=(),
            approval_bindings=(),
        )
    except WriterJobError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid git sync effect receipt") from None
    if receipt.effect is not ScheduledEffect.LOCAL_GIT_SYNC or _receipt_bytes(receipt) != payload:
        raise WriterJobError("invalid git sync effect receipt")
    return receipt


def _pointer_from_bytes(payload: bytes) -> str:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_POINTER_BYTES:
        raise WriterJobError("invalid git sync applied pointer")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict or frozenset(value) != _POINTER_FIELDS or value["version"] != 1:
            raise WriterJobError("invalid git sync applied pointer")
        effect_digest = value["effect_digest_sha256"]
        if not isinstance(effect_digest, str):
            raise WriterJobError("invalid git sync applied pointer")
    except WriterJobError:
        raise
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid git sync applied pointer") from None
    if canonical_json_bytes(value) != payload:
        raise WriterJobError("invalid git sync applied pointer")
    return effect_digest


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _validated_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise WriterJobError("git runtime root unavailable")
    try:
        metadata = root.lstat()
    except OSError:
        raise WriterJobError("git runtime root unavailable") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise WriterJobError("git runtime root unavailable")
    return root.resolve(strict=True)


def _resolve_repository_root(base_root: Path, relative_path: PurePosixPath) -> Path:
    candidate = (
        base_root
        if relative_path == PurePosixPath(".")
        else (base_root / Path(relative_path)).resolve(strict=True)
    )
    try:
        candidate.relative_to(base_root)
    except ValueError:
        raise WriterJobError("git repository path refused") from None
    try:
        metadata = candidate.lstat()
    except OSError:
        raise WriterJobError("git repository path refused") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise WriterJobError("git repository path refused")
    return candidate


def _require_shared_writer(authority: SharedWriterAuthority) -> None:
    if (
        not isinstance(authority, SharedWriterAuthority)
        or authority.scope is not LockScope.SHARED_WRITER
    ):
        raise WriterJobError("shared writer authority mismatch")


def _count_status_marker(line: str, marker: str) -> int:
    match = re.search(rf"\[{re.escape(marker)}([0-9]+)(?:,|\])", line)
    if match is None:
        return 0
    return int(match.group(1))


def _is_conflict_line(line: str) -> bool:
    return len(line) >= 2 and (line[:2] in _UNMERGED_PREFIXES or "U" in line[:2])


def _validate_push_target(target: str, expected_digest_sha256: str | None) -> None:
    if (
        not isinstance(target, str)
        or not target
        or any(ord(character) < 32 for character in target)
        or not isinstance(expected_digest_sha256, str)
        or _SHA256.fullmatch(expected_digest_sha256) is None
        or sha256(target.encode("utf-8")).hexdigest() != expected_digest_sha256
    ):
        raise WriterJobError("git push target binding mismatch")
    if _SCP_PUSH_TARGET.fullmatch(target) is not None:
        return
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError:
        raise WriterJobError("git push target binding mismatch") from None
    if (
        parsed.scheme not in {"https", "ssh"}
        or not parsed.hostname
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.strip("/")
        or ".." in PurePosixPath(parsed.path).parts
        or (parsed.scheme == "https" and (parsed.username is not None or port not in {None, 443}))
        or (parsed.scheme == "ssh" and (parsed.username != "git" or port not in {None, 22}))
    ):
        raise WriterJobError("git push target binding mismatch")
