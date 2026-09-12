"""Backup-first Markdown timestamp and layout migrations."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from open_brain_engine.core.models import SourceType
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown, render_markdown

from open_brain_legacy.integrations.obsidian import ObsidianTaxonomy

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
    StaleMigrationPlanError,
    build_plan,
)
from ._support import (
    create_backup,
    move_file,
    read_file,
    replace_file,
    restore_backup,
    walk_markdown,
)

_PAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def plan_processed_at_backfill(*, vault_root: Path) -> MigrationPlan:
    note_paths = walk_markdown(vault_root)
    actions: list[MigrationAction] = []
    issues: list[MigrationIssue] = []
    page_ids: list[str] = []
    for relative in note_paths:
        payload = read_file(vault_root, relative)
        try:
            document = parse_markdown(payload if payload is not None else b"")
            page_id = _page_id(document.fields.get("page_id"))
            page_ids.append(page_id)
            captured_at = _timestamp(document.fields.get("captured_at"))
            processed_at = document.fields.get("processed_at")
            if processed_at is not None:
                _timestamp(processed_at)
                continue
            fields = dict(document.fields)
            fields["processed_at"] = captured_at
            rendered = render_markdown(fields=fields, body=document.body).encode("utf-8")
            actions.append(MigrationAction(ActionKind.WRITE, relative, payload=rendered))
        except (MarkdownFormatError, ValueError):
            issues.append(MigrationIssue(IssueCode.MALFORMED_MARKDOWN))
    issues.extend(_duplicate_issues(page_ids))
    return build_plan(
        kind=MigrationKind.PROCESSED_AT_BACKFILL,
        vault_root=vault_root,
        target_root=vault_root,
        scanned_count=len(note_paths),
        action_count=len(actions),
        actions=tuple(actions),
        issues=tuple(issues),
    )


def apply_processed_at_backfill(*, plan: MigrationPlan, backup_root: Path) -> MigrationResult:
    if plan.kind is not MigrationKind.PROCESSED_AT_BACKFILL:
        raise MigrationBlockedError("invalid migration plan")
    if plan.issues:
        raise MigrationBlockedError("migration plan is blocked")
    current = plan_processed_at_backfill(vault_root=plan.vault_root)
    if (
        current.fingerprint != plan.fingerprint
        or current.vault_root != plan.vault_root
        or current.target_root != plan.target_root
        or current.actions != plan.actions
    ):
        raise StaleMigrationPlanError("migration inputs changed after dry-run")
    if not plan.actions:
        return MigrationResult(MigrationState.NOOP, 0, None)
    backup = create_backup(
        target_root=plan.target_root,
        backup_root=backup_root,
        relatives=tuple(action.target for action in plan.actions),
    )
    try:
        for action in plan.actions:
            if action.payload is None:
                raise MigrationBlockedError("invalid migration plan")
            replace_file(plan.target_root, action.target, action.payload, require_existing=True)
    except BaseException:
        _rollback_or_raise(backup, plan.target_root)
        raise
    return MigrationResult(MigrationState.APPLIED, plan.action_count, backup)


def plan_content_layout(
    *,
    vault_root: Path,
    taxonomy: ObsidianTaxonomy,
) -> MigrationPlan:
    note_paths = walk_markdown(vault_root)
    actions: list[MigrationAction] = []
    issues: list[MigrationIssue] = []
    page_ids: list[str] = []
    parsed: list[tuple[PurePosixPath, str, SourceType]] = []
    for relative in note_paths:
        payload = read_file(vault_root, relative)
        try:
            document = parse_markdown(payload if payload is not None else b"")
            page_id = _page_id(document.fields.get("page_id"))
            page_ids.append(page_id)
            raw_source_type = document.fields.get("source_type")
            if not isinstance(raw_source_type, str):
                issues.append(MigrationIssue(IssueCode.STRANDED_NOTE, page_id))
                continue
            try:
                source_type = SourceType(raw_source_type)
            except ValueError:
                issues.append(MigrationIssue(IssueCode.STRANDED_NOTE, page_id))
                continue
            parsed.append((relative, page_id, source_type))
        except (MarkdownFormatError, ValueError):
            issues.append(MigrationIssue(IssueCode.MALFORMED_MARKDOWN))
    issues.extend(_duplicate_issues(page_ids))
    occupied = set(note_paths)
    for relative, page_id, source_type in parsed:
        target = taxonomy.relative_path(source_type=source_type, page_id=page_id)
        if relative == target:
            continue
        if target in occupied:
            issues.append(MigrationIssue(IssueCode.DUPLICATE_PAGE_ID, page_id))
            continue
        actions.append(MigrationAction(ActionKind.MOVE, target, source=relative))
    return build_plan(
        kind=MigrationKind.CONTENT_LAYOUT,
        vault_root=vault_root,
        target_root=vault_root,
        scanned_count=len(note_paths),
        action_count=len(actions),
        actions=tuple(actions),
        issues=tuple(issues),
    )


def apply_content_layout(
    *,
    plan: MigrationPlan,
    taxonomy: ObsidianTaxonomy,
    backup_root: Path,
) -> MigrationResult:
    if plan.kind is not MigrationKind.CONTENT_LAYOUT:
        raise MigrationBlockedError("invalid migration plan")
    if plan.issues:
        raise MigrationBlockedError("migration plan is blocked")
    current = plan_content_layout(vault_root=plan.vault_root, taxonomy=taxonomy)
    if (
        current.fingerprint != plan.fingerprint
        or current.vault_root != plan.vault_root
        or current.target_root != plan.target_root
        or current.actions != plan.actions
    ):
        raise StaleMigrationPlanError("migration inputs changed after dry-run")
    if not plan.actions:
        return MigrationResult(MigrationState.NOOP, 0, None)
    relatives: list[PurePosixPath] = []
    for action in plan.actions:
        if action.source is None:
            raise MigrationBlockedError("invalid migration plan")
        relatives.extend((action.source, action.target))
    backup = create_backup(
        target_root=plan.target_root,
        backup_root=backup_root,
        relatives=tuple(relatives),
    )
    try:
        for action in plan.actions:
            if action.source is None:
                raise MigrationBlockedError("invalid migration plan")
            move_file(plan.target_root, action.source, action.target)
    except BaseException:
        _rollback_or_raise(backup, plan.target_root)
        raise
    return MigrationResult(MigrationState.APPLIED, plan.action_count, backup)


def _rollback_or_raise(backup: BackupReceipt, target_root: Path) -> None:
    try:
        restore_backup(backup, target_root=target_root)
    except Exception:
        raise MigrationError("migration rollback failed") from None


def _page_id(value: object) -> str:
    if not isinstance(value, str) or not _PAGE_ID.fullmatch(value):
        raise ValueError("invalid page ID")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("invalid timestamp")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _duplicate_issues(page_ids: list[str]) -> list[MigrationIssue]:
    counts = Counter(page_ids)
    return [
        MigrationIssue(IssueCode.DUPLICATE_PAGE_ID, page_id)
        for page_id, count in sorted(counts.items())
        if count > 1
    ]
