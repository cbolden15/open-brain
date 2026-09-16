from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import (
    PrivacyDecision,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    open_local_engine,
)
from open_brain_engine.storage.locks import FileLease, LockBusyError

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import MAX_MESSAGE_BYTES, LocalMcpAdapter
from open_brain.services.local_operations import search_brain
from open_brain.services.mcp_protocol import serve_stdio_mcp
from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorRunPage,
    CollectorSourceRuntime,
    CollectorStateStore,
    EngineCaptureSink,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


def test_collector_coexists_with_desktop_cli_mcp_and_obsidian_bridge_surface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brain_root = tmp_path / "brain"
    state_path = tmp_path / "collector" / "state.json"
    selection = _selection()
    source = _Source(selection)
    controller = CollectorController(CollectorStateStore(state_path), clock=_Clock(100))

    assert run_cli(("init", "--data-dir", str(brain_root), "--json")) == 0
    capsys.readouterr()
    collector_capture = EngineCaptureSink(_capture_sink(brain_root))
    controller.enable(
        source_id="github.fixture.coexist",
        selection=selection,
        interval_seconds=60,
    )

    desktop_sync = FileLease(brain_root / ".open-brain", owner_identity_id="desktop")
    with desktop_sync.acquire_shared_writer(), pytest.raises(LockBusyError):
        controller.sync_due(
            source_id="github.fixture.coexist",
            runtime=source,
            capture_sink=collector_capture,
        )

    blocked_state = json.loads(state_path.read_text(encoding="utf-8"))
    blocked_source = blocked_state["sources"]["github.fixture.coexist"]
    assert blocked_source["committed_revisions"] == {}
    assert blocked_source["last_run"]["failure_code"] == "capture_failed"
    assert run_cli(("search", "coexistence-token", "--data-dir", str(brain_root), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["results"] == []

    completed = controller.sync_due(
        source_id="github.fixture.coexist",
        runtime=source,
        capture_sink=collector_capture,
    )
    assert completed.outcome == "completed"
    assert completed.captured_count == 1

    assert run_cli(("search", "coexistence-token", "--data-dir", str(brain_root), "--json")) == 0
    cli_results = json.loads(capsys.readouterr().out)["results"]
    assert cli_results[0]["source_origin"] == "third_party"

    tasks = open_local_engine(compile_single_user_local(brain_root))
    adapter = LocalMcpAdapter(
        search=lambda query, limit: search_brain(
            tasks.retrieval,
            tasks.reconciliation,
            query,
            limit=limit,
        )
    )
    responses = _wire(
        adapter,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "brain_search",
                "arguments": {"query": "coexistence-token", "limit": 5},
            },
        },
    )
    mcp_result = cast(dict[str, Any], responses[1]["result"]["structuredContent"])
    assert mcp_result["results"][0]["source_origin"] == "third_party"
    assert "coexistence-token" in json.dumps(mcp_result)


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Source(CollectorSourceRuntime):
    def __init__(self, selection: SourceResourceSelection) -> None:
        self.selection = selection

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        assert selection == self.selection
        assert cursor is None
        return CollectorRunPage(selection=selection, intakes=(self._intake(),), next_cursor=None)

    def _intake(self) -> SourceRecordIntake:
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name="github",
                connection_id=self.selection.connection_id,
                resource_id=self.selection.resource_id,
                external_id="issue:coexist",
                revision_id="updated:2026-09-15T01:36:00Z",
            ),
            url="https://github.com/cbolden15/open-brain-fixture/issues/70",
            title="D3 coexistence fixture",
            text=(
                "Synthetic collector coexistence-token body shared through CLI, MCP, "
                "desktop, and Obsidian bridge surfaces."
            ),
            privacy=_privacy(),
        )


def _wire(adapter: LocalMcpAdapter, *messages: dict[str, object]) -> list[Any]:
    incoming = b"".join(json.dumps(item).encode() + b"\n" for item in messages)
    outgoing = io.BytesIO()
    serve_stdio_mcp(
        adapter,
        input_stream=io.BytesIO(incoming),
        output_stream=outgoing,
        maximum_message_bytes=MAX_MESSAGE_BYTES,
    )
    return [json.loads(line) for line in outgoing.getvalue().splitlines()]


def _selection() -> SourceResourceSelection:
    return SourceResourceSelection(
        connector_name="github",
        connection_id="account:cbolden15",
        resource_id="repo:cbolden15/open-brain-fixture",
        resource_type="repository",
    )


def _capture_sink(brain_root: Path) -> PublicJobCaptureSink:
    tasks = open_local_engine(compile_single_user_local(brain_root))
    actor = "actor_22222222-2222-4222-8222-222222222222"
    context = PublicJobCaptureContext.create(
        profile=tasks.profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_22222222-2222-4222-8222-222222222222",
            "role_id": "role_22222222-2222-4222-8222-222222222222",
            "tenant_id": tasks.profile.tenant_id,
        },
    )
    return tasks.capture.public_job_sink(context)


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": True},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "policy_public",
            "tier": "public",
        }
    )
