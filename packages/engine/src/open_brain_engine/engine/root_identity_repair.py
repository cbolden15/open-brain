"""Re-record a managed root's device number after the OS renumbered its volume.

Managed workspaces and Markdown import roots are recorded by ``(st_dev, st_ino)``
and every later operation requires that exact pair. macOS can assign a volume a
different ``st_dev`` across reboots while the folder itself is unchanged, which
made every such root fail closed forever. Before migration and engine admission
this repair rewrites only the recorded device number, and only when the recorded
path still opens, without following links, to a directory with the recorded
inode. A replaced folder has a new inode and still fails the strict checks.

Not repaired, and so still failing closed as before: a root on a different volume
from the Brain or at a mount point, a workspace with an in-flight managed or facet
write, and any root while a source-migration journal is in flight.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from open_brain_engine.storage.filesystem import RootConfinementError, capture_root_identity
from open_brain_engine.storage.sqlite import connect_database

from .contracts import LocalEngineContext
from .local_schema import PHASE1_STATE_DATABASE
from .source_migration import migration_pending

# (table, key column, path column). Both tables store device and inode as TEXT.
_RECORDED_ROOTS = (
    ("managed_workspaces", "workspace_id", "root_path"),
    ("markdown_import_roots", "root_id", "canonical_path"),
)


def repair_recorded_root_devices(profile: LocalEngineContext) -> int:
    """Return how many recorded roots had their device number re-recorded."""
    if not (profile.root / PHASE1_STATE_DATABASE).is_file() or migration_pending(profile):
        # A resumed source migration compares its saved inventory of these rows.
        return 0
    connection = connect_database(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    connection.isolation_level = None
    try:
        repairs = _planned_repairs(connection, brain_device=profile.root_identity[0])
        if not repairs:
            return 0
        repaired = 0
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table, key_column, key, device, inode, observed_device in repairs:
                try:
                    cursor = connection.execute(
                        f"UPDATE {table} SET device=? "  # noqa: S608 - fixed identifiers
                        f"WHERE {key_column}=? AND device=? AND inode=?",
                        (str(observed_device), key, device, inode),
                    )
                except sqlite3.IntegrityError:
                    # Another row already claims this identity; leave both to fail closed.
                    continue
                repaired += cursor.rowcount
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return repaired
    finally:
        connection.close()


def _planned_repairs(
    connection: sqlite3.Connection, *, brain_device: int
) -> list[tuple[str, str, str, str, str, int]]:
    existing = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    repairs = []
    for table, key_column, path_column in _RECORDED_ROOTS:
        if table not in existing:
            continue
        rows = connection.execute(
            f"SELECT {key_column}, {path_column}, device, inode FROM {table} "  # noqa: S608
            f"WHERE {path_column} IS NOT NULL AND device IS NOT NULL AND inode IS NOT NULL"
        ).fetchall()
        for key, path, device, inode in rows:
            root = Path(path)
            if not root.is_absolute():
                continue
            try:
                observed_device, observed_inode = capture_root_identity(root)
                recorded_device, recorded_inode = int(device), int(inode)
                parent_device = root.parent.stat().st_dev
            except RootConfinementError, OSError, ValueError:
                continue
            if observed_inode != recorded_inode or observed_device == recorded_device:
                continue
            # Inode numbers repeat across volumes (every APFS volume root is inode 2), so
            # the inode proves the same folder only on the volume that holds the Brain and
            # below its mount point. A root on another volume keeps failing closed.
            if observed_device != brain_device or parent_device != observed_device:
                continue
            if table == "managed_workspaces" and _has_pending_write(connection, existing, key):
                # In-flight writes bind the old device in their replay descriptor; leave
                # the workspace as it was rather than invalidate their recovery.
                continue
            repairs.append((table, key_column, key, device, inode, observed_device))
    return repairs


def _has_pending_write(
    connection: sqlite3.Connection, existing: set[str], workspace_id: str
) -> bool:
    pending = (
        ("managed_operations", "status IN ('prepared', 'writing', 'promoted')"),
        ("facet_projection_writes", "status = 'prepared'"),
    )
    return any(
        connection.execute(
            f"SELECT 1 FROM {table} WHERE workspace_id=? AND {condition} LIMIT 1",  # noqa: S608
            (workspace_id,),
        ).fetchone()
        is not None
        for table, condition in pending
        if table in existing
    )
