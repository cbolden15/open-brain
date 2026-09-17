from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    InjectedFault,
    ManagedWorkspaceFailure,
    ManagedWorkspaceFault,
    TextPayload,
)

from open_brain.profile import compile_single_user_local, open_existing_single_user_local


def _refresh_fixture(tmp_path: Path) -> tuple[BrainEngine, str, str, Path]:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    )
    engine.capture.accept(
        TextPayload("Recovery synthetic note"),
        delivery_id="recovery.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    note = engine.retrieval.search("Recovery synthetic note", record_type="canonical")[0].result_id
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = engine.managed_workspace.setup(str(workspace), operation_id="recovery.setup")
    return engine, setup.workspace_id, note, next(workspace.rglob(f"{note}.md"))


def _legacy_fixture(tmp_path: Path, *, schema5: bool = False) -> tuple[BrainEngine, str, str, Path]:
    engine, workspace, note, page = _refresh_fixture(tmp_path)
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.materialize(workspace, note, operation_id="legacy.pending")
    moved = tmp_path / "workspace/moved.md"
    page.rename(moved)
    # Reproduce retained schema-5 state after its observation commit and before
    # settlement. Version zero records the missing evidence; it does not invent it.
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE managed_notes SET relative_path='moved.md' WHERE note_id=?", (note,)
        )
        connection.execute(
            "UPDATE managed_write_authority SET authority_version=0, descriptor_json=NULL, "
            "descriptor_sha256=NULL WHERE operation_id='legacy.pending'"
        )
    if schema5:
        from open_brain_engine.engine.local_schema_catalog import REVIEW_SCHEMA

        with sqlite3.connect(
            engine.profile.root / ".open-brain/state/phase1.sqlite3"
        ) as connection:
            connection.execute("DROP TABLE managed_recovery_decisions")
            connection.execute("DROP TABLE managed_write_authority")
            connection.execute("DROP TABLE runtime_compatibility")
            for statement in REVIEW_SCHEMA[-2:]:
                connection.execute(statement)
            connection.execute("DELETE FROM schema_migrations WHERE version=6")
            connection.execute("PRAGMA user_version=5")
    return engine, workspace, note, moved


def test_legacy_blocker_has_bounded_startup_failure(tmp_path: Path) -> None:
    engine, _, _, moved = _legacy_fixture(tmp_path)
    protected = moved.read_bytes()
    for _ in range(2):
        with pytest.raises(ManagedWorkspaceFailure, match="^workspace_recovery_required$"):
            BrainEngine.open(open_existing_single_user_local(engine.profile.root))
    assert moved.read_bytes() == protected


def _database(engine: BrainEngine) -> Path:
    return engine.profile.root / ".open-brain/state/phase1.sqlite3"


