from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest
from open_brain_engine.engine import mcp_eligibility
from open_brain_engine.engine.sharing import EligibilityMode, sharing_eligible
from open_brain_engine.engine.t03_contracts import EffectiveAuthority


@pytest.fixture
def dependency_store() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA user_version=18")
    connection.execute("CREATE TABLE historical_relations(copy_capture_id,original_capture_id)")
    connection.execute("CREATE TABLE sharing_links(copy_capture_id,original_capture_id)")
    connection.execute("CREATE TABLE development_evidence(derived_capture_id,receipt_json)")
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize("mode", list(EligibilityMode))
def test_pending_capture_denied_before_any_sharing_body_lookup(
    monkeypatch: pytest.MonkeyPatch, mode: EligibilityMode
) -> None:
    # No source tables exist: pending evidence must be refused before body lookup,
    # including the owner-history fast path and semantic provider release.
    monkeypatch.setattr(mcp_eligibility, "lifecycle_permits_capture", lambda *a, **kw: False)
    with sqlite3.connect(":memory:") as connection:
        assert not sharing_eligible(connection, "pending", mode=mode)


@pytest.mark.parametrize("relation", ["historical", "sharing", "development"])
def test_retracted_dependency_hides_current_but_preserves_admitted_owner_history(
    dependency_store: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    relation: str,
) -> None:
    connection = dependency_store
    if relation == "development":
        receipt = {
            "supporting_capture_ids": ["retracted"],
            "supports_development_ids": [],
            "supersedes_development_ids": [],
        }
        connection.execute(
            "INSERT INTO development_evidence VALUES(?,?)", ("derived", json.dumps(receipt))
        )
    else:
        table = "historical_relations" if relation == "historical" else "sharing_links"
        connection.execute(f"INSERT INTO {table} VALUES(?,?)", ("derived", "retracted"))

    def permits(connection: sqlite3.Connection, capture_id: str, *, history: bool = False) -> bool:
        return history or capture_id != "retracted"

    monkeypatch.setattr(mcp_eligibility, "lifecycle_permits_capture", permits)
    owner = EffectiveAuthority("owner", "history", frozenset(), None, owner=True)
    scoped = EffectiveAuthority("scoped", "history", frozenset(), None)
    assert not mcp_eligibility.capture_visible(connection, "derived", owner)
    assert not mcp_eligibility.capture_visible(connection, "derived", scoped, history=True)
    assert mcp_eligibility.capture_visible(connection, "derived", owner, history=True)


def test_pending_dependency_hidden_from_owner_history(
    dependency_store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = dependency_store
    connection.execute("INSERT INTO historical_relations VALUES('derived','pending')")
    monkeypatch.setattr(
        mcp_eligibility,
        "lifecycle_permits_capture",
        lambda connection, capture_id, **kw: capture_id != "pending",
    )
    owner = EffectiveAuthority("owner", "history", frozenset(), None, owner=True)
    assert not mcp_eligibility.capture_visible(connection, "derived", owner, history=True)


def test_sql_candidate_filter_excludes_lifecycle_before_body_ranking(
    dependency_store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = dependency_store
    monkeypatch.setattr(
        mcp_eligibility,
        "lifecycle_permits_capture",
        lambda connection, capture_id, **kw: capture_id != "pending",
    )
    mcp_eligibility.install(connection, None)
    connection.execute("CREATE TABLE candidates(record_type,result_id,body)")
    connection.executemany(
        "INSERT INTO candidates VALUES('source',?,?)",
        [("pending", "hidden text"), ("admitted", "visible text")],
    )
    connection.commit()
    rows = connection.execute(
        "SELECT body FROM candidates WHERE mcp_record_visible(record_type,result_id)=1"
    ).fetchall()
    assert [row[0] for row in rows] == ["visible text"]
