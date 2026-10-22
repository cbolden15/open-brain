"""Safe rendering of untrusted guide names, bodies, notices and health."""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata

import pytest

from open_brain.services.source_guide_display import (
    GuideDisplayError,
    render_body,
    render_health,
    safe_display,
)

HOSTILE = (
    "Approved?\u001b[2J\u001b]52;c;VEVTVA==\u0007‮owner‬\n"
    "Decision: keep the workspace invite-only."
)


def _dangerous(text: str) -> list[str]:
    return [
        character
        for character in text
        if character != "\n"
        and (
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            and character not in {"‌", "‍"}
        )
    ]


def test_hostile_body_is_visibly_escaped_and_source_bytes_are_unchanged() -> None:
    original = HOSTILE.encode()
    digest = hashlib.sha256(original).hexdigest()

    rendered = safe_display(HOSTILE)

    assert rendered == (
        "Approved?\\x1b[2J\\x1b]52;c;VEVTVA==\\x07\\u202eowner\\u202c\n"
        "Decision: keep the workspace invite-only."
    )
    assert _dangerous(rendered) == []
    assert HOSTILE.encode() == original
    assert hashlib.sha256(HOSTILE.encode()).hexdigest() == digest


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("\u001b[31mred", "\\x1b[31mred"),
        ("\u001b]8;;https://example.invalid\u0007link\u001b]8;;\u0007", None),
        ("\u009b31m", "\\x9b31m"),
        ("tab\there", "tab\\x09here"),
        ("cr\roverwrite", "cr\\x0doverwrite"),
        ("del\u007f", "del\\x7f"),
        ("⁦isolate⁩", "\\u2066isolate\\u2069"),
        ("‎LRM‏", "\\u200eLRM\\u200f"),
        ("line sep", "line\\u2028sep"),
        ("bom﻿", "bom\\ufeff"),
        ("tag\U000e0041", "tag\\U000e0041"),
        ("lone\ud800", "lone\\ud800"),
    ],
)
def test_terminal_controls_bidi_and_format_controls_are_escaped(
    raw: str, escaped: str | None
) -> None:
    rendered = safe_display(raw)
    assert _dangerous(rendered) == []
    if escaped is not None:
        assert rendered == escaped


def test_readable_text_newlines_combining_marks_and_joiners_are_preserved() -> None:
    text = "Café שלום\nfamily \U0001f468‍\U0001f469 क्‍"
    assert safe_display(text) == text


def test_render_body_refuses_rather_than_truncates() -> None:
    assert render_body("small body", limit_bytes=64) == "small body"
    with pytest.raises(GuideDisplayError) as error:
        render_body("x" * 65, limit_bytes=64)
    assert error.value.code == "capacity_reached"
    with pytest.raises(GuideDisplayError):
        render_body("\u001b" * 20, limit_bytes=64)


def _health() -> dict[str, object]:
    return {
        "schema_version": 1,
        "snapshot": {
            "consistency": "per_store",
            "global_consistency": "unknown",
            "retryable": False,
            "reason_codes": [],
            "evidence_tokens": [],
        },
        "account": {
            "registration": "registered",
            "connection_id": "account:" + "c" * 64,
            "display_name": "Evil\u001b]52;c;eA==\u0007 ‮workspace",
            "credential_status": "unknown",
            "last_access_failure_code": None,
            "last_access_failure_epoch": None,
        },
        "source": {
            "source_id": "slack.0123456789abcdef01234567",
            "generation": "a" * 64,
            "control_epoch": 0,
            "configured_status": "disabled",
            "activity": "manual_idle",
            "schedule_running": "unknown",
            "stored_next_run_epoch": None,
        },
        "pending_preview": {
            "status": "absent",
            "preview_id": None,
            "count": None,
            "has_more": None,
            "coverage_codes": [],
        },
        "execution": {
            "outcome": "completed",
            "finished_epoch": 1_900_000_000,
            "captured_count": 0,
            "duplicate_count": None,
            "quarantined_count": None,
            "last_successful_completion_epoch": 1_900_000_000,
            "failure_code": None,
        },
        "coverage": {"status": "partial", "has_more": True, "codes": ["partial_page"]},
        "recovery": {
            "retry_deadline_epoch": None,
            "retry_deadline_proven": False,
            "unresolved_receipt_count": None,
        },
        "custody": {
            "scope": "selected_source",
            "status": "unavailable",
            "counts": None,
            "retained_items": None,
        },
        "receipt_bindings": [],
        "processing_backlog": {
            "scope": "global",
            "status": "unavailable",
            "reason_code": "not_read_by_v1",
        },
        "release": {"status": "unavailable", "reason_code": "app_workflow_owned"},
    }


def test_health_rendering_escapes_names_and_keeps_unknown_distinct_from_zero() -> None:
    report = _health()
    before = copy.deepcopy(report)

    text = render_health(report)

    assert report == before
    assert _dangerous(text) == []
    assert "Evil\\x1b]52;c;eA==\\x07 \\u202eworkspace" in text
    assert "Global consistency: unknown" in text
    assert "Captured: 0" in text
    assert "Duplicates: unknown" in text
    assert "Custody (selected source): unavailable" in text
    assert "Credential status: unknown" in text
    assert "Retry deadline: unknown" in text
    assert "Coverage: partial (partial_page)" in text
    assert "healthy" not in text.lower()
    json.dumps(report)


def test_literal_escape_text_is_distinguishable_from_an_escaped_control() -> None:
    assert safe_display("\\x1b[31m") == "\\\\x1b[31m"
    assert safe_display("\u001b[31m") == "\\x1b[31m"
    assert safe_display("\\x1b[31m") != safe_display("\u001b[31m")
    assert safe_display("C:\\path") == "C:\\\\path"
