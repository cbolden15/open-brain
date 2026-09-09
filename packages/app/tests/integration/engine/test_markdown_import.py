from __future__ import annotations

import importlib
import json
import os
import socket
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest
from open_brain_engine.core.models import Authority, PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    CaptureSubmission,
    CaptureWhyOrigin,
    ContentOrigin,
    DecisionOutcome,
    FilePayload,
    MarkdownImportCancelled,
    MarkdownImportFailure,
    MarkdownImportInterrupted,
    PrivacyDecision,
    PrivacyReason,
    ProposalDraft,
    Provenance,
    TextPayload,
)
from open_brain_engine.engine.markdown_import import (
    capture_submission_is_reserved,
    extract_markdown_title,
)
from open_brain_engine.engine.markdown_import_fs import (
    MAX_AGGREGATE_BYTES,
    MAX_FILE_BYTES,
    MAX_SELECTED_FILES,
    MAX_VISITED_ENTRIES,
    ScanLimits,
)
from open_brain_engine.storage.markdown import parse_markdown

from open_brain.profile import compile_single_user_local


def _engine(
    root: Path,
    *,
    limits: ScanLimits | None = None,
    starter_spaces: tuple[str, ...] = (),
) -> BrainEngine:
    engine = BrainEngine.open(compile_single_user_local(root, starter_spaces=starter_spaces))
    if limits is not None:
        engine.markdown_import._scan_limits = limits
    return engine


def _count(root: Path, table: str) -> int:
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        return cast(int, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _database(root: Path) -> Path:
    return root / ".open-brain/state/phase1.sqlite3"


def test_markdown_import_is_idempotent_updates_reactivates_and_exports_history(
    tmp_path: Path,
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    nested = vault / "folder"
    ignored = vault / ".obsidian"
    nested.mkdir(parents=True)
    ignored.mkdir()
    root_bytes = b"# Root note\r\nportable-root-token\r\n"
    first_nested = (
        b"\xef\xbb\xbf---\n"
        b"title: 'Nested ''display'' title'\n"
        b"---\n"
        b"portable-nested-token and [[inert-link]]\n"
    )
    second_nested = b"# Changed nested title\nportable-nested-token changed\n"
    (vault / "root.md").write_bytes(root_bytes)
    (nested / "note.md").write_bytes(first_nested)
    (vault / "empty.md").write_bytes(b"")
    (ignored / "ignored.md").write_text("must-not-be-imported", encoding="utf-8")
    (vault / "asset.txt").write_text("not Markdown", encoding="utf-8")

    engine = _engine(brain)
    preflights: list[object] = []

    def confirm(value: object) -> bool:
        preflights.append(value)
        return True

    first = engine.markdown_import.import_directory(
        str(vault),
        confirm=confirm,
    )
    original_nested_id = engine.retrieval.search("portable-nested-token")[0].capture_id

    assert len(preflights) == 1
    assert first.imported == 3
    assert first.selected == 3
    assert first.skipped == 2
    assert engine.retrieval.search("portable-nested-token")[0].title == "Nested 'display' title"
    assert engine.retrieval.search("portable-nested-token")[0].trust == "unverified"
    assert engine.retrieval.search("portable-nested-token")[0].provenance.source_origin == "unknown"
    assert engine.retrieval.search("must-not-be-imported") == ()
    assert _count(brain, "captures") == 3
    assert _count(brain, "markdown_import_revisions") == 3

    unchanged = engine.markdown_import.import_directory(str(vault))
    assert unchanged.unchanged == 3
    assert unchanged.imported == unchanged.updated == 0
    assert _count(brain, "captures") == 3

    (nested / "note.md").write_bytes(second_nested)
    changed = engine.markdown_import.import_directory(str(vault))
    assert (changed.updated, changed.unchanged) == (1, 2)
    changed_nested_id = engine.retrieval.search("portable-nested-token")[0].capture_id
    assert changed_nested_id != original_nested_id
    assert _count(brain, "captures") == 4

    (nested / "note.md").write_bytes(first_nested)
    restored_revision = engine.markdown_import.import_directory(str(vault))
    assert (restored_revision.updated, restored_revision.unchanged) == (1, 2)
    assert engine.retrieval.search("portable-nested-token")[0].capture_id == original_nested_id
    assert _count(brain, "captures") == 4

    (vault / "root.md").unlink()
    missing = engine.markdown_import.import_directory(str(vault))
    assert missing.missing == 1
    assert engine.retrieval.search("portable-root-token") == ()

    export = tmp_path / "portable"
    engine.portability.export(
        export,
        export_id="export_00000000-0000-4000-8000-000000000404",
    )
    for payload in (root_bytes, first_nested, second_nested, b""):
        digest = sha256(payload).hexdigest()
        assert (export / f"sources/blobs/sha256/{digest[:2]}/{digest}").read_bytes() == payload
    captures = [
        cast(dict[str, object], json.loads(path.read_bytes()))
        for path in (export / "sources/captures").rglob("*.json")
    ]
    imported = [
        item
        for item in captures
        if cast(dict[str, object], item["source"])["origin"] == "third_party"
    ]
    assert len(imported) == 4
    assert all(
        cast(dict[str, object], item["provenance"])["content_origin"] == "unknown"
        for item in imported
    )
    assert all(
        cast(dict[str, object], item["provenance"])["owner_context"] == "automation_absent"
        for item in imported
    )
    assert all(cast(dict[str, object], item["trust"])["label"] == "unverified" for item in imported)


def test_pending_capture_stays_unsearchable_until_retry_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "pending.md").write_text("# Pending\npending-reservation-token", encoding="utf-8")
    engine = _engine(brain)

    def fail_activation(**_kwargs: object) -> None:
        raise RuntimeError("synthetic activation stop")

    monkeypatch.setattr(engine.markdown_import, "_activate_revision", fail_activation)
    with pytest.raises(RuntimeError, match="activation stop"):
        engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)

    assert _count(brain, "captures") == 1
    assert engine.retrieval.search("pending-reservation-token") == ()
    reopened = _engine(brain)
    assert reopened.retrieval.search("pending-reservation-token") == ()

    resumed = reopened.markdown_import.import_directory(str(vault))
    assert resumed.imported == 1
    assert _count(brain, "captures") == 1
    assert reopened.retrieval.search("pending-reservation-token")[0].trust == "unverified"


