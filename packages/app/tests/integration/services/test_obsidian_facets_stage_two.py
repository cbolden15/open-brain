"""Stage 2 independent acceptance against live plugin stdio and real engine state.

All roots are synthetic. Restricted authority tests use the real byte-stream server
in-process; owner tests launch the source executable. No domain producer is mocked.
"""

from __future__ import annotations

import io
import json
import os
import select
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.contracts import CaptureAction, CaptureReceipt, TextPayload
from open_brain_engine.engine.facet_contracts import FacetError
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.local_data import select_local_root
from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.facets import MAX_SESSION_BYTES, FacetAppAdapter, FacetSessionBudget
from open_brain.services.plugin_bridge import serve_plugin_stdio
from open_brain.services.plugin_facet_contracts import response_size

ROOT = Path(__file__).resolve().parents[5]
EXECUTABLE = Path(sys.executable).parent / "open-brain"


def frame(operation: str, arguments: dict[str, Any], *, ascii_: bool = False) -> bytes:
    return (
        json.dumps(
            {
                "protocol": "open-brain-client",
                "protocol_version": 1,
                "request_id": f"plugin_{uuid.uuid4()}",
                "operation": operation,
                "arguments": arguments,
            },
            ensure_ascii=ascii_,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


class LivePlugin:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.process = subprocess.Popen(
            [str(EXECUTABLE), "plugin", "--data-dir", str(root)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def raw(self, payload: bytes) -> tuple[dict[str, Any], bytes]:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(payload)
        self.process.stdin.flush()
        ready, _, _ = select.select([self.process.stdout], [], [], 15)
        assert ready, "Actual plugin child did not produce a response within 15 seconds"
        raw = self.process.stdout.readline()
        assert raw, f"Plugin child exited: {self.process.poll()}"
        return json.loads(raw), raw

    def call(self, operation: str, **arguments: Any) -> dict[str, Any]:
        if (
            operation.startswith(("facet.", "publication.", "space.", "inbox."))
            or operation == "review.propose_from_facet_query"
        ):
            arguments.setdefault("dto_version", 1)
        response, _ = self.raw(frame(operation, arguments))
        assert response["ok"] is True, response
        return cast(dict[str, Any], response["result"])

    def error(self, operation: str, code: str | tuple[str, ...], **arguments: Any) -> None:
        if (
            operation.startswith(("facet.", "publication.", "space.", "inbox."))
            or operation == "review.propose_from_facet_query"
        ):
            arguments.setdefault("dto_version", 1)
        response, _ = self.raw(frame(operation, arguments))
        assert response["ok"] is False, response
        assert set(response["error"]) == {"code"}
        assert response["error"]["code"] in ((code,) if isinstance(code, str) else code)

    def write(self, operation: str, **arguments: Any) -> dict[str, Any]:
        arguments.setdefault("operation_id", f"synthetic-{uuid.uuid4()}")
        return self.call(operation, **arguments)

    def catalog(self) -> tuple[str, str]:
        dimension = self.write("facet.dimension.create", name="Project", expected_version=0)
        value = self.write(
            "facet.value.create",
            dimension_id=dimension["subject_id"],
            expected_dimension_version=1,
            expected_version=0,
            name="Apollo",
        )
        return dimension["subject_id"], value["subject_id"]

    def patch(
        self,
        record: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        version: int = 0,
        revision: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        arguments = dict(
            record_id=record,
            expected_revision_id=revision or record,
            expected_version=version,
            add_value_ids=add or [],
            remove_value_ids=remove or [],
        )
        if operation_id is not None:
            arguments["operation_id"] = operation_id
        return self.write("facet.assignment.patch", **arguments)

    def query(self, values: list[str], **filters: Any) -> str:
        return str(
            self.write(
                "facet.query.create",
                name=f"Synthetic {uuid.uuid4()}",
                expected_version=0,
                selector={"all_value_ids": values, **filters},
            )["subject_id"]
        )

    def propose(self, query: str, destination: str, **arguments: Any) -> dict[str, Any]:
        return self.write(
            "review.propose_from_facet_query",
            query_id=query,
            expected_query_version=1,
            target_page_id=None,
            expected_page_revision_id=None,
            destination_space_id=destination,
            title="Synthetic publication",
            markdown="Owner-written café 🙂.\n",
            **arguments,
        )

    def approve(self, proposal: dict[str, Any], *, reject: bool = False) -> dict[str, Any]:
        shown = self.call("publication.show", proposal_id=proposal["proposal_id"])
        return self.call(
            "publication.reject" if reject else "publication.approve",
            proposal_id=proposal["proposal_id"],
            review_token=shown["review_token"],
            idempotency_key=f"synthetic-decision-{uuid.uuid4()}",
        )

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[tuple[BrainEngine, LivePlugin]]:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "synthetic-brain"))
    engine.inbox.create_space("Synthetic default space", delivery_id="synthetic-default-space")
    plugin = LivePlugin(engine.profile.root)
    try:
        yield engine, plugin
    finally:
        plugin.close()


def source(engine: BrainEngine, serial: int, *, space: str | None = None) -> str:
    receipt = engine.capture.accept(
        TextPayload(f"Synthetic source {serial}"),
        delivery_id=f"synthetic-source-{serial}",
        space_id=space or engine.inbox.spaces()[0].space_id,
        privacy_tier=PrivacyTier.WORK,
    )
    assert isinstance(receipt, CaptureReceipt)
    return str(receipt.capture_id)


def counts(root: Path) -> tuple[int, int, int]:
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
        result = tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("facet_dimensions", "facet_events", "facet_operations")
        )
        assert len(result) == 3
        return result[0], result[1], result[2]


def canonical(engine: BrainEngine, plugin: LivePlugin) -> tuple[str, str, Path, str]:
    engine.capture.accept(
        TextPayload("Synthetic managed canonical body"),
        delivery_id="synthetic-canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    page = engine.retrieval.search("Synthetic managed canonical body", record_type="canonical")[
        0
    ].result_id
    _, value = plugin.catalog()
    revision = plugin.call("facet.assignment.get", record_id=page)["item"]["revision_id"]
    plugin.patch(page, add=[value], revision=revision)
    workspace = plugin.call("workspace.setup")
    plugin.call("workspace.refresh")
    vault = Path(workspace["vault_path"])
    file = next(vault.rglob(f"{page}.md"))
    return workspace["workspace_id"], page, file, value


def test_o01_real_scoped_discovery_and_direct_calls_cannot_assert_owner(tmp_path: Path) -> None:
    root = tmp_path / "synthetic-scoped"
    BrainEngine.open(compile_single_user_local(root))
    authority = EffectiveAuthority(
        "synthetic-agent", "synthetic-session", frozenset({"facet-read"}), frozenset(), owner=False
    )
    payload = (
        frame("system.handshake", {})
        + frame(
            "facet.dimension.create",
            {
                "dto_version": 1,
                "expected_version": 0,
                "name": "Denied",
                "operation_id": "synthetic-denied",
                "owner": True,
            },
        )
        + frame(
            "workspace.record.resolve",
            {
                "dto_version": 1,
                "workspace_id": f"workspace_{uuid.uuid4()}",
                "relative_path": "x.md",
            },
        )
        + frame("facet.dimension.list", {"dto_version": 1})
    )
    output = io.BytesIO()
    assert (
        serve_plugin_stdio(
            select_local_root(data_dir=str(root), environment=os.environ),
            authority=authority,
            input_stream=io.BytesIO(payload),
            output_stream=output,
        )
        == 0
    )
    responses = [json.loads(raw) for raw in output.getvalue().splitlines()]
    handshake = responses[0]["result"]
    assert "facet.dimension.list" in handshake["operations"]
    assert "facet.dimension.create" not in handshake["operations"]
    assert "workspace.record.resolve" not in handshake["operations"]
    assert {item["operation"] for item in handshake["facet_capabilities"]["operations"]} <= set(
        handshake["operations"]
    )
    assert responses[1]["error"] == {"code": "unsupported_capability"}
    assert responses[2]["error"] == {"code": "unsupported_capability"}
    assert responses[3]["result"]["items"] == []
    assert counts(root) == (0, 0, 0)


def test_o03_o04_o05_vocabulary_assignment_and_lost_response_retry(runtime: Any) -> None:
    engine, plugin = runtime
    record = source(engine, 1)
    dimension, value = plugin.catalog()
    first = plugin.patch(record, add=[value], operation_id="synthetic-lost-response")
    # The committed response is deliberately discarded by the consumer. A new
    # actual child retries the exact request and must return the original receipt.
    plugin.close()
    plugin.__init__(plugin.root)
    assert plugin.patch(record, add=[value], operation_id="synthetic-lost-response") == first
    plugin.write(
        "facet.dimension.rename", dimension_id=dimension, expected_version=1, name="Renamed project"
    )
    plugin.write("facet.value.rename", value_id=value, expected_version=1, name="Renamed value")
    current = plugin.call("facet.assignment.get", record_id=record)["item"]
    assert current["memberships"] == [{"value_id": value, "membership_version": 1}]
    plugin.error(
        "facet.assignment.patch",
        "version_conflict",
        operation_id="synthetic-stale",
        record_id=record,
        expected_revision_id=record,
        expected_version=0,
        add_value_ids=[],
        remove_value_ids=[value],
    )
    assert plugin.call("facet.assignment.get", record_id=record)["item"] == current
    plugin.write("facet.value.archive", value_id=value, expected_version=2)
    plugin.write("facet.dimension.archive", dimension_id=dimension, expected_version=2)
    assert plugin.call("facet.dimension.list", include_archived=True)["items"][0]["archived"]
    other = source(engine, 2)
    plugin.error(
        "facet.assignment.patch",
        "facet_archived",
        operation_id="synthetic-archived",
        record_id=other,
        expected_revision_id=other,
        expected_version=0,
        add_value_ids=[value],
        remove_value_ids=[],
    )
    history = plugin.call("facet.history.list", subject=current["subject"])
    assert len(history["items"]) == 1


def test_o01_real_scoped_query_filters_cannot_broaden_identity_or_review(runtime: Any) -> None:
    engine, plugin = runtime
    _, value = plugin.catalog()
    allowed_space = engine.inbox.spaces()[0].space_id
    denied_space = engine.inbox.create_space(
        "Synthetic denied space", delivery_id="synthetic-denied-space"
    )
    allowed = source(engine, 1)
    denied = source(engine, 2, space=denied_space.space_id)
    for record in (allowed, denied):
        plugin.patch(record, add=[value])
    denied_subject = plugin.call("facet.assignment.get", record_id=denied)["item"]["subject"]
    plugin.close()
    authority = EffectiveAuthority(
        "synthetic-reader",
        "synthetic-scoped-session",
        frozenset({"facet-read", "search"}),
        frozenset({allowed_space}),
    )
    raw = frame(
        "facet.query.page",
        {
            "dto_version": 1,
            "selector": {
                "all_value_ids": [value],
                "space_ids": [allowed_space, denied_space.space_id],
            },
        },
    )
    raw += frame("facet.assignment.get", {"dto_version": 1, "record_id": denied})
    raw += frame("facet.history.list", {"dto_version": 1, "subject": denied_subject})
    raw += frame("review.propose_from_facet_query", {"dto_version": 1, "owner": True})
    output = io.BytesIO()
    assert (
        serve_plugin_stdio(
            select_local_root(data_dir=str(plugin.root), environment=os.environ),
            authority=authority,
            input_stream=io.BytesIO(raw),
            output_stream=output,
        )
        == 0
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item["record"]["record_id"] for item in responses[0]["result"]["items"]] == [allowed]
    assert responses[1]["error"] == {"code": "not_found"}
    assert responses[2]["error"] == {"code": "unsupported_capability"}
    assert responses[3]["error"] == {"code": "unsupported_capability"}
    assert denied not in output.getvalue().decode()
    assert denied_subject["id"] not in output.getvalue().decode()


def test_o06_match_all_filters_pagination_and_stale_cursor(runtime: Any) -> None:
    engine, plugin = runtime
    dimension, first = plugin.catalog()
    second = plugin.write(
        "facet.value.create",
        dimension_id=dimension,
        expected_dimension_version=1,
        expected_version=0,
        name="Research",
    )["subject_id"]
    records = [source(engine, i) for i in range(4)]
    for record in records:
        plugin.patch(record, add=[first, second])
    excluded = source(engine, 5)
    plugin.patch(excluded, add=[first])
    selector = {
        "all_value_ids": sorted([first, second]),
        "record_types": ["source"],
        "space_ids": [engine.inbox.spaces()[0].space_id],
        "payload_families": ["text"],
    }
    query = plugin.query(
        [first, second], **{key: val for key, val in selector.items() if key != "all_value_ids"}
    )
    assert plugin.call("facet.query.get", query_id=query)["item"]["selector"] == selector
    seen: list[str] = []
    cursor = None
    while True:
        page = plugin.call(
            "facet.query.page", query_id=query, expected_query_version=1, cursor=cursor, limit=2
        )
        seen.extend(item["record"]["record_id"] for item in page["items"])
        if page["complete"]:
            break
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 4 and set(seen) == set(records)
    adhoc = plugin.call("facet.query.page", selector=selector)
    assert {item["record"]["record_id"] for item in adhoc["items"]} == set(records)
    old = plugin.call("facet.query.page", query_id=query, expected_query_version=1, limit=1)
    plugin.write(
        "facet.query.update",
        query_id=query,
        expected_version=1,
        name="Changed",
        selector={"all_value_ids": [first], "record_types": ["source"]},
    )
    plugin.error(
        "facet.query.page",
        "version_conflict",
        query_id=query,
        expected_query_version=1,
        cursor=old["next_cursor"],
        limit=1,
    )
    plugin.error(
        "facet.query.page",
        "cursor_invalid",
        query_id=query,
        expected_query_version=2,
        cursor=old["next_cursor"],
        limit=1,
    )
    fresh = plugin.call("facet.query.page", query_id=query, expected_query_version=2, limit=1)
    plugin.patch(records[0], remove=[first], version=1)
    plugin.error(
        "facet.query.page",
        "cursor_stale",
        query_id=query,
        expected_query_version=2,
        cursor=fresh["next_cursor"],
        limit=1,
    )


def test_o07_o08_crossspace_publication_update_and_evidence_conflict(runtime: Any) -> None:
    engine, plugin = runtime
    dimension, value = plugin.catalog()
    other = engine.inbox.create_space("Synthetic other space", delivery_id="synthetic-other")
    destination = engine.inbox.create_space("Synthetic destination", delivery_id="synthetic-dest")
    records = [source(engine, 1), source(engine, 2, space=other.space_id)]
    for record in records:
        plugin.patch(record, add=[value])
    query = plugin.query([value], record_types=["source"])
    proposal = plugin.propose(query, destination.space_id)
    shown = plugin.call("publication.show", proposal_id=proposal["proposal_id"])
    assert set(shown["selected_capture_ids"]) == set(records)
    assert shown["facet_selection"]["destination_space_id"] == destination.space_id
    before = plugin.call("facet.query.page", query_id=query, expected_query_version=1)
    plugin.approve(proposal)
    assert plugin.call("facet.query.page", query_id=query, expected_query_version=1) == before
    page = proposal["page_id"]
    state = plugin.call("facet.assignment.get", record_id=page)["item"]
    assert state["memberships"] == []
    assert plugin.call("facet.query.attachment.get", page_id=page)["item"]["query_id"] == query
    plugin.patch(page, add=[value], revision=state["revision_id"])
    narrowed = plugin.write(
        "facet.value.create",
        dimension_id=dimension,
        expected_dimension_version=1,
        expected_version=0,
        name="Synthetic narrowed selection",
    )["subject_id"]
    plugin.patch(records[1], add=[narrowed], version=1)
    plugin.write(
        "facet.query.update",
        query_id=query,
        expected_version=1,
        name="Synthetic narrowed query",
        selector={"all_value_ids": [narrowed], "record_types": ["source"]},
    )
    update = plugin.write(
        "review.propose_from_facet_query",
        query_id=query,
        expected_query_version=2,
        target_page_id=page,
        expected_page_revision_id=state["revision_id"],
        destination_space_id=None,
        title="Synthetic update",
        markdown="Owner-written updated body",
    )
    inspected = plugin.call("publication.show", proposal_id=update["proposal_id"])
    assert inspected["space_id"] == destination.space_id
    assert set(inspected["capture_ids"]) == set(records)
    assert inspected["selected_capture_ids"] == [records[1]]
    plugin.approve(update)
    current_page = plugin.call("facet.assignment.get", record_id=page)["item"]
    assert current_page["revision_id"] != state["revision_id"]
    assert current_page["memberships"] == [{"value_id": value, "membership_version": 1}]
    stale = plugin.write(
        "review.propose_from_facet_query",
        query_id=query,
        expected_query_version=2,
        target_page_id=page,
        expected_page_revision_id=current_page["revision_id"],
        destination_space_id=None,
        title="Synthetic stale update",
        markdown="Owner-written stale draft",
    )
    inspected_stale = plugin.call("publication.show", proposal_id=stale["proposal_id"])
    plugin.patch(records[1], remove=[narrowed], version=2)
    plugin.error(
        "publication.approve",
        "review_conflict",
        proposal_id=stale["proposal_id"],
        review_token=inspected_stale["review_token"],
        idempotency_key="synthetic-drift-approve",
    )
    assert (
        plugin.call("facet.assignment.get", record_id=page)["item"]["revision_id"]
        == current_page["revision_id"]
    )


@pytest.mark.parametrize("count,code", [(0, "invalid_arguments"), (33, "limit_exceeded")])
def test_o07_publication_complete_selection_bounds(runtime: Any, count: int, code: str) -> None:
    engine, plugin = runtime
    _, value = plugin.catalog()
    for serial in range(count):
        plugin.patch(source(engine, serial), add=[value])
    query = plugin.query([value], record_types=["source"])
    plugin.error(
        "review.propose_from_facet_query",
        code,
        operation_id="synthetic-bounds",
        query_id=query,
        expected_query_version=1,
        target_page_id=None,
        expected_page_revision_id=None,
        destination_space_id=engine.inbox.spaces()[0].space_id,
        title="Synthetic bounds",
        markdown="Synthetic draft",
    )
    assert plugin.call("publication.list")["proposals"] == []


def test_o09_rejection_never_publishes(runtime: Any) -> None:
    engine, plugin = runtime
    _, value = plugin.catalog()
    plugin.patch(source(engine, 1), add=[value])
    proposal = plugin.propose(plugin.query([value]), engine.inbox.spaces()[0].space_id)
    plugin.approve(proposal, reject=True)
    plugin.error("facet.assignment.get", "not_found", record_id=proposal["page_id"])


def test_o02_observed_custody_resolution_is_readonly_and_source_mapping_refused(
    runtime: Any,
) -> None:
    engine, plugin = runtime
    workspace, page, file, value = canonical(engine, plugin)
    relative = file.relative_to(Path(plugin.call("workspace.status")["vault_path"])).as_posix()
    args = dict(dto_version=1, workspace_id=workspace, relative_path=relative)
    plugin.error("workspace.record.resolve", "not_found", **args)
    plugin.call("workspace.observe")
    resolved = plugin.call("workspace.record.resolve", **args)
    assert resolved["record_id"] == page and resolved["subject"] == {
        "kind": "canonical",
        "id": page,
    }
    before = file.read_bytes()
    vault = Path(plugin.call("workspace.status")["vault_path"])
    record = source(engine, 9)
    unregistered = vault / "synthetic-source.md"
    unregistered.write_text(
        f"---\nrecord_id: {record}\nfacets:\n  Owner: [local]\n---\nSynthetic source file\n"
    )
    unregistered_bytes = unregistered.read_bytes()
    plugin.error(
        "workspace.record.resolve", "not_found", **{**args, "relative_path": unregistered.name}
    )
    plugin.patch(record, add=[value])
    plugin.call("workspace.observe")
    assert unregistered.read_bytes() == unregistered_bytes
    assert plugin.call("facet.assignment.get", record_id=record)["item"]["version"] == 1
    indexes = [
        path for path in vault.rglob("*.md") if "facets" in {part.casefold() for part in path.parts}
    ]
    assert indexes
    plugin.error(
        "workspace.record.resolve",
        "not_found",
        **{**args, "relative_path": indexes[0].relative_to(vault).as_posix()},
    )
    for path in ("../escape.md", "/absolute.md", "unknown.md", "sources/synthetic.md"):
        plugin.error(
            "workspace.record.resolve",
            ("not_found", "invalid_arguments"),
            **{**args, "relative_path": path},
        )
    plugin.error(
        "workspace.record.resolve",
        "not_found",
        **{**args, "workspace_id": f"workspace_{uuid.uuid4()}"},
    )
    parsed = parse_markdown(before)
    fields = {**parsed.fields, "page_id": f"page_{uuid.uuid4()}"}
    file.write_text(render_markdown(fields=fields, body=parsed.body))
    plugin.error("workspace.record.resolve", "not_found", **args)
    assert file.read_bytes() != before
    file.write_bytes(before)
    replacement = file.with_suffix(".replacement")
    replacement.write_bytes(before)
    replacement.replace(file)
    plugin.error("workspace.record.resolve", "not_found", **args)
    plugin.call("workspace.observe")
    assert plugin.call("workspace.record.resolve", **args)["record_id"] == page
    outside = plugin.root.parent / "synthetic-outside.md"
    outside.write_bytes(before)
    file.unlink()
    file.symlink_to(outside)
    plugin.error("workspace.record.resolve", "not_found", **args)
    assert outside.read_bytes() == before
    vault.rename(vault.with_name("Synthetic displaced original vault"))
    replacement_file = vault / relative
    replacement_file.parent.mkdir(parents=True, mode=0o700)
    replacement_file.write_bytes(before)
    plugin.error("workspace.record.resolve", "not_found", **args)
    assert plugin.call("facet.assignment.get", record_id=page)["item"]["version"] == 1


def test_o10_o11_preview_missing_subtree_retry_and_body_index_preservation(runtime: Any) -> None:
    engine, plugin = runtime
    workspace, page, file, value = canonical(engine, plugin)
    parsed = parse_markdown(file.read_bytes())
    fields = {key: val for key, val in parsed.fields.items() if key != "facets"}
    file.write_text(render_markdown(fields=fields, body=parsed.body))
    plugin.call("workspace.observe")
    assert plugin.call("facet.assignment.get", record_id=page)["item"]["version"] == 1
    preview = plugin.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    assert preview["conflicts"] == [] and preview["remove_value_ids"] == [value]
    vault = Path(plugin.call("workspace.status")["vault_path"])
    indexes = [
        path for path in vault.rglob("*.md") if path != file and "facet" in str(path).lower()
    ]
    assert indexes, "Expected a real generated facet index"
    index = indexes[0]
    index.write_bytes(index.read_bytes() + b"Synthetic owner index edit\n")
    edited_index = index.read_bytes()
    original_mode = file.parent.stat().st_mode & 0o777
    file.parent.chmod(0o500)
    try:
        first = plugin.write(
            "facet.vault.apply",
            preview_id=preview["preview_id"],
            operation_id="synthetic-preview-retry",
        )
    finally:
        file.parent.chmod(original_mode)
    assert first["receipt"]["version"] == 2
    assert first["projection_status"] == "refresh_required"
    plugin.close()
    plugin.__init__(plugin.root)
    assert (
        plugin.write(
            "facet.vault.apply",
            preview_id=preview["preview_id"],
            operation_id="synthetic-preview-retry",
        )["receipt"]
        == first["receipt"]
    )
    assert index.read_bytes() == edited_index
    assert parse_markdown(file.read_bytes()).body == parsed.body
    file.write_text(
        render_markdown(
            fields={**fields, "facets": {"Project": ["Unknown"]}},
            body=parsed.body + "Synthetic body edit\n",
        )
    )
    edited = file.read_bytes()
    conflict = plugin.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    assert conflict["conflicts"]
    plugin.error(
        "facet.vault.apply",
        ("projection_conflict", "preview_stale"),
        preview_id=conflict["preview_id"],
        operation_id="synthetic-denied-apply",
    )
    plugin.error("workspace.refresh", "projection_conflict")
    assert file.read_bytes() == edited and index.read_bytes() == edited_index


def test_o09_real_committed_publication_recovers_refresh_without_republishing(runtime: Any) -> None:
    engine, plugin = runtime
    _workspace, _page, file, value = canonical(engine, plugin)
    vault = Path(plugin.call("workspace.status")["vault_path"])
    index = next(path for path in vault.rglob("*.md") if "Facets" in path.parts)
    original_index = index.read_bytes()
    index.write_bytes(original_index + b"Synthetic owner index edit\n")
    plugin.patch(source(engine, 1), add=[value])
    proposal = plugin.propose(
        plugin.query([value], record_types=["source"]), engine.inbox.spaces()[0].space_id
    )
    committed = plugin.approve(proposal)
    assert committed["status"] == "approved"
    for _ in range(2):
        plugin.error("workspace.refresh", "projection_conflict")
    shown = plugin.call("publication.show", proposal_id=proposal["proposal_id"])
    assert shown["proposal_status"] == "approved"
    assert len(plugin.call("publication.list")["proposals"]) == 1
    index.write_bytes(original_index)
    plugin.call("workspace.refresh")
    assert list(vault.rglob(f"{proposal['page_id']}.md"))
    assert file.exists()
    assert len(plugin.call("publication.list")["proposals"]) == 1


@pytest.mark.parametrize("kind", ["oversize", "duplicate", "fraction", "unknown", "assert_owner"])
def test_o12_server_raw_wire_refuses_before_effects(runtime: Any, kind: str) -> None:
    engine, plugin = runtime
    args = {
        "dto_version": 1,
        "name": "研究🙂 café",
        "expected_version": 0,
        "operation_id": "synthetic-raw-wire",
    }
    raw = frame("facet.dimension.create", args, ascii_=True)
    if kind == "oversize":
        raw = raw[:-1] + b" " * 65536 + b"\n"
    elif kind == "duplicate":
        raw = raw.replace(b'"expected_version":0', b'"expected_version":0,"expected_version":0')
    elif kind == "fraction":
        raw = raw.replace(b'"dto_version":1', b'"dto_version":1.0')
    elif kind == "unknown":
        raw = raw.replace(b"facet.dimension.create", b"facet.unimplemented.write")
    else:
        raw = raw.replace(b'"expected_version":0', b'"expected_version":0,"owner":true')
    response, _ = plugin.raw(raw)
    assert response["ok"] is False
    assert set(response["error"]) == {"code"}
    assert counts(engine.profile.root) == (0, 0, 0)


@pytest.mark.parametrize(
    "conflict", ["unknown_name", "archived_addition", "content_changed", "invalid_frontmatter"]
)
def test_o10_real_preview_conflicts_never_apply_or_create_vocabulary(
    runtime: Any, conflict: str
) -> None:
    engine, plugin = runtime
    workspace, page, file, value = canonical(engine, plugin)
    parsed = parse_markdown(file.read_bytes())
    if conflict == "archived_addition":
        revision = plugin.call("facet.assignment.get", record_id=page)["item"]["revision_id"]
        plugin.patch(page, remove=[value], version=1, revision=revision)
        plugin.write("facet.value.archive", value_id=value, expected_version=1)
        plugin.call("workspace.refresh")
        parsed = parse_markdown(file.read_bytes())
    fields = dict(parsed.fields)
    body = parsed.body
    if conflict == "unknown_name":
        fields["facets"] = {"Project": ["Unknown synthetic value"]}
    elif conflict == "archived_addition":
        fields["facets"] = {"Project": ["Apollo"]}
    elif conflict == "content_changed":
        body += "Synthetic owner body edit\n"
    if conflict == "invalid_frontmatter":
        file.write_text("---\nfacets: [\n---\nSynthetic malformed YAML\n")
    else:
        file.write_text(render_markdown(fields=fields, body=body))
    edited = file.read_bytes()
    previous = plugin.call("facet.assignment.get", record_id=page)["item"]
    vocabulary_before = counts(engine.profile.root)
    preview = plugin.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    assert conflict in {item["code"] for item in preview["conflicts"]}
    plugin.error(
        "facet.vault.apply",
        ("projection_conflict", "preview_stale"),
        preview_id=preview["preview_id"],
        operation_id="synthetic-conflict-apply",
    )
    assert file.read_bytes() == edited
    assert plugin.call("facet.assignment.get", record_id=page)["item"] == previous
    assert counts(engine.profile.root) == vocabulary_before


def test_o12_actual_unicode_boundary_and_shared_search_quota(runtime: Any) -> None:
    engine, plugin = runtime
    raw = frame(
        "facet.dimension.create",
        {
            "dto_version": 1,
            "name": "研究🙂 café",
            "expected_version": 0,
            "operation_id": "synthetic-boundary",
        },
        ascii_=True,
    )
    raw = raw[:-1] + b" " * (65536 - len(raw)) + b"\n"
    assert len(raw) == 65536
    response, receipt = plugin.raw(raw)
    assert response["ok"] is True and len(receipt) <= 4096
    dimension = response["result"]["subject_id"]
    value = plugin.write(
        "facet.value.create",
        dimension_id=dimension,
        expected_dimension_version=1,
        expected_version=0,
        name="Synthetic quota value",
    )["subject_id"]
    for _ in range(499):
        plugin.call("facet.query.page", selector={"all_value_ids": [value]})
    # A conventional search consumes the same counter as facet browsing.
    plugin.call(
        "search.page",
        dto_version=1,
        query="Synthetic",
        mode="lexical",
        limit=1,
        cursor=None,
        filters={"space_ids": [], "payload_families": [], "record_types": []},
    )
    plugin.error("facet.query.page", "limit_exceeded", selector={"all_value_ids": [value]})


def test_o14_fresh_portable_preserves_plugin_mutations_and_rejects_old_preview(
    runtime: Any, tmp_path: Path
) -> None:
    engine, plugin = runtime
    workspace, page, _, value = canonical(engine, plugin)
    capture = source(engine, 1)
    plugin.patch(capture, add=[value])
    query = plugin.query([value], record_types=["source"])
    proposal = plugin.propose(query, engine.inbox.spaces()[0].space_id)
    plugin.approve(proposal)
    plugin.call("workspace.refresh")
    preview = plugin.call("facet.vault.preview", workspace_id=workspace, record_id=page)
    original = plugin.call("facet.query.page", query_id=query, expected_query_version=1)
    inspection = plugin.call("publication.show", proposal_id=proposal["proposal_id"])
    plugin.close()
    archive = tmp_path / "synthetic-portable"
    engine.portability.export(archive, export_id=f"export_{uuid.uuid4()}")
    target = tmp_path / "fresh" / "synthetic-restored"
    target.parent.mkdir(mode=0o700)
    imported = engine.portability.import_clean(archive, target, import_id=f"import_{uuid.uuid4()}")
    assert imported.schema_version == 13
    reopened = BrainEngine.open(open_existing_single_user_local(target))
    plugin.__init__(target)
    assert plugin.call("facet.query.page", query_id=query, expected_query_version=1) == original
    assert (
        plugin.call("publication.show", proposal_id=proposal["proposal_id"])["facet_selection"]
        == inspection["facet_selection"]
    )
    plugin.error(
        "facet.vault.apply",
        "preview_stale",
        preview_id=preview["preview_id"],
        operation_id="synthetic-restored-preview",
    )
    fresh_workspace = plugin.call("workspace.setup")
    # Imported content carries evidence, not local body-write authority.
    plugin.error("workspace.refresh", "ineligible_source")
    plugin.call("workspace.facets.refresh")
    assert (
        fresh_workspace["workspace_id"] == workspace
    )  # Portable identity survives local reattach.
    fresh_vault = Path(fresh_workspace["vault_path"])
    restored_note = next(fresh_vault.rglob(f"{page}.md"))
    assert parse_markdown(restored_note.read_bytes()).fields["facets"] == {"Project": ["Apollo"]}
    assert list((fresh_vault / "Facets").rglob(f"{value}.md"))
    assert reopened.facets is not None


@pytest.mark.parametrize("oversize", [False, True])
def test_o12_live_publication_uses_one_mib_envelope_before_effects(
    runtime: Any, oversize: bool
) -> None:
    engine, plugin = runtime
    _, value = plugin.catalog()
    plugin.patch(source(engine, 1), add=[value])
    query = plugin.query([value], record_types=["source"])
    raw = frame(
        "review.propose_from_facet_query",
        {
            "dto_version": 1,
            "operation_id": "synthetic-publication-bound",
            "query_id": query,
            "expected_query_version": 1,
            "target_page_id": None,
            "expected_page_revision_id": None,
            "destination_space_id": engine.inbox.spaces()[0].space_id,
            "title": "Synthetic Unicode envelope",
            "markdown": "Owner-written 研究🙂 café",
        },
        ascii_=True,
    )
    maximum = 1024 * 1024
    raw = raw[:-1] + b" " * (maximum - len(raw) + int(oversize)) + b"\n"
    assert len(raw) == maximum + int(oversize)
    response, encoded = plugin.raw(raw)
    if oversize:
        assert response["error"] == {"code": "invalid_request"}
        plugin.close()
        assert engine.review.list() == ()
    else:
        assert response["ok"] is True and len(encoded) <= 4096
        assert len(plugin.call("publication.list")["proposals"]) == 1


def test_o12_live_unicode_history_admission_never_loses_events(runtime: Any) -> None:
    _engine, plugin = runtime
    created = plugin.write("facet.dimension.create", expected_version=0, name="🙂" * 112 + "-000")
    dimension = created["subject_id"]
    for serial in range(1, 81):
        plugin.write(
            "facet.dimension.rename",
            dimension_id=dimension,
            expected_version=serial,
            name="🙂" * 112 + f"-{serial:03d}",
        )
    cursor = None
    sequences: list[int] = []
    pages = 0
    while True:
        response, encoded = plugin.raw(
            frame(
                "facet.history.list",
                {
                    "dto_version": 1,
                    "subject": {"kind": "dimension", "id": dimension},
                    "limit": 100,
                    "cursor": cursor,
                },
            )
        )
        assert response["ok"] is True and len(encoded) <= 250000, response
        page = response["result"]
        assert page["items"], "A complete event fits and each continuation must progress"
        sequences.extend(item["sequence"] for item in page["items"])
        pages += 1
        if page["complete"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
        assert cursor is not None and pages < 100
    assert pages > 1, "The engine's conservative ASCII DTO cap remains binding"
    assert sequences == list(range(1, 82))


def test_o12_real_plugin_adapter_reserves_receipt_before_mutation(runtime: Any) -> None:
    engine, plugin = runtime
    plugin.close()
    owner = EffectiveAuthority(
        "synthetic-owner", "synthetic-reservation", frozenset(), None, owner=True
    )
    adapter = FacetAppAdapter(
        engine.facets,
        owner,
        budget=FacetSessionBudget(response_bytes={"write": MAX_SESSION_BYTES - 1}),
    )
    before = counts(engine.profile.root)
    with pytest.raises(FacetError, match="limit_exceeded"):
        adapter.invoke(
            "facet.dimension.create",
            {
                "dto_version": 1,
                "name": "Synthetic reservation refused",
                "expected_version": 0,
                "operation_id": "synthetic-reservation",
            },
            transport="plugin",
            maximum_response_bytes=4096,
            encoded_size=lambda result: response_size(f"plugin_{uuid.uuid4()}", result),
        )
    assert counts(engine.profile.root) == before


def test_o12_live_facet_and_manual_review_share_proposal_quota(runtime: Any) -> None:
    engine, plugin = runtime
    _, value = plugin.catalog()
    record = source(engine, 1)
    plugin.patch(record, add=[value])
    query = plugin.query([value], record_types=["source"])
    first = plugin.propose(
        query, engine.inbox.spaces()[0].space_id, operation_id="synthetic-shared-review"
    )
    for _ in range(98):
        assert (
            plugin.propose(
                query, engine.inbox.spaces()[0].space_id, operation_id="synthetic-shared-review"
            )
            == first
        )
    manual = plugin.call(
        "publication.propose",
        capture_ids=[record],
        title="Synthetic manual review",
        markdown="Owner-written manual draft",
        idempotency_key="synthetic-manual-review",
    )
    plugin.error(
        "review.propose_from_facet_query",
        "limit_exceeded",
        dto_version=1,
        query_id=query,
        expected_query_version=1,
        target_page_id=None,
        expected_page_revision_id=None,
        destination_space_id=engine.inbox.spaces()[0].space_id,
        title="Synthetic final",
        markdown="Owner-written final",
        operation_id="synthetic-review-overflow",
    )
    unfiltered = plugin.call("publication.list")["proposals"]
    assert {item["proposal_id"] for item in unfiltered} == {
        first["proposal_id"],
        manual["proposal_id"],
    }
    filtered = plugin.call("publication.list", facet_query_id=query)["proposals"]
    assert [item["proposal_id"] for item in filtered] == [first["proposal_id"]]
    other_query = plugin.query([value], record_types=["source"])
    assert plugin.call("publication.list", facet_query_id=other_query)["proposals"] == []
    plugin.error("publication.list", "invalid_arguments", facet_query_id="malformed-query-id")
    assert plugin.call("publication.list")["proposals"] == unfiltered


def test_o12_live_history_and_facet_history_share_quota(runtime: Any) -> None:
    engine, plugin = runtime
    record = source(engine, 1)
    dimension = plugin.write("facet.dimension.create", expected_version=0, name="Synthetic history")
    subject = {"kind": "dimension", "id": dimension["subject_id"]}
    for _ in range(499):
        plugin.call("facet.history.list", subject=subject, limit=1)
    plugin.call("history.list", dto_version=1, record_id=record, limit=1, cursor=None)
    plugin.error("facet.history.list", "limit_exceeded", subject=subject, limit=1)


def test_o12_live_write_retries_consume_quota_without_duplicate_durable_effects(
    runtime: Any,
) -> None:
    engine, plugin = runtime
    arguments = {
        "name": "Synthetic quota retry",
        "expected_version": 0,
        "operation_id": "synthetic-write-quota",
    }
    first = plugin.write("facet.dimension.create", **arguments)
    for _ in range(499):
        assert plugin.write("facet.dimension.create", **arguments) == first
    plugin.error("facet.dimension.create", "limit_exceeded", **arguments)
    assert counts(engine.profile.root) == (1, 1, 1)
