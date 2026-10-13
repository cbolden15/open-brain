"""Warm pure decoding must never bypass current authority or confinement."""

import os
from collections import OrderedDict
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine import historical_decode_cache as cache
from open_brain_engine.engine import historical_projection as projection
from open_brain_engine.engine import historical_transition as v1
from open_brain_engine.engine.historical_dispatch import VersionedHistoricalTransitionStore
from open_brain_engine.engine.historical_projection import verify_versioned_historical_projection
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.historical_registry import HistoricalRegistryStore
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    capture_root_identity,
    read_confined,
)

from ._historical_compatibility_fixtures import portable5_import_engine
from .test_historical_compatibility import linked_v2
from .test_historical_transition import _transition


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "_values", OrderedDict())
    monkeypatch.setattr(cache, "_bytes", 0)
    monkeypatch.setattr(projection, "_projection_cache", None)


def test_warm_transition_still_reads_every_current_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "brain"
    root.mkdir(mode=0o700)
    store = v1.HistoricalTransitionStore(root, capture_root_identity(root))
    record = _transition()
    store.persist(record)
    original_read, original_decode = read_confined, v1.HistoricalTransition.from_bytes
    reads = decodes = 0

    def read(**kwargs: Any) -> bytes | None:
        nonlocal reads
        reads += 1
        return original_read(**kwargs)

    def decode(cls: type[v1.HistoricalTransition], raw: bytes) -> v1.HistoricalTransition:
        nonlocal decodes
        decodes += 1
        return original_decode(raw)

    monkeypatch.setattr(v1, "read_confined", read)
    monkeypatch.setattr(v1.HistoricalTransition, "from_bytes", classmethod(decode))
    assert store.read(record.request.operation_id) == record
    assert store.read(record.request.operation_id) == record
    assert reads == 2 and decodes == 1
    path = root / v1._path(record.request.operation_id)
    previous = path.stat()
    raw = path.read_bytes()
    damaged = raw.replace(b'"dto_version":1', b'"dto_version":9', 1)
    assert len(damaged) == len(raw) and damaged != raw
    path.write_bytes(damaged)
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    with pytest.raises(SharingError, match="invalid_arguments"):
        store.read(record.request.operation_id)
    assert reads == 3 and decodes == 2


@pytest.mark.parametrize("damage", ["missing", "symlink", "root"])
def test_warm_decode_never_caches_filesystem_safety(tmp_path: Path, damage: str) -> None:
    root = tmp_path / "brain"
    root.mkdir(mode=0o700)
    store = v1.HistoricalTransitionStore(root, capture_root_identity(root))
    record = _transition()
    store.persist(record)
    assert store.read(record.request.operation_id) == record
    path = root / v1._path(record.request.operation_id)
    if damage == "root":
        root.rename(tmp_path / "retained-root")
        root.mkdir(mode=0o700)
    elif damage == "missing":
        path.unlink()
    else:
        retained = tmp_path / "retained-record"
        path.rename(retained)
        path.symlink_to(retained)
    with pytest.raises((SharingError, RootConfinementError)):
        store.read(record.request.operation_id)


@pytest.mark.parametrize("change", ["hardlink", "mode"])
def test_warm_and_cold_reads_preserve_existing_metadata_policy(
    tmp_path: Path, change: str
) -> None:
    root = tmp_path / "brain"
    root.mkdir(mode=0o700)
    identity = capture_root_identity(root)
    store = v1.HistoricalTransitionStore(root, identity)
    record = _transition()
    store.persist(record)
    store.read(record.request.operation_id)
    path = root / v1._path(record.request.operation_id)
    if change == "hardlink":
        os.link(path, tmp_path / "retained-link")
    else:
        path.chmod(0o644)
    # Confined reads do not impose a new private-file policy on public history.
    raw = read_confined(
        root=root, relative=v1._path(record.request.operation_id),
        maximum_bytes=v1._MAX_BYTES, expected_root_identity=identity,
    )
    assert raw is not None
    assert store.read(record.request.operation_id) == v1.HistoricalTransition.from_bytes(raw)
    path.write_bytes(raw.replace(b'"dto_version":1', b'"dto_version":9', 1))
    with pytest.raises(SharingError):
        store.read(record.request.operation_id)


