from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest
from open_brain_engine.core.models import ContentKind, SourceType
from open_brain_engine.engine import ReferencePayload

from open_brain_connectors.runtime.live_common import (
    LiveSourceError,
    bounded_json,
    local_source_privacy,
)
from open_brain_connectors.runtime.saved_item import (
    MAX_ENCODED_BYTES,
    MAX_RECORD_BYTES,
    MAX_TEXT_CHARS,
    RECORD_REFUSAL_CODES,
    TRACKING_RULE_VERSION,
    TRACKING_RULES,
    TRUNCATED_MARKER,
    SavedItemRecord,
    SavedItemRefusal,
    SavedItemSourceAdapter,
    content_digest,
    encoded_intake_size,
    identity_url,
    item_key,
    parse_record,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.example.com/a?utm_source=x&UTM_Medium=y&fbclid=abc&gclid=def"
            "&ref=keep&si=keep#frag",
            "https://www.example.com/a?ref=keep&si=keep#frag",
        ),
        ("https://www.instagram.com/p/abc/?igsh=xyz&igshid=q", "https://www.instagram.com/p/abc/"),
        (
            "https://www.threads.net/@user/post/1?igshid=1&x=2",
            "https://www.threads.net/@user/post/1?x=2",
        ),
        ("https://threads.com/@u/post/2?igsh=1", "https://threads.com/@u/post/2"),
        ("https://m.youtube.com/watch?v=abc&si=xyz", "https://m.youtube.com/watch?v=abc"),
        ("https://youtu.be/abc?si=xyz", "https://youtu.be/abc"),
        ("https://open.spotify.com/track/1?si=x", "https://open.spotify.com/track/1"),
        ("https://example.com/a?igsh=1&si=2&ref=3", "https://example.com/a?igsh=1&si=2&ref=3"),
        ("https://notyoutube.com/v?si=1", "https://notyoutube.com/v?si=1"),
        ("https://Example.COM:443/a%2fb?x=%2f#Frag%2f", "https://example.com/a%2Fb?x=%2F#Frag%2F"),
    ],
)
def test_identity_applies_each_tracking_row_with_subdomains(raw: str, expected: str) -> None:
    identity, stored = identity_url(raw)
    assert identity == expected
    assert stored == expected.partition("#")[0]
    assert "#" not in stored


def test_tracking_table_is_versioned() -> None:
    assert TRACKING_RULE_VERSION == 1
    assert (
        ((), frozenset({"fbclid", "gclid"}), ("utm_",)),
        (("instagram.com", "threads.net", "threads.com"), frozenset({"igsh", "igshid"}), ()),
        (("youtube.com", "youtu.be", "spotify.com"), frozenset({"si"}), ()),
    ) == TRACKING_RULES


def test_fragment_is_identity_input_but_never_stored() -> None:
    first, first_stored = identity_url("https://app.example/#/note/123")
    second, second_stored = identity_url("https://app.example/#/note/456")
    assert first_stored == second_stored == "https://app.example/"
    assert item_key(first) != item_key(second)
    assert re.fullmatch(r"[0-9a-f]{32}", item_key(first))
    assert item_key(identity_url("https://example.com/a?utm_source=x")[0]) == item_key(
        identity_url("https://example.com/a")[0]
    )


def test_long_path_ids_and_removed_tracking_values_are_admitted() -> None:
    path_id = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2"
    assert len(path_id) == 44
    assert identity_url(f"https://example.com/{path_id}")[1] == f"https://example.com/{path_id}"
    fbclid = "IwAR2" + "x" * 50
    assert identity_url(f"https://example.com/a?fbclid={fbclid}") == (
        "https://example.com/a",
        "https://example.com/a",
    )


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/a?token=abc",
        "https://example.com/a#access_token=abc",
        "https://example.com/a?id=" + "0123456789abcdef" * 3,
    ],
)
def test_secret_shaped_query_or_fragment_is_refused(raw: str) -> None:
    with pytest.raises(SavedItemRefusal) as refusal:
        identity_url(raw)
    assert refusal.value.code == "url_secret"


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.com/a",
        "ftp://example.com/a",
        "https://user@example.com/",
        "https://",
        "not a url",
        7,
        None,
    ],
)
def test_non_https_or_malformed_urls_are_refused(raw: object) -> None:
    with pytest.raises(SavedItemRefusal) as refusal:
        identity_url(raw)
    assert refusal.value.code == "invalid_url"


@pytest.mark.parametrize(
    "raw",
    [
        "https://exam\nple.com/a",
        "https://example.com/a\rb",
        "https://example.com/a?x=\t1",
        "\x00https://example.com/a",
        "https://example.com/a#frag\x00",
    ],
)
def test_raw_url_control_characters_are_refused_before_parsing(raw: str) -> None:
    with pytest.raises(SavedItemRefusal) as refusal:
        identity_url(raw)
    assert refusal.value.code == "invalid_url"


