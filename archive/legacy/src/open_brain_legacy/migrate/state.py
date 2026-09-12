"""Idempotent note-identity backfill for capture state."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Protocol, cast

from open_brain_engine.storage.filesystem import RootConfinementError
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown

from ._models import (
    ActionKind,
    BackupReceipt,
    IssueCode,
    MigrationAction,
    MigrationBlockedError,
    MigrationError,
    MigrationIssue,
    MigrationKind,
    MigrationPlan,
    MigrationResult,
    MigrationState,
    RestoreReceipt,
    StaleMigrationPlanError,
    StateAdoptionManifest,
    StateAdoptionPlan,
    StateAdoptionReceipt,
    StateAdoptionReceiptEvidence,
    StateApplyCapabilities,
    StateArtifact,
    StateArtifactEvidence,
    StateArtifactKind,
    StateAuthorityReceipt,
    StateAuthorityReceiptEvidence,
    StateFamily,
    StateFamilyManifest,
    StateJsonKeySpec,
    StatePlanCapabilities,
    StateReadOnlySourceHandle,
    StateSqliteSnapshotEvidence,
    StateSqliteSnapshotReceipt,
    StateSqliteSnapshotRequest,
    StateSqliteSpec,
    StateSqliteTableEvidence,
    StateSqliteTableSpec,
    StateTargetState,
    build_plan,
)
from ._support import (
    create_backup,
    read_file,
    replace_file,
    restore_backup,
    restore_backup_copy,
    safe_relative,
    validate_root,
    walk_markdown,
)

__all__ = (
    "AtomicStateArtifactPublisher",
    "ConfinedStateArtifactReadBack",
    "PythonSqliteSnapshotCapability",
    "StateApplyCapabilities",
    "StateAuthorityReceipt",
    "StateCapabilityIssuer",
    "StateAdoptionManifest",
    "StateAdoptionPlan",
    "StateAdoptionReceipt",
    "StateArtifactPublisher",
    "StateArtifactReadBack",
    "StateArtifact",
    "StateArtifactKind",
    "StateFamily",
    "StateFamilyManifest",
    "StateJsonKeySpec",
    "StatePlanCapabilities",
    "StateReadOnlySourceHandle",
    "StateSqliteSnapshotCapability",
    "StateSqliteSnapshotReceipt",
    "StateSqliteSnapshotRequest",
    "StateSqliteSpec",
    "StateSqliteTableEvidence",
    "StateSqliteTableSpec",
    "StateTargetState",
    "apply_state_backfill",
    "apply_state_adoption",
    "canonical_state_manifest",
    "plan_state_adoption",
    "plan_state_backfill",
    "validate_state_manifest",
)

_PAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STATE_MANIFEST_VERSION = 1
_STATE_STORE_VERSION = 1
_STATE_CONTROL_DIRECTORY = ".open-brain-state-adoption"
_STATE_CURRENT = "CURRENT"
_STATE_JOURNAL = "transaction.json"
_STATE_GENERATIONS = "generations"
_STATE_STAGING = "staging"
_STATE_LEASE = "lease"
_STATE_GENERATION_ID = re.compile(r"[0-9a-f]{32}")

_EVENTS_SQL = "CREATE TABLE events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
_REVIEWS_SQL = "CREATE TABLE reviews (review_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
_REVIEW_AUDIT_SQL = (
    "CREATE TABLE review_audit (audit_id TEXT PRIMARY KEY, review_id TEXT NOT NULL, "
    "payload TEXT NOT NULL)"
)
_LEDGER_SQL = (
    "CREATE TABLE ledger_rows (stage_digest TEXT PRIMARY KEY, payload TEXT NOT NULL)"
)
_SCHEMA_METADATA_SQL = "CREATE TABLE schema_metadata (version INTEGER NOT NULL)"
_INFLIGHT_SQL = (
    "CREATE TABLE inflight_journal (stage_digest TEXT PRIMARY KEY, payload TEXT NOT NULL)"
)


def canonical_state_manifest() -> StateAdoptionManifest:
    return StateAdoptionManifest(
        schema_version=1,
        families=(
            StateFamilyManifest(
                family=StateFamily.QUEUE_REQUESTED_VIDEO,
                artifacts=(
                    _canonical_json_artifact(
                        "capture/queue.json", collection="items", key="item_id"
                    ),
                    _canonical_json_artifact(
                        "capture/requested-videos.json",
                        collection="records",
                        key="video_id",
                    ),
                ),
            ),
            StateFamilyManifest(
                family=StateFamily.CAPTURE_CONTEXT,
                artifacts=(
                    _canonical_json_artifact(
                        "capture/context-sidecars.json",
                        collection="contexts",
                        key="capture_id",
                    ),
                ),
            ),
            StateFamilyManifest(
                family=StateFamily.EVENT_LEDGERS,
                artifacts=(
                    _canonical_sqlite_artifact(
                        relative="events/events.sqlite3",
                        schema_version=3,
                        application_id=4242,
                        schema_table=None,
                        schema_column=None,
                        schema_sql=(('events', _EVENTS_SQL),),
                        tables=(
                            StateSqliteTableSpec(
                                table="events",
                                key_columns=("event_id",),
                                row_count=2,
                                idempotency_key_digest=_canonical_key_digest(
                                    [["event-001"], ["event-002"]]
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            StateFamilyManifest(
                family=StateFamily.REVIEW_AUDIT,
                artifacts=(
                    _canonical_sqlite_artifact(
                        relative="review/review.sqlite3",
                        schema_version=1,
                        application_id=4243,
                        schema_table=None,
                        schema_column=None,
                        schema_sql=(
                            ("review_audit", _REVIEW_AUDIT_SQL),
                            ("reviews", _REVIEWS_SQL),
                        ),
                        tables=(
                            StateSqliteTableSpec(
                                table="reviews",
                                key_columns=("review_id",),
                                row_count=1,
                                idempotency_key_digest=_canonical_key_digest(
                                    [["review-001"]]
                                ),
                            ),
                            StateSqliteTableSpec(
                                table="review_audit",
                                key_columns=("audit_id",),
                                row_count=1,
                                idempotency_key_digest=_canonical_key_digest(
                                    [["audit-001"]]
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            StateFamilyManifest(
                family=StateFamily.LEDGER_INFLIGHT,
                artifacts=(
                    _canonical_sqlite_artifact(
                        relative="ledger/ledger.sqlite3",
                        schema_version=1,
                        application_id=4244,
                        schema_table=None,
                        schema_column=None,
                        schema_sql=(("ledger_rows", _LEDGER_SQL),),
                        tables=(
                            StateSqliteTableSpec(
                                table="ledger_rows",
                                key_columns=("stage_digest",),
                                row_count=1,
                                idempotency_key_digest=_canonical_key_digest(
                                    [["stage-001"]]
                                ),
                            ),
                        ),
                    ),
                    _canonical_sqlite_artifact(
                        relative="ledger/inflight.sqlite3",
                        schema_version=1,
                        application_id=None,
                        schema_table="schema_metadata",
                        schema_column="version",
                        schema_sql=(
                            ("inflight_journal", _INFLIGHT_SQL),
                            ("schema_metadata", _SCHEMA_METADATA_SQL),
                        ),
                        tables=(
                            StateSqliteTableSpec(
                                table="inflight_journal",
                                key_columns=("stage_digest",),
                                row_count=1,
                                idempotency_key_digest=_canonical_key_digest(
                                    [["stage-002"]]
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            StateFamilyManifest(
                family=StateFamily.PROVIDER_RETRIEVAL,
                artifacts=(
                    _canonical_json_artifact(
                        "providers/retrieval-metadata.json",
                        collection="records",
                        key="metadata_id",
                    ),
                ),
            ),
            StateFamilyManifest(
                family=StateFamily.RECOVERY_BACKUP,
                artifacts=(
                    _canonical_json_artifact(
                        "recovery/backup-metadata.json",
                        collection="backups",
                        key="backup_id",
                    ),
                ),
            ),
        ),
    )


def _canonical_json_artifact(
    relative: str, *, collection: str, key: str
) -> StateArtifact:
    return StateArtifact(
        relative=PurePosixPath(relative),
        kind=StateArtifactKind.JSON,
        schema_version=1,
        json_keys=(StateJsonKeySpec(collection_path=(collection,), key_fields=(key,)),),
    )


def _canonical_sqlite_artifact(
    *,
    relative: str,
    schema_version: int,
    application_id: int | None,
    schema_table: str | None,
    schema_column: str | None,
    schema_sql: tuple[tuple[str, str], ...],
    tables: tuple[StateSqliteTableSpec, ...],
) -> StateArtifact:
    schema_rows = [
        ["table", name, name, sql] for name, sql in sorted(schema_sql)
    ]
    return StateArtifact(
        relative=PurePosixPath(relative),
        kind=StateArtifactKind.SQLITE,
        schema_version=schema_version,
        json_keys=(),
        sqlite=StateSqliteSpec(
            application_id=application_id,
            schema_table=schema_table,
            schema_column=schema_column,
            schema_digest_sha256=sha256(_canonical_json_bytes(schema_rows)).hexdigest(),
            tables=tables,
        ),
    )


def _canonical_key_digest(keys: list[list[str | int]]) -> str:
    return sha256(_canonical_json_bytes(sorted(keys))).hexdigest()


@dataclass(frozen=True, slots=True)
class _StateArtifactSnapshot:
    evidence: StateArtifactEvidence
    payload: bytes
    keyed_records: dict[str, str]


@dataclass(frozen=True, slots=True)
class _RootGrant:
    role: str
    path: Path
    binding: str
    device: int
    inode: int


@dataclass(slots=True)
class _CapabilityRecord:
    version: int
    operation: str
    capability_binding: str
    plan_fingerprint: str | None
    expires_at: datetime
    roots: tuple[_RootGrant, ...]


@dataclass(frozen=True, slots=True)
class _ReceiptRecord:
    version: int
    operation: str
    plan_fingerprint: str
    capability_binding: str
    expires_at: datetime
    roots: tuple[_RootGrant, ...]
    evidence: object


class StateCapabilityIssuer:
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._capabilities: dict[object, _CapabilityRecord] = {}
        self._source_handles: dict[object, _CapabilityRecord] = {}
        self._receipts: dict[object, _ReceiptRecord] = {}

    def issue_plan_capabilities(
        self,
        *,
        source_root: Path,
        target_root: Path,
        backup_root: Path,
        expires_at: datetime,
    ) -> StatePlanCapabilities:
        roots = (
            self._root_grant("source", source_root),
            self._root_grant("target", target_root),
            self._root_grant("backup", backup_root),
        )
        _require_distinct_grants(roots)
        record = self._new_capability("plan", None, expires_at, roots)
        token = object()
        self._capabilities[token] = record
        return StatePlanCapabilities._issued(token)

    def issue_apply_capabilities(
        self,
        *,
        plan: StateAdoptionPlan,
        restore_root: Path,
        expires_at: datetime,
    ) -> StateApplyCapabilities:
        planning = self._resolve_capability(plan.capabilities, "plan")
        if planning.plan_fingerprint != plan.fingerprint:
            raise MigrationBlockedError("state plan capability is unbound")
        restore = self._root_grant("restore", restore_root)
        roots = (*planning.roots, restore)
        _require_distinct_grants(roots)
        record = self._new_capability("apply", plan.fingerprint, expires_at, roots)
        token = object()
        self._capabilities[token] = record
        return StateApplyCapabilities._issued(token)

    def inspect_authority_receipt(
        self, receipt: StateAuthorityReceipt, *, operation: str
    ) -> StateAuthorityReceiptEvidence:
        record = self._resolve_receipt(receipt, operation)
        if not isinstance(record.evidence, StateAuthorityReceiptEvidence):
            raise MigrationBlockedError("state authority receipt type is invalid")
        return record.evidence

    def inspect_adoption_receipt(
        self, receipt: StateAdoptionReceipt
    ) -> StateAdoptionReceiptEvidence:
        record = self._resolve_receipt(receipt, "apply")
        if not isinstance(record.evidence, StateAdoptionReceiptEvidence):
            raise MigrationBlockedError("state adoption receipt type is invalid")
        return record.evidence

    def _new_capability(
        self,
        operation: str,
        plan_fingerprint: str | None,
        expires_at: datetime,
        roots: tuple[_RootGrant, ...],
    ) -> _CapabilityRecord:
        _require_expiry(self._now(), expires_at)
        return _CapabilityRecord(
            version=1,
            operation=operation,
            capability_binding=sha256(secrets.token_bytes(32)).hexdigest(),
            plan_fingerprint=plan_fingerprint,
            expires_at=expires_at,
            roots=roots,
        )

    def _resolve_capability(
        self,
        capability: StatePlanCapabilities | StateApplyCapabilities,
        operation: str,
    ) -> _CapabilityRecord:
        if not isinstance(capability, StatePlanCapabilities | StateApplyCapabilities):
            raise MigrationBlockedError("state capability is forged")
        record = self._capabilities.get(capability._authority_token())
        if record is None:
            raise MigrationBlockedError("state capability is forged")
        if record.version != 1 or record.operation != operation:
            raise MigrationBlockedError("state capability operation mismatch")
        if self._now() >= record.expires_at:
            raise MigrationBlockedError("state capability is expired")
        for root in record.roots:
            self._verify_root_grant(root)
        return record

    def _bind_plan(
        self, capability: StatePlanCapabilities, fingerprint: str
    ) -> _CapabilityRecord:
        record = self._resolve_capability(capability, "plan")
        if record.plan_fingerprint not in {None, fingerprint}:
            raise MigrationBlockedError("state plan capability is already bound")
        record.plan_fingerprint = fingerprint
        return record

    def _resolve_apply(
        self,
        capability: StateApplyCapabilities,
        plan: StateAdoptionPlan,
        restore_root: Path,
    ) -> _CapabilityRecord:
        record = self._resolve_capability(capability, "apply")
        if record.plan_fingerprint != plan.fingerprint:
            raise MigrationBlockedError("state apply capability plan mismatch")
        expected = (
            plan.source_root_binding,
            plan.target_root_binding,
            plan.backup_root_binding,
        )
        if tuple(grant.binding for grant in record.roots[:3]) != expected:
            raise MigrationBlockedError("state apply capability root mismatch")
        restore = _grant_for(record, "restore")
        supplied = self._root_grant("restore", restore_root)
        if supplied != restore:
            raise MigrationBlockedError("state restore root mismatch")
        return record

    def _source_handle(self, record: _CapabilityRecord) -> StateReadOnlySourceHandle:
        source = _grant_for(record, "source")
        self._verify_root_grant(source)
        token = object()
        self._source_handles[token] = record
        return StateReadOnlySourceHandle._issued_reader(
            token,
            lambda artifact: self._snapshot_from_handle(token, artifact),
        )

    @contextmanager
    def _target_lease(
        self, record: _CapabilityRecord
    ) -> Iterator[_TargetGenerationStore]:
        target = _grant_for(record, "target")
        self._verify_root_grant(target)
        store = _TargetGenerationStore.acquire(
            target=target,
            capability_binding=record.capability_binding,
        )
        try:
            yield store
        finally:
            store.close()

    def _snapshot_from_handle(
        self, token: object, artifact: StateArtifact
    ) -> StateSqliteSnapshotReceipt:
        record = self._source_handles.get(token)
        if record is None:
            raise MigrationBlockedError("read-only source handle is forged")
        if self._now() >= record.expires_at:
            raise MigrationBlockedError("read-only source handle is expired")
        source = _grant_for(record, "source")
        self._verify_root_grant(source)
        evidence = _snapshot_sqlite_from_read_only_root(
            source=source,
            artifact=artifact,
            operation=record.operation,
            plan_binding=record.capability_binding,
        )
        return cast(
            StateSqliteSnapshotReceipt,
            self._issue_receipt(
                receipt_type=StateSqliteSnapshotReceipt,
                operation="sqlite_snapshot",
                plan_fingerprint=record.plan_fingerprint or record.capability_binding,
                capability_binding=record.capability_binding,
                expires_at=record.expires_at,
                roots=(source,),
                evidence=evidence,
            ),
        )

    def _resolve_sqlite_receipt(
        self,
        receipt: StateSqliteSnapshotReceipt,
        record: _CapabilityRecord,
        artifact: StateArtifact,
    ) -> StateSqliteSnapshotEvidence:
        resolved = self._resolve_receipt(receipt, "sqlite_snapshot")
        source = _grant_for(record, "source")
        if (
            resolved.capability_binding != record.capability_binding
            or resolved.roots != (source,)
            or not isinstance(resolved.evidence, StateSqliteSnapshotEvidence)
            or resolved.evidence.artifact_relative != artifact.relative
        ):
            raise MigrationBlockedError("sqlite snapshot receipt is unbound")
        return resolved.evidence

    def _issue_authority_receipt(
        self,
        *,
        operation: str,
        plan_fingerprint: str,
        capability: _CapabilityRecord,
        roots: tuple[_RootGrant, ...],
        evidence: StateAuthorityReceiptEvidence,
    ) -> StateAuthorityReceipt:
        return self._issue_receipt(
            receipt_type=StateAuthorityReceipt,
            operation=operation,
            plan_fingerprint=plan_fingerprint,
            capability_binding=capability.capability_binding,
            expires_at=capability.expires_at,
            roots=roots,
            evidence=evidence,
        )

    def _issue_adoption_receipt(
        self,
        *,
        plan_fingerprint: str,
        capability: _CapabilityRecord,
        evidence: StateAdoptionReceiptEvidence,
    ) -> StateAdoptionReceipt:
        return cast(
            StateAdoptionReceipt,
            self._issue_receipt(
                receipt_type=StateAdoptionReceipt,
                operation="apply",
                plan_fingerprint=plan_fingerprint,
                capability_binding=capability.capability_binding,
                expires_at=capability.expires_at,
                roots=capability.roots,
                evidence=evidence,
            ),
        )

    def _issue_receipt(
        self,
        *,
        receipt_type: type[StateAuthorityReceipt],
        operation: str,
        plan_fingerprint: str,
        capability_binding: str,
        expires_at: datetime,
        roots: tuple[_RootGrant, ...],
        evidence: object,
    ) -> StateAuthorityReceipt:
        token = object()
        self._receipts[token] = _ReceiptRecord(
            version=1,
            operation=operation,
            plan_fingerprint=plan_fingerprint,
            capability_binding=capability_binding,
            expires_at=expires_at,
            roots=roots,
            evidence=evidence,
        )
        return receipt_type._issued(token)

    def _resolve_receipt(
        self, receipt: StateAuthorityReceipt, operation: str
    ) -> _ReceiptRecord:
        if not isinstance(receipt, StateAuthorityReceipt):
            raise MigrationBlockedError("state authority receipt is forged")
        record = self._receipts.get(receipt._authority_token())
        if record is None:
            raise MigrationBlockedError("state authority receipt is forged")
        if record.version != 1 or record.operation != operation:
            raise MigrationBlockedError("state receipt operation mismatch")
        if self._now() >= record.expires_at:
            raise MigrationBlockedError("state authority receipt is expired")
        for root in record.roots:
            self._verify_root_grant(root)
        return record

    def _root_grant(self, role: str, root: Path) -> _RootGrant:
        try:
            validate_root(root)
            canonical = root.resolve(strict=True)
            metadata = canonical.stat()
        except (OSError, RootConfinementError):
            raise MigrationBlockedError("state capability root is invalid") from None
        binding = sha256(
            f"1:{role}:{canonical}:{metadata.st_dev}:{metadata.st_ino}".encode()
        ).hexdigest()
        return _RootGrant(role, canonical, binding, metadata.st_dev, metadata.st_ino)

    def _verify_root_grant(self, grant: _RootGrant) -> None:
        current = self._root_grant(grant.role, grant.path)
        if current != grant:
            raise MigrationBlockedError("state capability root mismatch")

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise MigrationBlockedError("state capability clock is invalid")
        return now.astimezone(UTC)


def _require_expiry(now: datetime, expires_at: datetime) -> None:
    if (
        not isinstance(expires_at, datetime)
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
        or expires_at.astimezone(UTC) <= now
    ):
        raise MigrationBlockedError("state capability expiry is invalid")


def _require_distinct_grants(grants: tuple[_RootGrant, ...]) -> None:
    for index, left in enumerate(grants):
        for right in grants[index + 1 :]:
            if (
                left.path == right.path
                or left.path in right.path.parents
                or right.path in left.path.parents
                or (left.device, left.inode) == (right.device, right.inode)
            ):
                raise MigrationBlockedError("state capability roots must be separate")


def _grant_for(record: _CapabilityRecord, role: str) -> _RootGrant:
    for grant in record.roots:
        if grant.role == role:
            return grant
    raise MigrationBlockedError("state capability role is missing")


class StateSqliteSnapshotCapability(Protocol):
    def snapshot(self, request: StateSqliteSnapshotRequest) -> StateSqliteSnapshotReceipt: ...


class PythonSqliteSnapshotCapability:
    def snapshot(self, request: StateSqliteSnapshotRequest) -> StateSqliteSnapshotReceipt:
        if not isinstance(request, StateSqliteSnapshotRequest):
            raise MigrationBlockedError("sqlite snapshot request is invalid")
        return request.source.snapshot_sqlite(request.artifact)


class StateArtifactPublisher(Protocol):
    def publish(
        self, *, target_root: Path, relative: PurePosixPath, payload: bytes
    ) -> None: ...


class AtomicStateArtifactPublisher:
    def publish(
        self, *, target_root: Path, relative: PurePosixPath, payload: bytes
    ) -> None:
        replace_file(target_root, relative, payload, require_existing=False)


class StateArtifactReadBack(Protocol):
    def read(self, *, target_root: Path, relative: PurePosixPath) -> bytes | None: ...


class ConfinedStateArtifactReadBack:
    def read(self, *, target_root: Path, relative: PurePosixPath) -> bytes | None:
        return read_file(target_root, relative)


@dataclass(frozen=True, slots=True)
class _StateTransaction:
    generation: str
    generation_device: int
    generation_inode: int
    journal_payload: bytes
    pointer_payload: bytes
    artifact_digests: dict[str, str]


class _TargetGenerationStore:
    def __init__(
        self,
        *,
        target: _RootGrant,
        root_fd: int,
        control_fd: int,
        generations_fd: int,
        staging_fd: int,
        lease_fd: int,
    ) -> None:
        self.target = target
        self.root_fd = root_fd
        self.control_fd = control_fd
        self.generations_fd = generations_fd
        self.staging_fd = staging_fd
        self.lease_fd = lease_fd
        self.control_path = target.path / _STATE_CONTROL_DIRECTORY

    @classmethod
    def acquire(
        cls, *, target: _RootGrant, capability_binding: str
    ) -> _TargetGenerationStore:
        root_fd = control_fd = generations_fd = staging_fd = lease_fd = -1
        try:
            root_fd = _open_pinned_directory(target.path)
            _verify_descriptor_identity(root_fd, target)
            _mkdir_at(root_fd, _STATE_CONTROL_DIRECTORY)
            control_fd = _open_directory_at(root_fd, _STATE_CONTROL_DIRECTORY)
            _mkdir_at(control_fd, _STATE_GENERATIONS)
            generations_fd = _open_directory_at(control_fd, _STATE_GENERATIONS)
            _mkdir_at(control_fd, _STATE_STAGING)
            staging_fd = _open_directory_at(control_fd, _STATE_STAGING)
            lease_fd = os.open(
                _STATE_LEASE,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=control_fd,
            )
            try:
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MigrationBlockedError("state target lease is unavailable") from None
            lease_payload = _canonical_json_bytes(
                {
                    "capability_binding": capability_binding,
                    "target_root_binding": target.binding,
                    "version": _STATE_STORE_VERSION,
                }
            )
            os.ftruncate(lease_fd, 0)
            _write_descriptor(lease_fd, lease_payload)
            os.fsync(lease_fd)
            os.fsync(control_fd)
            return cls(
                target=target,
                root_fd=root_fd,
                control_fd=control_fd,
                generations_fd=generations_fd,
                staging_fd=staging_fd,
                lease_fd=lease_fd,
            )
        except MigrationBlockedError:
            for descriptor in (
                lease_fd,
                staging_fd,
                generations_fd,
                control_fd,
                root_fd,
            ):
                if descriptor >= 0:
                    os.close(descriptor)
            raise
        except OSError:
            for descriptor in (
                lease_fd,
                staging_fd,
                generations_fd,
                control_fd,
                root_fd,
            ):
                if descriptor >= 0:
                    os.close(descriptor)
            raise MigrationBlockedError("state target lease failed") from None

    def close(self) -> None:
        for descriptor in (
            self.lease_fd,
            self.staging_fd,
            self.generations_fd,
            self.control_fd,
            self.root_fd,
        ):
            if descriptor >= 0:
                os.close(descriptor)
        self.lease_fd = -1
        self.staging_fd = -1
        self.generations_fd = -1
        self.control_fd = -1
        self.root_fd = -1

    def verify_pins(self) -> None:
        _verify_descriptor_identity(self.root_fd, self.target)
        metadata = os.fstat(self.control_fd)
        current = os.stat(
            _STATE_CONTROL_DIRECTORY,
            dir_fd=self.root_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(current.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise StaleMigrationPlanError("state target control directory changed")

    def recover(self, manifest: StateAdoptionManifest) -> None:
        self.verify_pins()
        journal_payload = _read_regular_at(self.control_fd, _STATE_JOURNAL)
        if journal_payload is None:
            return
        transaction = _parse_state_transaction(
            journal_payload,
            manifest=manifest,
            target_root_binding=self.target.binding,
        )
        current = _read_regular_at(self.control_fd, _STATE_CURRENT)
        if current is None:
            self._remove_owned_generation(transaction)
            self._cleanup_transaction(transaction)
            return
        transaction = self._bind_recovered_transaction(transaction, manifest)
        if current != transaction.pointer_payload:
            raise MigrationBlockedError("state recovery CURRENT conflicts with transaction")
        files, directories = _read_committed_generation(
            target=self.target,
            manifest=manifest,
            pointer_payload=current,
        )
        _validate_generation(
            manifest,
            files,
            directories,
            transaction.artifact_digests,
        )
        self._cleanup_transaction(transaction)

    def _bind_recovered_transaction(
        self,
        transaction: _StateTransaction,
        manifest: StateAdoptionManifest,
    ) -> _StateTransaction:
        generation_fd = _open_directory_at(
            self.generations_fd, transaction.generation
        )
        try:
            metadata = _verify_generation_owner(generation_fd, transaction)
        finally:
            os.close(generation_fd)
        journal = _parse_store_object(
            transaction.journal_payload, "state transaction journal is invalid"
        )
        return _bind_state_transaction(
            transaction,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            manifest_digest=_state_manifest_digest(manifest),
            plan_fingerprint=cast(str, journal["plan_fingerprint"]),
            target_root_binding=self.target.binding,
        )

    def begin(
        self,
        *,
        plan: StateAdoptionPlan,
        artifact_digests: dict[str, str],
    ) -> _StateTransaction:
        self.verify_pins()
        if _read_regular_at(self.control_fd, _STATE_JOURNAL) is not None:
            raise MigrationBlockedError("state transaction is already active")
        if _read_regular_at(self.control_fd, _STATE_CURRENT) is not None:
            raise StaleMigrationPlanError("state target CURRENT changed")
        generation = secrets.token_hex(16)
        journal_payload = _canonical_json_bytes(
            {
                "artifact_digests": artifact_digests,
                "generation": generation,
                "manifest_digest": plan.manifest_digest,
                "operation": "apply",
                "plan_fingerprint": plan.fingerprint,
                "target_root_binding": self.target.binding,
                "version": _STATE_STORE_VERSION,
            }
        )
        transaction = _StateTransaction(
            generation=generation,
            generation_device=-1,
            generation_inode=-1,
            journal_payload=journal_payload,
            pointer_payload=b"",
            artifact_digests=artifact_digests,
        )
        _write_no_replace_at(self.control_fd, _STATE_JOURNAL, journal_payload)
        try:
            os.mkdir(generation, 0o700, dir_fd=self.staging_fd)
            generation_fd = _open_directory_at(self.staging_fd, generation)
            try:
                metadata = os.fstat(generation_fd)
                _write_no_replace_at(
                    generation_fd,
                    "OWNER",
                    _generation_owner_payload(
                        journal_payload,
                        device=metadata.st_dev,
                        inode=metadata.st_ino,
                    ),
                )
                os.mkdir("artifacts", 0o700, dir_fd=generation_fd)
                os.fsync(generation_fd)
            finally:
                os.close(generation_fd)
            os.fsync(self.staging_fd)
            transaction = _bind_state_transaction(
                transaction,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                manifest_digest=plan.manifest_digest,
                plan_fingerprint=plan.fingerprint,
                target_root_binding=self.target.binding,
            )
        except BaseException:
            self.abort_precommit(transaction)
            raise
        return transaction

    def generation_artifact_root(self, transaction: _StateTransaction) -> Path:
        return self.control_path / _STATE_STAGING / transaction.generation / "artifacts"

    def write_artifact(
        self, transaction: _StateTransaction, relative: PurePosixPath, payload: bytes
    ) -> None:
        generation_fd = _open_directory_at(self.staging_fd, transaction.generation)
        try:
            _verify_generation_owner(generation_fd, transaction)
            artifacts_fd = _open_directory_at(generation_fd, "artifacts")
            try:
                _write_relative_no_replace(artifacts_fd, relative, payload)
            finally:
                os.close(artifacts_fd)
        finally:
            os.close(generation_fd)

    def read_artifact(
        self, transaction: _StateTransaction, relative: PurePosixPath
    ) -> bytes | None:
        generation_fd = _open_directory_at(self.staging_fd, transaction.generation)
        try:
            _verify_generation_owner(generation_fd, transaction)
            artifacts_fd = _open_directory_at(generation_fd, "artifacts")
            try:
                return _read_relative_at(artifacts_fd, relative)
            finally:
                os.close(artifacts_fd)
        finally:
            os.close(generation_fd)

    def seal(
        self,
        transaction: _StateTransaction,
        manifest: StateAdoptionManifest,
    ) -> tuple[dict[str, bytes], set[str]]:
        generation_fd = _open_directory_at(self.staging_fd, transaction.generation)
        try:
            _verify_generation_owner(generation_fd, transaction)
            artifacts_fd = _open_directory_at(generation_fd, "artifacts")
            try:
                files, directories = _snapshot_tree_at(artifacts_fd)
                _fsync_tree_at(artifacts_fd)
            finally:
                os.close(artifacts_fd)
            _validate_generation(
                manifest,
                files,
                directories,
                transaction.artifact_digests,
            )
            _write_no_replace_at(generation_fd, "READY", transaction.pointer_payload)
            os.fsync(generation_fd)
        finally:
            os.close(generation_fd)
        os.fsync(self.staging_fd)
        return files, directories

    def promote(self, transaction: _StateTransaction) -> None:
        generation_fd = _open_directory_at(self.staging_fd, transaction.generation)
        try:
            _verify_generation_owner(generation_fd, transaction)
            if _read_regular_at(generation_fd, "READY") != transaction.pointer_payload:
                raise MigrationError("state generation is not ready")
        finally:
            os.close(generation_fd)
        _rename_directory_no_replace(
            self.staging_fd,
            transaction.generation,
            self.generations_fd,
            transaction.generation,
        )

    def publish_current(self, transaction: _StateTransaction) -> None:
        self.verify_pins()
        generation_fd = _open_directory_at(
            self.generations_fd, transaction.generation
        )
        try:
            _verify_generation_owner(generation_fd, transaction)
            if _read_regular_at(generation_fd, "READY") != transaction.pointer_payload:
                raise StaleMigrationPlanError("state generation changed before commit")
        finally:
            os.close(generation_fd)
        temp_name = ".current-" + transaction.generation
        _write_no_replace_at(self.control_fd, temp_name, transaction.pointer_payload)
        try:
            os.link(
                temp_name,
                _STATE_CURRENT,
                src_dir_fd=self.control_fd,
                dst_dir_fd=self.control_fd,
                follow_symlinks=False,
            )
            os.fsync(self.control_fd)
        except FileExistsError:
            raise StaleMigrationPlanError("state target CURRENT changed") from None
        except OSError:
            raise MigrationError("state CURRENT publication failed") from None
        finally:
            try:
                os.unlink(temp_name, dir_fd=self.control_fd)
                os.fsync(self.control_fd)
            except FileNotFoundError:
                pass

    def finish(self, transaction: _StateTransaction) -> None:
        current = _read_regular_at(self.control_fd, _STATE_CURRENT)
        if current != transaction.pointer_payload:
            raise MigrationError("state committed generation verification failed")
        self._cleanup_transaction(transaction)

    def abort_precommit(self, transaction: _StateTransaction) -> None:
        current = _read_regular_at(self.control_fd, _STATE_CURRENT)
        if current == transaction.pointer_payload:
            return
        self._remove_owned_generation(transaction)
        self._cleanup_transaction(transaction)

    def _remove_owned_generation(self, transaction: _StateTransaction) -> None:
        removed = False
        for parent_fd in (self.staging_fd, self.generations_fd):
            try:
                generation_fd = _open_directory_at(parent_fd, transaction.generation)
            except FileNotFoundError:
                continue
            try:
                try:
                    _verify_generation_owner(generation_fd, transaction)
                except MigrationBlockedError:
                    continue
                if removed:
                    raise MigrationBlockedError(
                        "state owned generation exists in two locations"
                    )
                _remove_tree_contents_at(generation_fd)
                removed = True
            finally:
                os.close(generation_fd)
            if removed:
                os.rmdir(transaction.generation, dir_fd=parent_fd)
                os.fsync(parent_fd)

    def _cleanup_transaction(self, transaction: _StateTransaction) -> None:
        observed = _read_regular_at(self.control_fd, _STATE_JOURNAL)
        if observed is None:
            return
        if observed != transaction.journal_payload:
            raise MigrationBlockedError("state transaction journal changed")
        os.unlink(_STATE_JOURNAL, dir_fd=self.control_fd)
        os.fsync(self.control_fd)


def _open_pinned_directory(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )


def _open_directory_at(parent_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )


def _verify_descriptor_identity(descriptor: int, grant: _RootGrant) -> None:
    metadata = os.fstat(descriptor)
    if (metadata.st_dev, metadata.st_ino) != (grant.device, grant.inode):
        raise MigrationBlockedError("state capability root mismatch")


def _mkdir_at(parent_fd: int, name: str) -> None:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise MigrationBlockedError("state control entry is invalid") from None


def _rename_directory_no_replace(
    source_parent_fd: int,
    source_name: str,
    target_parent_fd: int,
    target_name: str,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    target = os.fsencode(target_name)
    if sys.platform == "darwin":
        rename = library.renameatx_np
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(
            source_parent_fd,
            source,
            target_parent_fd,
            target,
            0x00000004,
        )
    elif sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError:
            raise MigrationBlockedError(
                "state generation no-replace rename is unavailable"
            ) from None
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(
            source_parent_fd,
            source,
            target_parent_fd,
            target,
            0x00000001,
        )
    else:
        raise MigrationBlockedError(
            "state generation no-replace rename is unavailable"
        )
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise StaleMigrationPlanError("state generation already exists") from None
        raise MigrationError("state generation promotion failed") from OSError(
            error, os.strerror(error)
        )
    os.fsync(source_parent_fd)
    os.fsync(target_parent_fd)


def _write_descriptor(descriptor: int, payload: bytes) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError
        view = view[written:]


def _write_no_replace_at(parent_fd: int, name: str, payload: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        _write_descriptor(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent_fd)


def _write_relative_no_replace(
    root_fd: int, relative: PurePosixPath, payload: bytes
) -> None:
    safe = safe_relative(relative)
    descriptors: list[int] = []
    parent_fd = root_fd
    try:
        for part in safe.parts[:-1]:
            _mkdir_at(parent_fd, part)
            child_fd = _open_directory_at(parent_fd, part)
            descriptors.append(child_fd)
            parent_fd = child_fd
        _write_no_replace_at(parent_fd, safe.name, payload)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_regular_at(parent_fd: int, name: str) -> bytes | None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MigrationBlockedError("state store file is invalid")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65_536):
            chunks.append(chunk)
        return b"".join(chunks)
    except FileNotFoundError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_relative_at(root_fd: int, relative: PurePosixPath) -> bytes | None:
    safe = safe_relative(relative)
    descriptors: list[int] = []
    parent_fd = root_fd
    try:
        for part in safe.parts[:-1]:
            child_fd = _open_directory_at(parent_fd, part)
            descriptors.append(child_fd)
            parent_fd = child_fd
        return _read_regular_at(parent_fd, safe.name)
    except FileNotFoundError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _snapshot_tree_at(root_fd: int) -> tuple[dict[str, bytes], set[str]]:
    files: dict[str, bytes] = {}
    directories: set[str] = set()

    def visit(directory_fd: int, prefix: PurePosixPath | None) -> None:
        for entry in sorted(os.scandir(directory_fd), key=lambda item: item.name):
            metadata = entry.stat(follow_symlinks=False)
            relative = PurePosixPath(entry.name) if prefix is None else prefix / entry.name
            if stat.S_ISLNK(metadata.st_mode):
                raise MigrationBlockedError("state store refuses symlink")
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(str(relative))
                child_fd = _open_directory_at(directory_fd, entry.name)
                try:
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                payload = _read_regular_at(directory_fd, entry.name)
                if payload is None:
                    raise MigrationBlockedError("state store changed during read")
                files[str(relative)] = payload
            else:
                raise MigrationBlockedError("state store entry is invalid")

    visit(root_fd, None)
    return files, directories


def _fsync_tree_at(root_fd: int) -> None:
    for entry in sorted(os.scandir(root_fd), key=lambda item: item.name):
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory_at(root_fd, entry.name)
            try:
                _fsync_tree_at(child_fd)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            descriptor = os.open(
                entry.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            raise MigrationBlockedError("state store entry is invalid")
    os.fsync(root_fd)


def _remove_tree_contents_at(root_fd: int) -> None:
    for entry in sorted(os.scandir(root_fd), key=lambda item: item.name):
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory_at(root_fd, entry.name)
            try:
                _remove_tree_contents_at(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(entry.name, dir_fd=root_fd)
        elif stat.S_ISREG(metadata.st_mode):
            os.unlink(entry.name, dir_fd=root_fd)
        else:
            raise MigrationBlockedError("state store entry is invalid")
    os.fsync(root_fd)


def _parse_store_object(payload: bytes, message: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_state_json_object)
    except (UnicodeDecodeError, ValueError):
        raise MigrationBlockedError(message) from None
    if not isinstance(value, dict) or payload != _canonical_json_bytes(value):
        raise MigrationBlockedError(message)
    return value


def _generation_owner_payload(
    journal_payload: bytes, *, device: int, inode: int
) -> bytes:
    return _canonical_json_bytes(
        {
            "generation_device": device,
            "generation_inode": inode,
            "journal_digest": sha256(journal_payload).hexdigest(),
            "version": _STATE_STORE_VERSION,
        }
    )


def _verify_generation_owner(
    generation_fd: int, transaction: _StateTransaction
) -> os.stat_result:
    metadata = os.fstat(generation_fd)
    owner = _read_regular_at(generation_fd, "OWNER")
    if owner != _generation_owner_payload(
        transaction.journal_payload,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    ):
        raise MigrationBlockedError("state generation ownership mismatch")
    if (
        transaction.generation_device >= 0
        and transaction.generation_inode >= 0
        and (metadata.st_dev, metadata.st_ino)
        != (transaction.generation_device, transaction.generation_inode)
    ):
        raise MigrationBlockedError("state generation identity mismatch")
    return metadata


def _bind_state_transaction(
    transaction: _StateTransaction,
    *,
    device: int,
    inode: int,
    manifest_digest: str,
    plan_fingerprint: str,
    target_root_binding: str,
) -> _StateTransaction:
    pointer_payload = _canonical_json_bytes(
        {
            "artifact_digests": transaction.artifact_digests,
            "generation": transaction.generation,
            "generation_device": device,
            "generation_inode": inode,
            "manifest_digest": manifest_digest,
            "plan_fingerprint": plan_fingerprint,
            "target_root_binding": target_root_binding,
            "version": _STATE_STORE_VERSION,
        }
    )
    return _StateTransaction(
        generation=transaction.generation,
        generation_device=device,
        generation_inode=inode,
        journal_payload=transaction.journal_payload,
        pointer_payload=pointer_payload,
        artifact_digests=transaction.artifact_digests,
    )


def _parse_artifact_digests(
    value: object, manifest: StateAdoptionManifest, message: str
) -> dict[str, str]:
    expected = {
        str(artifact.relative)
        for family in manifest.families
        for artifact in family.artifacts
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in value.values()
        )
    ):
        raise MigrationBlockedError(message)
    return cast(dict[str, str], value)


def _parse_state_transaction(
    payload: bytes,
    *,
    manifest: StateAdoptionManifest,
    target_root_binding: str,
) -> _StateTransaction:
    message = "state transaction journal is invalid"
    value = _parse_store_object(payload, message)
    expected_keys = {
        "artifact_digests",
        "generation",
        "manifest_digest",
        "operation",
        "plan_fingerprint",
        "target_root_binding",
        "version",
    }
    generation = value.get("generation")
    if (
        set(value) != expected_keys
        or value.get("version") != _STATE_STORE_VERSION
        or value.get("operation") != "apply"
        or not isinstance(generation, str)
        or _STATE_GENERATION_ID.fullmatch(generation) is None
        or value.get("manifest_digest") != _state_manifest_digest(manifest)
        or value.get("target_root_binding") != target_root_binding
        or not isinstance(value.get("plan_fingerprint"), str)
        or _SHA256.fullmatch(cast(str, value["plan_fingerprint"])) is None
    ):
        raise MigrationBlockedError(message)
    artifact_digests = _parse_artifact_digests(
        value.get("artifact_digests"), manifest, message
    )
    return _StateTransaction(
        generation=generation,
        generation_device=-1,
        generation_inode=-1,
        journal_payload=payload,
        pointer_payload=b"",
        artifact_digests=artifact_digests,
    )


def _parse_current_pointer(
    payload: bytes,
    *,
    manifest: StateAdoptionManifest,
    target_root_binding: str,
) -> tuple[str, int, int, dict[str, str]]:
    message = "state CURRENT pointer is invalid"
    value = _parse_store_object(payload, message)
    generation = value.get("generation")
    if (
        set(value)
        != {
            "artifact_digests",
            "generation",
            "generation_device",
            "generation_inode",
            "manifest_digest",
            "plan_fingerprint",
            "target_root_binding",
            "version",
        }
        or value.get("version") != _STATE_STORE_VERSION
        or not isinstance(generation, str)
        or _STATE_GENERATION_ID.fullmatch(generation) is None
        or not isinstance(value.get("generation_device"), int)
        or isinstance(value.get("generation_device"), bool)
        or cast(int, value["generation_device"]) < 0
        or not isinstance(value.get("generation_inode"), int)
        or isinstance(value.get("generation_inode"), bool)
        or cast(int, value["generation_inode"]) < 0
        or value.get("manifest_digest") != _state_manifest_digest(manifest)
        or value.get("target_root_binding") != target_root_binding
        or not isinstance(value.get("plan_fingerprint"), str)
        or _SHA256.fullmatch(cast(str, value["plan_fingerprint"])) is None
    ):
        raise MigrationBlockedError(message)
    return (
        generation,
        cast(int, value["generation_device"]),
        cast(int, value["generation_inode"]),
        _parse_artifact_digests(value.get("artifact_digests"), manifest, message),
    )


def _read_committed_generation(
    *,
    target: _RootGrant,
    manifest: StateAdoptionManifest,
    pointer_payload: bytes,
) -> tuple[dict[str, bytes], set[str]]:
    generation, generation_device, generation_inode, artifact_digests = (
        _parse_current_pointer(
        pointer_payload,
        manifest=manifest,
        target_root_binding=target.binding,
        )
    )
    root_fd = control_fd = generations_fd = generation_fd = artifacts_fd = -1
    try:
        root_fd = _open_pinned_directory(target.path)
        _verify_descriptor_identity(root_fd, target)
        control_fd = _open_directory_at(root_fd, _STATE_CONTROL_DIRECTORY)
        generations_fd = _open_directory_at(control_fd, _STATE_GENERATIONS)
        generation_fd = _open_directory_at(generations_fd, generation)
        generation_metadata = os.fstat(generation_fd)
        if (generation_metadata.st_dev, generation_metadata.st_ino) != (
            generation_device,
            generation_inode,
        ):
            raise MigrationBlockedError("state committed generation identity mismatch")
        ready = _read_regular_at(generation_fd, "READY")
        if ready != pointer_payload:
            raise MigrationBlockedError("state committed generation is not sealed")
        artifacts_fd = _open_directory_at(generation_fd, "artifacts")
        files, directories = _snapshot_tree_at(artifacts_fd)
        _validate_generation(manifest, files, directories, artifact_digests)
        return files, directories
    except FileNotFoundError:
        raise MigrationBlockedError("state committed generation is unavailable") from None
    finally:
        for descriptor in (
            artifacts_fd,
            generation_fd,
            generations_fd,
            control_fd,
            root_fd,
        ):
            if descriptor >= 0:
                os.close(descriptor)


def _snapshot_committed_target(
    target: _RootGrant, manifest: StateAdoptionManifest
) -> tuple[dict[str, bytes], set[str]]:
    root_fd = control_fd = -1
    try:
        root_fd = _open_pinned_directory(target.path)
        _verify_descriptor_identity(root_fd, target)
        entries = sorted(os.scandir(root_fd), key=lambda item: item.name)
        if any(entry.name != _STATE_CONTROL_DIRECTORY for entry in entries):
            raise MigrationBlockedError("state target contains uncommitted entries")
        if not entries:
            return {}, set()
        metadata = entries[0].stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise MigrationBlockedError("state target control directory is invalid")
        control_fd = _open_directory_at(root_fd, _STATE_CONTROL_DIRECTORY)
        current = _read_regular_at(control_fd, _STATE_CURRENT)
        if current is None:
            return {}, set()
        return _read_committed_generation(
            target=target,
            manifest=manifest,
            pointer_payload=current,
        )
    finally:
        for descriptor in (control_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _validate_generation(
    manifest: StateAdoptionManifest,
    files: dict[str, bytes],
    directories: set[str],
    artifact_digests: dict[str, str],
) -> None:
    expected_files, expected_directories, sidecars = _manifest_entries(manifest)
    if set(files) & sidecars:
        raise MigrationBlockedError("state target contains sqlite sidecar")
    if set(files) != expected_files or directories != expected_directories:
        raise MigrationBlockedError("state generation does not match manifest")
    if {
        relative: sha256(payload).hexdigest() for relative, payload in files.items()
    } != artifact_digests:
        raise MigrationBlockedError("state generation digest mismatch")
    for family in manifest.families:
        for artifact in family.artifacts:
            payload = files[str(artifact.relative)]
            if artifact.kind is StateArtifactKind.JSON:
                _inspect_json_artifact(family.family, artifact, payload)
            else:
                _sqlite_evidence_from_payload(
                    payload,
                    artifact=artifact,
                    operation="generation_validation",
                    plan_binding="generation",
                    source_root_binding="generation",
                    source_artifact_binding="generation",
                    observed_sidecars=(),
                )


def _target_transaction_present(target: _RootGrant) -> bool:
    try:
        return (
            read_file(
                target.path,
                PurePosixPath(_STATE_CONTROL_DIRECTORY) / _STATE_JOURNAL,
            )
            is not None
        )
    except (MigrationError, RootConfinementError):
        raise MigrationBlockedError("state transaction probe failed") from None


def plan_state_adoption(
    *,
    issuer: StateCapabilityIssuer,
    capabilities: StatePlanCapabilities,
    sqlite_snapshot: StateSqliteSnapshotCapability | None = None,
) -> StateAdoptionPlan:
    record = issuer._resolve_capability(capabilities, "plan")
    if record.plan_fingerprint is not None:
        raise MigrationBlockedError("state plan capability is already bound")
    target = _grant_for(record, "target")
    if _target_transaction_present(target):
        with issuer._target_lease(record) as store:
            store.recover(canonical_state_manifest())
    plan = _build_state_adoption_plan(
        issuer=issuer,
        record=record,
        capabilities=capabilities,
        sqlite_snapshot=sqlite_snapshot,
    )
    issuer._bind_plan(capabilities, plan.fingerprint)
    return plan


def _build_state_adoption_plan(
    *,
    issuer: StateCapabilityIssuer,
    record: _CapabilityRecord,
    capabilities: StatePlanCapabilities,
    sqlite_snapshot: StateSqliteSnapshotCapability | None,
) -> StateAdoptionPlan:
    manifest = canonical_state_manifest()
    validate_state_manifest(manifest)
    source = _grant_for(record, "source")
    target = _grant_for(record, "target")
    backup = _grant_for(record, "backup")
    expected_files, expected_directories, allowed_source_sidecars = _manifest_entries(manifest)
    source_before, source_directories = _snapshot_state_tree(source.path)
    source_files = set(source_before)
    if (
        not expected_files <= source_files
        or not source_files - expected_files <= allowed_source_sidecars
        or source_directories != expected_directories
    ):
        raise MigrationBlockedError("source does not match state manifest")

    snapshots: list[_StateArtifactSnapshot] = []
    snapshot_capability = sqlite_snapshot or PythonSqliteSnapshotCapability()
    source_handle = issuer._source_handle(record)
    for family in manifest.families:
        for artifact in family.artifacts:
            payload = source_before[str(artifact.relative)]
            if artifact.kind is StateArtifactKind.JSON:
                snapshot = _inspect_json_artifact(family.family, artifact, payload)
            else:
                snapshot = _inspect_sqlite_artifact(
                    family=family.family,
                    artifact=artifact,
                    issuer=issuer,
                    capability_record=record,
                    source=source_handle,
                    source_files=source_before,
                    capability=snapshot_capability,
                )
            snapshots.append(snapshot)

    source_after, source_after_directories = _snapshot_state_tree(source.path)
    if source_after != source_before or source_after_directories != source_directories:
        raise MigrationBlockedError("state source changed during snapshot")

    target_files, target_directories = _snapshot_committed_target(target, manifest)
    expected_target_files = {
        str(artifact.relative): snapshot.payload
        for artifact, snapshot in zip(
            (artifact for family in manifest.families for artifact in family.artifacts),
            snapshots,
            strict=True,
        )
    }
    if not target_files and not target_directories:
        target_state = StateTargetState.EMPTY
    elif target_files == expected_target_files and target_directories == expected_directories:
        target_state = StateTargetState.EXACT_REPLAY
    else:
        if _target_has_idempotency_conflict(manifest, snapshots, target_files):
            raise MigrationBlockedError("state idempotency conflict")
        raise MigrationBlockedError("state target is neither empty nor exact replay")

    manifest_digest = _state_manifest_digest(manifest)
    source_digest = _state_tree_digest(source_before)
    target_digest = _state_tree_digest(target_files)
    key_count = sum(snapshot.evidence.idempotency_key_count for snapshot in snapshots)
    key_digest = _digest_strings(
        [
            f"{snapshot.evidence.relative_digest}:{snapshot.evidence.idempotency_key_digest}"
            for snapshot in snapshots
        ]
    )
    identity = {
        "backup_root_binding": backup.binding,
        "manifest_digest": manifest_digest,
        "source_root_binding": source.binding,
        "source_snapshot_digest": source_digest,
        "snapshot_payload_digests": [
            snapshot.evidence.payload_digest for snapshot in snapshots
        ],
        "target_root_binding": target.binding,
        "target_snapshot_digest": target_digest,
        "target_state": target_state.value,
    }
    fingerprint = sha256(_canonical_json_bytes(identity)).hexdigest()
    return StateAdoptionPlan(
        manifest=manifest,
        capabilities=capabilities,
        source_root_binding=source.binding,
        target_root_binding=target.binding,
        backup_root_binding=backup.binding,
        target_state=target_state,
        source_snapshot_digest=source_digest,
        target_snapshot_digest=target_digest,
        manifest_digest=manifest_digest,
        idempotency_key_count=key_count,
        idempotency_key_digest=key_digest,
        artifacts=tuple(snapshot.evidence for snapshot in snapshots),
        snapshot_payloads=tuple(snapshot.payload for snapshot in snapshots),
        fingerprint=fingerprint,
    )


def validate_state_manifest(manifest: StateAdoptionManifest) -> None:
    if (
        not isinstance(manifest, StateAdoptionManifest)
        or manifest.schema_version != _STATE_MANIFEST_VERSION
    ):
        raise MigrationBlockedError("state manifest version is unsupported")
    if any(not isinstance(family, StateFamilyManifest) for family in manifest.families):
        raise MigrationBlockedError("state manifest family is invalid")
    families = tuple(family.family for family in manifest.families)
    if len(families) != len(StateFamily) or set(families) != set(StateFamily):
        raise MigrationBlockedError("state manifest is incomplete")
    relatives: set[PurePosixPath] = set()
    for family in manifest.families:
        if not isinstance(family, StateFamilyManifest) or not family.artifacts:
            raise MigrationBlockedError("state manifest family is invalid")
        for artifact in family.artifacts:
            _validate_state_artifact(artifact, relatives)
    if manifest != canonical_state_manifest():
        raise MigrationBlockedError("state manifest does not match canonical state manifest")


def _validate_state_artifact(
    artifact: StateArtifact, relatives: set[PurePosixPath]
) -> None:
    if (
        not isinstance(artifact, StateArtifact)
        or not isinstance(artifact.relative, PurePosixPath)
        or not isinstance(artifact.kind, StateArtifactKind)
        or not isinstance(artifact.schema_version, int)
        or isinstance(artifact.schema_version, bool)
        or artifact.schema_version < 1
    ):
        raise MigrationBlockedError("state manifest artifact is invalid")
    try:
        relative = safe_relative(artifact.relative)
    except RootConfinementError:
        raise MigrationBlockedError("state manifest artifact path is unsafe") from None
    if relative in relatives:
        raise MigrationBlockedError("state manifest artifact path is duplicated")
    relatives.add(relative)
    if artifact.kind is StateArtifactKind.SQLITE:
        if artifact.json_keys or not isinstance(artifact.sqlite, StateSqliteSpec):
            raise MigrationBlockedError("state manifest sqlite artifact is invalid")
        _validate_sqlite_spec(artifact.sqlite)
        return
    if not artifact.json_keys or artifact.sqlite is not None:
        raise MigrationBlockedError("state manifest json artifact is invalid")
    for key_spec in artifact.json_keys:
        if (
            not isinstance(key_spec, StateJsonKeySpec)
            or not key_spec.collection_path
            or not key_spec.key_fields
            or any(not isinstance(part, str) or not part for part in key_spec.collection_path)
            or any(not isinstance(field, str) or not field for field in key_spec.key_fields)
        ):
            raise MigrationBlockedError("state manifest idempotency key is invalid")


def _validate_sqlite_spec(spec: StateSqliteSpec) -> None:
    pragma_version = (
        isinstance(spec.application_id, int)
        and not isinstance(spec.application_id, bool)
        and 0 <= spec.application_id <= 2_147_483_647
        and spec.schema_table is None
        and spec.schema_column is None
    )
    explicit_version = (
        spec.application_id is None
        and isinstance(spec.schema_table, str)
        and _SQL_IDENTIFIER.fullmatch(spec.schema_table) is not None
        and isinstance(spec.schema_column, str)
        and _SQL_IDENTIFIER.fullmatch(spec.schema_column) is not None
    )
    if (
        pragma_version == explicit_version
        or not isinstance(spec.schema_digest_sha256, str)
        or _SHA256.fullmatch(spec.schema_digest_sha256) is None
        or not spec.tables
    ):
        raise MigrationBlockedError("state manifest sqlite schema is invalid")
    names: set[str] = set()
    for table in spec.tables:
        if (
            not isinstance(table, StateSqliteTableSpec)
            or _SQL_IDENTIFIER.fullmatch(table.table) is None
            or table.table in names
            or not table.key_columns
            or len(set(table.key_columns)) != len(table.key_columns)
            or any(_SQL_IDENTIFIER.fullmatch(column) is None for column in table.key_columns)
            or not isinstance(table.row_count, int)
            or isinstance(table.row_count, bool)
            or table.row_count < 0
            or not isinstance(table.idempotency_key_digest, str)
            or _SHA256.fullmatch(table.idempotency_key_digest) is None
        ):
            raise MigrationBlockedError("state manifest sqlite table is invalid")
        names.add(table.table)


def _manifest_entries(
    manifest: StateAdoptionManifest,
) -> tuple[set[str], set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    sidecars: set[str] = set()
    for family in manifest.families:
        for artifact in family.artifacts:
            files.add(str(artifact.relative))
            if artifact.kind is StateArtifactKind.SQLITE:
                sidecars.update(
                    str(relative) for relative in _sqlite_sidecar_relatives(artifact.relative)
                )
            for depth in range(1, len(artifact.relative.parts)):
                directories.add(str(PurePosixPath(*artifact.relative.parts[:depth])))
    return files, directories, sidecars


def _snapshot_state_tree(root: Path) -> tuple[dict[str, bytes], set[str]]:
    files: dict[str, bytes] = {}
    directories: set[str] = set()

    def visit(directory: Path, prefix: PurePosixPath | None) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            raise MigrationBlockedError("state snapshot failed") from None
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise MigrationBlockedError("state snapshot failed") from None
            relative = PurePosixPath(entry.name) if prefix is None else prefix / entry.name
            if stat.S_ISLNK(metadata.st_mode):
                raise MigrationBlockedError("state snapshot refuses symlink")
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(str(relative))
                visit(Path(entry.path), relative)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise MigrationBlockedError("state snapshot refuses non-file entry")
            payload = read_file(root, relative)
            if payload is None:
                raise MigrationBlockedError("state snapshot changed during read")
            files[str(relative)] = payload

    visit(root, None)
    return files, directories


def _inspect_json_artifact(
    family: StateFamily,
    artifact: StateArtifact,
    payload: bytes,
) -> _StateArtifactSnapshot:
    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_state_json_object)
        if (
            not isinstance(decoded, dict)
            or decoded.get("schema_version") != artifact.schema_version
        ):
            raise ValueError
        keyed_records: dict[str, str] = {}
        for index, key_spec in enumerate(artifact.json_keys):
            records = _json_collection(decoded, key_spec.collection_path)
            for record in records:
                key = _json_record_key(record, key_spec.key_fields)
                token = f"{artifact.relative}:{index}:{key}"
                if token in keyed_records:
                    raise MigrationBlockedError("state source contains duplicate idempotency key")
                keyed_records[token] = sha256(_canonical_json_bytes(record)).hexdigest()
    except MigrationBlockedError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise MigrationBlockedError("state artifact is invalid") from None
    keys = sorted(keyed_records)
    evidence = StateArtifactEvidence(
        family=family,
        kind=artifact.kind,
        relative_digest=sha256(str(artifact.relative).encode("utf-8")).hexdigest(),
        payload_digest=sha256(payload).hexdigest(),
        schema_version=artifact.schema_version,
        idempotency_key_count=len(keys),
        idempotency_key_digest=_digest_strings(keys),
    )
    return _StateArtifactSnapshot(evidence, payload, keyed_records)


def _snapshot_sqlite_from_read_only_root(
    *,
    source: _RootGrant,
    artifact: StateArtifact,
    operation: str,
    plan_binding: str,
) -> StateSqliteSnapshotEvidence:
    before = _sqlite_source_files(source.path, artifact.relative)
    observed_sidecars = _observed_sqlite_sidecars(before, artifact.relative)
    if observed_sidecars not in {(), ("-shm", "-wal")}:
        raise MigrationBlockedError("sqlite source sidecar set is invalid")
    artifact_binding = _sqlite_artifact_binding(
        source_root_binding=source.binding,
        artifact=artifact,
        files=before,
    )
    connection: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    temporary = TemporaryDirectory(prefix="open-brain-state-snapshot-")
    try:
        database = source.path.joinpath(*artifact.relative.parts)
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        destination = sqlite3.connect(Path(temporary.name) / "snapshot.sqlite3")
        connection.backup(destination)
        journal_mode = destination.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        if journal_mode != "delete":
            raise MigrationBlockedError("sqlite snapshot journal mode is invalid")
        evidence = _sqlite_snapshot_evidence(
            destination,
            artifact=artifact,
            operation=operation,
            plan_binding=plan_binding,
            source_root_binding=source.binding,
            source_artifact_binding=artifact_binding,
            observed_sidecars=observed_sidecars,
        )
    except MigrationBlockedError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise MigrationBlockedError("sqlite snapshot failed") from None
    finally:
        if destination is not None:
            destination.close()
        if connection is not None:
            connection.close()
        temporary.cleanup()
    after = _sqlite_source_files(source.path, artifact.relative)
    if after != before:
        raise MigrationBlockedError("sqlite source changed during snapshot")
    return evidence


def _sqlite_source_files(root: Path, relative: PurePosixPath) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for item in (relative, *_sqlite_sidecar_relatives(relative)):
        payload = read_file(root, item)
        if payload is not None:
            files[str(item)] = payload
    return files


def _inspect_sqlite_artifact(
    *,
    family: StateFamily,
    artifact: StateArtifact,
    issuer: StateCapabilityIssuer,
    capability_record: _CapabilityRecord,
    source: StateReadOnlySourceHandle,
    source_files: dict[str, bytes],
    capability: StateSqliteSnapshotCapability,
) -> _StateArtifactSnapshot:
    spec = artifact.sqlite
    if not isinstance(spec, StateSqliteSpec):
        raise MigrationBlockedError("state manifest sqlite artifact is invalid")
    expected_sidecars = _observed_sqlite_sidecars(source_files, artifact.relative)
    if expected_sidecars not in {(), ("-shm", "-wal")}:
        raise MigrationBlockedError("sqlite source sidecar set is invalid")
    source_grant = _grant_for(capability_record, "source")
    source_artifact_binding = _sqlite_artifact_binding(
        source_root_binding=source_grant.binding,
        artifact=artifact,
        files=source_files,
    )
    request = StateSqliteSnapshotRequest(source=source, artifact=artifact)
    try:
        receipt = capability.snapshot(request)
    except MigrationBlockedError:
        raise
    except Exception:
        raise MigrationBlockedError("sqlite snapshot failed") from None
    if not isinstance(receipt, StateSqliteSnapshotReceipt):
        raise MigrationBlockedError("sqlite snapshot receipt is invalid")
    resolved = issuer._resolve_sqlite_receipt(receipt, capability_record, artifact)
    if resolved.source_artifact_binding != source_artifact_binding:
        raise MigrationBlockedError("sqlite snapshot receipt is unbound")
    if resolved.copied_sidecars:
        raise MigrationBlockedError("sqlite snapshot copied sidecar")
    if resolved.integrity_check != "ok":
        raise MigrationBlockedError("sqlite integrity check failed")
    if resolved.observed_sidecars != expected_sidecars:
        raise MigrationBlockedError("sqlite snapshot sidecar evidence is invalid")
    if resolved.snapshot_digest_sha256 != sha256(resolved.snapshot_payload).hexdigest():
        raise MigrationBlockedError("sqlite snapshot digest mismatch")
    verified = _sqlite_evidence_from_payload(
        resolved.snapshot_payload,
        artifact=artifact,
        operation=capability_record.operation,
        plan_binding=capability_record.capability_binding,
        source_root_binding=source_grant.binding,
        source_artifact_binding=source_artifact_binding,
        observed_sidecars=expected_sidecars,
    )
    if resolved != verified:
        raise MigrationBlockedError("sqlite snapshot receipt is invalid")
    key_count = sum(table.idempotency_key_count for table in resolved.tables)
    key_digest = _digest_strings(
        [f"{table.table}:{table.idempotency_key_digest}" for table in resolved.tables]
    )
    evidence = StateArtifactEvidence(
        family=family,
        kind=artifact.kind,
        relative_digest=sha256(str(artifact.relative).encode()).hexdigest(),
        payload_digest=resolved.snapshot_digest_sha256,
        schema_version=artifact.schema_version,
        idempotency_key_count=key_count,
        idempotency_key_digest=key_digest,
        sqlite_integrity_check=resolved.integrity_check,
        sqlite_schema_digest=resolved.schema_digest_sha256,
        sqlite_tables=resolved.tables,
        sqlite_sidecars_observed=resolved.observed_sidecars,
    )
    return _StateArtifactSnapshot(evidence, resolved.snapshot_payload, {})


def _sqlite_evidence_from_payload(
    payload: bytes,
    *,
    artifact: StateArtifact,
    operation: str,
    plan_binding: str,
    source_root_binding: str,
    source_artifact_binding: str,
    observed_sidecars: tuple[str, ...],
) -> StateSqliteSnapshotEvidence:
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(payload)
        return _sqlite_snapshot_evidence(
            connection,
            artifact=artifact,
            operation=operation,
            plan_binding=plan_binding,
            source_root_binding=source_root_binding,
            source_artifact_binding=source_artifact_binding,
            observed_sidecars=observed_sidecars,
        )
    except (sqlite3.Error, TypeError, ValueError):
        raise MigrationBlockedError("sqlite snapshot payload is invalid") from None
    finally:
        connection.close()


def _sqlite_snapshot_evidence(
    connection: sqlite3.Connection,
    *,
    artifact: StateArtifact,
    operation: str,
    plan_binding: str,
    source_root_binding: str,
    source_artifact_binding: str,
    observed_sidecars: tuple[str, ...],
) -> StateSqliteSnapshotEvidence:
    spec = artifact.sqlite
    if not isinstance(spec, StateSqliteSpec):
        raise MigrationBlockedError("state manifest sqlite artifact is invalid")
    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    if integrity_rows != [("ok",)]:
        raise MigrationBlockedError("sqlite integrity check failed")
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    explicit_schema_version: int | None = None
    if spec.application_id is not None:
        if application_id != spec.application_id or user_version != artifact.schema_version:
            raise MigrationBlockedError("sqlite schema version mismatch")
    else:
        assert spec.schema_table is not None
        assert spec.schema_column is not None
        version_rows = connection.execute(
            f"SELECT {_quote_sql_identifier(spec.schema_column)} "
            f"FROM {_quote_sql_identifier(spec.schema_table)}"
        ).fetchall()
        if (
            len(version_rows) != 1
            or not isinstance(version_rows[0][0], int)
            or isinstance(version_rows[0][0], bool)
        ):
            raise MigrationBlockedError("sqlite explicit schema version is invalid")
        explicit_schema_version = int(version_rows[0][0])
        if explicit_schema_version != artifact.schema_version:
            raise MigrationBlockedError("sqlite schema version mismatch")
    schema_digest = _sqlite_schema_digest(connection)
    if schema_digest != spec.schema_digest_sha256:
        raise MigrationBlockedError("sqlite schema drift")
    tables = tuple(_sqlite_table_evidence(connection, table) for table in spec.tables)
    payload = connection.serialize()
    return StateSqliteSnapshotEvidence(
        version=1,
        operation=operation,
        plan_binding=plan_binding,
        source_root_binding=source_root_binding,
        source_artifact_binding=source_artifact_binding,
        artifact_relative=artifact.relative,
        snapshot_payload=payload,
        snapshot_digest_sha256=sha256(payload).hexdigest(),
        integrity_check="ok",
        application_id=application_id,
        user_version=user_version,
        explicit_schema_version=explicit_schema_version,
        schema_digest_sha256=schema_digest,
        tables=tables,
        observed_sidecars=observed_sidecars,
        copied_sidecars=(),
    )


def _sqlite_table_evidence(
    connection: sqlite3.Connection, spec: StateSqliteTableSpec
) -> StateSqliteTableEvidence:
    table = _quote_sql_identifier(spec.table)
    columns = ", ".join(_quote_sql_identifier(column) for column in spec.key_columns)
    try:
        row_count = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        rows = connection.execute(
            f"SELECT {columns} FROM {table} ORDER BY {columns}"
        ).fetchall()
    except sqlite3.Error:
        raise MigrationBlockedError("sqlite declared table is unavailable") from None
    keys: list[list[str | int]] = []
    encoded: set[bytes] = set()
    for row in rows:
        key: list[str | int] = []
        for value in row:
            if not isinstance(value, str | int) or isinstance(value, bool):
                raise MigrationBlockedError("sqlite idempotency key is invalid")
            key.append(value)
        encoded_key = _canonical_json_bytes(key)
        if encoded_key in encoded:
            raise MigrationBlockedError("sqlite idempotency key is duplicated")
        encoded.add(encoded_key)
        keys.append(key)
    keys.sort(key=_canonical_json_bytes)
    key_digest = sha256(_canonical_json_bytes(keys)).hexdigest()
    if row_count != spec.row_count or len(keys) != row_count:
        raise MigrationBlockedError("sqlite row reconciliation mismatch")
    if key_digest != spec.idempotency_key_digest:
        raise MigrationBlockedError("sqlite idempotency reconciliation mismatch")
    return StateSqliteTableEvidence(
        table=spec.table,
        row_count=row_count,
        idempotency_key_count=len(keys),
        idempotency_key_digest=key_digest,
    )


def _sqlite_schema_digest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return sha256(_canonical_json_bytes([list(row) for row in rows])).hexdigest()


def _quote_sql_identifier(value: str) -> str:
    if _SQL_IDENTIFIER.fullmatch(value) is None:
        raise MigrationBlockedError("sqlite identifier is invalid")
    return f'"{value}"'


def _sqlite_sidecar_relatives(relative: PurePosixPath) -> tuple[PurePosixPath, ...]:
    return tuple(PurePosixPath(str(relative) + suffix) for suffix in ("-shm", "-wal"))


def _observed_sqlite_sidecars(
    files: dict[str, bytes], relative: PurePosixPath
) -> tuple[str, ...]:
    return tuple(
        suffix
        for suffix in ("-shm", "-wal")
        if str(PurePosixPath(str(relative) + suffix)) in files
    )


def _observed_sqlite_sidecars_from_root(
    root: Path, relative: PurePosixPath
) -> tuple[str, ...]:
    return tuple(
        suffix
        for suffix in ("-shm", "-wal")
        if read_file(root, PurePosixPath(str(relative) + suffix)) is not None
    )


def _sqlite_artifact_binding(
    *,
    source_root_binding: str,
    artifact: StateArtifact,
    files: dict[str, bytes],
) -> str:
    relatives = (artifact.relative, *_sqlite_sidecar_relatives(artifact.relative))
    digests = {
        str(relative): (
            sha256(files[str(relative)]).hexdigest() if str(relative) in files else None
        )
        for relative in relatives
    }
    if digests[str(artifact.relative)] is None:
        raise MigrationBlockedError("sqlite source artifact is unavailable")
    value = {
        "artifact": str(artifact.relative),
        "files": digests,
        "source_root_binding": source_root_binding,
    }
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _state_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _json_collection(value: dict[str, object], path: tuple[str, ...]) -> list[dict[str, object]]:
    current: object = value
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise ValueError
        current = current[part]
    if not isinstance(current, list):
        raise ValueError
    records: list[dict[str, object]] = []
    for record in current:
        if not isinstance(record, dict):
            raise ValueError
        records.append(record)
    return records


def _json_record_key(record: dict[str, object], fields: tuple[str, ...]) -> str:
    values: list[str | int] = []
    for field in fields:
        value = record.get(field)
        if not isinstance(value, str | int) or isinstance(value, bool):
            raise ValueError
        values.append(value)
    return _canonical_json_bytes(values).decode("utf-8")


def _target_has_idempotency_conflict(
    manifest: StateAdoptionManifest,
    source_snapshots: list[_StateArtifactSnapshot],
    target_files: dict[str, bytes],
) -> bool:
    by_relative = {
        str(artifact.relative): (family.family, artifact)
        for family in manifest.families
        for artifact in family.artifacts
        if artifact.kind is StateArtifactKind.JSON
    }
    artifacts = [artifact for family in manifest.families for artifact in family.artifacts]
    source_by_relative = {
        str(artifact.relative): snapshot
        for artifact, snapshot in zip(artifacts, source_snapshots, strict=True)
        if artifact.kind is StateArtifactKind.JSON
    }
    for relative, payload in target_files.items():
        declared = by_relative.get(relative)
        source = source_by_relative.get(relative)
        if declared is None or source is None:
            continue
        family, artifact = declared
        try:
            target = _inspect_json_artifact(family, artifact, payload)
        except MigrationBlockedError:
            continue
        for key in source.keyed_records.keys() & target.keyed_records.keys():
            if source.keyed_records[key] != target.keyed_records[key]:
                return True
    return False


def _state_manifest_digest(manifest: StateAdoptionManifest) -> str:
    value = {
        "families": [
            {
                "artifacts": [
                    {
                        "json_keys": [
                            {
                                "collection_path": list(key.collection_path),
                                "key_fields": list(key.key_fields),
                            }
                            for key in artifact.json_keys
                        ],
                        "kind": artifact.kind.value,
                        "relative": str(artifact.relative),
                        "schema_version": artifact.schema_version,
                        "sqlite": (
                            None
                            if artifact.sqlite is None
                            else {
                                "application_id": artifact.sqlite.application_id,
                                "schema_column": artifact.sqlite.schema_column,
                                "schema_digest_sha256": (
                                    artifact.sqlite.schema_digest_sha256
                                ),
                                "schema_table": artifact.sqlite.schema_table,
                                "tables": [
                                    {
                                        "idempotency_key_digest": (
                                            table.idempotency_key_digest
                                        ),
                                        "key_columns": list(table.key_columns),
                                        "row_count": table.row_count,
                                        "table": table.table,
                                    }
                                    for table in artifact.sqlite.tables
                                ],
                            }
                        ),
                    }
                    for artifact in family.artifacts
                ],
                "family": family.family.value,
            }
            for family in manifest.families
        ],
        "schema_version": manifest.schema_version,
    }
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _state_tree_digest(files: dict[str, bytes]) -> str:
    digests = {relative: sha256(payload).hexdigest() for relative, payload in sorted(files.items())}
    return sha256(_canonical_json_bytes(digests)).hexdigest()


def _digest_strings(values: list[str]) -> str:
    return sha256(_canonical_json_bytes(sorted(values))).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _create_logical_state_backup(
    *,
    target_files: dict[str, bytes],
    backup_root: Path,
    restore_root: Path,
    relatives: tuple[PurePosixPath, ...],
) -> tuple[BackupReceipt, RestoreReceipt]:
    temporary = TemporaryDirectory(prefix="open-brain-state-backup-view-")
    logical_root = Path(temporary.name)
    try:
        for relative, payload in target_files.items():
            replace_file(
                logical_root,
                PurePosixPath(relative),
                payload,
                require_existing=False,
            )
        backup = create_backup(
            target_root=logical_root,
            backup_root=backup_root,
            relatives=relatives,
        )
        disposable_restore = restore_backup_copy(backup, target_root=restore_root)
        return backup, disposable_restore
    finally:
        temporary.cleanup()


def apply_state_adoption(
    *,
    plan: StateAdoptionPlan,
    issuer: StateCapabilityIssuer,
    capabilities: StateApplyCapabilities,
    restore_root: Path,
    sqlite_snapshot: StateSqliteSnapshotCapability | None = None,
    publisher: StateArtifactPublisher | None = None,
    read_back: StateArtifactReadBack | None = None,
    crash_hook: Callable[[str], None] | None = None,
) -> StateAdoptionReceipt:
    if not isinstance(plan, StateAdoptionPlan) or not plan.ready:
        raise MigrationBlockedError("state adoption plan is not ready")
    capability_record = issuer._resolve_apply(capabilities, plan, restore_root)
    source = _grant_for(capability_record, "source")
    target = _grant_for(capability_record, "target")
    backup_root = _grant_for(capability_record, "backup")
    restore = _grant_for(capability_record, "restore")
    _validate_disposable_restore_root(restore.path)
    hook = crash_hook or (lambda _point: None)
    with issuer._target_lease(capability_record) as store:
        store.recover(plan.manifest)
        try:
            current = _build_state_adoption_plan(
                issuer=issuer,
                record=capability_record,
                capabilities=plan.capabilities,
                sqlite_snapshot=sqlite_snapshot,
            )
        except (MigrationError, RootConfinementError):
            raise StaleMigrationPlanError("state adoption plan is stale") from None
        if current != plan:
            raise StaleMigrationPlanError("state adoption plan is stale")

        source_before, source_directories = _snapshot_state_tree(source.path)
        target_before, target_directories = _snapshot_committed_target(
            target, plan.manifest
        )
        if (
            _state_tree_digest(source_before) != plan.source_snapshot_digest
            or _state_tree_digest(target_before) != plan.target_snapshot_digest
        ):
            raise StaleMigrationPlanError("state adoption plan is stale")
        if plan.target_state is StateTargetState.EXACT_REPLAY:
            _verify_unchanged_tree(
                root=source.path,
                files=source_before,
                directories=source_directories,
                message="state source changed during no-op",
            )
            hook("before_noop_receipt")
            try:
                observed_target = _snapshot_committed_target(target, plan.manifest)
            except MigrationBlockedError:
                raise StaleMigrationPlanError("state target changed during no-op") from None
            if observed_target != (target_before, target_directories):
                raise StaleMigrationPlanError("state target changed during no-op")
            adoption_evidence = StateAdoptionReceiptEvidence(
                schema_version=1,
                operation="apply",
                state=MigrationState.NOOP,
                plan_fingerprint=plan.fingerprint,
                manifest_digest=plan.manifest_digest,
                source_snapshot_digest=plan.source_snapshot_digest,
                target_before_digest=plan.target_snapshot_digest,
                target_after_digest=plan.target_snapshot_digest,
                write_count=0,
                duplicate_idempotency_keys=0,
                duplicate_captures=0,
                backup=None,
                disposable_restore=None,
            )
            receipt = issuer._issue_adoption_receipt(
                plan_fingerprint=plan.fingerprint,
                capability=capability_record,
                evidence=adoption_evidence,
            )
            issuer.inspect_adoption_receipt(receipt)
            store.verify_pins()
            return receipt
        if plan.target_state is not StateTargetState.EMPTY:
            raise MigrationBlockedError("state adoption target is not writable")

        flattened = tuple(
            (family.family, artifact, payload, evidence)
            for family, snapshots in _zip_manifest_snapshots(plan)
            for artifact, payload, evidence in snapshots
        )
        relatives = tuple(
            artifact.relative for _family, artifact, _payload, _evidence in flattened
        )
        backup, disposable_restore = _create_logical_state_backup(
            target_files=target_before,
            backup_root=backup_root.path,
            restore_root=restore.path,
            relatives=relatives,
        )
        if tuple(entry.relative for entry in backup.entries) != tuple(
            sorted(set(relatives))
        ):
            raise MigrationError("state adoption backup verification failed")
        restored_files, restored_directories = _snapshot_state_tree(restore.path)
        if restored_files != target_before or restored_directories != target_directories:
            raise MigrationError("state adoption disposable restore mismatch")
        _verify_unchanged_tree(
            root=source.path,
            files=source_before,
            directories=source_directories,
            message="state source changed before publication",
        )
        if _snapshot_committed_target(target, plan.manifest) != (
            target_before,
            target_directories,
        ):
            raise StaleMigrationPlanError("state target changed before publication")

        expected_target = {
            str(artifact.relative): payload
            for _family, artifact, payload, _evidence in flattened
        }
        artifact_digests = {
            relative: sha256(payload).hexdigest()
            for relative, payload in expected_target.items()
        }
        transaction = store.begin(plan=plan, artifact_digests=artifact_digests)
        generation_root = store.generation_artifact_root(transaction)
        try:
            for index, (family, artifact, payload, artifact_evidence) in enumerate(
                flattened
            ):
                if publisher is None:
                    store.write_artifact(transaction, artifact.relative, payload)
                else:
                    publisher.publish(
                        target_root=generation_root,
                        relative=artifact.relative,
                        payload=payload,
                    )
                observed = (
                    store.read_artifact(transaction, artifact.relative)
                    if read_back is None
                    else read_back.read(
                        target_root=generation_root,
                        relative=artifact.relative,
                    )
                )
                if observed != payload:
                    raise MigrationError("state adoption read-back mismatch")
                _validate_published_artifact(
                    family=family,
                    artifact=artifact,
                    payload=observed,
                    expected=artifact_evidence,
                    source_root=source.path,
                    operation=capability_record.operation,
                    plan_binding=capability_record.capability_binding,
                    source_root_binding=source.binding,
                    source_files=source_before,
                )
                if index == 0:
                    hook("stage")
            staged_files, staged_directories = store.seal(
                transaction, plan.manifest
            )
            hook("before_generation_rename")
            store.promote(transaction)
            hook("generation_rename")
            _verify_unchanged_tree(
                root=source.path,
                files=source_before,
                directories=source_directories,
                message="state source changed during publication",
            )
            if _snapshot_committed_target(target, plan.manifest) != (
                target_before,
                target_directories,
            ):
                raise StaleMigrationPlanError("state target changed before commit")
            hook("before_pointer_publish")
            store.publish_current(transaction)
            hook("pointer_publish")
            target_after, target_after_directories = _snapshot_committed_target(
                target, plan.manifest
            )
            if (
                staged_files != expected_target
                or target_after != expected_target
                or target_after_directories != staged_directories
            ):
                raise MigrationError("state adoption target verification failed")
            _verify_unchanged_tree(
                root=source.path,
                files=source_before,
                directories=source_directories,
                message="state source changed during publication",
            )

            target_after_digest = _state_tree_digest(target_after)
            backup_evidence = StateAuthorityReceiptEvidence(
                version=1,
                operation="backup",
                plan_fingerprint=plan.fingerprint,
                root_bindings=(target.binding, backup_root.binding),
                expires_at=capability_record.expires_at,
                tracked_count=len(backup.entries),
                file_count=backup.file_count,
                restored_count=0,
                removed_count=0,
            )
            backup_authority = issuer._issue_authority_receipt(
                operation="backup",
                plan_fingerprint=plan.fingerprint,
                capability=capability_record,
                roots=(target, backup_root),
                evidence=backup_evidence,
            )
            restore_evidence = StateAuthorityReceiptEvidence(
                version=1,
                operation="restore",
                plan_fingerprint=plan.fingerprint,
                root_bindings=(backup_root.binding, restore.binding),
                expires_at=capability_record.expires_at,
                tracked_count=len(backup.entries),
                file_count=backup.file_count,
                restored_count=disposable_restore.restored_count,
                removed_count=disposable_restore.removed_count,
            )
            restore_authority = issuer._issue_authority_receipt(
                operation="restore",
                plan_fingerprint=plan.fingerprint,
                capability=capability_record,
                roots=(backup_root, restore),
                evidence=restore_evidence,
            )
            adoption_evidence = StateAdoptionReceiptEvidence(
                schema_version=1,
                operation="apply",
                state=MigrationState.APPLIED,
                plan_fingerprint=plan.fingerprint,
                manifest_digest=plan.manifest_digest,
                source_snapshot_digest=plan.source_snapshot_digest,
                target_before_digest=plan.target_snapshot_digest,
                target_after_digest=target_after_digest,
                write_count=len(flattened),
                duplicate_idempotency_keys=0,
                duplicate_captures=0,
                backup=backup_authority,
                disposable_restore=restore_authority,
            )
            receipt = issuer._issue_adoption_receipt(
                plan_fingerprint=plan.fingerprint,
                capability=capability_record,
                evidence=adoption_evidence,
            )
            issuer.inspect_authority_receipt(backup_authority, operation="backup")
            issuer.inspect_authority_receipt(restore_authority, operation="restore")
            issuer.inspect_adoption_receipt(receipt)
            store.finish(transaction)
            hook("journal_cleanup")
            return receipt
        except BaseException:
            try:
                store.abort_precommit(transaction)
                store.recover(plan.manifest)
            except Exception:
                raise MigrationError("state adoption recovery failed") from None
            raise


def _validate_disposable_restore_root(restore_root: Path) -> None:
    try:
        validate_root(restore_root)
        if any(restore_root.iterdir()):
            raise MigrationBlockedError("state restore root must be empty")
    except MigrationBlockedError:
        raise
    except (OSError, RootConfinementError):
        raise MigrationBlockedError("state restore root is invalid") from None


def _zip_manifest_snapshots(
    plan: StateAdoptionPlan,
) -> tuple[
    tuple[
        StateFamilyManifest,
        tuple[tuple[StateArtifact, bytes, StateArtifactEvidence], ...],
    ],
    ...,
]:
    artifacts = [artifact for family in plan.manifest.families for artifact in family.artifacts]
    paired = iter(zip(artifacts, plan.snapshot_payloads, plan.artifacts, strict=True))
    grouped: list[
        tuple[
            StateFamilyManifest,
            tuple[tuple[StateArtifact, bytes, StateArtifactEvidence], ...],
        ]
    ] = []
    for family in plan.manifest.families:
        grouped.append((family, tuple(next(paired) for _artifact in family.artifacts)))
    return tuple(grouped)


def _validate_published_artifact(
    *,
    family: StateFamily,
    artifact: StateArtifact,
    payload: bytes,
    expected: StateArtifactEvidence,
    source_root: Path,
    operation: str,
    plan_binding: str,
    source_root_binding: str,
    source_files: dict[str, bytes],
) -> None:
    if artifact.kind is StateArtifactKind.JSON:
        observed = _inspect_json_artifact(family, artifact, payload).evidence
        if observed != expected:
            raise MigrationError("state JSON read-back validation failed")
        return
    source_artifact_binding = _sqlite_artifact_binding(
        source_root_binding=source_root_binding,
        artifact=artifact,
        files=source_files,
    )
    receipt = _sqlite_evidence_from_payload(
        payload,
        artifact=artifact,
        operation=operation,
        plan_binding=plan_binding,
        source_root_binding=source_root_binding,
        source_artifact_binding=source_artifact_binding,
        observed_sidecars=_observed_sqlite_sidecars_from_root(
            source_root, artifact.relative
        ),
    )
    if (
        receipt.snapshot_digest_sha256 != expected.payload_digest
        or receipt.integrity_check != expected.sqlite_integrity_check
        or receipt.schema_digest_sha256 != expected.sqlite_schema_digest
        or receipt.tables != expected.sqlite_tables
        or receipt.observed_sidecars != expected.sqlite_sidecars_observed
    ):
        raise MigrationError("state SQLite read-back validation failed")


def _verify_unchanged_tree(
    *,
    root: Path,
    files: dict[str, bytes],
    directories: set[str],
    message: str,
) -> None:
    observed_files, observed_directories = _snapshot_state_tree(root)
    if observed_files != files or observed_directories != directories:
        raise StaleMigrationPlanError(message)


def plan_state_backfill(
    *,
    vault_root: Path,
    state_root: Path,
    state_relative: str | PurePosixPath = "capture-state.json",
) -> MigrationPlan:
    relative = safe_relative(state_relative)
    note_paths = walk_markdown(vault_root)
    page_paths: dict[str, list[PurePosixPath]] = {}
    issues: list[MigrationIssue] = []
    for note_path in note_paths:
        payload = read_file(vault_root, note_path)
        try:
            document = parse_markdown(payload if payload is not None else b"")
            page_id = document.fields["page_id"]
            if not isinstance(page_id, str) or not _PAGE_ID.fullmatch(page_id):
                raise ValueError
        except (KeyError, MarkdownFormatError, ValueError):
            issues.append(MigrationIssue(IssueCode.MALFORMED_MARKDOWN))
            continue
        page_paths.setdefault(page_id, []).append(note_path)
    for page_id, paths in sorted(page_paths.items()):
        if len(paths) > 1:
            issues.append(MigrationIssue(IssueCode.DUPLICATE_PAGE_ID, page_id))

    state_payload = read_file(state_root, relative)
    state: dict[str, object]
    if state_payload is None:
        state = {"processed_page_ids": [], "schema_version": 1}
    else:
        try:
            raw_state = json.loads(state_payload.decode("utf-8"))
            if not isinstance(raw_state, dict):
                raise ValueError
            raw_ids = raw_state.get("processed_page_ids")
            if not isinstance(raw_ids, list) or any(
                not isinstance(item, str) or not _PAGE_ID.fullmatch(item) for item in raw_ids
            ):
                raise ValueError
            state = dict(raw_state)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            issues.append(MigrationIssue(IssueCode.MALFORMED_STATE))
            state = {"processed_page_ids": [], "schema_version": 1}

    existing_value = state.get("processed_page_ids", [])
    existing = set(existing_value) if isinstance(existing_value, list) else set()
    missing = sorted(set(page_paths) - existing)
    actions: tuple[MigrationAction, ...] = ()
    if missing:
        updated = dict(state)
        updated["processed_page_ids"] = sorted(existing | set(missing))
        updated.setdefault("schema_version", 1)
        payload = json.dumps(updated, sort_keys=True, separators=(",", ":")).encode("utf-8")
        actions = (MigrationAction(ActionKind.WRITE, relative, payload=payload),)
    return build_plan(
        kind=MigrationKind.STATE_BACKFILL,
        vault_root=vault_root,
        target_root=state_root,
        scanned_count=len(note_paths),
        action_count=len(missing),
        actions=actions,
        issues=tuple(issues),
    )


def apply_state_backfill(*, plan: MigrationPlan, backup_root: Path) -> MigrationResult:
    if plan.kind is not MigrationKind.STATE_BACKFILL:
        raise MigrationBlockedError("invalid migration plan")
    if plan.issues:
        raise MigrationBlockedError("migration plan is blocked")
    state_relative = plan.actions[0].target if plan.actions else PurePosixPath("capture-state.json")
    current = plan_state_backfill(
        vault_root=plan.vault_root,
        state_root=plan.target_root,
        state_relative=state_relative,
    )
    if (
        current.fingerprint != plan.fingerprint
        or current.vault_root != plan.vault_root
        or current.target_root != plan.target_root
        or current.actions != plan.actions
    ):
        raise StaleMigrationPlanError("migration inputs changed after dry-run")
    if not plan.actions:
        return MigrationResult(MigrationState.NOOP, 0, None)
    action = plan.actions[0]
    if action.payload is None:
        raise MigrationBlockedError("invalid migration plan")
    backup = create_backup(
        target_root=plan.target_root,
        backup_root=backup_root,
        relatives=(action.target,),
    )
    try:
        replace_file(plan.target_root, action.target, action.payload, require_existing=None)
    except BaseException:
        try:
            restore_backup(backup, target_root=plan.target_root)
        except Exception:
            raise MigrationError("migration rollback failed") from None
        raise
    return MigrationResult(MigrationState.APPLIED, plan.action_count, backup)