@pytest.mark.parametrize("schema5", [False, True])
def test_preview_is_read_only_and_abandonment_is_durable(tmp_path: Path, schema5: bool) -> None:
    from open_brain_engine.engine.managed_recovery import (
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, workspace, note, moved = _legacy_fixture(tmp_path, schema5=schema5)
    protected = moved.read_bytes()
    with sqlite3.connect(_database(engine)) as connection:
        before = list(connection.iterdump())
        original = connection.execute(
            "SELECT * FROM managed_operations WHERE operation_id='legacy.pending'"
        ).fetchone()
    inspection = inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
    entry = inspection.entries[0]
    assert entry.eligible and entry.preview_digest is not None
    serialized = json.dumps(inspection.to_dict())
    for private in (
        str(engine.profile.root),
        str(moved),
        "Recovery synthetic note",
        "body_bytes",
        "moved.md",
    ):
        assert private not in serialized
    with sqlite3.connect(_database(engine)) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (5 if schema5 else 6)
    receipt = abandon_managed_write(
        engine.profile,
        operation_id="legacy.pending",
        expected_digest=entry.preview_digest,
        request_id="recovery.first",
        validate_before_write=engine._assert_root,
    )
    assert receipt.to_dict() == {
        "status": "abandoned",
        "request_id": "recovery.first",
        "operation_id": "legacy.pending",
        "duplicate": False,
        "schema_upgraded": schema5,
    }
    with sqlite3.connect(_database(engine)) as connection:
        connection.row_factory = sqlite3.Row
        op = connection.execute(
            "SELECT * FROM managed_operations WHERE operation_id='legacy.pending'"
        ).fetchone()
        # Compare every immutable column; cancellation changes only its terminal fields.
        assert original is not None
        assert tuple(op)[:11] == original[:11]
        assert op["status"] == "cancelled" and op["stage"] == 3 and op["completed_at"]
        audit = connection.execute("SELECT * FROM managed_recovery_decisions").fetchone()
        assert audit["preview_sha256"] == entry.preview_digest
        assert "body_bytes" not in audit["snapshot_json"]
        assert "Recovery synthetic note" not in audit["snapshot_json"]
    for _ in range(2):
        reopened = BrainEngine.open(open_existing_single_user_local(engine.profile.root))
        with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
            reopened.managed_workspace.materialize(workspace, note, operation_id="legacy.pending")
        assert moved.read_bytes() == protected
    assert (
        reopened.managed_workspace.materialize(
            workspace, note, operation_id="recovery.fresh-materialize"
        ).status
        == "materialized"
    )


def test_replay_uses_historical_audit_after_later_owner_edit(tmp_path: Path) -> None:
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        ManagedRecoveryReceipt,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, workspace, note, moved = _legacy_fixture(tmp_path)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None

    def apply(request_id: str, expected: str = digest) -> ManagedRecoveryReceipt:
        return abandon_managed_write(
            engine.profile,
            operation_id="legacy.pending",
            expected_digest=expected,
            request_id=request_id,
            validate_before_write=engine._assert_root,
        )

    first = apply("recovery.original")
    assert not first.duplicate
    moved.write_bytes(moved.read_bytes() + b"\nA later legitimate owner edit.\n")
    observed = engine.managed_workspace.observe(workspace)
    engine.managed_workspace.accept_observed(
        workspace, note, generation=observed.generation, operation_id="recovery.later-edit"
    )
    expected_bytes = moved.read_bytes()
    with sqlite3.connect(_database(engine)) as connection:
        before = list(connection.iterdump())
    for request in ("recovery.original", "recovery.another-id"):
        receipt = apply(request)
        assert receipt.duplicate and receipt.request_id == "recovery.original"
    with sqlite3.connect(_database(engine)) as connection:
        assert list(connection.iterdump()) == before
        assert (
            connection.execute("SELECT count(*) FROM managed_recovery_decisions").fetchone()[0] == 1
        )
    with pytest.raises(ManagedRecoveryFailure, match="^request_replay_mismatch$"):
        apply("recovery.original", "0" * 64)
    assert moved.read_bytes() == expected_bytes


@pytest.mark.parametrize("schema5", [False, True])
def test_stale_preview_refuses_before_schema_upgrade(tmp_path: Path, schema5: bool) -> None:
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, _, note, moved = _legacy_fixture(tmp_path, schema5=schema5)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    with sqlite3.connect(_database(engine)) as connection:
        connection.execute("UPDATE managed_notes SET active=0 WHERE note_id=?", (note,))
        before = list(connection.iterdump())
    with pytest.raises(ManagedRecoveryFailure, match="^stale_preview$") as error:
        abandon_managed_write(
            engine.profile,
            operation_id="legacy.pending",
            expected_digest=digest,
            request_id="recovery.stale",
            validate_before_write=engine._assert_root,
        )
    assert not error.value.schema_upgraded
    with sqlite3.connect(_database(engine)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (5 if schema5 else 6)
        assert list(connection.iterdump()) == before
    assert moved.exists()


@pytest.mark.parametrize("failure_point", ["audit", "cancel", "post_migration"])
def test_abandonment_rolls_back_decision_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    from open_brain_engine.engine import managed_recovery
    from open_brain_engine.storage import sqlite as storage

    engine, _, _, moved = _legacy_fixture(tmp_path, schema5=failure_point == "post_migration")
    digest = (
        managed_recovery.inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    original = storage._connect_from_parent

    def instrument(parent: int, name: str) -> sqlite3.Connection:
        connection = original(parent, name)

        def refuse(action: int, table: str | None, column: str | None, *_: object) -> int:
            if (
                failure_point == "audit"
                and action == sqlite3.SQLITE_INSERT
                and table == "managed_recovery_decisions"
            ):
                return sqlite3.SQLITE_DENY
            if (
                failure_point == "cancel"
                and action == sqlite3.SQLITE_UPDATE
                and table == "managed_operations"
                and column == "status"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(refuse)
        return connection

    def validate() -> None:
        engine._assert_root()
        with sqlite3.connect(_database(engine)) as current:
            version = current.execute("PRAGMA user_version").fetchone()[0]
        if failure_point == "post_migration" and version == 6:
            raise ValueError("synthetic refusal after committed migration")

    with monkeypatch.context() as scoped:
        scoped.setattr(storage, "_connect_from_parent", instrument)
        with pytest.raises(managed_recovery.ManagedRecoveryFailure) as error:
            managed_recovery.abandon_managed_write(
                engine.profile,
                operation_id="legacy.pending",
                expected_digest=digest,
                request_id="recovery.refused",
                validate_before_write=validate,
            )
    assert error.value.schema_upgraded == (failure_point == "post_migration")
    with sqlite3.connect(_database(engine)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM managed_recovery_decisions").fetchone()[0] == 0
        )
        assert connection.execute(
            "SELECT status FROM managed_operations WHERE operation_id='legacy.pending'"
        ).fetchone() == ("prepared",)
    assert moved.exists()


@pytest.mark.parametrize(
    "damage", ["body", "preimage", "owner", "path", "root", "marker", "revision"]
)
def test_recovery_refuses_corruption(tmp_path: Path, damage: str) -> None:
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, _, note, moved = _legacy_fixture(tmp_path)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    with sqlite3.connect(_database(engine)) as connection:
        if damage == "marker":
            connection.execute(
                "DELETE FROM managed_write_authority WHERE operation_id='legacy.pending'"
            )
        elif damage == "root":
            connection.execute("UPDATE managed_workspaces SET inode='0'")
        elif damage == "revision":
            connection.execute(
                "UPDATE managed_notes SET accepted_revision_id='revision_missing' WHERE note_id=?",
                (note,),
            )
        else:
            column, value = {
                "body": ("body_bytes", b"Broken retained body"),
                "preimage": ("expected_target_sha256", "0" * 64),
                "owner": ("caller_actor_id", "actor_wrong"),
                "path": ("target_relative_path", "../escape.md"),
            }[damage]
            connection.execute(
                f"UPDATE managed_operations SET {column}=? WHERE operation_id='legacy.pending'",
                (value,),
            )
    protected = moved.read_bytes()
    with pytest.raises(ManagedRecoveryFailure):
        abandon_managed_write(
            engine.profile,
            operation_id="legacy.pending",
            expected_digest=digest,
            request_id="recovery.corrupt",
            validate_before_write=engine._assert_root,
        )
    with sqlite3.connect(_database(engine)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM managed_recovery_decisions").fetchone()[0] == 0
        )
        assert connection.execute(
            "SELECT status FROM managed_operations WHERE operation_id='legacy.pending'"
        ).fetchone() == ("prepared",)
    assert moved.read_bytes() == protected


def test_inspection_is_bounded_and_no_generic_engine_is_constructed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, _, _, _ = _legacy_fixture(tmp_path)

    def reject(*args: object, **kwargs: object) -> None:
        raise AssertionError("Recovery must not construct an ordinary engine")

    monkeypatch.setattr(BrainEngine, "__init__", reject)
    first = inspect_managed_recovery(engine.profile, limit=1)
    assert len(first.entries) == 1
    entry = first.entries[0]
    assert entry.preview_digest is not None
    with pytest.raises(ManagedRecoveryFailure, match="^invalid_request$"):
        inspect_managed_recovery(engine.profile, limit=101)
    with pytest.raises(ManagedRecoveryFailure, match="^invalid_request$"):
        inspect_managed_recovery(
            engine.profile, operation_id="legacy.pending", after="legacy.pending"
        )
    abandon_managed_write(
        engine.profile,
        operation_id=entry.operation_id,
        expected_digest=entry.preview_digest,
        request_id="recovery.narrow",
        validate_before_write=engine._assert_root,
    )


def test_startup_finishes_valid_sibling_before_reporting_legacy_blocker(tmp_path: Path) -> None:
    from open_brain_engine.engine.managed_recovery import (
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, workspace, note, page = _refresh_fixture(tmp_path)
    engine.capture.accept(
        TextPayload("Valid sibling recovery"),
        delivery_id="recovery.sibling",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    sibling = engine.retrieval.search("Valid sibling recovery", record_type="canonical")[
        0
    ].result_id
    engine.managed_workspace.refresh(workspace, operation_id="recovery.add-sibling")
    for selected, operation_id in ((note, "legacy.pending"), (sibling, "sibling.pending")):
        engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
        with pytest.raises(InjectedFault):
            engine.managed_workspace.materialize(workspace, selected, operation_id=operation_id)
    moved = tmp_path / "workspace/moved.md"
    page.rename(moved)
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE managed_notes SET relative_path='moved.md' WHERE note_id=?", (note,)
        )
        connection.execute(
            "UPDATE managed_write_authority SET authority_version=0, descriptor_json=NULL, "
            "descriptor_sha256=NULL WHERE operation_id='legacy.pending'"
        )
    protected = moved.read_bytes()
    with pytest.raises(ManagedWorkspaceFailure, match="^workspace_recovery_required$"):
        BrainEngine.open(open_existing_single_user_local(engine.profile.root))
    sibling_path = next((tmp_path / "workspace").rglob(f"{sibling}.md"))
    sibling_bytes = sibling_path.read_bytes()
    with sqlite3.connect(_database(engine)) as connection:
        pending = connection.execute(
            "SELECT operation_id FROM managed_operations "
            "WHERE status IN ('prepared','writing','promoted')"
        ).fetchall()
    assert pending == [("legacy.pending",)]
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    abandon_managed_write(
        engine.profile,
        operation_id="legacy.pending",
        expected_digest=digest,
        request_id="recovery.with-sibling",
        validate_before_write=engine._assert_root,
    )
    for _ in range(2):
        BrainEngine.open(open_existing_single_user_local(engine.profile.root))
        assert moved.read_bytes() == protected and sibling_path.read_bytes() == sibling_bytes


@pytest.mark.parametrize("boundary", ["after_audit", "after_cancel", "after_commit"])
def test_process_exit_preserves_atomic_abandonment(tmp_path: Path, boundary: str) -> None:
    import subprocess
    import sys

    script = r"""
import contextlib, json, os, runpy, sys
from pathlib import Path
from open_brain_engine.engine import managed_recovery as recovery
from open_brain_engine.storage import sqlite as storage
helpers = runpy.run_path(sys.argv[1])
engine, workspace, note, moved = helpers["_legacy_fixture"](Path(sys.argv[2]))
digest = recovery.inspect_managed_recovery(
    engine.profile, operation_id="legacy.pending"
).entries[0].preview_digest
(Path(sys.argv[2]) / "case.json").write_text(json.dumps({
    "workspace":workspace, "note":note, "digest":digest, "body":moved.read_bytes().hex()
}))
boundary = sys.argv[3]
original_connect = storage._connect_from_parent
def connect(parent, name):
    connection = original_connect(parent, name)
    writing = False
    def trace(sql):
        nonlocal writing
        if sql.startswith("INSERT INTO managed_recovery_decisions"):
            writing = True
        if writing and boundary == "after_audit" and sql.startswith("UPDATE managed_operations"):
            os._exit(81)
        if writing and boundary == "after_cancel" and sql == "COMMIT":
            os._exit(81)
    connection.set_trace_callback(trace)
    return connection
storage._connect_from_parent = connect
original_transaction = recovery._LocalStore.transaction
@contextlib.contextmanager
def transaction(self):
    with original_transaction(self) as connection:
        yield connection
    if boundary == "after_commit":
        os._exit(81)
recovery._LocalStore.transaction = transaction
recovery.abandon_managed_write(
    engine.profile, operation_id="legacy.pending", expected_digest=digest,
    request_id="recovery.crash", validate_before_write=engine._assert_root
)
raise AssertionError("process exit not reached")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, __file__, str(tmp_path), boundary],
        text=True,
        capture_output=True,
        timeout=25,
    )
    assert result.returncode == 81, result.stderr
    case = json.loads((tmp_path / "case.json").read_text())
    profile = open_existing_single_user_local(tmp_path / "brain")
    database = profile.root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        decision_count = connection.execute(
            "SELECT count(*) FROM managed_recovery_decisions"
        ).fetchone()[0]
        status = connection.execute(
            "SELECT status FROM managed_operations WHERE operation_id='legacy.pending'"
        ).fetchone()[0]
    committed = boundary == "after_commit"
    assert decision_count == int(committed)
    assert status == ("cancelled" if committed else "prepared")
    from open_brain_engine.engine.managed_recovery import abandon_managed_write
    from open_brain_engine.storage.filesystem import assert_root_identity

    receipt = abandon_managed_write(
        profile,
        operation_id="legacy.pending",
        expected_digest=case["digest"],
        request_id="recovery.crash",
        validate_before_write=lambda: assert_root_identity(profile.root, profile.root_identity),
    )
    assert receipt.duplicate == committed
    for _ in range(2):
        reopened = BrainEngine.open(profile)
        with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
            reopened.managed_workspace.materialize(
                case["workspace"], case["note"], operation_id="legacy.pending"
            )
        assert (tmp_path / "workspace/moved.md").read_bytes() == bytes.fromhex(case["body"])


@pytest.mark.parametrize(
    "damage", ["snapshot", "preview_digest", "terminal_status", "completed_at"]
)
def test_corrupt_audit_refuses_replay(tmp_path: Path, damage: str) -> None:
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, _, _, moved = _legacy_fixture(tmp_path)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    abandon_managed_write(
        engine.profile,
        operation_id="legacy.pending",
        expected_digest=digest,
        request_id="recovery.audit",
        validate_before_write=engine._assert_root,
    )
    with sqlite3.connect(_database(engine)) as connection:
        if damage == "snapshot":
            connection.execute("UPDATE managed_recovery_decisions SET snapshot_json='{}'")
        elif damage == "preview_digest":
            connection.execute(
                "UPDATE managed_recovery_decisions SET preview_sha256=?", ("0" * 64,)
            )
        elif damage == "terminal_status":
            connection.execute(
                "UPDATE managed_operations SET status='prepared',stage=0,completed_at=NULL "
                "WHERE operation_id='legacy.pending'"
            )
        else:
            connection.execute(
                "UPDATE managed_recovery_decisions SET completed_at='2000-01-01T00:00:00Z'"
            )
        before = list(connection.iterdump())
    protected = moved.read_bytes()
    with pytest.raises(ManagedRecoveryFailure):
        abandon_managed_write(
            engine.profile,
            operation_id="legacy.pending",
            expected_digest=digest,
            request_id="recovery.audit",
            validate_before_write=engine._assert_root,
        )
    with sqlite3.connect(_database(engine)) as connection:
        assert list(connection.iterdump()) == before
    assert moved.read_bytes() == protected


def test_recovery_uses_the_same_writer_lease_as_normal_operations(tmp_path: Path) -> None:
    from open_brain_engine.engine.managed_recovery import (
        abandon_managed_write,
        inspect_managed_recovery,
    )
    from open_brain_engine.storage.locks import LockBusyError

    engine, _, _, moved = _legacy_fixture(tmp_path)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    protected = moved.read_bytes()
    with engine._writer_lease.acquire_shared_writer(), pytest.raises(LockBusyError):
        abandon_managed_write(
            engine.profile,
            operation_id="legacy.pending",
            expected_digest=digest,
            request_id="recovery.busy",
            validate_before_write=engine._assert_root,
        )
    with sqlite3.connect(_database(engine)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM managed_recovery_decisions").fetchone()[0] == 0
        )
    assert moved.read_bytes() == protected


def test_preview_keyset_pages_and_refuses_modern_requests(tmp_path: Path) -> None:
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    )
    for index in range(2):
        engine.capture.accept(
            TextPayload(f"Pagination synthetic note {index}"),
            delivery_id=f"pagination.capture.{index}",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=engine.inbox.spaces()[0].space_id,
        )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = engine.managed_workspace.setup(str(workspace), operation_id="pagination.setup")
    with sqlite3.connect(_database(engine)) as connection:
        notes = [
            row[0]
            for row in connection.execute("SELECT note_id FROM managed_notes ORDER BY note_id")
        ]
    for index, note in enumerate(notes):
        engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
        with pytest.raises(InjectedFault):
            engine.managed_workspace.materialize(
                setup.workspace_id, note, operation_id=f"pagination.pending.{index}"
            )
    with sqlite3.connect(_database(engine)) as connection:
        before = list(connection.iterdump())
    first = inspect_managed_recovery(engine.profile, limit=1)
    assert [item.operation_id for item in first.entries] == ["pagination.pending.0"]
    assert first.next_after == "pagination.pending.0"
    second = inspect_managed_recovery(engine.profile, after=first.next_after, limit=1)
    assert [item.operation_id for item in second.entries] == ["pagination.pending.1"]
    assert second.next_after is None
    for item in (*first.entries, *second.entries):
        assert not item.eligible and item.reason == "legacy_authority_required"
    with pytest.raises(ManagedRecoveryFailure, match="^not_eligible$"):
        abandon_managed_write(
            engine.profile,
            operation_id="pagination.pending.0",
            expected_digest="0" * 64,
            request_id="pagination.recovery",
            validate_before_write=engine._assert_root,
        )
    with sqlite3.connect(_database(engine)) as connection:
        assert list(connection.iterdump()) == before


def test_recovery_request_id_cannot_be_reused_for_another_target(tmp_path: Path) -> None:
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, _, _, _ = _legacy_fixture(tmp_path)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    abandon_managed_write(
        engine.profile,
        operation_id="legacy.pending",
        expected_digest=digest,
        request_id="recovery.fixed-id",
        validate_before_write=engine._assert_root,
    )
    with sqlite3.connect(_database(engine)) as connection:
        before = list(connection.iterdump())
    with pytest.raises(ManagedRecoveryFailure, match="^request_replay_mismatch$"):
        abandon_managed_write(
            engine.profile,
            operation_id="recovery.setup",
            expected_digest=digest,
            request_id="recovery.fixed-id",
            validate_before_write=engine._assert_root,
        )
    with sqlite3.connect(_database(engine)) as connection:
        assert list(connection.iterdump()) == before


@pytest.mark.parametrize("symlink", [False, True])
def test_unavailable_or_symlink_workspace_refuses_with_bounded_failure(
    tmp_path: Path, symlink: bool
) -> None:
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, _, _, moved = _legacy_fixture(tmp_path)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    workspace = moved.parent
    relocated = tmp_path / "relocated"
    workspace.rename(relocated)
    if symlink:
        workspace.symlink_to(relocated, target_is_directory=True)
    with sqlite3.connect(_database(engine)) as connection:
        before = list(connection.iterdump())
    with pytest.raises(ManagedRecoveryFailure, match="^operation_replay_mismatch$"):
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
    with pytest.raises(ManagedRecoveryFailure, match="^operation_replay_mismatch$"):
        abandon_managed_write(
            engine.profile,
            operation_id="legacy.pending",
            expected_digest=digest,
            request_id="recovery.bad-root",
            validate_before_write=engine._assert_root,
        )
    with sqlite3.connect(_database(engine)) as connection:
        assert list(connection.iterdump()) == before
    assert (relocated / "moved.md").exists()


@pytest.mark.parametrize("schema5", [False, True])
@pytest.mark.parametrize(
    "damage", ["missing_materialized", "materialized_digest", "missing_parent"]
)
def test_legacy_recovery_refuses_broken_retained_revision_evidence(
    tmp_path: Path, schema5: bool, damage: str
) -> None:
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    engine, _, note, moved = _legacy_fixture(tmp_path, schema5=schema5)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    protected = moved.read_bytes()
    with sqlite3.connect(_database(engine)) as connection:
        if damage == "missing_materialized":
            connection.execute(
                "UPDATE managed_notes SET materialized_revision_id='revision_missing' "
                "WHERE note_id=?",
                (note,),
            )
        elif damage == "materialized_digest":
            connection.execute(
                "UPDATE managed_notes SET materialized_sha256=? WHERE note_id=?", ("0" * 64, note)
            )
        else:
            connection.execute(
                "UPDATE managed_note_revisions SET parent_revision_id='revision_missing' "
                "WHERE revision_id=(SELECT accepted_revision_id "
                "FROM managed_notes WHERE note_id=?)",
                (note,),
            )
        before = list(connection.iterdump())
    with pytest.raises(ManagedRecoveryFailure, match="^operation_replay_mismatch$"):
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
    with pytest.raises(ManagedRecoveryFailure, match="^operation_replay_mismatch$") as failure:
        abandon_managed_write(
            engine.profile,
            operation_id="legacy.pending",
            expected_digest=digest,
            request_id="recovery.bad-retained",
            validate_before_write=engine._assert_root,
        )
    assert not failure.value.schema_upgraded
    with sqlite3.connect(_database(engine)) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (5 if schema5 else 6)
    assert moved.read_bytes() == protected


@pytest.mark.parametrize("schema5", [False, True])
@pytest.mark.parametrize("failure_point", ["permissions", "commit_cleanup"])
def test_store_open_failure_preserves_committed_upgrade_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema5: bool, failure_point: str
) -> None:
    from open_brain_engine.engine import local_schema
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )
    from open_brain_engine.storage import sqlite as storage

    engine, _, _, moved = _legacy_fixture(tmp_path, schema5=schema5)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    protected = moved.read_bytes()

    def refuse_permissions(_parent: int, _name: str) -> None:
        raise OSError("synthetic post-prepare failure")

    original_restore = storage.restore_busy_timeout

    def refuse_after_commit(connection: sqlite3.Connection) -> None:
        if (
            not connection.in_transaction
            and connection.execute("PRAGMA user_version").fetchone()[0] == 6
        ):
            raise sqlite3.OperationalError("synthetic post-commit failure")
        original_restore(connection)

    with monkeypatch.context() as patch:
        if failure_point == "permissions":
            patch.setattr(storage, "_restrict_existing_file", refuse_permissions)
        else:
            patch.setattr(local_schema, "restore_busy_timeout", refuse_after_commit)
        with pytest.raises(ManagedRecoveryFailure) as failure:
            abandon_managed_write(
                engine.profile,
                operation_id="legacy.pending",
                expected_digest=digest,
                request_id="recovery.open-failure",
                validate_before_write=engine._assert_root,
            )
    assert failure.value.schema_upgraded is schema5
    with sqlite3.connect(_database(engine)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("SELECT count(*) FROM managed_recovery_decisions").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT status FROM managed_operations WHERE operation_id='legacy.pending'"
        ).fetchone() == ("prepared",)
    assert moved.read_bytes() == protected


def test_failed_migration_does_not_report_committed_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import local_schema
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )
    from open_brain_engine.storage.sqlite import SchemaError

    engine, _, _, _ = _legacy_fixture(tmp_path, schema5=True)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="legacy.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    with sqlite3.connect(_database(engine)) as connection:
        before = list(connection.iterdump())

    def fail_before_commit(_connection: sqlite3.Connection) -> None:
        raise SchemaError("synthetic migration rollback")

    with monkeypatch.context() as patch:
        patch.setattr(local_schema, "_validate_backfill", fail_before_commit)
        with pytest.raises(ManagedRecoveryFailure) as failure:
            abandon_managed_write(
                engine.profile,
                operation_id="legacy.pending",
                expected_digest=digest,
                request_id="recovery.rollback",
                validate_before_write=engine._assert_root,
            )
    assert not failure.value.schema_upgraded
    with sqlite3.connect(_database(engine)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)
        assert list(connection.iterdump()) == before
