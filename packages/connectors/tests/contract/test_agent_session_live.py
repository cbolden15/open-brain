from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from open_brain_connectors.runtime.agent_session_hooks import enqueue_hook_event, queued_events
from open_brain_connectors.runtime.agent_session_live import AgentSessionLiveSource
from open_brain_connectors.runtime.live_common import LiveBatch, LiveSourceError


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


def test_long_history_keeps_recent_window_and_distinguishes_identical_later_turn(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "long.jsonl"
    records = [json.loads(line) for line in _claude_transcript(project, "session-123").splitlines()]
    # Tool-heavy history exceeds the former 512 KiB limit; tool bodies are excluded.
    records.extend({"type": "tool", "data": "x" * 4096} for _ in range(150))
    repeated = [
        json.loads(line) for line in _claude_transcript(project, "session-123").splitlines()
    ]
    records.extend(repeated * 40)
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    root = tmp_path / "queue"
    _enqueue(root, project, transcript)
    source = AgentSessionLiveSource(root)
    selection = source.selection(client="claude_code", project_path=project)
    first = source.fetch(selection, _options("claude_code", project), None)
    records.extend(repeated)
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    _enqueue(root, project, transcript)
    second = source.fetch(selection, _options("claude_code", project), first.checkpoint)
    assert first.intakes[1].text == second.intakes[1].text
    assert first.intakes[0].key.delivery_id() != second.intakes[0].key.delivery_id()


def test_secret_in_older_history_is_not_lost_by_recent_window(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "long-secret.jsonl"
    text = _claude_transcript(project, "session-123", secret=True)
    text += _claude_transcript(project, "session-123") * 40
    transcript.write_text(text)
    root = tmp_path / "queue"
    _enqueue(root, project, transcript)
    source = AgentSessionLiveSource(root)
    batch = source.fetch(source.selection(client="claude_code", project_path=project),
                         _options("claude_code", project), None)
    assert not batch.intakes
    assert "session_quarantined_secret" in batch.notices


def _fetch_claude(tmp_path: Path, project: Path, transcript: Path) -> LiveBatch:
    root = tmp_path / "queue"
    _enqueue(root, project, transcript)
    source = AgentSessionLiveSource(root)
    return source.fetch(
        source.selection(client="claude_code", project_path=project),
        _options("claude_code", project),
        None,
    )


def _user_record(project: Path, text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "sessionId": "session-123",
            "cwd": str(project),
            "message": {"role": "user", "content": text},
        }
    )


def test_transcript_beyond_former_eight_mebibyte_limit_is_captured(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "large.jsonl"
    tool_record = json.dumps({"type": "tool", "data": "x" * 8192}) + "\n"
    tool_noise = tool_record * 1100
    transcript.write_text(tool_noise + _claude_transcript(project, "session-123"))
    assert transcript.stat().st_size > 8 * 1024 * 1024
    batch = _fetch_claude(tmp_path, project, transcript)
    assert batch.notices == ()
    assert any("The queue has one event." in intake.text for intake in batch.intakes)


def test_oversized_message_is_skipped_without_refusing_session(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "oversized.jsonl"
    oversized = _user_record(project, "pasted log " + "y" * 70_000)
    transcript.write_text(oversized + "\n" + _claude_transcript(project, "session-123"))
    batch = _fetch_claude(tmp_path, project, transcript)
    assert batch.notices == ()
    assert batch.intakes
    for intake in batch.intakes:
        assert "pasted log" not in intake.text
        assert "The queue has one event." in intake.text


def test_secret_in_oversized_message_still_quarantines(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "oversized-secret.jsonl"
    oversized = _user_record(project, "token=synthetic-private-value " + "y" * 70_000)
    transcript.write_text(oversized + "\n" + _claude_transcript(project, "session-123"))
    batch = _fetch_claude(tmp_path, project, transcript)
    assert not batch.intakes
    assert batch.notices == ("session_quarantined_secret",)


def test_transcript_beyond_hard_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_connectors.runtime import agent_session_live

    monkeypatch.setattr(agent_session_live, "_MAX_TRANSCRIPT_BYTES", 1024)
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "huge.jsonl"
    transcript.write_text(_claude_transcript(project, "session-123") * 20)
    batch = _fetch_claude(tmp_path, project, transcript)
    assert not batch.intakes
    assert batch.notices == ("session_transcript_too_large",)


def test_identifiers_and_personal_details_do_not_quarantine_a_session(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "identifiers.jsonl"
    text = _claude_transcript(project, "session-123").replace(
        "Please inspect the queue.",
        "Commit 9f50bd0c5e1a2b3c4d5e6f708192a3b4c5d6e7f8 wrote "
        "capture_65fdf64a-f32b-4c38-98ca-248de7fd2219; ask owner@example.com today",
    )
    transcript.write_text(text, encoding="utf-8")
    root = tmp_path / "queue"
    _enqueue(root, project, transcript)
    source = AgentSessionLiveSource(root)
    batch = source.fetch(
        source.selection(client="claude_code", project_path=project),
        _options("claude_code", project),
        None,
    )
    assert batch.notices == ()
    assert batch.intakes
    for intake in batch.intakes:
        assert "Commit [REDACTED_CREDENTIAL] wrote" in intake.text
        assert "65fdf64a" not in intake.text
        assert "owner@example.com" not in intake.text


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

    assert len(batch.intakes) == 1
    assert batch.intakes[0].key.external_id.startswith("transcript:")
    assert "token=" not in batch.intakes[0].text
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


@pytest.mark.parametrize("user_secret", [False, True])
def test_native_codex_content_kinds_exclude_injected_context_and_keep_secret_checks(
    tmp_path: Path, user_secret: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "native-codex.jsonl"
    user_text = "token=synthetic-private-value" if user_secret else "Please inspect the queue."
    records = [
        {"type": "session_meta", "payload": {"id": "native-session", "cwd": str(project)}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": [
                        "plugins.recommendations",
                        "agents_md.instructions",
                        "environments.environment_context",
                        "unknown",
                        "user.text",
                    ]
                },
                "content": [
                    {"type": "input_text", "text": "plugin_id=" + "a" * 36},
                    {"type": "input_text", "text": "Do not capture injected instructions."},
                    {"type": "input_text", "text": "token=synthetic-environment-value"},
                    {"type": "input_text", "text": "Do not capture unknown context."},
                    {"type": "input_text", "text": user_text},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["unknown"]},
                "content": [{"type": "output_text", "text": "The queue is ready."}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    root = tmp_path / "queue"
    _enqueue(root, project, transcript, client="codex", session_id="native-session")
    source = AgentSessionLiveSource(root)
    batch = source.fetch(
        source.selection(client="codex", project_path=project), _options("codex", project), None
    )
    if user_secret:
        assert batch.intakes == ()
        assert batch.notices == ("session_quarantined_secret",)
    else:
        assert len(batch.intakes) == 2
        assert batch.notices == ()
        for intake in batch.intakes:
            assert user_text in intake.text and "The queue is ready." in intake.text
            assert "plugin_id" not in intake.text
            assert "Do not capture" not in intake.text
            assert "synthetic-environment-value" not in intake.text


@pytest.mark.parametrize("kinds", [None, [], ["user.text", "user.text"], [7]])
def test_codex_misaligned_content_kinds_fail_closed(tmp_path: Path, kinds: object) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "codex.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "session-codex", "cwd": str(project)}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "internal_chat_message_metadata_passthrough": {"content_item_kinds": kinds},
                "content": [{"type": "input_text", "text": "Ambiguous input"}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    root = tmp_path / "queue"
    _enqueue(root, project, transcript, client="codex", session_id="session-codex")
    source = AgentSessionLiveSource(root)
    batch = source.fetch(
        source.selection(client="codex", project_path=project), _options("codex", project), None
    )
    assert batch.intakes == ()
    assert batch.notices == ("session_unsupported_format",)
    assert len(queued_events(root)) == 1


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


@pytest.mark.parametrize("hook_in_worktree", [False, True])
def test_session_that_moves_into_a_project_worktree_is_captured(
    tmp_path: Path, hook_in_worktree: bool
) -> None:
    project = tmp_path / "project"
    worktree = project / ".worktrees" / "feature"
    worktree.mkdir(parents=True)
    transcript = tmp_path / "moved.jsonl"
    transcript.write_text(
        _claude_transcript(project, "session-123") + _claude_transcript(worktree, "session-123")
    )
    target = worktree if hook_in_worktree else project
    batch = _fetch_claude(tmp_path, target, transcript)
    assert batch.notices == ()
    assert batch.intakes


def test_session_that_moves_outside_its_starting_tree_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    transcript = tmp_path / "escaped.jsonl"
    transcript.write_text(
        _claude_transcript(project, "session-123") + _claude_transcript(other, "session-123")
    )
    for target in (project, other):
        batch = _fetch_claude(tmp_path / target.name, target, transcript)
        assert batch.intakes == ()
        assert batch.notices == ("session_identity_mismatch",)


def test_later_record_from_another_session_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "spliced.jsonl"
    transcript.write_text(
        _claude_transcript(project, "session-123") + _claude_transcript(project, "session-456")
    )
    batch = _fetch_claude(tmp_path, project, transcript)
    assert batch.intakes == ()
    assert batch.notices == ("session_identity_mismatch",)


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

    # Allocate while the original exists: unlink/recreate may reuse its inode.
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(_claude_transcript(project, "session-123"), encoding="utf-8")
    assert replacement.stat().st_ino != transcript.stat().st_ino
    replacement.replace(transcript)
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
