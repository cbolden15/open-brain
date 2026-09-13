from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.engine import CaptureAction, ReferencePayload, TextPayload

from open_brain.capture.http import HttpRequest
from open_brain.cli.phase1 import Phase1CommandAdapter
from open_brain.integrations.mcp import EngineMcpAdapter
from open_brain.services.phase1_application import SingleUserLocalApplication


def test_single_user_local_application_owns_one_engine_task_set(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")

    assert application.tasks.profile.root == tmp_path / "brain"
    capture_adapter = application.cli_adapters().get("capture")
    assert isinstance(capture_adapter, Phase1CommandAdapter)
    assert capture_adapter.task is application.tasks.capture
    assert application.ui_handler("synthetic-ui-token").tasks is application.tasks.phase1
    assert not hasattr(capture_adapter, "profile")
    assert not hasattr(capture_adapter, "portability")
    assert not hasattr(application.ui_handler("synthetic-ui-token").tasks, "profile")
    assert not hasattr(application.ui_handler("synthetic-ui-token").tasks, "portability")
    handler = application.share_handler(
        expected_bearer_token="synthetic-http-token",
        body_reader=lambda _maximum, _timeout: b"",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert object.__getattribute__(handler, "capture") is application.tasks.capture
    mcp = application.mcp_adapter()
    assert mcp.retrieval is not application.tasks.retrieval
    assert not callable(getattr(mcp.retrieval, "scoped", None))
    with pytest.raises(ValueError, match="invalid engine MCP capabilities"):
        EngineMcpAdapter(
            retrieval=application.tasks.retrieval,
            feedback=application.feedback,
        )
    assert application.public_job_sink("JOB-005")._capture is application.tasks.capture


def test_mcp_fetch_hides_known_disallowed_results_like_unknown(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    space = application.tasks.inbox.create_space("MCP", delivery_id="mcp.space")
    application.tasks.capture.accept(
        TextPayload("Synthetic MCP scoped token"),
        delivery_id="mcp.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    allowed = application.mcp_adapter(allowed_space_ids=frozenset({space.space_id}))
    default = application.mcp_adapter()
    result = allowed.call_tool("brain_query", {"question": "scoped token"})
    result_id = cast(list[dict[str, object]], result["results"])[0]["result_id"]

    assert isinstance(result_id, str)
    assert allowed.retrieval.fetch(result_id) is not None
    assert default.retrieval.fetch(result_id) is None
    assert default.call_tool("brain_query", {"question": "scoped token"})["results"] == []
    assert default.call_tool("brain_fetch", {"result_id": result_id}) == default.call_tool(
        "brain_fetch", {"result_id": "unknown_result"}
    )


def test_public_surface_results_exclude_raw_source_references_and_digests(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    source_reference = "https://example.test/synthetic-private-source"
    space = application.tasks.inbox.create_space("Safe", delivery_id="safe.space")
    capture = application.tasks.capture.accept(
        ReferencePayload(source_reference, "Synthetic source-safe token"),
        delivery_id="safe.capture",
    )
    application.tasks.inbox.route(capture.capture_id, space.space_id, delivery_id="safe.route")
    adapter = application.mcp_adapter(allowed_space_ids=frozenset({space.space_id}))
    question = source_reference
    result = adapter.call_tool("brain_query", {"question": question})
    replay = adapter.call_tool("brain_query", {"question": question})
    rendered = json.dumps(result, sort_keys=True)
    query_digest = sha256(question.encode()).hexdigest()

    assert source_reference not in rendered
    assert sha256(source_reference.encode()).hexdigest() not in rendered
    assert query_digest[:32] not in str(result["retrieval_id"])
    assert result["retrieval_id"] != replay["retrieval_id"]
    assert "source_ref" not in rendered
    assert "source_url" not in rendered
    assert str(tmp_path) not in rendered


def test_authenticated_http_submits_four_families_and_replays(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    token = "synthetic-http-token"
    payloads = (
        {"family": "text", "text": "Synthetic HTTP text"},
        {
            "family": "reference_or_file",
            "kind": "reference",
            "url": "https://example.test/http-reference",
        },
        {
            "data_base64": base64.b64encode(b"Synthetic HTTP file").decode(),
            "family": "reference_or_file",
            "file_name": "http.txt",
            "kind": "file",
            "media_type": "text/plain",
        },
        {
            "attributes": {"label": "Synthetic HTTP event"},
            "event_type": "synthetic.http",
            "family": "event",
        },
        {
            "dimensions": {"label": "Synthetic HTTP measurement"},
            "family": "measurement",
            "unit": "count",
            "value": "7",
        },
    )

    responses = []
    for index, payload in enumerate(payloads):
        body = canonical_json_bytes({"delivery_id": f"http.family.{index}", "payload": payload})
        handler = application.share_handler(
            expected_bearer_token=token,
            body_reader=lambda _maximum, _timeout, value=body: value,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        responses.append(
            handler.handle(
                HttpRequest(
                    "POST",
                    "/captures",
                    (
                        ("Authorization", "Bearer " + token),
                        ("Content-Length", str(len(body))),
                        ("Content-Type", "application/json"),
                    ),
                )
            )
        )

    replay_body = canonical_json_bytes(
        {"delivery_id": "http.family.0", "payload": payloads[0]}
    )
    replay = application.share_handler(
        expected_bearer_token=token,
        body_reader=lambda _maximum, _timeout: replay_body,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    ).handle(
        HttpRequest(
            "POST",
            "/captures",
            (
                ("Authorization", "Bearer " + token),
                ("Content-Length", str(len(replay_body))),
                ("Content-Type", "application/json"),
            ),
        )
    )

    assert [response.status for response in responses] == [201, 201, 201, 201, 201]
    assert replay.status == 200
    assert json.loads(responses[0].body)["capture_id"] == json.loads(replay.body)["capture_id"]
