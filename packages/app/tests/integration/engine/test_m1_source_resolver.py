from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    DecisionOutcome,
    ProposalDraft,
    ReferencePayload,
    TextPayload,
)
from open_brain_engine.engine import reconciliation as reconciliation_module
from open_brain_engine.engine.search_projection import (
    SearchDocumentProjection,
    canonical_source_rows,
    project_search_document,
)
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.profile import compile_single_user_local

SECONDARY_REFERENCE = "https://example.test/m1-secondary-protected-canary"


def _publication(tmp_path: Path) -> tuple[BrainEngine, Path, tuple[str, ...]]:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    space = engine.inbox.create_space("Synthetic", delivery_id="resolver.space")
    captures = tuple(
        engine.capture.accept(
            ReferencePayload(reference, f"Evidence {index}"),
            delivery_id=f"resolver.capture.{index}",
            space_id=space.space_id,
        ).capture_id
        for index, reference in enumerate(
            ("https://example.test/first", SECONDARY_REFERENCE, "https://example.test/third")
        )
    )
    proposal = engine.review.propose(
        captures,
        (ProposalDraft("Nebula", "Nebula reviewed body"),),
        delivery_id="resolver.propose",
    )[0]
    decision = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="resolver.approve",
        expected_review_digest=engine.review.show(proposal.proposal_id).review_digest,
    )
    page = next((engine.profile.root / "content/spaces").rglob(f"{decision.page_id}.md"))
    return engine, page, captures


