from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/integrations/google-sources.md"
PROPER_NOUNS = ("Open", "Brain", "Google", "Drive", "Docs", "Gemini", "Meet", "Gmail", "Slack")
TELLS = ("not just", "seamless", "robust", "powerful", "effortless", "dive in")


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
