"""Independent real CLI/engine/Portable pipeline; no fake task installation."""

from __future__ import annotations

import importlib
import io
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.contracts import CaptureReceipt, TextPayload
from open_brain_engine.engine.facet_contracts import (
    DimensionCreateRequest,
    DimensionRenameRequest,
    FacetError,
)
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, SourceRouteRequest
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.mcp_protocol import McpCallError, serve_stdio_mcp
from packages.engine.tests.contract.test_facets_acceptance import FacetHarness, public_revision


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def test_f22_real_cli_revision_query_publish_archive_reconcile_export_import_and_fresh_query(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Complete prospective F22 path; requires integrated E/A/P/X/V producers."""
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    destination = engine.inbox.create_space("Synthetic facets", delivery_id="independent-space")
    source = public_revision(engine, 1, None)
    assert source.capture_id is not None and source.source_id is not None
    engine.sources.route(
        SourceRouteRequest(
            source_id=source.source_id,
            expected_head=source.capture_id,
            expected_route_version=0,
            space_id=destination.space_id,
            operation_id="operation_00000000-0000-4000-8000-000000000095",
        ),
        authority=EffectiveAuthority("pipeline-owner", "owner", frozenset(), None, owner=True),
    )

    def cli(data_root: Path, *arguments: str) -> dict[str, Any]:
        status = run_cli(
            (*arguments, "--data-dir", str(data_root), "--json"), filesystem_type_probe=_filesystem
        )
        output = capsys.readouterr()
        assert status == 0, (arguments, output.out, output.err)
        assert output.err == ""
        return cast(dict[str, Any], json.loads(output.out))

    dimension = cli(
        root,
        "facet",
        "dimension",
        "create",
        "Project",
        "--operation-id",
        "pipeline-dimension",
        "--expected-version",
        "0",
    )["subject_id"]
    values = []
    for label in ("Apollo", "Research"):
        values.append(
            cli(
                root,
                "facet",
                "value",
                "create",
                "--dimension",
                dimension,
                label,
                "--operation-id",
                f"pipeline-value-{label}",
                "--expected-version",
                "0",
                "--expected-dimension-version",
                "1",
            )["subject_id"]
        )
    assignment = cli(
        root,
        "facet",
        "assignment",
        "patch",
        source.capture_id,
        "--add",
        values[0],
        "--add",
        values[1],
        "--operation-id",
        "pipeline-assignment",
        "--expected-version",
        "0",
        "--expected-revision-id",
        source.capture_id,
    )
    assert assignment["status"] == "applied" and assignment["version"] == 1
    revised = public_revision(engine, 2, source.capture_id)
    assert revised.capture_id is not None and revised.source_id == source.source_id
    selector = tmp_path / "selector.json"
    selector.write_text(json.dumps({"all_value_ids": values, "record_types": ["source"]}))
    query = cli(
        root,
        "facet",
        "query",
        "create",
        "Apollo research",
        "--selector-file",
        str(selector),
        "--operation-id",
        "pipeline-query",
        "--expected-version",
        "0",
    )["subject_id"]
    original = cli(root, "facet", "query", "run", query, "--expected-query-version", "1")
    assert [row["record"]["record_id"] for row in original["items"]] == [revised.capture_id]
    draft = tmp_path / "draft.md"
    draft.write_text("Synthetic reviewed facet page.\n")
    proposal = cli(
        root,
        "review",
        "propose",
        "--facet-query-id",
        query,
        "--expected-query-version",
        "1",
        "--destination-space-id",
        destination.space_id,
        "--title",
        "Apollo research page",
        "--markdown-file",
        str(draft),
        "--idempotency-key",
        "pipeline-proposal",
    )
    inspected = cli(root, "review", "show", proposal["proposal_id"])
    binding = inspected["facet_selection"]
    assert binding["schema_version"] == 5
    assert {
        "query_id",
        "query_version",
        "query_selection_version",
        "selector",
        "facet_source_states",
        "destination_space_id",
        "attachment_version",
    } <= set(binding)
    assert binding["destination_space_id"] == destination.space_id
    published = cli(
        root,
        "review",
        "approve",
        proposal["proposal_id"],
        "--review-token",
        inspected["review_token"],
        "--idempotency-key",
        "pipeline-approve",
    )
    page = published["page_id"]
    page_state = cli(root, "facet", "assignment", "show", page)["item"]
    assert page_state["memberships"] == []
    cli(
        root,
        "facet",
        "assignment",
        "patch",
        page,
        "--add",
        values[1],
        "--expected-version",
        "0",
        "--expected-revision-id",
        page_state["revision_id"],
        "--operation-id",
        "pipeline-page-assignment",
    )
    cli(
        root,
        "facet",
        "value",
        "archive",
        values[0],
        "--operation-id",
        "pipeline-archive",
        "--expected-version",
        "1",
    )
    assert cli(root, "facet", "query", "run", query, "--expected-query-version", "1") == original
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    workspace = engine.managed_workspace.setup(
        str(vault), operation_id="pipeline-workspace"
    ).workspace_id
    engine.managed_workspace.refresh(workspace, operation_id="pipeline-projection-refresh")
    managed_page = next(vault.rglob(f"{page}.md"))
    parsed = parse_markdown(managed_page.read_bytes())
    managed_page.write_text(
        render_markdown(
            fields={key: value for key, value in parsed.fields.items() if key != "facets"},
            body=parsed.body,
        )
    )
    preview = cli(
        root, "facet", "vault", "preview", "--workspace-id", workspace, "--record-id", page
    )
    assert preview["conflicts"] == [] and preview["remove_value_ids"] == [values[1]]
    applied = cli(
        root,
        "facet",
        "vault",
        "apply",
        "--preview-id",
        preview["preview_id"],
        "--operation-id",
        "pipeline-reconcile",
    )
    assert applied["receipt"]["version"] == 2
    assert parse_markdown(managed_page.read_bytes()).body == parsed.body
    archive = tmp_path / "portable"
    cli(root, "export", str(archive), "--verify")
    manifest = json.loads((archive / "portable-manifest.json").read_bytes())
    assert manifest["schema_version"] == 18
    restored_root = tmp_path / "restored"
    receipt = engine.portability.import_clean(
        archive, restored_root, import_id="import_00000000-0000-4000-8000-000000000001"
    )
    assert receipt.schema_version == 18
    reopened = BrainEngine.open(open_existing_single_user_local(restored_root))
    assert getattr(reopened, "facets", None) is not None
    fresh = cli(restored_root, "facet", "query", "run", query, "--expected-query-version", "1")
    assert fresh == original
    reexport = tmp_path / "reexport"
    cli(restored_root, "export", str(reexport), "--verify")
    for relative in (
        "history/facets/events-v1.json",
        "history/facets/operations-v1.json",
        "content/facets/state-v1.json",
    ):
        assert (archive / relative).read_bytes() == (reexport / relative).read_bytes()


def test_f15_real_cli_retry_preserves_effective_operation_id_and_original_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "brain"
    compile_single_user_local(root)
    arguments = (
        "facet",
        "dimension",
        "create",
        "Retry",
        "--operation-id",
        "cli-retry",
        "--expected-version",
        "0",
        "--data-dir",
        str(root),
        "--json",
    )
    assert run_cli(arguments, filesystem_type_probe=_filesystem) == 0
    original = json.loads(capsys.readouterr().out)
    assert original["operation_id"] == "cli-retry"
    assert run_cli(arguments, filesystem_type_probe=_filesystem) == 0
    assert json.loads(capsys.readouterr().out) == original


def _mcp(engine: BrainEngine, grants: frozenset[str], *, owner: bool = False) -> Any:
    """Construct actual registered adapters with explicit independent session grants."""
    facet_module = importlib.import_module("open_brain.services.facets")
    mcp_module = importlib.import_module("open_brain.services.local_mcp")
    authority = EffectiveAuthority("independent-mcp", "mcp", grants, None, owner=owner)
    facets = getattr(engine, "facets", None)
    assert facets is not None, "Real engine facet registration must precede MCP verification"
    adapter = facet_module.FacetAppAdapter(
        facets=facets,
        publication=getattr(engine, "facet_publication", None),
        authority=authority,
        granted_capabilities=grants,
        budget=facet_module.FacetSessionBudget(),
    )
    return mcp_module.LocalMcpAdapter(authority=authority, facets=adapter)


def _facet_row_counts(root: Path) -> tuple[int, int, int]:
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("facet_dimensions", "facet_events", "facet_operations")
        )


@pytest.mark.parametrize(
    "grants",
    [
        frozenset(),
        frozenset({"search"}),
        frozenset({"organize"}),
        frozenset({"facet-read"}),
        frozenset({"facet-assign"}),
    ],
)
def test_f15_real_mcp_direct_denied_catalogue_calls_have_no_effects(
    tmp_path: Path,
    grants: frozenset[str],
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    adapter = _mcp(engine, grants)
    before = _facet_row_counts(root)
    assert "brain_facet_dimension_create" not in {tool["name"] for tool in adapter.list_tools()}
    with pytest.raises(McpCallError):
        adapter.call_tool(
            "brain_facet_dimension_create",
            {
                "dto_version": 1,
                "name": "Denied",
                "expected_version": 0,
                "operation_id": "denied-direct",
            },
        )
    assert _facet_row_counts(root) == before


def test_f15_even_owner_mcp_authority_retains_explicit_default_off_grants(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    adapter = _mcp(engine, frozenset(), owner=True)
    assert not any(tool["name"].startswith("brain_facet_") for tool in adapter.list_tools())
    with pytest.raises(McpCallError):
        adapter.call_tool(
            "brain_facet_dimension_create",
            {
                "dto_version": 1,
                "name": "Denied",
                "expected_version": 0,
                "operation_id": "owner-denied",
            },
        )
    assert _facet_row_counts(root) == (0, 0, 0)


def test_f15_real_mcp_write_grant_gives_narrow_receipt_without_read_grant(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    adapter = _mcp(engine, frozenset({"facet-catalog-write"}))
    tools = {tool["name"] for tool in adapter.list_tools()}
    assert "brain_facet_dimension_create" in tools and "brain_facet_dimension_list" not in tools
    result = adapter.call_tool(
        "brain_facet_dimension_create",
        {"dto_version": 1, "name": "Narrow", "expected_version": 0, "operation_id": "narrow-write"},
    )
    assert result["status"] == "applied"
    assert set(result) == {
        "dto_version",
        "status",
        "operation_id",
        "subject_id",
        "version",
        "event_id",
    }


def test_f15_real_mcp_retry_attempts_consume_shared_write_call_quota(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    adapter = _mcp(engine, frozenset({"facet-catalog-write"}))
    arguments = {
        "dto_version": 1,
        "name": "Quota retry",
        "expected_version": 0,
        "operation_id": "quota-retry",
    }
    original = adapter.call_tool("brain_facet_dimension_create", arguments)
    for _ in range(499):
        assert adapter.call_tool("brain_facet_dimension_create", arguments) == original
    with pytest.raises(McpCallError, match="limit_exceeded"):
        adapter.call_tool("brain_facet_dimension_create", arguments)
    assert _facet_row_counts(root) == (1, 1, 1)


def test_f15_real_mcp_reserves_wrapped_receipt_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    adapter = _mcp(engine, frozenset({"facet-catalog-write"}))
    with pytest.raises(McpCallError, match="response_too_large|limit_exceeded"):
        adapter.call_tool(
            "brain_facet_dimension_create",
            {
                "dto_version": 1,
                "name": "Too small",
                "expected_version": 0,
                "operation_id": "small-transport",
            },
            maximum_response_bytes=200,
        )
    assert _facet_row_counts(root) == (0, 0, 0)


def _owner_facet_review(
    tmp_path: Path,
    *,
    tier: PrivacyTier = PrivacyTier.WORK,
) -> tuple[FacetHarness, dict[str, Any], str, str]:
    brain = FacetHarness(tmp_path)
    source_space = brain.engine.inbox.spaces()[0].space_id
    destination = brain.engine.inbox.create_space(
        "Explicit review destination", delivery_id="mcp-review-destination"
    )
    capture = brain.engine.capture.accept(
        TextPayload("SYNTHETIC_PRIVATE_REVIEW_SOURCE"),
        delivery_id="mcp-review-source",
        space_id=source_space,
        privacy_tier=tier,
    )
    assert isinstance(capture, CaptureReceipt)
    value = brain.value(brain.dimension())
    brain.patch(capture.capture_id, add=(value,))
    query = brain.write(
        "facet.query.create",
        name="MCP frozen sources",
        expected_version=0,
        selector={"all_value_ids": [value], "record_types": ["source"]},
    )["subject_id"]
    proposal = brain.write(
        "review.propose_from_facet_query",
        query_id=query,
        expected_query_version=1,
        target_page_id=None,
        expected_page_revision_id=None,
        destination_space_id=destination.space_id,
        title="Synthetic granted review",
        markdown="SYNTHETIC_PRIVATE_REVIEW_DRAFT",
    )
    return brain, proposal, source_space, destination.space_id


def _launch_scoped_review_mcp(
    brain: FacetHarness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    spaces: frozenset[str],
    exercise: Callable[[Any, EffectiveAuthority], None],
) -> None:
    """Exercise production CLI MCP composition with a trusted scoped session fixture."""
    from open_brain.services import mcp_protocol
    from open_brain.services.local_operations import current_brain_identity

    exercise_errors: list[Exception] = []
    authority_root = brain.root.parent / "review-authority"
    authority_root.mkdir(mode=0o700)
    policy_path = authority_root / "session.json"
    brain_id, issuer_epoch = current_brain_identity(brain.engine.tasks)
    policy_path.write_text(json.dumps({
        "policy_version": "launcher-policy.v1",
        "principal_id": "synthetic-review-agent",
        "session_id": "synthetic-review-session",
        "capabilities": ["review-read", "review-decide"],
        "space_ids": sorted(spaces),
        "allowed_read_tiers": ["work"],
        "allowed_capture_tiers": [],
        "egress_mode": "owner_local",
        "provider_id": None,
        "consent_id": None,
        "authorization_generation": 0,
        "brain_id": brain_id,
        "issuer_epoch": issuer_epoch,
    }))

    def serve(adapter: Any, **_transport: Any) -> None:
        try:
            trusted = adapter.authority
            assert adapter.facets.authority is trusted
            assert trusted.owner is False
            assert trusted.space_ids == spaces
            assert trusted.allowed_read_tiers == frozenset({PrivacyTier.WORK})
            assert "review-propose" not in trusted.capabilities
            exercise(adapter, trusted)
        except Exception as error:
            # Preserve client-side test failures outside the launcher's safe
            # error wrapper, after its real runtime-session cleanup completes.
            exercise_errors.append(error)

    # Only the local byte transport is replaced. Dispatcher, adapters, authority
    # construction, review service and engine handlers are the real producers.
    monkeypatch.setattr(mcp_protocol, "serve_stdio_mcp", serve)
    result = run_cli(
        ("mcp", "--allow-review-read", "--allow-review-decide", "--session-policy",
         str(policy_path), "--data-dir", str(brain.root)),
        filesystem_type_probe=_filesystem,
    )
    if exercise_errors:
        raise exercise_errors[0]
    assert result == 0


def test_f15_nonowner_review_grants_do_not_admit_owner_review_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain, proposal, source_space, destination = _owner_facet_review(tmp_path)

    def exercise(adapter: Any, _trusted: EffectiveAuthority) -> None:
        tools = {tool["name"] for tool in adapter.list_tools()}
        assert not {"brain_review_show", "brain_review_approve"} & tools
        assert "brain_review_propose_from_facet_query" not in tools
        with pytest.raises(McpCallError, match="unknown tool"):
            adapter.call_tool("brain_review_show", {"proposal_id": proposal["proposal_id"]})
        with sqlite3.connect(brain.root / ".open-brain/state/phase1.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM facet_page_queries").fetchone()[0] == 0

    _launch_scoped_review_mcp(
        brain, monkeypatch, spaces=frozenset({source_space, destination}), exercise=exercise
    )


@pytest.mark.parametrize("denied", ["private_source", "source_scope", "destination_scope"])
def test_f15_granted_mcp_review_cannot_use_owner_bypass_for_denied_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    denied: str,
) -> None:
    tier = PrivacyTier.SECRET if denied == "private_source" else PrivacyTier.WORK
    brain, proposal, source_space, destination = _owner_facet_review(tmp_path, tier=tier)
    owner_token = brain.engine.review.show(proposal["proposal_id"]).review_digest
    spaces = (
        {source_space, destination}
        if denied == "private_source"
        else {destination}
        if denied == "source_scope"
        else {source_space}
    )

    def exercise(adapter: Any, _trusted: EffectiveAuthority) -> None:
        assert not {"brain_review_show", "brain_review_approve"} & {
            tool["name"] for tool in adapter.list_tools()
        }
        for tool, arguments in (
            ("brain_review_show", {"proposal_id": proposal["proposal_id"]}),
            (
                "brain_review_approve",
                {
                    "proposal_id": proposal["proposal_id"],
                    "review_token": owner_token,
                    "idempotency_key": "denied-review",
                },
            ),
        ):
            with pytest.raises(McpCallError, match="unknown tool") as error:
                adapter.call_tool(tool, arguments)
            assert "SYNTHETIC_PRIVATE_REVIEW" not in str(error.value)
            assert proposal["page_id"] not in str(error.value)
        with sqlite3.connect(brain.root / ".open-brain/state/phase1.sqlite3") as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM decisions WHERE proposal_id=?", (proposal["proposal_id"],)
                ).fetchone()[0]
                == 0
            )
            assert connection.execute("SELECT count(*) FROM facet_page_queries").fetchone()[0] == 0

    _launch_scoped_review_mcp(brain, monkeypatch, spaces=frozenset(spaces), exercise=exercise)
    output = capsys.readouterr()
    assert "SYNTHETIC_PRIVATE_REVIEW" not in output.out + output.err


@pytest.mark.parametrize(
    "length,code", [(66000, "invalid_arguments"), (5000, "response_too_large")]
)
def test_f15_mcp_request_id_bytes_and_wrapped_receipt_cap_refuse_before_effects(
    tmp_path: Path,
    length: int,
    code: str,
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    adapter = _mcp(engine, frozenset({"facet-catalog-write"}))
    with pytest.raises(McpCallError, match=f"^{code}$"):
        adapter.call_tool(
            "brain_facet_dimension_create",
            {
                "dto_version": 1,
                "name": "Synthetic transport bound",
                "expected_version": 0,
                "operation_id": "transport-request-id",
            },
            request_id="x" * length,
        )
    assert _facet_row_counts(root) == (0, 0, 0)


def _stdio_facet_request(adapter: Any, raw_call: bytes) -> tuple[dict[str, Any], bytes]:
    initialize = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {},
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    sink = io.BytesIO()
    serve_stdio_mcp(
        adapter,
        input_stream=io.BytesIO(initialize + raw_call),
        output_stream=sink,
        maximum_message_bytes=1024 * 1024,
    )
    lines = sink.getvalue().splitlines(keepends=True)
    assert len(lines) == 2
    assert "result" in json.loads(lines[0])
    return cast(dict[str, Any], json.loads(lines[1])), lines[1].removesuffix(b"\n")


@pytest.mark.parametrize("padding", ["metadata", "whitespace"])
def test_f15_actual_stdio_counts_discarded_metadata_and_raw_whitespace_before_effects(
    tmp_path: Path,
    padding: str,
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    adapter = _mcp(engine, frozenset({"facet-catalog-write"}))
    params: dict[str, Any] = {
        "name": "brain_facet_dimension_create",
        "arguments": {
            "dto_version": 1,
            "name": "Raw transport bound",
            "expected_version": 0,
            "operation_id": "raw-transport-bound",
        },
    }
    if padding == "metadata":
        params["_meta"] = {"synthetic_padding": "x" * 66000}
    raw = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}, separators=(",", ":")
    ).encode("utf-8")
    if padding == "whitespace":
        raw += b" " * 66000
    raw += b"\n"
    assert 65536 < len(raw) < 1024 * 1024
    response, _ = _stdio_facet_request(adapter, raw)
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "invalid_arguments"
    assert _facet_row_counts(root) == (0, 0, 0)


def test_f15_actual_stdio_admits_ordinary_utf8_request_and_bounded_receipt(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    adapter = _mcp(engine, frozenset({"facet-catalog-write"}))
    name = "研究🙂 café"
    raw = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "🙂" * 100,
                "method": "tools/call",
                "params": {
                    "name": "brain_facet_dimension_create",
                    "arguments": {
                        "dto_version": 1,
                        "name": name,
                        "expected_version": 0,
                        "operation_id": "valid-utf8",
                    },
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    assert len(raw) < 65536
    response, encoded = _stdio_facet_request(adapter, raw)
    assert response["result"]["structuredContent"]["status"] == "applied"
    assert len(encoded) <= 4096
    assert _facet_row_counts(root) == (1, 1, 1)
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute("SELECT name FROM facet_dimensions").fetchone()[0] == name


def test_f15_unicode_history_pages_fit_full_encoded_stdio_envelope_without_lost_events(
    tmp_path: Path,
) -> None:
    brain = FacetHarness(tmp_path)
    created = brain.engine.facets.dimension_create(
        DimensionCreateRequest(
            operation_id="unicode-history-0",
            expected_version=0,
            name="🙂" * 112 + "-000",
        ),
        authority=brain.authority,
    ).to_wire()
    dimension = cast(str, created["subject_id"])
    for index in range(1, 100):
        brain.engine.facets.dimension_rename(
            DimensionRenameRequest(
                dimension_id=dimension,
                expected_version=index,
                operation_id=f"unicode-history-{index}",
                name="🙂" * 112 + f"-{index:03d}",
            ),
            authority=brain.authority,
        )
    adapter = _mcp(brain.engine, frozenset({"facet-read", "history-read"}))
    sequences: list[int] = []
    cursor = None
    page_count = 0
    for _ in range(100):
        raw = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "brain_facet_history_list",
                        "arguments": {
                            "dto_version": 1,
                            "subject": {"kind": "dimension", "id": dimension},
                            "limit": 100,
                            "cursor": cursor,
                        },
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        response, encoded = _stdio_facet_request(adapter, raw)
        assert not response["result"].get("isError", False), response
        assert len(encoded) <= 250000
        page = response["result"]["structuredContent"]
        assert page["items"], "A complete synthetic event fits; continuation must make progress"
        sequences.extend(item["sequence"] for item in page["items"])
        page_count += 1
        cursor = page["next_cursor"]
        if page["complete"]:
            assert cursor is None
            break
        assert cursor is not None
    assert page_count > 1, "The full ASCII-escaped double representation requires pagination"
    assert sequences == list(range(1, 101))


@pytest.mark.parametrize(
    "code",
    [
        "name_conflict",
        "version_conflict",
        "facet_archived",
        "query_archived",
        "limit_exceeded",
        "projection_conflict",
    ],
)
def test_f15_stdio_preserves_closed_facet_error_codes_from_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    """Transport fault-injection proof; does not claim real domain-error production."""
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    adapter = _mcp(engine, frozenset({"facet-catalog-write"}))

    def fail_at_adapter(_self: Any, *_arguments: Any, **_context: Any) -> dict[str, object]:
        raise FacetError(code)

    # Inject only a closed engine failure at the adapter boundary. In particular,
    # projection_conflict has no public vault MCP operation in this increment.
    monkeypatch.setattr(type(adapter.facets), "invoke_mcp", fail_at_adapter)
    raw = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "brain_facet_dimension_create",
                    "arguments": {
                        "dto_version": 1,
                        "name": "Synthetic injected failure",
                        "expected_version": 0,
                        "operation_id": "error-code-preservation",
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    response, _ = _stdio_facet_request(adapter, raw)
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == code
    assert _facet_row_counts(root) == (0, 0, 0)
