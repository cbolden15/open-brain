"""Guide workflow journal v1: private files, closed shape, CAS writes and lifetime guard."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import cast

import pytest

from open_brain.services import source_guide_journal as journal_module
from open_brain.services.source_guide_journal import (
    MAX_JOURNAL_BYTES,
    JournalError,
    WorkflowJournal,
    canonical_json,
    new_journal,
    operation_record,
    validate_journal,
)

EXECUTABLE = {"path": "/opt/collector", "sha256": "e" * 64, "device": 1, "inode": 2}


def _binding(tmp_path: Path) -> dict[str, object]:
    return {
        "brain_id": "brain_synthetic",
        "issuer_epoch": 1,
        "brain_root": str(tmp_path / "brain"),
        "collector_state": str(tmp_path / "collector" / "state.json"),
        "collector_binding": "sha256:" + "c" * 64,
        "executable": dict(EXECUTABLE),
    }


def _private(tmp_path: Path) -> Path:
    directory = tmp_path / "guide"
    directory.mkdir(mode=0o700)
    return directory / "journal.json"


def _code(error: pytest.ExceptionInfo[JournalError]) -> str:
    return error.value.code


def _operation(sequence: int = 0) -> dict[str, object]:
    return operation_record(
        sequence=sequence,
        action="preview",
        request={"source_id": "slack.0123456789abcdef01234567"},
        expected_versions=[{"kind": "collector", "id": "slack.x", "version": "g1"}],
        execution_binding={
            "brain_id": "brain_synthetic",
            "issuer_epoch": 1,
            "collector_binding": "sha256:" + "c" * 64,
            "source_generation": "g1",
            "control_epoch": 0,
            "executable": dict(EXECUTABLE),
        },
    )


def test_create_is_private_atomic_and_never_replaces(tmp_path: Path) -> None:
    path = _private(tmp_path)
    journal = WorkflowJournal(path)
    document = new_journal("guide:synthetic-1", _binding(tmp_path))
    journal.create(document)

    for item in (path, journal.guard_path, journal.lock_path):
        info = os.lstat(item)
        assert oct(info.st_mode & 0o777) == "0o600" and info.st_nlink == 1
    assert journal.read() == document
    assert document["revision"] == 1 and document["status"] == "active"
    with pytest.raises(JournalError) as error:
        journal.create(new_journal("guide:other", _binding(tmp_path)))
    assert _code(error) == "revision_conflict"
    assert journal.read() == document


def test_absent_journal_reads_none_without_creating_anything(tmp_path: Path) -> None:
    path = _private(tmp_path)
    assert WorkflowJournal(path).read() is None
    assert list(path.parent.iterdir()) == []


@pytest.mark.parametrize("mode", [0o755, 0o750])
def test_unsafe_parent_refuses(tmp_path: Path, mode: int) -> None:
    path = _private(tmp_path)
    path.parent.chmod(mode)
    with pytest.raises(JournalError) as error:
        WorkflowJournal(path).create(new_journal("guide:synthetic-1", _binding(tmp_path)))
    assert _code(error) == "unsafe_path"


def test_symlinked_or_widened_journal_refuses(tmp_path: Path) -> None:
    path = _private(tmp_path)
    journal = WorkflowJournal(path)
    journal.create(new_journal("guide:synthetic-1", _binding(tmp_path)))
    path.chmod(0o644)
    with pytest.raises(JournalError) as error:
        journal.read()
    assert _code(error) == "unsafe_path"
    path.chmod(0o600)
    target = path.with_name("elsewhere.json")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(JournalError) as error:
        journal.read()
    assert _code(error) == "unsafe_path"


def test_hardlinked_journal_refuses(tmp_path: Path) -> None:
    path = _private(tmp_path)
    journal = WorkflowJournal(path)
    journal.create(new_journal("guide:synthetic-1", _binding(tmp_path)))
    os.link(path, path.with_name("second.json"))
    with pytest.raises(JournalError) as error:
        journal.read()
    assert _code(error) == "unsafe_path"


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"schema_version":1,"schema_version":1}',
        b"[1]",
        b'{"schema_version":NaN}',
        b"\xff",
        b"[" * 40 + b"]" * 40,
    ],
)
def test_malformed_journal_is_incompatible(tmp_path: Path, raw: bytes) -> None:
    path = _private(tmp_path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, raw)
    os.close(fd)
    with pytest.raises(JournalError) as error:
        WorkflowJournal(path).read()
    assert _code(error) == "journal_incompatible"


def test_oversized_journal_is_incompatible(tmp_path: Path) -> None:
    path = _private(tmp_path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b" " * (MAX_JOURNAL_BYTES + 1))
    os.close(fd)
    with pytest.raises(JournalError) as error:
        WorkflowJournal(path).read()
    assert _code(error) == "journal_incompatible"


def _valid(tmp_path: Path) -> dict[str, object]:
    document = new_journal("guide:synthetic-1", _binding(tmp_path))
    cast(list[object], document["operations"]).append(_operation())
    return document


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(extra=True),
        lambda d: d.update(revision=True),
        lambda d: d.update(status="paused"),
        lambda d: d["binding"].update(issuer_epoch=-1),
        lambda d: d["binding"]["executable"].update(sha256="E" * 64),
        lambda d: d["choices"].pop("scope"),
        lambda d: d["choices"].update(token="x"),
        lambda d: d["operations"][0].update(request_sha256="0" * 64),
        lambda d: d["operations"][0].update(canonical_request_utf8='{"source_id":"other"}'),
        lambda d: d["operations"][0].update(action="configure"),
        lambda d: d["operations"][0].update(state="done"),
        lambda d: d["operations"][0].update(executing_pid=True),
        lambda d: d["operations"][0].update(
            evidence={"source_id": "s", "preview_id": None, "receipt_ids": [], "body": "x"}
        ),
        lambda d: d["operations"][0].update(sequence=3),
        lambda d: d.update(retained_receipt_ids=["a", "a"]),
        lambda d: d.update(workflow_id="bad\u0007id"),
    ],
)
def test_closed_shape_and_digest_correspondence(tmp_path: Path, mutate: object) -> None:
    document = _valid(tmp_path)
    validate_journal(copy.deepcopy(document))
    mutate(document)  # type: ignore[operator]
    with pytest.raises(JournalError) as error:
        validate_journal(document)
    assert _code(error) == "journal_incompatible"


def test_intent_preimage_is_exact_and_preserved(tmp_path: Path) -> None:
    operation = _operation()
    intent = json.loads(cast(str, operation["canonical_intent_utf8"]))
    assert list(intent) == sorted(intent)
    assert intent["action"] == "preview"
    assert intent["request_sha256"] == operation["request_sha256"]
    assert intent["canonical_request_utf8"] == operation["canonical_request_utf8"]
    assert intent["expected_versions"] == [{"kind": "collector", "id": "slack.x", "version": "g1"}]
    assert (
        operation["intent_sha256"]
        == hashlib.sha256(cast(str, operation["canonical_intent_utf8"]).encode()).hexdigest()
    )
    assert canonical_json({"b": 1, "a": [2]}) == '{"a":[2],"b":1}'


def test_cas_write_requires_expected_revision_and_preserves_bytes(tmp_path: Path) -> None:
    path = _private(tmp_path)
    journal = WorkflowJournal(path)
    document = new_journal("guide:synthetic-1", _binding(tmp_path))
    journal.create(document)
    updated = _valid(tmp_path)
    written = journal.write(updated, expected_revision=1)
    assert written["revision"] == 2
    stored = journal.read()
    assert stored is not None
    assert cast(list[dict[str, object]], stored["operations"])[0] == _operation()
    with pytest.raises(JournalError) as error:
        journal.write(updated, expected_revision=1)
    assert _code(error) == "revision_conflict"
    assert oct(os.lstat(path).st_mode & 0o777) == "0o600"


def test_cas_lock_busy_refuses_as_journal_busy(tmp_path: Path) -> None:
    path = _private(tmp_path)
    journal = WorkflowJournal(path)
    journal.create(new_journal("guide:synthetic-1", _binding(tmp_path)))
    holder = os.open(journal.lock_path, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(JournalError) as error:
            journal.write(_valid(tmp_path), expected_revision=1)
        assert _code(error) == "journal_busy"
    finally:
        os.close(holder)


def test_capacity_is_refused_before_write(tmp_path: Path) -> None:
    path = _private(tmp_path)
    journal = WorkflowJournal(path)
    journal.create(new_journal("guide:synthetic-1", _binding(tmp_path)))
    document = _valid(tmp_path)
    operations = cast(list[object], document["operations"])
    operations.extend(_operation(index) for index in range(1, 400))
    with pytest.raises(JournalError) as error:
        journal.write(document, expected_revision=1, reserve_bytes=0)
    assert _code(error) == "capacity_reached"
    stored = journal.read()
    assert stored is not None and stored["revision"] == 1


def test_guard_is_bound_exclusive_and_never_replaced(tmp_path: Path) -> None:
    path = _private(tmp_path)
    journal = WorkflowJournal(path)
    journal.create(new_journal("guide:synthetic-1", _binding(tmp_path)))
    fd = journal.acquire_guard("guide:synthetic-1")
    try:
        with pytest.raises(JournalError) as error:
            journal.acquire_guard("guide:synthetic-1")
        assert _code(error) == "journal_busy"
    finally:
        os.close(fd)
    with pytest.raises(JournalError) as error:
        journal.acquire_guard("guide:other")
    assert _code(error) == "journal_busy"
    os.close(journal.acquire_guard("guide:synthetic-1"))


def test_forged_missing_or_unsafe_guard_is_unavailable(tmp_path: Path) -> None:
    path = _private(tmp_path)
    journal = WorkflowJournal(path)
    journal.create(new_journal("guide:synthetic-1", _binding(tmp_path)))
    journal.guard_path.chmod(0o644)
    with pytest.raises(JournalError) as error:
        journal.acquire_guard("guide:synthetic-1")
    assert _code(error) == "journal_busy"
    journal.guard_path.chmod(0o600)
    journal.guard_path.write_text('{"workflow_id":"guide:forged"}')
    with pytest.raises(JournalError) as error:
        journal.acquire_guard("guide:synthetic-1")
    assert _code(error) == "journal_busy"
    journal.guard_path.unlink()
    with pytest.raises(JournalError) as error:
        journal.acquire_guard("guide:synthetic-1")
    assert _code(error) == "journal_busy"
    assert not journal.guard_path.exists()


def test_guard_probe_reports_busy_without_taking_it(tmp_path: Path) -> None:
    path = _private(tmp_path)
    journal = WorkflowJournal(path)
    journal.create(new_journal("guide:synthetic-1", _binding(tmp_path)))
    assert journal.guard_free("guide:synthetic-1") is True
    fd = journal.acquire_guard("guide:synthetic-1")
    try:
        assert journal.guard_free("guide:synthetic-1") is False
    finally:
        os.close(fd)


def test_module_never_imports_optional_collector() -> None:
    source = Path(journal_module.__file__).read_text()
    assert "open_brain_collector" not in source and "open_brain_connectors" not in source
