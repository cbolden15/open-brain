from __future__ import annotations

from pathlib import Path

from open_brain.services.brief_contracts import (
    BRIEF_ERROR_CODES,
    BRIEF_FOLLOWUP_SCAN_LIMIT,
    BRIEF_LOG_MAX_BRIEFS,
    BRIEF_LOG_MAX_FILE_BYTES,
    BRIEF_LOG_MAX_KEYS,
    BRIEF_MAX_FAMILY_ITEMS,
    BRIEF_MAX_ITEMS,
    BRIEF_RESPONSE_MAX_BYTES,
    BRIEF_STALE_DAYS_DEFAULT,
    BRIEF_STALE_DAYS_MAX,
    BriefChange,
    BriefItemKind,
    BriefReasonCode,
)
from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/daily-brief.md"
PROPER_NOUNS = (
    "Open",
    "Brain",
    "MCP",
    "CLI",
    "Portable",
    "Work",
    "Personal",
    "Markdown",
    "Claude",
    "Codex",
    "JSON",
)
COVERAGE_FIELDS = (
    "families_checked",
    "families_stale",
    "captures_unavailable",
    "followups_scanned",
    "followups_truncated",
    "hidden_dropped",
)


def test_daily_brief_guide_names_every_tool_grant_command_kind_code_and_limit() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    tools = sorted(
        str(tool["name"])
        for tool in MCP_REGISTERED_TOOLS
        if str(tool["name"]).startswith("brain_brief")
    )
    assert tools == ["brain_brief", "brain_brief_ack"]
    required = (
        *tools,
        "`brief-read`",
        "`brief-ack`",
        "--allow-brief-read",
        "--allow-brief-ack",
        "`followup-read`",
        "open-brain brief",
        "open-brain brief generate",
        "open-brain brief ack",
        "open-brain brief last",
        "`--space`",
        "`--stale-days`",
        "`--json`",
        *(f"`{kind.value}`" for kind in BriefItemKind),
        *(f"`{code.value}`" for code in BriefReasonCode),
        *(f"`{change.value}`" for change in BriefChange),
        *(f"`{name}`" for name in COVERAGE_FIELDS),
        "`not_read`",
        "`not_granted`",
        "`failed`",
        "`withheld`",
        "`unassigned`",
        *(f"`{code}`" for code in sorted(BRIEF_ERROR_CODES)),
        f"{BRIEF_MAX_ITEMS} items",
        f"{BRIEF_MAX_FAMILY_ITEMS} family items",
        f"{BRIEF_STALE_DAYS_DEFAULT} days",
        f"{BRIEF_STALE_DAYS_MAX}",
        f"{BRIEF_FOLLOWUP_SCAN_LIMIT} open",
        f"{BRIEF_RESPONSE_MAX_BYTES // 1024} KB",
        f"{BRIEF_LOG_MAX_BRIEFS} briefs",
        f"{BRIEF_LOG_MAX_FILE_BYTES // 1024} KB",
        f"{BRIEF_LOG_MAX_KEYS} key files",
        "1 to 2200",
        "`model`",
        "`none`",
        "`since`",
        "`saved`",
        "`nothing_needs_attention`",
        "`<key>.json.aside`",
        "`.open-brain/state/briefs/`",
        "followup show",
        "followup decide",
        "followups.md",
        "`--session-policy`",
    )
    missing = [token for token in required if token not in text]
    assert not missing, missing
    headings = [line for line in text.splitlines() if line.startswith("#")]
    for heading in headings[1:]:
        words = heading.lstrip("# ").split()
        assert words[0][0].isupper() and all(
            word == word.lower() or word.startswith(PROPER_NOUNS) for word in words[1:]
        ), heading
    lowered = text.lower()
    for tell in ("not just", "seamless", "robust", "powerful", "effortless", "dive in"):
        assert tell not in lowered, tell
    # The guide states the version-1 boundary plainly and never promises background work.
    assert "collector health" in lowered
    assert "scheduled generation" in lowered
    assert "not in version 1" in lowered
    assert "no model call" in lowered
    assert "morning summary" in lowered
    # Spec 2.1 execution amendments the guide must state.
    assert "needs both" in lowered  # acknowledging needs brief-read and brief-ack
    assert "never evicted" in lowered  # owner key files
    assert "reason codes, not" in lowered  # brief last prints codes, not sentences
    # Docs never name a principal ID or a private path.
    assert "principal_id" not in text
    assert "/Users/" not in text
