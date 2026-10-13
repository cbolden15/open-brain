"""Query-scoped reuse must preserve current authority and terminal denial."""

import contextvars
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import historical_read_snapshot as snapshots
from open_brain_engine.engine import historical_visibility as visibility
from open_brain_engine.engine.historical_contracts_v2 import HistoricalBaselineRequestV2
from open_brain_engine.engine.historical_projection import verify_versioned_historical_projection
from open_brain_engine.engine.paging import read_snapshot
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.t03_contracts import SearchPageRequest, T03Error

from ._historical_compatibility_fixtures import portable5_import_engine
from .test_historical_compatibility import linked_v2
from .test_paging import authority, external_authority, wire


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    engine = portable5_import_engine(tmp_path, monkeypatch)
    relation, _, _ = linked_v2(engine)
    return engine, relation


def _visible(
    connection: sqlite3.Connection, engine: Any, relation: Any, **kwargs: Any
) -> bool | None:
    parameters = dict(
        local_history=False,
        provider_id="openai",
        brain_id=relation.destination.brain_id,
        issuer_epoch=relation.destination.issuer_epoch,
    )
    parameters.update(kwargs)
    return visibility.historical_capture_visibility(
        connection,
        engine.profile,
        relation.retained_copy.capture_id,
        **parameters,
    )


def test_many_candidates_have_two_complete_audits_and_recheck_all_grants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    audits = registry_reads = 0
    grants: list[tuple[str, bool, str | None]] = []
    original_audit = verify_versioned_historical_projection
    original_registry = visibility.read_historical_registry
    original_evaluate = visibility.evaluate_historical_visibility

    def audit(*args: Any) -> Any:
        nonlocal audits
        audits += 1
        return original_audit(*args)

    def registry(*args: Any) -> Any:
        nonlocal registry_reads
        registry_reads += 1
        return original_registry(*args)

    def evaluate(*args: Any, **kwargs: Any) -> Any:
        grants.append((args[3], kwargs["local_history"], kwargs["provider_id"]))
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(visibility, "verify_versioned_historical_projection", audit)
    monkeypatch.setattr(visibility, "read_historical_registry", registry)
    monkeypatch.setattr(visibility, "evaluate_historical_visibility", evaluate)
    with read_snapshot(engine) as connection:
        for _ in range(20):
            assert _visible(connection, engine, relation) is True
        assert _visible(connection, engine, relation, local_history=True, provider_id=None) is True
        assert _visible(connection, engine, relation, provider_id="unapproved") is False
        assert (
            visibility.historical_capture_visibility(
                connection,
                engine.profile,
                "capture_" + "a" * 26,
                local_history=False,
                provider_id="openai",
                brain_id=relation.destination.brain_id,
                issuer_epoch=relation.destination.issuer_epoch,
            )
            is None
        )
        assert audits == 1
    assert audits == 2 and registry_reads == 24
    assert grants[-2:] == [
        (relation.retained_copy.capture_id, True, None),
        (relation.retained_copy.capture_id, False, "openai"),
    ] or grants[-2:] == [
        (relation.retained_copy.capture_id, False, "openai"),
        (relation.retained_copy.capture_id, True, None),
    ]


