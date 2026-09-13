"""Thin, redacted CLI adapters for backup-first migration services."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
from open_brain_legacy.integrations.obsidian import ObsidianTaxonomy
from open_brain_legacy.migrate import (
    BackupReceipt,
    MigrationPlan,
    MigrationResult,
    RestoreReceipt,
    restore_backup_copy,
)
from open_brain_legacy.migrate._models import MigrationKind
from open_brain_legacy.migrate.content import (
    apply_content_layout,
    apply_processed_at_backfill,
    plan_content_layout,
    plan_processed_at_backfill,
)
from open_brain_legacy.migrate.state import apply_state_backfill, plan_state_backfill


@dataclass(frozen=True, slots=True)
class DisposableMigrationTarget:
    """Capability minted only while a synthetic restore root is empty."""

    path: Path
    device: int
    inode: int

    @classmethod
    def create(cls, path: Path) -> DisposableMigrationTarget:
        try:
            canonical, device, inode = _directory_identity(path)
        except (OSError, ValueError):
            raise ValueError("restore target must be an empty disposable root") from None
        if any(canonical.iterdir()):
            raise ValueError("restore target must be an empty disposable root")
        return cls(canonical, device, inode)

    def revalidate(self) -> Path:
        canonical, device, inode = _directory_identity(self.path)
        if (
            canonical != self.path
            or device != self.device
            or inode != self.inode
            or any(canonical.iterdir())
        ):
            raise ValueError("restore target is no longer disposable")
        return canonical


@dataclass(frozen=True, slots=True)
class MigrationCliResult:
    """A result envelope that retains no paths, note text, or exception details."""

    exit_code: ExitCode
    envelope: dict[str, object]
    plan: MigrationPlan | None = None
    result: MigrationResult | None = None
    restored: RestoreReceipt | None = None

    def to_json(self) -> str:
        """Serialize the public metadata envelope with stable key ordering."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


def plan_migration(
    *,
    kind: MigrationKind | str,
    vault_root: Path,
    state_root: Path | None = None,
    taxonomy: ObsidianTaxonomy | None = None,
) -> MigrationCliResult:
    """Create a non-mutating migration plan through the selected typed service."""
    try:
        selected_kind = MigrationKind(kind)
        plan = _plan(
            kind=selected_kind,
            vault_root=vault_root,
            state_root=state_root,
            taxonomy=taxonomy,
        )
    except Exception:
        return _failed("migration_plan_failed")
    return MigrationCliResult(
        ExitCode.SUCCESS if plan.ready else ExitCode.FAILURE,
        {
            "command": "migration",
            "plan": plan.to_redacted_dict(),
            "status": "planned" if plan.ready else "blocked",
        },
        plan=plan,
    )


def apply_migration(
    *,
    plan: MigrationPlan,
    backup_root: Path,
    taxonomy: ObsidianTaxonomy | None = None,
) -> MigrationCliResult:
    """Apply an unchanged ready plan through its backup-first typed service."""
    try:
        result = _apply(plan=plan, backup_root=backup_root, taxonomy=taxonomy)
    except Exception:
        return _failed("migration_apply_failed")
    envelope: dict[str, object] = {
        "action_count": result.action_count,
        "command": "migration",
        "state": result.state.value,
        "status": "applied",
    }
    if result.backup is not None:
        envelope["backup"] = result.backup.to_redacted_dict()
    return MigrationCliResult(ExitCode.SUCCESS, envelope, result=result)


def restore_migration(
    *,
    backup: BackupReceipt | None,
    target: DisposableMigrationTarget,
) -> MigrationCliResult:
    """Restore only through a capability minted from an empty synthetic root."""
    if not isinstance(target, DisposableMigrationTarget):
        return _failed("migration_restore_requires_disposable_target")
    try:
        if not isinstance(backup, BackupReceipt):
            raise ValueError("invalid backup")
        restored = restore_backup_copy(backup, target_root=target.revalidate())
    except Exception:
        return _failed("migration_restore_failed")
    return MigrationCliResult(
        ExitCode.SUCCESS,
        {
            "backup_id": restored.backup_id,
            "command": "migration",
            "manifest_digest": restored.manifest_digest,
            "removed_count": restored.removed_count,
            "restored_count": restored.restored_count,
            "status": "restored",
        },
        restored=restored,
    )


def _plan(
    *,
    kind: MigrationKind,
    vault_root: Path,
    state_root: Path | None,
    taxonomy: ObsidianTaxonomy | None,
) -> MigrationPlan:
    if kind is MigrationKind.STATE_BACKFILL:
        if not isinstance(state_root, Path):
            raise ValueError("state root required")
        return plan_state_backfill(vault_root=vault_root, state_root=state_root)
    if kind is MigrationKind.PROCESSED_AT_BACKFILL:
        return plan_processed_at_backfill(vault_root=vault_root)
    if kind is MigrationKind.CONTENT_LAYOUT and isinstance(taxonomy, ObsidianTaxonomy):
        return plan_content_layout(vault_root=vault_root, taxonomy=taxonomy)
    raise ValueError("invalid migration request")


def _apply(
    *,
    plan: MigrationPlan,
    backup_root: Path,
    taxonomy: ObsidianTaxonomy | None,
) -> MigrationResult:
    if not isinstance(plan, MigrationPlan):
        raise ValueError("invalid plan")
    if plan.kind is MigrationKind.STATE_BACKFILL:
        return apply_state_backfill(plan=plan, backup_root=backup_root)
    if plan.kind is MigrationKind.PROCESSED_AT_BACKFILL:
        return apply_processed_at_backfill(plan=plan, backup_root=backup_root)
    if plan.kind is MigrationKind.CONTENT_LAYOUT and isinstance(taxonomy, ObsidianTaxonomy):
        return apply_content_layout(plan=plan, taxonomy=taxonomy, backup_root=backup_root)
    raise ValueError("invalid migration request")


def _failed(code: str) -> MigrationCliResult:
    return MigrationCliResult(
        ExitCode.FAILURE,
        {"command": "migration", "error": redacted_error(code), "status": "failed"},
    )


def _directory_identity(path: Path) -> tuple[Path, int, int]:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("invalid directory")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("symlinked directory authority")
    canonical = path.resolve(strict=True)
    metadata = canonical.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("invalid directory")
    return canonical, metadata.st_dev, metadata.st_ino
