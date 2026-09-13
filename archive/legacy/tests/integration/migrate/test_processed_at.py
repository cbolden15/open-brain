from __future__ import annotations

from pathlib import Path

import pytest
from open_brain_engine.storage.markdown import parse_markdown

from open_brain_legacy.cli.migrate import apply_migration, plan_migration
from open_brain_legacy.migrate import (
    IssueCode,
    MigrationBlockedError,
    MigrationState,
    restore_backup,
)
from open_brain_legacy.migrate.content import (
    apply_processed_at_backfill,
    plan_processed_at_backfill,
)

from ._synthetic import note_fields, write_note


def test_processed_at_backfill_preserves_metadata_and_body_and_has_noop_rerun(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    backups = tmp_path / "backups"
    vault.mkdir()
    backups.mkdir()
    note = write_note(
        vault,
        "inbox/note.md",
        fields=note_fields(page_id="note.timestamp-001", processed_at=None),
    )
    original = note.read_bytes()

    plan = plan_processed_at_backfill(vault_root=vault)

    assert plan.to_redacted_dict() == {
        "action_count": 1,
        "issue_counts": {},
        "kind": "processed_at_backfill",
        "ready": True,
        "scanned_count": 1,
    }
    assert note.read_bytes() == original

    result = apply_processed_at_backfill(plan=plan, backup_root=backups)
    parsed = parse_markdown(note.read_bytes())

    assert result.state is MigrationState.APPLIED
    assert result.backup is not None
    assert parsed.fields["processed_at"] == "2026-01-02T03:04:05Z"
    assert parsed.fields["custom"] == {
        "labels": ["synthetic", "public"],
        "reviewed": True,
    }
    assert parsed.body == "# Synthetic migration note\n\nSynthetic body."

    rerun = plan_processed_at_backfill(vault_root=vault)
    noop = apply_processed_at_backfill(plan=rerun, backup_root=backups)

    assert noop.state is MigrationState.NOOP
    assert noop.backup is None

    restored = restore_backup(result.backup, target_root=vault)
    assert restored.restored_count == 1
    assert note.read_bytes() == original


def test_processed_at_backfill_fails_closed_on_malformed_frontmatter(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    backups = tmp_path / "backups"
    vault.mkdir()
    backups.mkdir()
    malformed = vault / "malformed.md"
    malformed.write_text("---\npage_id: not-json\n---\n\nSynthetic body")
    original = malformed.read_bytes()

    plan = plan_processed_at_backfill(vault_root=vault)

    assert {issue.code for issue in plan.issues} == {IssueCode.MALFORMED_MARKDOWN}
    with pytest.raises(MigrationBlockedError, match="migration plan is blocked"):
        apply_processed_at_backfill(plan=plan, backup_root=backups)

    assert malformed.read_bytes() == original
    assert not tuple(backups.iterdir())


def test_processed_at_cli_rejects_a_stale_plan_without_a_backup(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    backups = tmp_path / "backups"
    vault.mkdir()
    backups.mkdir()
    note = write_note(
        vault,
        "inbox/note.md",
        fields=note_fields(page_id="note.cli-stale-001", processed_at=None),
    )
    planned = plan_migration(kind="processed_at_backfill", vault_root=vault)

    assert planned.plan is not None
    note.write_text("---\npage_id: broken\n---\n")
    applied = apply_migration(plan=planned.plan, backup_root=backups)

    assert applied.result is None
    assert applied.envelope["status"] == "failed"
    error = applied.envelope["error"]
    assert isinstance(error, dict)
    assert error["redacted"] is True
    assert not tuple(backups.iterdir())
