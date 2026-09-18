from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveSourceError,
    local_source_privacy,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

SELECTION = SourceResourceSelection("gmail", "account:synthetic", "label:INBOX", "mail_label")


def _intake(
    text: str = "Originalneedle", *, message: str = "message1", revision: str = "r1"
) -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey("gmail", "account:synthetic", "label:INBOX", message, revision),
        "https://mail.google.com/mail/u/0/#all/" + message,
        text,
        local_source_privacy(),
        title="Synthetic message",
    )


class Runtime:
    def __init__(self) -> None:
        self.batch = LiveBatch((_intake(),), {"cursor": "next"})
        self.calls = 0
        self.checkpoints: list[dict[str, object] | None] = []

    def acknowledge(
        self, selection: SourceResourceSelection, checkpoint: dict[str, object]
    ) -> None:
        pass

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        assert selection == SELECTION
        self.calls += 1
        self.checkpoints.append(checkpoint)
        return self.batch


def _service(tmp_path: Path, runtime: Runtime) -> LiveCaptureService:
    compile_single_user_local(tmp_path / "brain")
    return LiveCaptureService(tmp_path / "state", tmp_path / "brain", runtime=runtime)


def test_preview_then_unordered_revision_refusal_preserves_source_and_checkpoint(
    tmp_path: Path,
) -> None:
    runtime = Runtime()
    service = _service(tmp_path, runtime)
    service.configure("gmail", SELECTION, {"date_floor": "2026-09-01T00:00:00Z"})
    first = service.preview("gmail")
    assert first["count"] == 1
    assert "Originalneedle" not in json.dumps(first)
    profile = compile_single_user_local(tmp_path / "brain")
    assert not open_local_engine(profile).retrieval.search("Originalneedle")
    service.apply("gmail", cast(str, first["preview_id"]))
    hits = open_local_engine(profile).retrieval.search("Originalneedle")
    assert len(hits) == 1 and hits[0].trust == "third_party"
    runtime.batch = LiveBatch(
        (_intake("Revisedneedle <script>alert(1)</script>", revision="r2"),), {"cursor": "new"}
    )
    second = service.preview("gmail")
    result = service.apply("gmail", cast(str, second["preview_id"]))
    assert result["quarantined_count"] == 1
    assert runtime.checkpoints == [None, {"cursor": "next"}]
    assert len(open_local_engine(profile).retrieval.search("Originalneedle")) == 1
    assert not open_local_engine(profile).retrieval.search("Revisedneedle")
    custody = service.custody_status("gmail")
    assert cast(dict[str, int], custody["counts"])["quarantined"] == 1
    receipt_id = cast(list[str], custody["receipt_ids"])[0]
    assert service.custody_inspect(receipt_id)["reason_code"] == "source_revision_conflict"
    restarted = LiveCaptureService(tmp_path / "state", tmp_path / "brain", runtime=runtime)
    assert restarted.custody_inspect(receipt_id)["outcome"] == "quarantined"


def test_crash_reuses_staged_batch_without_provider_refetch_and_commits_after_ack(
    tmp_path: Path,
) -> None:
    runtime = Runtime()
    runtime.batch = LiveBatch((_intake(), _intake(message="message2")), {"cursor": "after-both"})
    service = _service(tmp_path, runtime)
    service.configure("gmail", SELECTION, {})
    preview = service.preview("gmail")
    captured: dict[str, str] = {}
    crash = True

    def sink(intake: SourceRecordIntake) -> None:
        if intake.key.external_id == "message2" and crash:
            raise RuntimeError("synthetic crash")
        captured[intake.key.delivery_id()] = intake.key.revision_identity()

    service._sink = sink
    with pytest.raises(RuntimeError, match="synthetic crash"):
        service.apply("gmail", cast(str, preview["preview_id"]))
    assert len(captured) == 1
    crash = False
    restarted = LiveCaptureService(
        tmp_path / "state", tmp_path / "brain", runtime=runtime, sink=sink
    )
    again = restarted.preview("gmail")
    assert again["preview_id"] == preview["preview_id"] and runtime.calls == 1
    result = restarted.apply("gmail", cast(str, again["preview_id"]))
    assert result["captured_count"] == 1 and result["duplicate_count"] == 1
    assert len(captured) == 2


