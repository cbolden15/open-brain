"""Pure daily-brief contracts: closed enums, wire shapes, limits, ref parsing."""

from __future__ import annotations

import json
from typing import cast

import pytest

from open_brain.services.brief_contracts import (
    BRIEF_ERROR_CODES,
    BRIEF_FOLLOWUP_SCAN_LIMIT,
    BRIEF_GRANT_ACK,
    BRIEF_GRANT_READ,
    BRIEF_GRANTS,
    BRIEF_LOG_MAX_BRIEFS,
    BRIEF_LOG_MAX_FILE_BYTES,
    BRIEF_LOG_MAX_KEYS,
    BRIEF_MAX_FAMILY_ITEMS,
    BRIEF_MAX_ITEMS,
    BRIEF_RESPONSE_MAX_BYTES,
    BRIEF_STALE_DAYS_DEFAULT,
    BRIEF_STALE_DAYS_MAX,
    BRIEF_STALE_DAYS_MIN,
    Brief,
    BriefChange,
    BriefCoverage,
    BriefError,
    BriefItem,
    BriefItemKind,
    BriefLogCoverage,
    BriefReasonCode,
    BriefScope,
    parse_family_ref,
    parse_followup_ref,
)

FOLLOWUP = "followup:followup_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
FAMILY = "family:markdown:space_455d0d63-a720-4b09-b15d-aa741c598abd"
EVIDENCE = "evidence_8d87546c-3008-42ee-8632-0f2401904b35"


def _item(**overrides: object) -> BriefItem:
    fields: dict[str, object] = {
        "kind": BriefItemKind.FOLLOWUP_DUE,
        "ref": FOLLOWUP,
        "reasons": (BriefReasonCode.OVERDUE,),
        "reason_text": "Due 2026-10-20, still open.",
        "evidence": (EVIDENCE,),
        "changed": BriefChange.NEW,
        "rank": 1,
    }
    fields.update(overrides)
    return BriefItem(**fields)  # type: ignore[arg-type]


def _family_item(rank: int = 2, **overrides: object) -> BriefItem:
    fields: dict[str, object] = {
        "kind": BriefItemKind.SOURCE_STALE,
        "ref": FAMILY,
        "reasons": (BriefReasonCode.NO_CAPTURE_SINCE,),
        "reason_text": "No capture in 9 days.",
        "evidence": (),
        "changed": BriefChange.STILL_OPEN,
        "rank": rank,
    }
    fields.update(overrides)
    return BriefItem(**fields)  # type: ignore[arg-type]


def _coverage(**overrides: object) -> BriefCoverage:
    fields: dict[str, object] = {
        "families_checked": 3,
        "families_stale": 1,
        "captures_unavailable": 0,
        "followups_scanned": 12,
        "followups_truncated": False,
        "followups": "read",
        "freshness": "read",
        "collector": "not_read",
        "hidden_dropped": 0,
        "log": BriefLogCoverage(entries=2, bytes=1536, unavailable=False),
    }
    fields.update(overrides)
    return BriefCoverage(**fields)  # type: ignore[arg-type]


def _brief(*items: BriefItem, **overrides: object) -> Brief:
    fields: dict[str, object] = {
        "generated_at": "2026-10-24T09:00:00Z",
        "scope": BriefScope(space_ids=None, tiers=("work",)),
        "items": items,
        "nothing_needs_attention": not items,
        "since": None,
        "coverage": _coverage(),
        "model": "none",
        "saved": True,
    }
    fields.update(overrides)
    return Brief(**fields)  # type: ignore[arg-type]


def test_limits_are_the_spec_bounds() -> None:
    assert (BRIEF_MAX_ITEMS, BRIEF_MAX_FAMILY_ITEMS, BRIEF_FOLLOWUP_SCAN_LIMIT) == (5, 2, 500)
    assert BRIEF_RESPONSE_MAX_BYTES == 32 * 1024
    assert (BRIEF_LOG_MAX_BRIEFS, BRIEF_LOG_MAX_FILE_BYTES, BRIEF_LOG_MAX_KEYS) == (
        30,
        64 * 1024,
        16,
    )
    assert (BRIEF_STALE_DAYS_DEFAULT, BRIEF_STALE_DAYS_MIN, BRIEF_STALE_DAYS_MAX) == (7, 1, 365)
    assert (BRIEF_GRANT_READ, BRIEF_GRANT_ACK) == ("brief-read", "brief-ack")
    assert frozenset({"brief-read", "brief-ack"}) == BRIEF_GRANTS
    assert (
        frozenset(
            {
                "invalid_request",
                "unsupported_capability",
                "operation_pending",
                "version_conflict",
                "not_found",
            }
        )
        == BRIEF_ERROR_CODES
    )


def test_enums_are_the_registered_values() -> None:
    assert [kind.value for kind in BriefItemKind] == [
        "followup_due",
        "followup_new_evidence",
        "followup_open",
        "source_unavailable",
        "source_stale",
    ]
    assert [change.value for change in BriefChange] == ["new", "still_open", "changed"]
    assert [code.value for code in BriefReasonCode] == [
        "overdue",
        "due_today",
        "new_evidence",
        "open_owned",
        "captures_unavailable",
        "no_capture_since",
    ]


