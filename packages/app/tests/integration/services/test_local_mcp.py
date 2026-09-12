from __future__ import annotations

import io
import json
import os
import select
import sqlite3
import subprocess
import sys
import time
from collections.abc import Buffer
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import (
    CaptureReceipt,
    CaptureTask,
    PublicJobCaptureSink,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.storage.operational import FileLease

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import MAX_MESSAGE_BYTES, LocalMcpAdapter
from open_brain.services.local_operations import mcp_capture_sink, search_brain
from open_brain.services.mcp_protocol import McpCallError, serve_stdio_mcp

ROOT = Path(__file__).resolve().parents[5]
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
}
PROGRAM = """
import socket, sys
from open_brain.services.local_entrypoints import run_cli

def forbidden(*args, **kwargs):
    raise AssertionError('network use')
for name in ('connect', 'connect_ex', 'bind', 'listen'):
    setattr(socket.socket, name, forbidden)
result = run_cli()
assert not any(
    name.startswith(
        (
            'open_brain.integrations',
            'open_brain.services.appliance_',
            'open_brain.services.phase1_',
            'open_brain.services.secure_node',
            'open_brain_connectors',
        )
    )
    for name in sys.modules
)
raise SystemExit(result)
"""


def _call(name: str, arguments: dict[str, object], identifier: int = 2) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


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


def _adapter(tasks: Any, capture: bool = True, search: bool = True) -> LocalMcpAdapter:
    return LocalMcpAdapter(
        capture=mcp_capture_sink(tasks) if capture else None,
        search=(
            lambda query, limit: search_brain(
                tasks.retrieval, tasks.reconciliation, query, limit=limit
            )
        )
        if search
        else None,
    )


@pytest.fixture
def tasks(tmp_path: Path) -> Any:
    return open_local_engine(compile_single_user_local(tmp_path / "brain"))


@pytest.mark.parametrize(("capture", "search"), [(True, False), (False, True), (True, True)])
def test_capabilities_are_independently_listed_and_enforced(
    tasks: Any,
    capture: bool,
    search: bool,
) -> None:
    adapter = _adapter(tasks, capture, search)
    responses = _wire(
        adapter,
        INITIALIZE,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        _call("brain_capture", {"text": "w6 capability token"}, 3),
        _call("brain_search", {"query": "w6 capability token"}, 4),
        _call("brain_fetch", {"result_id": "unused"}, 5),
    )
    assert responses[0]["result"]["capabilities"] == {"tools": {}}
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == (
        ({"brain_capture"} if capture else set()) | ({"brain_search"} if search else set())
    )
    assert responses[2]["result"].get("isError", False) is not capture
    assert responses[3]["result"].get("isError", False) is not search
    assert responses[4]["result"]["content"][0]["text"] == "unknown tool"
    if capture and search:
        result = responses[3]["result"]["structuredContent"]["results"][0]
        assert result["trust"] == "unverified"
        assert result["source_origin"] == "unknown"


def test_neither_flag_and_json_fail_before_bootstrap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "absent"
    for args in [
        ("mcp",),
        ("mcp", "--allow-capture", "--json"),
        ("--json", "mcp", "--allow-search"),
    ]:
        assert run_cli((*args, "--data-dir", str(root))) == 2
        assert capsys.readouterr().out == ""
        assert not root.exists()
    with pytest.raises(ValueError, match="no MCP capability"):
        LocalMcpAdapter()


