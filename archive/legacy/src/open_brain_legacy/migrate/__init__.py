"""Backup-first, root-confined migrations for synthetic or copied state."""

from ._models import (
    BackupReceipt,
    IssueCode,
    MigrationBlockedError,
    MigrationError,
    MigrationPlan,
    MigrationResult,
    MigrationState,
    RestoreReceipt,
    StaleMigrationPlanError,
)
from ._support import restore_backup, restore_backup_copy

__all__ = [
    "BackupReceipt",
    "IssueCode",
    "MigrationBlockedError",
    "MigrationError",
    "MigrationPlan",
    "MigrationResult",
    "MigrationState",
    "RestoreReceipt",
    "StaleMigrationPlanError",
    "restore_backup",
    "restore_backup_copy",
]
