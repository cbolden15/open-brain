"""One safe terminal renderer for untrusted guide names, bodies and health."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import cast

__all__ = ["GuideDisplayError", "render_body", "render_health", "safe_display"]

# Joiners are required by scripts and emoji sequences; every other format control
# can reorder, hide or spoof visible text and is escaped.
_KEPT_FORMAT = frozenset({"‌", "‍"})
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class GuideDisplayError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _escape(character: str) -> str:
    point = ord(character)
    if point <= 0xFF:
        return f"\\x{point:02x}"
    if point <= 0xFFFF:
        return f"\\u{point:04x}"
    return f"\\U{point:08x}"


def safe_display(text: str) -> str:
    """Return visible text with controls escaped; newlines and readable text are kept.

    Escaping is display-only: callers keep the original string for digests,
    engine reads and approved copies.
    """
    # Doubling a literal backslash keeps source text distinct from an escaped control.
    return "".join(
        "\\\\"
        if character == "\\"
        else _escape(character)
        if character != "\n"
        and character not in _KEPT_FORMAT
        and unicodedata.category(character) in _ESCAPED_CATEGORIES
        else character
        for character in text
    )


def render_body(body: str, *, limit_bytes: int) -> str:
    """Render a complete body or refuse; truncated text is never shown as inspected."""
    rendered = safe_display(body)
    if len(rendered.encode("utf-8", "surrogatepass")) > limit_bytes:
        raise GuideDisplayError("capacity_reached")
    return rendered


def _value(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return safe_display(str(value))


def _codes(values: object) -> str:
    items = [safe_display(str(item)) for item in cast(list[object], values)]
    return ", ".join(items) if items else "none"


def render_health(report: Mapping[str, object]) -> str:
    """Human HealthV1 view; separate fields, never one overall healthy claim."""
    snapshot = cast(Mapping[str, object], report["snapshot"])
    account = cast(Mapping[str, object], report["account"])
    source = cast(Mapping[str, object], report["source"])
    pending = cast(Mapping[str, object], report["pending_preview"])
    execution = cast(Mapping[str, object], report["execution"])
    coverage = cast(Mapping[str, object], report["coverage"])
    recovery = cast(Mapping[str, object], report["recovery"])
    custody = cast(Mapping[str, object], report["custody"])
    counts = custody["counts"]
    custody_counts = (
        "unknown"
        if counts is None
        else ", ".join(
            f"{name} {_value(count)}" for name, count in cast(Mapping[str, object], counts).items()
        )
    )
    lines = [
        "Source health (recorded per-store evidence)",
        f"Snapshot: {_value(snapshot['consistency'])}",
        f"Global consistency: {_value(snapshot['global_consistency'])}",
        f"Reasons: {_codes(snapshot['reason_codes'])}",
        f"Account: {_value(account['registration'])}",
        f"Account name: {_value(account['display_name'])}",
        f"Credential status: {_value(account['credential_status'])}",
        f"Last access failure: {_value(account['last_access_failure_code'])}"
        f" at {_value(account['last_access_failure_epoch'])}",
        f"Source: {_value(source['source_id'])}",
        f"Configured status: {_value(source['configured_status'])}",
        f"Activity: {_value(source['activity'])}",
        f"Schedule running: {_value(source['schedule_running'])}",
        f"Stored next run: {_value(source['stored_next_run_epoch'])}",
        f"Pending preview: {_value(pending['status'])}"
        f" (records {_value(pending['count'])}, more {_value(pending['has_more'])},"
        f" coverage {_codes(pending['coverage_codes'])})",
        f"Last import: {_value(execution['outcome'])} at {_value(execution['finished_epoch'])}",
        f"Captured: {_value(execution['captured_count'])}",
        f"Duplicates: {_value(execution['duplicate_count'])}",
        f"Quarantined: {_value(execution['quarantined_count'])}",
        "Last successful completion: "
        + _value(execution["last_successful_completion_epoch"]),
        f"Failure: {_value(execution['failure_code'])}",
        f"Coverage: {_value(coverage['status'])} ({_codes(coverage['codes'])})",
        "Retry deadline: unknown"
        if recovery["retry_deadline_proven"] is not True
        else f"Retry deadline: {_value(recovery['retry_deadline_epoch'])}",
        f"Unresolved receipts: {_value(recovery['unresolved_receipt_count'])}",
        f"Custody (selected source): {_value(custody['status'])}; {custody_counts}",
        "Processing backlog (global): unavailable",
        "Release: unavailable (owned by the guide workflow)",
    ]
    return "\n".join(lines) + "\n"
