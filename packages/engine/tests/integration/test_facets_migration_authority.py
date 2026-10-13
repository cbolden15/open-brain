"""The appended facet migration retains main's independent authority fence."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, local_schema
from open_brain_engine.engine.facet_schema_migration import migrate_facets
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_registry import _REGISTRY_PATH
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_projection import _claim_transition


@pytest.mark.parametrize("damage", ["pending", "missing_registry", "registry_changed"])
def test_facet_migration_refuses_unsettled_history_without_schema_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 14)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:14])
        engine = BrainEngine.open(profile)
        if damage == "pending":
            transition = _claim_transition(engine)
            HistoricalPendingFence(profile.root, profile.root_identity).prepare(transition)
        elif damage == "missing_registry":
            (profile.root / _REGISTRY_PATH).unlink()
    database = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = database.read_bytes()

    def checkpoint(stage: str) -> None:
        if damage == "registry_changed" and stage == "facet_schema_applied":
            (profile.root / _REGISTRY_PATH).unlink()

    with exclusive_runtime_admission(profile) as admission, pytest.raises(SharingError):
        migrate_facets(
            profile,
            admission=admission,
            clock=lambda: datetime.now(UTC),
            checkpoint=checkpoint,
        )
    assert database.read_bytes() == before
    assert local_schema.inspect_phase1_state(profile).version == 14
