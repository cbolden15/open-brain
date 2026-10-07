from __future__ import annotations

import json
from pathlib import Path

import pytest
from open_brain_engine.engine import open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_collector.live_manager import PriorityRuntime
from open_brain_connectors.runtime.agent_session_hooks import enqueue_hook_event, queued_events
from open_brain_connectors.runtime.agent_session_live import AgentSessionLiveSource


@pytest.mark.parametrize("client", ["claude_code", "codex"])
def test_offline_restart_replay_and_later_turn_are_searchable(tmp_path: Path, client: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    brain = tmp_path / "brain"
    profile = compile_single_user_local(brain)
    runtime = PriorityRuntime(tmp_path / "live")
    queue = runtime.root / "sessions"
    transcript = tmp_path / "session.jsonl"
    records = [] if client == "claude_code" else [
        {"type": "session_meta", "payload": {"id": "fixture", "cwd": str(project)}}
    ]

    def append_turn(marker: str) -> None:
        for role, text in (("user", "Remember a fixture decision."), ("assistant", marker)):
            if client == "claude_code":
                records.append({"type": role, "sessionId": "fixture", "cwd": str(project),
                    "message": {"role": role, "content": text}})
            else:
                kind = "input_text" if role == "user" else "output_text"
                records.append({"type": "response_item", "payload": {"type": "message",
                    "role": role, "content": [{"type": kind, "text": text}]}})
        transcript.write_text("".join(json.dumps(record) + "\n" for record in records))

    def stop() -> None:
        assert enqueue_hook_event(queue, client=client, project_path=project, payload={
            "cwd": str(project), "hook_event_name": "Stop", "session_id": "fixture",
            "transcript_path": str(transcript)})

    append_turn("Firstturnneedle choose blue.")
    stop()
    assert len(queued_events(queue)) == 1
    source = AgentSessionLiveSource(queue)
    service = LiveCaptureService(tmp_path / "state", brain, runtime=runtime)
    service.configure("sessions", source.selection(client=client, project_path=project), {
        "client": client, "project_path": str(project), "capture_summary": True,
        "capture_transcript": False})
    service.control("sessions", "enable")
    assert service.sync_due()[0]["captured_count"] == 1
    assert not queued_events(queue)
    stop()
    service = LiveCaptureService(tmp_path / "state", brain, runtime=PriorityRuntime(tmp_path / "live"))
    replay = service.apply("sessions", service.preview("sessions")["preview_id"])
    assert replay["duplicate_count"] == 1
    assert replay["quarantined_count"] == 0
    append_turn("Secondturnneedle choose green.")
    stop()
    second = service.apply("sessions", service.preview("sessions")["preview_id"])
    assert second["captured_count"] == 1
    assert second["quarantined_count"] == 0
    assert len(open_local_engine(profile).retrieval.search("Secondturnneedle")) == 1
    assert not queued_events(queue)
