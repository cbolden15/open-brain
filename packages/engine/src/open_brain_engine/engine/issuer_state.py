"""Durable Brain identity, stationary issuer state, and the schema-9 cutover."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from open_brain_engine.core.access_contracts import (
    derive_brain_id,
    validate_issuer_epoch,
)
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import ValidationError
from open_brain_engine.portable.v4 import manifest_v4
from open_brain_engine.storage.locks import FileLease, LockBusyError
from open_brain_engine.storage.migrations import _format_timestamp, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

from .contracts import LocalEngineContext
from .local_schema import (
    PHASE1_STATE_DATABASE,
    _MigrationClock,
    classify_local_schema,
)
from .local_schema_catalog import LOCAL_MIGRATIONS
from .runtime_admission import HeldRuntimeAdmission
from .t03_contracts import T03Error

CUTOVER_CREATED_AT = "1970-01-01T00:00:00Z"
CUTOVER_MANIFEST_DOMAIN = "open-brain-cutover-manifest-v1"
ISSUER_STATE_SCHEMA_VERSION = 9
LEGACY_ISSUER_EPOCH = 1
CUTOVER_ISSUER_EPOCH = 2

_ISSUER_SHA256 = re.compile(r"[0-9a-f]{64}")


def validate_issuer_digest(value: object) -> str:
    """Return one lowercase-hex SHA-256 issuer digest or fail closed."""
    if not isinstance(value, str) or not _ISSUER_SHA256.fullmatch(value):
        raise ValidationError("invalid issuer digest")
    return value


def synthetic_cutover_manifest(
    files: Mapping[str, bytes], *, tenant_id: str
) -> dict[str, object]:
    """Build the deterministic pre-cutover Portable v4 manifest for an existing Brain."""
    inventory = [
        {"path": path, "sha256": sha256(payload).hexdigest()}
        for path, payload in sorted(files.items())
    ]
    inventory_digest = sha256(portable_canonical_json_bytes(inventory)).hexdigest()
    export_id = "export_" + str(
        uuid5(
            NAMESPACE_URL,
            CUTOVER_MANIFEST_DOMAIN + ":" + tenant_id + ":" + inventory_digest,
        )
    )
    return manifest_v4(
        files, tenant_id=tenant_id, export_id=export_id, created_at=CUTOVER_CREATED_AT
    )


def derive_legacy_bindings(
    files: Mapping[str, bytes],
) -> tuple[tuple[str, int | None, str], ...]:
    """Bind every declared non-JSONL file once and every nonempty JSONL row once."""
    bindings: list[tuple[str, int | None, str]] = []
    for path in sorted(files):
        payload = files[path]
        if path.endswith(".jsonl"):
            for ordinal, row in enumerate(payload.split(b"\n")):
                if row:
                    bindings.append((path, ordinal, sha256(row).hexdigest()))
        else:
            bindings.append((path, None, sha256(payload).hexdigest()))
    return tuple(bindings)


def legacy_binding_manifest_sha256(
    bindings: Sequence[tuple[str, int | None, str]],
    *,
    issuer_epoch: int,
) -> str:
    """Digest the canonical legacy-binding manifest for the migration marker.

    Every binding record carries its issuer epoch, so the digest covers both
    the bound evidence and the epoch that issued it.
    """
    epoch = validate_issuer_epoch(issuer_epoch)
    payload = [
        {
            "artifact_path": path,
            "issuer_epoch": epoch,
            "jsonl_ordinal": ordinal,
            "payload_sha256": validate_issuer_digest(digest),
        }
        for path, ordinal, digest in bindings
    ]
    return sha256(portable_canonical_json_bytes(payload)).hexdigest()


def cutover_inventory(profile: LocalEngineContext) -> dict[str, bytes]:
    """Read the confined exact file inventory the synthetic manifest freezes."""
    from .portability_ports import LocalTenantStorage

    storage = LocalTenantStorage(profile.root, profile.tenant_id, profile.root_identity)
    return {
        path: payload
        for path, payload in storage.portable_files()
        if path == "brain.toml" or path.startswith(("content/", "history/", "sources/"))
    }


def verify_synthetic_cutover_manifest(
    manifest: object, *, files: Mapping[str, bytes], tenant_id: str
) -> None:
    """Fail closed unless the manifest is the exact frozen cutover record."""
    if (
        not isinstance(manifest, dict)
        or manifest != synthetic_cutover_manifest(files, tenant_id=tenant_id)
        or manifest.get("created_at") != CUTOVER_CREATED_AT
    ):
        raise ValidationError("synthetic cutover manifest mismatch")
    for entry in manifest.get("files", ()):
        if not isinstance(entry, dict):
            raise ValidationError("synthetic cutover manifest entry invalid")
        validate_issuer_digest(entry.get("sha256"))


def verify_issuer_evidence(
    connection: sqlite3.Connection, *, tenant_id: str
) -> None:
    """Fail closed unless the durable issuer state is internally exact."""
    rows = connection.execute(
        "SELECT tenant_id, brain_id, issuer_epoch, legacy_issuer_epoch FROM brain_identity"
    ).fetchall()
    if len(rows) != 1:
        raise ValidationError("issuer identity missing")
    stored_tenant, brain_id, issuer_epoch, legacy_epoch = rows[0]
    if stored_tenant != tenant_id or brain_id != derive_brain_id(tenant_id):
        raise ValidationError("issuer identity mismatch")
    validate_issuer_epoch(issuer_epoch)
    if legacy_epoch is not None:
        validate_issuer_epoch(legacy_epoch)
        if legacy_epoch >= issuer_epoch:
            raise ValidationError("issuer legacy epoch is not strictly lower")
    markers = connection.execute(
        "SELECT source_manifest_bytes, source_manifest_sha256, brain_id, "
        "legacy_issuer_epoch, current_issuer_epoch, legacy_binding_manifest_sha256 "
        "FROM issuer_migration_marker"
    ).fetchall()
    bindings = [
        (path, ordinal, digest, epoch)
        for path, ordinal, digest, epoch in connection.execute(
            "SELECT artifact_path, jsonl_ordinal, payload_sha256, issuer_epoch "
            "FROM legacy_issuer_bindings ORDER BY artifact_path, jsonl_ordinal"
        )
    ]
    if not markers:
        if legacy_epoch is not None or bindings:
            raise ValidationError("issuer migration marker missing")
        return
    if len(markers) != 1:
        raise ValidationError("issuer migration marker is not singular")
    (
        manifest_bytes,
        manifest_sha,
        marker_brain,
        marker_legacy,
        marker_current,
        binding_manifest_sha,
    ) = markers[0]
    if not isinstance(manifest_bytes, bytes) or not manifest_bytes:
        raise ValidationError("issuer manifest bytes invalid")
    if sha256(manifest_bytes).hexdigest() != validate_issuer_digest(manifest_sha):
        raise ValidationError("issuer manifest digest mismatch")
    try:
        manifest: object = json.loads(manifest_bytes.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        raise ValidationError("issuer manifest bytes are not canonical JSON") from None
    if not isinstance(manifest, dict) or portable_canonical_json_bytes(manifest) != manifest_bytes:
        raise ValidationError("issuer manifest bytes are not canonical")
    if (
        manifest.get("tenant_id") != tenant_id
        or manifest.get("created_at") != CUTOVER_CREATED_AT
    ):
        raise ValidationError("issuer manifest content mismatch")
    if marker_brain != brain_id:
        raise ValidationError("issuer marker brain mismatch")
    if (marker_legacy, marker_current) != (legacy_epoch, issuer_epoch):
        raise ValidationError("issuer marker epochs mismatch")
    if any(binding_epoch != legacy_epoch for *_prefix, binding_epoch in bindings):
        raise ValidationError("issuer binding epoch mismatch")
    if (
        legacy_binding_manifest_sha256(
            [(path, ordinal, digest) for path, ordinal, digest, _epoch in bindings],
            issuer_epoch=legacy_epoch,
        )
        != validate_issuer_digest(binding_manifest_sha)
    ):
        raise ValidationError("issuer binding manifest digest mismatch")


def migrate_issuer(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    admission.validate(profile)
    if admission.live_peer_count:
        raise T03Error("operation_pending")
    lease = FileLease(
        profile.root / ".open-brain",
        "issuer-migration",
        clock=clock,
        parent_root_identity=profile.root_identity,
    )
    try:
        with lease.acquire_shared_writer():
            _migrate_issuer(
                profile, admission=admission, clock=clock, checkpoint=checkpoint
            )
    except LockBusyError:
        raise T03Error("operation_pending") from None


def _migrate_issuer(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None],
) -> None:
    """Provision migrated issuer identity, bindings, and marker in one transaction."""
    admission.validate(profile)
    connection = connect_database(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    try:
        state = classify_local_schema(connection)
        if state.state in {"current", "supported_old"} and state.version in {
            ISSUER_STATE_SCHEMA_VERSION,
            ISSUER_STATE_SCHEMA_VERSION + 1,
        }:
            # Re-entry on committed schema nine verifies the stored evidence only;
            # the vault legitimately changes after the cutover and is never re-read.
            verify_issuer_evidence(connection, tenant_id=profile.tenant_id)
            return
        if state.state != "supported_old" or state.version != 8:
            raise T03Error("operation_pending")
        files = cutover_inventory(profile)
        manifest = synthetic_cutover_manifest(files, tenant_id=profile.tenant_id)
        verify_synthetic_cutover_manifest(manifest, files=files, tenant_id=profile.tenant_id)
        manifest_bytes = portable_canonical_json_bytes(manifest)
        manifest_sha = sha256(manifest_bytes).hexdigest()
        bindings = derive_legacy_bindings(files)
        binding_manifest_sha = legacy_binding_manifest_sha256(
            bindings, issuer_epoch=LEGACY_ISSUER_EPOCH
        )
        brain_id = derive_brain_id(profile.tenant_id)
        recorded_at = _format_timestamp(clock())
        checkpoint("issuer_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        state = classify_local_schema(connection)
        if state.state != "supported_old" or state.version != 8:
            raise T03Error("operation_pending")
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:ISSUER_STATE_SCHEMA_VERSION],
            schema_version=ISSUER_STATE_SCHEMA_VERSION,
        )
        connection.execute(
            "INSERT INTO brain_identity ("
            "singleton, tenant_id, brain_id, issuer_epoch, legacy_issuer_epoch, recorded_at"
            ") VALUES (1, ?, ?, ?, ?, ?)",
            (
                profile.tenant_id,
                brain_id,
                CUTOVER_ISSUER_EPOCH,
                LEGACY_ISSUER_EPOCH,
                recorded_at,
            ),
        )
        connection.executemany(
            "INSERT INTO legacy_issuer_bindings ("
            "artifact_path, jsonl_ordinal, payload_sha256, issuer_epoch"
            ") VALUES (?, ?, ?, ?)",
            [
                (path, ordinal, digest, LEGACY_ISSUER_EPOCH)
                for path, ordinal, digest in bindings
            ],
        )
        connection.execute(
            "INSERT INTO issuer_migration_marker ("
            "singleton, source_manifest_bytes, source_manifest_sha256, brain_id, "
            "legacy_issuer_epoch, current_issuer_epoch, legacy_binding_manifest_sha256, "
            "recorded_at"
            ") VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                manifest_bytes,
                manifest_sha,
                brain_id,
                LEGACY_ISSUER_EPOCH,
                CUTOVER_ISSUER_EPOCH,
                binding_manifest_sha,
                recorded_at,
            ),
        )
        verify_issuer_evidence(connection, tenant_id=profile.tenant_id)
        checkpoint("issuer_evidence_durable")
        connection.execute("COMMIT")
        checkpoint("issuer_committed")
        state = classify_local_schema(connection)
        if (
            state.state not in {"current", "supported_old"}
            or state.version != ISSUER_STATE_SCHEMA_VERSION
        ):
            raise T03Error("operation_pending")
        verify_issuer_evidence(connection, tenant_id=profile.tenant_id)
        checkpoint("issuer_validated")
    except ValueError, KeyError, TypeError, sqlite3.Error, OSError:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise T03Error("operation_pending") from None
    finally:
        connection.close()
