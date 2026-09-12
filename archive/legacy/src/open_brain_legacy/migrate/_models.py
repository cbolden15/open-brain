from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Self


class MigrationError(Exception):
    """A migration could not complete safely."""


class MigrationBlockedError(MigrationError):
    """A dry-run found issues that prohibit apply."""


class StaleMigrationPlanError(MigrationError):
    """The migration inputs changed after the dry-run."""


class MigrationKind(StrEnum):
    STATE_BACKFILL = "state_backfill"
    PROCESSED_AT_BACKFILL = "processed_at_backfill"
    CONTENT_LAYOUT = "content_layout"


class MigrationState(StrEnum):
    APPLIED = "applied"
    NOOP = "noop"


class StateFamily(StrEnum):
    QUEUE_REQUESTED_VIDEO = "queue-requested-video"
    CAPTURE_CONTEXT = "capture-context"
    EVENT_LEDGERS = "event-ledgers"
    REVIEW_AUDIT = "review-audit"
    LEDGER_INFLIGHT = "ledger-inflight"
    PROVIDER_RETRIEVAL = "provider-retrieval"
    RECOVERY_BACKUP = "recovery-backup"


class StateArtifactKind(StrEnum):
    JSON = "json"
    SQLITE = "sqlite"


class StateTargetState(StrEnum):
    EMPTY = "empty"
    EXACT_REPLAY = "exact_replay"


class IssueCode(StrEnum):
    DUPLICATE_PAGE_ID = "duplicate_page_id"
    MALFORMED_MARKDOWN = "malformed_markdown"
    MALFORMED_STATE = "malformed_state"
    STRANDED_NOTE = "stranded_note"


class ActionKind(StrEnum):
    WRITE = "write"
    MOVE = "move"


@dataclass(frozen=True, slots=True)
class MigrationIssue:
    code: IssueCode
    page_id: str | None = None


@dataclass(frozen=True, slots=True)
class StateJsonKeySpec:
    collection_path: tuple[str, ...]
    key_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StateSqliteTableSpec:
    table: str
    key_columns: tuple[str, ...]
    row_count: int
    idempotency_key_digest: str


@dataclass(frozen=True, slots=True)
class StateSqliteSpec:
    application_id: int | None
    schema_table: str | None
    schema_column: str | None
    schema_digest_sha256: str
    tables: tuple[StateSqliteTableSpec, ...]


@dataclass(frozen=True, slots=True)
class StateArtifact:
    relative: PurePosixPath
    kind: StateArtifactKind
    schema_version: int
    json_keys: tuple[StateJsonKeySpec, ...]
    sqlite: StateSqliteSpec | None = None


@dataclass(frozen=True, slots=True)
class StateFamilyManifest:
    family: StateFamily
    artifacts: tuple[StateArtifact, ...]


@dataclass(frozen=True, slots=True)
class StateAdoptionManifest:
    schema_version: int
    families: tuple[StateFamilyManifest, ...]


class _OpaqueAuthority:
    _token: object
    __slots__ = ("_token",)

    def __init__(self) -> None:
        raise TypeError("authority objects are issuer-created")

    @classmethod
    def _issued(cls, token: object) -> Self:
        instance = object.__new__(cls)
        instance._token = token
        return instance

    def _authority_token(self) -> object:
        return self._token


class StatePlanCapabilities(_OpaqueAuthority):
    pass


class StateApplyCapabilities(_OpaqueAuthority):
    pass


class StateAuthorityReceipt(_OpaqueAuthority):
    pass


class StateSqliteSnapshotReceipt(StateAuthorityReceipt):
    pass


class StateAdoptionReceipt(StateAuthorityReceipt):
    pass


class StateReadOnlySourceHandle(_OpaqueAuthority):
    _snapshot_sqlite: Callable[[StateArtifact], StateSqliteSnapshotReceipt]
    __slots__ = ("_snapshot_sqlite",)

    @classmethod
    def _issued_reader(
        cls,
        token: object,
        snapshot_sqlite: Callable[[StateArtifact], StateSqliteSnapshotReceipt],
    ) -> Self:
        instance = cls._issued(token)
        instance._snapshot_sqlite = snapshot_sqlite
        return instance

    def snapshot_sqlite(self, artifact: StateArtifact) -> StateSqliteSnapshotReceipt:
        return self._snapshot_sqlite(artifact)


@dataclass(frozen=True, slots=True)
class StateSqliteSnapshotRequest:
    source: StateReadOnlySourceHandle
    artifact: StateArtifact


@dataclass(frozen=True, slots=True)
class StateSqliteTableEvidence:
    table: str
    row_count: int
    idempotency_key_count: int
    idempotency_key_digest: str


