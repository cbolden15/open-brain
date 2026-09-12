"""Metadata-only bounded appliance run history over scheduler receipts."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

from open_brain_engine.engine import canonical_json_bytes
from open_brain_engine.storage.operational import RootIdentity, read_confined

from open_brain.profile import open_existing_single_user_local
from open_brain.services.appliance_scheduler import (
    APPLIANCE_SCHEDULER_DIRECTORY,
    MAXIMUM_RETAINED_RUN_RECEIPTS,
    ApplianceRunReceipt,
    is_appliance_job_name,
)

_RUNS_DIRECTORY = APPLIANCE_SCHEDULER_DIRECTORY / "runs"
_MAXIMUM_HISTORY_LIMIT = 20
_MAXIMUM_HISTORY_BYTES = 16_384
_MAXIMUM_RECEIPT_BYTES = 2_048
_MAXIMUM_SCANNED_ENTRIES = MAXIMUM_RETAINED_RUN_RECEIPTS * 16


@dataclass(frozen=True, slots=True)
class ApplianceRunHistory:
    runs: tuple[ApplianceRunReceipt, ...]
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "runs": [run.to_dict() for run in self.runs],
            "status": "ok",
            "truncated": self.truncated,
        }


def read_appliance_run_history(
    root: Path,
    *,
    limit: int = 10,
    maximum_bytes: int = _MAXIMUM_HISTORY_BYTES,
) -> ApplianceRunHistory:
    if (
        not isinstance(root, Path)
        or type(limit) is not int
        or not 1 <= limit <= _MAXIMUM_HISTORY_LIMIT
        or type(maximum_bytes) is not int
        or not 1_024 <= maximum_bytes <= 65_536
    ):
        raise ValueError("invalid appliance run history request")
    profile = open_existing_single_user_local(root)
    receipts, scan_truncated = _iter_receipts(profile.root, profile.root_identity)
    sorted_receipts = sorted(
        receipts,
        key=lambda item: (item.finished_at, item.started_at, item.job_name, item.run_id),
        reverse=True,
    )
    selected: list[ApplianceRunReceipt] = []
    truncated = scan_truncated or len(sorted_receipts) > limit
    for receipt in sorted_receipts:
        if len(selected) >= limit:
            truncated = True
            break
        candidate = {
            "runs": [run.to_dict() for run in (*selected, receipt)],
            "status": "ok",
            "truncated": truncated,
        }
        if len(canonical_json_bytes(candidate)) > maximum_bytes:
            truncated = True
            break
        selected.append(receipt)
    return ApplianceRunHistory(runs=tuple(selected), truncated=truncated)


def last_successful_run(history: ApplianceRunHistory) -> ApplianceRunReceipt | None:
    if not isinstance(history, ApplianceRunHistory):
        raise ValueError("invalid appliance run history")
    for receipt in history.runs:
        if receipt.status in {"completed", "empty"}:
            return receipt
    return None


def _iter_receipts(
    root: Path,
    root_identity: RootIdentity,
) -> tuple[tuple[ApplianceRunReceipt, ...], bool]:
    runs_root = root / _RUNS_DIRECTORY
    try:
        root_metadata = runs_root.lstat()
    except FileNotFoundError:
        return (), False
    except OSError:
        return (), True
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        return (), True
    receipts: list[ApplianceRunReceipt] = []
    scanned_entries = 0
    try:
        job_directories = runs_root.iterdir()
        for job_directory in job_directories:
            scanned_entries += 1
            if scanned_entries > _MAXIMUM_SCANNED_ENTRIES:
                return tuple(receipts), True
            metadata = job_directory.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or not is_appliance_job_name(job_directory.name)
            ):
                continue
            for entry in job_directory.iterdir():
                scanned_entries += 1
                if scanned_entries > _MAXIMUM_SCANNED_ENTRIES:
                    return tuple(receipts), True
                entry_metadata = entry.lstat()
                if (
                    stat.S_ISLNK(entry_metadata.st_mode)
                    or not stat.S_ISREG(entry_metadata.st_mode)
                    or not entry.name.endswith(".json")
                ):
                    continue
                relative = _RUNS_DIRECTORY / job_directory.name / entry.name
                payload = read_confined(
                    root=root,
                    relative=relative.as_posix(),
                    expected_root_identity=root_identity,
                    maximum_bytes=_MAXIMUM_RECEIPT_BYTES,
                )
                if payload is None:
                    continue
                try:
                    receipts.append(_receipt_from_bytes(payload, job_name=job_directory.name))
                except ValueError:
                    continue
    except OSError:
        return tuple(receipts), True
    return tuple(receipts), False


def _receipt_from_bytes(payload: bytes, *, job_name: str) -> ApplianceRunReceipt:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid appliance run receipt") from None
    if (
        type(value) is not dict
        or canonical_json_bytes(value) != payload
        or set(value)
        != {
            "attempt",
            "finished_at",
            "job_name",
            "next_due_at",
            "reason",
            "run_id",
            "started_at",
            "status",
        }
        or value.get("job_name") != job_name
    ):
        raise ValueError("invalid appliance run receipt")
    return ApplianceRunReceipt(
        attempt=value["attempt"],
        finished_at=value["finished_at"],
        job_name=value["job_name"],
        next_due_at=value["next_due_at"],
        reason=value["reason"],
        run_id=value["run_id"],
        started_at=value["started_at"],
        status=value["status"],
    )


__all__ = [
    "ApplianceRunHistory",
    "last_successful_run",
    "read_appliance_run_history",
]