def test_help_discloses_both_choices_without_bootstrap(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(("mcp", "--help"), environment={}) == 0
    output = capsys.readouterr().out
    for phrase in (
        "whole-Brain",
        "network-backed",
        "provider",
        "durable",
        "0.1.0",
        "delete",
        "500",
        "16 MiB",
        "2,000",
        "--allow-capture",
        "--allow-search",
    ):
        assert phrase in output


def test_delivery_is_exact_idempotent_across_sessions_and_namespaced(tasks: Any) -> None:
    adapter = _adapter(tasks)
    key = "synthetic-key-with-private-meaning"
    first = adapter.call_tool("brain_capture", {"text": "Café nebula", "idempotency_key": key})
    repeat = _adapter(tasks).call_tool(
        "brain_capture",
        {
            "text": "Café nebula",
            "idempotency_key": key,
        },
    )
    assert repeat == {**first, "duplicate": True}
    for different in ("other text", "Cafe\u0301 nebula"):
        with pytest.raises(McpCallError, match="^idempotency_conflict$"):
            adapter.call_tool("brain_capture", {"text": different, "idempotency_key": key})
    tasks.capture.accept(TextPayload("CLI namespace"), delivery_id=key)
    result = adapter.call_tool("brain_search", {"query": "nebula"})
    assert len(cast(list[object], result["results"])) == 1
    assert key not in json.dumps([first, repeat, result])
    connection = sqlite3.connect(tasks.profile.root / ".open-brain/state/phase1.sqlite3")
    try:
        rows = connection.execute(
            "SELECT delivery_id, actor_id, role_claim_json, submission_path FROM captures"
        ).fetchall()
        assert len(rows) == 2
        mcp_row = next(row for row in rows if row[0].startswith("delivery.mcp.key."))
        assert key not in mcp_row[0]
        assert mcp_row[1] != tasks.profile.owner_actor_id
        assert json.loads(mcp_row[2])["capabilities"] == ["capture.accept"]
        assert mcp_row[3] == "public_job"
    finally:
        connection.close()
    a = adapter.call_tool("brain_capture", {"text": "keyless"})
    b = adapter.call_tool("brain_capture", {"text": "keyless"})
    assert a["capture_id"] != b["capture_id"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("brain_capture", {}),
        ("brain_capture", {"text": ""}),
        ("brain_capture", {"text": " "}),
        ("brain_capture", {"text": "\ud800"}),
        ("brain_capture", {"text": "x" * 65537}),
        ("brain_capture", {"text": "note", "idempotency_key": None}),
        ("brain_capture", {"text": "note", "idempotency_key": "x" * 129}),
        ("brain_capture", {"text": "note", "idempotency_key": "\ud800"}),
        ("brain_capture", {"text": "note", "action": "publish"}),
        ("brain_search", {}),
        ("brain_search", {"query": "x", "limit": True}),
        ("brain_search", {"query": "x", "limit": 0}),
        ("brain_search", {"query": "x", "limit": 11}),
        ("brain_search", {"query": "x" * 501}),
        ("brain_search", {"query": "\ud800"}),
        ("brain_search", {"query": "\x00"}),
        ("brain_search", {"query": "***"}),
        ("brain_search", {"query": "x", "space_id": "unauthorized"}),
    ],
)
def test_invalid_arguments_do_not_consume_budget(tasks: Any, tool: str, arguments: Any) -> None:
    adapter = _adapter(tasks)
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool(tool, arguments)
    assert (adapter._capture_calls, adapter._capture_bytes, adapter._search_calls) == (0, 0, 0)
    assert tasks.inbox.list() == ()


class _CountingCapture:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, submission: Any) -> CaptureReceipt:
        self.calls += 1
        if submission.payload.text == "conflict":
            raise ValueError("conflicting delivery")
        return CaptureReceipt(
            capture_id="capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
            payload_family="text",
            state="inbox",
            enrichment_state="pending",
            space_id=None,
            canonical_path=None,
            duplicate=self.calls > 1,
        )


def _counting_adapter(tasks: Any) -> tuple[LocalMcpAdapter, _CountingCapture]:
    capture = _CountingCapture()
    sink = PublicJobCaptureSink(cast(CaptureTask, capture), context=mcp_capture_sink(tasks).context)
    return LocalMcpAdapter(capture=sink), capture


def test_exact_500_capture_calls_include_duplicates_and_conflicts(tasks: Any) -> None:
    adapter, capture = _counting_adapter(tasks)
    for index in range(500):
        if index % 2:
            with pytest.raises(McpCallError, match="idempotency_conflict"):
                adapter.call_tool("brain_capture", {"text": "conflict"})
        else:
            adapter.call_tool("brain_capture", {"text": "duplicate", "idempotency_key": "same"})
    with pytest.raises(McpCallError, match="^session_capture_limit$"):
        adapter.call_tool("brain_capture", {"text": "next"})
    assert capture.calls == 500


def test_exact_utf8_16_mib_budget_rejects_before_work(tasks: Any) -> None:
    adapter, capture = _counting_adapter(tasks)
    text = "\U0001f680" * 65536
    for _ in range(64):
        adapter.call_tool("brain_capture", {"text": text})
    assert adapter._capture_bytes == 16 * 1024 * 1024
    with pytest.raises(McpCallError, match="^session_capture_limit$"):
        adapter.call_tool("brain_capture", {"text": "a"})
    assert capture.calls == 64


def test_byte_budget_rejects_crossing_call_without_partial_charge(tasks: Any) -> None:
    adapter, capture = _counting_adapter(tasks)
    adapter._capture_bytes = 16 * 1024 * 1024 - 1
    with pytest.raises(McpCallError, match="session_capture_limit"):
        adapter.call_tool("brain_capture", {"text": "é"})
    assert capture.calls == 0
    assert adapter._capture_calls == 1
    adapter.call_tool("brain_capture", {"text": "a"})
    assert capture.calls == 1
    assert adapter._capture_calls == 2