def test_byte_and_entry_limits_evict_without_changing_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cache, "_MAX_BYTES", 5)
    monkeypatch.setattr(cache, "_MAX_ENTRIES", 2)
    calls = 0

    def decode(raw: bytes) -> bytes:
        nonlocal calls
        calls += 1
        return raw.upper()

    for raw in (b"aa", b"bb", b"aa", b"ccc", b"bb", b"oversized", b"oversized"):
        assert cache.decode_historical_bytes(raw, decode) == raw.upper()
        assert cache._bytes <= 5 and len(cache._values) <= 2
    assert calls == 6


def test_mutable_results_and_failures_never_poison_later_decode() -> None:
    calls = 0

    def mutable(raw: bytes) -> dict[str, list[bytes]]:
        nonlocal calls
        calls += 1
        return {"items": [raw]}

    first = cache.decode_historical_bytes(b"x", mutable)
    first["items"].append(b"poison")
    assert cache.decode_historical_bytes(b"x", mutable) == {"items": [b"x"]}
    assert calls == 2 and not cache._values

    def failed(raw: bytes) -> bytes:
        nonlocal calls
        calls += 1
        raise SharingError("invalid_arguments")

    for _ in range(2):
        with pytest.raises(SharingError):
            cache.decode_historical_bytes(b"x", failed)
    assert calls == 4 and not cache._values


def test_v2_nested_evidence_is_immutable_and_warm_sql_damage_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = portable5_import_engine(tmp_path, monkeypatch)
    relation, _, _ = linked_v2(engine)
    store = VersionedHistoricalTransitionStore(engine.profile.root, engine.profile.root_identity)
    baseline = store.read("baseline.synthetic.import")
    assert cache._immutable(baseline)
    observed = baseline.request.observed_delivery  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        observed.submission.namespace["connector_name"] = "poison"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        baseline.proposed.generation = 900  # type: ignore[misc]
    assert store.read("baseline.synthetic.import").canonical_bytes() == baseline.canonical_bytes()
    registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
        relation.destination
    )
    with engine._store.connect() as connection:
        records = verify_versioned_historical_projection(connection, engine.profile, registry)
        assert len(records) == 3
    with _historical_transaction(engine.profile, lambda: None) as connection:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='historical_relations_delete_immutable'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER historical_relations_delete_immutable")
        connection.execute("DELETE FROM historical_relations")
        connection.execute(definition)
    with (
        engine._store.connect() as connection,
        pytest.raises(SharingError, match="binding_mismatch"),
    ):
        verify_versioned_historical_projection(connection, engine.profile, registry)


def test_pure_projection_reuse_never_exposes_cached_mutable_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _transition()
    original = projection._derive_historical_projection_rows
    calls = 0

    def derive(registry: Any, records: Any) -> projection.ProjectionRows:
        nonlocal calls
        calls += 1
        return original(registry, records)

    monkeypatch.setattr(projection, "_derive_historical_projection_rows", derive)
    expected = projection.historical_projection_rows(record.proposed, [record])
    poisoned = projection.historical_projection_rows(record.proposed, [record])
    poisoned["historical_operations"].clear()
    poisoned["historical_claims"].append(("poison",))
    assert projection.historical_projection_rows(record.proposed, [record]) == expected
    assert calls == 1
    # A different exact registry must undergo full pure chain validation.
    with pytest.raises(SharingError, match="binding_mismatch"):
        projection.historical_projection_rows(record.previous, [record])
    assert calls == 2
    # Freshly decoded input instances cannot inherit a previous derivation hit.
    equivalent = v1.HistoricalTransition.from_bytes(record.canonical_bytes())
    assert projection.historical_projection_rows(equivalent.proposed, [equivalent]) == expected
    assert calls == 3


@pytest.mark.parametrize("bound", ["_PROJECTION_MAX_BYTES", "_PROJECTION_MAX_RECORDS"])
def test_pure_projection_budget_never_changes_result(
    monkeypatch: pytest.MonkeyPatch, bound: str,
) -> None:
    record = _transition()
    monkeypatch.setattr(projection, bound, 0)
    expected = projection._derive_historical_projection_rows(record.proposed, [record])
    assert projection.historical_projection_rows(record.proposed, [record]) == expected
    assert projection._projection_cache is None
