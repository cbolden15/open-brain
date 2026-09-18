"""Journaled schema-7 cutover, called only under runtime and legacy writer admission."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.v4 import (
    SOURCE_METADATA_PATH,
    manifest_v4,
    validate_portable_file_set_v4,
)
from open_brain_engine.storage.filesystem import atomic_replace, read_confined
from open_brain_engine.storage.locks import FileLease, LockBusyError
from open_brain_engine.storage.migrations import _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

from .contracts import LocalEngineContext
from .local_schema import PHASE1_STATE_DATABASE, _MigrationClock, classify_local_schema
from .local_schema_catalog import LOCAL_MIGRATIONS
from .runtime_admission import HeldRuntimeAdmission
from .source_inventory import inventory_private_state, inventory_sources
from .source_schema import SOURCE_HISTORY_SCHEMA
from .t03_contracts import T03Error

JOURNAL = ".open-brain/state/source-migration.json"
FENCE = ".open-brain/state/source-migration-fence.json"


def _read(profile: LocalEngineContext, relative: str) -> bytes | None:
    return read_confined(
        root=profile.root, relative=relative, expected_root_identity=profile.root_identity
    )


def _write(profile: LocalEngineContext, relative: str, payload: bytes) -> None:
    atomic_replace(
        root=profile.root,
        relative=relative,
        data=payload,
        expected_root_identity=profile.root_identity,
    )


def migration_pending(profile: LocalEngineContext) -> bool:
    payload = _read(profile, JOURNAL)
    if payload is None:
        return False
    try:
        return bool(json.loads(payload)["stage"] != "complete")
    except KeyError, ValueError, TypeError:
        raise T03Error("operation_pending") from None


def _new_plan(
    profile: LocalEngineContext, connection: sqlite3.Connection, clock: Callable[[], datetime]
) -> dict[str, Any]:
    inventory = inventory_sources(profile, connection)
    private = inventory_private_state(connection)
    sources = []
    revisions = []
    identities = {}
    for capture_id, (path, payload, record) in sorted(inventory.captures.items()):
        source_id = "source_" + str(uuid4())
        identities[capture_id] = source_id
        current = inventory.current_rows.get(capture_id)
        historical_only = current is None
        import_row = connection.execute(
            "SELECT f.active_revision_id=r.revision_id FROM markdown_import_revisions r "
            "JOIN markdown_import_files f USING(file_id) WHERE r.capture_id=?",
            (capture_id,),
        ).fetchone()
        if import_row is not None and not import_row[0]:
            historical_only = True
        sources.append(
            {
                "source_id": source_id,
                "head_capture_id": capture_id,
                "space_id": current["space_id"] if current is not None else record["space_id"],
                "route_version": connection.execute(
                    "SELECT count(*) FROM route_operations WHERE capture_id=?", (capture_id,)
                ).fetchone()[0],
                "head_version": 1,
                "historical_only": historical_only,
                "lifecycle": "active",
                "availability": "available",
            }
        )
        revisions.append(
            {
                "capture_id": capture_id,
                "source_id": source_id,
                "sequence": 1,
                "predecessor_capture_id": None,
                "source_path": path,
                "source_sha256": sha256(payload).hexdigest(),
                "recorded_at": record["accepted_at"],
                "diagnostic": "ungrouped_legacy_history" if historical_only else None,
            }
        )
    metadata = {
        "schema_version": 1,
        "sources": sources,
        "revisions": revisions,
        "canonical_members": [
            dict(
                zip(
                    ("revision_id", "page_id", "publication_id", "ordinal", "capture_id"),
                    row,
                    strict=True,
                )
            )
            for row in inventory.memberships
        ],
    }
    portable_files = {
        path: data
        for path, data in inventory.files.items()
        if path == "brain.toml" or path.startswith(("content/", "history/", "sources/"))
    }
    portable_files[SOURCE_METADATA_PATH] = portable_canonical_json_bytes(metadata)
    manifest = manifest_v4(
        portable_files,
        tenant_id=profile.tenant_id,
        export_id="export_" + str(uuid4()),
        created_at=clock().isoformat().replace("+00:00", "Z"),
    )
    return {
        "version": 1,
        "stage": "journal_durable",
        "root_identity": list(profile.root_identity),
        "old_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "incarnation": str(uuid4()),
        "preimages": inventory.evidence(),
        "private": private,
        "post_private": inventory_private_state(
            connection, publication_paths=inventory.publication_paths
        ),
        "publication_paths": inventory.publication_paths,
        "metadata": metadata,
        "manifest": manifest,
        "aliases": [
            {
                "delivery_id": delivery,
                "source_id": identities[capture_id],
                "evidence_sha256": sha256(
                    portable_canonical_json_bytes(
                        inventory.current_rows[capture_id]["request_sha256"]
                    )
                ).hexdigest(),
            }
            for delivery, capture_id in sorted(inventory.aliases.items())
        ],
    }


def migrate_sources(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _: None,
) -> None:
    admission.validate(profile)
    if admission.live_peer_count:
        raise T03Error("operation_pending")
    lease = FileLease(
        profile.root / ".open-brain",
        "source-migration",
        clock=clock,
        parent_root_identity=profile.root_identity,
    )
    try:
        with lease.acquire_shared_writer():
            _migrate_sources(profile, admission=admission, clock=clock, checkpoint=checkpoint)
    except LockBusyError:
        raise T03Error("operation_pending") from None


def _migrate_sources(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _: None,
) -> None:
    """Finish forward after the old-visible user_version barrier; never replay old writes."""
    admission.validate(profile)
    connection = connect_database(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    try:
        raw = _read(profile, JOURNAL)
        if raw is None:
            state = classify_local_schema(connection)
            if state.state not in {"current", "supported_old"} or state.version not in {5, 6}:
                raise T03Error("operation_pending")
            checkpoint("exclusive_preflight")
            plan = _new_plan(profile, connection, clock)
            _write(profile, JOURNAL, portable_canonical_json_bytes(plan))
            checkpoint("journal_durable")
        else:
            plan = json.loads(raw)
            if (
                plan.get("version") != 1
                or plan.get("root_identity") != list(profile.root_identity)
                or plan.get("old_version") not in {5, 6}
            ):
                raise T03Error("operation_pending")
            if plan["stage"] == "complete":
                return
        fence = _read(profile, FENCE)
        epoch = 1 if fence is None else json.loads(fence)["epoch"] + 1
        _write(profile, FENCE, portable_canonical_json_bytes({"epoch": epoch}))
        # All old evidence must retain its exact preimage on every recovery attempt.
        for entry in plan["preimages"]:
            payload = _read(profile, entry["path"])
            if entry[
                "path"
            ] == "portable-manifest.json" and payload == portable_canonical_json_bytes(
                plan["manifest"]
            ):
                continue
            if (
                payload is None
                or len(payload) != entry["bytes"]
                or sha256(payload).hexdigest() != entry["sha256"]
            ):
                raise T03Error("operation_pending")
        current_private = inventory_private_state(connection)
        installed = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='engine_generations' AND type='table'"
            ).fetchone()
            is not None
        )
        old_tables = (
            plan.get("post_private", plan["private"])["tables"]
            if installed
            else plan["private"]["tables"]
        )
        current_tables = cast(dict[str, object], current_private["tables"])
        if any(current_tables.get(table) != evidence for table, evidence in old_tables.items()):
            raise T03Error("operation_pending")
        if current_private["managed_files"] != plan["private"]["managed_files"]:
            raise T03Error("operation_pending")
        legacy_files = {
            entry["path"]: cast(bytes, _read(profile, entry["path"]))
            for entry in plan["preimages"]
            if entry["path"] == "brain.toml"
            or entry["path"].startswith(("content/", "history/", "sources/"))
        }
        legacy_files[SOURCE_METADATA_PATH] = portable_canonical_json_bytes(plan["metadata"])
        validate_portable_file_set_v4(legacy_files, tenant_id=profile.tenant_id)
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA user_version=7")
        connection.execute("COMMIT")
        checkpoint("old_visible_barrier")
        metadata_bytes = portable_canonical_json_bytes(plan["metadata"])
        existing = _read(profile, SOURCE_METADATA_PATH)
        if existing is not None and existing != metadata_bytes:
            raise T03Error("operation_pending")
        _write(profile, SOURCE_METADATA_PATH, metadata_bytes)
        checkpoint("sidecars_durable")
        connection.execute("BEGIN IMMEDIATE")
        migrations = LOCAL_MIGRATIONS[:6] + (
            _migration(7, "immutable_source_history", SOURCE_HISTORY_SCHEMA),
        )
        apply_migrations(
            connection, clock=_MigrationClock(clock), migrations=migrations, schema_version=7
        )
        existing_generation = connection.execute("SELECT 1 FROM engine_generations").fetchone()
        if existing_generation is None:
            connection.execute(
                "INSERT INTO engine_generations VALUES(1,?,0,0,1,0,?,0)",
                (plan["incarnation"], epoch),
            )
            for source in plan["metadata"]["sources"]:
                connection.execute(
                    "INSERT INTO logical_sources VALUES(?,?,?,?,?,?,?,?)",
                    (
                        source["source_id"],
                        source["head_capture_id"],
                        int(source["historical_only"]),
                        source["space_id"],
                        source["route_version"],
                        source["head_version"],
                        source["lifecycle"],
                        source["availability"],
                    ),
                )
            for revision in plan["metadata"]["revisions"]:
                payload = _read(profile, revision["source_path"])
                connection.execute(
                    "INSERT INTO source_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        revision["capture_id"],
                        revision["source_id"],
                        revision["sequence"],
                        revision["predecessor_capture_id"],
                        revision["source_path"],
                        revision["source_sha256"],
                        payload,
                        None,
                        None,
                        None,
                        revision["recorded_at"],
                        revision["diagnostic"],
                    ),
                )
            for alias in plan["aliases"]:
                connection.execute(
                    "INSERT INTO source_aliases VALUES(?,?,?)",
                    (alias["delivery_id"], alias["source_id"], alias["evidence_sha256"]),
                )
            for member in plan["metadata"]["canonical_members"]:
                connection.execute(
                    "INSERT INTO canonical_revision_members VALUES(?,?,?,?,?)",
                    tuple(
                        member[key]
                        for key in (
                            "revision_id",
                            "page_id",
                            "publication_id",
                            "ordinal",
                            "capture_id",
                        )
                    ),
                )
        connection.execute(
            "UPDATE engine_generations SET fencing_epoch=? WHERE singleton=1", (epoch,)
        )
        for decision_id, path in plan.get("publication_paths", {}).items():
            connection.execute(
                "UPDATE decisions SET publication_path=? WHERE decision_id=?", (path, decision_id)
            )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise T03Error("operation_pending")
        connection.execute("COMMIT")
        checkpoint("schema_committed")
        _write(
            profile,
            ".open-brain/runtime-sessions/registry-version",
            b"open-brain-runtime-sessions-v2\n",
        )
        _write(profile, "portable-manifest.json", portable_canonical_json_bytes(plan["manifest"]))
        checkpoint("metadata_published")
        files = {
            entry["path"]: _read(profile, entry["path"])
            for entry in plan["preimages"]
            if entry["path"] == "brain.toml"
            or entry["path"].startswith(("content/", "history/", "sources/"))
        }
        files[SOURCE_METADATA_PATH] = metadata_bytes
        validate_portable_file_set_v4(cast(dict[str, bytes], files), tenant_id=profile.tenant_id)
        checkpoint("validated")
        plan["stage"] = "complete"
        _write(profile, JOURNAL, portable_canonical_json_bytes(plan))
        checkpoint("complete")
    except ValueError, KeyError, TypeError, sqlite3.Error, OSError:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise T03Error("operation_pending") from None
    finally:
        connection.close()
