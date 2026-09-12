from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    CaptureAction,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.profile import compile_single_user_local


def test_reconciliation_updates_retrieval_and_space_name_without_rewriting_owner_markdown(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    space = tasks.inbox.create_space("Studio", delivery_id="reconcile.space")
    capture = tasks.capture.accept(
        TextPayload("Original canonical body\n"),
        delivery_id="reconcile.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    page = next((root / "content" / "spaces").rglob("page_*.md"))
    original_page = page.read_bytes()
    parsed_page = parse_markdown(original_page)
    page.write_bytes(
        render_markdown(
            fields={
                **parsed_page.fields,
                "modified_at": "2026-09-01T12:30:00Z",
                "title": "Edited title",
            },
            body="Edited owner Markdown body\n",
        ).encode("utf-8")
    )
    space_file = next((root / "content" / "spaces").rglob("_space.md"))
    original_space = space_file.read_bytes()
    parsed_space = parse_markdown(original_space)
    space_file.write_bytes(
        render_markdown(fields={**parsed_space.fields, "name": "Renamed Studio"}, body="").encode(
            "utf-8"
        )
    )
    expected_page = page.read_bytes()
    expected_space = space_file.read_bytes()
    receipt = tasks.reconciliation.reconcile()

    refreshed = tasks.retrieval.search("Edited owner Markdown")[0]
    renamed = tasks.inbox.spaces()[0]

    assert receipt.status == "reconciled"
    assert receipt.page_updates == 1
    assert receipt.space_updates == 1
    assert refreshed.result_id != capture.capture_id
    assert refreshed.title == "Edited title"
    assert renamed.space_id == space.space_id
    assert renamed.slug == space.slug
    assert renamed.name == "Renamed Studio"
    assert page.read_bytes() == expected_page
    assert space_file.read_bytes() == expected_space


def test_reconciliation_repairs_stored_trust_but_rejects_edited_trust(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root, starter_spaces=("Notes",)))
    space = tasks.inbox.spaces()[0]
    capture = tasks.capture.accept(
        TextPayload("Durable trust canary"),
        delivery_id="reconcile.trust.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    page = next((root / "content/spaces").rglob("page_*.md"))
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.execute(
            "UPDATE search_documents SET trust = 'unverified' "
            "WHERE capture_id = ? AND record_type = 'canonical'",
            (capture.capture_id,),
        )

    repaired = tasks.reconciliation.reconcile()
    result = tasks.retrieval.search("Durable trust canary", record_type="canonical")[0]

    assert repaired.page_updates == 1
    assert result.trust == "owner"

    parsed = parse_markdown(page.read_bytes())
    page.write_bytes(
        render_markdown(
            fields={**parsed.fields, "trust": "unverified"},
            body=parsed.body,
        ).encode("utf-8")
    )

    with pytest.raises(ValueError, match="canonical"):
        tasks.reconciliation.reconcile()
    assert tasks.retrieval.search("Durable trust canary", record_type="canonical")[0].trust == (
        "owner"
    )
