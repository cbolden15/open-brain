from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, local_schema
from open_brain_engine.engine.local_schema_catalog import BASELINE, LOCAL_MIGRATIONS

from open_brain.profile import compile_single_user_local


def test_expected_shape_cache_is_bound_to_exact_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = BASELINE
    first_definition = (*baseline, "CREATE TABLE synthetic_cache_first (value TEXT)")
    second_definition = (*baseline, "CREATE TABLE synthetic_cache_second (value INTEGER)")
    connect = sqlite3.connect
    builds = 0

    def counted_connect(database: str) -> sqlite3.Connection:
        nonlocal builds
        builds += 1
        return connect(database)

    monkeypatch.setattr(local_schema, "BASELINE", first_definition)
    monkeypatch.setattr(sqlite3, "connect", counted_connect)
    first = local_schema._expected_shape(13, False, True)
    assert local_schema._expected_shape(13, False, True) == first
    assert builds == 1

    monkeypatch.setattr(local_schema, "BASELINE", second_definition)
    second = local_schema._expected_shape(13, False, True)
    assert second != first
    assert any(name == "synthetic_cache_second" for _, name, _ in second)
    assert builds == 2

    monkeypatch.setattr(local_schema, "BASELINE", first_definition)
    assert local_schema._expected_shape(13, False, True) == first
    assert builds == 2


def test_expected_shape_tracks_historical_catalog_and_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = local_schema._expected_shape(13, False, True)
    with monkeypatch.context() as historical:
        historical.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:10])
        assert local_schema._expected_shape(13, False, True) != current
    assert local_schema._expected_shape(13, False, True) == current
    assert local_schema._expected_shape(2, True, True) != local_schema._expected_shape(
        2, False, True
    )
    assert local_schema._expected_shape(13, False, False) != current


@pytest.mark.parametrize("version", [6, 11, 12, 13, 14, 15, 16])
def test_classifier_builds_only_requested_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with monkeypatch.context() as historical:
        historical.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", version)
        historical.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:version])
        BrainEngine.open(profile)

    expected_shape = local_schema._expected_shape
    requested: list[tuple[int, bool, bool]] = []

    def recorded_shape(era: int, nullable: bool, ledger: bool) -> tuple[tuple[str, str, str], ...]:
        requested.append((era, nullable, ledger))
        return expected_shape(era, nullable, ledger)

    monkeypatch.setattr(local_schema, "_expected_shape", recorded_shape)
    with sqlite3.connect(profile.root / local_schema.PHASE1_STATE_DATABASE) as connection:
        state = local_schema.classify_local_schema(connection)
    assert state == local_schema.SchemaState(
        "current" if version == 16 else "supported_old", version
    )
    assert requested == [(version + 2, False, True)]


def test_repeated_current_schema_classification_has_no_normalization_misses(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    BrainEngine.open(profile)
    connection = sqlite3.connect(profile.root / local_schema.PHASE1_STATE_DATABASE)
    try:
        expected = local_schema.SchemaState("current", local_schema.PHASE1_STATE_SCHEMA_VERSION)
        assert local_schema.classify_local_schema(connection) == expected
        misses_before = local_schema._normalized_sql.cache_info().misses

        assert local_schema.classify_local_schema(connection) == expected

        additional_misses = local_schema._normalized_sql.cache_info().misses - misses_before
        assert additional_misses == 0, (
            f"Repeated current-schema classification caused {additional_misses} normalization "
            "cache misses; the cache must retain a complete schema scan"
        )
    finally:
        connection.close()


def test_normalization_cache_covers_all_supported_schema_shapes() -> None:
    statements = {
        sql
        for era in range(2, local_schema.PHASE1_STATE_SCHEMA_VERSION + 3)
        for nullable in (True, False)
        for ledger in (True, False)
        for _, _, sql in local_schema._expected_shape(era, nullable, ledger)
    }
    maxsize = local_schema._normalized_sql.cache_info().maxsize
    assert maxsize is not None and maxsize >= len(statements), (
        f"Normalization cache capacity {maxsize} cannot retain the {len(statements)} distinct "
        "normalized statements across supported schema shapes; increase _normalized_sql maxsize "
        "when the schema grows"
    )