def test_pause_while_fetching_cannot_be_overwritten_or_capture_after_ack(tmp_path: Path) -> None:
    class Pausing(Runtime):
        service: LiveCaptureService

        def fetch(
            self,
            selection: SourceResourceSelection,
            options: dict[str, object],
            checkpoint: dict[str, object] | None,
        ) -> LiveBatch:
            self.service.control("gmail", "pause")
            return super().fetch(selection, options, checkpoint)

    runtime = Pausing()
    runtime.service = service = _service(tmp_path, runtime)
    service.configure("gmail", SELECTION, {})
    service.control("gmail", "enable")
    result = service.sync_due()
    assert result[0]["failure_code"] == "source_changed_during_fetch"
    assert service.status("gmail")["status"] == "paused"
    assert service.status("gmail")["pause_ack_epoch"] is not None
    assert not open_local_engine(compile_single_user_local(tmp_path / "brain")).retrieval.search(
        "Originalneedle"
    )
    assert service.sync_due() == []


def test_selection_reset_and_other_brain_reject_previews(tmp_path: Path) -> None:
    runtime = Runtime()
    service = _service(tmp_path, runtime)
    service.configure("gmail", SELECTION, {"floor": 1})
    first = service.preview("gmail")
    with pytest.raises(LiveSourceError, match="source_selection_reset_required"):
        service.configure("gmail", SELECTION, {"floor": 2})
    service.configure("gmail", SELECTION, {"floor": 2}, reset=True)
    with pytest.raises(LiveSourceError, match="source_stale_preview"):
        service.apply("gmail", cast(str, first["preview_id"]))
    compile_single_user_local(tmp_path / "other-brain")
    with pytest.raises(LiveSourceError, match="source_brain_mismatch"):
        LiveCaptureService(tmp_path / "state", tmp_path / "other-brain", runtime=runtime)


def test_manual_import_does_not_enable_schedule_and_disable_cancels_existing_import(
    tmp_path: Path,
) -> None:
    runtime = Runtime()
    service = _service(tmp_path, runtime)
    service.configure("gmail", SELECTION, {})
    preview = service.preview("gmail")
    assert service.sync_due() == []
    service.apply("gmail", cast(str, preview["preview_id"]))
    assert service.status("gmail")["status"] == "disabled"
    assert service.status("gmail")["next_run_epoch"] is None
    service.control("gmail", "enable")
    assert service.sync_due()[0]["duplicate_count"] == 1
    service.control("gmail", "disable")
    assert service.sync_due() == []


def test_rate_limit_preserves_checkpoint_and_respects_retry_time(tmp_path: Path) -> None:
    class Limited(Runtime):
        def fetch(
            self,
            selection: SourceResourceSelection,
            options: dict[str, object],
            checkpoint: dict[str, object] | None,
        ) -> LiveBatch:
            self.calls += 1
            raise LiveSourceError("rate_limited", retry_after_seconds=120)

    runtime = Limited()
    service = _service(tmp_path, runtime)
    service._clock = lambda: 100
    service.configure("gmail", SELECTION, {})
    service.control("gmail", "enable")
    assert service.sync_due()[0]["failure_code"] == "rate_limited"
    assert service.status("gmail")["next_run_epoch"] == 220
    assert service.sync_due() == [] and runtime.calls == 1


def test_acknowledgement_follows_commit_and_lost_response_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    class Acknowledging(Runtime):
        service: LiveCaptureService
        fail_cleanup = True

        def acknowledge(
            self, selection: SourceResourceSelection, checkpoint: dict[str, object]
        ) -> None:
            state = self.service._load("gmail")
            assert state["checkpoint"] == checkpoint and state["pending"] is None
            assert len(cast(dict[str, str], state["committed"])) == 1
            if self.fail_cleanup:
                raise LiveSourceError("source_store_busy")

    runtime = Acknowledging()
    runtime.service = service = _service(tmp_path, runtime)
    service.configure("gmail", SELECTION, {})
    preview = service.preview("gmail")
    result = service.apply("gmail", cast(str, preview["preview_id"]))
    assert result["outcome"] == "completed" and result["captured_count"] == 1
    assert "source_queue_cleanup_pending" in cast(list[str], result["notices"])
    runtime.fail_cleanup = False
    assert service.apply("gmail", cast(str, preview["preview_id"])) == result
    assert runtime.calls == 1


