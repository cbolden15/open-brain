from __future__ import annotations

import pytest

from open_brain_connectors.runtime.google_drive_sets import (
    DriveSetSelection,
    parse_drive_set,
    require_file_id,
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
