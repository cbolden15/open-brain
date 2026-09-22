"""Schema-nine portable orchestration and promotion evidence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import open_brain_engine.engine.portability as portability_module
import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    InjectedFault,
    PortabilityFault,
    TextPayload,
)
from open_brain_engine.engine.materializer import _profile
from open_brain_engine.engine.reconciliation import rederive_live_search_projection
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.portable import v5
from open_brain_engine.portable.relationships_v1 import RELATIONSHIP_METADATA_PATH
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.engine.tests.contract.test_portable_brain_v5 import (
    NOW,
    _fixture,
    _privacy_state,
    _repair_state,
    _upgraded,
    _write,
)

EXPORT = "export_123e4567-e89b-42d3-a456-426614174030"
IMPORT = "import_123e4567-e89b-42d3-a456-426614174031"


def _engine(root: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(root, starter_spaces=("Notes",)))


def test_export_denies_scoped_authority_before_root_or_destination_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path / "brain")
    destination = tmp_path / "must-not-exist"
    scoped = EffectiveAuthority(
        principal_id="scoped-export",
        session_id="scoped-export-session",
        capabilities=frozenset(),
        space_ids=None,
    )
    monkeypatch.setattr(
        engine,
        "_assert_root",
        lambda: pytest.fail("scoped export reached root lookup"),
    )

    with pytest.raises(T03Error, match="^unsupported_capability$"):
        engine.portability.export(destination, export_id=EXPORT, authority=scoped)
    assert not destination.exists()


def test_fresh_export_import_reexport(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    engine = _engine(tmp_path / "brain")
    engine.capture.accept(
        TextPayload("Portable synthetic canonical"),
        delivery_id="portable.v5.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    source, target, again = (tmp_path / name for name in ("export", "import", "again"))
    assert engine.portability.export(source, export_id=EXPORT).schema_version == 5
    assert engine.portability.import_clean(source, target, import_id=IMPORT).schema_version == 5
    reopened = BrainEngine.open(_profile(target, validated_portable_snapshot(target)))
    reopened.portability.export(again, export_id=EXPORT)
    first = dict(validated_portable_snapshot(source).files)
    second = dict(validated_portable_snapshot(again).files)
    first.pop("portable-manifest.json")
    second.pop("portable-manifest.json")
    assert second == first


def _relationship(files: dict[str, bytes], presence: str) -> None:
    if presence == "absent":
        return
    relationships, decisions = [], []
    if presence == "nonempty":
        captures = [
            row["capture_id"] for row in json.loads(files[SOURCE_METADATA_PATH])["revisions"]
        ]
        identity = "relationship_00000000-0000-4000-8000-000000000001"
        relationships.append(
            dict(
                relationship_id=identity,
                left=dict(record_id=captures[0], revision_id=captures[0]),
                right=dict(record_id=captures[1], revision_id=captures[1]),
                kind="duplicate_of",
                status="accepted",
                version=1,
            )
        )
        decisions.append(
            dict(
                decision_id="decision_00000000-0000-4000-8000-000000000001",
                relationship_id=identity,
                sequence=1,
                decision="accept",
                version=1,
                recorded_at=NOW,
                actor_id="actor_123e4567-e89b-42d3-a456-426614174001",
            )
        )
    files[RELATIONSHIP_METADATA_PATH] = canonical(
        dict(
            schema_version=1,
            relationships=relationships,
            decisions=decisions,
        )
    )


@pytest.mark.parametrize("upgraded", [False, True])
@pytest.mark.parametrize("presence", ["absent", "empty", "nonempty"])
@pytest.mark.parametrize("privacy", ["original", "null", "blob", "repaired"])
def test_historical_semantic_roundtrip(
    tmp_path: Path,
    upgraded: bool,
    presence: str,
    privacy: str,
) -> None:
    tmp_path.chmod(0o700)
    files = _fixture()
    _relationship(files, presence)
    if upgraded:
        _upgraded(files)
    if privacy == "repaired":
        state = _repair_state(files)
        if upgraded:
            for receipt in state["repairs"]:
                receipt["issuer_epoch"] = 2
            files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    elif privacy in {"null", "blob"}:
        _privacy_state(files, None if privacy == "null" else b"\x00invalid\xff")
    source, target, again = (tmp_path / name for name in ("source", "target", "again"))
    _write(source, files)
    control = _engine(tmp_path / "control")
    receipt = control.portability.import_clean(source, target, import_id=IMPORT)
    assert receipt.schema_version == 5
    ready = json.loads((target / ".open-brain/state/portability-ready.json").read_bytes())
    assert ready["schema_version"] == 2
    assert ready["authoritative_counts"]["captures"] == receipt.captures
    assert ready["index"]["documents"] == ready["authoritative_counts"]["search_documents"]
    assert control.portability.import_clean(source, target, import_id=IMPORT).duplicate
    restored = BrainEngine.open(_profile(target, validated_portable_snapshot(target)))
    rederive_live_search_projection(restored)
    assert restored.tasks.reconciliation.reconcile().status == "noop"
    (target / ".open-brain/indexes/search.sqlite3").unlink()
    assert restored.portability.rebuild_index().index_generation == 1
    restored.portability.export(again, export_id=EXPORT)
    observed = dict(validated_portable_snapshot(again).files)
    observed.pop("portable-manifest.json")
    assert observed == files
    with restored._store.connect() as connection:
        assert set(
            row[0]
            for row in connection.execute(
                "SELECT trust FROM search_documents WHERE record_type='canonical'"
            )
        ) == {"reviewed"}


@pytest.mark.parametrize("fault", list(PortabilityFault))
def test_import_promotion_fault_matrix(tmp_path: Path, fault: PortabilityFault) -> None:
    tmp_path.chmod(0o700)
    source, target = tmp_path / "source", tmp_path / "target"
    _write(source, _fixture())
    control = BrainEngine.open(compile_single_user_local(tmp_path / "control"), faults={fault})
    with pytest.raises(InjectedFault):
        control.portability.import_clean(source, target, import_id=IMPORT)
    assert target.exists() == (fault == PortabilityFault.AFTER_PROMOTION)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".target")]
    if target.exists():
        assert control.portability.import_clean(source, target, import_id=IMPORT).duplicate


@pytest.mark.parametrize(
    "fault",
    [
        PortabilityFault.AFTER_STAGE_CREATED,
        PortabilityFault.AFTER_PORTABLE_FILE,
        PortabilityFault.AFTER_MANIFEST,
        PortabilityFault.BEFORE_PROMOTION,
        PortabilityFault.AFTER_PROMOTION,
    ],
)
def test_export_promotion_fault_matrix(tmp_path: Path, fault: PortabilityFault) -> None:
    tmp_path.chmod(0o700)
    control = BrainEngine.open(compile_single_user_local(tmp_path / "control"), faults={fault})
    target = tmp_path / "target"
    with pytest.raises(InjectedFault):
        control.portability.export(target, export_id=EXPORT)
    assert target.exists() == (fault == PortabilityFault.AFTER_PROMOTION)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".target")]
    if target.exists():
        assert control.portability.export(target, export_id=EXPORT).duplicate


def _imported(tmp_path: Path) -> tuple[BrainEngine, Path, Path]:
    tmp_path.chmod(0o700)
    source, target = tmp_path / "source", tmp_path / "target"
    _write(source, _fixture())
    control = _engine(tmp_path / "control")
    control.portability.import_clean(source, target, import_id=IMPORT)
    return control, source, target


@pytest.mark.parametrize(
    "tamper",
    [
        "digest",
        "counts",
        "index_count",
        "source_binding",
        "privacy",
        "search",
        "index_row",
        "missing_index",
        "sidecar",
    ],
)
def test_duplicate_rechecks_complete_evidence(tmp_path: Path, tamper: str) -> None:
    control, source, target = _imported(tmp_path)
    ready_path = target / ".open-brain/state/portability-ready.json"
    ready = json.loads(ready_path.read_bytes())
    if tamper == "digest":
        ready["semantic_state_sha256"] = "0" * 64
    elif tamper == "counts":
        ready["authoritative_counts"]["source_revisions"] += 1
    elif tamper == "index_count":
        ready["index"]["documents"] += 1
    elif tamper == "source_binding":
        ready["source_manifest"]["digest_sha256"] = "0" * 64
    elif tamper in {"privacy", "search"}:
        with sqlite3.connect(target / ".open-brain/state/phase1.sqlite3") as connection:
            connection.execute(
                "UPDATE captures SET privacy_json='changed'"
                if tamper == "privacy"
                else "DELETE FROM search_documents"
            )
    elif tamper == "index_row":
        with sqlite3.connect(target / ".open-brain/indexes/search.sqlite3") as connection:
            connection.execute("UPDATE search_documents SET body='tampered'")
    elif tamper == "missing_index":
        (target / ".open-brain/indexes/search.sqlite3").unlink()
    else:
        (target / v5.EFFECTIVE_PRIVACY_PATH).write_bytes(b"{}")
    ready_path.write_bytes(canonical(ready))
    with pytest.raises(ValueError):
        control.portability.import_clean(source, target, import_id=IMPORT)


def _retry_database_contents(root: Path) -> tuple[tuple[str, ...], tuple[str, ...], bytes]:
    """Observe logical state so a failed retry cannot silently repair drift."""
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        state = tuple(connection.iterdump())
    with sqlite3.connect(root / ".open-brain/indexes/search.sqlite3") as connection:
        index = tuple(connection.iterdump())
    return state, index, (root / ".open-brain/state/portability-ready.json").read_bytes()


@pytest.mark.parametrize("tamper", ["capture_search_text", "matching_search_and_index_body"])
def test_duplicate_rejects_archive_content_drift_without_repair(
    tmp_path: Path,
    tamper: str,
) -> None:
    control, source, target = _imported(tmp_path)
    source_files = dict(validated_portable_snapshot(source).files)
    target_files = dict(validated_portable_snapshot(target).files)
    before = _retry_database_contents(target)
    changed_text = "synthetic closure probe absent from validated archive"
    with sqlite3.connect(target / ".open-brain/state/phase1.sqlite3") as connection:
        if tamper == "capture_search_text":
            capture_id = connection.execute(
                "SELECT capture_id FROM captures ORDER BY capture_id LIMIT 1"
            ).fetchone()[0]
            assert (
                connection.execute(
                    "UPDATE captures SET search_text=? WHERE capture_id=?",
                    (changed_text, capture_id),
                ).rowcount
                == 1
            )
        else:
            result_id = connection.execute(
                "SELECT result_id FROM search_documents ORDER BY result_id LIMIT 1"
            ).fetchone()[0]
            assert (
                connection.execute(
                    "UPDATE search_documents SET body=? WHERE result_id=?",
                    (changed_text, result_id),
                ).rowcount
                == 1
            )
    if tamper == "matching_search_and_index_body":
        with sqlite3.connect(target / ".open-brain/indexes/search.sqlite3") as connection:
            assert (
                connection.execute(
                    "UPDATE search_documents SET body=? WHERE result_id=?",
                    (changed_text, result_id),
                ).rowcount
                == 1
            )
    changed = _retry_database_contents(target)
    assert changed != before
    assert changed[2] == before[2]
    with pytest.raises(ValueError):
        control.portability.import_clean(source, target, import_id=IMPORT)
    assert _retry_database_contents(target) == changed
    assert dict(validated_portable_snapshot(source).files) == source_files
    assert dict(validated_portable_snapshot(target).files) == target_files


@pytest.mark.parametrize("boundary", ["before_ready", "before_rename"])
def test_capture_content_drift_prevents_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from open_brain_engine.storage.staging import SiblingStage

    tmp_path.chmod(0o700)
    source, target = tmp_path / "source", tmp_path / "target"
    _write(source, _fixture())
    control = _engine(tmp_path / "control")
    injected: list[Path] = []

    def inject(root: Path) -> None:
        ready = root / ".open-brain/state/portability-ready.json"
        assert ready.exists() == (boundary == "before_rename")
        with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
            assert (
                connection.execute(
                    "UPDATE captures SET search_text='synthetic staged closure drift' "
                    "WHERE capture_id=(SELECT capture_id FROM captures ORDER BY capture_id LIMIT 1)"
                ).rowcount
                == 1
            )
        injected.append(root)

    if boundary == "before_ready":
        original_fault = control._fault

        def fault(point: PortabilityFault) -> None:
            if point == PortabilityFault.AFTER_INDEX:
                stages = list(tmp_path.glob(".target.portable-stage-*"))
                assert len(stages) == 1
                inject(stages[0])
            original_fault(point)

        monkeypatch.setattr(control, "_fault", fault)
    else:
        original_promote = SiblingStage.promote

        def promote(stage: SiblingStage, *, pre_rename: Callable[[], None] | None = None) -> None:
            assert pre_rename is not None
            inject(stage.root)
            original_promote(stage, pre_rename=pre_rename)

        monkeypatch.setattr(SiblingStage, "promote", promote)
    with pytest.raises(ValueError):
        control.portability.import_clean(source, target, import_id=IMPORT)
    assert len(injected) == 1
    assert not target.exists()
    assert not injected[0].exists()
    assert not list(tmp_path.glob(".target.portable-stage-*"))


def test_changed_import_id_and_source_conflict(tmp_path: Path) -> None:
    control, source, target = _imported(tmp_path)
    with pytest.raises(ValueError, match="retry evidence"):
        control.portability.import_clean(source, target, import_id=IMPORT[:-1] + "2")
    manifest_path = source / "portable-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["export_id"] = EXPORT[:-1] + "2"
    manifest_path.write_bytes(canonical(manifest))
    with pytest.raises(ValueError, match="conflicts"):
        control.portability.import_clean(source, target, import_id=IMPORT)


@pytest.mark.parametrize("tamper", ["database", "files", "ready", "index"])
def test_final_promotion_revalidates_after_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    from open_brain_engine.storage.staging import SiblingStage

    tmp_path.chmod(0o700)
    source, target = tmp_path / "source", tmp_path / "target"
    _write(source, _fixture())
    control = _engine(tmp_path / "control")
    original = SiblingStage.promote

    def promote(stage: SiblingStage, *, pre_rename: Callable[[], None] | None = None) -> None:
        if tamper == "database":
            with sqlite3.connect(stage.root / ".open-brain/state/phase1.sqlite3") as connection:
                connection.execute("UPDATE captures SET privacy_json=NULL")
        elif tamper == "files":
            (stage.root / v5.ISSUER_MIGRATION_PATH).write_bytes(b"{}")
        elif tamper == "ready":
            (stage.root / ".open-brain/state/portability-ready.json").write_bytes(b"{}")
        else:
            (stage.root / ".open-brain/indexes/search.sqlite3").unlink()
        original(stage, pre_rename=pre_rename)

    monkeypatch.setattr(SiblingStage, "promote", promote)
    with pytest.raises(ValueError):
        control.portability.import_clean(source, target, import_id=IMPORT)
    assert not target.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".target")]


def test_standalone_v4_refusal_is_exact(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "source"
    files = {
        path: payload for path, payload in _fixture().items() if path not in v5.V5_SIDECAR_PATHS
    }
    _write(source, files)
    manifest = portability_module._manifest(
        sorted(files.items()),
        export_id=EXPORT,
        created_at=NOW,
        tenant_id="tenant_123e4567-e89b-42d3-a456-426614174000",
        version=4,
    )
    (source / "portable-manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(ValueError, match="^Portable v4 import is not supported$"):
        _engine(tmp_path / "control").portability.import_clean(
            source,
            tmp_path / "target",
            import_id=IMPORT,
        )
    assert not (tmp_path / "target").exists()


def test_managed_export_uses_one_caller_owned_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_engine.engine.managed_portability import export_managed_workspace_state

    tmp_path.chmod(0o700)
    engine = _engine(tmp_path / "brain")
    engine.capture.accept(
        TextPayload("Managed portable canonical"),
        delivery_id="portable.v5.managed",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    engine.managed_workspace.setup(str(workspace), operation_id="portable.v5.setup")
    original = engine._store.connect
    connections = []

    def connect() -> sqlite3.Connection:
        connection = original()
        assert connection.in_transaction
        connections.append(connection)
        return connection

    monkeypatch.setattr(engine._store, "connect", connect)
    source, target, again = (tmp_path / name for name in ("source", "target", "again"))
    engine.portability.export(source, export_id=EXPORT)
    assert len(connections) == 1
    with original() as connection:
        assert export_managed_workspace_state(engine, connection=connection) is not None
        assert connection.in_transaction
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    engine.portability.import_clean(source, target, import_id=IMPORT)
    restored = BrainEngine.open(_profile(target, validated_portable_snapshot(target)))
    restored.portability.export(again, export_id=EXPORT)
    first, second = (dict(validated_portable_snapshot(path).files) for path in (source, again))
    first.pop("portable-manifest.json")
    second.pop("portable-manifest.json")
    assert first == second


@pytest.mark.parametrize(
    "mutation",
    [
        "actor_id",
        "tenant_id",
        "role_id",
        "role_claim_id",
        "unknown",
        "duplicate",
        "unsorted",
    ],
)
def test_historical_claim_cannot_expand_current_authority(tmp_path: Path, mutation: str) -> None:
    from open_brain_engine.engine.reconciliation import _require_owner_identity

    engine = _engine(tmp_path / "brain")
    claim = dict(engine.profile.owner_role_claim)
    capabilities = list(cast(tuple[str, ...], claim["capabilities"]))
    claim["capabilities"] = capabilities
    fields: dict[str, object] = dict(
        actor_id=engine.profile.owner_actor_id,
        tenant_id=engine.profile.tenant_id,
        role_claim=claim,
    )
    if mutation in {"actor_id", "tenant_id", "role_id", "role_claim_id"}:
        claim[mutation] = "changed"
    elif mutation == "unknown":
        claim["capabilities"] = ["unknown.authority"]
    elif mutation == "duplicate":
        claim["capabilities"] = [capabilities[0]] * 2
    else:
        claim["capabilities"] = list(reversed(capabilities))
    with pytest.raises(ValueError, match="owner identity changed"):
        _require_owner_identity(fields, engine)


def test_imported_repair_replay_conflict_and_next_sequence(tmp_path: Path) -> None:
    from dataclasses import replace

    from open_brain_engine.engine.privacy_repairs import PrivacyRepairRequest
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    tmp_path.chmod(0o700)
    files = _fixture()
    state = _repair_state(files)
    source, target = tmp_path / "source", tmp_path / "target"
    _write(source, files)
    _engine(tmp_path / "control").portability.import_clean(source, target, import_id=IMPORT)
    engine = BrainEngine.open(_profile(target, validated_portable_snapshot(target)))
    final = state["repairs"][-1]
    request = PrivacyRepairRequest(
        final["target_kind"],
        final["target_id"],
        final["invalid_evidence_sha256"],
        final["replacement_privacy"],
        final["operation_id"],
        final["supersedes_repair_id"],
    )
    authority = EffectiveAuthority(
        engine.profile.owner_actor_id,
        "session",
        frozenset(),
        None,
        owner=True,
    )
    task = engine.tasks.privacy_repair
    assert task is not None
    with engine._store.connect() as connection:
        generation = connection.execute(
            "SELECT retrieval_generation FROM engine_generations"
        ).fetchone()[0]
    assert task.repair_privacy(request, authority=authority).encode() == canonical(final).decode()
    with engine._store.connect() as connection:
        assert (
            connection.execute("SELECT retrieval_generation FROM engine_generations").fetchone()[0]
            == generation
        )
    with pytest.raises(ValueError, match="operation_conflict"):
        task.repair_privacy(replace(request, supersedes_repair_id=None), authority=authority)
    appended = task.repair_privacy(
        replace(request, operation_id="imported.next", supersedes_repair_id=final["repair_id"]),
        authority=authority,
    )
    assert appended.repair_sequence == final["repair_sequence"] + 1
    changed = tmp_path / "changed"
    engine.portability.export(changed, export_id=EXPORT)
    exported = validated_portable_snapshot(changed)
    assert json.loads(exported.files[v5.EFFECTIVE_PRIVACY_PATH])["repairs"][-1] == json.loads(
        appended.encode()
    )