def test_complete_membership_and_projection_share_a_validation_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, page, captures = _publication(tmp_path)
    parsed = parse_markdown(page.read_bytes())
    page.write_text(
        render_markdown(
            fields={**parsed.fields, "title": "Edited nebula"},
            body=f"Edited nebula body {SECONDARY_REFERENCE}",
        ),
        encoding="utf-8",
    )
    expected_bytes = page.read_bytes()
    original_resolver = canonical_source_rows
    original_projection = project_search_document
    snapshot: list[sqlite3.Connection] = []

    def resolve(
        connection: sqlite3.Connection, *, result_id: str, capture_id: str
    ) -> tuple[sqlite3.Row, ...]:
        assert connection.in_transaction
        snapshot.append(connection)
        # A second writer cannot change membership between validation and projection.
        with (
            closing(
                sqlite3.connect(engine.profile.root / ".open-brain/state/phase1.sqlite3", timeout=0)
            ) as concurrent,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            concurrent.execute("UPDATE review_sources SET ordinal = ordinal + 10")
        rows = original_resolver(connection, result_id=result_id, capture_id=capture_id)
        assert tuple(row["capture_id"] for row in rows) == captures
        return rows

    def project(
        connection: sqlite3.Connection,
        *,
        result_id: str,
        capture_id: str,
        record_type: str,
        title: str,
        body: str,
        canonical_path: str | None,
    ) -> SearchDocumentProjection:
        assert snapshot == [connection]
        assert connection.in_transaction
        return original_projection(
            connection,
            result_id=result_id,
            capture_id=capture_id,
            record_type=record_type,
            title=title,
            body=body,
            canonical_path=canonical_path,
        )

    monkeypatch.setattr(reconciliation_module, "canonical_source_rows", resolve)
    monkeypatch.setattr(reconciliation_module, "project_search_document", project)
    receipt = engine.reconciliation.reconcile()
    assert receipt.page_updates == 1
    result = engine.retrieval.fetch(str(parsed.fields["page_id"]))
    assert result is not None
    assert result.provenance.capture_ids == captures
    assert result.trust == "reviewed"
    assert SECONDARY_REFERENCE not in result.title + result.excerpt
    assert page.read_bytes() == expected_bytes


@pytest.mark.parametrize(
    "tamper",
    (
        "missing",
        "scalar",
        "malformed",
        "reordered",
        "secondary-reordered",
        "duplicate",
        "truncated",
        "unknown",
        "trust",
        "space",
        "tenant",
        "page-id",
        "path",
        "representative-id",
        "head-representative-id",
    ),
)
def test_reconciliation_rejects_changed_identity_or_membership(tmp_path: Path, tamper: str) -> None:
    engine, page, captures = _publication(tmp_path)
    parsed = parse_markdown(page.read_bytes())
    fields = dict(parsed.fields)
    page_id = str(fields["page_id"])
    before = engine.retrieval.fetch(page_id)
    if tamper == "missing":
        fields.pop("provenance")
    elif tamper == "scalar":
        fields["provenance"] = captures[0]
    elif tamper == "malformed":
        fields["provenance"] = [captures[0], 7]
    elif tamper == "reordered":
        fields["provenance"] = list(reversed(captures))
    elif tamper == "secondary-reordered":
        fields["provenance"] = [captures[0], captures[2], captures[1]]
    elif tamper == "duplicate":
        fields["provenance"] = [captures[0], captures[1], captures[1]]
    elif tamper == "truncated":
        fields["provenance"] = list(captures[:1])
    elif tamper == "unknown":
        fields["provenance"] = [*captures[:2], "capture_00000000-0000-4000-8000-000000000001"]
    elif tamper == "trust":
        fields["trust"] = "owner"
    elif tamper == "space":
        fields["space_id"] = engine.inbox.create_space(
            "Other", delivery_id="resolver.other-space"
        ).space_id
    elif tamper == "tenant":
        fields["tenant_id"] = "tenant_00000000-0000-4000-8000-000000000001"
    elif tamper == "page-id":
        fields["page_id"] = "page_00000000-0000-4000-8000-000000000001"
    elif tamper == "path":
        page = page.rename(page.with_name("page_00000000-0000-4000-8000-000000000001.md"))
    else:
        table = "search_documents" if tamper == "representative-id" else "review_page_heads"
        column = "result_id" if tamper == "representative-id" else "page_id"
        with engine._store.transaction() as connection:
            connection.execute(
                f"UPDATE {table} SET capture_id = ? WHERE {column} = ?", (captures[1], page_id)
            )
    page.write_text(render_markdown(fields=fields, body="Rejected new body"), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical"):
        engine.reconciliation.reconcile()
    with closing(engine._store.connect()) as connection:
        row = connection.execute(
            "SELECT title, body FROM search_documents WHERE result_id = ?", (page_id,)
        ).fetchone()
        assert before is not None and row["title"] == before.title
        assert "Rejected new body" not in row["body"]


@pytest.mark.parametrize("reviewed", (False, True), ids=("owner-note", "legacy-review"))
def test_legacy_single_source_resolution_and_reconciliation(tmp_path: Path, reviewed: bool) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    space = engine.inbox.create_space("Legacy", delivery_id="legacy.space")
    capture = engine.capture.accept(
        TextPayload("Legacy evidence"),
        delivery_id="legacy.capture",
        space_id=space.space_id,
        action=CaptureAction.QUICK if reviewed else CaptureAction.CANONICAL_NOTE,
    )
    if reviewed:
        proposal = engine.review.propose(
            capture.capture_id,
            (ProposalDraft("Legacy", "Legacy reviewed"),),
            delivery_id="legacy.propose",
        )[0]
        engine.review.decide(
            proposal.proposal_id, DecisionOutcome.APPROVED, delivery_id="legacy.approve"
        )
    page = next((engine.profile.root / "content/spaces").rglob("page_*.md"))
    parsed = parse_markdown(page.read_bytes())
    page_id = str(parsed.fields["page_id"])
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM review_page_heads").fetchone()[0] == 0
        rows = canonical_source_rows(connection, result_id=page_id, capture_id=capture.capture_id)
        assert tuple(row["capture_id"] for row in rows) == (capture.capture_id,)
    page.write_text(
        render_markdown(fields=parsed.fields, body="Legacy edited body"), encoding="utf-8"
    )
    assert engine.reconciliation.reconcile().page_updates == 1
    result = engine.retrieval.fetch(page_id)
    assert result is not None and result.provenance.capture_ids == (capture.capture_id,)
    assert result.trust == ("reviewed" if reviewed else "owner")
    with closing(engine._store.connect()) as connection:
        provenance = connection.execute(
            "SELECT provenance_json FROM search_documents WHERE result_id = ?", (page_id,)
        ).fetchone()[0]
    assert json.loads(provenance) == {"capture_id": capture.capture_id}