@pytest.mark.parametrize(
    "encoded_url",
    [
        r'"https://example.com/\ud800"',
        r'"https://example.com/\udfff"',
        r'"https://example.com/a#\ud800"',
        r'"https://example.com/a#\udfff"',
    ],
)
def test_json_escaped_lone_surrogates_are_refused(encoded_url: str) -> None:
    raw = json.loads(encoded_url)
    with pytest.raises(SavedItemRefusal) as refusal:
        identity_url(raw)
    assert refusal.value.code == "invalid_url"


THIN: dict[str, object] = {
    "schema_version": 1,
    "url": "https://example.com/post/123",
    "why": "Recipe to try this weekend",
    "received_at": "2026-10-26T14:00:00Z",
    "fetch_status": "not_attempted",
}
RICH: dict[str, object] = {
    **THIN,
    "url": "https://www.instagram.com/p/abc/?igsh=xyz",
    "why": "  Compare with the other recipe  ",
    "received_at": "2026-10-26T14:00:00.123456789Z",
    "fetch_status": "complete",
    "source_type": "social",
    "content_kind": "post",
    "platform": "instagram",
    "title": "Pasta night",
    "author": "Ada",
    "body": "Full caption text.",
    "media_text": "Transcript words.",
    "shared_text": "Shared by a friend.",
    "summary": "Short summary.",
    "summary_origin": "producer:shortcut",
}


def _raw(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _refusal(raw: bytes) -> str:
    with pytest.raises(SavedItemRefusal) as refusal:
        parse_record(raw)
    return refusal.value.code


def test_thin_record_maps_required_fields_and_defaults() -> None:
    assert parse_record(_raw(THIN)) == SavedItemRecord(
        identity_url="https://example.com/post/123",
        stored_url="https://example.com/post/123",
        item_key=item_key("https://example.com/post/123"),
        why="Recipe to try this weekend",
        received_at=datetime(2026, 10, 26, 14, 0, tzinfo=UTC),
        fetch_status="not_attempted",
        source_type=SourceType.WEB,
        content_kind=ContentKind.OTHER,
        platform=None,
        title=None,
        author=None,
        body="",
        media_text="",
        shared_text="",
        summary=None,
        summary_origin=None,
    )


def test_rich_record_keeps_every_field() -> None:
    record = parse_record(_raw(RICH))
    assert record.stored_url == "https://www.instagram.com/p/abc/"
    assert record.why == "Compare with the other recipe"
    assert record.received_at == datetime(2026, 10, 26, 14, 0, 0, 123456, tzinfo=UTC)
    assert (record.source_type, record.content_kind, record.platform) == (
        SourceType.SOCIAL,
        ContentKind.POST,
        "instagram",
    )
    assert (record.title, record.author) == ("Pasta night", "Ada")
    assert (record.body, record.media_text, record.shared_text) == (
        "Full caption text.",
        "Transcript words.",
        "Shared by a friend.",
    )
    assert (record.summary, record.summary_origin) == ("Short summary.", "producer:shortcut")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=a", SourceType.YOUTUBE),
        ("https://youtu.be/a", SourceType.YOUTUBE),
        ("https://x.com/u/status/1", SourceType.SOCIAL),
        ("https://www.instagram.com/p/a/", SourceType.SOCIAL),
        ("https://example.com/a", SourceType.WEB),
    ],
)
def test_source_type_is_derived_from_the_host_when_null(url: str, expected: SourceType) -> None:
    record = parse_record(_raw({**THIN, "url": url, "source_type": None}))
    assert record.source_type is expected


def test_refusal_codes_are_the_documented_set() -> None:
    assert frozenset(
        {
            "file_too_large",
            "invalid_field",
            "invalid_json",
            "invalid_url",
            "invalid_why",
            "missing_field",
            "payload_too_large",
            "unknown_field",
            "url_secret",
        }
    ) == RECORD_REFUSAL_CODES


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{", "invalid_json"),
        (b"[]", "invalid_json"),
        (b"\xff\xfe", "invalid_json"),
        (b'{"schema_version": 1, "schema_version": 1}', "invalid_json"),
        (b'{"schema_version": NaN}', "invalid_json"),
        (_raw({**THIN, "extra": 1}), "unknown_field"),
        (b"x" * (MAX_RECORD_BYTES + 1), "file_too_large"),
        (_raw({**THIN, "url": "http://example.com/a"}), "invalid_url"),
        (_raw({**THIN, "url": "https://example.com/a?token=abc"}), "url_secret"),
    ],
)
def test_malformed_records_are_refused_with_named_codes(raw: bytes, code: str) -> None:
    assert _refusal(raw) == code


