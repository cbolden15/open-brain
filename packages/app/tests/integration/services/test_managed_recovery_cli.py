from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    InjectedFault,
    ManagedWorkspaceFault,
    TextPayload,
)
from open_brain_engine.engine.managed_inference import recover_inference_sessions

import open_brain.services.local_entrypoints as entrypoints
from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_runtime_session import hold_local_runtime_session


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _legacy_fixture(tmp_path: Path, *, schema5: bool = False) -> tuple[BrainEngine, Path, Path]:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root, starter_spaces=("Notes",)))
    engine.capture.accept(
        TextPayload("Synthetic CLI recovery note"),
        delivery_id="recovery.cli.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    note_id = engine.retrieval.search("Synthetic CLI recovery note", record_type="canonical")[
        0
    ].result_id
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = engine.managed_workspace.setup(str(workspace), operation_id="recovery.cli.setup")
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.materialize(
            setup.workspace_id,
            note_id,
            operation_id="recovery.cli.pending",
        )
    original = next(workspace.rglob(f"{note_id}.md"))
    moved = workspace / "moved.md"
    original.rename(moved)
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE managed_notes SET relative_path='moved.md' WHERE note_id=?",
            (note_id,),
        )
        connection.execute(
            """UPDATE managed_write_authority
            SET authority_version=0, descriptor_json=NULL, descriptor_sha256=NULL
            WHERE operation_id='recovery.cli.pending'"""
        )
    if schema5:
        from open_brain_engine.engine.local_schema_catalog import REVIEW_SCHEMA

        with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
            connection.execute("DROP TABLE managed_recovery_decisions")
            connection.execute("DROP TABLE managed_write_authority")
            connection.execute("DROP TABLE runtime_compatibility")
            for statement in REVIEW_SCHEMA[-2:]:
                connection.execute(statement)
            connection.execute("DELETE FROM schema_migrations WHERE version=6")
            connection.execute("PRAGMA user_version=5")
    return engine, root, moved


