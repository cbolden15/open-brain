"""Composed bridge/service coverage; this does not exercise the native desktop UI."""

from __future__ import annotations

import json
import sqlite3
import uuid
from io import BytesIO
from pathlib import Path
from typing import cast

from open_brain_engine.engine import ReferencePayload
from open_brain_engine.storage.markdown import parse_markdown

from open_brain.local_data import select_local_root
from open_brain.services.local_bootstrap import open_local_brain
from open_brain.services.plugin_bridge import (
    OPEN_BRAIN_CLIENT_PROTOCOL,
    OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
    dispatch_plugin_request,
    serve_plugin_stdio,
)
from open_brain.services.review_publication import ReviewPublicationService
from open_brain.services.space_inbox import SpaceInboxService


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def test_multi_source_bridge_and_desktop_service_refresh_preserve_provenance(
    tmp_path: Path,
) -> None:
    selection = select_local_root(
        data_dir=str(tmp_path / "brain"),
        environment={"HOME": str(tmp_path)},
        platform_name="darwin",
    )
    canary = "https://example.test/secondary-protected-m1-canary"

    def bridge(operation: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
        request = {
            "arguments": arguments or {},
            "operation": operation,
            "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
            "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
            "request_id": f"plugin_{uuid.uuid4()}",
        }
        output = BytesIO()
        assert (
            serve_plugin_stdio(
                selection,
                input_stream=BytesIO(json.dumps(request).encode()),
                output_stream=output,
                filesystem_type_probe=_filesystem,
                environment={"HOME": str(tmp_path)},
            )
            == 0
        )
        response = json.loads(output.getvalue())
        assert response["ok"] is True
        assert canary not in output.getvalue().decode()
        return cast(dict[str, object], response["result"])

    assert not selection.brain_root.exists()
    bridge("brain.initialize")
    with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
        organization = SpaceInboxService(session.tasks.inbox)
        space = cast(dict[str, object], organization.space_create({"name": "Synthetic"})["space"])
        captures = []
        for index, reference in enumerate(
            ("https://example.test/primary", canary, "https://example.test/third")
        ):
            receipt = session.tasks.capture.accept(
                ReferencePayload(reference, f"Evidence {index}"),
                delivery_id=f"m1.bridge.capture.{index}",
            )
            captures.append(receipt.capture_id)
            assert (
                organization.inbox_route(
                    {"capture_id": receipt.capture_id, "space_id": space["space_id"]}
                )["status"]
                == "routed"
            )

    page_id = ""
    workspace_path: Path | None = None
    relative_path: str | None = None
    for stage, selected in enumerate((captures[:2], captures[2:]), start=1):
        with open_local_brain(selection, filesystem_type_probe=_filesystem) as session:
            review = ReviewPublicationService(session.tasks.review)
            arguments: dict[str, object] = {
                "capture_ids": selected,
                "title": "Nebula",
                "markdown": f"Nebula revision {stage}. {canary}",
            }
            if page_id:
                arguments["target_page_id"] = page_id
            proposed = review.propose(arguments)
            shown = review.show({"proposal_id": proposed["proposal_id"]})
            expected_sources = tuple(captures[: stage + 1])
            assert shown["capture_ids"] == expected_sources
            assert shown["selected_capture_ids"] == tuple(selected)
            assert canary not in json.dumps(shown)
            approved = review.approve(
                {"proposal_id": proposed["proposal_id"], "review_token": shown["review_token"]}
            )
            assert approved["status"] == "approved"
            assert not page_id or page_id == approved["page_id"]
            page_id = str(approved["page_id"])
            fetched = session.tasks.retrieval.fetch(page_id)
            assert fetched is not None and fetched.provenance.capture_ids == expected_sources
            page = session.tasks.retrieval.read_page(page_id)
            assert page is not None and canary not in page.markdown
            # Desktop invokes this same dispatch service with the search.query operation.
            for query, expected in (("nebula", [page_id]), ("unrelatedquasar", [])):
                desktop = dispatch_plugin_request(
                    session,
                    "search.query",
                    {"query": query, "limit": 30},
                    request_id=f"plugin_{uuid.uuid4()}",
                    base_executable=None,
                )
                assert [
                    hit["result_id"] for hit in cast(list[dict[str, object]], desktop["results"])
                ] == expected
                assert canary not in json.dumps(desktop)

        workspace = bridge("workspace.setup" if stage == 1 else "workspace.refresh")
        vault = Path(str(workspace["vault_path"]))
        assert workspace_path is None or workspace_path == vault
        workspace_path = vault
        related = bridge("search.query", {"query": "nebula", "limit": 10})
        hits = cast(list[dict[str, object]], related["results"])
        assert [hit["result_id"] for hit in hits] == [page_id]
        assert hits[0]["capture_id"] == captures[0]
        assert hits[0]["trust"] == "reviewed"
        assert relative_path is None or hits[0]["relative_path"] == relative_path
        relative_path = str(hits[0]["relative_path"])
        materialized = (vault / relative_path).read_text(encoding="utf-8")
        assert f"revision {stage}" in materialized
        canonical = next((selection.brain_root / "content/spaces").rglob(f"{page_id}.md"))
        # The owner vault preserves durable bytes; public responses above are projected.
        assert materialized == canonical.read_text(encoding="utf-8")
        assert parse_markdown(canonical.read_bytes()).fields["provenance"] == list(expected_sources)
        with sqlite3.connect(selection.brain_root / ".open-brain/state/phase1.sqlite3") as db:
            revisions = db.execute(
                "SELECT revision_id, parent_revision_id, body_bytes, privacy_json, provenance_json "
                "FROM managed_note_revisions WHERE note_id = ? ORDER BY recorded_at",
                (page_id,),
            ).fetchall()
            representative = db.execute(
                "SELECT privacy_json, provenance_json FROM captures WHERE capture_id = ?",
                (captures[0],),
            ).fetchone()
        assert len(revisions) == stage
        assert revisions[-1][2] == canonical.read_bytes()
        assert revisions[-1][3:] == representative
        assert b"revision 1" in revisions[0][2]
        if stage == 2:
            assert revisions[1][1] == revisions[0][0]
        assert bridge("search.query", {"query": "unrelatedquasar", "limit": 10}) == {
            "results": [],
            "status": "ok",
        }