@pytest.mark.parametrize("field", ["schema_version", "url", "why", "received_at", "fetch_status"])
def test_each_required_field_is_required(field: str) -> None:
    assert _refusal(_raw({key: value for key, value in THIN.items() if key != field})) == (
        "missing_field"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": "1"},
        {"fetch_status": "done"},
        {"received_at": "2026-10-26 14:00:00Z"},
        {"received_at": "2026-10-26T14:00:00+01:00"},
        {"received_at": "2026-02-30T00:00:00Z"},
        {"source_type": "blog"},
        {"content_kind": "story"},
        {"platform": "Instagram"},
        {"platform": "x" * 41},
        {"title": "t" * 301},
        {"author": "a" * 201},
        {"body": 5},
        {"body": "nul\x00byte"},
        {"summary": "Only a summary."},
        {"summary_origin": "producer:shortcut"},
        {"summary": "Summary.", "summary_origin": "shortcut"},
    ],
)
def test_invalid_values_are_refused_as_invalid_field(change: dict[str, object]) -> None:
    assert _refusal(_raw({**THIN, **change})) == "invalid_field"


@pytest.mark.parametrize(
    "why", ["", "   ", "\t", "\t \t", "line one\nline two", "a b", "x" * 281, 7, None]
)
def test_blank_multiline_or_long_why_is_refused(why: object) -> None:
    assert _refusal(_raw({**THIN, "why": why})) == "invalid_why"


def test_why_is_trimmed_and_may_be_280_characters() -> None:
    assert parse_record(_raw({**THIN, "why": " " + "x" * 280 + " "})).why == "x" * 280


@pytest.mark.parametrize("status", [[], {}])
def test_unhashable_fetch_status_is_refused(status: object) -> None:
    assert _refusal(_raw({**THIN, "fetch_status": status})) == "invalid_field"


@pytest.mark.parametrize(
    "field",
    [
        "schema_version", "url", "why", "received_at", "fetch_status", "source_type",
        "content_kind", "platform", "title", "author", "body", "media_text", "shared_text",
        "summary", "summary_origin",
    ],
)
def test_nul_in_string_fields_is_refused(field: str) -> None:
    values = {**THIN, field: "nul\x00byte"}
    if field in {"summary", "summary_origin"}:
        values.update(
            {"summary": "valid", "summary_origin": "producer:valid", field: "nul\x00byte"}
        )
    expected = {"url": "invalid_url", "why": "invalid_why"}.get(field, "invalid_field")
    assert _refusal(_raw(values)) == expected


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high", "low"])
@pytest.mark.parametrize(
    "field", ["why", "title", "author", "body", "media_text", "shared_text", "summary"]
)
def test_decoded_lone_surrogates_are_refused(field: str, surrogate: str) -> None:
    text = f"Synthetic {surrogate} text"
    # ASCII escapes reach json.loads; the UTF-8 fixture helper cannot encode these values.
    raw = json.dumps({**RICH, field: text}, ensure_ascii=True).encode("ascii")
    assert json.loads(raw)[field] == text
    assert _refusal(raw) == ("invalid_why" if field == "why" else "invalid_field")


@pytest.mark.parametrize("escaped", [False, True], ids=["utf8", "paired-escapes"])
def test_valid_unicode_text_and_paired_surrogate_escapes_are_admitted(escaped: bool) -> None:
    text = "Caf\u00e9 \u6771 \U0001f9ed"
    fields = ("why", "title", "author", "body", "media_text", "shared_text", "summary")
    raw = json.dumps({**RICH, **dict.fromkeys(fields, text)}, ensure_ascii=escaped).encode("utf-8")
    if escaped:
        assert b"\\ud83e\\udded" in raw
    else:
        assert text.encode("utf-8") in raw
    record = parse_record(raw)
    assert all(getattr(record, field) == text for field in fields)
    intake = SavedItemSourceAdapter(SELECTION).intake(record)
    assert intake.title == text
    assert intake.text.count(text) == 5
    intake.text.encode("utf-8")


SELECTION = SourceResourceSelection(
    "saved_item", "folder:" + "a" * 32, "items:" + "b" * 32, "local_folder"
)


def _intake(value: dict[str, object]) -> SourceRecordIntake:
    adapter = SavedItemSourceAdapter(SELECTION)
    return adapter.intake(adapter.record_from_bytes(_raw(value)))


def _encoded(intake: SourceRecordIntake) -> int:
    payload = ReferencePayload(url=intake.url, supplied_text=intake.text).to_dict()
    return len(bounded_json(payload, 1_048_576))


def test_thin_record_intake_layout_and_key() -> None:
    intake = _intake(THIN)
    assert intake.text == (
        "Stated reason\nRecipe to try this weekend\n\n"
        "Kind\nsource_type: web; content_kind: other\n\n"
        "Fetch status\nnot_attempted"
    )
    assert intake.title == "https://example.com/post/123"
    assert intake.url == "https://example.com/post/123"
    assert intake.privacy == local_source_privacy()
    assert intake.key == SourceRecordKey(
        "saved_item",
        SELECTION.connection_id,
        SELECTION.resource_id,
        item_key("https://example.com/post/123"),
        "1",
    )


