"""Independent real CLI/engine/Portable pipeline; no fake task installation."""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, SourceRouteRequest
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.mcp_protocol import McpCallError
from packages.engine.tests.contract.test_facets_acceptance import public_revision


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
    assert manifest["schema_version"] == 6
    restored_root = tmp_path / "restored"
    receipt = engine.portability.import_clean(
        archive, restored_root, import_id="import_00000000-0000-4000-8000-000000000001"
    )
    assert receipt.schema_version == 6
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