def test_recovery_inspection_dispatches_before_normal_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "existing-brain"
    calls: list[dict[str, object]] = []

    def fake_recovery(_selection: object, **arguments: object) -> dict[str, object]:
        calls.append(arguments)
        return {
            "entries": [],
            "next_after": None,
            "schema_upgraded": False,
            "status": "inspected",
        }

    monkeypatch.setattr(entrypoints, "run_managed_recovery", fake_recovery)
    monkeypatch.setattr(
        entrypoints,
        "open_local_brain",
        lambda *_args, **_kwargs: pytest.fail("normal bootstrap must not open"),
    )

    assert (
        run_cli(
            ("workspace", "recover", "--data-dir", str(root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "entries": [],
        "next_after": None,
        "schema_upgraded": False,
        "status": "inspected",
    }
    assert calls == [
        {
            "abandon": False,
            "after": None,
            "expected_digest": None,
            "filesystem_type_probe": _filesystem,
            "limit": 100,
            "operation_id": None,
            "request_id": None,
        }
    ]


def test_recovery_apply_passes_exact_owner_decision_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "existing-brain"
    calls: list[dict[str, object]] = []

    def fake_recovery(_selection: object, **arguments: object) -> dict[str, object]:
        calls.append(arguments)
        return {
            "duplicate": False,
            "operation_id": "operation.synthetic",
            "request_id": "recovery.synthetic",
            "schema_upgraded": True,
            "status": "abandoned",
        }

    monkeypatch.setattr(entrypoints, "run_managed_recovery", fake_recovery)

    assert (
        run_cli(
            (
                "workspace",
                "recover",
                "--data-dir",
                str(root),
                "--operation-id",
                "operation.synthetic",
                "--abandon",
                "--expected-digest",
                "a" * 64,
                "--request-id",
                "recovery.synthetic",
                "--json",
            ),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["schema_upgraded"] is True
    assert calls[0]["operation_id"] == "operation.synthetic"
    assert calls[0]["expected_digest"] == "a" * 64
    assert calls[0]["request_id"] == "recovery.synthetic"
    assert calls[0]["abandon"] is True


@pytest.mark.parametrize(
    "arguments",
    (
        ("workspace", "recover", "synthetic-note"),
        ("workspace", "recover", "--generation", "1"),
        ("workspace", "recover", "--choice", "accepted"),
        ("workspace", "recover", "--expected-digest", "a" * 64),
        ("workspace", "recover", "--request-id", "recovery.synthetic"),
        ("workspace", "recover", "--abandon"),
        (
            "workspace",
            "recover",
            "--operation-id",
            "operation.synthetic",
            "--abandon",
            "--expected-digest",
            "a" * 64,
        ),
        (
            "workspace",
            "recover",
            "--operation-id",
            "operation.synthetic",
            "--after",
            "operation.before",
        ),
        ("workspace", "status", "--operation-id", "operation.synthetic"),
        ("workspace", "status", "--abandon"),
        ("workspace", "recover", "--all"),
        ("workspace", "recover", "--force"),
    ),
)
def test_recovery_rejects_ambiguous_or_out_of_scope_arguments_before_open(
    arguments: tuple[str, ...],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "must-remain-absent"

    assert (
        run_cli(
            (*arguments, "--data-dir", str(root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_command"
    assert not root.exists()


def test_recovery_limit_is_bounded_before_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "must-remain-absent"
    for limit in (0, 101):
        assert (
            run_cli(
                (
                    "workspace",
                    "recover",
                    "--limit",
                    str(limit),
                    "--data-dir",
                    str(root),
                    "--json",
                ),
                filesystem_type_probe=_filesystem,
            )
            == 2
        )
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_command"
    assert not root.exists()


def test_owner_cli_inspects_then_abandons_without_ordinary_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _engine, root, moved = _legacy_fixture(tmp_path)
    protected = moved.read_bytes()
    runtime = root / ".open-brain/runtime-sessions"
    assert not runtime.exists()

    assert (
        run_cli(
            (
                "workspace",
                "recover",
                "--operation-id",
                "recovery.cli.pending",
                "--data-dir",
                str(root),
                "--json",
            ),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["schema_upgraded"] is False
    assert preview["entries"][0]["eligible"] is True
    assert not runtime.exists()
    digest = preview["entries"][0]["preview_digest"]

    assert (
        run_cli(
            (
                "workspace",
                "recover",
                "--operation-id",
                "recovery.cli.pending",
                "--abandon",
                "--expected-digest",
                digest,
                "--request-id",
                "recovery.cli.decision",
                "--data-dir",
                str(root),
                "--json",
            ),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {
        "duplicate": False,
        "operation_id": "recovery.cli.pending",
        "request_id": "recovery.cli.decision",
        "schema_upgraded": False,
        "status": "abandoned",
    }
    assert moved.read_bytes() == protected
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM managed_operations WHERE operation_id='recovery.cli.pending'"
        ).fetchone() == ("cancelled",)
        assert connection.execute("SELECT count(*) FROM managed_recovery_decisions").fetchone() == (
            1,
        )


def test_owner_cli_abandonment_refuses_a_live_peer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine, root, _moved = _legacy_fixture(tmp_path)
    assert (
        run_cli(
            (
                "workspace",
                "recover",
                "--operation-id",
                "recovery.cli.pending",
                "--data-dir",
                str(root),
                "--json",
            ),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    digest = json.loads(capsys.readouterr().out)["entries"][0]["preview_digest"]

    with hold_local_runtime_session(
        root,
        engine.profile.root_identity,
        legacy_state_exists=True,
        recover_abandoned_sessions=lambda: recover_inference_sessions(
            engine.profile, validate_before_write=lambda: None
        ),
    ):
        assert (
            run_cli(
                (
                    "workspace",
                    "recover",
                    "--operation-id",
                    "recovery.cli.pending",
                    "--abandon",
                    "--expected-digest",
                    digest,
                    "--request-id",
                    "recovery.cli.live-peer",
                    "--data-dir",
                    str(root),
                    "--json",
                ),
                filesystem_type_probe=_filesystem,
            )
            == 75
        )
        failure = json.loads(capsys.readouterr().out)
        assert failure["error"]["code"] == "runtime_in_use"
        assert failure["schema_upgraded"] is False

    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM managed_operations WHERE operation_id='recovery.cli.pending'"
        ).fetchone() == ("prepared",)


def test_owner_cli_schema5_preview_is_read_only_and_apply_reports_upgrade(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _engine, root, _moved = _legacy_fixture(tmp_path, schema5=True)

    assert (
        run_cli(
            (
                "workspace",
                "recover",
                "--operation-id",
                "recovery.cli.pending",
                "--data-dir",
                str(root),
                "--json",
            ),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    digest = preview["entries"][0]["preview_digest"]
    assert preview["schema_upgraded"] is False
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)

    assert (
        run_cli(
            (
                "workspace",
                "recover",
                "--operation-id",
                "recovery.cli.pending",
                "--abandon",
                "--expected-digest",
                digest,
                "--request-id",
                "recovery.cli.schema5",
                "--data-dir",
                str(root),
                "--json",
            ),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["schema_upgraded"] is True
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)


def test_failed_final_cleanup_retains_marker_and_reports_committed_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _engine, root, _moved = _legacy_fixture(tmp_path, schema5=True)
    assert (
        run_cli(
            (
                "workspace",
                "recover",
                "--operation-id",
                "recovery.cli.pending",
                "--data-dir",
                str(root),
                "--json",
            ),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    digest = json.loads(capsys.readouterr().out)["entries"][0]["preview_digest"]

    import open_brain_engine.engine.managed_inference as managed_inference

    def fail_cleanup(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("synthetic cleanup refusal")

    monkeypatch.setattr(managed_inference, "recover_inference_sessions", fail_cleanup)
    assert (
        run_cli(
            (
                "workspace",
                "recover",
                "--operation-id",
                "recovery.cli.pending",
                "--abandon",
                "--expected-digest",
                digest,
                "--request-id",
                "recovery.cli.failed-cleanup",
                "--data-dir",
                str(root),
                "--json",
            ),
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    failure = json.loads(capsys.readouterr().out)
    assert failure["error"]["code"] == "recovery_unavailable"
    assert failure["schema_upgraded"] is True
    markers = tuple((root / ".open-brain/runtime-sessions").glob("session-*.lock"))
    assert len(markers) == 1
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert connection.execute(
            "SELECT status FROM managed_operations WHERE operation_id='recovery.cli.pending'"
        ).fetchone() == ("cancelled",)


def test_normal_cli_reports_owner_recovery_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _engine, root, moved = _legacy_fixture(tmp_path)
    protected = moved.read_bytes()
    assert (
        run_cli(
            ("workspace", "status", "--data-dir", str(root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    failure = json.loads(capsys.readouterr().out)
    assert failure["error"]["code"] == "workspace_recovery_required"
    assert str(root) not in json.dumps(failure)
    assert moved.read_bytes() == protected


def test_owner_cli_writer_contention_is_retryable_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime
    from hashlib import sha256

    from open_brain_engine.engine.managed_recovery import inspect_managed_recovery
    from open_brain_engine.storage.locks import FileLease

    engine, root, _moved = _legacy_fixture(tmp_path, schema5=True)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="recovery.cli.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    lease = FileLease(
        root / ".open-brain",
        "engine-" + sha256(engine.profile.owner_actor_id.encode()).hexdigest()[:32],
        clock=lambda: datetime.now(UTC),
        validate_acquire=engine._assert_root,
        parent_root_identity=engine.profile.root_identity,
    )
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        before = list(connection.iterdump())
    with lease.acquire_shared_writer():
        assert (
            run_cli(
                (
                    "workspace",
                    "recover",
                    "--data-dir",
                    str(root),
                    "--json",
                    "--operation-id",
                    "recovery.cli.pending",
                    "--abandon",
                    "--expected-digest",
                    digest,
                    "--request-id",
                    "recovery.cli.busy",
                ),
                filesystem_type_probe=_filesystem,
            )
            == 75
        )
    failure = json.loads(capsys.readouterr().out)
    assert failure["error"]["code"] == "database_busy"
    assert failure["schema_upgraded"] is False
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert list(connection.iterdump()) == before


@pytest.mark.parametrize("schema5", [False, True])
def test_cli_preserves_upgrade_fact_after_guarded_opener_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    schema5: bool,
) -> None:
    from open_brain_engine.engine.managed_recovery import inspect_managed_recovery
    from open_brain_engine.storage import sqlite as storage

    engine, root, _ = _legacy_fixture(tmp_path, schema5=schema5)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="recovery.cli.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None

    def refuse_permissions(_parent: int, _name: str) -> None:
        raise OSError("synthetic post-prepare failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_restrict_existing_file", refuse_permissions)
        code = run_cli(
            (
                "workspace",
                "recover",
                "--data-dir",
                str(root),
                "--json",
                "--operation-id",
                "recovery.cli.pending",
                "--abandon",
                "--expected-digest",
                digest,
                "--request-id",
                "recovery.cli.open-failure",
            ),
            filesystem_type_probe=_filesystem,
        )
    failure = json.loads(capsys.readouterr().out)
    assert code == 78
    assert failure["schema_upgraded"] is schema5
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert connection.execute("SELECT count(*) FROM managed_recovery_decisions").fetchone() == (
            0,
        )


@pytest.mark.parametrize("schema5", [False, True])
def test_cli_refuses_broken_materialized_evidence_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], schema5: bool
) -> None:
    from open_brain_engine.engine.managed_recovery import inspect_managed_recovery

    engine, root, moved = _legacy_fixture(tmp_path, schema5=schema5)
    digest = (
        inspect_managed_recovery(engine.profile, operation_id="recovery.cli.pending")
        .entries[0]
        .preview_digest
    )
    assert digest is not None
    protected = moved.read_bytes()
    database = root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE managed_notes SET materialized_revision_id='revision_missing'")
        before = list(connection.iterdump())
    for extra in (
        (),
        ("--abandon", "--expected-digest", digest, "--request-id", "recovery.cli.bad-reference"),
    ):
        assert (
            run_cli(
                (
                    "workspace",
                    "recover",
                    "--data-dir",
                    str(root),
                    "--json",
                    "--operation-id",
                    "recovery.cli.pending",
                    *extra,
                ),
                filesystem_type_probe=_filesystem,
            )
            == 78
        )
        failure = json.loads(capsys.readouterr().out)
        assert failure["error"]["code"] == "operation_replay_mismatch"
        assert failure["schema_upgraded"] is False
    with sqlite3.connect(database) as connection:
        assert list(connection.iterdump()) == before
    assert moved.read_bytes() == protected
