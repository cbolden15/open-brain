from __future__ import annotations

from pathlib import Path

from open_brain_engine.engine.followup_contracts import (
    MAX_EVIDENCE_PER_ITEM,
    MAX_ITEMS,
    MAX_SUMMARY_CHARS,
)

from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/followups.md"
PROPER_NOUNS = (
    "Open",
    "Brain",
    "MCP",
    "CLI",
    "Portable",
    "Work",
    "Personal",
    "SQLite",
    "Gmail",
    "Drive",
    "Slack",
    "Claude",
    "Codex",
    "Markdown",
    "Jira",
)


def test_followups_guide_names_every_tool_grant_command_code_and_limit() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    tools = sorted(
        str(tool["name"])
        for tool in MCP_REGISTERED_TOOLS
        if str(tool["name"]).startswith("brain_followup_")
    )
    assert tools == [
        "brain_followup_add_evidence",
        "brain_followup_decide",
        "brain_followup_list",
        "brain_followup_propose",
        "brain_followup_show",
    ]
    required = (
        *tools,
        "`followup-read`",
        "`followup-propose`",
        "`followup-decide`",
        "--allow-followup-read",
        "--allow-followup-propose",
        "--allow-followup-decide",
        "open-brain followup list",
        "open-brain followup show",
        "open-brain followup propose",
        "open-brain followup add-evidence",
        "open-brain followup decide",
        "`evidence_mismatch`",
        "`not_found`",
        "`tier_mismatch`",
        "`version_conflict`",
        "`operation_conflict`",
        "`duplicate_item`",
        "`invalid_request`",
        "`capacity_reached`",
        "`unsupported_capability`",
        "`operation_pending`",
        f"{MAX_SUMMARY_CHARS} characters",
        f"{MAX_ITEMS:,} items",
        f"{MAX_EVIDENCE_PER_ITEM} evidence",
        "no owner identified",
        "no date stated",
        "(quote withheld)",
        "evidence_retracted",
        "`--session-policy`",
        "portable-brain-v18.md",
    )
    missing = [token for token in required if token not in text]
    assert not missing, missing
    # No marketing tone, no title-case headings, no "not just".
    headings = [line for line in text.splitlines() if line.startswith("#")]
    for heading in headings[1:]:
        words = heading.lstrip("# ").split()
        assert words[0][0].isupper() and all(
            word == word.lower() or word.startswith(PROPER_NOUNS) for word in words[1:]
        ), heading
    lowered = text.lower()
    assert "not just" not in lowered
    assert "seamless" not in lowered
    # Docs never promise follow-up tools to external-provider sessions or name a principal ID.
    assert "principal_id" not in text
    assert "external-provider sessions get no follow-up tools" in lowered
