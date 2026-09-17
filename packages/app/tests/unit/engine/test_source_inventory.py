from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine import ReferencePayload, TextPayload, open_local_engine
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.source_inventory import inventory_sources
from open_brain_engine.engine.t03_contracts import T03Error

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


def test_inventory_preserves_ungrouped_historical_bytes_and_current_alias(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    submission = _public_submission(tasks)
    first = replace(submission, payload=ReferencePayload(submission.source_reference, "first"))
    second = replace(first, payload=ReferencePayload(submission.source_reference, "second"))
    old = tasks.capture.submit(first)
    current = tasks.capture.submit(second)
    manual_a = tasks.capture.accept(TextPayload("identical"), delivery_id="manual.a")
    manual_b = tasks.capture.accept(TextPayload("identical"), delivery_id="manual.b")
    with open_local_database_read_only(profile) as connection:
        inventory = inventory_sources(profile, connection)
    assert set(inventory.captures) == {
        old.capture_id,
        current.capture_id,
        manual_a.capture_id,
        manual_b.capture_id,
    }
    assert old.capture_id not in inventory.current_rows
    assert inventory.aliases[first.delivery_id] == current.capture_id
    for path, payload, _ in inventory.captures.values():
        assert (profile.root / path).read_bytes() == payload


@pytest.mark.parametrize("damage", ["missing", "symlink", "digest"])
def test_inventory_blocks_damaged_history_before_writes(tmp_path: Path, damage: str) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    tasks.capture.accept(TextPayload("synthetic"), delivery_id="manual")
    source = next((profile.root / "sources/captures").rglob("*.json"))
    original = source.read_bytes()
    if damage == "missing":
        source.unlink()
    elif damage == "symlink":
        target = tmp_path / "synthetic.json"
        target.write_bytes(original)
        source.unlink()
        source.symlink_to(target)
    else:
        source.write_bytes(original.replace(b"synthetic", b"tampered!"))
    with open_local_database_read_only(profile) as connection:
        before = connection.execute("PRAGMA user_version").fetchone()[0]
        with pytest.raises(T03Error):
            inventory_sources(profile, connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == before
