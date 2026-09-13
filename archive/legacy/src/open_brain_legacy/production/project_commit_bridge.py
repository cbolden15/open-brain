"""Replay-safe project-commit ingress from an editor host to the canonical writer."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Protocol

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.engine import LockScope
from open_brain_engine.storage.locks import FileLease, LockBusyError
from open_brain_engine.storage.writer_record import WriterRecordError, read_canonical_writer_record

from open_brain_legacy._compat.open_brain.config import AppConfig, ConfigError

_SCHEMA_VERSION = 1
_MAX_RECORD_BYTES = 64 * 1024
_MAX_FILES = 200
_DIGEST = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SOURCE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ENVELOPE_FIELDS = frozenset(
    {"identity_sha256", "record", "record_sha256", "schema_version", "source_id"}
)
_RECORD_FIELDS = frozenset(
    {
        "author",
        "body",
        "branch",
        "deletions",
        "files",
        "insertions",
        "kind",
        "project_path",
        "project_relpath",
        "repo",
        "sha",
        "subject",
        "ts",
        "worktree_path",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectCommitEnvelope:
    source_id: str
    identity_sha256: str
    record_sha256: str
    record: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "identity_sha256": self.identity_sha256,
            "record": self.record,
            "record_sha256": self.record_sha256,
            "schema_version": _SCHEMA_VERSION,
            "source_id": self.source_id,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class ProjectCommitResult:
    processed: int
    duplicates: int
    failed: int


class ProjectCommitTransport(Protocol):
    def submit(self, path: Path) -> None: ...


def build_project_commit_envelope(
    record: Mapping[str, object], *, source_id: str
) -> ProjectCommitEnvelope:
    normalized = _validate_record(record)
    if not isinstance(source_id, str) or _SOURCE_ID.fullmatch(source_id) is None:
        raise ValueError("invalid project commit source")
    identity = canonical_json_bytes(
        {
            "project_relpath": normalized["project_relpath"],
            "repo": normalized["repo"],
            "schema_version": _SCHEMA_VERSION,
            "sha": normalized["sha"],
            "source_id": source_id,
            "worktree_path": normalized["worktree_path"],
        }
    )
    record_bytes = canonical_json_bytes(normalized)
    return ProjectCommitEnvelope(
        source_id=source_id,
        identity_sha256=sha256(b"open-brain-project-commit-v1\0" + identity).hexdigest(),
        record_sha256=sha256(record_bytes).hexdigest(),
        record=normalized,
    )


def queue_project_commit(
    root: Path, record: Mapping[str, object], *, source_id: str
) -> bool:
    _prepare_directory(root)
    envelope = build_project_commit_envelope(record, source_id=source_id)
    destination = root / f"{envelope.identity_sha256}.json"
    payload = envelope.to_bytes()
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != payload:
            raise ValueError("project commit queue conflict")
        return False
    temporary = root / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, destination)
    except FileExistsError:
        if destination.is_symlink() or destination.read_bytes() != payload:
            raise ValueError("project commit queue conflict") from None
        return False
    finally:
        temporary.unlink(missing_ok=True)
    _fsync_directory(root)
    return True


def relay_project_commit_spool(
    root: Path,
    *,
    transport: ProjectCommitTransport,
    max_files: int = _MAX_FILES,
) -> ProjectCommitResult:
    _prepare_directory(root)
    _validate_limit(max_files)
    processed = 0
    failed = 0
    for path in sorted(root.glob("*.json"))[:max_files]:
        try:
            _read_envelope(path)
            transport.submit(path)
            path.unlink()
            _fsync_directory(root)
            processed += 1
        except Exception:
            failed += 1
    return ProjectCommitResult(processed=processed, duplicates=0, failed=failed)


def consume_project_commit_spool(
    root: Path,
    *,
    inbox_path: Path,
    state_root: Path,
    max_files: int = _MAX_FILES,
) -> ProjectCommitResult:
    _prepare_directory(root)
    _prepare_state_root(state_root)
    _validate_inbox_path(inbox_path)
    _validate_limit(max_files)
    database_path = state_root / "project-commit-ingress/receipts.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    processed = 0
    duplicates = 0
    failed = 0
    with sqlite3.connect(database_path, timeout=5) as database:
        _initialize_database(database)
        _bootstrap_inbox(database, inbox_path)
        for path in sorted(root.glob("*.json"))[:max_files]:
            try:
                envelope = _read_envelope(path)
                existing = database.execute(
                    "SELECT record_sha256 FROM accepted WHERE identity_sha256 = ?",
                    (envelope.identity_sha256,),
                ).fetchone()
                if existing is None:
                    _append_record(inbox_path, envelope.record)
                    database.execute(
                        "INSERT INTO accepted "
                        "(identity_sha256, record_sha256, source_id, accepted_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            envelope.identity_sha256,
                            envelope.record_sha256,
                            envelope.source_id,
                            _timestamp(datetime.now(UTC)),
                        ),
                    )
                    database.commit()
                    processed += 1
                elif existing[0] == envelope.record_sha256:
                    duplicates += 1
                else:
                    raise ValueError("project commit identity conflict")
                path.unlink()
                _fsync_directory(root)
            except Exception:
                database.rollback()
                failed += 1
    return ProjectCommitResult(
        processed=processed,
        duplicates=duplicates,
        failed=failed,
    )


def queue_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-brain-project-commit-queue")
    parser.add_argument("--outbox-root", type=Path, required=True)
    parser.add_argument("--source-id", default="macbook")
    parser.add_argument("--from-jsonl", type=Path)
    arguments = parser.parse_args(argv)
    queued = 0
    duplicates = 0
    failed = 0
    try:
        records = _input_records(arguments.from_jsonl)
        for record in records:
            try:
                created = queue_project_commit(
                    arguments.outbox_root.expanduser().resolve(),
                    record,
                    source_id=arguments.source_id,
                )
                queued += int(created)
                duplicates += int(not created)
            except Exception:
                failed += 1
    except Exception:
        failed += 1
    _print_result(ProjectCommitResult(queued, duplicates, failed))
    return 0 if failed == 0 else 1


def relay_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-brain-project-commit-relay")
    parser.add_argument("--outbox-root", type=Path, required=True)
    parser.add_argument("--remote-host", required=True)
    parser.add_argument("--remote-root", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = relay_project_commit_spool(
            arguments.outbox_root.expanduser().resolve(),
            transport=_ScpTransport(arguments.remote_host, arguments.remote_root),
        )
    except Exception:
        result = ProjectCommitResult(0, 0, 1)
    _print_result(result)
    return 0 if result.failed == 0 else 1


def bridge_main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="open-brain-project-commit-bridge")
    parser.add_argument("--spool-root", type=Path, required=True)
    arguments = parser.parse_args(argv)
    env = os.environ if environment is None else environment
    try:
        config = AppConfig.load(environment=env)
        writer = read_canonical_writer_record(config.state_root)
        if (
            writer is None
            or config.host_identity is None
            or writer.identity_id != config.host_identity
        ):
            raise ValueError("canonical writer authority unavailable")
        with FileLease(config.state_root, writer.identity_id).acquire(LockScope.INGRESS):
            result = consume_project_commit_spool(
                arguments.spool_root.expanduser().resolve(),
                inbox_path=config.work_root / "inbox/project-commits.jsonl",
                state_root=config.state_root,
            )
    except (
        ConfigError,
        LockBusyError,
        OSError,
        sqlite3.Error,
        ValueError,
        WriterRecordError,
    ):
        result = ProjectCommitResult(0, 0, 1)
    _print_result(result)
    return 0 if result.failed == 0 else 1


class _ScpTransport:
    def __init__(self, remote_host: str, remote_root: str) -> None:
        if not remote_host or any(char.isspace() for char in remote_host):
            raise ValueError("invalid remote host")
        root = PurePosixPath(remote_root)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("invalid remote root")
        self._remote_host = remote_host
        self._remote_root = root

    def submit(self, path: Path) -> None:
        if _DIGEST.fullmatch(path.stem) is None or path.suffix != ".json":
            raise ValueError("invalid relay record")
        suffix = secrets.token_hex(8)
        temporary = str(self._remote_root / f".{path.name}.{suffix}.incoming")
        destination = str(self._remote_root / path.name)
        subprocess.run(
            (
                "/usr/bin/scp",
                "-q",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "--",
                str(path),
                f"{self._remote_host}:{temporary}",
            ),
            check=True,
            capture_output=True,
            timeout=20,
        )
        command = (
            f"/bin/chmod 600 {shlex.quote(temporary)} && "
            f"if /usr/bin/test -e {shlex.quote(destination)}; then "
            f"/usr/bin/cmp -s {shlex.quote(temporary)} {shlex.quote(destination)} && "
            f"/bin/rm {shlex.quote(temporary)}; else "
            f"/bin/mv {shlex.quote(temporary)} {shlex.quote(destination)}; fi"
        )
        subprocess.run(
            (
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                self._remote_host,
                command,
            ),
            check=True,
            capture_output=True,
            timeout=20,
        )


def _read_envelope(path: Path) -> ProjectCommitEnvelope:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.suffix != ".json"
        or _DIGEST.fullmatch(path.stem) is None
        or path.stat().st_size > _MAX_RECORD_BYTES
    ):
        raise ValueError("invalid project commit envelope")
    payload = path.read_bytes()
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    if type(value) is not dict or frozenset(value) != _ENVELOPE_FIELDS:
        raise ValueError("invalid project commit envelope")
    record = value["record"]
    if type(record) is not dict:
        raise ValueError("invalid project commit envelope")
    envelope = build_project_commit_envelope(record, source_id=value["source_id"])
    if (
        value["schema_version"] != _SCHEMA_VERSION
        or value["identity_sha256"] != envelope.identity_sha256
        or value["record_sha256"] != envelope.record_sha256
        or path.stem != envelope.identity_sha256
        or payload != envelope.to_bytes()
    ):
        raise ValueError("invalid project commit envelope")
    return envelope


def _validate_record(record: Mapping[str, object]) -> dict[str, object]:
    if type(record) is not dict or frozenset(record) != _RECORD_FIELDS:
        raise ValueError("invalid project commit record")
    normalized = dict(record)
    for field in (
        "author",
        "body",
        "branch",
        "project_path",
        "project_relpath",
        "repo",
        "sha",
        "subject",
        "ts",
        "worktree_path",
    ):
        value = normalized[field]
        if not isinstance(value, str) or len(value.encode("utf-8")) > 16_384:
            raise ValueError("invalid project commit field")
    if (
        normalized["kind"] != "commit"
        or normalized["repo"] != normalized["project_relpath"]
        or not normalized["repo"]
        or PurePosixPath(str(normalized["repo"])).is_absolute()
        or ".." in PurePosixPath(str(normalized["repo"])).parts
        or not Path(str(normalized["project_path"])).is_absolute()
        or not Path(str(normalized["worktree_path"])).is_absolute()
        or _REVISION.fullmatch(str(normalized["sha"])) is None
    ):
        raise ValueError("invalid project commit identity")
    try:
        timestamp = datetime.fromisoformat(str(normalized["ts"]).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid project commit timestamp") from None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("invalid project commit timestamp")
    for field in ("files", "insertions", "deletions"):
        value = normalized[field]
        if type(value) is not int or value < 0:
            raise ValueError("invalid project commit statistic")
    return normalized


def _initialize_database(database: sqlite3.Connection) -> None:
    database.execute("PRAGMA journal_mode=WAL")
    database.execute(
        "CREATE TABLE IF NOT EXISTS accepted ("
        "identity_sha256 TEXT PRIMARY KEY, "
        "record_sha256 TEXT NOT NULL, "
        "source_id TEXT NOT NULL, "
        "accepted_at TEXT NOT NULL)"
    )
    database.commit()


def _bootstrap_inbox(database: sqlite3.Connection, inbox_path: Path) -> None:
    if not inbox_path.exists():
        return
    if inbox_path.is_symlink() or not inbox_path.is_file():
        raise ValueError("invalid project commit inbox")
    for line in inbox_path.read_bytes().splitlines():
        if not line:
            continue
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict:
            raise ValueError("invalid project commit inbox")
        envelope = build_project_commit_envelope(value, source_id="macbook")
        existing = database.execute(
            "SELECT record_sha256 FROM accepted WHERE identity_sha256 = ?",
            (envelope.identity_sha256,),
        ).fetchone()
        if existing is not None and existing[0] != envelope.record_sha256:
            raise ValueError("project commit inbox identity conflict")
        database.execute(
            "INSERT OR IGNORE INTO accepted "
            "(identity_sha256, record_sha256, source_id, accepted_at) "
            "VALUES (?, ?, ?, ?)",
            (
                envelope.identity_sha256,
                envelope.record_sha256,
                envelope.source_id,
                _timestamp(datetime.now(UTC)),
            ),
        )
    database.commit()


def _append_record(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError("invalid project commit inbox")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, canonical_json_bytes(record) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _input_records(path: Path | None) -> list[dict[str, object]]:
    if path is None:
        payload = sys.stdin.buffer.read(_MAX_RECORD_BYTES + 1)
        if len(payload) > _MAX_RECORD_BYTES:
            raise ValueError("project commit input too large")
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict:
            raise ValueError("invalid project commit input")
        return [value]
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("invalid project commit JSONL input")
    records: list[dict[str, object]] = []
    for line in path.read_bytes().splitlines():
        if not line:
            continue
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict:
            raise ValueError("invalid project commit JSONL input")
        records.append(value)
    return records


def _prepare_directory(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("invalid project commit spool root")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("invalid project commit spool root")


def _prepare_state_root(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise ValueError("invalid project commit state root")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _validate_inbox_path(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or path.name != "project-commits.jsonl":
        raise ValueError("invalid project commit inbox")


def _validate_limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 500:
        raise ValueError("invalid project commit limit")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("project commit write failed")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _print_result(result: ProjectCommitResult) -> None:
    print(
        json.dumps(
            {
                "duplicates": result.duplicates,
                "failed": result.failed,
                "processed": result.processed,
                "status": "ok" if result.failed == 0 else "failed",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    raise SystemExit(bridge_main())
