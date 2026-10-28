from __future__ import annotations

import json
from pathlib import Path

from open_brain_connectors.runtime.saved_item import RECORD_REFUSAL_CODES, TRACKING_RULE_VERSION

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/integrations/saved-items.md"
CONTRIBUTOR_GUIDE = ROOT / "CLAUDE.md"
GOTCHAS = ROOT / "docs/engineering/gotchas/README.md"
PROPER_NOUNS = ("Open", "Brain", "iOS", "Shortcut", "iCloud", "Drive", "JSON", "URL")
TELLS = ("not just", "seamless", "robust", "powerful", "effortless", "dive in")
FENCE = chr(96) * 3
THIN = {
    "schema_version": 1,
    "url": "https://example.com/post/123",
    "why": "Recipe to try this weekend",
    "received_at": "2026-10-26T14:00:00Z",
    "fetch_status": "not_attempted",
}
SECTIONS = (
    "## Record format",
    "## What a capture contains",
    "## The stated reason",
    "## Identity and tracking parameters",
    "## First save wins",
    "## Select a folder you control",
    "## Set up the source",
    "## Refusals and reports",
    "## Example iOS Shortcut",
)


def _check_headings_and_tone(text: str) -> None:
    headings = [line for line in text.splitlines() if line.startswith("#")]
    for heading in headings[1:]:
        words = heading.lstrip("# ").split()
        assert words[0][0].isupper() and all(
            word == word.lower() or word.startswith(PROPER_NOUNS) for word in words[1:]
        ), heading
    lowered = text.lower()
    for tell in TELLS:
        assert tell not in lowered, tell
    assert "/Users/" not in text


def _json_block_containing(text: str, needle: str) -> object:
    # FENCE is built from chr(96) so this file never contains a literal Markdown fence.
    for block in text.split(FENCE + "json")[1:]:
        body = block.split(FENCE, 1)[0]
        if needle in body:
            parsed: object = json.loads(body)
            return parsed
    raise AssertionError(f"no fenced json block contains {needle!r}")


def test_saved_items_guide_documents_the_record_contract() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    _check_headings_and_tone(text)
    positions = [text.index(section) for section in SECTIONS]
    assert positions == sorted(positions)
    assert _json_block_containing(text, '"fetch_status": "not_attempted"') == THIN
    for row in (
        "| Any | `utm_*`, `fbclid`, `gclid` |",
        "| `instagram.com`, `threads.net`, `threads.com` | `igsh`, `igshid` |",
        "| `youtube.com`, `youtu.be`, `spotify.com` | `si` |",
    ):
        assert text.count(row) == 1, row
    assert f"Tracking rule version {TRACKING_RULE_VERSION} " in text
    codes = RECORD_REFUSAL_CODES | {
        "already_captured",
        "not_regular_file",
        "folder_missing",
        "folder_unsafe",
        "folder_over_limit",
    }
    for code in sorted(codes):
        assert f"`{code}`" in text, code
    for phrase in (
        "`Stated reason`",
        "first writer of a URL wins",
        "`[truncated]`",
        "131,072 bytes",
        "65,536 bytes",
        "at most 25 items",
        "1 MiB",
        "Why are you saving this?",
        "Share Sheet",
    ):
        assert phrase in text, phrase


def test_saved_items_guide_gives_the_exact_selection_commands() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "--foreground saved-item-select --folder-path /absolute/path/to/saved-items" in text
    assert "--privacy-tier personal > saved-items-selection.json" in text
    assert '--foreground configure --arguments-file "$PWD/saved-items-selection.json"' in text
    assert "not on any revision allow-list" in text


def test_contributor_guide_names_the_saved_item_source() -> None:
    text = CONTRIBUTOR_GUIDE.read_text(encoding="utf-8")
    sentence = (
        "Saved item capture reads producer-written JSON records from one selected folder "
        "(`docs/integrations/saved-items.md`)."
    )
    assert text.count(sentence) == 1
    drive = text.index("Drive capture selects a file, a folder of Docs")
    assert text.index("## Product boundary") < drive < text.index(sentence)
    assert text.index(sentence) < text.index("## Architecture")


def test_gotcha_registry_records_the_redaction_breadth() -> None:
    text = GOTCHAS.read_text(encoding="utf-8")
    assert text.count("### CAPTURE-004:") == 1
    entry = text[text.index("### CAPTURE-004:") :]
    entry = entry[: entry.index("\n### ", 1)]
    for token in (
        "Category: Capture / Saved items.",
        "Symptom:",
        "Action:",
        "Discovered: 2026-10-26",
        "`has_redaction_finding`",
        "`redact_text`",
        "`url_secret`",
        "`fbclid`",
        "docs/integrations/saved-items.md",
    ):
        assert token in entry, token
    assert text.index("### CAPTURE-004:") < text.index("### DOCS-005:")
    assert "/Users/" not in entry
