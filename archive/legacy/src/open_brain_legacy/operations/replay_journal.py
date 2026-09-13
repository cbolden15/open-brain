from __future__ import annotations

import re
import sqlite3
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.ports import Clock
from open_brain_engine.storage.sqlite import Migration, connect_database, migrate

from .writer_jobs import (
    JobRunDisposition,
    JobRunResult,
    ScheduledEffect,
    WriterJobError,
)

_JOB_ID = re.compile(r"JOB-[0-9]{3}")
_REPLAY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCHEMA_VERSION = 1
_STATEMENTS = (
    """
CREATE TABLE writer_job_runs (
    job_id TEXT NOT NULL,
    replay_key TEXT NOT NULL,
    request_digest_sha256 TEXT NOT NULL CHECK (length(request_digest_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
    disposition TEXT,
    effect TEXT,
    effect_count INTEGER,
    review_items_queued INTEGER,
    approved_inputs_applied INTEGER,
    PRIMARY KEY (job_id, replay_key),
    CHECK (
        state = 'pending'
        OR disposition IS NOT NULL
        AND effect IS NOT NULL
        AND effect_count >= 0
        AND review_items_queued >= 0
        AND approved_inputs_applied >= 0
    )
)
""".strip(),
)


def _migration() -> Migration:
    version = 1
    name = "writer-job-runs"
    checksum = sha256(
        canonical_json_bytes({"version": version, "name": name, "statements": list(_STATEMENTS)})
    ).hexdigest()
    return Migration(version, name, checksum, _STATEMENTS)


_MIGRATIONS = (_migration(),)


def _validate_identity(job_id: str, replay_key: str) -> None:
    if (
        not isinstance(job_id, str)
        or _JOB_ID.fullmatch(job_id) is None
        or not isinstance(replay_key, str)
        or _REPLAY_KEY.fullmatch(replay_key) is None
    ):
        raise WriterJobError("invalid replay identity")


def _validate_digest(value: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise WriterJobError("invalid replay request digest")


def _validate_result(result: JobRunResult) -> None:
    if not isinstance(result, JobRunResult):
        raise WriterJobError("invalid replay result")
    _validate_identity(result.job_id, result.replay_key)
    _validate_digest(result.request_digest_sha256)
    if (
        not isinstance(result.disposition, JobRunDisposition)
        or not isinstance(result.effect, ScheduledEffect)
        or any(
            type(value) is not int or value < 0
            for value in (
                result.effect_count,
                result.review_items_queued,
                result.approved_inputs_applied,
            )
        )
    ):
        raise WriterJobError("invalid replay result")


class SqliteReplayJournal:
    """Root-confined durable replay state for scheduled writer jobs."""

    def __init__(
        self,
        *,
        root: Path,
        clock: Clock,
        database_name: str | PurePosixPath = "operations/replay-journal.sqlite3",
    ) -> None:
        self._connection = connect_database(root=root, database_name=database_name)
        try:
            migrate(
                self._connection,
                clock=clock,
                migrations=_MIGRATIONS,
                schema_version=_SCHEMA_VERSION,
            )
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SqliteReplayJournal:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def completed(self, job_id: str, replay_key: str) -> JobRunResult | None:
        _validate_identity(job_id, replay_key)
        try:
            row = self._connection.execute(
                "SELECT * FROM writer_job_runs WHERE job_id = ? AND replay_key = ?",
                (job_id, replay_key),
            ).fetchone()
            if row is None or row["state"] == "pending":
                return None
            if row["state"] != "completed":
                raise WriterJobError("invalid replay journal entry")
            return self._result_from_row(row)
        except WriterJobError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            raise WriterJobError("invalid replay journal entry") from None

    def begin(self, job_id: str, replay_key: str, request_digest_sha256: str) -> None:
        _validate_identity(job_id, replay_key)
        _validate_digest(request_digest_sha256)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT request_digest_sha256 FROM writer_job_runs "
                "WHERE job_id = ? AND replay_key = ?",
                (job_id, replay_key),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO writer_job_runs("
                    "job_id, replay_key, request_digest_sha256, state"
                    ") VALUES (?, ?, ?, 'pending')",
                    (job_id, replay_key, request_digest_sha256),
                )
            elif row["request_digest_sha256"] != request_digest_sha256:
                raise WriterJobError("replay digest conflict")
            self._connection.commit()
        except WriterJobError:
            self._connection.rollback()
            raise
        except sqlite3.Error:
            self._connection.rollback()
            raise WriterJobError("replay journal begin failed") from None

    def complete(self, result: JobRunResult) -> None:
        _validate_result(result)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM writer_job_runs WHERE job_id = ? AND replay_key = ?",
                (result.job_id, result.replay_key),
            ).fetchone()
            if row is not None and row["request_digest_sha256"] != result.request_digest_sha256:
                raise WriterJobError("replay digest conflict")
            if row is not None and row["state"] == "completed":
                stored = self._result_from_row(row)
                if stored != result:
                    raise WriterJobError("replay result conflict")
            elif row is None:
                self._insert_completed(result)
            else:
                self._update_completed(result)
            self._connection.commit()
        except WriterJobError:
            self._connection.rollback()
            raise
        except sqlite3.Error:
            self._connection.rollback()
            raise WriterJobError("replay journal completion failed") from None

    def _result_from_row(self, row: sqlite3.Row) -> JobRunResult:
        try:
            result = JobRunResult(
                job_id=row["job_id"],
                replay_key=row["replay_key"],
                request_digest_sha256=row["request_digest_sha256"],
                disposition=JobRunDisposition(row["disposition"]),
                effect=ScheduledEffect(row["effect"]),
                effect_count=row["effect_count"],
                review_items_queued=row["review_items_queued"],
                approved_inputs_applied=row["approved_inputs_applied"],
            )
            _validate_result(result)
            return result
        except (TypeError, ValueError):
            raise WriterJobError("invalid replay journal entry") from None

    def _insert_completed(self, result: JobRunResult) -> None:
        self._connection.execute(
            "INSERT INTO writer_job_runs("
            "job_id, replay_key, request_digest_sha256, state, disposition, effect, "
            "effect_count, review_items_queued, approved_inputs_applied"
            ") VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?)",
            self._result_values(result),
        )

    def _update_completed(self, result: JobRunResult) -> None:
        self._connection.execute(
            "UPDATE writer_job_runs SET state = 'completed', disposition = ?, effect = ?, "
            "effect_count = ?, review_items_queued = ?, approved_inputs_applied = ? "
            "WHERE job_id = ? AND replay_key = ? AND request_digest_sha256 = ?",
            (
                result.disposition.value,
                result.effect.value,
                result.effect_count,
                result.review_items_queued,
                result.approved_inputs_applied,
                result.job_id,
                result.replay_key,
                result.request_digest_sha256,
            ),
        )

    @staticmethod
    def _result_values(result: JobRunResult) -> tuple[object, ...]:
        return (
            result.job_id,
            result.replay_key,
            result.request_digest_sha256,
            result.disposition.value,
            result.effect.value,
            result.effect_count,
            result.review_items_queued,
            result.approved_inputs_applied,
        )
