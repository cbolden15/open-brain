from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from open_brain_connectors.runtime.agent_session_hooks import enqueue_hook_event, queued_events
from open_brain_connectors.runtime.agent_session_live import AgentSessionLiveSource
from open_brain_connectors.runtime.live_common import LiveSourceError


def _claude_transcript(
    project: Path,
    session_id: str,
    *,
    secret: bool = False,
    partial: bool = False,
) -> str:
    user_text = (
        "token=" + "abcdefghijklmnopqrstuvwxyz0123456789" if secret else "Please inspect the queue."
    )
    lines = [
        json.dumps(
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": str(project),
                "message": {"role": "user", "content": user_text},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "cwd": str(project),
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "The queue has one event."},
                        {"type": "tool_use", "name": "private_tool", "input": {"secret": "ignore"}},
                    ],
                },
            }
        ),
    ]
    if partial:
        lines.append('{"type":"assistant"')
        return "\n".join(lines)
    return "\n".join(lines) + "\n"


def _enqueue(
    root: Path,
    project: Path,
    transcript: Path,
    *,
    client: str = "claude_code",
    session_id: str = "session-123",
) -> None:
    assert enqueue_hook_event(
        root,
        client=client,  # type: ignore[arg-type]
        project_path=project,
        payload={
            "cwd": str(project),
            "hook_event_name": "Stop",
            "session_id": session_id,
            "transcript_path": str(transcript),
        },
    )


def _options(
    client: str,
    project: Path,
    *,
    summary: bool = True,
    transcript: bool = True,
) -> dict[str, object]:
    return {
        "capture_summary": summary,
        "capture_transcript": transcript,
        "client": client,
        "project_path": str(project),
    }


def test_claude_session_uses_extract_only_and_preserves_adapter_identity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        _claude_transcript(project, "session-123", partial=True),
        encoding="utf-8",
    )
    root = tmp_path / "queue"
    _enqueue(root, project, transcript)
    source = AgentSessionLiveSource(root)

    batch = source.fetch(
        source.selection(client="claude_code", project_path=project),
        _options("claude_code", project),
        None,
    )

    assert len(batch.intakes) == 2
    assert batch.intakes[0].key.connector_name == "agent_session"
    assert batch.intakes[0].key.connection_id == "account:local:claude_code"
    assert batch.intakes[0].text.startswith("Extractive session summary:")
    assert "private_tool" not in batch.intakes[1].text
    assert "session-123" not in batch.intakes[1].text
    captured_ids = batch.checkpoint["captured_event_ids"]
    assert isinstance(captured_ids, list)
    assert len(captured_ids) == 1


def test_secret_event_is_quarantined_without_blocking_later_clean_session(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    bad = tmp_path / "bad.jsonl"
    clean = tmp_path / "clean.jsonl"
    bad.write_text(_claude_transcript(project, "session-bad", secret=True), encoding="utf-8")
    clean.write_text(_claude_transcript(project, "session-clean"), encoding="utf-8")
    root = tmp_path / "queue"
    _enqueue(root, project, bad, session_id="session-bad")
    _enqueue(root, project, clean, session_id="session-clean")
    source = AgentSessionLiveSource(root)

    batch = source.fetch(
        source.selection(client="claude_code", project_path=project),
        _options("claude_code", project, summary=False, transcript=True),
        None,
    )

    assert [item.key.external_id for item in batch.intakes] == ["transcript:session-clean"]
    assert batch.notices == ("session_quarantined_secret",)
    assert batch.checkpoint["captured_event_ids"]


def test_codex_format_has_a_distinct_client_identity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "codex.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "session-codex", "cwd": str(project)},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Show status"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "Status is ready"},
                                {"type": "reasoning", "text": "must not capture"},
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "queue"
    _enqueue(root, project, transcript, client="codex", session_id="session-codex")
    source = AgentSessionLiveSource(root)

    batch = source.fetch(
        source.selection(client="codex", project_path=project),
        _options("codex", project, summary=False, transcript=True),
        None,
    )

    assert batch.intakes[0].key.connection_id == "account:local:codex"
    assert "reasoning" not in batch.intakes[0].text
    assert "must not capture" not in batch.intakes[0].text


