"""Bounded Portable Brain tasks for the local engine."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.locks import LockScope
from open_brain_engine.portable import (
    PORTABLE_V1_SCHEMA_CATALOG_DIGEST,
    PORTABLE_V3_SCHEMA_CATALOG_DIGEST,
)
from open_brain_engine.portable.managed_v2 import PORTABLE_V2_SCHEMA_CATALOG_DIGEST
from open_brain_engine.portable.relationships_v1 import RELATIONSHIP_METADATA_PATH
from open_brain_engine.portable.v1 import PortableSnapshot
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH, catalog_digest
from open_brain_engine.portable.v5 import V5_SIDECAR_PATHS, manifest_v5
from open_brain_engine.portable.versioned import validate_portable_root, validated_portable_snapshot
from open_brain_engine.storage.filesystem import RootIdentity, capture_root_identity, read_confined
from open_brain_engine.storage.locks import FileLease
from open_brain_engine.storage.sqlite import SchemaError, connect_database_read_only
from open_brain_engine.storage.staging import (
    StagingError,
    capture_destination_parent,
    destination_child_identity,
    sibling_stage,
)

from .contracts import PortabilityFault, PortabilityReceipt
from .managed_portability import export_managed_workspace_state, import_managed_workspace_state
from .materializer import Materialization, _profile, materialize_portable_root
from .portability_ports import LocalPortableWrites, LocalTenantStorage, local_portability_ports
from .portable_index import IndexBuild, rebuild_portable_index
from .portable_v5_evidence import serialize_portable_v5_state, verify_portable_v5_semantic_state
from .portable_v5_restore import audit_restored_v5, restore_portable_v5_root
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid portability clock")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _portable_id(value: str, prefix: str) -> str:
    from .normalization import _portable_id as validate_identifier

    return validate_identifier(value, prefix)


def _receipt(
    manifest: dict[str, object],
    *,
    status: str,
    duplicate: bool = False,
    index_generation: int | None = None,
) -> PortabilityReceipt:
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] not in {
        1,
        2,
        3,
        4,
        5,
    }:
        raise ValueError("unsupported Portable Brain schema")
    entries = cast(list[dict[str, object]], manifest["files"])
    paths = [cast(str, entry["path"]) for entry in entries]
    return PortabilityReceipt(
        status=status,
        portable_files=len(paths),
        captures=sum(path.startswith("sources/captures/") for path in paths),
        batches=sum(path.startswith("sources/batches/") for path in paths),
        blobs=sum(path.startswith("sources/blobs/") for path in paths),
        history_records=sum(path.startswith("history/") for path in paths),
        schema_version=manifest["schema_version"],
        index_generation=index_generation,
        duplicate=duplicate,
    )


def _manifest(
    files: list[tuple[str, bytes]],
    *,
    export_id: str,
    created_at: str,
    tenant_id: str,
    version: int = 1,
) -> dict[str, object]:
    if version == 5:
        return manifest_v5(
            dict(files), tenant_id=tenant_id, export_id=export_id, created_at=created_at
        )
    if version not in {1, 2, 3, 4}:
        raise ValueError("unsupported Portable Brain schema")
    return {
        "compatibility": {
            "maximum_contract_version": str(version),
            "minimum_contract_version": "1",
        },
        "contract_version": str(version),
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": relative, "sha256": sha256(payload).hexdigest()} for relative, payload in files
        ],
        "layout_version": version,
        "schema_catalog_digest": {
            1: PORTABLE_V1_SCHEMA_CATALOG_DIGEST,
            2: PORTABLE_V2_SCHEMA_CATALOG_DIGEST,
            3: PORTABLE_V3_SCHEMA_CATALOG_DIGEST,
            4: catalog_digest(dict(files)),
        }[version],
        "schema_version": version,
        "tenant_id": tenant_id,
    }


def _promotion_lease(
    destination: Path,
    actor_id: str,
    parent_identity: RootIdentity,
) -> FileLease:
    identity = "portability-" + sha256(actor_id.encode("utf-8")).hexdigest()[:32]
    return FileLease(
        destination.parent,
        identity,
        root_identity=parent_identity,
        required_root_mode=0o700,
    )


def _manifest_digest(manifest: dict[str, object]) -> str:
    return sha256(portable_canonical_json_bytes(manifest)).hexdigest()


def _ready_record(
    manifest: dict[str, object],
    *,
    import_id: str,
    materialization: Materialization,
    index: IndexBuild,
) -> dict[str, object]:
    ready: dict[str, object] = {
        "import_id": import_id,
        "index": {"generation": index.generation, "state": "complete"},
        "materialization": {
            "counts": {
                "batches": materialization.batches,
                "blobs": materialization.blobs,
                "captures": materialization.captures,
                "history_records": materialization.history_records,
            },
            "state": "complete",
        },
        "schema_version": 1,
        "source_manifest": {
            "digest_sha256": _manifest_digest(manifest),
            "export_id": manifest["export_id"],
            "tenant_id": manifest["tenant_id"],
        },
    }
    if manifest["schema_version"] == 5:
        from .local_schema import open_local_database_read_only

        connection = open_local_database_read_only(materialization.profile)
        try:
            evidence = serialize_portable_v5_state(
                connection,
                tenant_id=materialization.profile.tenant_id,
                relationship_sidecar_present=_has_relationships(manifest),
            )
            ready.update(
                schema_version=2,
                authoritative_counts=_authoritative_counts(connection),
                semantic_state_sha256=evidence.semantic_state_sha256,
                index={
                    "generation": index.generation,
                    "documents": index.documents,
                    "state": "complete",
                },
            )
        finally:
            connection.close()
    return ready


# Portable durable rows and their authoritative search projection. Local replay,
# migration bookkeeping, FTS identities and index generations are not portable.
_AUTHORITATIVE_TABLES = (
    "captures",
    "spaces",
    "proposal_sets",
    "proposals",
    "decisions",
    "review_contexts",
    "review_sources",
    "review_page_heads",
    "logical_sources",
    "source_revisions",
    "canonical_revision_members",
    "revision_relationships",
    "relationship_decisions",
    "source_revision_privacy",
    "canonical_revision_privacy",
    "privacy_invalid_evidence",
    "privacy_repair_ledger",
    "brain_identity",
    "legacy_issuer_bindings",
    "issuer_migration_marker",
    "search_documents",
    "managed_workspaces",
    "managed_notes",
    "managed_note_revisions",
    "managed_links",
    "managed_conflicts",
    "managed_consents",
    "managed_exclusions",
    "managed_inference_budgets",
)


def _authoritative_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in _AUTHORITATIVE_TABLES
    }


def _has_relationships(manifest: dict[str, object]) -> bool:
    return any(
        entry["path"] == RELATIONSHIP_METADATA_PATH
        for entry in cast(list[dict[str, object]], manifest["files"])
    )


def _read_ready_record(
    destination: Path, expected_root_identity: RootIdentity | None = None
) -> dict[str, object]:
    try:
        payload = read_confined(
            root=destination,
            relative=".open-brain/state/portability-ready.json",
            expected_root_identity=expected_root_identity,
        )
        if payload is None:
            raise ValueError
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("portable import retry evidence is invalid") from error
    if not isinstance(value, dict) or portable_canonical_json_bytes(value) != payload:
        raise ValueError("portable import retry evidence is invalid")
    return cast(dict[str, object], value)


def _materialization_counts(manifest: dict[str, object]) -> dict[str, int]:
    paths = [cast(str, entry["path"]) for entry in cast(list[dict[str, object]], manifest["files"])]
    return {
        "batches": sum(path.startswith("sources/batches/") for path in paths),
        "blobs": sum(path.startswith("sources/blobs/") for path in paths),
        "captures": sum(path.startswith("sources/captures/") for path in paths),
        "history_records": sum(path.startswith("history/") for path in paths),
    }


def _validate_ready_record(
    manifest: dict[str, object], *, import_id: str, ready: dict[str, object]
) -> tuple[dict[str, int], int]:
    v5 = manifest["schema_version"] == 5
    extra_keys = {"authoritative_counts", "semantic_state_sha256"} if v5 else set()
    if (
        set(ready)
        != {
            "import_id",
            "index",
            "materialization",
            "schema_version",
            "source_manifest",
        }
        | extra_keys
        or ready.get("import_id") != import_id
        or type(ready.get("schema_version")) is not int
        or ready.get("schema_version") != (2 if v5 else 1)
    ):
        raise ValueError("portable import retry evidence is invalid")
    source_manifest = ready.get("source_manifest")
    index = ready.get("index")
    materialization = ready.get("materialization")
    if (
        not isinstance(source_manifest, dict)
        or source_manifest
        != {
            "digest_sha256": _manifest_digest(manifest),
            "export_id": manifest["export_id"],
            "tenant_id": manifest["tenant_id"],
        }
        or not isinstance(index, dict)
        or set(index) != ({"generation", "state", "documents"} if v5 else {"generation", "state"})
        or type(index.get("generation")) is not int
        or index["generation"] < 1
        or index.get("state") != "complete"
        or not isinstance(materialization, dict)
        or set(materialization) != {"counts", "state"}
        or materialization.get("state") != "complete"
        or materialization.get("counts") != _materialization_counts(manifest)
    ):
        raise ValueError("portable import retry evidence is invalid")
    if v5:
        counts = ready["authoritative_counts"]
        digest = ready["semantic_state_sha256"]
        if (
            not isinstance(counts, dict)
            or set(counts) != set(_AUTHORITATIVE_TABLES)
            or any(type(value) is not int or value < 0 for value in counts.values())
            or type(index.get("documents")) is not int
            or index["documents"] != counts["search_documents"]
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("portable import retry evidence is invalid")
    return _materialization_counts(manifest), cast(int, index["generation"])


def _reject_containment(source: Path, destination: Path) -> None:
    try:
        source_real = source.resolve(strict=True)
        destination_real = destination.resolve(strict=False)
    except OSError as error:
        raise ValueError("portable source and destination cannot be resolved") from error
    if (
        source_real == destination_real
        or source_real.is_relative_to(destination_real)
        or destination_real.is_relative_to(source_real)
    ):
        raise ValueError("portable source and destination must not contain one another")


def _target_manifest_matches(
    source: dict[str, object],
    destination: Path,
    destination_identity: RootIdentity,
) -> bool:
    try:
        target = validate_portable_root(
            destination,
            expected_root_identity=destination_identity,
        )
    except ValueError:
        return False
    return target == source


def _destination_parent_identity(
    destination: Path,
    source_identity: RootIdentity,
    *,
    expected_identity: RootIdentity | None = None,
) -> RootIdentity:
    try:
        return capture_destination_parent(
            destination,
            forbidden_ancestor_identity=source_identity,
            expected_identity=expected_identity,
        )
    except StagingError as error:
        raise ValueError("portable destination parent is unsafe") from error


def _destination_identity(
    destination: Path,
    parent_identity: RootIdentity,
) -> RootIdentity | None:
    try:
        return destination_child_identity(
            destination,
            parent_identity=parent_identity,
        )
    except StagingError as error:
        raise ValueError("portable destination is unsafe") from error


def _index_generation(root: Path, expected_root_identity: RootIdentity | None = None) -> int | None:
    try:
        connection = connect_database_read_only(
            root=root,
            database_name=".open-brain/indexes/search.sqlite3",
            expected_root_identity=expected_root_identity,
        )
    except SchemaError:
        return None
    try:
        row = connection.execute(
            "SELECT generation FROM index_metadata WHERE singleton = 1"
        ).fetchone()
        return int(row[0]) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _validate_reopened_import(
    destination: Path,
    expected_snapshot: PortableSnapshot,
    *,
    import_id: str,
    expected_ready: dict[str, object],
    expected_root_identity: RootIdentity | None = None,
) -> None:
    snapshot = validated_portable_snapshot(
        destination,
        expected_root_identity=expected_root_identity,
    )
    if snapshot.files != expected_snapshot.files:
        raise ValueError("portable import retry evidence is invalid")
    manifest = expected_snapshot.manifest
    if _read_ready_record(destination, expected_root_identity) != expected_ready:
        raise ValueError("portable import retry evidence is invalid")
    counts, index_generation = _validate_ready_record(
        manifest,
        import_id=import_id,
        ready=expected_ready,
    )
    if _index_generation(destination, expected_root_identity) != index_generation:
        raise ValueError("portable import retry evidence is invalid")
    profile = _profile(destination, snapshot)
    if profile.tenant_id != manifest["tenant_id"]:
        raise ValueError("portable import retry evidence is invalid")
    if manifest["schema_version"] == 5:
        # Reject archive-bound drift before engine recovery can rewrite it.
        audit_restored_v5(profile, snapshot=snapshot)
    from .local import BrainEngine

    reopened = BrainEngine.open(profile)
    connection = reopened._store.connect()
    try:
        row = connection.execute("SELECT COUNT(*) FROM captures").fetchone()
        if manifest["schema_version"] == 5:
            verify_portable_v5_semantic_state(
                connection,
                tenant_id=profile.tenant_id,
                relationship_sidecar_present=_has_relationships(manifest),
                expected_sha256=cast(str, expected_ready["semantic_state_sha256"]),
            )
            if _authoritative_counts(connection) != expected_ready["authoritative_counts"]:
                raise ValueError("portable import retry authoritative counts differ")
            managed = export_managed_workspace_state(reopened, connection=connection)
            expected_managed = [
                (path, payload)
                for path, payload in snapshot.files.items()
                if path.startswith("history/managed-workspace/")
            ]
            if ([] if managed is None else [managed]) != expected_managed:
                raise ValueError("portable import retry managed evidence differs")
            index_connection = connect_database_read_only(
                root=destination,
                database_name=".open-brain/indexes/search.sqlite3",
                expected_root_identity=expected_root_identity,
            )
            try:
                query = (
                    "SELECT result_id, capture_id, record_type, payload_family, space_id, "
                    "title, body, trust, provenance_json, canonical_path, updated_at "
                    "FROM search_documents ORDER BY result_id"
                )
                authoritative = [tuple(item) for item in connection.execute(query)]
                indexed = [tuple(item) for item in index_connection.execute(query)]
                if indexed != authoritative:
                    raise ValueError("portable import retry index evidence differs")
            finally:
                index_connection.close()
    finally:
        connection.close()
    if row is None:
        raise ValueError("portable import retry evidence is invalid")
    if (
        int(row[0]) != counts["captures"]
        or _read_ready_record(destination, expected_root_identity).get("import_id") != import_id
    ):
        raise ValueError("portable import retry evidence is invalid")


def _matching_portable_snapshot(
    root: Path,
    *,
    expected_root_identity: RootIdentity,
    expected_snapshot: PortableSnapshot,
) -> PortableSnapshot:
    observed = validated_portable_snapshot(
        root,
        expected_root_identity=expected_root_identity,
    )
    if observed.files != expected_snapshot.files:
        raise ValueError("staged Portable bytes changed before promotion")
    return observed


class PortabilityTasks:
    """Public task capability; it returns receipts and never filesystem references."""

    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine
        engine.__dict__["_portable_writes"] = LocalPortableWrites(
            root=engine.profile.root,
            tenant_id=engine.profile.tenant_id,
            root_identity=engine.profile.root_identity,
        )

    def validate(self, source: Path) -> PortabilityReceipt:
        self._engine._assert_root()
        manifest = validate_portable_root(source)
        return _receipt(manifest, status="validated")

    def export(
        self,
        destination: Path,
        *,
        export_id: str,
        authority: EffectiveAuthority | None = None,
    ) -> PortabilityReceipt:
        if authority is not None and not authority.owner:
            raise T03Error("unsupported_capability")
        self._engine._assert_root()
        _portable_id(export_id, "export")
        _reject_containment(self._engine.profile.root, destination)
        source_identity = self._engine.profile.root_identity
        parent_identity = _destination_parent_identity(
            destination,
            source_identity,
        )
        with (
            self._engine._writer_lease.acquire_shared_writer(),
            _promotion_lease(
                destination,
                self._engine.profile.owner_actor_id,
                parent_identity,
            ).acquire(LockScope.PORTABILITY_PROMOTION),
        ):
            _destination_parent_identity(
                destination,
                source_identity,
                expected_identity=parent_identity,
            )
            destination_identity = _destination_identity(destination, parent_identity)
            if destination_identity is not None:
                existing = validate_portable_root(
                    destination,
                    expected_root_identity=destination_identity,
                )
                if existing.get("export_id") == export_id:
                    return _receipt(existing, status="exported", duplicate=True)
                raise ValueError("portable export destination conflicts")
            return self._export(
                destination,
                export_id,
                parent_identity=parent_identity,
                source_identity=source_identity,
            )

    def _export(
        self,
        destination: Path,
        export_id: str,
        *,
        parent_identity: RootIdentity,
        source_identity: RootIdentity,
    ) -> PortabilityReceipt:
        _, _, _, _, storage, protection = local_portability_ports(
            self._engine.profile.root,
            self._engine.profile.tenant_id,
            self._engine.profile.root_identity,
        )
        if protection.declaration().encrypted:
            raise ValueError("local Portable export protection is unsupported")
        files = [
            (relative, payload)
            for relative, payload in storage.portable_files()
            if (
                relative == "brain.toml"
                or relative.startswith(("content/", "history/", "sources/"))
            )
            and not relative.startswith("history/managed-workspace/")
        ]
        if not files or files[0][0] != "brain.toml":
            raise ValueError("local Portable profile is unavailable")
        # connect() starts one explicit read transaction; every database-backed
        # payload below is derived from that view while the writer fence is held.
        connection = self._engine._store.connect()
        try:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            managed = export_managed_workspace_state(self._engine, connection=connection)
            if managed is not None:
                files.append(managed)
            if schema_version >= 7:
                from .relationship_store import relationship_metadata
                from .source_store import source_metadata

                relation = relationship_metadata(connection)
                relationship_present = any(path == RELATIONSHIP_METADATA_PATH for path, _ in files)
                if schema_version == 9 and relationship_present and relation is None:
                    relation = {"schema_version": 1, "relationships": [], "decisions": []}
                files = [(path, data) for path, data in files if path != RELATIONSHIP_METADATA_PATH]
                if relation is not None:
                    files.append(
                        (RELATIONSHIP_METADATA_PATH, portable_canonical_json_bytes(relation))
                    )
                files = [(path, data) for path, data in files if path != SOURCE_METADATA_PATH]
                files.append(
                    (
                        SOURCE_METADATA_PATH,
                        portable_canonical_json_bytes(source_metadata(connection)),
                    )
                )
                if schema_version == 9:
                    evidence = serialize_portable_v5_state(
                        connection,
                        tenant_id=self._engine.profile.tenant_id,
                        relationship_sidecar_present=relation is not None,
                    )
                    files = [(path, data) for path, data in files if path not in V5_SIDECAR_PATHS]
                    files.extend(evidence.sidecars.items())
        finally:
            connection.close()
        files.sort(key=lambda item: item[0])
        has_review_bindings = any(
            relative.startswith("history/review-bindings/") for relative, _ in files
        )
        manifest = _manifest(
            files,
            export_id=export_id,
            created_at=_timestamp(self._engine._clock()),
            tenant_id=self._engine.profile.tenant_id,
            version=5
            if schema_version == 9
            else 4
            if any(path == SOURCE_METADATA_PATH for path, _ in files)
            else 3
            if has_review_bindings
            else 2
            if managed is not None
            else 1,
        )
        try:
            with sibling_stage(
                destination,
                expected_parent_identity=parent_identity,
                forbidden_ancestor_identity=source_identity,
            ) as stage:
                self._engine._fault(PortabilityFault.AFTER_STAGE_CREATED)
                for relative, payload in files:
                    stage.write_bytes(relative, payload)
                    self._engine._fault(PortabilityFault.AFTER_PORTABLE_FILE)
                stage.write_bytes(
                    "portable-manifest.json",
                    portable_canonical_json_bytes(manifest),
                )
                self._engine._fault(PortabilityFault.AFTER_MANIFEST)
                stage_root = stage.root
                stage_identity = stage.identity
                stage_snapshot = validated_portable_snapshot(
                    stage_root,
                    expected_root_identity=stage_identity,
                )
                stage.assert_identity()
                self._engine._fault(PortabilityFault.BEFORE_PROMOTION)

                def verify_staged_export() -> None:
                    _matching_portable_snapshot(
                        stage_root,
                        expected_root_identity=stage_identity,
                        expected_snapshot=stage_snapshot,
                    )

                stage.promote(pre_rename=verify_staged_export)
                self._engine._fault(PortabilityFault.AFTER_PROMOTION)
        except StagingError as error:
            raise ValueError("portable export staging failed") from error
        return _receipt(manifest, status="exported")

    def import_clean(
        self, source: Path, destination: Path, *, import_id: str
    ) -> PortabilityReceipt:
        self._engine._assert_root()
        _portable_id(import_id, "import")
        _reject_containment(source, destination)
        try:
            source_root = source.resolve(strict=True)
        except OSError as error:
            raise ValueError("portable import source cannot be resolved") from error
        source_identity = capture_root_identity(source_root)
        source_snapshot = validated_portable_snapshot(
            source_root,
            expected_root_identity=source_identity,
        )
        manifest = source_snapshot.manifest
        if manifest["schema_version"] == 4:
            raise ValueError("Portable v4 import is not supported")
        parent_identity = _destination_parent_identity(
            destination,
            source_identity,
        )
        with _promotion_lease(
            destination,
            self._engine.profile.owner_actor_id,
            parent_identity,
        ).acquire(LockScope.PORTABILITY_PROMOTION):
            _destination_parent_identity(
                destination,
                source_identity,
                expected_identity=parent_identity,
            )
            destination_identity = _destination_identity(destination, parent_identity)
            if destination_identity is None:
                return self._import_clean(
                    destination,
                    import_id,
                    source_snapshot,
                    source_identity,
                    parent_identity,
                )
            if _target_manifest_matches(manifest, destination, destination_identity):
                expected_ready = _read_ready_record(destination, destination_identity)
                _validate_reopened_import(
                    destination,
                    source_snapshot,
                    import_id=import_id,
                    expected_ready=expected_ready,
                    expected_root_identity=destination_identity,
                )
                return _receipt(
                    manifest,
                    status="imported",
                    duplicate=True,
                    index_generation=cast(
                        int,
                        cast(dict[str, object], expected_ready["index"])["generation"],
                    ),
                )
            raise ValueError("portable import destination conflicts")

    def _import_clean(
        self,
        destination: Path,
        import_id: str,
        source_snapshot: PortableSnapshot,
        source_identity: RootIdentity,
        parent_identity: RootIdentity,
    ) -> PortabilityReceipt:
        manifest = source_snapshot.manifest
        entries = cast(list[dict[str, object]], manifest["files"])
        try:
            with sibling_stage(
                destination,
                expected_parent_identity=parent_identity,
                forbidden_ancestor_identity=source_identity,
            ) as stage:
                self._engine._fault(PortabilityFault.AFTER_STAGE_CREATED)
                for entry in entries:
                    relative = cast(str, entry["path"])
                    try:
                        payload = source_snapshot.files[relative]
                    except KeyError:
                        raise ValueError("portable import source changed") from None
                    stage.write_bytes(relative, payload)
                    self._engine._fault(PortabilityFault.AFTER_PORTABLE_FILE)
                stage.write_bytes(
                    "portable-manifest.json",
                    portable_canonical_json_bytes(manifest),
                )
                self._engine._fault(PortabilityFault.AFTER_MANIFEST)
                stage_root = stage.root
                stage_identity = stage.identity
                stage_snapshot = validated_portable_snapshot(
                    stage_root,
                    expected_root_identity=stage_identity,
                )
                if stage_snapshot.files != source_snapshot.files:
                    raise ValueError("portable import stage differs from its source snapshot")
                stage.assert_identity()
                self._engine._fault(PortabilityFault.AFTER_PROFILE)
                restore = (
                    restore_portable_v5_root
                    if manifest["schema_version"] == 5
                    else materialize_portable_root
                )
                materialization = restore(
                    stage_root,
                    snapshot=stage_snapshot,
                    expected_root_identity=stage_identity,
                )
                if manifest["schema_version"] == 5:
                    materialization = replace(
                        materialization,
                        history_records=_materialization_counts(manifest)["history_records"],
                    )
                if manifest["schema_version"] in {2, 3, 5}:
                    managed_paths = [
                        path
                        for path in stage_snapshot.files
                        if path.startswith("history/managed-workspace/")
                    ]
                    if manifest["schema_version"] == 2 and len(managed_paths) != 1:
                        raise ValueError("Portable v2 managed state is unavailable")
                    if managed_paths:
                        if len(managed_paths) != 1:
                            raise ValueError("Portable managed state is invalid")
                        from .local import BrainEngine

                        staged_engine = BrainEngine.open(materialization.profile)
                        import_managed_workspace_state(
                            staged_engine,
                            stage_snapshot.files[managed_paths[0]],
                        )
                        if manifest["schema_version"] != 5:
                            materialization = replace(
                                materialization,
                                history_records=materialization.history_records + 1,
                            )
                stage.assert_identity()
                self._engine._fault(PortabilityFault.AFTER_MATERIALIZATION)
                index = rebuild_portable_index(materialization.profile)
                self._engine._fault(PortabilityFault.AFTER_INDEX)
                ready = _ready_record(
                    manifest,
                    import_id=import_id,
                    materialization=materialization,
                    index=index,
                )
                stage.write_bytes(
                    ".open-brain/state/portability-ready.json",
                    portable_canonical_json_bytes(ready),
                )
                _validate_reopened_import(
                    stage_root,
                    stage_snapshot,
                    import_id=import_id,
                    expected_ready=ready,
                    expected_root_identity=stage_identity,
                )
                stage.assert_identity()
                self._engine._fault(PortabilityFault.AFTER_READY)
                self._engine._fault(PortabilityFault.BEFORE_PROMOTION)

                def verify_staged_import() -> None:
                    _validate_reopened_import(
                        stage_root,
                        stage_snapshot,
                        import_id=import_id,
                        expected_ready=ready,
                        expected_root_identity=stage_identity,
                    )

                stage.promote(pre_rename=verify_staged_import)
                self._engine._fault(PortabilityFault.AFTER_PROMOTION)
        except StagingError as error:
            raise ValueError("portable import staging failed") from error
        return _receipt(
            manifest,
            status="imported",
            index_generation=index.generation,
        )

    def rebuild_index(self) -> PortabilityReceipt:
        self._engine._assert_root()
        with self._engine._writer_lease.acquire_shared_writer():
            lease_identity = (
                "portable-index-"
                + sha256(self._engine.profile.owner_actor_id.encode("utf-8")).hexdigest()[:32]
            )
            lease = FileLease(
                self._engine.profile.root / ".open-brain",
                lease_identity,
                clock=self._engine._clock,
                parent_root_identity=self._engine.profile.root_identity,
            )
            with lease.acquire(LockScope.INDEX):
                index = rebuild_portable_index(self._engine.profile)
        return self._rebuild_receipt(index)

    def _rebuild_receipt(self, index: IndexBuild) -> PortabilityReceipt:
        storage = LocalTenantStorage(
            root=self._engine.profile.root,
            tenant_id=self._engine.profile.tenant_id,
            root_identity=self._engine.profile.root_identity,
        )
        files = [
            (relative, payload)
            for relative, payload in storage.portable_files()
            if relative != "portable-manifest.json"
            and (
                relative == "brain.toml"
                or relative.startswith(("content/", "history/", "sources/"))
            )
            and not relative.startswith("history/managed-workspace/")
        ]
        manifest = _manifest(
            files,
            export_id="export_00000000-0000-4000-8000-000000000000",
            created_at="1970-01-01T00:00:00Z",
            tenant_id=self._engine.profile.tenant_id,
            version=(
                5
                if all(any(path == sidecar for path, _ in files) for sidecar in V5_SIDECAR_PATHS)
                else 4
                if any(path == SOURCE_METADATA_PATH for path, _ in files)
                else 3
                if any(relative.startswith("history/review-bindings/") for relative, _ in files)
                else 1
            ),
        )
        return _receipt(manifest, status="rebuilt", index_generation=index.generation)
