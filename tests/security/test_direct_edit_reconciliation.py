from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    CaptureAction,
    TextPayload,
    open_local_engine,
)

from open_brain.profile import compile_single_user_local, open_existing_single_user_local


def test_reconciliation_rejects_symlinked_canonical_page_without_overwriting_search_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    space = tasks.inbox.create_space("Studio", delivery_id="reconcile.symlink.space")
    tasks.capture.accept(
        TextPayload("Original safe body\n"),
        delivery_id="reconcile.symlink.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    page = next((root / "content" / "spaces").rglob("page_*.md"))
    target = tmp_path / "outside.md"
    target.write_text("outside\n", encoding="utf-8")
    page.unlink()
    page.symlink_to(target)
    profile = open_existing_single_user_local(root)

    with pytest.raises(ValueError, match="symlink"):
        open_local_engine(profile).reconciliation.reconcile()

    with sqlite3.connect(root / ".open-brain" / "state" / "phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT title FROM search_documents WHERE record_type = 'canonical'"
        ).fetchone() == ("Original safe body",)


def test_reconciliation_rejects_over_budget_markdown_without_mutating_retrieval(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    space = tasks.inbox.create_space("Studio", delivery_id="reconcile.large.space")
    tasks.capture.accept(
        TextPayload("Original bounded body\n"),
        delivery_id="reconcile.large.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    page = next((root / "content" / "spaces").rglob("page_*.md"))
    page.write_text("x" * 70_000, encoding="utf-8")
    profile = open_existing_single_user_local(root)

    with pytest.raises(ValueError, match="bounded size"):
        open_local_engine(profile).reconciliation.reconcile()

    with sqlite3.connect(root / ".open-brain" / "state" / "phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT title FROM search_documents WHERE record_type = 'canonical'"
        ).fetchone() == ("Original bounded body",)


def test_reconciliation_rejects_deleted_canonical_page_without_deleting_retrieval(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    space = tasks.inbox.create_space("Studio", delivery_id="reconcile.deleted.space")
    tasks.capture.accept(
        TextPayload("Original retained body\n"),
        delivery_id="reconcile.deleted.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    page = next((root / "content" / "spaces").rglob("page_*.md"))
    page.unlink()
    profile = open_existing_single_user_local(root)

    with pytest.raises(ValueError, match="missing"):
        open_local_engine(profile).reconciliation.reconcile()

    with sqlite3.connect(root / ".open-brain" / "state" / "phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT title FROM search_documents WHERE record_type = 'canonical'"
        ).fetchone() == ("Original retained body",)


def test_reconciliation_rejects_changed_owner_identity_without_mutating_retrieval(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    space = tasks.inbox.create_space("Studio", delivery_id="reconcile.owner.space")
    tasks.capture.accept(
        TextPayload("Original owner body\n"),
        delivery_id="reconcile.owner.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    page = next((root / "content" / "spaces").rglob("page_*.md"))
    payload = page.read_text(encoding="utf-8")
    page.write_text(
        payload.replace(
            open_existing_single_user_local(root).owner_actor_id,
            "actor_123e4567-e89b-42d3-a456-426614174099",
        ),
        encoding="utf-8",
    )
    profile = open_existing_single_user_local(root)

    with pytest.raises(ValueError, match="owner identity"):
        open_local_engine(profile).reconciliation.reconcile()

    assert tasks.retrieval.search("Original owner body")[0].title == "Original owner body"
