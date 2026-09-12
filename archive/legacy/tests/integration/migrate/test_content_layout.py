from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.core.models import SourceType
from open_brain_engine.storage.filesystem import RootConfinementError

from open_brain_legacy.cli.migrate import (
    DisposableMigrationTarget,
    apply_migration,
    plan_migration,
    restore_migration,
)
from open_brain_legacy.integrations.obsidian import ObsidianTaxonomy
from open_brain_legacy.migrate import (
    IssueCode,
    MigrationBlockedError,
    MigrationError,
    MigrationState,
    StaleMigrationPlanError,
    restore_backup,
)
from open_brain_legacy.migrate import content as content_module
from open_brain_legacy.migrate._support import move_file as actual_move
from open_brain_legacy.migrate.content import apply_content_layout, plan_content_layout

from ._synthetic import note_fields, write_note


def _taxonomy() -> ObsidianTaxonomy:
    return ObsidianTaxonomy.create(
        reviewed="reviewed",
        destinations={
            SourceType.YOUTUBE: "reference/videos",
            SourceType.SOCIAL: "reference/social",
            SourceType.WEB: "reference/articles",
            SourceType.TEXT: "reference/notes",
        },
    )


def test_layout_migration_is_redacted_backup_first_restorable_and_idempotent(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    backups = tmp_path / "backups"
    vault.mkdir()
    backups.mkdir()
    source = write_note(
        vault,
        "inbox/video.md",
        fields=note_fields(page_id="note.layout-001"),
    )
    original = source.read_bytes()
    target = vault / "reference/videos/note.layout-001.md"

    plan = plan_content_layout(vault_root=vault, taxonomy=_taxonomy())
    redacted = plan.to_redacted_dict()

    assert redacted == {
        "action_count": 1,
        "issue_counts": {},
        "kind": "content_layout",
        "ready": True,
        "scanned_count": 1,
    }
    assert str(tmp_path) not in json.dumps(redacted)
    assert "inbox" not in json.dumps(redacted)
    assert source.read_bytes() == original
    assert not target.exists()

    result = apply_content_layout(plan=plan, taxonomy=_taxonomy(), backup_root=backups)

    assert result.state is MigrationState.APPLIED
    assert result.backup is not None
    assert not source.exists()
    assert target.read_bytes() == original

    rerun = plan_content_layout(vault_root=vault, taxonomy=_taxonomy())
    noop = apply_content_layout(plan=rerun, taxonomy=_taxonomy(), backup_root=backups)

    assert noop.state is MigrationState.NOOP
    assert noop.backup is None

    restored = restore_backup(result.backup, target_root=vault)

    assert restored.restored_count == 1
    assert restored.removed_count == 1
    assert source.read_bytes() == original
    assert not target.exists()


def test_layout_blocks_duplicates_and_stranded_notes_without_mutation(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    backups = tmp_path / "backups"
    vault.mkdir()
    backups.mkdir()
    first = write_note(vault, "one.md", fields=note_fields(page_id="note.duplicate-001"))
    second = write_note(vault, "two.md", fields=note_fields(page_id="note.duplicate-001"))
    stranded = write_note(
        vault,
        "stranded.md",
        fields=note_fields(page_id="note.stranded-001", source_type="unknown"),
    )
    originals = {path: path.read_bytes() for path in (first, second, stranded)}

    plan = plan_content_layout(vault_root=vault, taxonomy=_taxonomy())

    assert {issue.code for issue in plan.issues} == {
        IssueCode.DUPLICATE_PAGE_ID,
        IssueCode.STRANDED_NOTE,
    }
    with pytest.raises(MigrationBlockedError, match="migration plan is blocked"):
        apply_content_layout(plan=plan, taxonomy=_taxonomy(), backup_root=backups)

    assert all(path.read_bytes() == payload for path, payload in originals.items())
    assert not tuple(backups.iterdir())


def test_layout_refuses_symlinks_and_unsafe_taxonomy(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    write_note(outside, "outside.md", fields=note_fields(page_id="note.outside-001"))
    (vault / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RootConfinementError, match="symlink"):
        plan_content_layout(vault_root=vault, taxonomy=_taxonomy())

    with pytest.raises(ValueError, match="unsafe taxonomy path"):
        ObsidianTaxonomy.create(
            reviewed="reviewed",
            destinations={
                SourceType.YOUTUBE: "../outside",
                SourceType.SOCIAL: "social",
                SourceType.WEB: "web",
                SourceType.TEXT: "text",
            },
        )


def test_layout_never_mutates_when_backup_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    backups = tmp_path / "backups"
    vault.mkdir()
    backups.mkdir()
    source = write_note(
        vault,
        "inbox/video.md",
        fields=note_fields(page_id="note.backup-001"),
    )
    original = source.read_bytes()
    target = vault / "reference/videos/note.backup-001.md"
    plan = plan_content_layout(vault_root=vault, taxonomy=_taxonomy())

    def fail_backup(*_: object, **__: object) -> None:
        raise MigrationError("synthetic backup failure")

    monkeypatch.setattr(content_module, "create_backup", fail_backup)

    with pytest.raises(MigrationError, match="synthetic backup failure"):
        apply_content_layout(plan=plan, taxonomy=_taxonomy(), backup_root=backups)

    assert source.read_bytes() == original
    assert not target.exists()


def test_layout_rolls_back_after_interruption_between_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SyntheticInterruption(BaseException):
        pass

    vault = tmp_path / "vault"
    backups = tmp_path / "backups"
    vault.mkdir()
    backups.mkdir()
    first = write_note(
        vault,
        "inbox/first.md",
        fields=note_fields(page_id="note.interrupt-001"),
    )
    second = write_note(
        vault,
        "inbox/second.md",
        fields=note_fields(page_id="note.interrupt-002"),
    )
    originals = {first: first.read_bytes(), second: second.read_bytes()}
    plan = plan_content_layout(vault_root=vault, taxonomy=_taxonomy())
    move_count = 0

    def interrupt_after_first_move(
        root: Path,
        source: PurePosixPath,
        target: PurePosixPath,
    ) -> None:
        nonlocal move_count
        actual_move(root, source, target)
        move_count += 1
        if move_count == 1:
            raise SyntheticInterruption

    monkeypatch.setattr(content_module, "move_file", interrupt_after_first_move)

    with pytest.raises(SyntheticInterruption):
        apply_content_layout(plan=plan, taxonomy=_taxonomy(), backup_root=backups)

    assert {path: path.read_bytes() for path in originals} == originals
    assert not (vault / "reference/videos/note.interrupt-001.md").exists()
    assert not (vault / "reference/videos/note.interrupt-002.md").exists()


def test_layout_rejects_plan_target_root_substitution(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    other_root = tmp_path / "other"
    backups = tmp_path / "backups"
    vault.mkdir()
    other_root.mkdir()
    backups.mkdir()
    source = write_note(
        vault,
        "inbox/note.md",
        fields=note_fields(page_id="note.root-bound-001"),
    )
    original = source.read_bytes()
    plan = plan_content_layout(vault_root=vault, taxonomy=_taxonomy())
    substituted = replace(plan, target_root=other_root)

    with pytest.raises(StaleMigrationPlanError, match="inputs changed"):
        apply_content_layout(
            plan=substituted,
            taxonomy=_taxonomy(),
            backup_root=backups,
        )

    assert source.read_bytes() == original
    assert not tuple(other_root.iterdir())
    assert not tuple(backups.iterdir())


def test_disposable_restore_target_rejects_a_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    restore = real / "restore"
    alias = tmp_path / "alias"
    real.mkdir()
    restore.mkdir()
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="empty disposable root"):
        DisposableMigrationTarget.create(alias / "restore")


def test_layout_cli_restore_requires_a_disposable_target(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    backups = tmp_path / "backups"
    restore_root = tmp_path / "restore"
    vault.mkdir()
    backups.mkdir()
    restore_root.mkdir()
    restore_target = DisposableMigrationTarget.create(restore_root)
    source = write_note(vault, "inbox/note.md", fields=note_fields(page_id="note.cli-layout-001"))
    planned = plan_migration(
        kind="content_layout",
        taxonomy=_taxonomy(),
        vault_root=vault,
    )

    assert planned.plan is not None
    applied = apply_migration(plan=planned.plan, backup_root=backups, taxonomy=_taxonomy())
    assert applied.result is not None
    assert applied.result.backup is not None

    refused = restore_migration(
        backup=applied.result.backup,
        target=vault,  # type: ignore[arg-type]
    )
    assert refused.envelope["status"] == "failed"
    assert not source.exists()

    restored = restore_migration(
        backup=applied.result.backup,
        target=restore_target,
    )
    assert restored.envelope["restored_count"] == 1
    assert not source.exists()
    assert (restore_root / "inbox/note.md").exists()