def test_exact_2000_search_attempts_count_backend_failures() -> None:
    calls = 0

    def search(query: str, limit: int) -> tuple[()]:
        nonlocal calls
        calls += 1
        if calls % 2:
            raise RuntimeError("synthetic-private-error")
        return ()

    adapter = LocalMcpAdapter(search=search)
    for _ in range(1000):
        with pytest.raises(McpCallError, match="^tool call failed$"):
            adapter.call_tool("brain_search", {"query": "token"})
        adapter.call_tool("brain_search", {"query": "token"})
    with pytest.raises(McpCallError, match="^session_search_limit$"):
        adapter.call_tool("brain_search", {"query": "token"})
    assert calls == 2000


def test_transport_refuses_bad_initialize_and_recovers_from_oversize(tasks: Any) -> None:
    adapter = _adapter(tasks)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        _call("brain_capture", {"text": "must not be written"}),
        INITIALIZE,
        _call("brain_capture", {"text": "after valid init"}),
    ]
    responses = _wire(adapter, *messages)
    assert responses[0]["error"]["code"] == -32602
    assert responses[1]["error"]["message"] == "server not initialized"
    assert responses[3]["result"]["structuredContent"]["status"] == "captured"
    incoming = io.BytesIO(
        b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n" + json.dumps(INITIALIZE).encode() + b"\n"
    )
    outgoing = io.BytesIO()
    serve_stdio_mcp(
        adapter,
        input_stream=incoming,
        output_stream=outgoing,
        maximum_message_bytes=MAX_MESSAGE_BYTES,
    )
    replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32600
    assert "result" in replies[1]
    assert adapter._capture_calls == 1


def test_protocol_errors_are_redacted_and_eof_and_broken_pipe_are_clean() -> None:
    def search(query: str, limit: int) -> tuple[()]:
        raise RuntimeError("synthetic-private-path-and-credential")

    adapter = LocalMcpAdapter(search=search)
    responses = _wire(adapter, INITIALIZE, _call("brain_search", {"query": "private-query"}))
    assert responses[1]["result"]["content"] == [{"type": "text", "text": "tool call failed"}]
    assert "private" not in json.dumps(responses)
    assert _wire(adapter) == []

    class BrokenOutput(io.BytesIO):
        def write(self, data: Buffer, /) -> int:
            raise BrokenPipeError

    serve_stdio_mcp(
        adapter,
        input_stream=io.BytesIO(json.dumps(INITIALIZE).encode() + b"\n"),
        output_stream=BrokenOutput(),
    )


