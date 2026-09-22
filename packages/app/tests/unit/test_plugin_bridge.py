from __future__ import annotations

import json
import sqlite3
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    CaptureAction,
    DecisionOutcome,
    ManagedAccessMode,
    ManagedProvider,
    ManagedWorkspaceFailure,
    ProposalDraft,
    PublicJobCaptureSink,
    TextPayload,
    canonical_json_bytes,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

import open_brain.services.plugin_bridge as plugin_bridge_module
from open_brain.local_data import LocalRootSelection, select_local_root
from open_brain.profile import open_existing_single_user_local
from open_brain.services.local_bootstrap import open_local_brain
from open_brain.services.local_operations import mcp_capture_sink, refresh_graph
from open_brain.services.local_runtime_session import (
    LocalRuntimeCompatibilityError,
    hold_local_runtime_session,
)
from open_brain.services.managed_providers import ManagedGraphProviderResult
from open_brain.services.plugin_bridge import (
    OPEN_BRAIN_CLIENT_PROTOCOL,
    OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
    PluginBridgeFailure,
)
from open_brain.services.plugin_bridge import PluginRuntimeState as _PluginRuntimeState
from open_brain.services.plugin_bridge import (
    dispatch_plugin_request as _dispatch_plugin_request,
)
from open_brain.services.plugin_bridge import serve_plugin_stdio as _serve_plugin_stdio


def _owner_authority() -> EffectiveAuthority:
    return EffectiveAuthority(
        "plugin-test-owner",
        "bridge-" + str(uuid.uuid4()),
        frozenset(),
        None,
        owner=True,
    )


class PluginRuntimeState(_PluginRuntimeState):
    """Test runtime with explicit owner authority unless a scoped one is supplied."""

    def __init__(
        self,
        credential_store: object,
        authority: EffectiveAuthority | None = None,
        *,
        capture: PublicJobCaptureSink | None = None,
    ) -> None:
        super().__init__(
            cast(Any, credential_store),
            _owner_authority() if authority is None else authority,
            capture=capture,
        )


def dispatch_plugin_request(
    session: Any,
    operation: str,
    arguments: dict[str, object],
    *,
    request_id: str,
    base_executable: Path | None,
    environment: dict[str, object] | None = None,
    runtime: _PluginRuntimeState | None = None,
) -> dict[str, object]:
    return _dispatch_plugin_request(
        session,
        operation,
        arguments,
        request_id=request_id,
        base_executable=base_executable,
        environment=environment,
        runtime=PluginRuntimeState(None) if runtime is None else runtime,
    )


def serve_plugin_stdio(selection: LocalRootSelection, **arguments: Any) -> int:
    return _serve_plugin_stdio(
        selection,
        authority=_owner_authority(),
        **arguments,
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
    base_executable: Path | None = None,
    environment: dict[str, object] | None = None,
) -> dict[str, object]:
    request = {
        "arguments": arguments or {},
        "operation": operation,
        "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
        "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
        "request_id": request_id or f"plugin_{uuid.uuid4()}",
    }
    output = BytesIO()
    assert (
        serve_plugin_stdio(
            selection,
            input_stream=BytesIO(json.dumps(request).encode("utf-8")),
            output_stream=output,
            filesystem_type_probe=_filesystem,
            base_executable=base_executable,
            environment=environment,
        )
        == 0
    )
    return cast(dict[str, object], json.loads(output.getvalue()))


def _collector_environment(tmp_path: Path) -> dict[str, object]:
    executable = tmp_path / "runtime/open-brain-collector"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return {"HOME": str(tmp_path), "OPEN_BRAIN_COLLECTOR": str(executable)}


def test_handshake_is_bounded_and_does_not_initialize_the_brain(tmp_path: Path) -> None:
    selection = _selection(tmp_path)

    response = _call(selection, "system.handshake")

    assert response["ok"] is True
    result = cast(dict[str, object], response["result"])
    assert response["protocol"] == OPEN_BRAIN_CLIENT_PROTOCOL
    assert response["protocol_version"] == OPEN_BRAIN_CLIENT_PROTOCOL_VERSION
    assert result["protocol"] == OPEN_BRAIN_CLIENT_PROTOCOL
    assert result["protocol_version"] == OPEN_BRAIN_CLIENT_PROTOCOL_VERSION
    assert result["desktop_only"] is True
    assert result["brain_root"] == str(selection.brain_root)
    assert result["runtime_session_version"] == 4
    assert result["state_schema_version"] == 9
    assert "graph.review" in cast(list[str], result["operations"])
    assert "system.status" in cast(list[str], result["operations"])
    assert "agent.setup.preview" in cast(list[str], result["operations"])
    assert "contract.describe" in cast(list[str], result["operations"])
    assert "record.read" in cast(list[str], result["operations"])
    assert not selection.brain_root.exists()


