from __future__ import annotations

from pathlib import Path

from open_brain_engine.storage.markdown import render_markdown


def note_fields(
    *,
    page_id: str,
    source_type: str = "youtube",
    processed_at: str | None = "2026-01-02T04:05:06Z",
) -> dict[str, object]:
    fields: dict[str, object] = {
        "captured_at": "2026-01-02T03:04:05Z",
        "custom": {"labels": ["synthetic", "public"], "reviewed": True},
        "page_id": page_id,
        "schema_version": 1,
        "source_type": source_type,
        "title": "Synthetic migration note",
    }
    if processed_at is not None:
        fields["processed_at"] = processed_at
    return fields


def write_note(
    root: Path,
    relative: str,
    *,
    fields: dict[str, object],
    body: str = "# Synthetic migration note\n\nSynthetic body.",
) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(fields=fields, body=body))
    return path
