from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_brain_legacy.cli.migrate import (
    DisposableMigrationTarget,
    apply_migration,
    plan_migration,
    restore_migration,
)
from open_brain_legacy.migrate import (
    IssueCode,
    MigrationBlockedError,
    MigrationState,
    restore_backup,
)
from open_brain_legacy.migrate.state import apply_state_backfill, plan_state_backfill

from ._synthetic import note_fields, write_note


def test_state_backfill_is_redacted_backup_first_restorable_and_idempotent(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    backups = tmp_path / "backups"
    vault.mkdir()
    state_root.mkdir()
    backups.mkdir()
    write_note(
        vault,
        "inbox/existing.md",
        fields=note_fields(page_id="note.existing-001"),
    )
    write_note(
        vault,
        "inbox/new.md",
        fields=note_fields(page_id="note.new-001", source_type="web"),
    )
    state_path = state_root / "capture-state.json"
    original = (
        b'{"operator_label":"synthetic","processed_page_ids":["note.existing-001"],'
        b'"schema_version":1}'
    )
    state_path.write_bytes(original)

    plan = plan_state_backfill(vault_root=vault, state_root=state_root)
    redacted = plan.to_redacted_dict()

    assert redacted == {
        "action_count": 1,
        "issue_counts": {},
        "kind": "state_backfill",
        "ready": True,
        "scanned_count": 2,
    }
    assert str(tmp_path) not in json.dumps(redacted)
    assert "Synthetic migration note" not in json.dumps(redacted)
    assert state_path.read_bytes() == original

    result = apply_state_backfill(plan=plan, backup_root=backups)
    updated = json.loads(state_path.read_text())

    assert result.state is MigrationState.APPLIED
    assert result.action_count == 1
    assert result.backup is not None
    assert result.backup.file_count == 1
    assert updated == {
        "operator_label": "synthetic",
        "processed_page_ids": ["note.existing-001", "note.new-001"],
        "schema_version": 1,
    }

    rerun = plan_state_backfill(vault_root=vault, state_root=state_root)
    noop = apply_state_backfill(plan=rerun, backup_root=backups)

    assert rerun.action_count == 0
    assert noop.state is MigrationState.NOOP
    assert noop.backup is None

    restored = restore_backup(result.backup, target_root=state_root)

    assert restored.restored_count == 1
    assert restored.removed_count == 0
    assert state_path.read_bytes() == original


def test_state_backfill_blocks_duplicate_page_ids_without_backup_or_mutation(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    backups = tmp_path / "backups"
    vault.mkdir()
    state_root.mkdir()
    backups.mkdir()
    write_note(vault, "one.md", fields=note_fields(page_id="note.duplicate-001"))
    write_note(vault, "two.md", fields=note_fields(page_id="note.duplicate-001"))

    plan = plan_state_backfill(vault_root=vault, state_root=state_root)

    assert {issue.code for issue in plan.issues} == {IssueCode.DUPLICATE_PAGE_ID}
    with pytest.raises(MigrationBlockedError, match="migration plan is blocked"):
        apply_state_backfill(plan=plan, backup_root=backups)

    assert not (state_root / "capture-state.json").exists()
    assert not tuple(backups.iterdir())


def test_state_backfill_cli_plans_applies_and_restores_only_redacted_metadata(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    backups = tmp_path / "backups"
    restore_root = tmp_path / "restore"
    vault.mkdir()
    state_root.mkdir()
    backups.mkdir()
    restore_root.mkdir()
    restore_target = DisposableMigrationTarget.create(restore_root)
    write_note(vault, "inbox/note.md", fields=note_fields(page_id="note.cli-state-001"))

    planned = plan_migration(
        kind="state_backfill",
        state_root=state_root,
        vault_root=vault,
    )

    assert planned.plan is not None
    assert planned.envelope["plan"] == {
        "action_count": 1,
        "issue_counts": {},
        "kind": "state_backfill",
        "ready": True,
        "scanned_count": 1,
    }
    assert str(tmp_path) not in planned.to_json()

    applied = apply_migration(plan=planned.plan, backup_root=backups)

    assert applied.result is not None
    assert applied.envelope["state"] == "applied"
    restored = restore_migration(
        backup=applied.result.backup,
        target=restore_target,
    )
    assert restored.envelope["restored_count"] == 0
    assert not tuple(restore_root.iterdir())