def test_preflight_refusal_and_cancellation_write_no_import_state(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "one.md").write_text("one", encoding="utf-8")
    (vault / "two.md").write_text("two", encoding="utf-8")
    limits = ScanLimits(visited_entries=10, selected_files=1, aggregate_bytes=10, file_bytes=10)
    engine = _engine(brain, limits=limits)

    with pytest.raises(MarkdownImportFailure) as exceeded:
        engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    assert exceeded.value.code == "large_vault_confirmation_required"
    assert _count(brain, "markdown_import_roots") == 0
    assert _count(brain, "captures") == 0

    imported = engine.markdown_import.import_directory(
        str(vault), allow_large_vault=True, confirm=lambda _value: True
    )
    assert imported.imported == 2

    second = tmp_path / "second"
    second.mkdir()
    (second / "cancel.md").write_text("cancel", encoding="utf-8")
    with pytest.raises(MarkdownImportCancelled):
        engine.markdown_import.import_directory(str(second), confirm=lambda _value: False)
    assert _count(brain, "markdown_import_roots") == 1


def test_special_entries_are_skipped_without_following_or_blocking(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    with TemporaryDirectory(prefix="obw4-", dir="/tmp") as raw_vault:
        vault = Path(raw_vault)
        target = vault / "target.md"
        target.write_text("special-entry-token", encoding="utf-8")
        os.link(target, vault / "hardlink.md")
        (vault / "link.md").symlink_to(tmp_path / "outside.md")
        os.mkfifo(vault / "pipe.md")
        socket_path = vault / "socket.md"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(socket_path))
        listener.close()

        summary = _engine(brain).markdown_import.import_directory(
            str(vault), confirm=lambda _value: True
        )

    assert summary.selected == 0
    assert summary.skipped == 5
    assert {entry.reason for entry in summary.entries} == {"hardlink", "symlink", "special_file"}
    assert _count(brain, "captures") == 0


