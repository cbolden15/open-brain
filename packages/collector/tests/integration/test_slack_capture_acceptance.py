"""Independent synthetic acceptance for Slack privacy and revision admission."""

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import RetrievalResult, open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveSourceError,
    local_source_privacy,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

ACCOUNT = "account:" + "b" * 64
SELECTION = SourceResourceSelection("slack", ACCOUNT, "channel:CSYNTHETIC", "channel")


def intake(text: str = "Originalacceptanceneedle", revision: str = "r1") -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey("slack", ACCOUNT, SELECTION.resource_id, "123.456", revision),
        "https://example.slack.com/archives/CSYNTHETIC/p123456",
        text,
        local_source_privacy(),
        title="Synthetic Slack thread",
    )


class Runtime:
    def __init__(self) -> None:
        self.batch = LiveBatch((intake(),), {"cursor": "1"})
        self.calls = 0

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        assert selection == SELECTION
        assert "privacy_tier" not in options
        self.calls += 1
        return self.batch

    def acknowledge(
        self, selection: SourceResourceSelection, checkpoint: dict[str, object]
    ) -> None:
        assert selection == SELECTION


def service(tmp_path: Path, runtime: Runtime) -> LiveCaptureService:
    compile_single_user_local(tmp_path / "brain")
    return LiveCaptureService(tmp_path / "state", tmp_path / "brain", runtime=runtime)


def apply(current: LiveCaptureService, source_id: str = "slack") -> dict[str, object]:
    preview = current.preview(source_id)
    return current.apply(source_id, cast(str, preview["preview_id"]))


def search(tmp_path: Path, text: str) -> tuple[RetrievalResult, ...]:
    return open_local_engine(compile_single_user_local(tmp_path / "brain")).retrieval.search(text)


def test_work_privacy_supported_and_dual_selection_deduplicates(tmp_path: Path) -> None:
    runtime = Runtime()
    current = service(tmp_path, runtime)
    current.configure("slack", SELECTION, {"privacy_tier": "work"})
    first = apply(current)
    assert first["outcome"] == "completed"
    hits = search(tmp_path, "Originalacceptanceneedle")
    assert len(hits) == 1
    with sqlite3.connect(tmp_path / "brain/.open-brain/state/phase1.sqlite3") as connection:
        row = connection.execute(
            "SELECT privacy_json FROM captures WHERE capture_id = ?", (hits[0].capture_id,)
        ).fetchone()
    assert json.loads(row[0])["tier"] == "work"
    current.configure("alternate", SELECTION, {"privacy_tier": "work"})
    second = apply(current, "alternate")
    assert second["outcome"] == "completed"
    assert len(search(tmp_path, "Originalacceptanceneedle")) == 1


def test_unknown_retains_preview_without_retrieval_and_invalid_refuses_before_fetch(
    tmp_path: Path,
) -> None:
    runtime = Runtime()
    current = service(tmp_path, runtime)
    with pytest.raises(LiveSourceError, match="source_invalid_privacy_policy"):
        current.configure("slack", SELECTION, {"privacy_tier": "invalid"})
    assert runtime.calls == 0
    current.configure("slack", SELECTION, {"privacy_tier": "unknown"})
    preview = current.preview("slack")
    with pytest.raises(LiveSourceError, match="source_privacy_unresolved"):
        current.apply("slack", cast(str, preview["preview_id"]))
    assert not search(tmp_path, "Originalacceptanceneedle")
    assert current.preview("slack")["preview_id"] == preview["preview_id"]
    assert runtime.calls == 1


def test_policy_reset_fences_old_preview(tmp_path: Path) -> None:
    runtime = Runtime()
    current = service(tmp_path, runtime)
    current.configure("slack", SELECTION, {"privacy_tier": "personal"})
    preview = current.preview("slack")
    with pytest.raises(LiveSourceError, match="source_selection_reset_required"):
        current.configure("slack", SELECTION, {"privacy_tier": "work"})
    current.configure("slack", SELECTION, {"privacy_tier": "work"}, reset=True)
    with pytest.raises(LiveSourceError, match="source_stale_preview"):
        current.apply("slack", cast(str, preview["preview_id"]))
    assert not search(tmp_path, "Originalacceptanceneedle")


def test_changed_revision_successor_restart_retry_and_deletion(tmp_path: Path) -> None:
    runtime = Runtime()
    current = service(tmp_path, runtime)
    current.configure("slack", SELECTION, {})
    apply(current)
    runtime.batch = LiveBatch((intake("Revisedacceptanceneedle", "r2"),), {"cursor": "2"})
    preview = current.preview("slack")
    result = current.apply("slack", cast(str, preview["preview_id"]))
    assert result["outcome"] == "completed"
    assert not search(tmp_path, "Originalacceptanceneedle")
    assert len(search(tmp_path, "Revisedacceptanceneedle")) == 1
    restarted = LiveCaptureService(tmp_path / "state", tmp_path / "brain", runtime=runtime)
    assert restarted.apply("slack", cast(str, preview["preview_id"])) == result
    runtime.batch = LiveBatch(
        (intake("[Slack message deleted]", "r3"),), {"cursor": "3"}, notices=("message_deleted",)
    )
    assert apply(restarted)["outcome"] == "completed"
    assert not search(tmp_path, "Revisedacceptanceneedle")


def test_historical_overlap_cannot_promote_old_slack_revision(tmp_path: Path) -> None:
    runtime = Runtime()
    current = service(tmp_path, runtime)
    current.configure("slack", SELECTION, {"privacy_tier": "work"})
    original = intake("Originalacceptanceneedle", "edited:1.000001")
    runtime.batch = LiveBatch((original,), {"cursor": "1"})
    apply(current)
    runtime.batch = LiveBatch(
        (intake("Revisedacceptanceneedle", "edited:2.000001"),), {"cursor": "2"}
    )
    apply(current)
    runtime.batch = LiveBatch((original,), {"cursor": "3"})
    assert apply(current)["duplicate_count"] == 1
    assert not search(tmp_path, "Originalacceptanceneedle")
    assert len(search(tmp_path, "Revisedacceptanceneedle")) == 1
    runtime.batch = LiveBatch(
        (intake("Unseenstaleacceptance", "edited:0.000001"),), {"cursor": "4"}
    )
    assert apply(current)["quarantined_count"] == 1
    assert not search(tmp_path, "Unseenstaleacceptance")
    assert len(search(tmp_path, "Revisedacceptanceneedle")) == 1