def test_cli_and_mcp_share_semantic_dataset_results_and_export(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = json.loads((ROOT / "tests/fixtures/local-schema/dataset.json").read_text())
    root = tmp_path / "brain"
    for text in dataset["captures"]:
        assert run_cli(("capture", text, "--data-dir", str(root), "--json")) == 0
        capsys.readouterr()
    vault = tmp_path / "vault"
    vault.mkdir()
    for name, text in dataset["markdown"].items():
        (vault / name).write_text(text)
    assert run_cli(("import", str(vault), "--yes", "--data-dir", str(root), "--json")) == 0
    capsys.readouterr()
    tasks = open_local_engine(compile_single_user_local(root))
    adapter = _adapter(tasks)
    captured = adapter.call_tool("brain_capture", {"text": dataset["changed"]})
    mcp_result = adapter.call_tool("brain_search", {"query": "nebula", "limit": 10})
    assert run_cli(("search", "nebula", "--data-dir", str(root), "--json")) == 0
    assert json.loads(capsys.readouterr().out) == mcp_result
    hits = cast(list[dict[str, object]], mcp_result["results"])
    assert len(hits) == 5
    hit = next(item for item in hits if item["capture_id"] == captured["capture_id"])
    assert hit["trust"] == "unverified" and hit["source_origin"] == "unknown"
    assert "source_ref" not in json.dumps(mcp_result)
    export = tmp_path / "export"
    assert run_cli(("export", str(export), "--verify", "--data-dir", str(root), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1
    record = json.loads(
        next((export / "sources/captures").rglob(str(captured["capture_id"]) + ".json")).read_text()
    )
    assert record["source"]["origin"] == "third_party"
    assert record["provenance"]["content_origin"] == "unknown"
    assert record["provenance"]["owner_context"] == "automation_absent"
    assert record["trust"]["label"] == "unverified"


def _start(root: Path, *flags: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", PROGRAM, "mcp", "--data-dir", str(root), *flags],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )


def _exchange(process: subprocess.Popen[str], message: dict[str, object]) -> Any:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    assert select.select([process.stdout], [], [], 15)[0], "MCP response timeout"
    return json.loads(process.stdout.readline())


def test_live_cli_and_mcp_share_brain_and_bound_contention(
    tasks: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tasks.profile.root
    process = _start(root, "--allow-capture", "--allow-search")
    try:
        assert "result" in _exchange(process, INITIALIZE)
        # Hold the actual cross-process mutation lease after MCP startup.
        lease = FileLease(root / ".open-brain", owner_identity_id="w6-test")
        with lease.acquire_shared_writer():
            started = time.monotonic()
            busy = _exchange(process, _call("brain_capture", {"text": "blocked-token"}))
            assert busy["result"]["content"][0]["text"] == "database_busy"
            assert run_cli(("capture", "blocked-cli", "--data-dir", str(root), "--json")) == 75
            assert json.loads(capsys.readouterr().out)["error"]["code"] == "database_busy"
            assert time.monotonic() - started < 10
        assert tasks.inbox.list() == ()
        cli = subprocess.Popen(
            [
                sys.executable,
                "-c",
                PROGRAM,
                "capture",
                "cli-parallel-nebula",
                "--data-dir",
                str(root),
                "--json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        mcp = _exchange(process, _call("brain_capture", {"text": "mcp-parallel-nebula"}))
        output, error = cli.communicate(timeout=15)
        assert cli.returncode in (0, 75), error
        assert mcp["result"].get("isError") is not True or (
            mcp["result"]["content"][0]["text"] == "database_busy"
        )
        if cli.returncode == 75:
            assert json.loads(output)["error"]["code"] == "database_busy"
        if mcp["result"].get("isError"):
            mcp = _exchange(process, _call("brain_capture", {"text": "mcp-parallel-nebula"}))
        assert mcp["result"]["structuredContent"]["status"] == "captured"
        assert run_cli(("search", "mcp-parallel-nebula", "--data-dir", str(root), "--json")) == 0
        assert json.loads(capsys.readouterr().out)["results"][0]["source_origin"] == "unknown"
        # An external SQLite writer exercises the real five-second busy timeout.
        connection = sqlite3.connect(
            root / ".open-brain/state/phase1.sqlite3", isolation_level=None
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            busy = _exchange(process, _call("brain_capture", {"text": "sqlite-blocked-token"}))
            assert busy["result"]["content"][0]["text"] == "database_busy"
            assert 4 <= time.monotonic() - started < 12
            connection.execute("ROLLBACK")
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()
        assert (
            _exchange(process, _call("brain_search", {"query": "sqlite-blocked-token"}))["result"][
                "structuredContent"
            ]["results"]
            == []
        )
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None and process.stderr.read() == ""
        assert not any((root / ".open-brain/run").iterdir())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_session_limits_are_public_protocol_errors_without_engine_work(tasks: Any) -> None:
    adapter = _adapter(tasks)
    adapter._capture_calls = 500
    adapter._search_calls = 2000
    replies = _wire(
        adapter,
        INITIALIZE,
        _call("brain_capture", {"text": "not accepted"}),
        _call("brain_search", {"query": "not accepted"}, 3),
    )
    assert [reply["result"]["content"][0]["text"] for reply in replies[1:]] == [
        "session_capture_limit",
        "session_search_limit",
    ]
    assert all(reply["result"]["isError"] for reply in replies[1:])
    assert tasks.inbox.list() == ()


def test_mcp_projected_results_and_conflicts_hide_protected_values(tasks: Any) -> None:
    path = "/synthetic-private/" + "note.txt"
    credential = "api" + "_key=" + "A" * 32
    digest = "ab" * 32
    key = "synthetic-client-private-key"
    text = "w6 residue canary " + path + " " + credential + " " + digest
    adapter = _adapter(tasks)
    replies = _wire(
        adapter,
        INITIALIZE,
        _call("brain_capture", {"text": text, "idempotency_key": key}),
        _call("brain_capture", {"text": "different", "idempotency_key": key}, 3),
        _call("brain_search", {"query": "w6 residue canary"}, 4),
    )
    rendered = json.dumps(replies)
    for private in (path, credential, digest, key, "urn:open-brain:mcp:", "source_reference"):
        assert private not in rendered
    assert replies[2]["result"]["content"][0]["text"] == "idempotency_conflict"
    assert len(replies[3]["result"]["structuredContent"]["results"]) == 1


@pytest.mark.parametrize(
    "payload", [b"[" * 2000 + b"]" * 2000, b"9" * 5000, b"\xff"],
    ids=["nested-array", "overlong-number", "invalid-utf8"],
)
def test_transport_parser_failures_are_bounded_and_recover(payload: bytes) -> None:
    adapter = LocalMcpAdapter(search=lambda _query, _limit: ())
    output = io.BytesIO()
    serve_stdio_mcp(
        adapter,
        input_stream=io.BytesIO(payload + b"\n" + json.dumps(INITIALIZE).encode() + b"\n"),
        output_stream=output,
    )
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["error"]["code"] in {-32700, -32600}
    assert "result" in replies[1]
