"""Clean-schema restore of already validated Portable v5 evidence.

The caller owns stage creation, portable file copying, index rebuild and promotion.
No restore operation updates the identity of an existing database.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.relationships_v1 import RELATIONSHIP_METADATA_PATH
from open_brain_engine.portable.v1 import PortableSnapshot
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH
from open_brain_engine.storage.filesystem import RootIdentity

from .contracts import LocalEngineContext
from .issuer_state import verify_issuer_evidence
from .privacy_migration import _verify_privacy_projections
from .privacy_projection import (
    RetainedPrivacyEvidence,
    effective_privacy_json,
    project_retained_privacy_evidence,
)
from .privacy_repairs import PrivacyRepairReceipt, _append_repair_row, audit_privacy_repair_ledger
from .relationship_store import relationship_metadata
from .source_store import source_metadata

if TYPE_CHECKING:
    from .local import BrainEngine
    from .materializer import Materialization


@dataclass(frozen=True, slots=True)
class ValidatedV5IssuerSeed:
    """Immutable issuer sidecar bytes from an already validated v5 snapshot."""

    migration_bytes: bytes
    bindings_bytes: bytes

    def install(self, connection: sqlite3.Connection, *, tenant_id: str | None) -> None:
        migration = json.loads(self.migration_bytes)
        bindings = json.loads(self.bindings_bytes)["bindings"]
        if migration["tenant_id"] != tenant_id:
            raise ValueError("v5 issuer tenant mismatch")
        connection.execute(
            "INSERT INTO brain_identity VALUES (1,?,?,?,?,?)",
            (
                tenant_id,
                migration["brain_id"],
                migration["current_issuer_epoch"],
                migration["legacy_issuer_epoch"],
                migration["identity_recorded_at"],
            ),
        )
        connection.executemany(
            "INSERT INTO legacy_issuer_bindings VALUES (?,?,?,?)",
            [
                (
                    row["artifact_path"],
                    row["jsonl_ordinal"],
                    row["payload_sha256"],
                    row["issuer_epoch"],
                )
                for row in bindings
            ],
        )
        marker = migration["migration_marker"]
        if marker is not None:
            connection.execute(
                "INSERT INTO issuer_migration_marker VALUES (1,?,?,?,?,?,?,?)",
                (
                    base64.b64decode(
                        marker["source_portable_manifest_bytes_base64"], validate=True
                    ),
                    marker["source_portable_manifest_sha256"],
                    marker["brain_id"],
                    marker["designated_legacy_issuer_epoch"],
                    marker["current_issuer_epoch"],
                    marker["legacy_binding_manifest_sha256"],
                    marker["recorded_at"],
                ),
            )
        assert isinstance(tenant_id, str)
        verify_issuer_evidence(connection, tenant_id=tenant_id)


@dataclass(frozen=True, slots=True)
class V5RestoreBundle:
    """One validated snapshot, never a second read of the export directory."""

    snapshot: PortableSnapshot
    issuer_seed: ValidatedV5IssuerSeed
    checkpoint: Callable[[str], None]

    @classmethod
    def decode(
        cls, snapshot: PortableSnapshot, *, checkpoint: Callable[[str], None]
    ) -> V5RestoreBundle:
        from open_brain_engine.portable.v5 import (
            EFFECTIVE_PRIVACY_PATH,
            ISSUER_MIGRATION_PATH,
            LEGACY_BINDINGS_PATH,
            decode_retained_privacy_value,
        )

        if snapshot.manifest["schema_version"] != 5:
            raise ValueError("v5 restore requires a validated v5 snapshot")
        for row in json.loads(snapshot.files[EFFECTIVE_PRIVACY_PATH])["retained_privacy"]:
            value = decode_retained_privacy_value(row["privacy_json"])
            if type(value) in (int, float):
                raise ValueError(
                    "Portable v5 numeric retained privacy cannot be restored to schema 9: "
                    "captures.privacy_json has TEXT affinity"
                )
        return cls(
            snapshot,
            ValidatedV5IssuerSeed(
                snapshot.files[ISSUER_MIGRATION_PATH], snapshot.files[LEGACY_BINDINGS_PATH]
            ),
            checkpoint,
        )

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        from open_brain_engine.portable.v5 import (
            EFFECTIVE_PRIVACY_PATH,
            decode_retained_privacy_value,
            encode_retained_privacy_value,
        )

        files = self.snapshot.files
        privacy = json.loads(files[EFFECTIVE_PRIVACY_PATH])
        self.checkpoint("base_materialized")
        for row in privacy["retained_privacy"]:
            connection.execute(
                "UPDATE captures SET privacy_json=? WHERE capture_id=?",
                (decode_retained_privacy_value(row["privacy_json"]), row["capture_id"]),
            )
            stored = connection.execute(
                "SELECT privacy_json FROM captures WHERE capture_id=?", (row["capture_id"],)
            ).fetchone()
            if stored is None or encode_retained_privacy_value(stored[0]) != row["privacy_json"]:
                raise ValueError("v5 retained privacy storage mismatch")
        metadata = json.loads(files[SOURCE_METADATA_PATH])
        for source in metadata["sources"]:
            connection.execute(
                "INSERT INTO logical_sources VALUES (?,?,?,?,?,?,?,?)",
                tuple(
                    source[key]
                    for key in (
                        "source_id",
                        "head_capture_id",
                        "historical_only",
                        "space_id",
                        "route_version",
                        "head_version",
                        "lifecycle",
                        "availability",
                    )
                ),
            )
            # Legacy base materialization sees routes by capture. V4 owns the
            # current route, which must already agree at transaction-exit registration.
            connection.execute(
                "UPDATE captures SET space_id=? WHERE capture_id=?",
                (source["space_id"], source["head_capture_id"]),
            )
        for revision in metadata["revisions"]:
            payload = files[revision["source_path"]]
            if sha256(payload).hexdigest() != revision["source_sha256"]:
                raise ValueError("v5 source revision digest mismatch")
            connection.execute(
                "INSERT INTO source_revisions VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
                (
                    revision["capture_id"],
                    revision["source_id"],
                    revision["sequence"],
                    revision["predecessor_capture_id"],
                    revision["source_path"],
                    revision["source_sha256"],
                    payload,
                    revision["recorded_at"],
                    revision["diagnostic"],
                ),
            )
        for member in metadata["canonical_members"]:
            connection.execute(
                "INSERT INTO canonical_revision_members VALUES (?,?,?,?,?)",
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
        _restore_relationships(connection, files.get(RELATIONSHIP_METADATA_PATH))
        self.checkpoint("source_history_restored")
        for row in privacy["base_projections"]:
            if row["target_kind"] == "source_revision":
                table = "source_revision_privacy"
                values = [
                    connection.execute(
                        "SELECT privacy_json FROM captures WHERE capture_id=?", (row["target_id"],)
                    ).fetchone()[0]
                ]
            else:
                table = "canonical_revision_privacy"
                values = [
                    item[0]
                    for item in connection.execute(
                        "SELECT c.privacy_json FROM canonical_revision_members m "
                        "JOIN captures c USING(capture_id) "
                        "WHERE m.revision_id=? ORDER BY m.ordinal",
                        (row["target_id"],),
                    )
                ]
            expected = canonical(row["effective_privacy"]).decode()
            if effective_privacy_json(project_retained_privacy_evidence(values)) != expected:
                raise ValueError("v5 immutable privacy base mismatch")
            connection.execute(f"INSERT INTO {table} VALUES (?,?)", (row["target_id"], expected))
        connection.executemany(
            "INSERT INTO privacy_invalid_evidence VALUES (?,?,?,?)",
            [
                tuple(
                    row[key]
                    for key in (
                        "target_kind",
                        "target_id",
                        "invalid_reason",
                        "invalid_evidence_sha256",
                    )
                )
                for row in privacy["invalid_evidence"]
            ],
        )
        for row in privacy["repairs"]:
            _append_repair_row(connection, receipt=PrivacyRepairReceipt.decode(row))
        audit_privacy_repair_ledger(connection)
        self.checkpoint("privacy_evidence_restored")
        _rederive_search(connection, profile, portable_files=files)
        self.checkpoint("search_rederived")
        _audit_connection(connection, profile=profile, snapshot=self.snapshot)


def _restore_relationships(connection: sqlite3.Connection, payload: bytes | None) -> None:
    if payload is None:
        return
    metadata = json.loads(payload)
    for row in metadata["relationships"]:
        connection.execute(
            "INSERT INTO revision_relationships VALUES (?,?,?,?,?,?,?,?)",
            (
                row["relationship_id"],
                row["left"]["record_id"],
                row["left"]["revision_id"],
                row["right"]["record_id"],
                row["right"]["revision_id"],
                row["kind"],
                {"accepted": "accept", "rejected": "reject", "removed": "remove"}[row["status"]],
                row["version"],
            ),
        )
    for row in metadata["decisions"]:
        # V4 never exported these replay fields. Domain-separated deterministic
        # placeholders cannot be mistaken for a historical owner operation.
        digest = sha256(
            canonical(
                {
                    "domain": "open-brain.portable-v5.relationship-replay.v1",
                    "decision": row,
                }
            )
        ).hexdigest()
        receipt = canonical(
            {
                "status": "ok",
                "dto_version": 1,
                "relationship_id": row["relationship_id"],
                "decision_id": row["decision_id"],
                "version": row["version"],
            }
        ).decode()
        connection.execute(
            "INSERT INTO relationship_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["decision_id"],
                row["relationship_id"],
                row["sequence"],
                "import.relationship." + digest,
                digest,
                row["decision"],
                row["version"],
                row["recorded_at"],
                row["actor_id"],
                receipt,
            ),
        )


def _rederive_search(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    portable_files: Mapping[str, bytes],
) -> None:
    from .reconciliation import _projection_inputs
    from .search_projection import upsert_search_document

    # The shared reader needs only the profile; constructing a live engine here
    # would open another connection before this restore transaction commits.
    context = cast("BrainEngine", SimpleNamespace(profile=profile))
    for item in _projection_inputs(context, connection, portable_files=portable_files):
        if (
            item.record_type == "source"
            and connection.execute(
                "SELECT 1 FROM logical_sources WHERE head_capture_id=? AND historical_only=0 "
                "AND lifecycle='active' AND availability='available'",
                (item.capture_id,),
            ).fetchone()
            is None
        ):
            continue
        upsert_search_document(
            connection,
            result_id=item.result_id,
            capture_id=item.capture_id,
            record_type=item.record_type,
            payload_family=item.payload_family,
            space_id=item.space_id,
            title=item.title,
            body=item.body,
            canonical_path=item.canonical_path,
            updated_at=item.updated_at,
        )


def _audit_connection(
    connection: sqlite3.Connection, *, profile: LocalEngineContext, snapshot: PortableSnapshot
) -> None:
    from .portable_v5_evidence import serialize_portable_v5_state

    verify_issuer_evidence(connection, tenant_id=profile.tenant_id)
    _verify_privacy_projections(connection)
    if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
        raise ValueError("v5 restored database integrity mismatch")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("v5 restored database reference mismatch")
    if canonical(source_metadata(connection)) != snapshot.files[SOURCE_METADATA_PATH]:
        raise ValueError("v5 restored source metadata mismatch")
    relationships = relationship_metadata(connection)
    expected = snapshot.files.get(RELATIONSHIP_METADATA_PATH)
    if relationships is None and expected is not None:
        relationships = {"schema_version": 1, "relationships": [], "decisions": []}
    if (None if relationships is None else canonical(relationships)) != expected:
        raise ValueError("v5 restored relationship metadata mismatch")
    _audit_capture_search_content(connection, profile=profile, snapshot=snapshot)
    evidence = serialize_portable_v5_state(
        connection,
        tenant_id=profile.tenant_id,
        relationship_sidecar_present=expected is not None,
    )
    if any(snapshot.files[path] != payload for path, payload in evidence.sidecars.items()):
        raise ValueError("v5 restored semantic evidence mismatch")


def _audit_capture_search_content(
    connection: sqlite3.Connection, *, profile: LocalEngineContext, snapshot: PortableSnapshot
) -> None:
    """Bind durable content to captured inputs without repairing any database row."""
    from .materializer import portable_capture_content
    from .portable_v5_evidence import (
        normalized_authoritative_search_rows,
        normalized_capture_rows,
    )
    from .privacy_repairs import resolve_search_privacy
    from .reconciliation import _projection_inputs
    from .search_projection import project_search_document

    if canonical(normalized_capture_rows(connection)) != canonical(
        portable_capture_content(snapshot.files)
    ):
        raise ValueError("v5 restored capture content mismatch")

    # The shared reader may consult capture content only after the archive-bound
    # comparison above. Otherwise corruption could certify itself as the baseline.
    context = cast("BrainEngine", SimpleNamespace(profile=profile))
    active_sources = {
        source["head_capture_id"]
        for source in json.loads(snapshot.files[SOURCE_METADATA_PATH])["sources"]
        if not source["historical_only"]
        and source["lifecycle"] == "active"
        and source["availability"] == "available"
    }
    expected_rows: list[dict[str, object]] = []
    for item in _projection_inputs(context, connection, portable_files=snapshot.files):
        if item.record_type == "source" and item.capture_id not in active_sources:
            continue
        projection = project_search_document(
            connection,
            result_id=item.result_id,
            capture_id=item.capture_id,
            record_type=item.record_type,
            title=item.title,
            body=item.body,
            canonical_path=item.canonical_path,
        )
        privacy, applied = resolve_search_privacy(
            connection,
            result_id=item.result_id,
            capture_id=item.capture_id,
            record_type=item.record_type,
        )
        expected_rows.append(
            {
                "result_id": item.result_id,
                "capture_id": item.capture_id,
                "record_type": item.record_type,
                "payload_family": item.payload_family,
                "space_id": item.space_id,
                "title": projection.title,
                "body": projection.body,
                "trust": projection.trust,
                "provenance_json": json.loads(projection.provenance_json),
                "canonical_path": item.canonical_path,
                "updated_at": item.updated_at,
                "effective_privacy": json.loads(
                    effective_privacy_json(cast(RetainedPrivacyEvidence, privacy))
                ),
                "applied_repair_id": None if applied is None else applied[0],
                "applied_repair_sequence": None if applied is None else applied[1],
            }
        )
    if canonical(normalized_authoritative_search_rows(connection)) != canonical(expected_rows):
        raise ValueError("v5 restored authoritative search content mismatch")


def audit_restored_v5(profile: LocalEngineContext, *, snapshot: PortableSnapshot) -> None:
    """Audit committed restore state through a fresh read connection."""
    from .local_schema import open_local_database_read_only

    connection = open_local_database_read_only(profile)
    try:
        _audit_connection(connection, profile=profile, snapshot=snapshot)
    finally:
        connection.close()


def restore_portable_v5_root(
    root: Path,
    *,
    snapshot: PortableSnapshot,
    expected_root_identity: RootIdentity,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> Materialization:
    """Restore one validated, copied v5 snapshot into a new hidden schema-9 stage."""
    from .materializer import materialize_portable_root

    bundle = V5RestoreBundle.decode(snapshot, checkpoint=checkpoint)
    materialization = materialize_portable_root(
        root, snapshot=snapshot, expected_root_identity=expected_root_identity, _v5_restore=bundle
    )
    audit_restored_v5(materialization.profile, snapshot=snapshot)
    return materialization