def test_disabling_invalidates_and_removes_staged_preview(tmp_path: Path) -> None:
    service = _service(tmp_path, Runtime())
    service.configure("gmail", SELECTION, {})
    preview = service.preview("gmail")
    preview_id = cast(str, preview["preview_id"])
    staged = tmp_path / "state" / (preview_id.replace(":", "-") + ".json")
    assert staged.exists()
    service.control("gmail", "disable")
    assert not staged.exists()
    with pytest.raises(LiveSourceError, match="source_stale_preview"):
        service.apply("gmail", preview_id)


def test_session_capture_acknowledges_more_than_checkpoint_window(tmp_path: Path) -> None:
    from open_brain_collector.live_manager import PriorityRuntime
    from open_brain_connectors.runtime.agent_session_hooks import (
        enqueue_hook_event,
        queued_events,
    )
    from open_brain_connectors.runtime.agent_session_live import AgentSessionLiveSource

    compile_single_user_local(tmp_path / "brain")
    project = tmp_path / "project"
    project.mkdir()
    runtime = PriorityRuntime(tmp_path / "live")
    source = AgentSessionLiveSource(runtime.root / "sessions")
    selection = source.selection(client="claude_code", project_path=project)
    captured: list[SourceRecordIntake] = []
    service = LiveCaptureService(
        tmp_path / "state", tmp_path / "brain", runtime=runtime, sink=captured.append
    )
    service.configure(
        "sessions",
        selection,
        {
            "client": "claude_code",
            "project_path": str(project),
            "capture_summary": True,
            "capture_transcript": False,
        },
    )
    transcript = tmp_path / "transcript.jsonl"
    for index in range(270):
        session_id = f"synthetic-{index}"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": session_id,
                    "cwd": str(project),
                    "message": {"role": "user", "content": "Synthetic queue marker"},
                }
            )
            + "\n"
        )
        assert enqueue_hook_event(
            runtime.root / "sessions",
            client="claude_code",
            project_path=project,
            payload={
                "cwd": str(project),
                "hook_event_name": "Stop",
                "session_id": session_id,
                "transcript_path": str(transcript),
            },
        )
        preview = service.preview("sessions")
        assert preview["count"] == 1
        service.apply("sessions", cast(str, preview["preview_id"]))
        assert not queued_events(runtime.root / "sessions")
    assert len(captured) == 270 and service.preview("sessions")["count"] == 0


def test_duplicate_cache_is_bounded_and_engine_deduplicates_after_eviction(tmp_path: Path) -> None:
    service = _service(tmp_path, Runtime())
    service.configure("gmail", SELECTION, {})
    first = service.preview("gmail")
    service.apply("gmail", cast(str, first["preview_id"]))
    entry = service._load("gmail")
    # Historical cache contents can be evicted without changing engine identity.
    entry["committed"] = {
        _intake(message=f"historical-{i}").key.delivery_id(): "a" * 64 for i in range(3000)
    }
    service._save(entry)
    second = service.preview("gmail")
    result = service.apply("gmail", cast(str, second["preview_id"]))
    assert result["captured_count"] == 0 and result["duplicate_count"] == 1
    assert len(cast(dict[str, str], service._load("gmail")["committed"])) <= 2048
    hits = open_local_engine(compile_single_user_local(tmp_path / "brain")).retrieval.search(
        "Originalneedle"
    )
    assert len(hits) == 1
    # An unchanged provider page can have the same deterministic preview ID in
    # the next scheduled cycle. It still needs to commit/clear its new staging.
    third = service.preview("gmail")
    assert third["preview_id"] == second["preview_id"]
    service.apply("gmail", cast(str, third["preview_id"]))
    assert service._load("gmail")["pending"] is None
