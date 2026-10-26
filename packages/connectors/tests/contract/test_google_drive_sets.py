from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from open_brain_connectors.runtime.google_drive_sets import (
    MAX_PART_JSON_BYTES,
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
from open_brain_connectors.runtime.google_sources import DriveSourceClient
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveResource,
    LiveSourceError,
    bounded_json,
)
from open_brain_connectors.runtime.live_http import LiveHttpResponse
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

from .test_google_sources import _auth, _Http, _json


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
    millis = DriveSetCheckpoint("incremental", None, "2026-10-05T10:00:00.123Z", {})
    assert query_bound(millis) == "2026-10-04T10:00:00.123Z"

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
        {**good, "mode": []},
        {**good, "mode": {}},
        {**good, "page_token": 3},
        {**good, "watermark": "2026-10-05"},
        {**good, "watermark": "2026-10-05T10:00:00+02:00"},
        {**good, "watermark": "2026-10-05T10:00:00"},
        {**good, "known": []},
        {**good, "known": {"f1": {"revision": "r1"}}},
        {**good, "known": {"f1": {"modified": "yesterday", "revision": "r1"}}},
        {**good, "known": {"f.1": entry}},
        {**good, "known": {f"f{index}": entry for index in range(501)}},
    )
    for value in bad_values:
        with pytest.raises(LiveSourceError, match="drive_invalid_checkpoint"):
            DriveSetCheckpoint.from_value(value)
    DriveSetCheckpoint.from_value({**good, "known": {f"f{index}": entry for index in range(500)}})

    known = {
        f"k{index}": DriveKnownEntry("r", f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z")
        for index in range(501)
    }
    evicted = evict_known(known)
    assert len(evicted) == 500
    assert "k0" not in evicted and "k1" in evicted and "k500" in evicted
    assert evict_known({"only": DriveKnownEntry("r", "2026-01-01T00:00:00Z")}) == {
        "only": DriveKnownEntry("r", "2026-01-01T00:00:00Z")
    }


# LiveBatch admits a checkpoint through bounded_json with its default `maximum`; there is no
# named module constant, so the limit is read from the helper's signature.
_CHECKPOINT_LIMIT = inspect.signature(bounded_json).parameters["maximum"].default


def test_full_known_checkpoint_with_realistic_sizes_fits_the_live_batch_limit() -> None:
    assert _CHECKPOINT_LIMIT == 131_072
    known = {
        f"{index:04d}{'aZ9_-' * 8}": DriveKnownEntry(
            hashlib.sha256(str(index).encode()).hexdigest(),
            f"2026-10-05T10:{index // 60 % 60:02d}:{index % 60:02d}.{index % 1000:03d}Z",
        )
        for index in range(500)
    }
    assert all(len(file_id) == 44 and require_file_id(file_id, "x") for file_id in known)
    checkpoint = DriveSetCheckpoint("incremental", "p" * 200, "2026-10-05T10:00:00.000Z", known)
    value = checkpoint.to_dict()
    assert DriveSetCheckpoint.from_value(value) == checkpoint

    assert len(bounded_json(value)) < _CHECKPOINT_LIMIT
    assert LiveBatch((), value).checkpoint == value


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


def test_split_document_bounds_each_part_by_its_ascii_json_size() -> None:
    # Each escaped BMP character costs six bytes, so 19,999 fit beside the two quote bytes.
    for text, sizes in (
        ("é" * 60_000, [19_999, 19_999, 19_999, 3]),
        ("日" * 22_000, [19_999, 2_001]),
    ):
        parts = split_document(text)
        assert [len(part) for part in parts] == sizes
        assert all(
            len(json.dumps(part, ensure_ascii=True)) <= MAX_PART_JSON_BYTES for part in parts
        )
        assert "".join(parts) == text

    paragraphs = "\n\n".join(["日" * 15_000] * 3)
    assert split_document(paragraphs) == ["日" * 15_000] * 3


def _client(tmp_path: Path, http: _Http) -> DriveSourceClient:
    return DriveSourceClient(_auth(tmp_path, "google_drive", http), http=http)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_resources_lists_folders_on_request_and_files_by_default(tmp_path: Path) -> None:
    http = _Http(
        [
            _json(
                200,
                {
                    "files": [
                        {
                            "id": "folder-1",
                            "mimeType": "application/vnd.google-apps.folder",
                            "name": "Meet Recordings",
                        }
                    ],
                    "nextPageToken": "n2",
                },
            ),
            _json(200, {"files": [{"id": "file-1", "name": "Synthetic doc"}]}),
        ]
    )
    client = _client(tmp_path, http)

    folders = client.resources("account:abc", resource_type="drive_folder")
    assert folders.resources == (
        LiveResource("drive_folder:folder-1", "Meet Recordings", "drive_folder"),
    )
    assert folders.next_cursor == "n2"
    folder_query = _query(http.urls[0])
    assert folder_query["q"] == [
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    ]
    assert folder_query["pageSize"] == ["100"]
    assert folder_query["includeItemsFromAllDrives"] == ["true"]
    assert folder_query["supportsAllDrives"] == ["true"]

    files = client.resources("account:abc", cursor="n2")
    assert files.resources == (LiveResource("drive_file:file-1", "Synthetic doc", "drive_file"),)
    file_query = _query(http.urls[1])
    assert file_query["q"] == ["trashed = false"] and file_query["pageToken"] == ["n2"]

    with pytest.raises(LiveSourceError, match="drive_invalid_selection"):
        client.resources("account:abc", resource_type="drive_account")
    assert len(http.urls) == 2


def test_live_batch_accepts_set_notices_with_reason_and_file_id() -> None:
    notices = (
        "drive_file_skipped:missing:1aB_c-9",
        "drive_folder_missing",
        "gmail_history_reset",
    )
    batch = LiveBatch((), {}, notices=notices)
    assert batch.notices == notices
    for notice in ("Drive:bad", "drive_file_skipped:missing:a:b:c", "drive_file_skipped::x"):
        with pytest.raises(LiveSourceError, match="source_invalid_batch"):
            LiveBatch((), {}, notices=(notice,))


_LIST_FIELDS = (
    "nextPageToken,files(id,name,mimeType,webViewLink,trashed,capabilities/canDownload,"
    "modifiedTime,version,md5Checksum,parents,createdTime)"
)


def _doc(file_id: str, name: str, modified: str, *, version: str = "1") -> dict[str, object]:
    return {
        "capabilities": {"canDownload": True},
        "id": file_id,
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": modified,
        "name": name,
        "parents": ["folder-1"],
        "trashed": False,
        "version": version,
        "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
    }


def _body(text: str) -> LiveHttpResponse:
    return LiveHttpResponse(200, {}, text.encode())


def _gemini_note() -> str:
    return (
        "Summary\n\nThe team reviewed the synthetic roadmap and agreed on two follow-ups.\n\n"
        "Details\n\n- Synthetic item one was discussed.\n- Synthetic item two was deferred.\n\n"
        "Suggested next steps\n\n- Send the synthetic summary to the group.\n"
    )


def _transcript() -> str:
    lines = (
        f"Speaker {'AB'[index % 2]}: synthetic remark number {index}." for index in range(6_000)
    )
    return "\n\n".join(lines)[:150_000]


def _known(batch_checkpoint: dict[str, object]) -> dict[str, dict[str, str]]:
    return cast(dict[str, dict[str, str]], batch_checkpoint["known"])


def test_folder_backfill_pages_once_switches_to_incremental_and_resubmits_edits(
    tmp_path: Path,
) -> None:
    note = _doc("a", "Standup - Notes by Gemini", "2026-10-02T10:00:00Z")
    transcript = _doc("b", "Standup - Transcript", "2026-10-03T10:00:00Z")
    http = _Http(
        [
            _json(200, {"files": [note, transcript], "nextPageToken": "p2"}),
            _json(200, note),
            _body(_gemini_note()),
            _json(200, transcript),
            _body("Speaker A: short transcript."),
        ]
    )
    client = _client(tmp_path, http)

    first = client.fetch(_folder_selection(), _meet_options(), None)

    listing = _query(http.urls[0])
    assert listing["q"] == [
        "mimeType = 'application/vnd.google-apps.document' and trashed = false "
        "and modifiedTime > '2026-10-01T00:00:00Z' and 'folder-1' in parents "
        "and (name contains 'Notes by Gemini' or name contains 'Transcript')"
    ]
    assert listing["orderBy"] == ["modifiedTime"] and listing["pageSize"] == ["100"]
    assert listing["fields"] == [_LIST_FIELDS]
    assert listing["includeItemsFromAllDrives"] == ["true"]
    assert "pageToken" not in listing
    assert "/files/a?" in http.urls[1] and "supportsAllDrives=true" in http.urls[1]
    assert "/files/a/export?" in http.urls[2] and "mimeType=text%2Fplain" in http.urls[2]
    assert http.responses == []

    assert [item.key.external_id for item in first.intakes] == ["file:a", "file:b"]
    assert first.intakes[0].text == _gemini_note()
    assert first.intakes[0].title == "Standup - Notes by Gemini"
    assert first.intakes[0].url == "https://drive.google.com/file/d/a/view"
    assert first.intakes[0].observation is None
    assert first.intakes[0].key.connector_name == "google_drive"
    assert first.intakes[0].key.connection_id == "account:abc"
    assert first.intakes[0].key.resource_id == "drive_folder:folder-1"
    assert first.intakes[0].key.revision_id.startswith("revision:")
    assert first.has_more is True
    assert first.notices == ()
    assert first.checkpoint["mode"] == "backfill"
    assert first.checkpoint["page_token"] == "p2"
    assert first.checkpoint["watermark"] == "2026-10-01T00:00:00Z"
    first_known = _known(first.checkpoint)
    assert set(first_known) == {"a", "b"}
    assert first_known["a"] == {
        "modified": "2026-10-02T10:00:00Z",
        "revision": first.intakes[0].key.revision_id.removeprefix("revision:"),
    }

    memo = _doc("c", "Budget memo", "2026-10-04T10:00:00Z")
    retro = _doc("d", "Retro - Transcript", "2026-10-05T09:00:00Z")
    http.responses.extend(
        [
            _json(200, {"files": [memo, retro]}),
            _json(200, retro),
            _body("Speaker B: retro transcript."),
        ]
    )
    second = client.fetch(_folder_selection(), _meet_options(), first.checkpoint)

    second_listing = _query(http.urls[5])
    assert second_listing["pageToken"] == ["p2"]
    assert second_listing["q"] == listing["q"]
    assert [item.key.external_id for item in second.intakes] == ["file:d"]
    assert second.has_more is False
    assert second.checkpoint["mode"] == "incremental"
    assert second.checkpoint["page_token"] is None
    assert second.checkpoint["watermark"] == "2026-10-05T09:00:00Z"
    assert set(_known(second.checkpoint)) == {"a", "b", "d"}

    edited = _doc("a", "Standup - Notes by Gemini", "2026-10-05T12:00:00Z", version="2")
    http.responses.extend(
        [
            _json(200, {"files": [retro, edited]}),
            _json(200, edited),
            _body("Summary\n\nUpdated after the meeting.\n"),
        ]
    )
    third = client.fetch(_folder_selection(), _meet_options(), second.checkpoint)

    third_listing = _query(http.urls[8])
    assert third_listing["q"] == [
        "mimeType = 'application/vnd.google-apps.document' and trashed = false "
        "and modifiedTime > '2026-10-04T09:00:00Z' and 'folder-1' in parents "
        "and (name contains 'Notes by Gemini' or name contains 'Transcript')"
    ]
    assert "pageToken" not in third_listing
    assert len(http.urls) == 11
    assert [item.key.external_id for item in third.intakes] == ["file:a"]
    assert third.intakes[0].text == "Summary\n\nUpdated after the meeting.\n"
    assert third.intakes[0].key.revision_id != first.intakes[0].key.revision_id
    assert third.intakes[0].key.delivery_id() == first.intakes[0].key.delivery_id()
    assert third.has_more is False
    assert third.checkpoint["mode"] == "incremental"
    assert third.checkpoint["watermark"] == "2026-10-05T12:00:00Z"
    edited_revision = third.intakes[0].key.revision_id.removeprefix("revision:")
    assert _known(third.checkpoint)["a"]["revision"] == edited_revision


def test_account_set_splits_transcript_and_repeats_page_at_the_intake_bound(
    tmp_path: Path,
) -> None:
    transcript = _transcript()
    assert len(transcript) == 150_000
    long_doc = _doc("t", "Weekly - Transcript", "2026-10-01T12:00:00Z")
    notes = [
        _doc(f"n{index}", f"Sync {index} - Notes by Gemini", f"2026-10-02T00:00:{index:02d}Z")
        for index in range(23)
    ]
    responses = [
        _json(200, {"files": [long_doc, *notes]}),
        _json(200, long_doc),
        _body(transcript),
    ]
    for index, note in enumerate(notes):
        responses.extend([_json(200, note), _body(f"Summary\n\nNote {index}.\n")])
    http = _Http(responses)
    client = _client(tmp_path, http)

    first = client.fetch(_account_selection(), _meet_options(), None)

    listing = _query(http.urls[0])
    assert listing["q"] == [
        "mimeType = 'application/vnd.google-apps.document' and trashed = false "
        "and modifiedTime > '2026-10-01T00:00:00Z' "
        "and (name contains 'Notes by Gemini' or name contains 'Transcript')"
    ]
    assert http.responses == []
    assert len(first.intakes) == 25
    parts = first.intakes[:3]
    assert [item.key.external_id for item in parts] == [
        "file:t#part:1",
        "file:t#part:2",
        "file:t#part:3",
    ]
    assert [item.title for item in parts] == [
        "Weekly - Transcript (part 1 of 3)",
        "Weekly - Transcript (part 2 of 3)",
        "Weekly - Transcript (part 3 of 3)",
    ]
    assert all(len(item.text) <= 60_000 for item in parts)
    assert "\n\n".join(item.text for item in parts) == transcript
    assert len({item.key.revision_id for item in parts}) == 1
    assert all(item.url == "https://drive.google.com/file/d/t/view" for item in parts)
    assert all(item.key.resource_id == "drive_account:all" for item in first.intakes)
    assert [item.key.external_id for item in first.intakes[3:]] == [
        f"file:n{index}" for index in range(22)
    ]
    assert first.has_more is True
    assert first.checkpoint["mode"] == "backfill"
    assert first.checkpoint["page_token"] is None
    assert first.checkpoint["watermark"] == "2026-10-01T00:00:00Z"
    assert set(_known(first.checkpoint)) == {"t", *(f"n{index}" for index in range(22))}

    http.responses.extend(
        [
            _json(200, {"files": [long_doc, *notes]}),
            _json(200, notes[22]),
            _body("Summary\n\nNote 22.\n"),
        ]
    )
    second = client.fetch(_account_selection(), _meet_options(), first.checkpoint)

    assert _query(http.urls[-3])["q"] == listing["q"]
    assert len(http.urls) == 52
    assert [item.key.external_id for item in second.intakes] == ["file:n22"]
    assert second.has_more is False
    assert second.checkpoint["mode"] == "incremental"
    assert second.checkpoint["watermark"] == "2026-10-02T00:00:22Z"
    assert len(_known(second.checkpoint)) == 24


def test_non_ascii_parts_fit_the_collector_intake_attestation_bound(tmp_path: Path) -> None:
    accented = _doc("e", "Accented - Transcript", "2026-10-02T00:00:00Z")
    wide = _doc("w", "Wide - Transcript", "2026-10-03T00:00:00Z")
    http = _Http(
        [
            _json(200, {"files": [accented, wide]}),
            _json(200, accented),
            _body("é" * 60_000),
            _json(200, wide),
            _body("日" * 22_000),
        ]
    )

    batch = _client(tmp_path, http).fetch(_folder_selection(), _meet_options(), None)

    assert [item.key.external_id for item in batch.intakes] == [
        *(f"file:e#part:{index}" for index in range(1, 5)),
        "file:w#part:1",
        "file:w#part:2",
    ]
    assert "".join(item.text for item in batch.intakes[:4]) == "é" * 60_000
    assert "".join(item.text for item in batch.intakes[4:]) == "日" * 22_000
    # The collector attests each set intake with bounded_json at the same default maximum.
    for item in batch.intakes:
        assert len(bounded_json(item.payload().to_dict())) <= _CHECKPOINT_LIMIT


def test_per_file_failures_become_notices_and_the_set_continues(tmp_path: Path) -> None:
    files = [
        _doc("m", "A - Transcript", "2026-10-02T00:00:00Z"),
        _doc("x", "B - Transcript", "2026-10-02T00:00:01Z"),
        _doc("tr", "C - Transcript", "2026-10-02T00:00:02Z"),
        _doc("nd", "D - Transcript", "2026-10-02T00:00:03Z"),
        _doc("nt", "E - Transcript", "2026-10-02T00:00:04Z"),
        _doc("tl", "F - Transcript", "2026-10-02T00:00:05Z"),
        _doc("em", "G - Transcript", "2026-10-02T00:00:06Z"),
        _doc("tp", "H - Transcript", "2026-10-02T00:00:07Z"),
        _doc("ok", "I - Transcript", "2026-10-02T00:00:08Z"),
    ]
    trashed = {**files[2], "trashed": True}
    locked = {**files[3], "capabilities": {"canDownload": False}}
    too_many_parts = "\n\n".join(["x" * 40_000] * 26)
    assert len(too_many_parts.encode()) < 1_048_576
    http = _Http(
        [
            _json(200, {"files": files}),
            LiveHttpResponse(404, {}, b"{}"),
            _json(403, {"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}),
            _json(200, trashed),
            _json(200, locked),
            _json(200, files[4]),
            LiveHttpResponse(200, {}, b"\xff\xfe\xfd"),
            _json(200, files[5]),
            LiveHttpResponse(200, {}, b"a" * 1_048_577),
            _json(200, files[6]),
            _body("  \n\n  "),
            _json(200, files[7]),
            _body(too_many_parts),
            _json(200, files[8]),
            _body("Summary\n\nFine.\n"),
        ]
    )
    client = _client(tmp_path, http)

    batch = client.fetch(_folder_selection(), _meet_options(), None)

    assert http.responses == []
    assert batch.notices == (
        "drive_file_skipped:missing:m",
        "drive_file_skipped:access_lost:x",
        "drive_file_skipped:trashed:tr",
        "drive_file_skipped:not_downloadable:nd",
        "drive_file_skipped:not_text:nt",
        "drive_file_skipped:too_large:tl",
        "drive_file_skipped:empty:em",
        "drive_file_skipped:too_large:tp",
    )
    assert [item.key.external_id for item in batch.intakes] == ["file:ok"]
    assert batch.has_more is False
    assert batch.checkpoint["mode"] == "incremental"
    assert batch.checkpoint["watermark"] == "2026-10-02T00:00:08Z"
    known = _known(batch.checkpoint)
    assert set(known) == {"m", "x", "tr", "nd", "nt", "tl", "em", "tp", "ok"}
    assert known["m"]["modified"] == "2026-10-02T00:00:00Z"
    assert len(known["m"]["revision"]) == 64


def test_skipped_notices_cap_at_25_and_the_rest_wait_for_the_next_call(tmp_path: Path) -> None:
    files = [
        _doc(f"s{index}", f"Sync {index} - Transcript", f"2026-10-02T00:00:{index:02d}Z")
        for index in range(26)
    ]
    http = _Http([_json(200, {"files": files}), *[LiveHttpResponse(404, {}, b"{}")] * 26])
    client = _client(tmp_path, http)

    first = client.fetch(_folder_selection(), _meet_options(), None)

    assert http.responses == []
    assert len(first.notices) == 25
    assert first.notices[0] == "drive_file_skipped:missing:s0"
    assert first.notices[-1] == "drive_file_skipped:missing:s24"
    assert first.intakes == ()
    assert first.has_more is True
    assert first.checkpoint["mode"] == "backfill"
    assert first.checkpoint["page_token"] is None
    assert "s25" not in _known(first.checkpoint) and len(_known(first.checkpoint)) == 25

    http.responses.extend([_json(200, {"files": files}), LiveHttpResponse(404, {}, b"{}")])
    second = client.fetch(_folder_selection(), _meet_options(), first.checkpoint)

    assert http.responses == []
    assert second.notices == ("drive_file_skipped:missing:s25",)
    assert second.has_more is False
    assert second.checkpoint["mode"] == "incremental"
    assert second.checkpoint["watermark"] == "2026-10-02T00:00:25Z"
    assert len(_known(second.checkpoint)) == 26


def test_folder_missing_and_account_errors_leave_the_checkpoint_unchanged(
    tmp_path: Path,
) -> None:
    checkpoint: dict[str, object] = {
        "known": {"a": {"modified": "2026-10-05T09:00:00Z", "revision": "r1"}},
        "mode": "incremental",
        "page_token": None,
        "watermark": "2026-10-05T10:00:00Z",
    }
    missing = _Http([LiveHttpResponse(404, {}, b"{}")])
    gone = _client(tmp_path / "missing", missing).fetch(
        _folder_selection(), _meet_options(), checkpoint
    )
    assert gone.intakes == () and gone.notices == ("drive_folder_missing",)
    assert gone.checkpoint == checkpoint and gone.has_more is False

    lost = _Http([_json(403, {"error": {"errors": [{"reason": "insufficientFilePermissions"}]}})])
    revoked = _client(tmp_path / "lost", lost).fetch(_folder_selection(), _meet_options(), None)
    assert revoked.notices == ("drive_folder_missing",)
    assert revoked.checkpoint == DriveSetCheckpoint.initial("2026-10-01").to_dict()

    rate_limited = _json(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})
    failures = (
        (LiveHttpResponse(401, {}, b"{}"), "google_auth_required"),
        (rate_limited, "google_rate_limited"),
        (_json(403, {"error": {"errors": [{"reason": "other"}]}}), "drive_forbidden"),
        (LiveHttpResponse(404, {}, b"{}"), "google_fetch_failed"),
    )
    for index, (response, code) in enumerate(failures):
        http = _Http([response])
        with pytest.raises(LiveSourceError, match=code):
            _client(tmp_path / f"account-{index}", http).fetch(
                _account_selection(), _meet_options(), checkpoint
            )
        assert http.responses == []

    bad_checkpoint = _Http([])
    single_file_checkpoint: dict[str, object] = {
        "file_id": "a",
        "revision": "r",
        "status": "available",
    }
    with pytest.raises(LiveSourceError, match="drive_invalid_checkpoint"):
        _client(tmp_path / "bad", bad_checkpoint).fetch(
            _folder_selection(), _meet_options(), single_file_checkpoint
        )
    assert bad_checkpoint.urls == []


def test_known_entries_evict_the_oldest_at_five_hundred(tmp_path: Path) -> None:
    known: dict[str, object] = {
        f"k{index}": {
            "modified": f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z",
            "revision": "r",
        }
        for index in range(500)
    }
    checkpoint: dict[str, object] = {
        "known": known,
        "mode": "incremental",
        "page_token": None,
        "watermark": "2026-09-30T00:00:00Z",
    }
    fresh = _doc("fresh", "Fresh - Transcript", "2026-10-01T00:00:00Z")
    http = _Http([_json(200, {"files": [fresh]}), _json(200, fresh), _body("Speaker A: hi.")])

    batch = _client(tmp_path, http).fetch(_folder_selection(), _meet_options(), checkpoint)

    assert "modifiedTime > '2026-09-29T00:00:00Z'" in _query(http.urls[0])["q"][0]
    assert [item.key.external_id for item in batch.intakes] == ["file:fresh"]
    result = _known(batch.checkpoint)
    assert len(result) == 500 and "k0" not in result and "k1" in result and "fresh" in result
    assert batch.checkpoint["watermark"] == "2026-10-01T00:00:00Z"
    assert isinstance(batch.intakes[0], SourceRecordIntake)
