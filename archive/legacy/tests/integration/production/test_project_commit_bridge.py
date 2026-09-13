from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from open_brain_legacy.production.project_commit_bridge import (
    build_project_commit_envelope,
    consume_project_commit_spool,
    queue_project_commit,
    relay_project_commit_spool,
)


def _record(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "author": "Tester",
        "body": "",
        "branch": "main",
        "deletions": 0,
        "files": 1,
        "insertions": 2,
        "kind": "commit",
        "project_path": "/workspace/projects/example",
        "project_relpath": "example",
        "repo": "example",
        "sha": "a" * 40,
        "subject": "Test commit",
        "ts": "2026-08-26T20:00:00Z",
        "worktree_path": "/workspace/projects/example",
    }
    value.update(overrides)
    return value


@dataclass
class _Transport:
    received: list[bytes] = field(default_factory=list)
    fail: bool = False

    def submit(self, path: Path) -> None:
        if self.fail:
            raise OSError("synthetic transport failure")
        self.received.append(path.read_bytes())


def test_queue_is_identity_keyed_and_detects_payload_conflict(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"

    assert queue_project_commit(outbox, _record(), source_id="macbook")
    assert not queue_project_commit(outbox, _record(), source_id="macbook")

    record = next(outbox.glob("*.json"))
    value = json.loads(record.read_text(encoding="utf-8"))
    value["record"]["subject"] = "Changed metadata"
    record.write_text(json.dumps(value), encoding="utf-8")

    try:
        queue_project_commit(outbox, _record(), source_id="macbook")
    except ValueError as error:
        assert str(error) == "project commit queue conflict"
    else:
        raise AssertionError("queue conflict was not rejected")


def test_relay_deletes_only_after_durable_transport_success(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    queue_project_commit(outbox, _record(), source_id="macbook")
    transport = _Transport(fail=True)

    failed = relay_project_commit_spool(outbox, transport=transport)
    assert failed.failed == 1
    assert len(list(outbox.glob("*.json"))) == 1

    transport.fail = False
    delivered = relay_project_commit_spool(outbox, transport=transport)
    assert delivered.processed == 1
    assert delivered.failed == 0
    assert len(transport.received) == 1
    assert list(outbox.glob("*.json")) == []


def test_consumer_bootstraps_existing_records_then_appends_once(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    inbox = tmp_path / "work-brain/inbox/project-commits.jsonl"
    state = tmp_path / "state"
    inbox.parent.mkdir(parents=True)
    existing = _record()
    inbox.write_text(json.dumps(existing) + "\n", encoding="utf-8")
    queue_project_commit(spool, existing, source_id="macbook")
    queue_project_commit(
        spool,
        _record(sha="b" * 40, subject="New commit"),
        source_id="macbook",
    )

    result = consume_project_commit_spool(
        spool,
        inbox_path=inbox,
        state_root=state,
    )

    assert result.processed == 1
    assert result.duplicates == 1
    assert result.failed == 0
    assert len(inbox.read_text(encoding="utf-8").splitlines()) == 2
    assert list(spool.glob("*.json")) == []


def test_consumer_replay_survives_a_drained_inbox(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    inbox = tmp_path / "work-brain/inbox/project-commits.jsonl"
    state = tmp_path / "state"
    record = _record()
    queue_project_commit(spool, record, source_id="macbook")

    first = consume_project_commit_spool(spool, inbox_path=inbox, state_root=state)
    inbox.write_text("", encoding="utf-8")
    queue_project_commit(spool, record, source_id="macbook")
    replay = consume_project_commit_spool(spool, inbox_path=inbox, state_root=state)

    assert first.processed == 1
    assert replay.duplicates == 1
    assert inbox.read_text(encoding="utf-8") == ""


def test_consumer_keeps_checksum_mismatch_for_recovery(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    inbox = tmp_path / "work-brain/inbox/project-commits.jsonl"
    state = tmp_path / "state"
    queue_project_commit(spool, _record(), source_id="macbook")
    path = next(spool.glob("*.json"))
    value = json.loads(path.read_text(encoding="utf-8"))
    value["record_sha256"] = "f" * 64
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    result = consume_project_commit_spool(spool, inbox_path=inbox, state_root=state)

    assert result.processed == 0
    assert result.failed == 1
    assert path.exists()
    assert not inbox.exists()


def test_envelope_identity_includes_worktree_path() -> None:
    main = build_project_commit_envelope(_record(), source_id="macbook")
    worktree = build_project_commit_envelope(
        _record(worktree_path="/workspace/projects/example-worktree"),
        source_id="macbook",
    )

    assert main.identity_sha256 != worktree.identity_sha256