@pytest.mark.parametrize("damage", ["registry", "intent", "namespace", "root"])
def test_terminal_authority_damage_prevents_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    root = engine.profile.root
    with pytest.raises(T03Error, match="operation_pending"), read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        if damage == "registry":
            (root / ".open-brain/historical-authority/historical-claims.v1.json").write_bytes(b"{}")
        elif damage == "intent":
            from open_brain_engine.engine.historical_transition_v2 import _path

            (root / _path(relation.operation_id)).unlink()
        elif damage == "namespace":
            from open_brain_engine.engine.historical_transition import _path as v1_path
            from open_brain_engine.engine.historical_transition_v2 import _path as v2_path

            path = root / v1_path(relation.operation_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((root / v2_path(relation.operation_id)).read_bytes())
        else:
            root.rename(tmp_path / "retained-root")
            root.mkdir(mode=0o700)
    assert snapshots._active.get() is None


def test_observed_then_restored_registry_damage_stays_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    path = engine.profile.root / ".open-brain/historical-authority/historical-claims.v1.json"
    raw = path.read_bytes()
    with pytest.raises(T03Error, match="operation_pending"), read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        path.write_bytes(b"{}")
        assert _visible(connection, engine, relation) is False
        path.write_bytes(raw)
        assert _visible(connection, engine, relation) is False


def test_initial_bad_registry_denial_is_sticky_without_new_terminal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    path = engine.profile.root / ".open-brain/historical-authority/historical-claims.v1.json"
    raw = path.read_bytes()
    path.write_bytes(b"{}")
    with read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is False
        path.write_bytes(raw)
        assert _visible(connection, engine, relation) is False
    with read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True


@pytest.mark.parametrize(
    "statement",
    [
        "ROLLBACK",
        "COMMIT",
        "SAVEPOINT other",
        "PRAGMA query_only=OFF",
        "PRAGMA QUERY_ONLY=OFF",
        "PRAGMA QuErY_OnLy=ON",
    ],
)
def test_transaction_control_is_denied_and_latches_even_if_caught(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    with pytest.raises(T03Error, match="operation_pending"), read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)
        assert connection.in_transaction
        assert _visible(connection, engine, relation) is False
    with read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True


def test_wrong_connection_copied_context_and_expired_scope_do_not_reuse_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    with read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        scope = snapshots._active.get()
        assert scope is not None
        copied = contextvars.copy_context()
        assert copied.run(snapshots.current_historical_snapshot, connection, engine.profile) is None
        with engine._store.connect() as other:
            assert snapshots.current_historical_snapshot(other, engine.profile) is None
            assert _visible(other, engine, relation) is True
        with read_snapshot(engine) as inner:
            assert inner is not connection
            assert snapshots.current_historical_snapshot(connection, engine.profile) is None
            assert _visible(inner, engine, relation) is True
        assert snapshots.current_historical_snapshot(connection, engine.profile) is scope
    assert not scope.alive
    assert copied.run(snapshots.current_historical_snapshot, connection, engine.profile) is None


def test_original_exception_and_scope_cleanup_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    with pytest.raises(T03Error, match="not_found"), read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        raise T03Error("not_found")
    assert snapshots._active.get() is None
    with read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True


def test_search_terminal_grant_denial_never_exposes_computed_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    reader = replace(
        external_authority(engine),
        provider_id="openai",
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC}),
    )
    original = snapshots._HistoricalReadSnapshot.finish
    seen: list[str] = []

    def finish(self: Any) -> None:
        seen.extend(key[0] for key in self.grants)
        original_evaluate = visibility.evaluate_historical_visibility

        def changed(*args: Any, **kwargs: Any) -> Any:
            if args[3] == relation.retained_copy.capture_id:
                return False
            return original_evaluate(*args, **kwargs)

        monkeypatch.setattr(visibility, "evaluate_historical_visibility", changed)
        original(self)

    monkeypatch.setattr(snapshots._HistoricalReadSnapshot, "finish", finish)
    with pytest.raises(T03Error, match="operation_pending"):
        engine.retrieval.search_page(
            SearchPageRequest(query="Synthetic", limit=1), authority=reader
        )
    assert relation.retained_copy.capture_id in seen
    assert snapshots._active.get() is None


def test_owner_search_does_not_initialize_historical_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _ = _fixture(tmp_path, monkeypatch)

    def unexpected(*args: Any) -> Any:
        pytest.fail("owner retained search initialized historical authority")

    monkeypatch.setattr(visibility, "load_historical_authority", unexpected)
    assert wire(
        engine.retrieval.search_page(
            SearchPageRequest(query="Synthetic", limit=10),
            authority=replace(authority(), owner=True, principal_id=engine.profile.owner_actor_id),
        )
    )["results"]


def test_same_connection_nested_scope_keeps_outer_transaction_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    with pytest.raises(T03Error, match="operation_pending"), read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        with (
            pytest.raises(SharingError, match="operation_pending"),
            snapshots.historical_read_snapshot(connection, engine.profile),
        ):
            pytest.fail("same-connection nested scope entered")
        with pytest.raises(sqlite3.DatabaseError):
            connection.rollback()
        assert connection.in_transaction
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("BEGIN")


