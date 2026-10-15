"""Schema17 is appended and requires explicit exclusive migration."""

from __future__ import annotations

from pathlib import Path

import pytest
from open_brain_engine.engine import local_schema, open_local_engine
from open_brain_engine.engine.local import StateSchemaUnavailableError
from open_brain_engine.engine.local_migration import coordinate_local_migration

from open_brain.profile import compile_single_user_local


@pytest.mark.parametrize("predecessor", [15, 16])
def test_slack_sharing_migration_from_predecessor_and_old_client_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, predecessor: int
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", predecessor)
        tasks = open_local_engine(profile)
        retained = tasks.inbox.create_space(
            "Retained synthetic space", delivery_id="retained.space"
        )
    with pytest.raises(StateSchemaUnavailableError, match="migration"):
        open_local_engine(profile)
    coordinate_local_migration(profile)
    reopened = open_local_engine(profile)
    with local_schema.open_local_database_read_only(profile) as connection:
        assert local_schema.classify_local_schema(connection).version == 17
        assert tuple(connection.execute("SELECT * FROM runtime_compatibility").fetchone()) == (
            1,
            12,
            17,
        )
        assert connection.execute("SELECT count(*) FROM slack_recall_selections").fetchone()[0] == 0
        assert connection.execute("SELECT space_id FROM spaces").fetchone()[0] == retained.space_id
    assert reopened.profile == tasks.profile
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 16)
        with pytest.raises(StateSchemaUnavailableError, match="newer"):
            open_local_engine(profile)
