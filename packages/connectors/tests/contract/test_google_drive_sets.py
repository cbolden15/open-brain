from __future__ import annotations

import pytest

from open_brain_connectors.runtime.google_drive_sets import (
    DriveKnownEntry,
    DriveSetCheckpoint,
    DriveSetSelection,
    advance_watermark,
    build_list_query,
    escape_query_value,
    evict_known,
    parse_drive_set,
    part_title,
    query_bound,
    require_file_id,
    split_document,
)
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


def _folder_selection(folder_id: str = "folder-1") -> SourceResourceSelection:
    return SourceResourceSelection(
        "google_drive", "account:abc", f"drive_folder:{folder_id}", "drive_folder"
    )


def _account_selection(resource_id: str = "drive_account:all") -> SourceResourceSelection:
    return SourceResourceSelection("google_drive", "account:abc", resource_id, "drive_account")


def _meet_options(date_floor: str = "2026-10-01") -> dict[str, object]:
    return {"date_floor": date_floor, "title_suffixes": ["Notes by Gemini", "Transcript"]}


def test_parse_drive_set_accepts_folder_and_account_shapes() -> None:
    folder = parse_drive_set(_folder_selection(), {"date_floor": "2026-10-01"})
    assert folder == DriveSetSelection("folder", "folder-1", "2026-10-01", ())
    assert folder.matches_name("Any document")

    account = parse_drive_set(
        _account_selection(),
        {"date_floor": "2026-10-01", "title_suffixes": [" Notes by Gemini ", "Transcript"]},
    )
    assert account == DriveSetSelection(
        "account", None, "2026-10-01", ("Notes by Gemini", "Transcript")
    )
    assert account.matches_name("Standup - Notes by Gemini")
    assert account.matches_name("Standup - Transcript")
    assert not account.matches_name("Standup - Notes by Gemini (copy)")
    assert not account.matches_name("Transcript of nothing")
    assert require_file_id("1aB_c-9", "drive_invalid_selection") == "1aB_c-9"


def test_parse_drive_set_rejects_mismatched_ids() -> None:
    bad_selections = (
        SourceResourceSelection("google_drive", "account:abc", "drive_file:f-1", "drive_folder"),
        SourceResourceSelection("google_drive", "account:abc", "drive_folder:", "drive_folder"),
        SourceResourceSelection("google_drive", "account:abc", "drive_folder:a.b", "drive_folder"),
        _account_selection("drive_account:mine"),
        _account_selection("drive_folder:folder-1"),
        SourceResourceSelection("google_drive", "account:abc", "drive_file:f-1", "drive_file"),
        SourceResourceSelection("gmail", "account:abc", "drive_folder:folder-1", "drive_folder"),
    )
    for selection in bad_selections:
        with pytest.raises(LiveSourceError, match="drive_invalid_selection"):
            parse_drive_set(selection, _meet_options())
    with pytest.raises(LiveSourceError, match="drive_invalid_selection"):
        require_file_id("a.b", "drive_invalid_selection")
    with pytest.raises(LiveSourceError, match="google_invalid_response"):
        require_file_id(7, "google_invalid_response")


def test_parse_drive_set_rejects_bad_options() -> None:
    bad_folder_options: tuple[dict[str, object], ...] = (
        {},
        {"date_floor": "2026-13-01"},
        {"date_floor": 20261001},
        {"date_floor": "2026-10-01", "extra": 1},
        {"date_floor": "2026-10-01", "title_suffixes": []},
        {"date_floor": "2026-10-01", "title_suffixes": [f"s{index}" for index in range(9)]},
        {"date_floor": "2026-10-01", "title_suffixes": [" "]},
        {"date_floor": "2026-10-01", "title_suffixes": ["x" * 65]},
        {"date_floor": "2026-10-01", "title_suffixes": ["Transcript", "Transcript "]},
        {"date_floor": "2026-10-01", "title_suffixes": ["Tran\x00script"]},
        {"date_floor": "2026-10-01", "title_suffixes": "Transcript"},
        {"date_floor": "2026-10-01", "title_suffixes": [1]},
    )
    for options in bad_folder_options:
        with pytest.raises(LiveSourceError, match="drive_invalid_options"):
            parse_drive_set(_folder_selection(), options)
    with pytest.raises(LiveSourceError, match="drive_invalid_options"):
        parse_drive_set(_account_selection(), {"date_floor": "2026-10-01"})


def _checkpoint_value() -> dict[str, object]:
    return {
        "known": {"f1": {"modified": "2026-10-05T10:00:00Z", "revision": "r1"}},
        "mode": "incremental",
        "page_token": "p2",
        "watermark": "2026-10-05T10:00:00Z",
    }


def test_drive_set_checkpoint_initial_roundtrip_bound_and_watermark() -> None:
    initial = DriveSetCheckpoint.initial("2026-10-01")
    assert initial.to_dict() == {
        "known": {},
        "mode": "backfill",
        "page_token": None,
        "watermark": "2026-10-01T00:00:00Z",
    }
    assert query_bound(initial) == "2026-10-01T00:00:00Z"

    parsed = DriveSetCheckpoint.from_value(_checkpoint_value())
    assert parsed.mode == "incremental" and parsed.page_token == "p2"
    assert parsed.known == {"f1": DriveKnownEntry("r1", "2026-10-05T10:00:00Z")}
    assert parsed.to_dict() == _checkpoint_value()
    assert query_bound(parsed) == "2026-10-04T10:00:00Z"

    newer = {
        "a": DriveKnownEntry("r", "2026-10-06T00:00:00Z"),
        "b": DriveKnownEntry("r", "2026-10-01T00:00:00Z"),
    }
    assert advance_watermark("2026-10-05T10:00:00Z", newer) == "2026-10-06T00:00:00Z"
    assert advance_watermark("2026-10-05T10:00:00Z", {}) == "2026-10-05T10:00:00Z"
    older = {"b": DriveKnownEntry("r", "2026-10-01T00:00:00Z")}
    assert advance_watermark("2026-10-05T10:00:00Z", older) == "2026-10-05T10:00:00Z"