def test_plugin_session_constructors_reject_missing_authority(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    with pytest.raises(ValueError, match="^invalid plugin authority$"):
        _PluginRuntimeState(None, cast(Any, None))
    with pytest.raises(ValueError, match="^invalid plugin authority$"):
        _serve_plugin_stdio(
            selection,
            authority=cast(Any, None),
            input_stream=BytesIO(),
            output_stream=BytesIO(),
            filesystem_type_probe=_filesystem,
        )


def test_scoped_plugin_operation_matrix_denies_before_task_lookup() -> None:
    class NoTaskSession:
        @property
        def tasks(self) -> object:
            raise AssertionError("scoped plugin operation reached task lookup")

    authority = EffectiveAuthority(
        principal_id="scoped-plugin",
        session_id="scoped-plugin-session",
        capabilities=frozenset(),
        space_ids=None,
        allowed_read_tiers=frozenset(),
    )
    for operation in plugin_bridge_module._OPERATIONS:
        runtime = PluginRuntimeState(None, authority=authority)
        with pytest.raises(PluginBridgeFailure, match="^unsupported_capability$"):
            dispatch_plugin_request(
                cast(Any, NoTaskSession()),
                operation,
                {},
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=runtime,
            )


def test_scoped_plugin_stdio_gates_owner_bootstrap_operations_before_handlers(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    authority = EffectiveAuthority(
        principal_id="scoped-plugin",
        session_id="scoped-plugin-stdio",
        capabilities=frozenset({"search"}),
        space_ids=None,
        allowed_read_tiers=frozenset(),
    )

    def call(operation: str, arguments: dict[str, object]) -> dict[str, object]:
        request = {
            "arguments": arguments,
            "operation": operation,
            "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
            "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
            "request_id": f"plugin_{uuid.uuid4()}",
        }
        output = BytesIO()
        assert (
            _serve_plugin_stdio(
                selection,
                authority=authority,
                input_stream=BytesIO(json.dumps(request).encode()),
                output_stream=output,
                filesystem_type_probe=_filesystem,
                environment={"HOME": str(tmp_path)},
            )
            == 0
        )
        return cast(dict[str, object], json.loads(output.getvalue()))

    handshake = call("system.handshake", {})
    assert handshake["ok"] is True
    assert cast(dict[str, object], handshake["result"])["operations"] == [
        "catalog.describe",
        "contract.describe",
        "system.handshake",
        "search.page",
    ]
    assert call("catalog.describe", {"schema_version": 2})["ok"] is True
    for operation in (
        "agent.setup.apply",
        "agent.setup.preview",
        "brain.initialize",
        "system.status",
    ):
        response = call(operation, {})
        assert response["ok"] is False
        assert cast(dict[str, object], response["error"])["code"] == ("unsupported_capability")
    assert not selection.brain_root.exists()


def test_scoped_plugin_capture_uses_only_the_injected_bounded_sink(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as owner_session:
        authority = EffectiveAuthority(
            principal_id="scoped-plugin",
            session_id="scoped-plugin-capture",
            capabilities=frozenset({"capture"}),
            space_ids=None,
            allowed_capture_tiers=frozenset({PrivacyTier.PERSONAL}),
        )
        runtime = PluginRuntimeState(
            None,
            authority=authority,
            capture=mcp_capture_sink(owner_session.tasks),
        )
        assert plugin_bridge_module._available_operations(
            {}, authority, capture_available=True
        ) == ["catalog.describe", "capture.create", "system.handshake"]
        assert plugin_bridge_module._available_operations(
            {}, authority, capture_available=False
        ) == ["catalog.describe", "system.handshake"]

        secret_only = EffectiveAuthority(
            principal_id="scoped-plugin",
            session_id="scoped-plugin-secret-only",
            capabilities=frozenset({"capture"}),
            space_ids=None,
            allowed_capture_tiers=frozenset({PrivacyTier.SECRET}),
        )
        assert plugin_bridge_module._available_operations(
            {}, secret_only, capture_available=True
        ) == ["catalog.describe", "system.handshake"]
        with pytest.raises(PluginBridgeFailure, match="^unsupported_capability$"):
            dispatch_plugin_request(
                cast(Any, object()),
                "capture.create",
                {"text": "wrong fixed tier"},
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=PluginRuntimeState(
                    None,
                    authority=secret_only,
                    capture=mcp_capture_sink(owner_session.tasks),
                ),
            )

        class OwnerTasksMustNotBeUsed:
            @property
            def capture(self) -> object:
                raise AssertionError("scoped capture reached the owner capture task")

        class ScopedSession:
            tasks = OwnerTasksMustNotBeUsed()

        result = dispatch_plugin_request(
            cast(Any, ScopedSession()),
            "capture.create",
            {"text": "scoped plugin capture"},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
            runtime=runtime,
        )

        assert result["status"] == "captured"
        assert runtime.authority is authority
        assert [item.preview for item in owner_session.tasks.inbox.list()] == [
            "scoped plugin capture"
        ]
        with pytest.raises(PluginBridgeFailure, match="^invalid_arguments$"):
            dispatch_plugin_request(
                cast(Any, ScopedSession()),
                "capture.create",
                {"text": "scoped tier override", "privacy_tier": "secret"},
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=runtime,
            )


def test_contract_discovery_exposes_only_implemented_negotiated_tasks(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True

    response = _call(selection, "contract.describe")

    assert response["ok"] is True
    result = cast(dict[str, object], response["result"])
    assert result["contract_version"] == "t03.v1"
    assert result["operations"] == [
        {
            "dto_version": 1,
            "name": "search.page",
            "required_grants": ["search"],
        },
        {
            "dto_version": 1,
            "name": "record.read",
            "required_grants": ["content-read"],
        },
        {
            "dto_version": 1,
            "name": "history.list",
            "required_grants": ["history-read"],
        },
        {
            "dto_version": 1,
            "name": "history.show",
            "required_grants": ["history-read"],
        },
        {
            "dto_version": 1,
            "name": "source.route",
            "required_grants": ["organize"],
        },
    ]


def test_bridge_pages_201_and_reconstructs_frozen_unicode_in_one_real_session(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    recipe = json.loads(
        (
            Path(__file__).resolve().parents[4]
            / "tests/fixtures/new-user-t03/security-boundaries.json"
        ).read_bytes()
    )["long_text_recipe"]
    unicode_body = "".join(part["text"] * part["repeat"] for part in recipe["parts"])
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        expected = [
            session.tasks.capture.accept(
                TextPayload("identical synthetic nebula"),
                delivery_id=f"plugin.retrieval.{index}",
            ).capture_id
            for index in range(201)
        ]
        unicode_payload = TextPayload(unicode_body)
        unicode_capture = session.tasks.capture.accept(
            unicode_payload, delivery_id="plugin.retrieval.unicode"
        )
        runtime = PluginRuntimeState(None)

        request: dict[str, object] = {
            "dto_version": 1,
            "query": "nebula",
            "limit": 100,
        }
        pages: list[dict[str, object]] = []
        while True:
            page = dispatch_plugin_request(
                session,
                "search.page",
                request,
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=runtime,
            )
            pages.append(page)
            if page["complete"]:
                break
            request = {**request, "cursor": page["next_cursor"]}
        found = [
            result["record_id"]
            for page in pages
            for result in cast(list[dict[str, object]], page["results"])
        ]
        assert found == sorted(expected)
        assert len(found) == len(set(found)) == 201
        assert [len(cast(list[object], page["results"])) for page in pages] == [100, 100, 1]

        read: dict[str, object] = {
            "dto_version": 1,
            "record_id": unicode_capture.capture_id,
            "expected_revision_id": unicode_capture.capture_id,
            "target_bytes": 32_768,
        }
        chunks: list[str] = []
        offset = 0
        while True:
            response = dispatch_plugin_request(
                session,
                "record.read",
                read,
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=runtime,
            )
            assert response["start_byte"] == offset
            text = cast(str, cast(dict[str, object], response["content"])["text"])
            offset += len(text.encode("utf-8"))
            assert response["end_byte"] == offset
            chunks.append(text)
            if response["complete"]:
                assert response["next_cursor"] is None
                break
            read = {**read, "cursor": response["next_cursor"]}
        reconstructed = "".join(chunks)
        assert reconstructed == unicode_payload.text
        assert "MIDDLE_SYNTHETIC_CANARY" in reconstructed
        assert chunks[-1].endswith("END_SYNTHETIC\n")

        first_cursor = pages[0]["next_cursor"]
        with pytest.raises(PluginBridgeFailure, match="^cursor_invalid$"):
            dispatch_plugin_request(
                session,
                "search.page",
                {"dto_version": 1, "query": "nebula", "limit": 100, "cursor": first_cursor},
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=PluginRuntimeState(None),
            )
        session.tasks.capture.accept(
            TextPayload("generation changed nebula"),
            delivery_id="plugin.retrieval.changed",
        )
        with pytest.raises(PluginBridgeFailure, match="^cursor_stale$"):
            dispatch_plugin_request(
                session,
                "search.page",
                {"dto_version": 1, "query": "nebula", "limit": 100, "cursor": first_cursor},
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=runtime,
            )


def test_separate_stdio_sessions_reject_cursor_when_first_request_ids_match(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        for index in range(3):
            session.tasks.capture.accept(
                TextPayload("bridge session collision nebula"),
                delivery_id=f"bridge.session.{index}",
            )

    request_id = "plugin_11111111-1111-4111-8111-111111111111"
    arguments: dict[str, object] = {"dto_version": 1, "query": "nebula", "limit": 2}
    first = _call(selection, "search.page", arguments, request_id=request_id)
    first_result = cast(dict[str, object], first["result"])
    cursor = cast(str, first_result["next_cursor"])

    second = _call(
        selection,
        "search.page",
        {**arguments, "cursor": cursor},
        request_id=request_id,
    )
    assert second["ok"] is False
    assert cast(dict[str, object], second["error"])["code"] == "cursor_invalid"


def test_bridge_lists_and_reads_actual_history_with_session_bound_cursor(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        space = session.tasks.spaces.create_space(
            "Bridge History", delivery_id="bridge.history.space"
        )
        source = session.tasks.capture.accept(
            TextPayload("bridge history source"),
            delivery_id="bridge.history.source",
            space_id=space.space_id,
        )
        bodies = [
            "Old bridge é🙂é 漢字\n" * 2000,
            "Middle bridge body",
            "Current bridge body",
        ]
        page_id: str | None = None
        for index, body in enumerate(bodies):
            proposal = session.tasks.review.propose(
                (source.capture_id,),
                (ProposalDraft(f"Bridge history {index}", body),),
                delivery_id=f"bridge.history.proposal.{index}",
                target_page_id=page_id,
            )[0]
            decision = session.tasks.review.decide(
                proposal.proposal_id,
                DecisionOutcome.APPROVED,
                delivery_id=f"bridge.history.decision.{index}",
                expected_review_digest=proposal.review_digest,
            )
            page_id = decision.page_id
        assert page_id is not None

        runtime = PluginRuntimeState(None)
        first = dispatch_plugin_request(
            session,
            "history.list",
            {"dto_version": 1, "record_id": page_id, "limit": 2},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
            runtime=runtime,
        )
        entries = cast(list[dict[str, object]], first["entries"])
        assert [entry["is_current"] for entry in entries] == [True, False]
        cursor = cast(str, first["next_cursor"])
        with pytest.raises(PluginBridgeFailure, match="^cursor_invalid$"):
            dispatch_plugin_request(
                session,
                "history.list",
                {"dto_version": 1, "record_id": page_id, "limit": 2, "cursor": cursor},
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=PluginRuntimeState(None),
            )
        tail = dispatch_plugin_request(
            session,
            "history.list",
            {"dto_version": 1, "record_id": page_id, "limit": 2, "cursor": cursor},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=None,
            runtime=runtime,
        )
        historical = cast(list[dict[str, object]], tail["entries"])[0]
        assert historical["is_current"] is False

        arguments: dict[str, object] = {
            "dto_version": 1,
            "record_id": page_id,
            "expected_revision_id": historical["revision_id"],
            "target_bytes": 32_768,
        }
        chunks: list[str] = []
        while True:
            shown = dispatch_plugin_request(
                session,
                "history.show",
                arguments,
                request_id=f"plugin_{uuid.uuid4()}",
                base_executable=None,
                runtime=runtime,
            )
            assert (
                cast(dict[str, object], shown["record"])["revision_id"] == historical["revision_id"]
            )
            chunks.append(cast(str, cast(dict[str, object], shown["content"])["text"]))
            if shown["complete"]:
                break
            arguments = {**arguments, "cursor": shown["next_cursor"]}
        reconstructed = "".join(chunks)
        assert TextPayload(bodies[0]).text in reconstructed
        assert bodies[-1] not in reconstructed


def test_bridge_dispatches_implemented_source_route(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    capture = cast(
        dict[str, object],
        _call(selection, "capture.create", {"text": "Synthetic routed source"})["result"],
    )
    space = cast(
        dict[str, object],
        _call(selection, "space.create", {"dto_version": 1, "name": "Routed"})["result"],
    )
    database = selection.brain_root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT source_id,head_capture_id,route_version FROM logical_sources "
            "WHERE head_capture_id=?",
            (capture["capture_id"],),
        ).fetchone()
    assert row is not None
    source_id, head, route_version = row
    response = _call(
        selection,
        "source.route",
        {
            "dto_version": 1,
            "source_id": source_id,
            "space_id": cast(dict[str, object], space["space"])["space_id"],
            "expected_head": head,
            "expected_route_version": route_version,
            "operation_id": "operation_123e4567-e89b-42d3-a456-426614174100",
        },
    )
    assert response["ok"] is True
    routed = cast(dict[str, object], response["result"])
    assert routed == {
        "status": "ok",
        "dto_version": 1,
        "source_id": source_id,
        "head": head,
        "route_version": route_version + 1,
    }


def test_bridge_uses_shared_organization_and_publication_services(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/t06-client-api.json").read_bytes()
    )
    examples = {example["operation"]: example for example in fixture["examples"]}

    def assert_fixture_shape(operation: str, result: dict[str, object]) -> None:
        expected = cast(dict[str, object], examples[operation]["response"])
        assert set(result) == set(expected)

    assert _call(selection, "brain.initialize")["ok"] is True
    setup = cast(dict[str, object], _call(selection, "workspace.setup")["result"])
    status = cast(dict[str, object], _call(selection, "workspace.status")["result"])
    assert_fixture_shape("workspace.setup", setup)
    assert_fixture_shape("workspace.status", status)
    assert setup["status"] == "setup"
    assert status["status"] == "ok"
    assert status["vault_path"] == setup["vault_path"]
    invalid_arguments: tuple[dict[str, object], ...] = (
        {"name": "Unversioned"},
        {"dto_version": True, "name": "Boolean"},
    )
    for invalid in invalid_arguments:
        denied = _call(selection, "space.create", invalid)
        assert denied["ok"] is False
        assert cast(dict[str, object], denied["error"])["code"] == "invalid_arguments"
    captures = [
        cast(
            dict[str, object],
            _call(selection, "capture.create", {"text": text})["result"],
        )
        for text in ("Synthetic first source", "Synthetic second source")
    ]
    inbox = cast(
        dict[str, object],
        _call(
            selection,
            "inbox.list",
            {"dto_version": 1, "unassigned_only": True, "limit": 50, "offset": 0},
        )["result"],
    )
    assert_fixture_shape("inbox.list", inbox)
    assert [row["capture_id"] for row in cast(list[dict[str, object]], inbox["items"])] == [
        capture["capture_id"] for capture in captures
    ]
    space = cast(
        dict[str, object],
        _call(selection, "space.create", {"dto_version": 1, "name": "Synthetic space"})["result"],
    )
    assert_fixture_shape("space.create", space)
    space_id = cast(dict[str, object], space["space"])["space_id"]
    spaces = cast(
        dict[str, object],
        _call(selection, "space.list", {"dto_version": 1, "limit": 50, "offset": 0})["result"],
    )
    assert_fixture_shape("space.list", spaces)
    for index, capture in enumerate(captures):
        routed = cast(
            dict[str, object],
            _call(
                selection,
                "inbox.route",
                {
                    "dto_version": 1,
                    "capture_id": capture["capture_id"],
                    "space_id": space_id,
                    "idempotency_key": f"synthetic-route-{index}",
                },
            )["result"],
        )
        assert_fixture_shape("inbox.route", routed)
    proposed = cast(
        dict[str, object],
        _call(
            selection,
            "publication.propose",
            {
                "dto_version": 1,
                "capture_ids": [capture["capture_id"] for capture in captures],
                "title": "Synthetic publication",
                "markdown": "Complete synthetic body",
            },
        )["result"],
    )
    assert_fixture_shape("publication.propose", proposed)
    shown = cast(
        dict[str, object],
        _call(
            selection,
            "publication.show",
            {"dto_version": 1, "proposal_id": proposed["proposal_id"]},
        )["result"],
    )
    assert_fixture_shape("publication.show", shown)
    approved = cast(
        dict[str, object],
        _call(
            selection,
            "publication.approve",
            {
                "dto_version": 1,
                "proposal_id": proposed["proposal_id"],
                "review_token": shown["review_token"],
            },
        )["result"],
    )
    assert_fixture_shape("publication.approve", approved)

    assert shown["markdown"] == "Complete synthetic body"
    assert approved["status"] == "approved"
    assert approved["page_id"] == proposed["page_id"]
    canonical_search = cast(
        dict[str, object],
        _call(
            selection,
            "search.page",
            {
                "dto_version": 1,
                "query": "synthetic publication",
                "filters": {
                    "space_ids": [],
                    "payload_families": [],
                    "record_types": ["canonical"],
                },
            },
        )["result"],
    )
    canonical_results = cast(list[dict[str, object]], canonical_search["results"])
    assert len(canonical_results) == 1
    canonical = canonical_results[0]
    assert canonical["record_id"] == approved["page_id"]
    assert canonical["record_type"] == "canonical"
    assert canonical["source_id"] is None
    canonical_read = cast(
        dict[str, object],
        _call(
            selection,
            "record.read",
            {
                "dto_version": 1,
                "record_id": canonical["record_id"],
                "expected_revision_id": canonical["revision_id"],
            },
        )["result"],
    )
    assert canonical_read["complete"] is True
    assert cast(dict[str, object], canonical_read["content"])["text"] == (
        "Complete synthetic body\n"
    )
    refreshed = cast(dict[str, object], _call(selection, "workspace.refresh")["result"])
    assert_fixture_shape("workspace.refresh", refreshed)
    note = next(
        row
        for row in cast(list[dict[str, object]], refreshed["notes"])
        if row["note_id"] == approved["page_id"]
    )
    note_path = Path(cast(str, refreshed["vault_path"])) / cast(str, note["relative_path"])
    materialized = note_path.read_text(encoding="utf-8")
    assert materialized.endswith("\nComplete synthetic body\n")
    assert f'page_id: "{approved["page_id"]}"' in materialized
    assert len(cast(list[dict[str, object]], shown["evidence"])) == 2
    stale = _call(
        selection,
        "publication.reject",
        {
            "dto_version": 1,
            "proposal_id": proposed["proposal_id"],
            "review_token": shown["review_token"],
        },
    )
    assert stale["ok"] is False
    assert cast(dict[str, object], stale["error"])["code"] == "terminal_decision"


def test_status_is_non_mutating_for_empty_state_and_reports_initialized_state(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)

    empty = cast(dict[str, object], _call(selection, "system.status")["result"])

    assert empty == {
        "brain_root": str(selection.brain_root),
        "initialized": False,
        "runtime_session_version": 4,
        "state_schema_version": 9,
        "status": "ok",
    }
    assert not selection.brain_root.exists()

    assert _call(selection, "brain.initialize")["ok"] is True
    initialized = cast(dict[str, object], _call(selection, "system.status")["result"])
    assert initialized["initialized"] is True
    assert initialized["state_schema_version"] == 9


def test_plugin_bridge_exposes_durable_collector_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPEN_BRAIN_COLLECTOR_TEST_EPOCH", "300")
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    state = selection.brain_root / "collector" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {
                    "github.fixture.closed": {
                        "active_run": None,
                        "committed_revisions": {},
                        "connection_id": "account:fixture",
                        "connector_name": "github",
                        "interval_seconds": 60,
                        "last_run": {
                            "captured_count": 1,
                            "duplicate_count": 0,
                            "failure_code": None,
                            "finished_epoch": 100,
                            "next_cursor": None,
                            "outcome": "completed",
                            "run_id": "github.fixture.closed:100:initial",
                        },
                        "next_cursor": None,
                        "next_run_epoch": 200,
                        "pause_ack_epoch": None,
                        "resource_id": "repo:fixture/open-brain",
                        "resource_type": "repository",
                        "status": "enabled",
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    environment = _collector_environment(tmp_path)
    status = cast(
        dict[str, object],
        _call(selection, "collector.status", environment=environment)["result"],
    )
    sources = cast(list[dict[str, object]], status["sources"])
    assert status["status"] == "ready"
    assert sources[0]["source_id"] == "github.fixture.closed"
    assert sources[0]["status"] == "enabled"
    assert sources[0]["interval_seconds"] == 60
    assert sources[0]["last_success_epoch"] == 100

    paused = cast(
        dict[str, object],
        _call(
            selection,
            "collector.pause",
            {"source_id": "github.fixture.closed"},
            environment=environment,
        )["result"],
    )
    assert paused["status"] == "paused"
    assert paused["pause_ack_epoch"] == 300
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["sources"]["github.fixture.closed"]["status"] == "paused"

    resumed = cast(
        dict[str, object],
        _call(
            selection,
            "collector.resume",
            {"source_id": "github.fixture.closed"},
            environment=environment,
        )["result"],
    )
    assert resumed["status"] == "enabled"
    assert resumed["pause_ack_epoch"] is None
    assert resumed["next_run_epoch"] == 300
    disabled = cast(
        dict[str, object],
        _call(
            selection,
            "collector.disable",
            {"source_id": "github.fixture.closed"},
            environment=environment,
        )["result"],
    )
    assert disabled["status"] == "disabled"
    assert disabled["next_run_epoch"] is None


def test_plugin_bridge_enables_schedules_and_controls_full_collector_source_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPEN_BRAIN_COLLECTOR_TEST_EPOCH", "300")
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    environment = _collector_environment(tmp_path)
    source_id = "github.fixture/repo#issues?label=goal&owner=cbolden15"

    enabled = cast(
        dict[str, object],
        _call(
            selection,
            "collector.enable",
            {
                "connection_id": "account:fixture",
                "connector_name": "github",
                "credential_ref": "github-user-token:fixture",
                "interval_seconds": 60,
                "resource_id": "repo:fixture/open-brain",
                "resource_type": "repository",
                "source_id": source_id,
            },
            environment=environment,
        )["result"],
    )

    assert enabled["source_id"] == source_id
    assert enabled["status"] == "enabled"
    assert enabled["interval_seconds"] == 60
    scheduled = cast(
        dict[str, object],
        _call(
            selection,
            "collector.schedule",
            {"interval_seconds": 120, "source_id": source_id},
            environment=environment,
        )["result"],
    )
    assert scheduled["interval_seconds"] == 120
    paused = cast(
        dict[str, object],
        _call(selection, "collector.pause", {"source_id": source_id}, environment=environment)[
            "result"
        ],
    )
    assert paused["status"] == "paused"
    saved = json.loads((selection.brain_root / "collector" / "state.json").read_text())
    assert saved["sources"][source_id]["interval_seconds"] == 120
    assert saved["sources"][source_id]["credential_ref"] == "github-user-token:fixture"


def test_plugin_bridge_rejects_collector_enable_without_credential_ref(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    response = _call(
        selection,
        "collector.enable",
        {
            "connection_id": "account:fixture",
            "connector_name": "github",
            "interval_seconds": 60,
            "resource_id": "repo:fixture/open-brain",
            "resource_type": "repository",
            "source_id": "github.fixture",
        },
        environment=_collector_environment(tmp_path),
    )

    assert response["ok"] is False
    assert cast(dict[str, object], response["error"])["code"] == "credential_missing"


def test_plugin_bridge_rejects_unsupported_collector_enable(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    response = _call(
        selection,
        "collector.enable",
        {
            "connection_id": "account:fixture",
            "connector_name": "slack",
            "credential_ref": "slack-user-token:fixture",
            "interval_seconds": 60,
            "resource_id": "channel:fixture",
            "resource_type": "channel",
            "source_id": "slack.fixture",
        },
        environment=_collector_environment(tmp_path),
    )

    assert response["ok"] is False
    assert cast(dict[str, object], response["error"])["code"] == "unsupported_collector_source"


def test_plugin_bridge_sync_now_persists_due_request_without_executing_collector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    state = selection.brain_root / "collector" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {
                    "github.fixture.closed": {
                        "active_run": None,
                        "committed_revisions": {},
                        "connection_id": "account:fixture",
                        "connector_name": "github",
                        "interval_seconds": 60,
                        "last_run": None,
                        "next_cursor": None,
                        "next_run_epoch": 200,
                        "pause_ack_epoch": None,
                        "resource_id": "repo:fixture/open-brain",
                        "resource_type": "repository",
                        "status": "enabled",
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    environment = _collector_environment(tmp_path)
    monkeypatch.setenv("OPEN_BRAIN_COLLECTOR", str(environment["OPEN_BRAIN_COLLECTOR"]))
    monkeypatch.setenv("OPEN_BRAIN_COLLECTOR_FIXTURE_RUNTIME_JSON", "/tmp/fixture-runtime.json")

    result = cast(
        dict[str, object],
        _call(
            selection,
            "collector.sync_now",
            {"source_id": "github.fixture.closed"},
            environment=environment,
        )["result"],
    )
    saved = json.loads(state.read_text(encoding="utf-8"))
    source = saved["sources"]["github.fixture.closed"]

    assert source["next_run_epoch"] > 200
    assert result["status"] == "requested"
    assert result["requested"] is True
    assert cast(dict[str, object], result["source"])["next_run_epoch"] == source["next_run_epoch"]


def test_plugin_bridge_sync_now_matches_discovery_without_collector_runtime(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    state = selection.brain_root / "collector" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {
                    "github.fixture.closed": {
                        "active_run": None,
                        "committed_revisions": {},
                        "connection_id": "account:fixture",
                        "connector_name": "github",
                        "interval_seconds": 60,
                        "last_run": None,
                        "next_cursor": None,
                        "next_run_epoch": 200,
                        "pause_ack_epoch": None,
                        "resource_id": "repo:fixture/open-brain",
                        "resource_type": "repository",
                        "status": "enabled",
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    response = _call(selection, "collector.sync_now", {"source_id": "github.fixture.closed"})

    assert response["ok"] is False
    assert cast(dict[str, object], response["error"])["code"] == "unsupported_capability"


def test_plugin_agent_setup_uses_known_runtime_and_never_initializes_brain(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime/open-brain"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o700)
    arguments: dict[str, object] = {
        "action": "configure",
        "allow_capture": True,
        "allow_search": False,
        "client": "claude-code",
        "project_dir": str(project),
        "scope": "project",
    }

    preview_response = _call(
        selection,
        "agent.setup.preview",
        arguments,
        base_executable=runtime,
        environment={"HOME": str(tmp_path)},
    )
    preview = cast(dict[str, object], preview_response["result"])
    applied = _call(
        selection,
        "agent.setup.apply",
        {**arguments, "preview_id": preview["preview_id"]},
        base_executable=runtime,
        environment={"HOME": str(tmp_path)},
    )

    assert cast(dict[str, object], applied["result"])["status"] == "configured"
    entry = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "open-brain"
    ]
    assert entry["command"] == str(runtime)
    assert entry["args"][-1] == "--allow-capture"
    assert not selection.brain_root.exists()

    denied = _call(
        selection,
        "agent.setup.preview",
        {**arguments, "allow_capture": False},
        base_executable=runtime,
        environment={"HOME": str(tmp_path)},
    )
    assert cast(dict[str, object], denied["error"])["code"] == "invalid_arguments"


def test_bridge_rejects_unknown_operations_without_echoing_input(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    request_id = f"plugin_{uuid.uuid4()}"
    request = {
        "arguments": {"text": "private body"},
        "operation": "unknown.operation",
        "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
        "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
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
            "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
            "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
            "request_id": f"plugin_{uuid.uuid4()}",
        },
        {
            "arguments": {},
            "operation": "workspace.status",
            "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
            "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
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


def test_persistent_stdio_initialize_is_idempotent_after_workspace_status(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    operations = [
        ("system.handshake", {}),
        ("workspace.status", {}),
        ("brain.initialize", {}),
        ("brain.initialize", {"unexpected": True}),
        ("brain.initialize", {}),
        ("workspace.status", {}),
    ]
    requests = [
        {
            "arguments": arguments,
            "operation": operation,
            "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
            "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
            "request_id": f"plugin_{uuid.uuid4()}",
        }
        for operation, arguments in operations
    ]
    output = BytesIO()

    assert (
        serve_plugin_stdio(
            selection,
            input_stream=BytesIO(
                b"".join(json.dumps(request).encode("utf-8") + b"\n" for request in requests)
            ),
            output_stream=output,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]

    assert [response["ok"] for response in responses] == [
        True,
        True,
        True,
        False,
        True,
        True,
    ]
    assert [cast(dict[str, object], responses[index]["result"])["status"] for index in (2, 4)] == [
        "already_initialized",
        "already_initialized",
    ]
    assert cast(dict[str, object], responses[3]["error"])["code"] == "invalid_arguments"
    assert cast(dict[str, object], responses[5]["result"])["status"] == "unconfigured"


def test_concurrent_local_clients_preserve_live_consent_and_reserved_state(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as first:
        space_id = first.tasks.inbox.create_space(
            "Concurrent", delivery_id="desktop.concurrent.space"
        ).space_id
        note_ids: list[str] = []
        for index, text in enumerate(("First concurrent note", "Second concurrent note")):
            first.tasks.capture.accept(
                TextPayload(text),
                delivery_id=f"desktop.concurrent.capture.{index}",
                action=CaptureAction.CANONICAL_NOTE,
                space_id=space_id,
            )
            result = first.tasks.retrieval.search(text, record_type="canonical")[0]
            note_ids.append(result.result_id)
        workspace = tmp_path / "concurrent-workspace"
        workspace.mkdir(mode=0o700)
        workspace_id = first.tasks.managed_workspace.setup(
            str(workspace), operation_id="desktop.concurrent.setup"
        ).workspace_id
        first.tasks.managed_policy.grant_consent(
            workspace_id,
            ManagedProvider.OPENAI_API,
            ManagedAccessMode.API_KEY,
            operation_id="desktop.concurrent.consent",
        )
        with open_local_brain(selection, filesystem_type_probe=_filesystem) as second:
            prepared = second.tasks.managed_inference.prepare(
                workspace_id,
                ManagedProvider.OPENAI_API,
                ManagedAccessMode.API_KEY,
                "openai_api:synthetic-v1",
                tuple(note_ids),
                request_id="request_00000000-0000-4000-8000-000000000901",
                max_output_bytes=4096,
                timeout_seconds=30,
            )
            assert prepared.request_id == "request_00000000-0000-4000-8000-000000000901"
            assert first.tasks.managed_inference.release(prepared.request_id) == prepared
            assert second.tasks.managed_inference.fail(prepared.request_id).status == "failed"


def test_v3_migration_waits_until_an_older_registered_runtime_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = _selection(tmp_path)
    from packages.app.tests.integration.engine._local_schema_fixtures import use_schema_six_runtime

    with monkeypatch.context() as historical:
        use_schema_six_runtime(historical, {})
        from open_brain.services import local_bootstrap

        historical.setattr(local_bootstrap, "PHASE1_STATE_SCHEMA_VERSION", 6)
        assert _call(selection, "brain.initialize")["ok"] is True
        database = selection.brain_root / ".open-brain/state/phase1.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version >= 4")
            for table in (
                "managed_recovery_decisions",
                "managed_write_authority",
                "review_page_heads",
                "review_sources",
                "review_contexts",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("DROP TABLE runtime_compatibility")
            connection.execute("PRAGMA user_version = 3")
        with open_local_brain(selection, filesystem_type_probe=_filesystem) as migrated:
            migrated.tasks.capture.accept(
                TextPayload("Synthetic compatibility record"),
                delivery_id="desktop.compatibility.capture",
            )
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version >= 4")
            for table in (
                "managed_recovery_decisions",
                "managed_write_authority",
                "review_page_heads",
                "review_sources",
                "review_contexts",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("DROP TABLE runtime_compatibility")
            connection.execute("PRAGMA user_version = 3")
    profile = open_existing_single_user_local(selection.brain_root)
    with (
        hold_local_runtime_session(
            profile.root,
            profile.root_identity,
            legacy_state_exists=True,
            recover_abandoned_sessions=lambda: None,
        ),
        pytest.raises(LocalRuntimeCompatibilityError, match="exclusive runtime"),
        open_local_brain(selection, filesystem_type_probe=_filesystem),
    ):
        pass
    assert sqlite3.connect(database).execute("PRAGMA user_version").fetchone() == (3,)

    with open_local_brain(selection, filesystem_type_probe=_filesystem) as reopened:
        assert reopened.tasks.retrieval.search("compatibility")[0].title
    assert sqlite3.connect(database).execute("PRAGMA user_version").fetchone() == (9,)


def test_bridge_rejects_wrong_or_missing_protocol_versions(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    request_id = f"plugin_{uuid.uuid4()}"
    for protocol_version, error_code in ((2, "incompatible_protocol"), (None, "invalid_request")):
        request: dict[str, object] = {
            "arguments": {},
            "operation": "system.handshake",
            "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
            "request_id": request_id,
        }
        if protocol_version is not None:
            request["protocol_version"] = protocol_version
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

        response = cast(dict[str, object], json.loads(output.getvalue()))
        assert response == {
            "error": {"code": error_code},
            "ok": False,
            "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
            "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
            "request_id": None,
        }


def test_bridge_rejects_newer_schema_without_mutating_it(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    database = selection.brain_root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")
    before = database.read_bytes()

    response = _call(selection, "capture.create", {"text": "synthetic rejection"})

    assert response["ok"] is False
    assert cast(dict[str, object], response["error"])["code"] == "incompatible_schema"
    assert database.read_bytes() == before


def test_bridge_reports_session_exhaustion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    monkeypatch.setattr(plugin_bridge_module, "MAX_PLUGIN_SESSION_REQUESTS", 1)
    requests = [
        {
            "arguments": {},
            "operation": "system.handshake",
            "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
            "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
            "request_id": f"plugin_{uuid.uuid4()}",
        }
        for _index in range(2)
    ]
    output = BytesIO()

    assert (
        serve_plugin_stdio(
            selection,
            input_stream=BytesIO(
                b"".join(json.dumps(request).encode("utf-8") + b"\n" for request in requests)
            ),
            output_stream=output,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["ok"] is True
    assert responses[1] == {
        "error": {"code": "session_exhausted"},
        "ok": False,
        "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
        "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
        "request_id": None,
    }


def test_stale_client_marker_runs_crash_recovery_and_revokes_consent(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    assert _call(selection, "brain.initialize")["ok"] is True
    workspace = tmp_path / "crash-workspace"
    workspace.mkdir(mode=0o700)
    request_id = "request_00000000-0000-4000-8000-000000000902"
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        space_id = session.tasks.inbox.create_space(
            "Crash recovery", delivery_id="desktop.crash.space"
        ).space_id
        session.tasks.capture.accept(
            TextPayload("Synthetic crash recovery note"),
            delivery_id="desktop.crash.capture",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=space_id,
        )
        note_id = session.tasks.retrieval.search(
            "Synthetic crash recovery note", record_type="canonical"
        )[0].result_id
        workspace_id = session.tasks.managed_workspace.setup(
            str(workspace), operation_id="desktop.crash.setup"
        ).workspace_id
        session.tasks.managed_policy.grant_consent(
            workspace_id,
            ManagedProvider.OPENAI_API,
            ManagedAccessMode.API_KEY,
            operation_id="desktop.crash.consent",
        )
        session.tasks.managed_inference.prepare(
            workspace_id,
            ManagedProvider.OPENAI_API,
            ManagedAccessMode.API_KEY,
            "openai_api:synthetic-v1",
            (note_id,),
            request_id=request_id,
            max_output_bytes=4096,
            timeout_seconds=30,
        )
        session_id = "b" * 32
        marker = selection.brain_root / f".open-brain/runtime-sessions/session-{session_id}.lock"
        marker.write_bytes(
            canonical_json_bytes({"pid": 12345, "session_id": session_id, "version": 1})
        )
        marker.chmod(0o600)

    with (
        open_local_brain(selection, filesystem_type_probe=_filesystem) as recovered,
        pytest.raises(ManagedWorkspaceFailure, match="active_consent_required"),
    ):
        recovered.tasks.managed_inference.prepare(
            workspace_id,
            ManagedProvider.OPENAI_API,
            ManagedAccessMode.API_KEY,
            "openai_api:synthetic-v1",
            (note_id,),
            request_id="request_00000000-0000-4000-8000-000000000903",
            max_output_bytes=4096,
            timeout_seconds=30,
        )
    database = selection.brain_root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM managed_inference_requests WHERE request_id = ?", (request_id,)
        ).fetchone() == ("cancelled",)
        assert connection.execute(
            "SELECT active FROM managed_consents WHERE workspace_id = ?", (workspace_id,)
        ).fetchone() == (0,)


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
        assert listed["exclusions"] == [{"kind": "note", "relative_path": source.relative_path}]
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
    credential_value = "synthetic-provider-secret"

    class FakeAdapter:
        identity = "openai_api:synthetic-plugin-v1"

        def bind(self, resolve: object) -> object:
            assert callable(resolve)

            def invoke(
                prompt: str, max_output_bytes: int, timeout_seconds: int
            ) -> ManagedGraphProviderResult:
                assert resolve() == credential_value
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
                "credential": credential_value,
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
        assert credential_value not in json.dumps((configured, refreshed, status))

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
