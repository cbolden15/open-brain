from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.core.models import (
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    Intent,
    Provenance,
    SourceType,
)
from open_brain_engine.storage.filesystem import DuplicateConflictError, RootConfinementError

from open_brain_legacy._compat.open_brain.integrations import (
    PageReadRequest,
    TrustLabel,
    VaultWriteDisposition,
    VaultWriteRequest,
)
from open_brain_legacy.integrations.obsidian import (
    NormalizedNote,
    NoteStatus,
    ObsidianTaxonomy,
    ObsidianVault,
)

CAPTURED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
PROCESSED_AT = datetime(2026, 1, 2, 4, 5, 6, tzinfo=UTC)


def _taxonomy() -> ObsidianTaxonomy:
    return ObsidianTaxonomy.create(
        reviewed="reviewed/pages",
        destinations={
            SourceType.YOUTUBE: "reference/videos",
            SourceType.SOCIAL: "reference/social",
            SourceType.WEB: "reference/articles",
            SourceType.TEXT: "reference/notes",
        },
    )


def _note(
    *,
    source_type: SourceType,
    status: NoteStatus = NoteStatus.READY,
) -> NormalizedNote:
    labels = {
        SourceType.YOUTUBE: ("youtube-001", "Synthetic video", ContentKind.VIDEO),
        SourceType.SOCIAL: ("social-001", "Synthetic post", ContentKind.POST),
        SourceType.WEB: ("web-001", "Synthetic article", ContentKind.ARTICLE),
    }
    suffix, title, content_kind = labels[source_type]
    source_url = f"https://{source_type.value}.example.invalid/items/{suffix}"
    failed = status is NoteStatus.FAILED
    return NormalizedNote.create(
        page_id=f"note.{suffix}",
        title=title if not failed else "Synthetic unavailable video",
        source_type=source_type,
        content_kind=content_kind,
        source_url=source_url,
        status=status,
        summary=None if failed else f"Synthetic {source_type.value} summary.",
        transcript=(
            "First synthetic line.\n\nSecond synthetic line."
            if source_type is SourceType.YOUTUBE and not failed
            else None
        ),
        failure_reason="Synthetic transcript unavailable." if failed else None,
        capture_why="Synthetic owner context.",
        trust=TrustLabel.UNREVIEWED_THIRD_PARTY,
        intent=Intent.REFERENCE,
        provenance=Provenance.create(
            source_ref=source_url,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        captured_at=CAPTURED_AT,
        processed_at=PROCESSED_AT,
        review_id="review.synthetic-001",
    )


def _expected(note: NormalizedNote) -> str:
    fields = {
        "capture_why": note.capture_why,
        "captured_at": "2026-01-02T03:04:05Z",
        "content_kind": note.content_kind.value,
        "intent": note.intent.value,
        "page_id": note.page_id,
        "processed_at": "2026-01-02T04:05:06Z",
        "provenance": note.provenance.to_dict(),
        "review_id": note.review_id,
        "schema_version": 1,
        "source_type": note.source_type.value,
        "source_url": note.source_url,
        "status": note.status.value,
        "title": note.title,
        "transcript_present": note.transcript is not None,
        "trust": note.trust.value,
    }
    lines = [
        "---",
        *[
            f"{key}: "
            + json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            for key, value in sorted(fields.items())
        ],
        "---",
        "",
        f"# {note.title}",
        "",
    ]
    if note.status is NoteStatus.FAILED:
        lines.extend(
            [
                "> [!warning] Processing failed",
                f"> {note.failure_reason}",
                "",
            ]
        )
    else:
        lines.extend(["## Summary", "", str(note.summary), ""])
    lines.extend(["## Why it was captured", "", note.capture_why])
    if note.transcript is not None:
        lines.extend(
            [
                "",
                "## Transcript",
                "",
                "> [!quote]- Transcript",
                "> First synthetic line.",
                ">",
                "> Second synthetic line.",
            ]
        )
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("source_type", "status", "destination"),
    [
        (SourceType.YOUTUBE, NoteStatus.READY, "reference/videos"),
        (SourceType.SOCIAL, NoteStatus.READY, "reference/social"),
        (SourceType.WEB, NoteStatus.READY, "reference/articles"),
        (SourceType.YOUTUBE, NoteStatus.FAILED, "reference/videos"),
    ],
)
def test_golden_normalized_notes_preserve_public_capture_context(
    tmp_path: Path,
    source_type: SourceType,
    status: NoteStatus,
    destination: str,
) -> None:
    note = _note(source_type=source_type, status=status)
    vault = ObsidianVault(root=tmp_path, taxonomy=_taxonomy())

    result = vault.write_note(note)
    relative = _taxonomy().relative_path(source_type=source_type, page_id=note.page_id)

    assert result.disposition is VaultWriteDisposition.CREATED
    assert relative.as_posix() == f"{destination}/{note.page_id}.md"
    assert (tmp_path / relative).read_text() == _expected(note)


def test_note_replay_is_a_noop_and_changed_collision_never_overwrites(tmp_path: Path) -> None:
    note = _note(source_type=SourceType.YOUTUBE)
    vault = ObsidianVault(root=tmp_path, taxonomy=_taxonomy())
    original = _expected(note)

    assert vault.write_note(note).disposition is VaultWriteDisposition.CREATED
    duplicate = vault.write_note(note)
    with pytest.raises(DuplicateConflictError, match="immutable record conflict"):
        vault.write_note(replace(note, summary="Changed synthetic summary."))

    relative = _taxonomy().relative_path(source_type=note.source_type, page_id=note.page_id)
    assert duplicate.disposition is VaultWriteDisposition.DUPLICATE
    assert duplicate.bytes_written == 0
    assert (tmp_path / relative).read_text() == original


def test_typed_vault_write_and_read_remain_review_bound(tmp_path: Path) -> None:
    vault = ObsidianVault(root=tmp_path, taxonomy=_taxonomy())
    request = VaultWriteRequest(
        page_id="page.synthetic-001",
        title="Synthetic reviewed page",
        markdown="# Synthetic reviewed page\n\nApproved body.",
        review_id="review.synthetic-001",
    )

    created = vault.write(request)
    duplicate = vault.write(request)
    loaded = vault.read(PageReadRequest(page_id=request.page_id))

    assert created.disposition is VaultWriteDisposition.CREATED
    assert duplicate.disposition is VaultWriteDisposition.DUPLICATE
    assert loaded is not None
    assert loaded.title.text == request.title
    assert loaded.markdown.text == request.markdown
    assert loaded.trust is TrustLabel.VERIFIED_WORK


def test_taxonomy_rejects_traversal_before_io(tmp_path: Path) -> None:
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

    assert not tuple(tmp_path.iterdir())


def test_vault_refuses_symlinked_destination(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "reference").symlink_to(outside, target_is_directory=True)
    vault = ObsidianVault(root=tmp_path, taxonomy=_taxonomy())

    with pytest.raises(RootConfinementError, match="unsafe storage path"):
        vault.write_note(_note(source_type=SourceType.YOUTUBE))

    assert not tuple(outside.iterdir())
