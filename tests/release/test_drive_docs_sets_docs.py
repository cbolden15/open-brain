from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/integrations/google-sources.md"
OPERATOR = ROOT / "docs/integrations/priority-capture-operator.md"
CONTRIBUTOR_GUIDE = ROOT / "CLAUDE.md"
GOTCHAS = ROOT / "docs/engineering/gotchas/README.md"
PROPER_NOUNS = ("Open", "Brain", "Google", "Drive", "Docs", "Gemini", "Meet", "Gmail", "Slack")
TELLS = ("not just", "seamless", "robust", "powerful", "effortless", "dive in")
FENCE = chr(96) * 3
MEET_SELECTION = {
    "selection": {
        "connector_name": "google_drive",
        "connection_id": "account:REPLACE_FROM_ACCOUNTS",
        "resource_id": "drive_account:all",
        "resource_type": "drive_account",
    },
    "options": {
        "date_floor": "2026-10-01",
        "title_suffixes": ["Notes by Gemini", "Transcript"],
    },
}


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


def test_drive_guide_documents_docs_sets_and_the_quarantined_single_file_edit() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    required = (
        "## Drive Docs sets",
        "### Shapes and options",
        "### Listing query",
        "### Checkpoint",
        "### Fetch and per-file notices",
        "### Splitting",
        "### Revision behavior",
        "### Live acceptance for Drive Docs sets",
        "`drive_file`",
        "`drive_folder`",
        "`drive_account`",
        "`drive_folder:<folderId>`",
        "`drive_account:all`",
        "`date_floor`",
        "`title_suffixes`",
        '`["Notes by Gemini", "Transcript"]`',
        "1 to 8 strings",
        "1 to 64 characters",
        "`drive_invalid_options`",
        "`drive_invalid_selection`",
        "`drive_invalid_checkpoint`",
        "mimeType = 'application/vnd.google-apps.document'",
        "modifiedTime > '<bound>'",
        "in parents",
        "name contains",
        "`orderBy=modifiedTime`",
        "`pageSize=100`",
        "`includeItemsFromAllDrives=true`",
        "`supportsAllDrives=true`",
        '"mode": "backfill"',
        "`incremental`",
        "`page_token`",
        "`watermark`",
        "`known`",
        "24 hours",
        "500 entries",
        "25 intakes and 25 notices",
        "`has_more`",
        "`drive_file_skipped:<reason>:<fileId>`",
        "`missing|access_lost|trashed|not_downloadable|not_text|too_large|empty`",
        "`drive_folder_missing`",
        "`google_auth_required`",
        "`google_rate_limited`",
        "`drive_forbidden`",
        "60,000 characters",
        "18 parts",
        "`drive_file_skipped:too_large:<fileId>`",
        "`file:<fileId>#part:<n>`",
        "`<name> (part <n> of <N>)`",
        "200 characters",
        "`revision:<drive revision>`",
        "`drive-revisions`",
        "new revision of the same source record",
        "surplus parts keep their last revision",
        "An edited `drive_file` record is quarantined, not re-imported.",
        "`source_revision_conflict`",
        "`Source revision:`",
        "DOCS-005",
        "owner-authorized",
        "outside Git",
        "one real Gemini note",
        "one real transcript over 60,000 characters",
        "one edit to the note",
        "synthetic acceptance only",
        "--resource-type drive_folder",
    )
    missing = [token for token in required if token not in text]
    assert not missing, missing
    # The old single-file-only statements are gone.
    assert "does not recursively mirror folders" not in text
    assert "exactly one file" not in text
    # The query block lists the two optional clauses in the spec's order.
    query = text.index("mimeType = 'application/vnd.google-apps.document'")
    assert query < text.index("[and '<folderId>' in parents]") < text.index("name contains")
    # The corrected edit statement sits in the Google Drive section, before the Docs-set section.
    assert text.index("quarantined, not re-imported") < text.index("## Drive Docs sets")
    _check_headings_and_tone(text)


def test_operator_guide_walks_through_meet_notes_with_the_exact_arguments_file() -> None:
    text = OPERATOR.read_text(encoding="utf-8")
    required = (
        "## Capture Gemini Meet notes from Drive",
        "resources --provider google_drive --connection-id account:REPLACE_FROM_ACCOUNTS",
        "--resource-type drive_folder",
        "`drive_file` (the default)",
        "`drive_folder:<id>`",
        "`drive_account:all` is a fixed id",
        "--foreground configure --arguments-file /absolute/path/to/meet-notes.json",
        "--foreground preview --source-id REPLACE_FROM_CONFIGURE",
        "import --source-id REPLACE_FROM_CONFIGURE --preview-id REPLACE_FROM_PREVIEW",
        "--action enable --interval-seconds 900",
        "`<name> (part <n> of <N>)`",
        "new revision of the same record",
        "`source_revision_conflict`",
        "[Google setup](google-sources.md)",
        "A `drive_file` selection takes an empty options object.",
    )
    missing = [token for token in required if token not in text]
    assert not missing, missing
    assert "without folder recursion" not in text
    assert _json_block_containing(text, '"drive_account:all"') == MEET_SELECTION
    # The walkthrough comes after the generic select/preview/import section and before recurring
    # collection, so a reader meets configure, preview and import once before the Drive variant.
    assert (
        text.index("## Select, preview and import")
        < text.index("## Capture Gemini Meet notes from Drive")
        < text.index("## Recurring collection")
    )
    _check_headings_and_tone(text)


def test_contributor_guide_states_the_drive_selection_shapes() -> None:
    text = CONTRIBUTOR_GUIDE.read_text(encoding="utf-8")
    sentence = "Drive capture selects a file, a folder of Docs, or account Docs by title suffix."
    assert text.count(sentence) == 1
    boundary = text.index("## Product boundary")
    assert boundary < text.index(sentence) < text.index("## Architecture")
    # The statements the product-split suite pins are still present.
    assert "does not install a desktop app, collector, scheduler" in text
    assert "Open Brain never requires root" in text


def test_gotcha_registry_records_the_drive_file_edit_contradiction() -> None:
    text = GOTCHAS.read_text(encoding="utf-8")
    assert text.count("### DOCS-005:") == 1
    entry = text[text.index("### DOCS-005:") :]
    entry = entry[: entry.index("\n### ", 1)]
    for token in (
        "Category: Documentation / Google Drive.",
        "Symptom:",
        "Action:",
        "Discovered: 2026-10-25",
        "`drive_file`",
        "`Source revision:`",
        "`source_revision_conflict`",
        "`9f22849`",
        "`drive_folder`",
        "`drive_account`",
        "`drive-revisions`",
        "docs/integrations/google-sources.md",
    ):
        assert token in entry, token
    # Newest entries go first; the new entry sits above the previous top entry.
    assert text.index("### DOCS-005:") < text.index("### BUILD-021:")
    assert "/Users/" not in entry
