from __future__ import annotations

import json
import sqlite3
import time
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, DecisionOutcome, ProposalDraft, TextPayload
from open_brain_engine.engine.local_schema import (
    PHASE1_STATE_DATABASE,
    classify_local_schema,
    open_local_database,
    open_local_database_read_only,
)
from open_brain_engine.portable import validate_portable_root
from open_brain_engine.storage import sqlite as storage_sqlite
from open_brain_engine.storage.sqlite import SchemaError

from open_brain.profile import compile_single_user_local

FIXTURE = Path(__file__).parents[5] / "tests/fixtures/local-schema/ledger-v4.sql"


def _restore_v4_layout(database: Path) -> None:
    """Copy old table data into an independently frozen historical layout."""
    historical = database.with_suffix(".v4-fixture")
    source = sqlite3.connect(database)
    target = sqlite3.connect(historical)
    try:
        target.executescript(FIXTURE.read_text())
        shadows = {row[1] for row in target.execute("PRAGMA table_list") if row[2] == "shadow"}
        tables = [
            row[0]
            for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] not in shadows | {"schema_migrations", "runtime_compatibility"}
            and row[0] != "search_documents_fts"
        ]
        # Importing projection rows invokes the historical FTS triggers itself.
        tables.remove("search_fts_identity")
        for table in tables:
            rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
            if rows:
                target.executemany(
                    f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in rows[0])})', rows
                )
        target.commit()
    finally:
        source.close()
        target.close()
    historical.replace(database)


def test_v4_pending_and_terminal_reviews_survive_upgrade(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    profile = compile_single_user_local(root, starter_spaces=("Synthetic",))
    engine = BrainEngine.open(profile)
    source = engine.capture.accept(
        TextPayload("Synthetic immutable original"),
        delivery_id="schema.source",
        space_id=engine.inbox.spaces()[0].space_id,
    )
    proposals = engine.review.propose(
        source.capture_id,
        tuple(
            ProposalDraft(name, f"Synthetic {name}")
            for name in ("pending", "approved", "rejected", "edited")
        ),
        delivery_id="schema.proposals",
    )
    for proposal, outcome in zip(proposals[1:], DecisionOutcome, strict=False):
        engine.review.decide(
            proposal.proposal_id,
            outcome,
            delivery_id=f"schema.{outcome.value}",
            edited_markdown="Synthetic edited content"
            if outcome is DecisionOutcome.EDITED
            else None,
        )
    preserved = {
        p.relative_to(root): p.read_bytes()
        for directory in ("sources", "history", "content")
        for p in (root / directory).rglob("*")
        if p.is_file()
    }
    # Verify the supported backup/restore route before reconstructing and migrating
    # the historical database. Retain these assets when invoked as a rehearsal.
    backup = tmp_path / "verified-backup"
    started = time.monotonic()
    engine.portability.export(backup, export_id="export_11111111-1111-4111-8111-111111111111")
    manifest = validate_portable_root(backup)
    restored_root = tmp_path / "verified-restore"
    engine.portability.import_clean(
        backup,
        restored_root,
        import_id="import_11111111-1111-4111-8111-111111111111",
    )
    restored = BrainEngine.open(compile_single_user_local(restored_root))
    assert {item.proposal_id: item for item in restored.review.list()} == {
        item.proposal_id: item for item in engine.review.list()
    }
    assert all(
        (restored_root / path).read_bytes() == payload for path, payload in preserved.items()
    )
    reexport = tmp_path / "verified-reexport"
    restored.portability.export(reexport, export_id="export_22222222-2222-4222-8222-222222222222")
    assert validate_portable_root(reexport)["files"] == manifest["files"]
    backup_restore_seconds = time.monotonic() - started
    database = root / PHASE1_STATE_DATABASE
    _restore_v4_layout(database)
    with pytest.raises(SchemaError):
        open_local_database_read_only(profile)
    started = time.monotonic()
    upgraded = open_local_database(profile)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 5
        assert classify_local_schema(upgraded).state == "current"
        assert upgraded.execute("SELECT count(*) FROM proposals").fetchone()[0] == 4
        assert upgraded.execute("SELECT count(*) FROM decisions").fetchone()[0] == 3
        assert [tuple(row) for row in upgraded.execute("SELECT * FROM runtime_compatibility")] == [
            (1, 1, 5)
        ]
    finally:
        upgraded.close()
    upgrade_seconds = time.monotonic() - started
    assert all((root / path).read_bytes() == payload for path, payload in preserved.items())
    reopened = BrainEngine.open(profile)
    decision = reopened.review.decide(
        proposals[0].proposal_id, DecisionOutcome.APPROVED, delivery_id="schema.pending-finished"
    )
    assert decision.page_id is not None
    assert len(reopened.review.list()) == 4
    (tmp_path / "rehearsal.json").write_text(
        json.dumps(
            {
                "backup_restore_seconds": backup_restore_seconds,
                "upgrade_seconds": upgrade_seconds,
                "source_history_fingerprints": {
                    str(path): sha256(payload).hexdigest() for path, payload in preserved.items()
                },
                "backup": str(backup),
                "restored_root": str(restored_root),
                "portable_equivalence": True,
                "pending_review_decidable": True,
            },
            indent=2,
        )
        + "\n"
    )


def test_review_migration_failure_keeps_historical_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    database = profile.root / PHASE1_STATE_DATABASE
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(FIXTURE.read_text())
    before = database.read_bytes()
    original = storage_sqlite._connect_from_parent

    def instrument(parent: int, name: str) -> sqlite3.Connection:
        connection = original(parent, name)

        def deny(action: int, table: str | None, *_args: object) -> int:
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_TABLE and table == "review_contexts"
                else sqlite3.SQLITE_OK
            )

        connection.set_authorizer(deny)
        return connection

    monkeypatch.setattr(storage_sqlite, "_connect_from_parent", instrument)
    with pytest.raises(SchemaError):
        open_local_database(profile)
    assert database.read_bytes() == before
    monkeypatch.setattr(storage_sqlite, "_connect_from_parent", original)
    retried = open_local_database(profile)
    try:
        assert retried.execute("PRAGMA user_version").fetchone()[0] == 5
        assert classify_local_schema(retried).state == "current"
    finally:
        retried.close()