def test_item_wire_shape_matches_the_spec_table() -> None:
    wire = _item().to_wire()
    assert list(wire) == ["kind", "ref", "reasons", "reason_text", "evidence", "changed", "rank"]
    assert wire == {
        "kind": "followup_due",
        "ref": FOLLOWUP,
        "reasons": ["overdue"],
        "reason_text": "Due 2026-10-20, still open.",
        "evidence": [EVIDENCE],
        "changed": "new",
        "rank": 1,
    }
    assert _family_item().to_wire()["evidence"] == []


def test_coverage_and_brief_wire_shapes_match_the_spec_tables() -> None:
    brief = _brief(_item(), _family_item())
    wire = brief.to_wire()
    assert list(wire) == [
        "generated_at",
        "scope",
        "items",
        "nothing_needs_attention",
        "since",
        "coverage",
        "model",
        "saved",
    ]
    assert wire["scope"] == {"space_ids": "all", "tiers": ["work"]}
    assert wire["model"] == "none"
    assert wire["nothing_needs_attention"] is False
    assert wire["since"] is None
    items = cast(list[dict[str, object]], wire["items"])
    assert [item["rank"] for item in items] == [1, 2]
    coverage = cast(dict[str, object], wire["coverage"])
    assert list(coverage) == [
        "families_checked",
        "families_stale",
        "captures_unavailable",
        "followups_scanned",
        "followups_truncated",
        "followups",
        "freshness",
        "collector",
        "hidden_dropped",
        "log",
    ]
    assert coverage["log"] == {"entries": 2, "bytes": 1536, "unavailable": False}
    assert json.loads(json.dumps(wire, sort_keys=True)) == wire
    selected = BriefScope(space_ids=("space_b", "space_a"), tiers=("work", "personal"))
    assert selected.to_wire() == {
        "space_ids": ["space_a", "space_b"],
        "tiers": ["personal", "work"],
    }


def test_empty_brief_says_nothing_needs_attention() -> None:
    empty = _brief()
    assert empty.nothing_needs_attention is True
    assert empty.to_wire()["items"] == []
    with pytest.raises(ValueError):
        _brief(nothing_needs_attention=True, items=(_item(),))
    with pytest.raises(ValueError):
        _brief(nothing_needs_attention=False)


def test_item_invariants() -> None:
    for bad in (
        {"rank": 0},
        {"rank": 6},
        {"rank": True},
        {"reasons": ()},
        {"reasons": (BriefReasonCode.OVERDUE, BriefReasonCode.OVERDUE)},
        {"reasons": ("overdue",)},
        {"reason_text": ""},
        {"reason_text": "x" * 201},
        {"reason_text": "two\nlines"},
        {"evidence": ("",)},
        {"evidence": [EVIDENCE]},
        {"ref": FAMILY},
        {"ref": "followup:not-an-id"},
        {"kind": "followup_due"},
        {"changed": "new"},
    ):
        with pytest.raises(ValueError):
            _item(**bad)
    with pytest.raises(ValueError):
        _family_item(evidence=(EVIDENCE,))
    with pytest.raises(ValueError):
        _family_item(ref=FOLLOWUP)
    with pytest.raises(ValueError):
        _family_item(ref="family:")


def test_brief_invariants() -> None:
    with pytest.raises(ValueError):
        _brief(_item(), _family_item(rank=3))
    with pytest.raises(ValueError):
        _brief(_item(), _item(rank=2))
    with pytest.raises(ValueError):
        _brief(
            _item(),
            _family_item(),
            _family_item(rank=3),
            _family_item(rank=4),
            _family_item(rank=5),
            _family_item(rank=6),
        )
    with pytest.raises(ValueError):
        _brief(model="gpt")
    with pytest.raises(ValueError):
        _brief(generated_at="")
    with pytest.raises(ValueError):
        _coverage(followups="maybe")
    with pytest.raises(ValueError):
        _coverage(hidden_dropped=-1)
    with pytest.raises(ValueError):
        _coverage(collector="read")
    with pytest.raises(ValueError):
        BriefLogCoverage(entries=0, bytes=-1, unavailable=False)
    with pytest.raises(ValueError):
        BriefScope(space_ids=(), tiers=("work",))
    with pytest.raises(ValueError):
        BriefScope(space_ids=None, tiers=("secret",))


def test_ref_parsing_is_closed() -> None:
    assert parse_family_ref(FAMILY) == "markdown:space_455d0d63-a720-4b09-b15d-aa741c598abd"
    assert parse_family_ref("family:consumer:consumer_chat/claude:unassigned") == (
        "consumer:consumer_chat/claude:unassigned"
    )
    assert parse_followup_ref(FOLLOWUP) == "followup_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
    bad_family: tuple[object, ...] = (
        "family:",
        "family",
        FOLLOWUP,
        "family:other:x",
        "family:markdown:a\nb",
        7,
    )
    for ref in bad_family:
        with pytest.raises(BriefError) as caught:
            parse_family_ref(ref)
        assert caught.value.code == "invalid_request"
    bad_followup: tuple[object, ...] = (
        "followup:",
        FAMILY,
        "followup:followup_x",
        "followup:followup_" + "0" * 36,
        None,
    )
    for ref in bad_followup:
        with pytest.raises(BriefError) as caught:
            parse_followup_ref(ref)
        assert caught.value.code == "invalid_request"


def test_brief_error_codes_are_closed() -> None:
    assert BriefError("not_found").code == "not_found"
    assert str(BriefError("version_conflict")) == "version_conflict"
    with pytest.raises(ValueError):
        BriefError("duplicate_item")
