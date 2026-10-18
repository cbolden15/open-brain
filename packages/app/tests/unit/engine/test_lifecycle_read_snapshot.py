"""One lifecycle journal verification per unchanged read snapshot, per-record decisions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine import capture_lifecycle_journal as journal_module
from open_brain_engine.engine.capture_lifecycle import lifecycle_read_snapshot
from open_brain_engine.engine.capture_lifecycle_policy import lifecycle_permits_capture
from open_brain_engine.engine.consumer_journal import LifecycleJournalV2
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, SearchPageRequest

from open_brain.profile import compile_single_user_local


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return EffectiveAuthority(
        engine.profile.owner_actor_id, "snapshot-test", frozenset(), None, owner=True
    )


def _count_opens(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    opened = [0]
    original = LifecycleJournalV2.for_engine.__func__  # type: ignore[attr-defined]

    def counted(
        cls: type[LifecycleJournalV2], /, connection: sqlite3.Connection, **kwargs: Any
    ) -> Any:
        opened[0] += 1
        return original(cls, connection, **kwargs)

    monkeypatch.setattr(LifecycleJournalV2, "for_engine", classmethod(counted))
    return opened


@pytest.fixture
def engine(tmp_path: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(tmp_path / "brain"))


def test_one_verification_per_snapshot_still_denies_one_record(
    engine: BrainEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    captures = [
        engine.capture.accept(TextPayload(f"garnet {name}"), delivery_id=name).capture_id
        for name in ("alpha", "beta", "gamma")
    ]
    denied = captures[1]
    original = journal_module._State.permits

    def permits(self, capture_id: str, *, history: bool) -> bool:  # type: ignore[no-untyped-def]
        return capture_id != denied and original(self, capture_id, history=history)

    monkeypatch.setattr(journal_module._State, "permits", permits)
    opened = _count_opens(monkeypatch)
    page = engine.retrieval.search_page(
        SearchPageRequest(query="garnet", mode="lexical", limit=10), authority=_owner(engine)
    ).to_wire()
    results = page["results"]
    assert isinstance(results, list)
    found = {item["record_id"] for item in results}
    assert found == {captures[0], captures[2]}
    # Three candidates, each checked twice, against one verified journal.
    assert opened[0] == 1


def test_a_commit_discards_the_verified_journal(
    engine: BrainEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = engine.capture.accept(TextPayload("garnet delta"), delivery_id="delta").capture_id
    opened = _count_opens(monkeypatch)
    database = engine.profile.root / ".open-brain/state/phase1.sqlite3"
    # read_snapshot refuses transaction control, so drive one read connection directly.
    with closing(engine._store.connect()) as connection, lifecycle_read_snapshot(connection):
        assert lifecycle_permits_capture(connection, capture)
        assert lifecycle_permits_capture(connection, capture)
        assert opened[0] == 1
        writer = sqlite3.connect(database)
        try:
            writer.execute("CREATE TABLE snapshot_probe(value)")
            writer.commit()
        finally:
            writer.close()
        # The open read transaction still sees its unchanged snapshot.
        assert lifecycle_permits_capture(connection, capture)
        assert opened[0] == 1
        # A new transaction sees the commit, so the journal is verified again.
        connection.rollback()
        connection.execute("BEGIN")
        assert lifecycle_permits_capture(connection, capture)
        assert opened[0] == 2


def test_outside_a_read_snapshot_every_check_verifies(
    engine: BrainEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = engine.capture.accept(TextPayload("garnet eta"), delivery_id="eta").capture_id
    opened = _count_opens(monkeypatch)
    with closing(engine._store.connect()) as connection:
        assert lifecycle_permits_capture(connection, capture)
        assert lifecycle_permits_capture(connection, capture)
    assert opened[0] == 2
