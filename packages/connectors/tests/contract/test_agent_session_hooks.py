from __future__ import annotations

import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from open_brain_connectors.runtime.agent_session_hooks import (
    AgentSessionHookManager,
    HookConfigError,
    discard_queued_event,
    enqueue_hook_event,
    queue_overflow_count,
    queued_events,
)


def _hook_payload(
    project: Path, transcript: Path, *, session_id: str = "session-123"
) -> dict[str, object]:
    return {
        "cwd": str(project),
        "hook_event_name": "Stop",
        "session_id": session_id,
        "transcript_path": str(transcript),
    }


def test_preview_apply_and_uninstall_preserve_unrelated_claude_hooks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Write", "hooks": [{"type": "command", "command": "keep"}]}
                    ]
                },
                "permissions": {"allow": ["Read"]},
            }
        ),
        encoding="utf-8",
    )
    manager = AgentSessionHookManager(tmp_path / "queue")

    preview = manager.preview_apply("claude_code", project)
    assert preview.changed is True
    manager.apply(preview)
    applied = json.loads(settings.read_text(encoding="utf-8"))
    assert applied["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "keep"
    command = applied["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "--owner open-brain-agent-session" in command
    handler = applied["hooks"]["Stop"][0]["hooks"][0]
    assert "async" not in handler
    assert handler["timeout"] == 2

    removal = manager.preview_uninstall("claude_code", project)
    manager.uninstall(removal)
    removed = json.loads(settings.read_text(encoding="utf-8"))
    assert "Stop" not in removed["hooks"]
    assert removed["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "keep"


def test_preimage_prevents_overwriting_changed_codex_config(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = AgentSessionHookManager(tmp_path / "queue")
    preview = manager.preview_apply("codex", project)
    config = project / ".codex" / "hooks.json"
    config.parent.mkdir()
    config.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")

    with pytest.raises(HookConfigError, match="changed since preview"):
        manager.apply(preview)


@pytest.mark.parametrize("client", ["claude_code", "codex"])
def test_generated_stop_command_persists_event_before_foreground_exit(
    tmp_path: Path, client: str
) -> None:
    project = tmp_path / "selected project"
    project.mkdir()
    transcript = tmp_path / "fresh session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    queue = tmp_path / "queue"
    manager = AgentSessionHookManager(queue)
    preview = manager.preview_apply(client, project)  # type: ignore[arg-type]
    manager.apply(preview)
    configured = json.loads(preview.config_path.read_text(encoding="utf-8"))
    handler = configured["hooks"]["Stop"][0]["hooks"][0]
    # Foreground clients cancel unfinished async hooks as the session exits.
    assert "async" not in handler
    assert handler["timeout"] == 2
    completed = subprocess.run(
        shlex.split(handler["command"]),
        input=json.dumps(_hook_payload(project, transcript)),
        text=True,
        capture_output=True,
        cwd=project,
        timeout=handler["timeout"],
        check=True,
    )
    assert completed.stdout == completed.stderr == ""
    events = queued_events(queue)
    assert len(events) == 1
    assert events[0].client == client
    assert events[0].project_path == str(project.resolve())
    assert events[0].transcript_path == str(transcript.resolve())


def test_reapply_replaces_async_codex_hook_and_preserves_unrelated_stop(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = AgentSessionHookManager(tmp_path / "queue")
    preview = manager.preview_apply("codex", project)
    manager.apply(preview)
    config = json.loads(preview.config_path.read_text(encoding="utf-8"))
    old_handler = config["hooks"]["Stop"][0]["hooks"][0]
    old_handler.update({"async": True, "timeout": 3})
    unrelated = {"hooks": [{"type": "command", "command": "keep-unrelated-hook"}]}
    config["hooks"]["Stop"].append(unrelated)
    preview.config_path.write_text(json.dumps(config), encoding="utf-8")

    upgrade = manager.preview_apply("codex", project)
    assert upgrade.changed
    manager.apply(upgrade)
    upgraded = json.loads(upgrade.config_path.read_text(encoding="utf-8"))
    entries = upgraded["hooks"]["Stop"]
    assert entries[0] == unrelated
    assert len(entries) == 2
    assert "async" not in entries[1]["hooks"][0]
    assert entries[1]["hooks"][0]["timeout"] == 2


def test_enqueue_records_metadata_only_and_discard_is_explicit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"private":"text is not persisted here"}\n', encoding="utf-8")
    queue_root = tmp_path / "queue"

    assert enqueue_hook_event(
        queue_root,
        client="claude_code",
        project_path=project,
        payload=_hook_payload(project, transcript),
    )
    event = queued_events(queue_root)[0]
    stored = (queue_root / "agent-session-queue" / f"event-{event.event_id}.json").read_text(
        encoding="utf-8"
    )
    assert "text is not persisted here" not in stored
    assert event.transcript_path == str(transcript.resolve())
    assert discard_queued_event(queue_root, event.event_id) is True
    assert queued_events(queue_root) == ()


def test_enqueue_rejects_event_bound_to_a_different_project(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    selected.mkdir()
    other.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")

    assert not enqueue_hook_event(
        tmp_path / "queue",
        client="codex",
        project_path=selected,
        payload=_hook_payload(other, transcript),
    )


def test_queue_saturation_records_a_metadata_only_notice(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    queue_root = tmp_path / "queue"
    for index in range(256):
        assert enqueue_hook_event(
            queue_root,
            client="codex",
            project_path=project,
            payload=_hook_payload(project, transcript, session_id=f"session-{index}"),
        )

    assert not enqueue_hook_event(
        queue_root,
        client="codex",
        project_path=project,
        payload=_hook_payload(project, transcript, session_id="session-overflow"),
    )
    assert queue_overflow_count(queue_root) == 1


def test_concurrent_enqueues_keep_every_metadata_event(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    queue_root = tmp_path / "queue"

    def enqueue(index: int) -> bool:
        return enqueue_hook_event(
            queue_root,
            client="codex",
            project_path=project,
            payload=_hook_payload(project, transcript, session_id=f"session-{index}"),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(enqueue, range(32)))
    assert all(results)
    assert len(queued_events(queue_root)) == 32
