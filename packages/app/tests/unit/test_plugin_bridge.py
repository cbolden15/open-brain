from __future__ import annotations

import json
import uuid
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    CaptureAction,
    ManagedAccessMode,
    ManagedProvider,
    ManagedWorkspaceFailure,
    TextPayload,
)

import open_brain.services.plugin_bridge as plugin_bridge_module
from open_brain.local_data import LocalRootSelection, select_local_root
from open_brain.services.local_bootstrap import open_local_brain
from open_brain.services.local_operations import refresh_graph
from open_brain.services.managed_providers import ManagedGraphProviderResult
from open_brain.services.plugin_bridge import (
    PLUGIN_PROTOCOL,
    PluginBridgeFailure,
    PluginRuntimeState,
    dispatch_plugin_request,
    serve_plugin_stdio,
)


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _selection(tmp_path: Path) -> LocalRootSelection:
    return select_local_root(
        data_dir=str(tmp_path / "brain"),
        environment={"HOME": str(tmp_path)},
        platform_name="darwin",
    )


def _call(
    selection: LocalRootSelection,
    operation: str,
    arguments: dict[str, object] | None = None,
    *,
    request_id: str | None = None,
) -> dict[str, object]:
    request = {
        "arguments": arguments or {},
        "operation": operation,
        "protocol": PLUGIN_PROTOCOL,
        "request_id": request_id or f"plugin_{uuid.uuid4()}",
    }
    output = BytesIO()
    assert (
        serve_plugin_stdio(
            selection,
            input_stream=BytesIO(json.dumps(request).encode("utf-8")),
            output_stream=output,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    return cast(dict[str, object], json.loads(output.getvalue()))


def test_handshake_is_bounded_and_does_not_initialize_the_brain(tmp_path: Path) -> None:
    selection = _selection(tmp_path)

    response = _call(selection, "system.handshake")

    assert response["ok"] is True
    result = cast(dict[str, object], response["result"])
    assert result["protocol"] == PLUGIN_PROTOCOL
    assert result["desktop_only"] is True
    assert "graph.review" in cast(list[str], result["operations"])
    assert not selection.brain_root.exists()


def test_bridge_rejects_unknown_operations_without_echoing_input(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    request_id = f"plugin_{uuid.uuid4()}"
    request = {
        "arguments": {"text": "private body"},
        "operation": "unknown.operation",
        "protocol": PLUGIN_PROTOCOL,
        "request_id": request_id,
    }
    output = BytesIO()

    assert (
        serve_plugin_stdio(
            selection,
            input_stream=BytesIO(json.dumps(request).encode("utf-8")),
            output_stream=output,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )

    rendered = output.getvalue().decode("utf-8")
    assert "unknown_operation" in rendered
    assert "private body" not in rendered
    assert not selection.brain_root.exists()


def test_stdio_session_serves_multiple_framed_requests_in_one_engine_lifecycle(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    requests = [
        {
            "arguments": {},
            "operation": "brain.initialize",
            "protocol": PLUGIN_PROTOCOL,
            "request_id": f"plugin_{uuid.uuid4()}",
        },
        {
            "arguments": {},
            "operation": "workspace.status",
            "protocol": PLUGIN_PROTOCOL,
            "request_id": f"plugin_{uuid.uuid4()}",
        },
    ]
    input_stream = BytesIO(
        b"".join(json.dumps(request).encode("utf-8") + b"\n" for request in requests)
    )
    output = BytesIO()

    assert (
        serve_plugin_stdio(
            selection,
            input_stream=input_stream,
            output_stream=output,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [response["request_id"] for response in responses] == [
        request["request_id"] for request in requests
    ]
    assert all(response["ok"] is True for response in responses)
    assert cast(dict[str, object], responses[1]["result"])["status"] == "unconfigured"


def test_plugin_capture_search_workspace_and_reconciliation_flow(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    captured = _call(selection, "capture.create", {"text": "Searchable owner thought"})
    assert cast(dict[str, object], captured["result"])["status"] == "captured"
    searched = _call(selection, "search.query", {"limit": 10, "query": "Searchable"})
    assert len(cast(list[object], cast(dict[str, object], searched["result"])["results"])) == 1

    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        space_id = session.tasks.inbox.create_space(
            "Notes", delivery_id="plugin.test.space"
        ).space_id
        session.tasks.capture.accept(
            TextPayload("Editable managed note"),
            delivery_id="plugin.test.canonical",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=space_id,
        )

    setup = cast(dict[str, object], _call(selection, "workspace.setup")["result"])
    workspace = Path(cast(str, setup["vault_path"]))
    note = next(workspace.rglob("*.md"))
    note.write_text(note.read_text(encoding="utf-8") + "\nOwner edit.\n", encoding="utf-8")

    reconciled = cast(dict[str, object], _call(selection, "workspace.reconcile")["result"])

    assert reconciled["status"] == "reconciled"
    assert len(cast(list[str], reconciled["accepted_note_ids"])) == 1
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        status = session.tasks.managed_workspace.status()
        assert status is not None
        source = session.tasks.managed_workspace.graph_snapshot(status.workspace_id).sources[0]
        assert "Owner edit." in source.body
        exclusion_arguments = {
            "excluded": True,
            "kind": "note",
            "relative_path": source.relative_path,
        }
        exclusion_id = f"plugin_{uuid.uuid4()}"
        excluded = dispatch_plugin_request(
            session,
            "policy.set_exclusion",
            exclusion_arguments,
            request_id=exclusion_id,
            base_executable=None,
        )
        replayed = dispatch_plugin_request(
            session,
            "policy.set_exclusion",
            exclusion_arguments,
            request_id=exclusion_id,
            base_executable=None,
        )
        listed = dispatch_plugin_request(
            session,
            "policy.exclusions",
            {},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
        )
        assert excluded["duplicate"] is False
        assert replayed["duplicate"] is True
        assert listed["exclusions"] == [
            {"kind": "note", "relative_path": source.relative_path}
        ]
        included = dispatch_plugin_request(
            session,
            "policy.set_exclusion",
            {**exclusion_arguments, "excluded": False},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
        )
        assert included["excluded"] is False


def test_checked_suggestion_acceptance_materializes_only_the_previewed_link(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    _call(selection, "brain.initialize")
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        space_id = session.tasks.inbox.create_space(
            "Graph", delivery_id="plugin.graph.space"
        ).space_id
        for index, body in enumerate(
            ("Solar generation peaks at midday.", "Battery storage works after sunset.")
        ):
            session.tasks.capture.accept(
                TextPayload(body),
                delivery_id=f"plugin.graph.capture.{index}",
                action=CaptureAction.CANONICAL_NOTE,
                space_id=space_id,
            )
    _call(selection, "workspace.setup")
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        status = session.tasks.managed_workspace.status()
        assert status is not None
        session.tasks.managed_policy.grant_consent(
            status.workspace_id,
            ManagedProvider.OPENAI_API,
            ManagedAccessMode.API_KEY,
            operation_id="plugin.graph.consent",
        )

        def fake_provider(
            prompt: str, max_output_bytes: int, timeout_seconds: int
        ) -> ManagedGraphProviderResult:
            sources = cast(list[dict[str, object]], json.loads(prompt)["selected_sources"])
            source = next(
                index
                for index, value in enumerate(sources, start=1)
                if "Solar generation" in cast(str, value["body"])
            )
            target = next(
                index
                for index, value in enumerate(sources, start=1)
                if "Battery storage" in cast(str, value["body"])
            )
            assert max_output_bytes == 16 * 1024
            assert timeout_seconds == 60
            return ManagedGraphProviderResult(
                source=source,
                source_quote="peaks at midday",
                target=target,
                target_quote="after sunset",
                actual_model="deterministic-fake-v1",
                usage={"total_tokens": 4},
            )

        result, _, _ = refresh_graph(
            session.tasks,
            provider=ManagedProvider.OPENAI_API,
            access_mode=ManagedAccessMode.API_KEY,
            adapter_identity="openai_api:deterministic-fake-v1",
            request_id="request_00000000-0000-4000-8000-000000000401",
            invoke=fake_provider,
            remaining_attempts=1,
            remaining_input_bytes=16 * 1024,
        )
        suggestion_id = cast(str, cast(dict[str, object], result["suggestion"])["suggestion_id"])
        review = dispatch_plugin_request(
            session,
            "graph.review",
            {"suggestion_id": suggestion_id},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
        )
        assert review["revision_status"] == "current"
        assert review["append_text"] == (
            "[[" + cast(str, cast(dict[str, object], review["target"])["note_id"]) + "]]"
        )
        operation_id = f"plugin_{uuid.uuid4()}"
        first_result = dispatch_plugin_request(
            session,
            "graph.accept",
            {"suggestion_id": suggestion_id},
            request_id=operation_id,
            base_executable=None,
        )
        replay_result = dispatch_plugin_request(
            session,
            "graph.accept",
            {"suggestion_id": suggestion_id},
            request_id=operation_id,
            base_executable=None,
        )

        assert first_result["status"] == "accepted"
        assert first_result["accepted_duplicate"] is False
        assert replay_result["accepted_duplicate"] is True
        assert replay_result["materialized_duplicate"] is True
        source_path = Path(tmp_path / "Open Brain Vault" / cast(str, first_result["relative_path"]))
        note_id = cast(str, cast(dict[str, object], review["source"])["note_id"])
        rendered = source_path.read_text(encoding="utf-8")
        assert rendered.count(review["append_text"]) == 1

        owner_edit = rendered + "\nOwner changed this while Open Brain also changed it.\n"
        source_path.write_text(owner_edit, encoding="utf-8")
        with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
            session.tasks.managed_workspace.materialize(
                status.workspace_id,
                note_id,
                operation_id="plugin.conflict.materialize",
            )
        conflicts = dispatch_plugin_request(
            session,
            "workspace.conflicts",
            {},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
        )
        summary = cast(list[dict[str, object]], conflicts["conflicts"])[0]
        conflict_review = dispatch_plugin_request(
            session,
            "workspace.conflict_review",
            {"note_id": note_id},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
        )
        assert conflict_review["accepted_body"] == rendered
        assert conflict_review["workspace_body"] == owner_edit
        assert conflict_review["conflict_id"] == summary["conflict_id"]

        resolution_id = f"plugin_{uuid.uuid4()}"
        resolution_arguments = {
            "choice": "accepted",
            "conflict_id": summary["conflict_id"],
            "note_id": note_id,
        }
        resolved = dispatch_plugin_request(
            session,
            "workspace.resolve",
            resolution_arguments,
            request_id=resolution_id,
            base_executable=None,
        )
        replayed = dispatch_plugin_request(
            session,
            "workspace.resolve",
            resolution_arguments,
            request_id=resolution_id,
            base_executable=None,
        )
        assert resolved["duplicate"] is False
        assert replayed["duplicate"] is True
        assert replayed["materialized_duplicate"] is True
        assert source_path.read_text(encoding="utf-8") == rendered


def test_direct_provider_setup_refresh_and_removal_keep_session_key_out_of_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection(tmp_path)
    _call(selection, "brain.initialize")
    secret = "synthetic-provider-secret"

    class FakeAdapter:
        identity = "openai_api:synthetic-plugin-v1"

        def bind(self, resolve: object) -> object:
            assert callable(resolve)

            def invoke(
                prompt: str, max_output_bytes: int, timeout_seconds: int
            ) -> ManagedGraphProviderResult:
                assert resolve() == secret
                assert "Solar generation" in prompt
                assert max_output_bytes == 16 * 1024
                assert timeout_seconds == 60
                sources = cast(list[dict[str, object]], json.loads(prompt)["selected_sources"])
                source = next(
                    index
                    for index, item in enumerate(sources, start=1)
                    if "Solar generation" in cast(str, item["body"])
                )
                target = next(
                    index
                    for index, item in enumerate(sources, start=1)
                    if "Battery storage" in cast(str, item["body"])
                )
                return ManagedGraphProviderResult(
                    source=source,
                    source_quote="Solar generation",
                    target=target,
                    target_quote="Battery storage",
                    actual_model="gpt-6-astra",
                    usage={"total_tokens": 9},
                )

            return invoke

    monkeypatch.setattr(
        plugin_bridge_module,
        "DirectApiAdapter",
        lambda _provider, model: FakeAdapter(),
    )
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        space_id = session.tasks.inbox.create_space(
            "Providers", delivery_id="plugin.provider.space"
        ).space_id
        for index, body in enumerate(
            (
                "Solar generation supports daytime demand.",
                "Battery storage supports nighttime demand.",
            )
        ):
            session.tasks.capture.accept(
                TextPayload(body),
                delivery_id=f"plugin.provider.capture.{index}",
                action=CaptureAction.CANONICAL_NOTE,
                space_id=space_id,
            )
        runtime = PluginRuntimeState(None)
        dispatch_plugin_request(
            session,
            "workspace.setup",
            {},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
            runtime=runtime,
        )
        status = dispatch_plugin_request(
            session,
            "provider.status",
            {},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
            runtime=runtime,
        )
        providers = cast(list[dict[str, object]], status["providers"])
        assert status["os_store"] == "unavailable"
        assert providers[-1] == {
            "access_mode": "subscription",
            "credential_saved": None,
            "provider": "claude_subscription",
            "reason": "subscription_isolation_unproven",
            "status": "blocked",
        }

        configured = dispatch_plugin_request(
            session,
            "provider.configure",
            {
                "credential": secret,
                "custody": "session",
                "provider": "openai_api",
                "scope_ack": True,
            },
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
            runtime=runtime,
        )
        refreshed = dispatch_plugin_request(
            session,
            "graph.refresh_semantic",
            {},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
            runtime=runtime,
        )

        assert configured["status"] == "configured"
        assert refreshed["status"] == "refreshed"
        assert refreshed["actual_model"] == "gpt-6-astra"
        assert secret not in json.dumps((configured, refreshed, status))

        removed = dispatch_plugin_request(
            session,
            "provider.remove",
            {"custody": "session", "provider": "openai_api"},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
            runtime=runtime,
        )
        assert removed["status"] == "removed"
        with pytest.raises(PluginBridgeFailure, match="setup_required"):
            dispatch_plugin_request(
                session,
                "graph.refresh_semantic",
                {},
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=runtime,
            )


def test_subscription_provider_remains_closed_at_the_plugin_boundary(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    _call(selection, "brain.initialize")
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        runtime = PluginRuntimeState(None)
        with pytest.raises(PluginBridgeFailure, match="subscription_unavailable"):
            dispatch_plugin_request(
                session,
                "provider.configure",
                {
                    "credential": None,
                    "custody": "session",
                    "provider": "claude_subscription",
                    "scope_ack": True,
                },
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=runtime,
            )