def test_root_overlap_and_reserved_delivery_are_refused_without_capture(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    engine = _engine(brain)
    nested = brain / "owner-notes"
    nested.mkdir()
    (nested / "note.md").write_text("overlap", encoding="utf-8")

    with pytest.raises(MarkdownImportFailure) as overlap:
        engine.markdown_import.import_directory(str(nested), confirm=lambda _value: True)
    assert overlap.value.code == "overlapping_import_root"

    with pytest.raises(ValueError, match="reserved capture delivery"):
        engine.capture.accept(
            TextPayload("not importer-owned"),
            delivery_id="markdown-import." + "0" * 64,
        )
    assert _count(brain, "captures") == 0


def test_registered_root_alias_reuses_identity_and_move_is_refused(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("root-identity-token", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(vault, target_is_directory=True)
    engine = _engine(brain)

    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    repeated = engine.markdown_import.import_directory(str(alias))

    assert repeated.unchanged == 1
    assert _count(brain, "markdown_import_roots") == 1

    moved = tmp_path / "moved"
    vault.rename(moved)
    with pytest.raises(MarkdownImportFailure) as failure:
        engine.markdown_import.import_directory(str(moved))
    assert failure.value.code == "import_root_changed"


def test_macos_case_alias_reuses_registered_root_identity(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        pytest.skip("case alias requires macOS")
    brain = tmp_path / "brain"
    vault = tmp_path / "CaseVault"
    vault.mkdir()
    (vault / "note.md").write_text("case-alias-token", encoding="utf-8")
    alias = tmp_path / "casevault"
    if not alias.is_dir() or alias.samefile(vault) is False:
        pytest.skip("test volume is case-sensitive")
    engine = _engine(brain)

    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    repeated = engine.markdown_import.import_directory(str(alias))

    assert repeated.unchanged == 1
    assert _count(brain, "markdown_import_roots") == 1


def test_replaced_root_and_registered_descendant_are_refused(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "note.md").write_text("overlap-token", encoding="utf-8")
    engine = _engine(brain)
    engine.markdown_import.import_directory(str(parent), confirm=lambda _value: True)

    with pytest.raises(MarkdownImportFailure) as overlap:
        engine.markdown_import.import_directory(str(child))
    assert overlap.value.code == "overlapping_import_root"

    archived = tmp_path / "archived"
    parent.rename(archived)
    parent.mkdir()
    (parent / "replacement.md").write_text("replacement", encoding="utf-8")
    with pytest.raises(MarkdownImportFailure) as replaced:
        engine.markdown_import.import_directory(str(parent))
    assert replaced.value.code == "import_root_changed"


def test_root_replacement_after_confirmation_stops_before_registration(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("pre-registration-root-swap", encoding="utf-8")
    archived = tmp_path / "archived"
    engine = _engine(brain)

    def replace_root(_preflight: object) -> bool:
        vault.rename(archived)
        vault.mkdir()
        return True

    with pytest.raises(MarkdownImportFailure) as failure:
        engine.markdown_import.import_directory(str(vault), confirm=replace_root)

    assert failure.value.code == "import_root_changed"
    assert _count(brain, "markdown_import_roots") == 0
    assert _count(brain, "markdown_import_revisions") == 0
    assert _count(brain, "captures") == 0


def test_registered_root_replacement_stops_before_missing_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("pre-finalization-root-swap", encoding="utf-8")
    engine = _engine(brain)
    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    note.unlink()
    with sqlite3.connect(_database(brain)) as connection:
        previous_scan = connection.execute(
            "SELECT last_complete_scan_id FROM markdown_import_roots"
        ).fetchone()[0]
    archived = tmp_path / "archived"
    calls = 0
    original_revalidate = engine.markdown_import._revalidate_selection

    def replace_before_final_revalidation(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            vault.rename(archived)
            vault.mkdir()
        original_revalidate(*args, **kwargs)

    monkeypatch.setattr(
        engine.markdown_import,
        "_revalidate_selection",
        replace_before_final_revalidation,
    )
    with pytest.raises(MarkdownImportFailure) as failure:
        engine.markdown_import.import_directory(str(vault))

    assert failure.value.code == "import_root_changed"
    assert len(engine.retrieval.search("pre-finalization-root-swap")) == 1
    with sqlite3.connect(_database(brain)) as connection:
        row = connection.execute(
            "SELECT active_revision_id FROM markdown_import_files"
        ).fetchone()
        current_scan = connection.execute(
            "SELECT last_complete_scan_id FROM markdown_import_roots"
        ).fetchone()[0]
    assert row[0] is not None
    assert current_scan == previous_scan


def test_rebuild_projects_only_active_import_revision_and_preserves_canonical_page(
    tmp_path: Path,
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# Source A\nactive-source-a-token", encoding="utf-8")
    engine = _engine(brain, starter_spaces=("Notes",))
    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    source_a = engine.retrieval.search("active-source-a-token")[0]
    space = engine.inbox.spaces()[0]
    engine.inbox.route(
        source_a.capture_id,
        space.space_id,
        delivery_id="markdown.import.route.canonical",
    )
    proposal = engine.review.propose(
        source_a.capture_id,
        (ProposalDraft("Imported canonical", "durable-canonical-token"),),
        delivery_id="markdown.import.proposal.canonical",
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="markdown.import.decision.canonical",
    )
    canonical_before = engine.retrieval.search("durable-canonical-token", record_type="canonical")
    assert len(canonical_before) == 1
    assert canonical_before[0].trust == "unverified"
    canonical_path = next((brain / "content/spaces").rglob("page_*.md"))
    assert parse_markdown(canonical_path.read_bytes()).fields["trust"] == "reviewed"

    portable = tmp_path / "portable-reviewed-import"
    engine.portability.export(
        portable,
        export_id="export_00000000-0000-4000-8000-000000000405",
    )
    engine.portability.validate(portable)
    restored = tmp_path / "restored-reviewed-import"
    engine.portability.import_clean(
        portable,
        restored,
        import_id="import_00000000-0000-4000-8000-000000000406",
    )
    restored_canonical = _engine(restored).retrieval.search(
        "durable-canonical-token",
        record_type="canonical",
    )
    assert len(restored_canonical) == 1
    assert restored_canonical[0].trust == "unverified"

    note.write_text("# Source B\nactive-source-b-token", encoding="utf-8")
    engine.markdown_import.import_directory(str(vault))
    engine._rederive_live_search_projection()

    assert engine.retrieval.search("active-source-a-token", record_type="source") == ()
    assert len(engine.retrieval.search("active-source-b-token", record_type="source")) == 1
    assert (
        engine.retrieval.search("durable-canonical-token", record_type="canonical")
        == canonical_before
    )


def test_rebuild_excludes_pending_superseded_and_missing_import_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("state-a-token", encoding="utf-8")
    engine = _engine(brain)
    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)

    note.write_text("state-b-token", encoding="utf-8")
    engine.markdown_import.import_directory(str(vault))
    original_activation = engine.markdown_import._activate_revision

    def stop_after_capture(**_kwargs: object) -> None:
        raise RuntimeError("synthetic pending revision")

    monkeypatch.setattr(engine.markdown_import, "_activate_revision", stop_after_capture)
    note.write_text("state-c-token", encoding="utf-8")
    with pytest.raises(RuntimeError, match="pending revision"):
        engine.markdown_import.import_directory(str(vault))
    monkeypatch.setattr(engine.markdown_import, "_activate_revision", original_activation)

    engine._rederive_live_search_projection()
    assert engine.retrieval.search("state-a-token", record_type="source") == ()
    assert len(engine.retrieval.search("state-b-token", record_type="source")) == 1
    assert engine.retrieval.search("state-c-token", record_type="source") == ()

    note.unlink()
    missing = engine.markdown_import.import_directory(str(vault))
    assert missing.missing == 1
    engine._rederive_live_search_projection()
    assert engine.retrieval.search("state-b-token", record_type="source") == ()


def test_pending_reservation_binds_complete_request_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("reservation-recovery-token", encoding="utf-8")
    engine = _engine(brain)
    original_submit = engine._submit_capture
    captured: list[CaptureSubmission] = []

    def stop_before_capture(submission: CaptureSubmission) -> None:
        captured.append(submission)
        raise RuntimeError("synthetic reservation stop")

    monkeypatch.setattr(engine, "_submit_capture", stop_before_capture)
    with pytest.raises(RuntimeError, match="reservation stop"):
        engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    assert _count(brain, "markdown_import_revisions") == 1
    assert _count(brain, "captures") == 0
    submission = captured[0]
    altered_role = dict(submission.role_claim)
    altered_role["role_id"] = "role_00000000-0000-4000-8000-000000000498"
    altered_actor = "actor_00000000-0000-4000-8000-000000000499"
    altered_actor_role = dict(submission.role_claim)
    altered_actor_role["actor_id"] = altered_actor
    altered_actor_role["role_claim_id"] = "role_claim_00000000-0000-4000-8000-000000000497"
    changed_reference = submission.source_reference + "-changed"
    variants = (
        replace(
            submission,
            payload=FilePayload("note.md", "text/markdown", b"changed payload"),
        ),
        replace(submission, title="Changed title"),
        replace(submission, role_claim=altered_role),
        replace(submission, actor_id=altered_actor, role_claim=altered_actor_role),
        replace(
            submission,
            source_reference=changed_reference,
            provenance=Provenance.create(
                source_ref=changed_reference,
                content_origin=ContentOrigin.UNKNOWN,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            ),
        ),
        replace(
            submission,
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.PERSONAL,
                reason=PrivacyReason.EXPLICIT_LOCAL_ONLY,
                policy_version="privacy-v1",
                authority=Authority(cloud=False, external_egress=False),
            ),
        ),
    )
    for altered in variants:
        with pytest.raises(ValueError, match="invalid reserved capture delivery"):
            capture_submission_is_reserved(engine, altered)

    monkeypatch.setattr(engine, "_submit_capture", original_submit)
    resumed = engine.markdown_import.import_directory(str(vault))

    assert resumed.imported == 1
    assert _count(brain, "markdown_import_revisions") == 1
    assert _count(brain, "captures") == 1
    assert len(engine.retrieval.search("reservation-recovery-token")) == 1


def test_invalid_content_preserves_revision_and_refreshes_stable_identity(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("stable-prior-token", encoding="utf-8")
    engine = _engine(brain)
    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    note.unlink()
    note.write_bytes(b"invalid-\xff")
    current = note.stat()

    summary = engine.markdown_import.import_directory(str(vault))

    assert summary.failed == 1
    assert summary.entries[0].reason == "invalid_utf8"
    assert len(engine.retrieval.search("stable-prior-token")) == 1
    with sqlite3.connect(_database(brain)) as connection:
        observed = connection.execute(
            "SELECT last_observed_device, last_observed_inode "
            "FROM markdown_import_files WHERE relative_path = 'note.md'"
        ).fetchone()
    assert observed == (str(current.st_dev), str(current.st_ino))


def test_failed_path_is_preserved_while_unrelated_missing_path_finalizes(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    failed = vault / "failed.md"
    missing = vault / "missing.md"
    failed.write_text("preserved-failed-token", encoding="utf-8")
    missing.write_text("removed-missing-token", encoding="utf-8")
    engine = _engine(brain)
    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    failed.write_bytes(b"invalid-\xff")
    missing.unlink()

    summary = engine.markdown_import.import_directory(str(vault))

    assert (summary.failed, summary.missing, summary.missing_finalized) == (1, 1, True)
    assert len(engine.retrieval.search("preserved-failed-token")) == 1
    assert engine.retrieval.search("removed-missing-token") == ()


def test_interruption_before_registration_writes_no_import_state(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("interrupt-before-register", encoding="utf-8")
    requested = False

    def confirm(_value: object) -> bool:
        nonlocal requested
        requested = True
        return True

    with pytest.raises(MarkdownImportInterrupted):
        _engine(brain).markdown_import.import_directory(
            str(vault),
            confirm=confirm,
            interrupted=lambda: requested,
        )
    assert _count(brain, "markdown_import_roots") == 0
    assert _count(brain, "captures") == 0


def test_interruption_after_stable_read_stops_before_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("interrupt-after-read", encoding="utf-8")
    engine = _engine(brain)
    requested = False
    import_module = cast(Any, importlib.import_module("open_brain_engine.engine.markdown_import"))
    original_read = cast(Any, import_module.read_markdown_candidate)

    def read_then_interrupt(*args: Any, **kwargs: Any) -> bytes:
        nonlocal requested
        payload = cast(bytes, original_read(*args, **kwargs))
        requested = True
        return payload

    monkeypatch.setattr(import_module, "read_markdown_candidate", read_then_interrupt)
    with pytest.raises(MarkdownImportInterrupted):
        engine.markdown_import.import_directory(
            str(vault),
            confirm=lambda _value: True,
            interrupted=lambda: requested,
        )

    assert _count(brain, "markdown_import_roots") == 1
    assert _count(brain, "markdown_import_revisions") == 0
    assert _count(brain, "captures") == 0


def test_interruption_after_file_outcome_keeps_commit_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("interrupt-after-outcome", encoding="utf-8")
    engine = _engine(brain)
    requested = False
    original_activate = engine.markdown_import._activate_revision

    def activate_then_interrupt(**kwargs: object) -> None:
        nonlocal requested
        cast(Any, original_activate)(**kwargs)
        requested = True

    monkeypatch.setattr(engine.markdown_import, "_activate_revision", activate_then_interrupt)
    with pytest.raises(MarkdownImportInterrupted):
        engine.markdown_import.import_directory(
            str(vault),
            confirm=lambda _value: True,
            interrupted=lambda: requested,
        )
    assert _count(brain, "captures") == 1
    assert len(engine.retrieval.search("interrupt-after-outcome")) == 1
    with sqlite3.connect(_database(brain)) as connection:
        assert (
            connection.execute(
                "SELECT last_complete_scan_id FROM markdown_import_roots"
            ).fetchone()[0]
            is None
        )

    monkeypatch.setattr(engine.markdown_import, "_activate_revision", original_activate)
    assert engine.markdown_import.import_directory(str(vault)).unchanged == 1


def test_interruption_after_final_revalidation_stops_before_missing_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("interrupt-before-finalization", encoding="utf-8")
    engine = _engine(brain)
    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    note.unlink()
    requested = False
    calls = 0
    original_revalidate = engine.markdown_import._revalidate_selection

    def revalidate_then_request(*args: Any, **kwargs: Any) -> None:
        nonlocal calls, requested
        original_revalidate(*args, **kwargs)
        calls += 1
        if calls == 2:
            requested = True

    monkeypatch.setattr(engine.markdown_import, "_revalidate_selection", revalidate_then_request)
    with pytest.raises(MarkdownImportInterrupted):
        engine.markdown_import.import_directory(str(vault), interrupted=lambda: requested)

    assert len(engine.retrieval.search("interrupt-before-finalization")) == 1
    with sqlite3.connect(_database(brain)) as connection:
        assert (
            connection.execute("SELECT active_revision_id FROM markdown_import_files").fetchone()[0]
            is not None
        )


def test_keyboard_interrupt_after_finalization_commit_returns_completed_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("post-commit-interrupt", encoding="utf-8")
    engine = _engine(brain)
    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    note.unlink()
    original_transaction = engine._store.transaction

    @contextmanager
    def interrupt_after_commit() -> Iterator[sqlite3.Connection]:
        with original_transaction() as connection:
            yield connection
        raise KeyboardInterrupt

    monkeypatch.setattr(engine._store, "transaction", interrupt_after_commit)

    summary = engine.markdown_import.import_directory(str(vault))

    assert (summary.missing, summary.missing_finalized) == (1, True)
    assert engine.retrieval.search("post-commit-interrupt") == ()


@pytest.mark.parametrize(
    ("text", "path", "expected"),
    (
        ("---\ntitle: Plain title\n---\n# Heading", "note.md", "Plain title"),
        ('---\ntitle: "JSON title"\n---\n', "note.md", "JSON title"),
        ("---\ntitle: 'Owner''s title'\n---\n", "note.md", "Owner's title"),
        ("---\ntitle: one\ntitle: two\n---\n# Heading", "note.md", "Heading"),
        ("---\ntitle: [collection]\n---\n# Heading", "note.md", "Heading"),
        ("---\nno close\n# Body heading", "note.md", "Body heading"),
        ("body", ".md", "Untitled note"),
        ("# A\u200b title\n", "note.md", "A title"),
    ),
)
def test_title_extraction_is_bounded_and_inert(text: str, path: str, expected: str) -> None:
    assert extract_markdown_title(text, path) == expected


def test_production_import_bounds_and_empty_file_contract_are_fixed() -> None:
    assert (
        MAX_VISITED_ENTRIES,
        MAX_SELECTED_FILES,
        MAX_AGGREGATE_BYTES,
        MAX_FILE_BYTES,
    ) == (100_000, 10_000, 536_870_912, 1_048_576)
    assert FilePayload("empty.md", "text/markdown", b"").data == b""
