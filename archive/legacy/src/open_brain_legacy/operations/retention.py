"""Dry-run-first, replay-safe retention for confined synthetic roots."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath


class RetentionError(RuntimeError):
    """Base error for retention contract violations."""


class RetentionApprovalError(RetentionError):
    """Apply was not bound to the exact approved plan and root."""


class RetentionPathError(RetentionError):
    """A root or candidate path was unsafe."""


class RetentionPlanStaleError(RetentionError):
    """A candidate changed after planning."""


class RetentionReplayConflictError(RetentionError):
    """A replay key was already bound to a different plan."""


class RetentionArtifactKind(StrEnum):
    EXPIRABLE = "expirable"
    RECOVERY_CRITICAL = "recovery_critical"


class RetentionDisposition(StrEnum):
    DRY_RUN = "dry_run"
    APPLIED = "applied"
    PARTIAL_FAILURE = "partial_failure"


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    artifact_id: str
    relative_path: str
    expires_at: datetime
    kind: RetentionArtifactKind

    def __post_init__(self) -> None:
        if (
            not isinstance(self.artifact_id, str)
            or _OPAQUE_ID.fullmatch(self.artifact_id) is None
            or not isinstance(self.relative_path, str)
            or not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or not isinstance(self.kind, RetentionArtifactKind)
        ):
            raise RetentionError("invalid retention candidate")


@dataclass(frozen=True, slots=True)
class _CandidateSnapshot:
    artifact_id: str
    device: int
    digest_sha256: str
    inode: int
    relative_path: PurePosixPath
    size_bytes: int

    def to_identity(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "device": self.device,
            "digest_sha256": self.digest_sha256,
            "inode": self.inode,
            "relative_path": str(self.relative_path),
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class _CandidateRead:
    payload: bytes
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    root: Path
    cutoff: datetime
    deletions: tuple[RetentionCandidate, ...]
    protected_count: int
    digest_sha256: str
    _snapshots: tuple[_CandidateSnapshot, ...]


@dataclass(frozen=True, slots=True)
class RetentionReceipt:
    disposition: RetentionDisposition
    deleted_count: int
    failure_count: int
    protected_count: int
    replayed: bool
    plan_digest_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted_count": self.deleted_count,
            "disposition": self.disposition.value,
            "failure_count": self.failure_count,
            "plan_digest_sha256": self.plan_digest_sha256,
            "protected_count": self.protected_count,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class _ReplayState:
    plan_digest_sha256: str
    replay_digest_sha256: str
    deleted_ids: tuple[str, ...]
    pending_id: str | None
    quarantine_name: str | None
    complete: bool

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "complete": self.complete,
                "deleted_ids": list(self.deleted_ids),
                "pending_id": self.pending_id,
                "quarantine_name": self.quarantine_name,
                "plan_digest_sha256": self.plan_digest_sha256,
                "replay_digest_sha256": self.replay_digest_sha256,
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


@dataclass(frozen=True, slots=True)
class _JournalHandle:
    directory_fd: int
    state_name: str


_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_JOURNAL_DIRECTORY = ".open-brain-retention"


def plan_retention(
    *,
    root: Path,
    cutoff: datetime,
    candidates: tuple[RetentionCandidate, ...],
) -> RetentionPlan:
    """Build a non-mutating plan bound to exact candidate bytes and root."""
    confined_root = _validate_root(root)
    if (
        not isinstance(cutoff, datetime)
        or cutoff.tzinfo is None
        or cutoff.utcoffset() is None
        or not isinstance(candidates, tuple)
        or any(not isinstance(candidate, RetentionCandidate) for candidate in candidates)
    ):
        raise RetentionError("invalid retention plan request")
    if len({candidate.artifact_id for candidate in candidates}) != len(candidates):
        raise RetentionError("duplicate retention candidate")

    deletions: list[RetentionCandidate] = []
    snapshots: list[_CandidateSnapshot] = []
    protected_count = 0
    seen_paths: set[PurePosixPath] = set()
    for candidate in candidates:
        relative, read = _candidate_path(confined_root, candidate.relative_path)
        if relative in seen_paths:
            raise RetentionError("duplicate retention candidate path")
        seen_paths.add(relative)
        if candidate.kind is RetentionArtifactKind.RECOVERY_CRITICAL:
            protected_count += 1
            continue
        if candidate.expires_at >= cutoff:
            continue
        deletions.append(candidate)
        snapshots.append(
            _CandidateSnapshot(
                artifact_id=candidate.artifact_id,
                device=read.device,
                digest_sha256=sha256(read.payload).hexdigest(),
                inode=read.inode,
                relative_path=relative,
                size_bytes=len(read.payload),
            )
        )

    paired = sorted(
        zip(deletions, snapshots, strict=True),
        key=lambda pair: (pair[0].artifact_id, str(pair[1].relative_path)),
    )
    ordered_deletions = tuple(pair[0] for pair in paired)
    ordered_snapshots = tuple(pair[1] for pair in paired)
    identity = {
        "cutoff": cutoff.isoformat(),
        "deletions": [snapshot.to_identity() for snapshot in ordered_snapshots],
        "protected_count": protected_count,
        "root_digest_sha256": sha256(str(confined_root).encode()).hexdigest(),
    }
    digest = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return RetentionPlan(
        root=confined_root,
        cutoff=cutoff,
        deletions=ordered_deletions,
        protected_count=protected_count,
        digest_sha256=digest,
        _snapshots=ordered_snapshots,
    )


def run_retention(
    *,
    root: Path,
    plan: RetentionPlan,
    replay_key: str,
    apply: bool = False,
    approval_digest: str | None = None,
    deleter: Callable[[Path], None] | None = None,
    after_quarantine: Callable[[], None] | None = None,
) -> RetentionReceipt:
    """Preview by default; apply only an exact approved plan with durable replay."""
    confined_root = _validate_root(root)
    if (
        not isinstance(plan, RetentionPlan)
        or not isinstance(replay_key, str)
        or _OPAQUE_ID.fullmatch(replay_key) is None
        or type(apply) is not bool
    ):
        raise RetentionError("invalid retention run request")
    if plan.root != confined_root:
        raise RetentionApprovalError("retention plan does not match root")
    if not apply:
        return RetentionReceipt(
            disposition=RetentionDisposition.DRY_RUN,
            deleted_count=0,
            failure_count=0,
            protected_count=plan.protected_count,
            replayed=False,
            plan_digest_sha256=plan.digest_sha256,
        )
    if approval_digest != plan.digest_sha256:
        raise RetentionApprovalError("retention approval does not match plan")

    replay_digest = sha256(replay_key.encode()).hexdigest()
    with _locked_journal(confined_root, replay_digest) as journal:
        return _run_retention_locked(
            root=confined_root,
            plan=plan,
            replay_digest=replay_digest,
            journal=journal,
            deleter=deleter,
            after_quarantine=after_quarantine,
        )


def _run_retention_locked(
    *,
    root: Path,
    plan: RetentionPlan,
    replay_digest: str,
    journal: _JournalHandle,
    deleter: Callable[[Path], None] | None,
    after_quarantine: Callable[[], None] | None,
) -> RetentionReceipt:
    state = _load_state(journal)
    replayed = state is not None
    if state is None:
        state = _ReplayState(plan.digest_sha256, replay_digest, (), None, None, False)
        _write_state(journal, state)
    elif (
        state.plan_digest_sha256 != plan.digest_sha256
        or state.replay_digest_sha256 != replay_digest
    ):
        raise RetentionReplayConflictError("retention replay conflicts with plan")

    state = _reconcile_state(plan, state, journal)
    if state.complete:
        return _receipt(plan, state, failure_count=0, replayed=True)

    failure_count = 0
    snapshots = {snapshot.artifact_id: snapshot for snapshot in plan._snapshots}
    for candidate in plan.deletions:
        if candidate.artifact_id in state.deleted_ids:
            continue
        snapshot = snapshots[candidate.artifact_id]
        path = root.joinpath(*snapshot.relative_path.parts)
        quarantine_name = ".delete-" + secrets.token_hex(16)
        state = _ReplayState(
            state.plan_digest_sha256,
            state.replay_digest_sha256,
            state.deleted_ids,
            candidate.artifact_id,
            quarantine_name,
            False,
        )
        _write_state(journal, state)
        try:
            if deleter is None:
                _unlink_candidate(
                    root,
                    snapshot,
                    journal,
                    quarantine_name,
                    after_quarantine=after_quarantine,
                )
            else:
                deleter(path)
        except (RetentionPathError, RetentionPlanStaleError):
            raise
        except Exception:
            failure_count += 1
            break
        state = _ReplayState(
            state.plan_digest_sha256,
            state.replay_digest_sha256,
            (*state.deleted_ids, candidate.artifact_id),
            None,
            None,
            False,
        )
        _write_state(journal, state)

    if failure_count == 0 and len(state.deleted_ids) == len(plan.deletions):
        state = _ReplayState(
            state.plan_digest_sha256,
            state.replay_digest_sha256,
            state.deleted_ids,
            None,
            None,
            True,
        )
        _write_state(journal, state)
    return _receipt(plan, state, failure_count=failure_count, replayed=replayed)


def _reconcile_state(
    plan: RetentionPlan,
    state: _ReplayState,
    journal: _JournalHandle,
) -> _ReplayState:
    snapshots = {snapshot.artifact_id: snapshot for snapshot in plan._snapshots}
    deleted = list(state.deleted_ids)
    if state.pending_id is not None:
        pending_id = state.pending_id
        quarantine_name = state.quarantine_name
        snapshot = snapshots.get(pending_id)
        if snapshot is None:
            raise RetentionReplayConflictError("retention replay has unknown pending item")
        original = _read_candidate_confined(
            plan.root,
            snapshot.relative_path,
            missing_ok=True,
            stale=True,
        )
        quarantined = (
            _try_read_journal_entry(journal, state.quarantine_name)
            if quarantine_name is not None
            else None
        )
        if quarantined is not None:
            assert quarantine_name is not None
            if _matches_snapshot(quarantined, snapshot):
                os.unlink(quarantine_name, dir_fd=journal.directory_fd)
                os.fsync(journal.directory_fd)
                deleted.append(pending_id)
                state = _ReplayState(
                    state.plan_digest_sha256,
                    state.replay_digest_sha256,
                    tuple(dict.fromkeys(deleted)),
                    None,
                    None,
                    False,
                )
                _write_state(journal, state)
            else:
                if original is None:
                    _restore_quarantine(
                        plan.root,
                        snapshot.relative_path,
                        journal,
                        quarantine_name,
                    )
                raise RetentionPlanStaleError("retention plan is stale")
        elif original is None:
            deleted.append(pending_id)
            state = _ReplayState(
                state.plan_digest_sha256,
                state.replay_digest_sha256,
                tuple(dict.fromkeys(deleted)),
                None,
                None,
                False,
            )
            _write_state(journal, state)
        elif _matches_snapshot(original, snapshot):
            state = _ReplayState(
                state.plan_digest_sha256,
                state.replay_digest_sha256,
                state.deleted_ids,
                state.pending_id,
                None,
                False,
            )
            _write_state(journal, state)
        else:
            raise RetentionPlanStaleError("retention plan is stale")

    for snapshot in plan._snapshots:
        payload = _read_candidate_confined(
            plan.root,
            snapshot.relative_path,
            missing_ok=True,
            stale=True,
        )
        if snapshot.artifact_id in state.deleted_ids:
            if payload is not None:
                raise RetentionPlanStaleError("retention plan is stale")
            continue
        if payload is None:
            raise RetentionPlanStaleError("retention plan is stale")
        if not _matches_snapshot(payload, snapshot):
            raise RetentionPlanStaleError("retention plan is stale")
    return state


def _receipt(
    plan: RetentionPlan,
    state: _ReplayState,
    *,
    failure_count: int,
    replayed: bool,
) -> RetentionReceipt:
    return RetentionReceipt(
        disposition=(
            RetentionDisposition.APPLIED
            if state.complete
            else RetentionDisposition.PARTIAL_FAILURE
        ),
        deleted_count=len(state.deleted_ids),
        failure_count=failure_count,
        protected_count=plan.protected_count,
        replayed=replayed,
        plan_digest_sha256=plan.digest_sha256,
    )


def _validate_root(root: Path) -> Path:
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or not root.is_dir()
        or root.is_symlink()
    ):
        raise RetentionPathError("invalid retention root")
    return root.resolve(strict=True)


def _candidate_path(root: Path, raw: str) -> tuple[PurePosixPath, _CandidateRead]:
    if (
        not isinstance(raw, str)
        or not raw
        or raw.startswith("/")
        or "\\" in raw
        or "\x00" in raw
    ):
        raise RetentionPathError("unsafe candidate path")
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.parts[0] == _JOURNAL_DIRECTORY
    ):
        raise RetentionPathError("unsafe candidate path")
    payload = _read_candidate_confined(root, relative, missing_ok=False, stale=False)
    if payload is None:
        raise RetentionPathError("unsafe candidate path")
    return relative, payload


def _read_candidate_confined(
    root: Path,
    relative: PurePosixPath,
    *,
    missing_ok: bool,
    stale: bool,
) -> _CandidateRead | None:
    root_fd = _open_root_fd(root)
    try:
        with _open_parent_fd(root_fd, relative) as (parent_fd, name):
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 65_536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return _CandidateRead(
                    payload=b"".join(chunks),
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                )
            finally:
                os.close(descriptor)
    except (FileNotFoundError, OSError):
        if missing_ok:
            return None
        if stale:
            raise RetentionPlanStaleError("retention plan is stale") from None
        raise RetentionPathError("unsafe candidate path") from None
    finally:
        os.close(root_fd)


def _unlink_candidate(
    root: Path,
    snapshot: _CandidateSnapshot,
    journal: _JournalHandle,
    quarantine_name: str,
    *,
    after_quarantine: Callable[[], None] | None,
) -> None:
    root_fd = _open_root_fd(root)
    try:
        with _open_parent_fd(root_fd, snapshot.relative_path) as (parent_fd, name):
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise RetentionPathError("unsafe candidate path")
            os.replace(
                name,
                quarantine_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=journal.directory_fd,
            )
            os.fsync(parent_fd)
            os.fsync(journal.directory_fd)
            if after_quarantine is not None:
                after_quarantine()
            captured = _read_journal_entry(journal, quarantine_name)
            if not _matches_snapshot(captured, snapshot):
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.replace(
                        quarantine_name,
                        name,
                        src_dir_fd=journal.directory_fd,
                        dst_dir_fd=parent_fd,
                    )
                    os.fsync(parent_fd)
                    os.fsync(journal.directory_fd)
                quarantine_name = ""
                raise RetentionPlanStaleError("retention plan is stale")
            os.unlink(quarantine_name, dir_fd=journal.directory_fd)
            os.fsync(journal.directory_fd)
            quarantine_name = ""
    except RetentionPathError:
        raise
    except RetentionPlanStaleError:
        raise
    except OSError:
        raise RetentionPathError("unsafe candidate path") from None
    finally:
        os.close(root_fd)


def _read_journal_entry(
    journal: _JournalHandle,
    name: str,
) -> _CandidateRead:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=journal.directory_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RetentionPathError("unsafe retention quarantine")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        return _CandidateRead(
            payload=b"".join(chunks),
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    finally:
        os.close(descriptor)


def _try_read_journal_entry(
    journal: _JournalHandle,
    name: str | None,
) -> _CandidateRead | None:
    if name is None:
        return None
    try:
        return _read_journal_entry(journal, name)
    except FileNotFoundError:
        return None


def _restore_quarantine(
    root: Path,
    relative: PurePosixPath,
    journal: _JournalHandle,
    quarantine_name: str,
) -> None:
    root_fd = _open_root_fd(root)
    try:
        with _open_parent_fd(root_fd, relative) as (parent_fd, name):
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.replace(
                    quarantine_name,
                    name,
                    src_dir_fd=journal.directory_fd,
                    dst_dir_fd=parent_fd,
                )
                os.fsync(parent_fd)
                os.fsync(journal.directory_fd)
                return
            raise RetentionPlanStaleError("retention plan is stale")
    finally:
        os.close(root_fd)


def _matches_snapshot(
    candidate: _CandidateRead,
    snapshot: _CandidateSnapshot,
) -> bool:
    return (
        candidate.device == snapshot.device
        and candidate.inode == snapshot.inode
        and len(candidate.payload) == snapshot.size_bytes
        and sha256(candidate.payload).hexdigest() == snapshot.digest_sha256
    )


def _open_root_fd(root: Path) -> int:
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise RetentionPathError("invalid retention root") from None
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RetentionPathError("invalid retention root")
    return descriptor


@contextmanager
def _open_parent_fd(
    root_fd: int,
    relative: PurePosixPath,
) -> Iterator[tuple[int, str]]:
    descriptors: list[int] = []
    current = root_fd
    try:
        for part in relative.parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise OSError
            descriptors.append(child)
            current = child
        yield current, relative.parts[-1]
    except OSError:
        raise RetentionPathError("unsafe candidate path") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _locked_journal(root: Path, replay_digest: str) -> Iterator[_JournalHandle]:
    root_fd = _open_root_fd(root)
    directory_fd = -1
    lock_fd = -1
    try:
        try:
            os.mkdir(_JOURNAL_DIRECTORY, mode=0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(
            _JOURNAL_DIRECTORY,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise OSError
        lock_fd = os.open(
            f"{replay_digest}.lock",
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield _JournalHandle(directory_fd, f"{replay_digest}.json")
    except OSError:
        raise RetentionPathError("unsafe retention journal") from None
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(root_fd)


def _load_state(journal: _JournalHandle) -> _ReplayState | None:
    descriptor = -1
    try:
        descriptor = os.open(
            journal.state_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=journal.directory_fd,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        value = json.loads(b"".join(chunks))
        state = _ReplayState(
            plan_digest_sha256=value["plan_digest_sha256"],
            replay_digest_sha256=value["replay_digest_sha256"],
            deleted_ids=tuple(value["deleted_ids"]),
            pending_id=value["pending_id"],
            quarantine_name=value.get("quarantine_name"),
            complete=value["complete"],
        )
    except FileNotFoundError:
        return None
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        raise RetentionReplayConflictError("invalid retention replay state") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        _SHA256.fullmatch(state.plan_digest_sha256) is None
        or _SHA256.fullmatch(state.replay_digest_sha256) is None
        or len(set(state.deleted_ids)) != len(state.deleted_ids)
        or any(_OPAQUE_ID.fullmatch(item) is None for item in state.deleted_ids)
        or state.pending_id is not None
        and _OPAQUE_ID.fullmatch(state.pending_id) is None
        or state.quarantine_name is not None
        and re.fullmatch(r"\.delete-[0-9a-f]{32}", state.quarantine_name) is None
        or type(state.complete) is not bool
    ):
        raise RetentionReplayConflictError("invalid retention replay state")
    return state


def _write_state(journal: _JournalHandle, state: _ReplayState) -> None:
    temporary = ".retention-" + secrets.token_hex(16)
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=journal.directory_fd,
        )
        payload = state.to_bytes()
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            journal.state_name,
            src_dir_fd=journal.directory_fd,
            dst_dir_fd=journal.directory_fd,
        )
        os.fsync(journal.directory_fd)
    except OSError:
        raise RetentionError("retention journal write failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=journal.directory_fd)