def test_drive_set_checkpoint_rejects_bad_shapes_and_evicts_oldest() -> None:
    good = _checkpoint_value()
    entry = {"modified": "2026-10-05T10:00:00Z", "revision": "r"}
    bad_values: tuple[object, ...] = (
        [],
        {**good, "extra": 1},
        {key: value for key, value in good.items() if key != "page_token"},
        {**good, "mode": "full"},
        {**good, "page_token": 3},
        {**good, "watermark": "2026-10-05"},
        {**good, "watermark": "2026-10-05T10:00:00+02:00"},
        {**good, "watermark": "2026-10-05T10:00:00"},
        {**good, "known": []},
        {**good, "known": {"f1": {"revision": "r1"}}},
        {**good, "known": {"f1": {"modified": "yesterday", "revision": "r1"}}},
        {**good, "known": {"f.1": entry}},
        {**good, "known": {f"f{index}": entry for index in range(2_001)}},
    )
    for value in bad_values:
        with pytest.raises(LiveSourceError, match="drive_invalid_checkpoint"):
            DriveSetCheckpoint.from_value(value)
    DriveSetCheckpoint.from_value({**good, "known": {f"f{index}": entry for index in range(2_000)}})

    known = {
        f"k{index}": DriveKnownEntry("r", f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z")
        for index in range(2_001)
    }
    evicted = evict_known(known)
    assert len(evicted) == 2_000
    assert "k0" not in evicted and "k1" in evicted and "k2000" in evicted
    assert evict_known({"only": DriveKnownEntry("r", "2026-01-01T00:00:00Z")}) == {
        "only": DriveKnownEntry("r", "2026-01-01T00:00:00Z")
    }


_DOC_CLAUSE = (
    "mimeType = 'application/vnd.google-apps.document' and trashed = false "
    "and modifiedTime > '2026-10-01T00:00:00Z'"
)


def test_build_list_query_for_each_shape_with_escaping() -> None:
    folder_plain = parse_drive_set(_folder_selection(), {"date_floor": "2026-10-01"})
    assert build_list_query(folder_plain, "2026-10-01T00:00:00Z") == (
        f"{_DOC_CLAUSE} and 'folder-1' in parents"
    )

    folder_meet = parse_drive_set(_folder_selection(), _meet_options())
    assert build_list_query(folder_meet, "2026-10-01T00:00:00Z") == (
        f"{_DOC_CLAUSE} and 'folder-1' in parents "
        "and (name contains 'Notes by Gemini' or name contains 'Transcript')"
    )

    account = parse_drive_set(_account_selection(), _meet_options())
    assert build_list_query(account, "2026-10-01T00:00:00Z") == (
        f"{_DOC_CLAUSE} and (name contains 'Notes by Gemini' or name contains 'Transcript')"
    )

    quoted = parse_drive_set(
        _account_selection(),
        {"date_floor": "2026-10-01", "title_suffixes": ["O'Brien \\ notes"]},
    )
    assert build_list_query(quoted, "2026-10-01T00:00:00Z") == (
        f"{_DOC_CLAUSE} and (name contains 'O\\'Brien \\\\ notes')"
    )
    assert escape_query_value("a'b\\c") == "a\\'b\\\\c"
    assert escape_query_value("plain") == "plain"


def test_split_document_cuts_at_blank_line_newline_then_hard_cut() -> None:
    short = "Summary\n\nOne paragraph.\n"
    assert split_document(short) == [short]
    assert split_document("a" * 60_000) == ["a" * 60_000]

    blank = "a" * 59_990 + "\n\n" + "b" * 100
    assert split_document(blank) == ["a" * 59_990, "b" * 100]

    runs = "a" * 59_990 + "\n\n\n\n" + "b" * 100
    assert split_document(runs) == ["a" * 59_990, "b" * 100]

    newline = "a" * 59_990 + "\n" + "b" * 100
    assert split_document(newline) == ["a" * 59_990, "b" * 100]

    assert split_document("a" * 60_010) == ["a" * 60_000, "a" * 10]

    crlf = "a" * 59_990 + "\r\n\r\n" + "b" * 100
    assert split_document(crlf) == ["a" * 59_990, "b" * 100]

    three = "\n\n".join(["x" * 40_000] * 3)
    assert split_document(three) == ["x" * 40_000] * 3


def test_split_document_caps_parts_and_part_titles_keep_the_suffix() -> None:
    assert len(split_document("\n\n".join(["x" * 40_000] * 18))) == 18
    with pytest.raises(LiveSourceError, match="drive_content_too_large"):
        split_document("\n\n".join(["x" * 40_000] * 19))

    assert part_title("Standup - Transcript", 2, 3) == "Standup - Transcript (part 2 of 3)"
    long_title = part_title("n" * 200, 12, 18)
    assert len(long_title) == 200
    assert long_title.endswith(" (part 12 of 18)")
    assert long_title.startswith("n" * 184)