def test_explicitly_disabled_choices_do_not_read_or_capture_queue(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_transcript(project, "session-123", secret=True), encoding="utf-8")
    root = tmp_path / "queue"
    _enqueue(root, project, transcript)
    source = AgentSessionLiveSource(root)

    batch = source.fetch(
        source.selection(client="claude_code", project_path=project),
        _options("claude_code", project, summary=False, transcript=False),
        None,
    )

    assert batch.intakes == ()
    assert batch.notices == ()


def test_newest_stop_suppresses_older_event_and_acknowledges_superseded_events(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    old = tmp_path / "old.jsonl"
    newest = tmp_path / "newest.jsonl"
    old.write_text(_claude_transcript(project, "same-session"), encoding="utf-8")
    newest.write_text(_claude_transcript(project, "same-session"), encoding="utf-8")
    stat = newest.stat()
    os.utime(newest, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    root = tmp_path / "queue"
    _enqueue(root, project, old, session_id="same-session")
    _enqueue(root, project, newest, session_id="same-session")
    source = AgentSessionLiveSource(root)
    selection = source.selection(client="claude_code", project_path=project)

    first = source.fetch(selection, _options("claude_code", project), None)
    assert len(first.intakes) == 2
    second = source.fetch(selection, _options("claude_code", project), first.checkpoint)
    assert second.intakes == ()
    source.acknowledge(selection, first.checkpoint)
    source.acknowledge(selection, first.checkpoint)
    assert queued_events(root) == ()
    assert source.fetch(selection, _options("claude_code", project), first.checkpoint).intakes == ()


def test_rejects_transcript_identity_mismatch_and_inode_swap(tmp_path: Path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_transcript(other, "session-123"), encoding="utf-8")
    root = tmp_path / "queue"
    _enqueue(root, project, transcript)
    source = AgentSessionLiveSource(root)
    selection = source.selection(client="claude_code", project_path=project)

    mismatched = source.fetch(selection, _options("claude_code", project), None)
    assert mismatched.intakes == ()
    assert mismatched.notices == ("session_identity_mismatch",)

    transcript.unlink()
    transcript.write_text(_claude_transcript(project, "session-123"), encoding="utf-8")
    swapped = source.fetch(selection, _options("claude_code", project), None)
    assert swapped.intakes == ()
    assert swapped.notices == ("session_transcript_changed",)


def test_acknowledge_keeps_queue_draining_beyond_256_events(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_transcript(project, "cycle"), encoding="utf-8")
    root = tmp_path / "queue"
    source = AgentSessionLiveSource(root)
    selection = source.selection(client="claude_code", project_path=project)
    checkpoint: dict[str, object] | None = None

    for index in range(300):
        transcript.write_text(
            _claude_transcript(project, f"cycle-{index}"),
            encoding="utf-8",
        )
        _enqueue(root, project, transcript, session_id=f"cycle-{index}")
        batch = source.fetch(selection, _options("claude_code", project), checkpoint)
        assert len(batch.intakes) == 2
        source.acknowledge(selection, batch.checkpoint)
        checkpoint = batch.checkpoint

    assert source.fetch(selection, _options("claude_code", project), checkpoint).intakes == ()


def test_acknowledge_cannot_delete_another_selected_project(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    selected.mkdir()
    other.mkdir()
    selected_transcript = tmp_path / "selected.jsonl"
    other_transcript = tmp_path / "other.jsonl"
    selected_transcript.write_text(
        _claude_transcript(selected, "selected-session"),
        encoding="utf-8",
    )
    other_transcript.write_text(
        _claude_transcript(other, "other-session"),
        encoding="utf-8",
    )
    root = tmp_path / "queue"
    _enqueue(root, selected, selected_transcript, session_id="selected-session")
    _enqueue(root, other, other_transcript, session_id="other-session")
    source = AgentSessionLiveSource(root)
    selected_selection = source.selection(client="claude_code", project_path=selected)
    other_selection = source.selection(client="claude_code", project_path=other)
    other_batch = source.fetch(other_selection, _options("claude_code", other), None)

    with pytest.raises(LiveSourceError, match="source_selection_mismatch"):
        source.acknowledge(selected_selection, other_batch.checkpoint)

    assert source.fetch(other_selection, _options("claude_code", other), None).intakes