def test_rich_record_intake_layout_in_section_order() -> None:
    intake = _intake(RICH)
    assert intake.text == (
        "Stated reason\nCompare with the other recipe\n\n"
        "Summary (producer:shortcut)\nShort summary.\n\n"
        "Body\nFull caption text.\n\n"
        "Media text\nTranscript words.\n\n"
        "Shared text\nShared by a friend.\n\n"
        "Kind\nsource_type: social; content_kind: post; platform: instagram\n\n"
        "Fetch status\ncomplete"
    )
    assert intake.title == "Pasta night"
    assert intake.url == "https://www.instagram.com/p/abc/"


def test_title_falls_back_to_author_on_platform_then_url_and_caps_at_200() -> None:
    no_title = {key: value for key, value in RICH.items() if key != "title"}
    assert _intake(no_title).title == "Ada on instagram"
    no_platform = {key: value for key, value in no_title.items() if key != "platform"}
    assert _intake(no_platform).title == "https://www.instagram.com/p/abc/"
    long_title = _intake({**THIN, "title": "Long title " * 27})
    assert long_title.title == ("Long title " * 27)[:200]
    assert len(long_title.title or "") == 200


def test_fragment_variants_share_the_stored_url_but_not_the_item() -> None:
    first = _intake({**THIN, "url": "https://app.example/#/note/123"})
    second = _intake({**THIN, "url": "https://app.example/#/note/456"})
    assert first.url == second.url == "https://app.example/"
    assert first.key.external_id != second.key.external_id
    assert "#" not in first.text and "#" not in (first.title or "")


def test_long_non_latin_body_is_truncated_to_the_encoded_bound() -> None:
    raw = _raw({**THIN, "body": "東" * 30_000})
    assert len(raw) <= MAX_RECORD_BYTES
    adapter = SavedItemSourceAdapter(SELECTION)
    intake = adapter.intake(adapter.record_from_bytes(raw))
    assert _encoded(intake) <= MAX_ENCODED_BYTES
    assert len(intake.text) <= MAX_TEXT_CHARS
    body = intake.text.split("Body\n", 1)[1].split("\n\nKind\n", 1)[0]
    assert body.endswith("\n" + TRUNCATED_MARKER)
    assert 10_000 < body.count("東") < 30_000
    assert intake.text.startswith("Stated reason\nRecipe to try this weekend\n\n")
    assert encoded_intake_size(intake) <= MAX_ENCODED_BYTES + 1_024


def test_media_text_is_truncated_before_body() -> None:
    intake = _intake({**THIN, "body": "Short body.", "media_text": "語" * 30_000})
    assert "Body\nShort body.\n\n" in intake.text
    assert f"\n{TRUNCATED_MARKER}\n\nKind\n" in intake.text
    assert _encoded(intake) <= MAX_ENCODED_BYTES


def test_record_whose_reason_and_url_alone_do_not_fit_is_refused() -> None:
    adapter = SavedItemSourceAdapter(SELECTION)
    record = adapter.record_from_bytes(_raw({**THIN, "url": "https://example.com/" + "a" * 70_000}))
    with pytest.raises(SavedItemRefusal) as refusal:
        adapter.intake(record)
    assert refusal.value.code == "payload_too_large"


def test_sensitive_text_is_masked_and_the_save_lands() -> None:
    intake = _intake(
        {**THIN, "why": "Works with token=abc123", "body": "Ask bob@example.com about it"}
    )
    assert "abc123" not in intake.text and "bob@example.com" not in intake.text
    assert "Stated reason\nWorks with token=[REDACTED_CREDENTIAL]\n\n" in intake.text
    assert "Body\nAsk [REDACTED_PERSONAL_IDENTIFIER] about it\n\n" in intake.text
    path_id = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2"
    assert _intake({**THIN, "url": f"https://example.com/{path_id}"}).url.endswith(path_id)


def test_content_digest_tracks_admitted_bytes() -> None:
    first = _intake(THIN)
    assert re.fullmatch(r"[0-9a-f]{32}", content_digest(first))
    later = _intake({**THIN, "received_at": "2026-10-27T00:00:00Z"})
    assert content_digest(first) == content_digest(later)
    assert content_digest(first) != content_digest(_intake({**THIN, "why": "Another reason"}))


def test_adapter_requires_a_saved_item_selection() -> None:
    with pytest.raises(LiveSourceError, match="source_selection_mismatch"):
        SavedItemSourceAdapter(
            SourceResourceSelection("gmail", "account:x", "label:INBOX", "mail_label")
        )