@dataclass(frozen=True, slots=True)
class StateSqliteSnapshotEvidence:
    version: int
    operation: str
    plan_binding: str
    source_root_binding: str
    source_artifact_binding: str
    artifact_relative: PurePosixPath
    snapshot_payload: bytes
    snapshot_digest_sha256: str
    integrity_check: str
    application_id: int
    user_version: int
    explicit_schema_version: int | None
    schema_digest_sha256: str
    tables: tuple[StateSqliteTableEvidence, ...]
    observed_sidecars: tuple[str, ...]
    copied_sidecars: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StateArtifactEvidence:
    family: StateFamily
    kind: StateArtifactKind
    relative_digest: str
    payload_digest: str
    schema_version: int
    idempotency_key_count: int
    idempotency_key_digest: str
    sqlite_integrity_check: str | None = None
    sqlite_schema_digest: str | None = None
    sqlite_tables: tuple[StateSqliteTableEvidence, ...] = ()
    sqlite_sidecars_observed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StateAdoptionPlan:
    manifest: StateAdoptionManifest
    capabilities: StatePlanCapabilities
    source_root_binding: str
    target_root_binding: str
    backup_root_binding: str
    target_state: StateTargetState
    source_snapshot_digest: str
    target_snapshot_digest: str
    manifest_digest: str
    idempotency_key_count: int
    idempotency_key_digest: str
    artifacts: tuple[StateArtifactEvidence, ...]
    snapshot_payloads: tuple[bytes, ...]
    fingerprint: str

    @property
    def ready(self) -> bool:
        return True

    @property
    def copy_count(self) -> int:
        return len(self.artifacts) if self.target_state is StateTargetState.EMPTY else 0

    def to_redacted_dict(self) -> dict[str, object]:
        return {
            "artifact_count": len(self.artifacts),
            "copy_count": self.copy_count,
            "family_count": len(self.manifest.families),
            "idempotency_key_count": self.idempotency_key_count,
            "manifest_schema_version": self.manifest.schema_version,
            "ready": self.ready,
            "source_snapshot_digest": self.source_snapshot_digest,
            "target_state": self.target_state.value,
        }


@dataclass(frozen=True, slots=True)
class MigrationAction:
    kind: ActionKind
    target: PurePosixPath
    source: PurePosixPath | None = None
    payload: bytes | None = None


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    kind: MigrationKind
    vault_root: Path
    target_root: Path
    scanned_count: int
    action_count: int
    actions: tuple[MigrationAction, ...]
    issues: tuple[MigrationIssue, ...]
    fingerprint: str

    @property
    def ready(self) -> bool:
        return not self.issues

    def to_redacted_dict(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.code.value] = counts.get(issue.code.value, 0) + 1
        return {
            "action_count": self.action_count,
            "issue_counts": dict(sorted(counts.items())),
            "kind": self.kind.value,
            "ready": self.ready,
            "scanned_count": self.scanned_count,
        }


@dataclass(frozen=True, slots=True)
class BackupEntry:
    relative: PurePosixPath
    existed: bool
    backup_relative: PurePosixPath | None
    digest_sha256: str | None


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    backup_id: str
    backup_root: Path
    target_root: Path
    entries: tuple[BackupEntry, ...]
    manifest_digest: str

    @property
    def file_count(self) -> int:
        return sum(entry.existed for entry in self.entries)

    def to_redacted_dict(self) -> dict[str, object]:
        return {
            "backup_id": self.backup_id,
            "file_count": self.file_count,
            "manifest_digest": self.manifest_digest,
            "tracked_count": len(self.entries),
        }


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
    backup_id: str
    manifest_digest: str
    restored_count: int
    removed_count: int


@dataclass(frozen=True, slots=True)
class StateAdoptionReceiptEvidence:
    schema_version: int
    operation: str
    state: MigrationState
    plan_fingerprint: str
    manifest_digest: str
    source_snapshot_digest: str
    target_before_digest: str
    target_after_digest: str
    write_count: int
    duplicate_idempotency_keys: int
    duplicate_captures: int
    backup: StateAuthorityReceipt | None
    disposable_restore: StateAuthorityReceipt | None

    def to_redacted_dict(self) -> dict[str, object]:
        return {
            "backup_present": self.backup is not None,
            "duplicate_captures": self.duplicate_captures,
            "duplicate_idempotency_keys": self.duplicate_idempotency_keys,
            "manifest_digest": self.manifest_digest,
            "schema_version": self.schema_version,
            "source_snapshot_digest": self.source_snapshot_digest,
            "state": self.state.value,
            "target_after_digest": self.target_after_digest,
            "target_before_digest": self.target_before_digest,
            "write_count": self.write_count,
        }


@dataclass(frozen=True, slots=True)
class StateAuthorityReceiptEvidence:
    version: int
    operation: str
    plan_fingerprint: str
    root_bindings: tuple[str, ...]
    expires_at: datetime
    tracked_count: int
    file_count: int
    restored_count: int
    removed_count: int


@dataclass(frozen=True, slots=True)
class MigrationResult:
    state: MigrationState
    action_count: int
    backup: BackupReceipt | None


def build_plan(
    *,
    kind: MigrationKind,
    vault_root: Path,
    target_root: Path,
    scanned_count: int,
    action_count: int,
    actions: tuple[MigrationAction, ...],
    issues: tuple[MigrationIssue, ...],
) -> MigrationPlan:
    vault_identity = sha256(str(vault_root.resolve(strict=True)).encode()).hexdigest()
    target_identity = sha256(str(target_root.resolve(strict=True)).encode()).hexdigest()
    identity = {
        "action_count": action_count,
        "actions": [
            {
                "kind": action.kind.value,
                "payload_digest": (
                    sha256(action.payload).hexdigest() if action.payload is not None else None
                ),
                "source": str(action.source) if action.source is not None else None,
                "target": str(action.target),
            }
            for action in actions
        ],
        "issues": [
            {"code": issue.code.value, "page_id": issue.page_id} for issue in issues
        ],
        "kind": kind.value,
        "scanned_count": scanned_count,
        "target_root_digest": target_identity,
        "vault_root_digest": vault_identity,
    }
    fingerprint = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return MigrationPlan(
        kind=kind,
        vault_root=vault_root,
        target_root=target_root,
        scanned_count=scanned_count,
        action_count=action_count,
        actions=actions,
        issues=issues,
        fingerprint=fingerprint,
    )