@pytest.mark.parametrize("which", ["original", "copy"])
def test_current_retained_bytes_are_rechecked_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    which: str,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    with pytest.raises(T03Error, match="operation_pending"), read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        if which == "copy":
            capture_id = relation.retained_copy.capture_id
        else:
            scope = snapshots._active.get()
            assert scope is not None and scope.bundle is not None
            bundle = scope.bundle
            baseline = next(
                record.request
                for record in bundle.records
                if record.request.operation_id == relation.baseline_operation_id
            )
            assert isinstance(baseline, HistoricalBaselineRequestV2)
            capture_id = baseline.retained_original.capture_id
        relative = connection.execute(
            "SELECT source_path FROM captures WHERE capture_id=?",
            (capture_id,),
        ).fetchone()[0]
        path = engine.profile.root / relative
        path.write_bytes(b"synthetic damaged retained body")


def test_fresh_query_detects_current_projection_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_engine.engine.historical_recovery import _historical_transaction

    engine, relation = _fixture(tmp_path, monkeypatch)
    with read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
    with _historical_transaction(engine.profile, lambda: None) as connection:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='historical_relations_delete_immutable'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER historical_relations_delete_immutable")
        connection.execute("DELETE FROM historical_relations")
        connection.execute(definition)
    with read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is False


def test_other_thread_and_wrong_root_cannot_reuse_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Thread

    engine, relation = _fixture(tmp_path, monkeypatch)
    with read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        copied = contextvars.copy_context()
        results: list[Any] = []
        thread = Thread(
            target=lambda: results.append(
                copied.run(
                    snapshots.current_historical_snapshot,
                    connection,
                    engine.profile,
                )
            )
        )
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and results == [None]
        assert (
            snapshots.current_historical_snapshot(
                connection,
                replace(engine.profile, root=tmp_path / "other-root"),
            )
            is None
        )
        assert _visible(connection, engine, relation) is True


@pytest.mark.parametrize("initial", [True, False])
def test_pending_fence_damage_preserves_initial_denial_or_blocks_late_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: bool,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    path = engine.profile.root / ".open-brain/historical-authority/historical-fence.v1.json"
    raw = path.read_bytes()
    if initial:
        path.write_bytes(b"{}")
        with read_snapshot(engine) as connection:
            assert _visible(connection, engine, relation) is False
            path.write_bytes(raw)
            assert _visible(connection, engine, relation) is False
    else:
        with (
            pytest.raises(T03Error, match="operation_pending"),
            read_snapshot(engine) as connection,
        ):
            assert _visible(connection, engine, relation) is True
            path.write_bytes(b"{}")
            assert _visible(connection, engine, relation) is False
            path.write_bytes(raw)


def test_same_size_mtime_transition_change_is_seen_at_terminal_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from open_brain_engine.engine.historical_transition_v2 import _path

    engine, relation = _fixture(tmp_path, monkeypatch)
    path = engine.profile.root / _path(relation.operation_id)
    original = path.stat()
    raw = path.read_bytes()
    changed = raw.replace(b'"dto_version":2', b'"dto_version":9', 1)
    assert len(changed) == len(raw) and changed != raw
    with pytest.raises(T03Error, match="operation_pending"), read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        path.write_bytes(changed)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))


def test_current_grant_damage_observed_then_repaired_stays_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, relation = _fixture(tmp_path, monkeypatch)
    with pytest.raises(T03Error, match="operation_pending"), read_snapshot(engine) as connection:
        assert _visible(connection, engine, relation) is True
        relative = connection.execute(
            "SELECT source_path FROM captures WHERE capture_id=?",
            (relation.retained_copy.capture_id,),
        ).fetchone()[0]
        path = engine.profile.root / relative
        raw = path.read_bytes()
        path.write_bytes(b"synthetic transient corruption")
        assert _visible(connection, engine, relation) is False
        path.write_bytes(raw)
        assert _visible(connection, engine, relation) is False
