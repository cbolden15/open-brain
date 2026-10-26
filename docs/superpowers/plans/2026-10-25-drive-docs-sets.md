# Drive Docs Sets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture Google Docs that Gemini writes after Meet calls (notes and transcripts) as live Work sources through two new Drive selection shapes, with bounded backfill, long-document splitting and revision-aware re-capture.

**Architecture:** The connector gains a `google_drive_sets` module (selection, options, checkpoint, query, splitter) and `DriveSourceClient.fetch` dispatches on `resource_type`; `drive_file` is untouched. The collector's Slack-only revision gate becomes a connector table so folder and account Docs intakes reach the existing engine revision sink. The CLI gains one flag and the docs gain the walkthrough.

**Tech Stack:** Python 3.14, uv workspace, pytest with the existing `LiveHttpTransport` fakes, Drive v3 `files` endpoints already in use.

Spec: `docs/superpowers/specs/2026-10-25-drive-docs-sets-design.md` (revision 1, `6563018`).

## Global Constraints

- Coordinates stay 23/16/18. No engine schema, runtime-session or Portable change. No new dependency. No network destination beyond the Drive v3 `files` endpoints already used.
- `drive_file` behavior is byte-for-byte unchanged, and existing Drive tests pass without edits.
- Resource ids: `drive_folder:<folderId>` and `drive_account:all`. Options: `date_floor` (`YYYY-MM-DD`, required for both), `title_suffixes` (list of 1 to 8 strings, each 1 to 64 characters after stripping, no duplicates, no NUL; required for `drive_account`, optional for `drive_folder`). Any other key, missing required option or malformed value is `drive_invalid_options`; a resource id that does not match its type is `drive_invalid_selection`.
- Listing query: `mimeType = 'application/vnd.google-apps.document' and trashed = false and modifiedTime > '<bound>'`, plus `'<folderId>' in parents` for the folder shape, plus `(name contains '<s1>' or ...)` when suffixes are set; `orderBy=modifiedTime`, `pageSize=100`, `includeItemsFromAllDrives=true`, `supportsAllDrives=true`, fields add `parents` and `createdTime`. The exact suffix check (`name.endswith`) runs client-side. Suffix values are escaped for the Drive query grammar (backslash and single quote).
- Checkpoint: `{"mode": "backfill"|"incremental", "page_token": str|null, "watermark": "<RFC 3339 UTC>", "known": {"<fileId>": {"revision": "<hash>", "modified": "<RFC 3339>"}}}`. First run `mode=backfill`, `watermark=<date_floor>T00:00:00Z`. The query bound is constant across one listing: `watermark` in backfill, `watermark` minus 24 hours (86,400 s) in incremental (a Drive page token is bound to its query). `page_token` advances when a page fully drains; `watermark` advances only when the listing completes (`nextPageToken` absent after the last page drains), to the newest `modified` in `known` if later; `mode` then becomes `incremental`. `known` holds at most 500 entries, oldest `modified` dropped first. Any other shape is `drive_invalid_checkpoint`.
- Batch: at most 25 intakes and 25 notices (`LiveBatch`); a split Doc's parts each count; `has_more` is true while files on the page or `nextPageToken` remain. Per-file failures are notices `drive_file_skipped:<reason>:<fileId>` with reason in `missing|access_lost|trashed|not_downloadable|not_text|too_large|empty`, the file recorded in `known`, the run continuing; files past the 25th notice stay out of `known`. An export body over 1 MiB is refused by the transport (`source_transport_unavailable`) before the client sees it and raises like an account-level error; `too_large` is reached by a body that splits into more than 18 parts. Folder 404 or lost access is notice `drive_folder_missing` with no intakes and the checkpoint unchanged. 401 `google_auth_required`, rate-limit 403 `google_rate_limited`, other 403 `drive_forbidden` raise with the checkpoint unchanged.
- Splitting: parts of at most 60,000 characters and at most 120,000 bytes when JSON-encoded with ASCII escapes, cut at the last blank line within the limit, else the last newline, else a hard cut; leading newlines of the next part stripped; at most 18 parts (a 19th is `too_large`). Part intake: `external_id` `file:<fileId>#part:<n>` (1-based), title `<name> (part <n> of <N>)` cut to 200 characters with the suffix preserved, `url` the Doc link, `revision_id` `revision:<hash>` for every part. A Doc under the limit is one intake with `external_id` `file:<fileId>`.
- Revision gate: `_REVISION_CONNECTORS = {"slack": ("slack-source-v1", "slack-revisions"), "google_drive": ("drive-source-v1", "drive-revisions")}`; `observe_revision` attests `slack` intakes and `google_drive` intakes whose `resource_id` does not start with `drive_file:`; `observe_slack` is removed; privacy narrowing runs before observation; Slack bytes are unchanged. The collector revision sink's connector allow-list (`EngineRevisionSink._revision_capability` in `lifecycle.py`) becomes `{"saved_markdown", "slack", "slack_development", "google_drive"}`; the engine revision path has no Slack-specific constant.
- Descriptor `resource_types == ("drive_account", "drive_file", "drive_folder")` (the descriptor requires sorted labels), `content_types` stays `("drive_file",)`. Collector provider map becomes a set of resource types per provider. `LiveBatch` notices match `[a-z][a-z0-9_]{0,99}(?::[A-Za-z0-9_-]{1,128}){0,2}`; file and folder ids are `[A-Za-z0-9_-]{1,128}`.
- CLI: `resources --resource-type {drive_file,drive_folder}` default `drive_file`, `google_drive` only. No other new command.
- Tests use synthetic fixtures only: no network, no real Brain, credentials or Google data. Commit messages go in a file and are committed with `git commit -F`; every commit ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Never commit private notes, captures, transcripts, credentials, hostnames or logs.

---
### Task 1: Drive set selection and options (`google_drive_sets.py`)

**Files:**
- Create: `packages/connectors/src/open_brain_connectors/runtime/google_drive_sets.py`
- Create: `packages/connectors/tests/contract/test_google_drive_sets.py`

**Interfaces:**
- Consumes: `LiveSourceError` (`runtime/live_common.py:15`), `SourceResourceSelection` (`runtime/source_registry.py:166`; fields `connector_name, connection_id, resource_id, resource_type`).
- Produces (all in `google_drive_sets.py`):
  - Constants `MAX_PART_CHARS = 60_000`, `MAX_PARTS = 18`, `MAX_KNOWN = 500`, `MAX_SUFFIXES = 8`, `MAX_SUFFIX_CHARS = 64`, `MAX_TITLE_CHARS = 200`, `INCREMENTAL_OVERLAP_SECONDS = 86_400`, `DOCUMENT_MIME_TYPE = "application/vnd.google-apps.document"`, `FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"`, `SET_RESOURCE_TYPES = frozenset({"drive_folder", "drive_account"})`.
  - `DriveSetShape = Literal["folder", "account"]`.
  - `@dataclass(frozen=True, slots=True) class DriveSetSelection(shape: DriveSetShape, folder_id: str | None, date_floor: str, title_suffixes: tuple[str, ...])` with `matches_name(self, name: str) -> bool` (true when no suffixes, else exact `str.endswith` on any suffix).
  - `parse_drive_set(selection: SourceResourceSelection, options: dict[str, object]) -> DriveSetSelection`; raises `LiveSourceError("drive_invalid_selection")` for a non-set type or a resource id that does not match its type, `LiveSourceError("drive_invalid_options")` for any option fault. `date_floor` is normalized to `YYYY-MM-DD`; suffixes are stripped.
  - `require_file_id(value: object, code: str) -> str`: accepts `[A-Za-z0-9_-]{1,128}`, else raises `LiveSourceError(code)`.

- [ ] **Step 1: Write the failing tests**

Create `packages/connectors/tests/contract/test_google_drive_sets.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'open_brain_connectors.runtime.google_drive_sets'`

- [ ] **Step 3: Write the module**

Create `packages/connectors/src/open_brain_connectors/runtime/google_drive_sets.py`:

```python
"""Selection, checkpoint, listing-query and splitting rules for Google Drive Docs sets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = [
    "DOCUMENT_MIME_TYPE",
    "FOLDER_MIME_TYPE",
    "INCREMENTAL_OVERLAP_SECONDS",
    "MAX_KNOWN",
    "MAX_PARTS",
    "MAX_PART_CHARS",
    "MAX_SUFFIXES",
    "MAX_SUFFIX_CHARS",
    "MAX_TITLE_CHARS",
    "SET_RESOURCE_TYPES",
    "DriveSetSelection",
    "parse_drive_set",
    "require_file_id",
]

MAX_PART_CHARS = 60_000
MAX_PARTS = 18
MAX_KNOWN = 500
MAX_SUFFIXES = 8
MAX_SUFFIX_CHARS = 64
MAX_TITLE_CHARS = 200
INCREMENTAL_OVERLAP_SECONDS = 86_400
DOCUMENT_MIME_TYPE = "application/vnd.google-apps.document"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SET_RESOURCE_TYPES = frozenset({"drive_folder", "drive_account"})

_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

DriveSetShape = Literal["folder", "account"]


@dataclass(frozen=True, slots=True)
class DriveSetSelection:
    """A validated folder or account Docs set with its first-run floor and title filter."""

    shape: DriveSetShape
    folder_id: str | None
    date_floor: str
    title_suffixes: tuple[str, ...]

    def matches_name(self, name: str) -> bool:
        if not self.title_suffixes:
            return True
        return any(name.endswith(suffix) for suffix in self.title_suffixes)


def parse_drive_set(
    selection: SourceResourceSelection, options: dict[str, object]
) -> DriveSetSelection:
    """Validate a `drive_folder` or `drive_account` selection with its options."""
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != "google_drive"
        or selection.resource_type not in SET_RESOURCE_TYPES
    ):
        raise LiveSourceError("drive_invalid_selection")
    shape: DriveSetShape
    folder_id: str | None
    if selection.resource_type == "drive_folder":
        shape = "folder"
        folder_id = _set_resource_id(selection.resource_id, "drive_folder:")
        required = {"date_floor"}
        allowed = {"date_floor", "title_suffixes"}
    else:
        if selection.resource_id != "drive_account:all":
            raise LiveSourceError("drive_invalid_selection")
        shape = "account"
        folder_id = None
        required = {"date_floor", "title_suffixes"}
        allowed = required
    if type(options) is not dict or not required <= set(options) <= allowed:
        raise LiveSourceError("drive_invalid_options")
    floor = _date_floor(options["date_floor"])
    suffixes = _title_suffixes(options["title_suffixes"]) if "title_suffixes" in options else ()
    return DriveSetSelection(shape, folder_id, floor.isoformat(), suffixes)


def require_file_id(value: object, code: str) -> str:
    """Accept one Drive file or folder id; ids are safe in notices and query strings."""
    if type(value) is not str or _FILE_ID.fullmatch(value) is None:
        raise LiveSourceError(code)
    return value


def _set_resource_id(value: str, prefix: str) -> str:
    if not value.startswith(prefix):
        raise LiveSourceError("drive_invalid_selection")
    return require_file_id(value.removeprefix(prefix), "drive_invalid_selection")


def _date_floor(value: object) -> date:
    if type(value) is not str:
        raise LiveSourceError("drive_invalid_options")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise LiveSourceError("drive_invalid_options") from None


def _title_suffixes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SUFFIXES:
        raise LiveSourceError("drive_invalid_options")
    result: list[str] = []
    for item in value:
        if type(item) is not str:
            raise LiveSourceError("drive_invalid_options")
        suffix = item.strip()
        if not 1 <= len(suffix) <= MAX_SUFFIX_CHARS or "\x00" in suffix or suffix in result:
            raise LiveSourceError("drive_invalid_options")
        result.append(suffix)
    return tuple(result)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/connectors/src/open_brain_connectors/runtime/google_drive_sets.py \
  packages/connectors/tests/contract/test_google_drive_sets.py
printf 'feat(connectors): parse Drive folder and account set selections\n' \
  > "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
```

---

### Task 2: Drive set checkpoint, timestamps, query bound and `known` eviction

**Files:**
- Modify: `packages/connectors/src/open_brain_connectors/runtime/google_drive_sets.py` (imports at lines 5-11, `__all__` at 13-27, append new code after `_title_suffixes`)
- Test: `packages/connectors/tests/contract/test_google_drive_sets.py`

**Interfaces:**
- Consumes: `MAX_KNOWN`, `INCREMENTAL_OVERLAP_SECONDS`, `require_file_id` (Task 1); `safe_text(value: object, *, maximum: int = 512) -> str` (`live_common.py:39`).
- Produces:
  - `DriveSetMode = Literal["backfill", "incremental"]`.
  - `@dataclass(frozen=True, slots=True) class DriveKnownEntry(revision: str, modified: str)`.
  - `@dataclass(frozen=True, slots=True) class DriveSetCheckpoint(mode: DriveSetMode, page_token: str | None, watermark: str, known: dict[str, DriveKnownEntry])` with `@classmethod initial(cls, date_floor: str) -> DriveSetCheckpoint` (`backfill`, no token, `<date_floor>T00:00:00Z`, empty known), `@classmethod from_value(cls, value: object) -> DriveSetCheckpoint` (raises `LiveSourceError("drive_invalid_checkpoint")` for any other shape, unknown key, bad mode, non-string token, non-RFC 3339 UTC timestamp, bad file id, or more than 500 entries) and `to_dict(self) -> dict[str, object]` (keys `known`, `mode`, `page_token`, `watermark`; `known` sorted by file id, each `{"modified", "revision"}`).
  - `parse_timestamp(value: str) -> datetime` (RFC 3339 ending in `Z`, UTC; raises `ValueError`), `format_timestamp(moment: datetime) -> str` (UTC ISO 8601 with `Z`).
  - `query_bound(checkpoint: DriveSetCheckpoint) -> str`: the watermark in `backfill`, the watermark minus `INCREMENTAL_OVERLAP_SECONDS` in `incremental`.
  - `evict_known(known: Mapping[str, DriveKnownEntry]) -> dict[str, DriveKnownEntry]`: at most `MAX_KNOWN` entries, dropping the oldest `modified` first.
  - `advance_watermark(watermark: str, known: Mapping[str, DriveKnownEntry]) -> str`: the newest `modified` in `known` when newer than `watermark`, else `watermark` unchanged.

- [ ] **Step 1: Write the failing tests**

Add to the import block of `test_google_drive_sets.py`:

```python
from open_brain_connectors.runtime.google_drive_sets import (
    DriveKnownEntry,
    DriveSetCheckpoint,
    DriveSetSelection,
    advance_watermark,
    evict_known,
    parse_drive_set,
    query_bound,
    require_file_id,
)
```

Append:

```python
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
    DriveSetCheckpoint.from_value({**good, "known": {f"f{index}": entry for index in range(500)}})

    known = {
        f"k{index}": DriveKnownEntry("r", f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z")
        for index in range(2_001)
    }
    evicted = evict_known(known)
    assert len(evicted) == 500
    assert "k0" not in evicted and "k1" in evicted and "k2000" in evicted
    assert evict_known({"only": DriveKnownEntry("r", "2026-01-01T00:00:00Z")}) == {
        "only": DriveKnownEntry("r", "2026-01-01T00:00:00Z")
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: collection error, `ImportError: cannot import name 'DriveKnownEntry' from 'open_brain_connectors.runtime.google_drive_sets'`

- [ ] **Step 3: Write the implementation**

In `google_drive_sets.py` replace the import block with:

```python
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast

from open_brain_connectors.runtime.live_common import LiveSourceError, safe_text
from open_brain_connectors.runtime.source_registry import SourceResourceSelection
```

Add to `__all__` (keep the list sorted as shown):

```python
__all__ = [
    "DOCUMENT_MIME_TYPE",
    "FOLDER_MIME_TYPE",
    "INCREMENTAL_OVERLAP_SECONDS",
    "MAX_KNOWN",
    "MAX_PARTS",
    "MAX_PART_CHARS",
    "MAX_SUFFIXES",
    "MAX_SUFFIX_CHARS",
    "MAX_TITLE_CHARS",
    "SET_RESOURCE_TYPES",
    "DriveKnownEntry",
    "DriveSetCheckpoint",
    "DriveSetSelection",
    "advance_watermark",
    "evict_known",
    "format_timestamp",
    "parse_drive_set",
    "parse_timestamp",
    "query_bound",
    "require_file_id",
]
```

Append after `_title_suffixes`:

```python
DriveSetMode = Literal["backfill", "incremental"]
_CHECKPOINT_KEYS = frozenset({"mode", "page_token", "watermark", "known"})
_KNOWN_KEYS = frozenset({"revision", "modified"})


@dataclass(frozen=True, slots=True)
class DriveKnownEntry:
    """The provider revision last submitted or skipped for one file and its modifiedTime."""

    revision: str
    modified: str


@dataclass(frozen=True, slots=True)
class DriveSetCheckpoint:
    """Metadata-only progress for one Docs set; validated on every fetch."""

    mode: DriveSetMode
    page_token: str | None
    watermark: str
    known: dict[str, DriveKnownEntry]

    @classmethod
    def initial(cls, date_floor: str) -> DriveSetCheckpoint:
        return cls("backfill", None, f"{date_floor}T00:00:00Z", {})

    @classmethod
    def from_value(cls, value: object) -> DriveSetCheckpoint:
        if type(value) is not dict or set(value) != _CHECKPOINT_KEYS:
            raise LiveSourceError("drive_invalid_checkpoint")
        mode = value["mode"]
        if mode not in {"backfill", "incremental"}:
            raise LiveSourceError("drive_invalid_checkpoint")
        page_token = value["page_token"]
        if page_token is not None:
            _checkpoint_text(page_token, maximum=8_192)
        watermark = _checkpoint_timestamp(value["watermark"])
        known_value = value["known"]
        if type(known_value) is not dict or len(known_value) > MAX_KNOWN:
            raise LiveSourceError("drive_invalid_checkpoint")
        known: dict[str, DriveKnownEntry] = {}
        for file_id, entry in known_value.items():
            require_file_id(file_id, "drive_invalid_checkpoint")
            if type(entry) is not dict or set(entry) != _KNOWN_KEYS:
                raise LiveSourceError("drive_invalid_checkpoint")
            known[file_id] = DriveKnownEntry(
                _checkpoint_text(entry["revision"], maximum=512),
                _checkpoint_timestamp(entry["modified"]),
            )
        return cls(cast(DriveSetMode, mode), cast(str | None, page_token), watermark, known)

    def to_dict(self) -> dict[str, object]:
        return {
            "known": {
                file_id: {"modified": entry.modified, "revision": entry.revision}
                for file_id, entry in sorted(self.known.items())
            },
            "mode": self.mode,
            "page_token": self.page_token,
            "watermark": self.watermark,
        }


def parse_timestamp(value: str) -> datetime:
    """Parse one RFC 3339 UTC timestamp ending in Z, the form Drive uses for modifiedTime."""
    if type(value) is not str or not 20 <= len(value) <= 64 or not value.endswith("Z"):
        raise ValueError("invalid timestamp")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("invalid timestamp")
    return moment


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def query_bound(checkpoint: DriveSetCheckpoint) -> str:
    """The exclusive modifiedTime lower bound for the next listing."""
    if checkpoint.mode == "backfill":
        return checkpoint.watermark
    overlap = timedelta(seconds=INCREMENTAL_OVERLAP_SECONDS)
    return format_timestamp(parse_timestamp(checkpoint.watermark) - overlap)


def evict_known(known: Mapping[str, DriveKnownEntry]) -> dict[str, DriveKnownEntry]:
    """Keep at most MAX_KNOWN entries, dropping the oldest modifiedTime first."""
    if len(known) <= MAX_KNOWN:
        return dict(known)
    ordered = sorted(known.items(), key=lambda item: (parse_timestamp(item[1].modified), item[0]))
    return dict(ordered[len(ordered) - MAX_KNOWN :])


def advance_watermark(watermark: str, known: Mapping[str, DriveKnownEntry]) -> str:
    """Move the watermark to the newest modifiedTime recorded in known, never backwards."""
    current = parse_timestamp(watermark)
    newest = max((parse_timestamp(entry.modified) for entry in known.values()), default=current)
    return watermark if newest <= current else format_timestamp(newest)


def _checkpoint_text(value: object, *, maximum: int) -> str:
    try:
        return safe_text(value, maximum=maximum)
    except LiveSourceError:
        raise LiveSourceError("drive_invalid_checkpoint") from None


def _checkpoint_timestamp(value: object) -> str:
    try:
        parse_timestamp(cast(str, value))
    except ValueError:
        raise LiveSourceError("drive_invalid_checkpoint") from None
    return cast(str, value)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/connectors/src/open_brain_connectors/runtime/google_drive_sets.py \
  packages/connectors/tests/contract/test_google_drive_sets.py
printf 'feat(connectors): Drive set checkpoint with bounded known entries\n' \
  > "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
```

---

### Task 3: Listing query builder and Drive query escaping

**Files:**
- Modify: `packages/connectors/src/open_brain_connectors/runtime/google_drive_sets.py` (`__all__`; append after `advance_watermark`)
- Test: `packages/connectors/tests/contract/test_google_drive_sets.py`

**Interfaces:**
- Consumes: `DriveSetSelection`, `DOCUMENT_MIME_TYPE` (Task 1).
- Produces:
  - `escape_query_value(value: str) -> str`: backslash becomes `\\`, single quote becomes `\'`.
  - `build_list_query(selection: DriveSetSelection, bound: str) -> str`: `mimeType = '<doc>' and trashed = false and modifiedTime > '<bound>'`, then ` and '<folderId>' in parents` for the folder shape, then ` and (name contains '<s1>' or name contains '<s2>' ...)` when suffixes are set.

- [ ] **Step 1: Write the failing tests**

Add `build_list_query` and `escape_query_value` to the `google_drive_sets` import list in the test file (keep the names sorted). Append:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: collection error, `ImportError: cannot import name 'build_list_query' from 'open_brain_connectors.runtime.google_drive_sets'`

- [ ] **Step 3: Write the implementation**

Add `"build_list_query"` and `"escape_query_value"` to `__all__` (sorted position: after `"advance_watermark"` and before `"evict_known"`; `"escape_query_value"` goes before `"evict_known"` too). Append after `advance_watermark`:

```python
def escape_query_value(value: str) -> str:
    """Escape one literal for the Drive v3 query grammar."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_list_query(selection: DriveSetSelection, bound: str) -> str:
    """One files.list `q` for a Docs set; the suffix check on names stays client-side."""
    clauses = [
        f"mimeType = '{DOCUMENT_MIME_TYPE}'",
        "trashed = false",
        f"modifiedTime > '{bound}'",
    ]
    if selection.folder_id is not None:
        clauses.append(f"'{escape_query_value(selection.folder_id)}' in parents")
    if selection.title_suffixes:
        names = " or ".join(
            f"name contains '{escape_query_value(suffix)}'" for suffix in selection.title_suffixes
        )
        clauses.append(f"({names})")
    return " and ".join(clauses)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/connectors/src/open_brain_connectors/runtime/google_drive_sets.py \
  packages/connectors/tests/contract/test_google_drive_sets.py
printf 'feat(connectors): build the Drive Docs set listing query\n' \
  > "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
```

---

### Task 4: `split_document` and part titles

**Files:**
- Modify: `packages/connectors/src/open_brain_connectors/runtime/google_drive_sets.py` (`__all__`; append after `build_list_query`)
- Test: `packages/connectors/tests/contract/test_google_drive_sets.py`

**Interfaces:**
- Consumes: `MAX_PART_CHARS`, `MAX_PARTS`, `MAX_TITLE_CHARS` (Task 1).
- Produces:
  - `split_document(text: str) -> list[str]`: CRLF and CR are normalized to `\n` first (the engine does the same on capture); a text of at most 60,000 characters returns `[text]` unchanged; otherwise each part is at most 60,000 characters, cut at the last blank line in the window, else the last newline, else a hard cut; trailing newlines of a cut part and leading newlines of the next part are stripped; whitespace-only parts are dropped; more than 18 parts raises `LiveSourceError("drive_content_too_large")`.
  - `part_title(name: str, index: int, total: int) -> str`: `<name> (part <index> of <total>)`, name cut so the whole title is at most 200 characters with the suffix preserved.

- [ ] **Step 1: Write the failing tests**

Add `part_title` and `split_document` to the `google_drive_sets` import list. Append:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: collection error, `ImportError: cannot import name 'part_title' from 'open_brain_connectors.runtime.google_drive_sets'`

- [ ] **Step 3: Write the implementation**

Add `"part_title"` (after `"parse_timestamp"`) and `"split_document"` (after `"require_file_id"`) to `__all__`. Append after `build_list_query`:

```python
def split_document(text: str) -> list[str]:
    """Split one exported Doc into at most MAX_PARTS parts of at most MAX_PART_CHARS."""
    parts: list[str] = []
    remaining = text.replace("\r\n", "\n").replace("\r", "\n")
    while remaining:
        if len(parts) == MAX_PARTS:
            raise LiveSourceError("drive_content_too_large")
        if len(remaining) <= MAX_PART_CHARS:
            part, remaining = remaining, ""
        else:
            window = remaining[:MAX_PART_CHARS]
            cut = window.rfind("\n\n")
            if cut <= 0:
                cut = window.rfind("\n")
            if cut <= 0:
                cut = MAX_PART_CHARS
            part, remaining = window[:cut].rstrip("\n"), remaining[cut:].lstrip("\n")
        if part.strip():
            parts.append(part)
    return parts


def part_title(name: str, index: int, total: int) -> str:
    """`<name> (part <index> of <total>)` within MAX_TITLE_CHARS, suffix preserved."""
    suffix = f" (part {index} of {total})"
    return f"{name[: MAX_TITLE_CHARS - len(suffix)].rstrip()}{suffix}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/connectors/src/open_brain_connectors/runtime/google_drive_sets.py \
  packages/connectors/tests/contract/test_google_drive_sets.py
printf 'feat(connectors): split long Drive Docs into bounded parts\n' \
  > "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
```

---

### Task 5: Folder listing in `resources` and set-shaped notices in `LiveBatch`

**Files:**
- Modify: `packages/connectors/src/open_brain_connectors/runtime/google_sources.py:12-29` (imports) and `:389-411` (`DriveSourceClient.resources`)
- Modify: `packages/connectors/src/open_brain_connectors/runtime/live_common.py:13-14` (add a notice pattern constant) and `:66` (use it)
- Test: `packages/connectors/tests/contract/test_google_drive_sets.py`

**Interfaces:**
- Consumes: `FOLDER_MIME_TYPE` (Task 1); `_Http`, `_auth`, `_json` from `packages/connectors/tests/contract/test_google_sources.py:33-51`.
- Produces:
  - `DriveSourceClient.resources(self, connection_id: str, *, cursor: str | None = None, resource_type: str = "drive_file") -> LiveResourcePage`. `"drive_file"` is byte-for-byte the current request and result. `"drive_folder"` lists `q=mimeType = 'application/vnd.google-apps.folder' and trashed = false` with the same fields, page size, drive flags and cursor handling and returns `LiveResource("drive_folder:<id>", name, "drive_folder")`. Any other value raises `LiveSourceError("drive_invalid_selection")` before any request.
  - `LiveBatch` accepts notices matching `[a-z][a-z0-9_]{0,99}(?::[A-Za-z0-9_-]{1,128}){0,2}` (`live_common._NOTICE`), so `drive_file_skipped:<reason>:<fileId>` is a valid notice; every existing notice still matches.

- [ ] **Step 1: Write the failing tests**

Add these imports to the test file (first-party block, sorted):

```python
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from open_brain_connectors.runtime.google_sources import DriveSourceClient
from open_brain_connectors.runtime.live_common import LiveBatch, LiveResource, LiveSourceError

from .test_google_sources import _auth, _Http, _json
```

(`LiveSourceError` is already imported from `live_common`; merge it into the one import line as shown. `Path` goes in the stdlib block at the top. Ruff's isort wants `_auth, _Http, _json` in that order.) Append:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: `2 failed, 8 passed`; the resources test fails with `TypeError: DriveSourceClient.resources() got an unexpected keyword argument 'resource_type'`, the batch test with `LiveSourceError: source_invalid_batch` on the first `LiveBatch(...)`.

- [ ] **Step 3: Write the implementation**

In `live_common.py`, after the imports (line 13) add:

```python
_NOTICE = re.compile(r"[a-z][a-z0-9_]{0,99}(?::[A-Za-z0-9_-]{1,128}){0,2}")
```

and replace line 66

```python
            or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", n) for n in self.notices)
```

with

```python
            or any(_NOTICE.fullmatch(n) is None for n in self.notices)
```

In `google_sources.py`, add the import (first-party block, before `google_sources_auth`):

```python
from open_brain_connectors.runtime.google_drive_sets import FOLDER_MIME_TYPE
```

Replace `DriveSourceClient.resources` (lines 389-411) with:

```python
    def resources(
        self,
        connection_id: str,
        *,
        cursor: str | None = None,
        resource_type: str = "drive_file",
    ) -> LiveResourcePage:
        if resource_type == "drive_file":
            listing = "trashed = false"
        elif resource_type == "drive_folder":
            listing = f"mimeType = '{FOLDER_MIME_TYPE}' and trashed = false"
        else:
            raise LiveSourceError("drive_invalid_selection")
        query = {
            "fields": f"nextPageToken,files({_DRIVE_FIELDS})",
            "includeItemsFromAllDrives": "true",
            "pageSize": "100",
            "q": listing,
            "supportsAllDrives": "true",
        }
        if cursor is not None:
            query["pageToken"] = _text(cursor, maximum=8_192)
        payload = self._json(connection_id, "GET", f"{_DRIVE_ROOT}/files?{urlencode(query)}")
        files = _items(payload, "files", maximum=100)
        resources = tuple(
            LiveResource(
                f"{resource_type}:{_text(file.get('id'), maximum=512)}",
                _text(file.get("name"), maximum=512),
                resource_type,
            )
            for file in files
        )
        return LiveResourcePage(
            resources, _optional_text(payload.get("nextPageToken"), maximum=8_192)
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py packages/connectors/tests/contract/test_google_sources.py packages/connectors/tests/contract/test_live_common.py -q`
Expected: all pass (`10 passed` in the new module plus the unchanged existing suites)

- [ ] **Step 5: Commit**

```bash
git add packages/connectors/src/open_brain_connectors/runtime/google_sources.py \
  packages/connectors/src/open_brain_connectors/runtime/live_common.py \
  packages/connectors/tests/contract/test_google_drive_sets.py
printf 'feat(connectors): list Drive folders and admit set-shaped notices\n' \
  > "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
```

---

### Task 6: The Docs set fetch loop in `DriveSourceClient.fetch`

**Files:**
- Modify: `packages/connectors/src/open_brain_connectors/runtime/google_sources.py:5-29` (imports), `:33-47` (constants), `:379-380` (class docstring), `:413-422` (dispatch at the top of `fetch`), new methods after `fetch` (before `_content`, currently line 484), new module helpers appended after `_drive_url` (currently line 915-919)
- Test: `packages/connectors/tests/contract/test_google_drive_sets.py`

**Interfaces:**
- Consumes (Tasks 1-5): `SET_RESOURCE_TYPES`, `DOCUMENT_MIME_TYPE`, `MAX_TITLE_CHARS`, `DriveSetCheckpoint`, `DriveKnownEntry`, `advance_watermark`, `build_list_query`, `evict_known`, `parse_drive_set`, `parse_timestamp`, `part_title`, `query_bound`, `require_file_id`, `split_document`; existing `_DRIVE_FIELDS`, `_MAX_PAGE`, `_response`, `_drive_forbidden`, `_drive_revision`, `_drive_url`, `_items`, `_text`, `_optional_text`, `_response_json`, `_provider_error`, `local_source_privacy`, `SourceRecordIntake`, `SourceRecordKey`.
- Produces:
  - `DriveSourceClient.fetch(selection, options, checkpoint)` dispatches on `selection.resource_type`: `"drive_file"` runs the existing code unchanged; `"drive_folder"` and `"drive_account"` run the set loop. Set intakes have `observation=None`, `title=<name>` or `part_title(...)`, `url=<Doc link>`, and keys `SourceRecordKey("google_drive", selection.connection_id, selection.resource_id, "file:<id>" | "file:<id>#part:<n>", "revision:<drive revision>")`.
  - Listing request: `GET {_DRIVE_ROOT}/files?fields=nextPageToken,files(<_DRIVE_FIELDS>,parents,createdTime)&includeItemsFromAllDrives=true&orderBy=modifiedTime&pageSize=100&q=<build_list_query>&supportsAllDrives=true[&pageToken=...]`; per-file metadata and export requests are identical to the `drive_file` path.
  - Notices `drive_file_skipped:<reason>:<fileId>` with reason in `missing|access_lost|trashed|not_downloadable|not_text|too_large|empty`, at most 25 per batch; `drive_folder_missing` for a folder-shape list 404 or lost-access 403 with the checkpoint unchanged.
  - Checkpoint rules: the query bound is `query_bound(state)` and does not change while a `page_token` is outstanding; `page_token` advances when a page drains and `nextPageToken` is present; when the last page drains, `mode` becomes `incremental`, `page_token` is `None` and `watermark` becomes `advance_watermark(watermark, known)`; an undrained page (25-intake bound or 26th notice) keeps `mode`, `page_token` and `watermark` and returns `has_more=True`; `known` is always carried forward with new entries and evicted to 500.
  - Module constants `_DRIVE_LIST_FIELDS = f"{_DRIVE_FIELDS},parents,createdTime"`, `_MAX_EXPORT_BYTES = 1_048_576`; private `_SetFile` dataclass and `_set_intakes`, `_drive_modified` helpers.

- [ ] **Step 1: Write the failing tests**

Add `from typing import cast` to the stdlib imports, `LiveHttpResponse` to the imports (`from open_brain_connectors.runtime.live_http import LiveHttpResponse`), and `SourceRecordIntake` (`from open_brain_connectors.runtime.source_intake import SourceRecordIntake`). Append:

```python
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


def test_known_entries_evict_the_oldest_at_two_thousand(tmp_path: Path) -> None:
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py -q`
Expected: `6 failed, 10 passed`; each new test fails with `LiveSourceError: drive_invalid_selection` raised by `_drive_selection` (the current `fetch` only accepts `drive_file`).

- [ ] **Step 3: Write the implementation**

In `google_sources.py` replace the import block (lines 5-29) with:

```python
import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import cast
from urllib.parse import quote, urlencode

from open_brain_connectors.runtime.google_drive_sets import (
    DOCUMENT_MIME_TYPE,
    FOLDER_MIME_TYPE,
    MAX_TITLE_CHARS,
    SET_RESOURCE_TYPES,
    DriveKnownEntry,
    DriveSetCheckpoint,
    advance_watermark,
    build_list_query,
    evict_known,
    parse_drive_set,
    parse_timestamp,
    part_title,
    query_bound,
    require_file_id,
    split_document,
)
from open_brain_connectors.runtime.google_sources_auth import GoogleSourcesAuth
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveResource,
    LiveResourcePage,
    LiveSourceError,
    local_source_privacy,
    safe_text,
)
from open_brain_connectors.runtime.live_http import LiveHttpResponse, LiveHttpTransport
from open_brain_connectors.runtime.mail_drive import (
    DriveFileRecord,
    DriveSourceAdapter,
    GmailSourceAdapter,
    MailRecord,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection
```

After the `_DRIVE_FIELDS` constant (line 39-41) add:

```python
_DRIVE_LIST_FIELDS = f"{_DRIVE_FIELDS},parents,createdTime"
_MAX_EXPORT_BYTES = 1_048_576
```

Replace the `DriveSourceClient` docstring (line 380) with:

```python
    """Discover files or folders; fetch one selected file or a bounded page of a Docs set."""
```

At the top of `fetch` (currently line 419, before `file_id = _drive_selection(selection)`) insert:

```python
        if (
            type(selection) is SourceResourceSelection
            and selection.resource_type in SET_RESOURCE_TYPES
        ):
            return self._fetch_set(selection, options, checkpoint)
```

After `fetch` returns (`return LiveBatch((intake,), base_checkpoint)`, currently line 482) and before `_content`, add:

```python
    def _fetch_set(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        drive_set = parse_drive_set(selection, options)
        state = (
            DriveSetCheckpoint.initial(drive_set.date_floor)
            if checkpoint is None
            else DriveSetCheckpoint.from_value(checkpoint)
        )
        query = {
            "fields": f"nextPageToken,files({_DRIVE_LIST_FIELDS})",
            "includeItemsFromAllDrives": "true",
            "orderBy": "modifiedTime",
            "pageSize": "100",
            "q": build_list_query(drive_set, query_bound(state)),
            "supportsAllDrives": "true",
        }
        if state.page_token is not None:
            query["pageToken"] = state.page_token
        response = self._response(
            selection.connection_id, "GET", f"{_DRIVE_ROOT}/files?{urlencode(query)}"
        )
        if response.status == 401:
            raise LiveSourceError("google_auth_required")
        if response.status == 403:
            error = _drive_forbidden(response)
            if drive_set.shape == "folder" and error.code == "drive_access_lost":
                return LiveBatch((), state.to_dict(), notices=("drive_folder_missing",))
            raise error
        if response.status == 404 and drive_set.shape == "folder":
            return LiveBatch((), state.to_dict(), notices=("drive_folder_missing",))
        payload = _response_json(response)
        files = _items(payload, "files", maximum=100)
        next_token = _optional_text(payload.get("nextPageToken"), maximum=8_192)
        intakes: list[SourceRecordIntake] = []
        notices: list[str] = []
        known = dict(state.known)
        drained = True
        for file in files:
            file_id = require_file_id(file.get("id"), "google_invalid_response")
            if not drive_set.matches_name(_text(file.get("name"), maximum=512)):
                continue
            modified = _drive_modified(file)
            listed_revision = _drive_revision(file)
            entry = known.get(file_id)
            if entry is not None and entry.revision == listed_revision:
                continue
            result = self._set_file(selection.connection_id, file_id, listed_revision)
            if result.skipped is not None:
                if len(notices) >= _MAX_PAGE:
                    drained = False
                    break
                notices.append(f"drive_file_skipped:{result.skipped}:{file_id}")
                known[file_id] = DriveKnownEntry(result.revision, modified)
                continue
            if len(intakes) + len(result.parts) > _MAX_PAGE:
                drained = False
                break
            intakes.extend(_set_intakes(selection, file_id, result))
            known[file_id] = DriveKnownEntry(result.revision, modified)
        known = evict_known(known)
        if not drained:
            next_state = replace(state, known=known)
        elif next_token is not None:
            next_state = replace(state, page_token=next_token, known=known)
        else:
            next_state = DriveSetCheckpoint(
                "incremental", None, advance_watermark(state.watermark, known), known
            )
        return LiveBatch(
            tuple(intakes),
            next_state.to_dict(),
            has_more=not drained or next_token is not None,
            notices=tuple(notices),
        )

    def _set_file(self, connection_id: str, file_id: str, listed_revision: str) -> _SetFile:
        metadata_query = urlencode({"fields": _DRIVE_FIELDS, "supportsAllDrives": "true"})
        response = self._response(
            connection_id,
            "GET",
            f"{_DRIVE_ROOT}/files/{quote(file_id, safe='')}?{metadata_query}",
        )
        if response.status == 404:
            return _SetFile(listed_revision, skipped="missing")
        if response.status == 401:
            raise LiveSourceError("google_auth_required")
        if response.status == 403:
            error = _drive_forbidden(response)
            if error.code == "drive_access_lost":
                return _SetFile(listed_revision, skipped="access_lost")
            raise error
        metadata = _response_json(response)
        if _text(metadata.get("id"), maximum=512) != file_id:
            raise LiveSourceError("drive_invalid_response")
        revision = _drive_revision(metadata)
        if metadata.get("trashed") is True:
            return _SetFile(revision, skipped="trashed")
        capability = metadata.get("capabilities")
        if not isinstance(capability, Mapping) or capability.get("canDownload") is not True:
            return _SetFile(revision, skipped="not_downloadable")
        if _text(metadata.get("mimeType"), maximum=256) != DOCUMENT_MIME_TYPE:
            return _SetFile(revision, skipped="not_text")
        export_query = urlencode({"mimeType": "text/plain", "supportsAllDrives": "true"})
        export = self._response(
            connection_id,
            "GET",
            f"{_DRIVE_ROOT}/files/{quote(file_id, safe='')}/export?{export_query}",
            accept="text/plain, text/csv",
        )
        if export.status == 404:
            return _SetFile(revision, skipped="missing")
        if export.status == 401:
            raise LiveSourceError("google_auth_required")
        if export.status == 403:
            error = _drive_forbidden(export)
            if error.code == "drive_access_lost":
                return _SetFile(revision, skipped="access_lost")
            raise error
        if export.status >= 400:
            raise _provider_error(export)
        if len(export.body) > _MAX_EXPORT_BYTES:
            return _SetFile(revision, skipped="too_large")
        try:
            text = export.body.decode("utf-8")
        except UnicodeDecodeError:
            return _SetFile(revision, skipped="not_text")
        if "\x00" in text:
            return _SetFile(revision, skipped="not_text")
        if not text.strip():
            return _SetFile(revision, skipped="empty")
        try:
            parts = split_document(text)
        except LiveSourceError:
            return _SetFile(revision, skipped="too_large")
        return _SetFile(
            revision,
            name=_text(metadata.get("name"), maximum=512)[:MAX_TITLE_CHARS],
            url=_drive_url(metadata, file_id),
            parts=tuple(parts),
        )
```

Append at the end of the module (after `_drive_url`):

```python
@dataclass(frozen=True, slots=True)
class _SetFile:
    """One Docs-set file after metadata and export: either parts to submit or a skip reason."""

    revision: str
    skipped: str | None = None
    name: str = ""
    url: str = ""
    parts: tuple[str, ...] = ()


def _set_intakes(
    selection: SourceResourceSelection, file_id: str, result: _SetFile
) -> list[SourceRecordIntake]:
    total = len(result.parts)
    intakes: list[SourceRecordIntake] = []
    try:
        for index, part in enumerate(result.parts, start=1):
            external_id = f"file:{file_id}" if total == 1 else f"file:{file_id}#part:{index}"
            title = result.name if total == 1 else part_title(result.name, index, total)
            intakes.append(
                SourceRecordIntake(
                    key=SourceRecordKey(
                        "google_drive",
                        selection.connection_id,
                        selection.resource_id,
                        external_id,
                        f"revision:{result.revision}",
                    ),
                    url=result.url,
                    title=title,
                    text=part,
                    privacy=local_source_privacy(),
                )
            )
    except Exception as error:
        raise LiveSourceError("drive_invalid_response") from error
    return intakes


def _drive_modified(metadata: Mapping[str, object]) -> str:
    value = _text(metadata.get("modifiedTime"), maximum=64)
    try:
        parse_timestamp(value)
    except ValueError:
        raise LiveSourceError("google_invalid_response") from None
    return value
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_google_drive_sets.py packages/connectors/tests/contract/test_google_sources.py packages/connectors/tests/contract/test_mail_drive_source_adapter.py -q`
Expected: all pass (`16 passed` in the new module; the two existing Drive suites unchanged and green)

- [ ] **Step 5: Commit**

```bash
git add packages/connectors/src/open_brain_connectors/runtime/google_sources.py \
  packages/connectors/tests/contract/test_google_drive_sets.py
printf 'feat(connectors): fetch Drive Docs sets by folder or title suffix\n' \
  > "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
```

---

### Task 7: Descriptor resource types, registry test, lint and typecheck

**Files:**
- Modify: `packages/connectors/src/open_brain_connectors/runtime/source_registry.py:376-387` (`google_drive_source_descriptor`)
- Modify: `packages/connectors/tests/contract/test_source_registry.py:73` (`test_source_catalog_lists_descriptors_without_import_or_capture_authority`) and `:112` (`test_d4_mail_and_drive_descriptors_declare_selected_resource_scopes`)

**Interfaces:**
- Consumes: `SourceConnectorDescriptor` (`source_registry.py:85`). Its `__post_init__` runs `_ordered_unique_labels` (`source_registry.py:556-563`), which rejects any `resource_types` tuple that is not sorted, so the only admissible three-tuple is the sorted one.
- Produces: `google_drive_source_descriptor().resource_types == ("drive_account", "drive_file", "drive_folder")`; `content_types` stays `("drive_file",)`.

- [ ] **Step 1: Update the registry tests so they fail**

In `test_source_registry.py` replace line 73

```python
    assert catalog.require("google_drive").resource_types == ("drive_file",)
```

with

```python
    assert catalog.require("google_drive").resource_types == (
        "drive_account",
        "drive_file",
        "drive_folder",
    )
```

and replace line 112

```python
    assert drive.resource_types == ("drive_file",)
```

with

```python
    assert drive.resource_types == ("drive_account", "drive_file", "drive_folder")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest packages/connectors/tests/contract/test_source_registry.py -q`
Expected: `2 failed`, both with `AssertionError: assert ('drive_file',) == ('drive_account', 'drive_file', 'drive_folder')`

- [ ] **Step 3: Update the descriptor**

In `source_registry.py` replace `google_drive_source_descriptor` with:

```python
def google_drive_source_descriptor() -> SourceConnectorDescriptor:
    """Return D4's Google Drive source capability: one file, a Docs folder, or account Docs."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D4_GOOGLE_DRIVE_SOURCE,
        display_name="Google Drive",
        auth_mode=SourceAuthMode.DEVICE_FLOW,
        resource_types=("drive_account", "drive_file", "drive_folder"),
        content_types=("drive_file",),
        preview_limit=25,
        public_onboarding=True,
    )
```

- [ ] **Step 4: Run the registry tests and the whole connectors contract suite**

Run: `uv run --frozen pytest packages/connectors/tests/contract -q`
Expected: all pass, no failures.

- [ ] **Step 5: Run the repository lint and typecheck gates**

Run: `make lint`
Expected: `All checks passed!`

Run: `make typecheck`
Expected: `Success: no issues found in N source files`

Run: `git diff --check`
Expected: no output.

Fix any finding inside the files this package created or modified (`google_drive_sets.py`, `google_sources.py`, `live_common.py`, `source_registry.py`, `test_google_drive_sets.py`, `test_source_registry.py`) and rerun both gates until clean.

- [ ] **Step 6: Commit**

```bash
git add packages/connectors/src/open_brain_connectors/runtime/source_registry.py \
  packages/connectors/tests/contract/test_source_registry.py
printf 'feat(connectors): declare drive_folder and drive_account resource types\n' \
  > "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/P1_COMMIT_MSG"
```
### Task 8: revision gate table, `observe_revision`, and the collector revision-sink allow-list

Prerequisite: none inside this package. Independent of Package 1 (no Drive provider code is called; Drive intakes are built by hand).

**Plan-time check (spec "Revision-aware intake"):** the revision write path is
`runner.collector_revision_sink` (`packages/collector/src/open_brain_collector/runner.py:670-676`)
→ `lifecycle.EngineRevisionSink` (`lifecycle.py:1462`) → `tasks.sources.public_revision_sink`
(`packages/engine/src/open_brain_engine/engine/sources.py:53`) → `PublicJobRevisionSink` and
`register_intake` in `engine/source_intake.py`. The engine side has **no** Slack-specific constant:
`SourceRevisionObservation` (`engine/source_observation.py:17-44`) accepts any
`normalization_version` of 1 to 1024 characters, `require_unreserved_consumer_namespace`
(`source_intake.py:41-48`) reserves only `consumer_chat`, and `SourceRevisionBinding` checks only
the four namespace keys. The two engine `"slack-source-v1"` literals (`engine/sharing.py:264`,
`portable/v7.py:511`) sit on the Slack sharing-preview path, selected by
`SlackSharingPreviewRequest`, not on revision capture; they are untouched. **One Slack-specific
constant does exist on the collector side of the path:** `EngineRevisionSink._revision_capability`
(`lifecycle.py:1488-1491`) admits only `{"saved_markdown", "slack", "slack_development"}` and raises
`ConnectorContractError("invalid collector capture")` for every other connector, so a
`google_drive` intake can never reach the engine sink today. This task generalizes that allow-list
with a test. The Slack ordering block at `lifecycle.py:1573-1593` keys on `edited:<timestamp>`
revision ids and `connector_name in {"slack", "slack_development"}`; Drive revision ids are
`revision:<hash>`, so Drive uses the head/predecessor ordering with no order file, and that block
stays as it is.

**Files:**
- Modify: `packages/collector/src/open_brain_collector/capture_policy.py:72-92` (replace `observe_slack`)
- Modify: `packages/collector/src/open_brain_collector/lifecycle.py:1488-1491` (allow-list)
- Modify: `packages/collector/src/open_brain_collector/live_capture.py:508-512` and `:528-530` (callers)
- Modify: `packages/collector/tests/integration/test_capture_revision_processing.py:22`, `:211-214` (caller)
- Create: `packages/collector/tests/contract/test_capture_policy_revision.py`
- Test: `packages/collector/tests/integration/test_capture_revision_processing.py` (one new function at the end)

**Interfaces:**
- Consumes: `SourceRecordIntake`, `SourceRecordKey` (`packages/connectors/src/open_brain_connectors/runtime/source_intake.py:34,84`); `SourceRevisionObservation` (`engine/source_observation.py:17`); `bounded_json`, `local_source_privacy`, `LiveSourceError` (`live_common.py`); `portable_canonical_json_bytes` (`open_brain_engine.core.ids`); `collector_revision_sink(brain_root: Path, state_root: Path) -> EngineRevisionSink` (`runner.py:670`); `ConnectorContractError` (`open_brain_connectors.runtime.connectors`).
- Produces (module `open_brain_collector.capture_policy`):
  - `_REVISION_CONNECTORS: dict[str, tuple[str, str]] = {"slack": ("slack-source-v1", "slack-revisions"), "google_drive": ("drive-source-v1", "drive-revisions")}`.
  - `observe_revision(intake: SourceRecordIntake) -> SourceRecordIntake`: returns the intake unchanged unless its connector is in the table and `observation is None`; for `google_drive` also returns it unchanged when `intake.key.resource_id.startswith("drive_file:")`. Otherwise attests with the connector's normalization version using exactly the former `observe_slack` formula.
  - `revision_store_dirname(connector_name: str) -> str | None`: the table's directory name, `None` for connectors outside the table.
  - `observe_slack` no longer exists.
- Produces (module `open_brain_collector.lifecycle`): `EngineRevisionSink._revision_capability` admits `google_drive` in addition to `saved_markdown`, `slack`, `slack_development`.

- [ ] **Step 1: write the failing contract tests**

```python
# packages/collector/tests/contract/test_capture_policy_revision.py
"""The revision gate is a table keyed by connector; Slack attestation bytes do not move."""

from __future__ import annotations

import hashlib
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.source_observation import SourceRevisionObservation

from open_brain_collector import capture_policy
from open_brain_collector.capture_policy import (
    _REVISION_CONNECTORS,
    observe_revision,
    revision_store_dirname,
)
from open_brain_connectors.runtime.live_common import bounded_json, local_source_privacy
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey

ACCOUNT = "account:synthetic"
DRIVE_REVISION = "revision:" + "a" * 64


def _intake(
    connector: str,
    resource_id: str,
    external_id: str,
    revision: str,
    text: str = "Synthetic body",
) -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey(connector, ACCOUNT, resource_id, external_id, revision),
        "https://docs.google.com/document/d/doc-1/edit",
        text,
        local_source_privacy(),
        title="Synthetic title",
    )


def _slack() -> SourceRecordIntake:
    return _intake("slack", "channel:CSYNTHETIC", "message:1791900000.000001", "edited:1")


def _expected(intake: SourceRecordIntake, normalization_version: str) -> SourceRevisionObservation:
    digest = hashlib.sha256(intake.text.encode()).hexdigest()
    privacy = intake.privacy.to_dict()
    payload = intake.payload().to_dict()
    return SourceRevisionObservation(
        original_sha256=digest,
        transformed_sha256=digest,
        normalization_version=normalization_version,
        privacy_policy_version=cast(str, privacy["policy_version"]),
        privacy_policy_sha256=hashlib.sha256(bounded_json(privacy)).hexdigest(),
        admitted_payload_sha256=hashlib.sha256(portable_canonical_json_bytes(payload)).hexdigest(),
    )


def test_table_holds_exactly_slack_and_drive_with_their_directories() -> None:
    assert _REVISION_CONNECTORS == {
        "slack": ("slack-source-v1", "slack-revisions"),
        "google_drive": ("drive-source-v1", "drive-revisions"),
    }
    assert revision_store_dirname("slack") == "slack-revisions"
    assert revision_store_dirname("google_drive") == "drive-revisions"
    assert revision_store_dirname("gmail") is None
    assert revision_store_dirname("agent_session") is None
    assert not hasattr(capture_policy, "observe_slack")


def test_slack_attestation_bytes_are_unchanged() -> None:
    intake = _slack()
    observed = observe_revision(intake)
    assert observed.observation == _expected(intake, "slack-source-v1")
    assert observed.observation is not None
    assert observed.observation.normalization_version == "slack-source-v1"
    assert observed.observation.privacy_policy_version == "priority-capture-v1"
    assert observed.observation.original_sha256 == hashlib.sha256(b"Synthetic body").hexdigest()
    assert observed.observation.transformed_sha256 == observed.observation.original_sha256
    assert (observed.key, observed.url, observed.text, observed.privacy, observed.title) == (
        intake.key,
        intake.url,
        intake.text,
        intake.privacy,
        intake.title,
    )
    assert observe_revision(observed) is observed


def test_drive_set_shapes_are_attested_with_the_drive_version() -> None:
    folder = _intake("google_drive", "drive_folder:folder-1", "file:doc-1", DRIVE_REVISION)
    account = _intake("google_drive", "drive_account:all", "file:doc-1#part:2", DRIVE_REVISION)
    for intake in (folder, account):
        observed = observe_revision(intake)
        assert observed.observation == _expected(intake, "drive-source-v1")
        assert observed.key == intake.key and observed.text == intake.text
        assert observe_revision(observed) is observed


def test_drive_file_and_connectors_outside_the_table_pass_through() -> None:
    single = _intake("google_drive", "drive_file:doc-1", "file:doc-1", DRIVE_REVISION)
    assert observe_revision(single) is single
    assert single.observation is None
    gmail = _intake("gmail", "label:INBOX", "message1", "r1")
    assert observe_revision(gmail) is gmail
    session = _intake("agent_session", "local_project:synthetic", "turn:1", "r1")
    assert observe_revision(session) is session
```

- [ ] **Step 2: run the contract tests and see them fail**

```sh
uv run --frozen pytest packages/collector/tests/contract/test_capture_policy_revision.py -q
```

Expected: collection error, `ImportError: cannot import name '_REVISION_CONNECTORS' from 'open_brain_collector.capture_policy'`.

- [ ] **Step 3: replace `observe_slack` with the table and `observe_revision`**

Replace `capture_policy.py` lines 72-92 (the whole `observe_slack` function) with:

```python
_REVISION_CONNECTORS: dict[str, tuple[str, str]] = {
    "slack": ("slack-source-v1", "slack-revisions"),
    "google_drive": ("drive-source-v1", "drive-revisions"),
}


def revision_store_dirname(connector_name: str) -> str | None:
    """Collector state directory holding a connector's retained revision envelopes."""
    entry = _REVISION_CONNECTORS.get(connector_name)
    return None if entry is None else entry[1]


def _revision_gated(intake: SourceRecordIntake) -> bool:
    if intake.key.connector_name not in _REVISION_CONNECTORS or intake.observation is not None:
        return False
    # A single selected Drive file keeps the plain capture path with its
    # `Source revision:` text prefix. Routing it through the revision sink would
    # turn its first edit into a conflict against the existing capture.
    return not (
        intake.key.connector_name == "google_drive"
        and intake.key.resource_id.startswith("drive_file:")
    )


def observe_revision(intake: SourceRecordIntake) -> SourceRecordIntake:
    """Attest transformed payload and privacy for the connector's revision capability."""
    if not _revision_gated(intake):
        return intake
    normalization_version = _REVISION_CONNECTORS[intake.key.connector_name][0]
    digest = hashlib.sha256(intake.text.encode()).hexdigest()
    privacy = intake.privacy.to_dict()
    payload = intake.payload().to_dict()
    bounded_json(payload)  # Preserve the existing intake size admission limit.
    return replace(
        intake,
        observation=SourceRevisionObservation(
            original_sha256=digest,
            transformed_sha256=digest,
            normalization_version=normalization_version,
            privacy_policy_version=cast(str, privacy["policy_version"]),
            privacy_policy_sha256=hashlib.sha256(bounded_json(privacy)).hexdigest(),
            admitted_payload_sha256=hashlib.sha256(
                portable_canonical_json_bytes(payload)
            ).hexdigest(),
        ),
    )
```

The imports at the top of `capture_policy.py` (`hashlib`, `replace`, `cast`, `portable_canonical_json_bytes`, `SourceRevisionObservation`, `bounded_json`) are unchanged and all still used.

- [ ] **Step 4: switch the two callers to `observe_revision`**

In `live_capture.py` lines 508-512, change the import block inside `preview` to:

```python
                from open_brain_collector.capture_policy import (
                    admitted_intake,
                    observe_revision,
                    provider_options,
                )
```

and lines 528-530 to:

```python
                admitted = tuple(
                    observe_revision(admitted_intake(item, options)) for item in batch.intakes
                )
```

In `packages/collector/tests/integration/test_capture_revision_processing.py` line 22:

```python
from open_brain_collector.capture_policy import observe_revision
```

and lines 212 and 214 inside `test_development_revision_sink_replays_history_without_promoting_it`:

```python
    first = observe_revision(intake("Originaldevelopment", "evidence-1"))
    first = replace(first, key=replace(first.key, connector_name="slack_development"))
    second = observe_revision(intake("Currentdevelopment", "evidence-2"))
    second = replace(second, key=replace(second.key, connector_name="slack_development"))
```

(The `intake` helper there builds a `slack` key, so the attestation still carries `slack-source-v1` before the connector is renamed; bytes are unchanged.)

- [ ] **Step 5: run the contract tests and the renamed caller**

```sh
uv run --frozen pytest packages/collector/tests/contract/test_capture_policy_revision.py packages/collector/tests/integration/test_capture_revision_processing.py -q
rg -n "observe_slack" packages docs --glob '!docs/superpowers/specs/*'
```

Expected: all PASS; `rg` prints nothing.

- [ ] **Step 6: write the failing sink test**

Append to `packages/collector/tests/integration/test_capture_revision_processing.py`. Add these two imports to the existing import block (keep the block sorted; `pytest`, `replace`, `compile_single_user_local`, `open_local_engine`, `collector_revision_sink`, `SourceRecordIntake`, `SourceRecordKey` are already imported there):

```python
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.live_common import LiveBatch, local_source_privacy
```

(`LiveBatch` is already imported from `live_common`; extend that line rather than adding a second one.)

```python
def test_revision_sink_admits_attested_drive_set_intake_and_refuses_gmail(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    sink = collector_revision_sink(profile.root, tmp_path / "drive-revisions")
    drive = SourceRecordIntake(
        SourceRecordKey(
            "google_drive",
            "account:synthetic",
            "drive_folder:folder-1",
            "file:doc-1",
            "revision:" + "a" * 64,
        ),
        "https://docs.google.com/document/d/doc-1/edit",
        "Drivesinkneedle",
        local_source_privacy(),
        title="Weekly sync - Notes by Gemini",
    )
    result = sink.submit(observe_revision(drive))
    assert result.outcome == "captured" and result.source_receipt is not None
    assert len(open_local_engine(profile).retrieval.search("Drivesinkneedle")) == 1
    gmail = replace(drive, key=replace(drive.key, connector_name="gmail"))
    with pytest.raises(ConnectorContractError, match="invalid collector capture"):
        sink.submit(gmail)
```

- [ ] **Step 7: run it and see it fail**

```sh
uv run --frozen pytest packages/collector/tests/integration/test_capture_revision_processing.py -q -k drive_set_intake
```

Expected: `ConnectorContractError: invalid collector capture` raised from `EngineRevisionSink._revision_capability` on the Drive submit.

- [ ] **Step 8: admit `google_drive` in the revision capability**

In `lifecycle.py` lines 1488-1491 change the allow-list to:

```python
        if type(intake) is not SourceRecordIntake or intake.key.connector_name not in {
            "saved_markdown", "slack", "slack_development", "google_drive"
        }:
            raise ConnectorContractError("invalid collector capture")
```

No other line of `lifecycle.py` changes. The Slack ordering block at lines 1573-1593 stays keyed on `slack` and `slack_development`.

- [ ] **Step 9: run the collector revision suites**

```sh
uv run --frozen pytest packages/collector/tests/contract/test_capture_policy_revision.py packages/collector/tests/integration/test_capture_revision_processing.py packages/collector/tests/integration/test_slack_capture_acceptance.py packages/collector/tests/integration/test_capture_workflow.py packages/collector/tests/integration/test_saved_markdown.py -q
```

Expected: all PASS. The Slack and saved-markdown suites are the regression net for unchanged bytes.

- [ ] **Step 10: commit**

Write this message to a file outside the checkout (your scratchpad directory) and commit with it:

```
feat(collector): revision gate keyed by connector; attest Drive set intakes

Replace observe_slack with observe_revision over a connector table
(slack -> slack-source-v1/slack-revisions, google_drive ->
drive-source-v1/drive-revisions). drive_file intakes stay on the plain
capture path. EngineRevisionSink now admits google_drive; the engine
side had no Slack-specific constant on the revision write path.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

```sh
git add packages/collector/src/open_brain_collector/capture_policy.py packages/collector/src/open_brain_collector/lifecycle.py packages/collector/src/open_brain_collector/live_capture.py packages/collector/tests/contract/test_capture_policy_revision.py packages/collector/tests/integration/test_capture_revision_processing.py
git commit -F <message file>
```

---

### Task 9: `_PROVIDERS` becomes a set of resource types per provider

Prerequisite: Task 8 (shares `live_capture.py`; no functional dependency).

**Files:**
- Modify: `packages/collector/src/open_brain_collector/live_capture.py:33-38` (the map) and `:86-92` (`_selection`, the only reader; `rg -n "_PROVIDERS" packages` confirms there is no other)
- Test: `packages/collector/tests/integration/test_live_capture.py` (one new function after `test_preview_then_unordered_revision_refusal_preserves_source_and_checkpoint`)

**Interfaces:**
- Consumes: `SourceResourceSelection(connector_name, connection_id, resource_id, resource_type)` (`source_registry.py:166`); `LiveCaptureService.configure(source_id, selection, options, *, reset=False, disable=False) -> dict[str, object]` (`live_capture.py:271`), whose summary holds `"selection": asdict(selection)`.
- Produces (module `open_brain_collector.live_capture`): `_PROVIDERS: dict[str, frozenset[str]] = {"gmail": frozenset({"mail_label"}), "google_drive": frozenset({"drive_file", "drive_folder", "drive_account"}), "slack": frozenset({"channel"}), "agent_session": frozenset({"local_project"})}`; `_selection(value)` raises `LiveSourceError("source_invalid_selection")` unless `resource_type` is in the provider's set.

- [ ] **Step 1: write the failing test**

Add `from dataclasses import asdict` to the imports of `test_live_capture.py` (after `import json`), and `from open_brain_collector.live_capture import _PROVIDERS, LiveCaptureService` in place of the existing `LiveCaptureService` import. Then add after the first test:

```python
def test_selection_admits_every_drive_shape_and_rejects_foreign_resource_types(
    tmp_path: Path,
) -> None:
    assert _PROVIDERS == {
        "gmail": frozenset({"mail_label"}),
        "google_drive": frozenset({"drive_file", "drive_folder", "drive_account"}),
        "slack": frozenset({"channel"}),
        "agent_session": frozenset({"local_project"}),
    }
    service = _service(tmp_path, Runtime())
    shapes = (
        ("drive_file", "drive_file:doc-1", {}),
        ("drive_folder", "drive_folder:folder-1", {"date_floor": "2026-10-01"}),
        (
            "drive_account",
            "drive_account:all",
            {"date_floor": "2026-10-01", "title_suffixes": ["Notes by Gemini", "Transcript"]},
        ),
    )
    for resource_type, resource_id, options in shapes:
        selection = SourceResourceSelection(
            "google_drive", "account:synthetic", resource_id, resource_type
        )
        summary = service.configure("drive-" + resource_type, selection, options)
        assert summary["selection"] == asdict(selection)
        assert summary["status"] == "disabled"
    assert len(cast(list[object], service.status()["sources"])) == 3
    for connector, resource_type in (
        ("google_drive", "mail_label"),
        ("google_drive", "channel"),
        ("gmail", "drive_folder"),
        ("slack", "drive_account"),
        ("agent_session", "drive_file"),
    ):
        selection = SourceResourceSelection(
            connector, "account:synthetic", resource_type + ":x", resource_type
        )
        with pytest.raises(LiveSourceError, match="source_invalid_selection"):
            service.configure("rejected", selection, {})
    assert len(cast(list[object], service.status()["sources"])) == 3
```

- [ ] **Step 2: run it and see it fail**

```sh
uv run --frozen pytest packages/collector/tests/integration/test_live_capture.py -q -k every_drive_shape
```

Expected: `AssertionError` on the `_PROVIDERS ==` comparison (the current values are plain strings). After fixing only the literal, the next failure would be `LiveSourceError: source_invalid_selection` on the `drive_folder` configure.

- [ ] **Step 3: make the map a set per provider and update its reader**

`live_capture.py` lines 33-38:

```python
_PROVIDERS: dict[str, frozenset[str]] = {
    "gmail": frozenset({"mail_label"}),
    "google_drive": frozenset({"drive_file", "drive_folder", "drive_account"}),
    "slack": frozenset({"channel"}),
    "agent_session": frozenset({"local_project"}),
}
```

`live_capture.py` lines 86-92 (inside `_selection`):

```python
    try:
        result = SourceResourceSelection(**value)
        if result.resource_type not in _PROVIDERS.get(result.connector_name, frozenset()):
            raise ValueError
        return result
    except TypeError, ValueError:
        raise LiveSourceError("source_invalid_selection") from None
```

- [ ] **Step 4: run the live-capture and manager suites**

```sh
uv run --frozen pytest packages/collector/tests/integration/test_live_capture.py packages/collector/tests/integration/test_live_manager.py packages/collector/tests/integration/test_live_control.py -q
```

Expected: all PASS.

- [ ] **Step 5: commit**

Message file:

```
feat(collector): map each provider to a set of resource types

google_drive now admits drive_file, drive_folder and drive_account
selections; the other providers keep their single type. _selection is
the only reader.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

```sh
git add packages/collector/src/open_brain_collector/live_capture.py packages/collector/tests/integration/test_live_capture.py
git commit -F <message file>
```

---

### Task 10: `_submit` routes on the table into a per-connector revisions directory

Prerequisite: Tasks 8 and 9.

**Files:**
- Modify: `packages/collector/src/open_brain_collector/live_capture.py:638-646` (the Slack branch of `_submit`)
- Test: `packages/collector/tests/integration/test_live_capture.py` (shared Drive and Slack fixtures plus one new function)

**Interfaces:**
- Consumes: `revision_store_dirname` (Task 8); `collector_revision_sink(brain_root, state_root)` (`runner.py:670`), whose `EngineRevisionSink` writes `saved-revision-<sha256>.json` under `state_root` through `PrivateJsonStore`, creating the directory on first write; `SavedMarkdownDelivery.outcome` and `.source_receipt` (`lifecycle.py:1444-1459`).
- Produces: `LiveCaptureService._submit` sends an intake to `collector_revision_sink(self._brain_root, self._store.root / revision_store_dirname(connector))` when `intake.observation is not None` and the connector is in `_REVISION_CONNECTORS`; Slack keeps `slack-revisions`, Drive sets use `drive-revisions`; every other intake, including attested-less `drive_file`, keeps the `Source revision:` prefix path unchanged.

- [ ] **Step 1: add the shared fixtures and the failing test**

In `test_live_capture.py`, directly after the existing `_service` helper, add:

```python
DRIVE_SET = SourceResourceSelection(
    "google_drive", "account:synthetic", "drive_folder:folder-1", "drive_folder"
)
DRIVE_FILE = SourceResourceSelection(
    "google_drive", "account:synthetic", "drive_file:doc-1", "drive_file"
)
SLACK = SourceResourceSelection("slack", "account:synthetic", "channel:CSYNTHETIC", "channel")


def _drive_intake(
    text: str = "Firstdrivetext",
    *,
    selection: SourceResourceSelection = DRIVE_SET,
    external_id: str = "file:doc-1",
    revision: str = "a",
) -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey(
            "google_drive",
            "account:synthetic",
            selection.resource_id,
            external_id,
            "revision:" + revision * 64,
        ),
        "https://docs.google.com/document/d/doc-1/edit",
        text,
        local_source_privacy(),
        title="Weekly sync - Notes by Gemini",
    )


def _slack_intake(text: str = "Slackneedle", *, revision: str = "edited:1") -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey(
            "slack", "account:synthetic", SLACK.resource_id, "message:1791900000.000001", revision
        ),
        "https://example.slack.com/archives/CSYNTHETIC/p1791900000000001",
        text,
        local_source_privacy(),
        title="Synthetic Slack thread",
    )


class Routed(Runtime):
    """One fake provider answering several selections by resource id."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: dict[str, LiveBatch] = {}

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        self.calls += 1
        self.checkpoints.append(checkpoint)
        return self.batches[selection.resource_id]


def _apply(service: LiveCaptureService, source_id: str) -> dict[str, object]:
    preview = service.preview(source_id)
    return service.apply(source_id, cast(str, preview["preview_id"]))


def _envelopes(state: Path, directory: str) -> list[Path]:
    return sorted((state / directory).glob("saved-revision-*.json"))
```

Then add this test at the end of the file:

```python
def test_revision_envelopes_live_in_the_connector_directory(tmp_path: Path) -> None:
    runtime = Routed()
    runtime.batches = {
        DRIVE_FILE.resource_id: LiveBatch(
            (_drive_intake("Singlefiletext", selection=DRIVE_FILE),), {"cursor": "1"}
        ),
        DRIVE_SET.resource_id: LiveBatch((_drive_intake("Setdrivetext"),), {"cursor": "1"}),
        SLACK.resource_id: LiveBatch((_slack_intake(),), {"cursor": "1"}),
    }
    service = _service(tmp_path, runtime)
    state = tmp_path / "state"
    service.configure("file", DRIVE_FILE, {})
    assert _apply(service, "file")["captured_count"] == 1
    assert not (state / "drive-revisions").exists()
    assert not (state / "slack-revisions").exists()
    service.configure("set", DRIVE_SET, {"date_floor": "2026-10-01"})
    assert _apply(service, "set")["captured_count"] == 1
    assert (state / "drive-revisions").is_dir()
    assert len(_envelopes(state, "drive-revisions")) == 1
    assert not (state / "slack-revisions").exists()
    service.configure("slack", SLACK, {})
    assert _apply(service, "slack")["captured_count"] == 1
    assert len(_envelopes(state, "slack-revisions")) == 1
    assert len(_envelopes(state, "drive-revisions")) == 1
    assert not list(state.glob("saved-revision-*.json"))
    assert not list(state.glob("slack-order-*.json"))
    assert len(list((state / "slack-revisions").glob("slack-order-*.json"))) == 1
    profile = compile_single_user_local(tmp_path / "brain")
    for needle in ("Singlefiletext", "Setdrivetext", "Slackneedle"):
        assert len(open_local_engine(profile).retrieval.search(needle)) == 1
```

- [ ] **Step 2: run it and see it fail**

```sh
uv run --frozen pytest packages/collector/tests/integration/test_live_capture.py -q -k connector_directory
```

Expected: `AssertionError` at `assert (state / "drive-revisions").is_dir()`. The attested set intake still takes the plain capture path because `_submit` only routes `connector_name == "slack"`.

- [ ] **Step 3: route on the table**

Replace `live_capture.py` lines 638-646 with:

```python
        from open_brain_collector.capture_policy import revision_store_dirname

        revisions = revision_store_dirname(intake.key.connector_name)
        if revisions is not None and intake.observation is not None:
            from open_brain_collector.runner import collector_revision_sink

            result = collector_revision_sink(
                self._brain_root, self._store.root / revisions
            ).submit(intake)
            if result.source_receipt is None or result.outcome not in {"captured", "duplicate"}:
                raise LiveSourceError("source_revision_pending")
            return result.outcome, result.source_receipt.capture_id
```

Lines 647-672 (the plain capture path with the `Source revision:` prefix) are unchanged.

- [ ] **Step 4: run the live-capture and Slack suites**

```sh
uv run --frozen pytest packages/collector/tests/integration/test_live_capture.py packages/collector/tests/integration/test_slack_capture_acceptance.py packages/collector/tests/integration/test_custody.py -q
```

Expected: all PASS.

- [ ] **Step 5: commit**

Message file:

```
feat(collector): route attested intakes to the connector's revision store

_submit consults the revision gate table instead of a Slack literal.
Slack keeps slack-revisions; Drive folder and account sets use
drive-revisions; drive_file and every other connector keep the plain
Source revision prefix path.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

```sh
git add packages/collector/src/open_brain_collector/live_capture.py packages/collector/tests/integration/test_live_capture.py
git commit -F <message file>
```

---

### Task 11: end-to-end Drive edit behavior and the package gates

Prerequisite: Task 10.

**Files:**
- Test: `packages/collector/tests/integration/test_live_capture.py` (two new functions at the end)
- No source change is expected. If a test fails, the fix belongs in Task 8 or Task 10 code and is made in this task's commit with the failing test as evidence.

**Interfaces:**
- Consumes: `DRIVE_SET`, `DRIVE_FILE`, `_drive_intake`, `Routed`, `_apply`, `_envelopes` (Task 10); `open_local_database_read_only(profile)` (`open_brain_engine.engine.local_schema`), which yields a connection with `sqlite3.Row` rows; the `source_revisions` columns `capture_id`, `source_id`, `sequence`, `predecessor_capture_id`, `source_bytes` (`engine/source_schema.py:18-28`), where `source_bytes` is the full retained record JSON including the payload text; `LiveCaptureService.custody_status`, `custody_inspect` (`live_capture.py:399-405`).
- Produces: nothing new. Pins the spec's four collector behaviors: an edited `drive_folder` Doc becomes a new searchable revision with the first text in history; an edited `drive_file` is still quarantined with `source_revision_conflict`; Slack bytes unchanged (Task 8 contract test and Task 10 directory test); the revisions directory is `drive-revisions` (Task 10).

- [ ] **Step 1: write the two tests**

Add `from open_brain_engine.engine.local_schema import open_local_database_read_only` to the imports of `test_live_capture.py` (after the `open_local_engine` import), then append:

```python
def test_edited_drive_set_doc_becomes_new_revision_with_first_text_in_history(
    tmp_path: Path,
) -> None:
    runtime = Routed()
    runtime.batches = {
        DRIVE_SET.resource_id: LiveBatch((_drive_intake("Firstdrivetext"),), {"mode": "backfill"})
    }
    service = _service(tmp_path, runtime)
    service.configure(
        "meet",
        DRIVE_SET,
        {"date_floor": "2026-10-01", "title_suffixes": ["Notes by Gemini", "Transcript"]},
    )
    first = service.preview("meet")
    assert first["count"] == 1
    assert "Firstdrivetext" not in json.dumps(first)
    assert cast(list[dict[str, str]], first["records"])[0]["title"] == (
        "Weekly sync - Notes by Gemini"
    )
    assert service.apply("meet", cast(str, first["preview_id"]))["captured_count"] == 1
    profile = compile_single_user_local(tmp_path / "brain")
    hits = open_local_engine(profile).retrieval.search("Firstdrivetext")
    assert len(hits) == 1 and hits[0].trust == "third_party"
    runtime.batches[DRIVE_SET.resource_id] = LiveBatch(
        (_drive_intake("Seconddrivetext", revision="b"),), {"mode": "incremental"}
    )
    second = service.preview("meet")
    assert second["preview_id"] != first["preview_id"]
    result = service.apply("meet", cast(str, second["preview_id"]))
    assert result["captured_count"] == 1
    assert result["quarantined_count"] == 0
    assert runtime.checkpoints == [None, {"mode": "backfill"}]
    tasks = open_local_engine(profile)
    assert len(tasks.retrieval.search("Seconddrivetext")) == 1
    assert not tasks.retrieval.search("Firstdrivetext")
    with open_local_database_read_only(profile) as connection:
        rows = connection.execute(
            "SELECT capture_id, source_id, sequence, predecessor_capture_id, source_bytes "
            "FROM source_revisions ORDER BY sequence"
        ).fetchall()
    assert [row["sequence"] for row in rows] == [1, 2]
    assert rows[0]["source_id"] == rows[1]["source_id"]
    assert rows[0]["predecessor_capture_id"] is None
    assert rows[1]["predecessor_capture_id"] == rows[0]["capture_id"]
    assert rows[0]["capture_id"] == hits[0].capture_id
    assert b"Firstdrivetext" in bytes(rows[0]["source_bytes"])
    assert b"Seconddrivetext" in bytes(rows[1]["source_bytes"])
    assert b"Firstdrivetext" not in bytes(rows[1]["source_bytes"])
    assert len(_envelopes(tmp_path / "state", "drive-revisions")) == 1
    custody = service.custody_status("meet")
    assert cast(dict[str, int], custody["counts"]).get("quarantined", 0) == 0
    restarted = LiveCaptureService(tmp_path / "state", tmp_path / "brain", runtime=runtime)
    assert restarted.apply("meet", cast(str, second["preview_id"])) == result


def test_edited_drive_file_is_still_quarantined_with_source_revision_conflict(
    tmp_path: Path,
) -> None:
    runtime = Routed()
    runtime.batches = {
        DRIVE_FILE.resource_id: LiveBatch(
            (_drive_intake("Firstfiletext", selection=DRIVE_FILE),), {"cursor": "1"}
        )
    }
    service = _service(tmp_path, runtime)
    service.configure("file", DRIVE_FILE, {})
    first = service.preview("file")
    assert service.apply("file", cast(str, first["preview_id"]))["captured_count"] == 1
    profile = compile_single_user_local(tmp_path / "brain")
    assert len(open_local_engine(profile).retrieval.search("Firstfiletext")) == 1
    runtime.batches[DRIVE_FILE.resource_id] = LiveBatch(
        (_drive_intake("Secondfiletext", selection=DRIVE_FILE, revision="b"),), {"cursor": "2"}
    )
    second = service.preview("file")
    result = service.apply("file", cast(str, second["preview_id"]))
    assert result["quarantined_count"] == 1
    assert result["captured_count"] == 0
    assert runtime.checkpoints == [None, {"cursor": "1"}]
    assert len(open_local_engine(profile).retrieval.search("Firstfiletext")) == 1
    assert not open_local_engine(profile).retrieval.search("Secondfiletext")
    custody = service.custody_status("file")
    assert cast(dict[str, int], custody["counts"])["quarantined"] == 1
    receipt_id = cast(list[str], custody["receipt_ids"])[0]
    assert service.custody_inspect(receipt_id)["reason_code"] == "source_revision_conflict"
    assert not (tmp_path / "state" / "drive-revisions").exists()
```

- [ ] **Step 2: run the two tests**

```sh
uv run --frozen pytest packages/collector/tests/integration/test_live_capture.py -q -k "edited_drive"
```

Expected: both PASS on the Task 10 code. If `test_edited_drive_set_doc_becomes_new_revision_with_first_text_in_history` fails at `result["captured_count"] == 1` with `quarantined_count == 1`, the set intake did not carry an observation into `_submit`; check `_revision_gated` in Task 8 (the `drive_file:` prefix test must use `resource_id`, not `external_id`) and the `_submit` routing condition in Task 10. If it fails at `predecessor_capture_id`, the second submit did not see the first head; check that `revision_store_dirname` returns the same directory on both runs.

- [ ] **Step 3: run the whole collector package and the connector contract tests that share the key layout**

```sh
uv run --frozen pytest packages/collector packages/connectors/tests/contract/test_source_intake.py -q
```

Expected: all PASS, including every pre-existing Slack, custody and saved-markdown test with no edits.

- [ ] **Step 4: run the repository gates**

```sh
make lint
make typecheck
make test-tiers-audit
git diff --check
```

Expected: `ruff` reports no findings; `mypy` reports `Success: no issues found`; the tiers audit prints `"ok": true` (new tests are collected into the fast tier; do not hand-edit `tests/tiers/integration.txt`, which is generated from timing input); `git diff --check` prints nothing.

- [ ] **Step 5: commit**

Message file:

```
test(collector): pin Drive set edits as revisions and drive_file edits as conflicts

An edited drive_folder Doc becomes sequence 2 of the same logical
source with the first text retained in source_revisions; an edited
drive_file is still quarantined with source_revision_conflict and
creates no drive-revisions state.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

```sh
git add packages/collector/tests/integration/test_live_capture.py
git commit -F <message file>
```
# Package 3: collector CLI flag, documentation, gotcha, acceptance gate and docs tests (Tasks 12–15)

Spec (authoritative, revision 1): `docs/superpowers/specs/2026-10-25-drive-docs-sets-design.md`.
Baseline: `feat/drive-docs-sets` at `6563018`, coordinates 23/16/18.

### Interface deviations

1. **The CLI default is filled in by the CLI, not by argparse.** `--resource-type` is declared with
   `default=None` and `choices=("drive_file", "drive_folder")`. `_command_arguments` adds
   `resource_type="drive_file"` only when the command is `resources`, the provider is
   `google_drive` and no value was given. Reason: `_command_arguments` forwards every non-`None`
   argparse value, so an argparse default of `"drive_file"` would reach `sources.resources` for
   `gmail` and `slack` and be rejected by the manager's `source_invalid_arguments` rule. The owner
   still sees the documented default (`drive_file`) and the manager still applies its own default
   for `request`-style callers that omit the key.
2. **The manager picks the Drive branch with `isinstance(client, DriveSourceClient)`**, not with a
   provider string, because `PriorityRuntime.client` returns
   `GmailSourceClient | DriveSourceClient | SlackSourceClient` and only the Drive client's
   `resources` accepts `resource_type` under `mypy --strict`. This mirrors the existing
   `assert isinstance(client, SlackSourceClient)` at `live_manager.py:134`.
3. **Gotcha id is `DOCS-005`** (Category `Documentation / Google Drive`). The registry is
   newest-first; the entry goes directly under `## Registry`, above `BUILD-021`. The last ids are
   `DOCS-004`, `INTEGRATION-029`, `COLLECTOR-008`, `CLI-004`.
4. **The docs test is one module, `tests/release/test_drive_docs_sets_docs.py`**, created in
   Task 14 with the Drive-guide assertions and rewritten in full in Task 15 with the operator-guide,
   `CLAUDE.md` and gotcha assertions added. Both tasks show the complete file.
5. **Task 12 typechecks only after Package 1's `DriveSourceClient.resources(..., resource_type=)`
   has landed.** Its test passes on its own (the fake client carries the keyword), but
   `make typecheck` reports `Unexpected keyword argument "resource_type" for "resources" of
   "DriveSourceClient"` until the connector change is on the branch. Order Package 1's `resources`
   task before Task 12, or run Task 12 and Task 15's `make typecheck` after it.

All code in this package was applied to scratch copies of the touched files and checked with the
repo's `ruff check`, `ruff format --check` and `mypy --strict` configuration, and the four docs
tests were run against scratch copies of the four documents with the plan's edits applied (5
passed). The only finding was the expected Package 1 dependency above.

### Verified baseline facts

Confirmed with `rg`/`sed -n` against the worktree at `6563018`. Line numbers drift as Package 1
and 2 tasks land; re-run the quoted command before editing.

- `live_manager.py:311-321` is the `sources.resources` branch:
  `_shape(args, {"provider", "connection_id"}, {"cursor"})`, then
  `self.runtime.client(_provider(args["provider"])).resources(safe_text(args["connection_id"]), cursor=...)`.
  `_shape` (`:80-84`) raises `LiveSourceError("source_invalid_arguments")` for any key outside
  `required | optional`. `safe_text` (`live_common.py:39-47`) raises `source_invalid_value` for a
  non-string, empty or over-long value. `DriveSourceClient` is already imported at `:23`.
- `LiveSourceManager.__init__(root, brain_root, *, background=False, runtime: PriorityRuntime | None = None)`
  (`live_manager.py:148-156`); tests subclass `PriorityRuntime` and pass `runtime=`
  (`packages/collector/tests/integration/test_capture_workflow.py:30-72`). A manager needs a compiled
  Brain: `compile_single_user_local(tmp_path / "brain")` (`test_live_manager.py:33`).
- `LiveResource(resource_id, name, resource_type)` validates each field with `safe_text`;
  `LiveResourcePage(resources: tuple[LiveResource, ...], next_cursor=None)` allows at most 100
  resources (`live_common.py:75-105`). `to_dict()` returns `{resource_id, name, resource_type}`.
- `sources_cli.py:288-302` builds the `accounts|connect|disconnect|resources` parsers;
  `resources` has `--provider` (choices `gmail`, `google_drive`, `slack`), `--connection-id`
  (required) and `--cursor`. `_command_arguments` (`:405-431`) forwards every argparse value that
  is not in `ignored` and not `None`. Failures print `{"outcome": "failed", "failure_code": <code>}`
  and return 78 (`:391-402`). Tests drive `main(argv)` with `--foreground` and either a real
  Brain (`test_slack_policy.py:79-108`) or a monkeypatched `sources_cli.LiveSourceManager`
  (`test_source_metadata.py:729-756`, helper `_cli` at `:611-615`).
- `mypy` is `strict = true` over `packages`, `tests`, `tools`; ruff selects `E F I UP B SIM`, line
  length 100, `py314` (`pyproject.toml`). No `conftest.py` under `packages/collector/tests`.
- `docs/integrations/google-sources.md:77-79` says "Select exactly one file. Fetch gets that file
  only; it does not recursively mirror folders." `:98-102` records the 2026-09-16 live "Drive body
  edit" check. Neither doc contains "not just", "seamless", "robust", "powerful", "effortless",
  "dive in" or "/Users/" today. No test pins these two docs
  (`rg -n 'google-sources|priority-capture-operator' tests packages --glob '*.py'` is empty).
- `docs/integrations/priority-capture-operator.md:47`: "Drive options are an empty object and its
  selection is one file, without folder recursion."
- `CLAUDE.md:33` ends the Product boundary sentence "and the collector CLI exposes the source
  operations headlessly." `tests/security/test_open_brain_product_split.py:39-51` pins
  "does not install a desktop app, collector, scheduler" and "Open Brain never requires root" in
  that file; both must survive the edit.
- `docs/engineering/gotchas/README.md` entries are `### <CAT>-<NNN>: <title>`, blank line,
  `Category: <text>.`, blank line, `Symptom:` paragraph, `Action:` paragraph, `Discovered: <date>,
  <context>.` (see `BUILD-021` at `:7-29`). Lines wrap near 80 characters.
- `tests/release/test_daily_brief_docs.py` is the mirror: `ROOT = Path(__file__).resolve().parents[2]`,
  a `required` tuple of literal tokens with `missing = [...]; assert not missing, missing`, a heading
  case check against `PROPER_NOUNS`, a marketing-tell check and a private-path check.

---

### Task 12: `sources.resources` accepts `resource_type` for Google Drive

**Files:**
- Modify: `packages/collector/src/open_brain_collector/live_manager.py:311-321`
- Test: `packages/collector/tests/integration/test_live_manager.py`

**Interfaces:**
- Consumes: `DriveSourceClient.resources(self, connection_id: str, *, cursor: str | None = None, resource_type: str = "drive_file") -> LiveResourcePage` (Package 1, `google_sources.py`); `LiveResource`, `LiveResourcePage`, `LiveSourceError`, `safe_text` from `open_brain_connectors.runtime.live_common`; `_shape`, `_provider`, `PriorityRuntime.client` in `live_manager.py`.
- Produces: `LiveSourceManager.dispatch("sources.resources", {"provider", "connection_id", "cursor"?, "resource_type"?})`. `resource_type` is optional; for `google_drive` it defaults to `"drive_file"` and is passed to `DriveSourceClient.resources(..., resource_type=...)`; for any other provider a present `resource_type` raises `LiveSourceError("source_invalid_arguments")` before any client is built. The response shape `{"resources": [...], "next_cursor": ...}` is unchanged.

- [ ] **Step 1: Write the failing test**

Append to `packages/collector/tests/integration/test_live_manager.py`. Replace the existing import
block (lines 1-15) with the one below, then add the fakes and the test at the end of the file.

```python
from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from open_brain.profile import compile_single_user_local
from open_brain_collector.live_manager import LiveSourceManager, PriorityRuntime
from open_brain_connectors.runtime.agent_session_hooks import (
    AgentSessionHookManager,
    HookConfigError,
    HookConfigPreview,
)
from open_brain_connectors.runtime.google_sources import DriveSourceClient, GmailSourceClient
from open_brain_connectors.runtime.live_common import (
    LiveResource,
    LiveResourcePage,
    LiveSourceError,
)
from open_brain_connectors.runtime.slack_live import SlackSourceClient
```

```python
class _DriveClient(DriveSourceClient):
    """Records every resources() call instead of talking to Google."""

    def __init__(self, calls: list[tuple[str, str | None, str]]) -> None:
        self.calls = calls

    def resources(
        self, connection_id: str, *, cursor: str | None = None, resource_type: str = "drive_file"
    ) -> LiveResourcePage:
        self.calls.append((connection_id, cursor, resource_type))
        return LiveResourcePage(
            (LiveResource(f"{resource_type}:synthetic", "Synthetic", resource_type),),
            next_cursor=None,
        )


class _ResourcesRuntime(PriorityRuntime):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls: list[tuple[str, str | None, str]] = []
        self.providers: list[str] = []

    def client(self, provider: str) -> GmailSourceClient | DriveSourceClient | SlackSourceClient:
        self.providers.append(provider)
        return _DriveClient(self.calls)


def test_resources_passes_drive_resource_type_and_rejects_it_for_other_providers(
    tmp_path: Path,
) -> None:
    compile_single_user_local(tmp_path / "brain")
    runtime = _ResourcesRuntime(tmp_path / "live")
    manager = LiveSourceManager(tmp_path / "live", tmp_path / "brain", runtime=runtime)
    base: dict[str, object] = {"provider": "google_drive", "connection_id": "account:abc"}

    files = manager.dispatch("sources.resources", base)
    assert files == {
        "resources": [
            {
                "resource_id": "drive_file:synthetic",
                "name": "Synthetic",
                "resource_type": "drive_file",
            }
        ],
        "next_cursor": None,
    }
    folders = manager.dispatch(
        "sources.resources", {**base, "resource_type": "drive_folder", "cursor": "page-2"}
    )
    assert folders["resources"] == [
        {
            "resource_id": "drive_folder:synthetic",
            "name": "Synthetic",
            "resource_type": "drive_folder",
        }
    ]
    assert runtime.calls == [
        ("account:abc", None, "drive_file"),
        ("account:abc", "page-2", "drive_folder"),
    ]

    for provider in ("gmail", "slack"):
        other: dict[str, object] = {
            "provider": provider,
            "connection_id": "account:abc",
            "resource_type": "drive_folder",
        }
        with pytest.raises(LiveSourceError, match="source_invalid_arguments"):
            manager.dispatch("sources.resources", other)
    with pytest.raises(LiveSourceError, match="source_invalid_value"):
        manager.dispatch("sources.resources", {**base, "resource_type": ""})
    # Rejections happen before any client is built.
    assert runtime.providers == ["google_drive", "google_drive"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest packages/collector/tests/integration/test_live_manager.py -q -k resource_type`

Expected: FAIL at the `folders = manager.dispatch(...)` line with
`LiveSourceError: source_invalid_arguments` (the current `_shape` call does not allow
`resource_type`).

- [ ] **Step 3: Write the minimal implementation**

Replace the `sources.resources` branch in `packages/collector/src/open_brain_collector/live_manager.py`
(currently lines 311-321) with:

```python
        if operation == "sources.resources":
            _shape(args, {"provider", "connection_id"}, {"cursor", "resource_type"})
            provider = _provider(args["provider"])
            resource_type = args.get("resource_type")
            if resource_type is not None and provider != "google_drive":
                raise LiveSourceError("source_invalid_arguments")
            connection_id = safe_text(args["connection_id"])
            cursor = args.get("cursor")
            page_cursor = None if cursor is None else safe_text(cursor, maximum=8192)
            kind = "drive_file" if resource_type is None else safe_text(resource_type)
            client = self.runtime.client(provider)
            if isinstance(client, DriveSourceClient):
                page = client.resources(connection_id, cursor=page_cursor, resource_type=kind)
            else:
                page = client.resources(connection_id, cursor=page_cursor)
            return {
                "resources": [item.to_dict() for item in page.resources],
                "next_cursor": page.next_cursor,
            }
```

No import changes: `DriveSourceClient`, `LiveSourceError` and `safe_text` are already imported.
`kind` is only forwarded on the Drive branch; the Gmail and Slack calls are byte-for-byte the same
as before. An unknown `kind` (for example `drive_account`) is left to the client, which raises
`LiveSourceError("drive_invalid_selection")` per Package 1.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --frozen pytest packages/collector/tests/integration/test_live_manager.py -q`

Expected: PASS (the three pre-existing tests in the module and the new one).

- [ ] **Step 5: Commit**

Write this message to a file outside the repository and commit with `git commit -F <file>`:

```
feat(collector): let sources.resources pass a Drive resource_type

sources.resources accepts an optional resource_type. For google_drive it
defaults to drive_file and is forwarded to DriveSourceClient.resources; for
gmail and slack a present value is source_invalid_arguments before any client
is built.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

```bash
git add packages/collector/src/open_brain_collector/live_manager.py \
  packages/collector/tests/integration/test_live_manager.py
git commit -F <file>
```

---

### Task 13: `resources --resource-type` on the collector CLI

**Files:**
- Modify: `packages/collector/src/open_brain_collector/sources_cli.py:299-302` (parser) and `:405-431` (`_command_arguments`)
- Create: `packages/collector/tests/integration/test_sources_cli.py`

**Interfaces:**
- Consumes: Task 12's `sources.resources` contract (`resource_type` optional, Drive-only); `sources_cli.main(argv) -> int`; `sources_cli.LiveSourceManager` (monkeypatch target, as `test_source_metadata.py:742`).
- Produces: `open-brain-collector sources ... resources --provider P --connection-id ID [--cursor C] [--resource-type {drive_file,drive_folder}]`. With `--provider google_drive` and no flag, the dispatched arguments carry `"resource_type": "drive_file"`. With another provider and no flag, no `resource_type` key is sent. Any value outside the two choices is an argparse error (exit 2). A flag with another provider reaches the manager and fails with `{"outcome": "failed", "failure_code": "source_invalid_arguments"}`, exit 78.

- [ ] **Step 1: Write the failing test**

Create `packages/collector/tests/integration/test_sources_cli.py`:

```python
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast

import pytest

from open_brain.profile import compile_single_user_local
from open_brain_collector import sources_cli


def _cli(argv: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = sources_cli.main(argv)
    return code, cast(dict[str, object], json.loads(output.getvalue()))


def test_resources_resource_type_defaults_to_drive_file_and_forwards_drive_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Manager:
        def __init__(self, *_args: object) -> None:
            pass

        def dispatch(self, operation: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((operation, arguments))
            return {"resources": [], "next_cursor": None}

    monkeypatch.setattr(sources_cli, "LiveSourceManager", Manager)
    base = [
        "--state",
        str(tmp_path / "state.json"),
        "--brain-root",
        str(tmp_path / "brain"),
        "--foreground",
        "resources",
        "--connection-id",
        "account:abc",
    ]
    assert _cli([*base, "--provider", "google_drive"]) == (
        0,
        {"resources": [], "next_cursor": None},
    )
    folders = [*base, "--provider", "google_drive", "--resource-type", "drive_folder"]
    assert _cli([*folders, "--cursor", "page-2"])[0] == 0
    assert _cli([*base, "--provider", "gmail"])[0] == 0
    assert calls == [
        (
            "sources.resources",
            {
                "provider": "google_drive",
                "connection_id": "account:abc",
                "resource_type": "drive_file",
            },
        ),
        (
            "sources.resources",
            {
                "provider": "google_drive",
                "connection_id": "account:abc",
                "cursor": "page-2",
                "resource_type": "drive_folder",
            },
        ),
        ("sources.resources", {"provider": "gmail", "connection_id": "account:abc"}),
    ]
    # drive_account is a fixed id, never a listing kind: argparse refuses it.
    with pytest.raises(SystemExit):
        sources_cli.main([*base, "--provider", "google_drive", "--resource-type", "drive_account"])
    assert len(calls) == 3


def test_resources_resource_type_is_rejected_for_other_providers(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    state = brain / "collector" / "state.json"
    code, result = _cli(
        [
            "--state",
            str(state),
            "--brain-root",
            str(brain),
            "--foreground",
            "resources",
            "--provider",
            "gmail",
            "--connection-id",
            "account:abc",
            "--resource-type",
            "drive_folder",
        ]
    )
    assert (code, result) == (
        78,
        {"outcome": "failed", "failure_code": "source_invalid_arguments"},
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest packages/collector/tests/integration/test_sources_cli.py -q`

Expected: both tests FAIL with `SystemExit: 2` raised from `parser.parse_args` (argparse:
`unrecognized arguments: --resource-type drive_folder`).

- [ ] **Step 3: Write the minimal implementation**

In `packages/collector/src/open_brain_collector/sources_cli.py`, replace the `resources` cursor
line inside the `for name in ("accounts", "connect", "disconnect", "resources"):` loop
(currently lines 301-302):

```python
        if name == "resources":
            item.add_argument("--cursor")
            item.add_argument(
                "--resource-type",
                choices=("drive_file", "drive_folder"),
                help="kind of Drive resource to list (google_drive only; default drive_file)",
            )
```

Then, in `_command_arguments`, add the Drive default directly after `result = {...}` and before
the `if args.command == "slack-policy-setup":` block:

```python
    if (
        args.command == "resources"
        and args.provider == "google_drive"
        and result.get("resource_type") is None
    ):
        result["resource_type"] = "drive_file"
```

The full function after the edit:

```python
def _command_arguments(args: argparse.Namespace) -> dict[str, object]:
    values = vars(args)
    ignored = {
        "state",
        "brain_root",
        "guide_guard_fd",
        "foreground",
        "command",
        "arguments_file",
        "continue_without_keywords",
        "lookback_hours",
        "proposal_opt_in",
        "proposal_opt_out",
    }
    result = {
        key: value for key, value in values.items() if key not in ignored and value is not None
    }
    if (
        args.command == "resources"
        and args.provider == "google_drive"
        and result.get("resource_type") is None
    ):
        result["resource_type"] = "drive_file"
    if args.command == "slack-policy-setup":
        if args.keywords is None and args.continue_without_keywords:
            result["keywords"] = []
        if args.lookback_hours is not None:
            result["lookback_seconds"] = args.lookback_hours * 3_600
        if args.proposal_opt_in:
            result["proposal_opt_in"] = True
        elif args.proposal_opt_out:
            result["proposal_opt_in"] = False
    return cast(dict[str, object], result)
```

A `None` default keeps `gmail` and `slack` requests identical to today; an explicit flag with those
providers is forwarded so the manager's `source_invalid_arguments` rule from Task 12 answers it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest packages/collector/tests/integration/test_sources_cli.py packages/collector/tests/integration/test_source_metadata.py -q`

Expected: PASS. `test_source_metadata.py` is included because it drives the same parser and
`_command_arguments`.

- [ ] **Step 5: Commit**

```
feat(collector): add --resource-type to the sources resources command

resources --resource-type {drive_file,drive_folder} lists Drive folders so an
owner can pick a drive_folder id. The CLI fills in drive_file for google_drive
when the flag is absent and never sends the key for other providers; an
explicit flag with gmail or slack fails as source_invalid_arguments.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

```bash
git add packages/collector/src/open_brain_collector/sources_cli.py \
  packages/collector/tests/integration/test_sources_cli.py
git commit -F <file>
```

---

### Task 14: Drive guide: Docs-set sections, corrected `drive_file` edit statement and the live acceptance gate

**Files:**
- Modify: `docs/integrations/google-sources.md:77-79` (discovery and selection paragraph), `:81-86` (append the corrected edit paragraph after it), insert new `## Drive Docs sets` section between `:94` and `## Live acceptance and limits` (`:96`), append `### Live acceptance for Drive Docs sets` at the end of that section (after `:114`, before `## References`)
- Create: `tests/release/test_drive_docs_sets_docs.py`

**Interfaces:**
- Consumes: the spec's Selection, Checkpoint, Fetch, Splitting, Revision-aware intake, Failure behavior, Bounds and Acceptance sections (values copied verbatim); the notice and error strings produced by Package 1 (`drive_file_skipped:<reason>:<fileId>`, `drive_folder_missing`, `drive_invalid_options`, `drive_invalid_selection`, `drive_invalid_checkpoint`) and Package 2 (`drive-revisions`, `source_revision_conflict` for `drive_file`).
- Produces: the pinned statements the docs test checks: `An edited \`drive_file\` record is quarantined, not re-imported.`, the `## Drive Docs sets` section with sub-headings `Shapes and options`, `Listing query`, `Checkpoint`, `Fetch and per-file notices`, `Splitting`, `Revision behavior`, and `### Live acceptance for Drive Docs sets`. Task 15 extends the same test module.

- [ ] **Step 1: Write the failing test**

Create `tests/release/test_drive_docs_sets_docs.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest tests/release/test_drive_docs_sets_docs.py -q`

Expected: FAIL with `AssertionError: ['## Drive Docs sets', '### Shapes and options', ...]` (the
`missing` list).

- [ ] **Step 3: Edit `docs/integrations/google-sources.md`**

Edit A. Replace the discovery paragraph (currently lines 77-79):

```markdown
Resource discovery lists files the account can read from My Drive and shared drives. Select
exactly one file. Fetch gets that file only; it does not recursively mirror folders. The source supports UTF-8 `text/*` blobs and
Google Workspace Docs, Sheets, and Slides exported as plain text or CSV where applicable.
```

with:

```markdown
Resource discovery lists files the account can read from My Drive and shared drives, or folders
when the collector's `resources --resource-type drive_folder` is used. A Drive selection has one
of three shapes: one file (`drive_file`), one folder of Google Docs (`drive_folder`), or Google
Docs across the account chosen by title suffix (`drive_account`). The single-file shape supports
UTF-8 `text/*` blobs and Google Workspace Docs, Sheets, and Slides exported as plain text or CSV
where applicable; the folder and account shapes capture Google Docs only. No shape narrows the
`drive.readonly` grant. A shape narrows what Open Brain imports.
```

Edit B. Directly after the paragraph that ends "while preserving already captured history."
(currently line 86), insert a new paragraph:

```markdown
An edited `drive_file` record is quarantined, not re-imported. The single-file shape submits each
record under a stable delivery id with a `Source revision:` text prefix and no revision
observation, so a changed body reaches the engine as a revision conflict; the collector records
the receipt as `quarantined` with `source_revision_conflict` and the first capture stays
searchable. Only the folder and account shapes described below go through the revision-aware
path. The 2026-09-16 live body-edit check ran on an older schema and does not describe the current
engine (gotcha DOCS-005).
```

Edit C. Directly after the paragraph that ends "other binary content as text." (currently line
94) and before `## Live acceptance and limits`, insert the new section:

````markdown
## Drive Docs sets

The folder and account shapes capture the Google Docs that Gemini writes after a Google Meet call,
the notes Doc and the transcript Doc, and any other folder of Docs. Both shapes list Docs, export
them as plain text, split long Docs into parts and store an edited Doc as a new revision of the
same record.

### Shapes and options

| Shape | `resource_type` | `resource_id` | Options |
|---|---|---|---|
| Folder | `drive_folder` | `drive_folder:<folderId>` | `date_floor` required, `title_suffixes` optional |
| Account | `drive_account` | `drive_account:all` | `date_floor` required, `title_suffixes` required |

- `date_floor` is a UTC calendar date, `YYYY-MM-DD`, in the same form Gmail takes. It bounds the
  first run: nothing modified before it is listed. It has no effect once the checkpoint is
  incremental.
- `title_suffixes` is a JSON list of 1 to 8 strings, each 1 to 64 characters after stripping, with
  no duplicates and no NUL. A Doc is selected when its name ends with one of them. The Meet values
  are `["Notes by Gemini", "Transcript"]`.
- Any other option key, a missing required option or a malformed value is `drive_invalid_options`.
  A resource id that does not match its type is `drive_invalid_selection`.
- `drive_account:all` is a fixed id and is not listed by discovery. Folder ids come from
  `resources --resource-type drive_folder`.

### Listing query

Each page is one `files.list` call:

```
mimeType = 'application/vnd.google-apps.document'
and trashed = false
and modifiedTime > '<bound>'
[and '<folderId>' in parents]
[and (name contains '<s1>' or name contains '<s2>' ...)]
```

The folder clause applies to the folder shape. The name clause applies when `title_suffixes` is
set; it is a server-side prefilter, and the exact suffix check runs on the returned name. Suffix
values are escaped for the Drive query grammar (backslash and single quote). The request uses
`orderBy=modifiedTime`, `pageSize=100`, `includeItemsFromAllDrives=true` and
`supportsAllDrives=true`. Recordings, PDFs and other types never match the MIME clause, so a Docs
set raises no `drive_unsupported_type` notices.

### Checkpoint

The checkpoint is metadata only and is validated on every fetch:

```json
{"mode": "backfill",
 "page_token": null,
 "watermark": "2026-10-01T00:00:00Z",
 "known": {"<fileId>": {"revision": "<revision>", "modified": "<RFC3339 modifiedTime>"}}}
```

- The first run is `backfill` with `watermark` set to `<date_floor>T00:00:00Z`, and the query bound
  is the watermark for the whole listing, because a Drive page token is bound to its query.
  `page_token` advances when a page is fully drained. When the last page drains without a
  `nextPageToken`, the watermark moves to the newest `modified` time recorded in `known` and `mode`
  becomes `incremental`.
- An incremental run queries from `watermark` minus 24 hours. The overlap covers late indexing and
  clock skew. Files in `known` with an unchanged revision are skipped without a fetch.
- `known` holds at most 500 entries. When it is full, the entries with the oldest `modified`
  value are dropped. A dropped entry risks one re-submit, which the sink answers as `duplicate`.
- Any other shape, an unknown key or more than 500 entries is `drive_invalid_checkpoint`. A
  `"reset": true` on configure clears it.

### Fetch and per-file notices

One fetch lists one page, then, for each file whose revision is new or changed, reads the
metadata, exports the body and splits it. A batch holds at most 25 intakes and 25 notices. Files
not reached stay on the same page for the next call, and `has_more` is true while files on the
page remain or a `nextPageToken` is present.

A single file never stalls a set. A file that returns 404, a lost-access 403, `trashed`,
`canDownload != true`, a non-UTF-8 body, a body of more than 18 parts or an empty body produces the notice
`drive_file_skipped:<reason>:<fileId>` with `reason` in
`missing|access_lost|trashed|not_downloadable|not_text|too_large|empty`; the file is recorded in
`known` and the run continues. Account-level errors still raise and leave the checkpoint
unchanged: 401 is `google_auth_required`, a rate-limit 403 is `google_rate_limited`, and any other
403 on the list call is `drive_forbidden`. When the folder itself returns 404 or lost access, the
fetch returns no intakes with the notice `drive_folder_missing` and the checkpoint unchanged; no
placeholder record is written.

### Splitting

An exported text over 60,000 characters is split into parts of at most 60,000 characters. The cut
is the last blank line within the limit, else the last newline, else a hard cut; leading newlines
of the next part are stripped. At most 18 parts are produced. A Doc that would need a 19th is
skipped with `drive_file_skipped:too_large:<fileId>`. Each part is one record. Its `external_id`
is `file:<fileId>#part:<n>` (1-based). Its title is `<name> (part <n> of <N>)`, cut to
200 characters with the suffix preserved. Its link is the Doc link, and every part carries the
same `revision:<drive revision>`. A Doc under the limit is one record with `external_id`
`file:<fileId>`, as for the single-file shape.

### Revision behavior

Folder and account records go through the collector's revision-aware path, as Slack records do.
The collector attests each intake and stores it under `drive-revisions` in its state root. When a
Doc is edited, its revision hash changes, the file is re-exported and re-split, and each part is
submitted as a new revision of the same source record: the earlier text stays in history and the
new text is searchable. Follow-up evidence quoted from an earlier revision stays bound to that
revision. When a later revision has fewer parts, the surplus parts keep their last revision; this
is documented, not repaired.
````

Edit D. At the end of `## Live acceptance and limits`, after the paragraph that ends "Plan
equivalent reconnection before testing upstream revocation." (currently line 114) and before
`## References`, insert:

```markdown
### Live acceptance for Drive Docs sets

The Docs-set live gate is owner-authorized and separate from the automated suite. It needs a
development OAuth client, a consenting test account and a disposable Brain, and its receipts stay
outside Git. The gate passes when three checks are recorded here: one real Gemini note captured
through `drive_account:all` with the suffixes above, one real transcript over 60,000 characters
captured as parts, and one edit to the note stored as a new revision with the first text still in
history. The same run answers the open question on Gemini's title format: if real titles differ
from `Notes by Gemini` and `Transcript`, only the `title_suffixes` values change. Until that run is
recorded, the Docs-set shapes have synthetic acceptance only, through the collector integration
tests.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --frozen pytest tests/release/test_drive_docs_sets_docs.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```
docs(integrations): document Drive Docs sets and the quarantined drive_file edit

The Drive guide gains the folder and account shapes, their options, listing
query, checkpoint, per-file notices, splitting and revision behavior, plus the
owner-only live acceptance gate. It now states that an edited drive_file is
quarantined on the current engine. A release docs test pins the statements.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

```bash
git add docs/integrations/google-sources.md tests/release/test_drive_docs_sets_docs.py
git commit -F <file>
```

---

### Task 15: Meet-notes walkthrough, product boundary sentence, gotcha DOCS-005 and the final gates

**Files:**
- Modify: `docs/integrations/priority-capture-operator.md:45-47` (selection paragraph), insert `## Capture Gemini Meet notes from Drive` between `:76` and `## Recurring collection` (`:78`)
- Modify: `CLAUDE.md:32-33` (Product boundary paragraph)
- Modify: `docs/engineering/gotchas/README.md:5-7` (insert `DOCS-005` directly under `## Registry`, above `BUILD-021`)
- Modify: `tests/release/test_drive_docs_sets_docs.py` (full rewrite below)

**Interfaces:**
- Consumes: Task 13's `--resource-type drive_folder` flag; Task 14's guide sections (the walkthrough links to them); Package 1's `drive_account:all` fixed id and `drive_folder:<id>` listing; Package 2's `drive_file` quarantine behavior.
- Produces: the exact `configure --arguments-file` JSON for `drive_account:all` with `["Notes by Gemini", "Transcript"]` and `"date_floor": "2026-10-01"` (the docs test parses it); the `CLAUDE.md` sentence `Drive capture selects a file, a folder of Docs, or account Docs by title suffix.` on one line; gotcha `DOCS-005`.

- [ ] **Step 1: Rewrite the docs test with the new assertions**

Replace the whole of `tests/release/test_drive_docs_sets_docs.py` with:

```python
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/integrations/google-sources.md"
OPERATOR = ROOT / "docs/integrations/priority-capture-operator.md"
CONTRIBUTOR_GUIDE = ROOT / "CLAUDE.md"
GOTCHAS = ROOT / "docs/engineering/gotchas/README.md"
PROPER_NOUNS = ("Open", "Brain", "Google", "Drive", "Docs", "Gemini", "Meet", "Gmail", "Slack")
TELLS = ("not just", "seamless", "robust", "powerful", "effortless", "dive in")
FENCE = chr(96) * 3
MEET_SELECTION = {
    "selection": {
        "connector_name": "google_drive",
        "connection_id": "account:REPLACE_FROM_ACCOUNTS",
        "resource_id": "drive_account:all",
        "resource_type": "drive_account",
    },
    "options": {
        "date_floor": "2026-10-01",
        "title_suffixes": ["Notes by Gemini", "Transcript"],
    },
}


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


def _json_block_containing(text: str, needle: str) -> object:
    # FENCE is built from chr(96) so this file never contains a literal Markdown fence.
    for block in text.split(FENCE + "json")[1:]:
        body = block.split(FENCE, 1)[0]
        if needle in body:
            parsed: object = json.loads(body)
            return parsed
    raise AssertionError(f"no fenced json block contains {needle!r}")


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


def test_operator_guide_walks_through_meet_notes_with_the_exact_arguments_file() -> None:
    text = OPERATOR.read_text(encoding="utf-8")
    required = (
        "## Capture Gemini Meet notes from Drive",
        "resources --provider google_drive --connection-id account:REPLACE_FROM_ACCOUNTS",
        "--resource-type drive_folder",
        "`drive_file` (the default)",
        "`drive_folder:<id>`",
        "`drive_account:all` is a fixed id",
        "--foreground configure --arguments-file /absolute/path/to/meet-notes.json",
        "--foreground preview --source-id REPLACE_FROM_CONFIGURE",
        "import --source-id REPLACE_FROM_CONFIGURE --preview-id REPLACE_FROM_PREVIEW",
        "--action enable --interval-seconds 900",
        "`<name> (part <n> of <N>)`",
        "new revision of the same record",
        "`source_revision_conflict`",
        "[Google setup](google-sources.md)",
        "A `drive_file` selection takes an empty options object.",
    )
    missing = [token for token in required if token not in text]
    assert not missing, missing
    assert "without folder recursion" not in text
    assert _json_block_containing(text, '"drive_account:all"') == MEET_SELECTION
    # The walkthrough comes after the generic select/preview/import section and before recurring
    # collection, so a reader meets configure, preview and import once before the Drive variant.
    assert (
        text.index("## Select, preview and import")
        < text.index("## Capture Gemini Meet notes from Drive")
        < text.index("## Recurring collection")
    )
    _check_headings_and_tone(text)


def test_contributor_guide_states_the_drive_selection_shapes() -> None:
    text = CONTRIBUTOR_GUIDE.read_text(encoding="utf-8")
    sentence = "Drive capture selects a file, a folder of Docs, or account Docs by title suffix."
    assert text.count(sentence) == 1
    boundary = text.index("## Product boundary")
    assert boundary < text.index(sentence) < text.index("## Architecture")
    # The statements the product-split suite pins are still present.
    assert "does not install a desktop app, collector, scheduler" in text
    assert "Open Brain never requires root" in text


def test_gotcha_registry_records_the_drive_file_edit_contradiction() -> None:
    text = GOTCHAS.read_text(encoding="utf-8")
    assert text.count("### DOCS-005:") == 1
    entry = text[text.index("### DOCS-005:") :]
    entry = entry[: entry.index("\n### ", 1)]
    for token in (
        "Category: Documentation / Google Drive.",
        "Symptom:",
        "Action:",
        "Discovered: 2026-10-25",
        "`drive_file`",
        "`Source revision:`",
        "`source_revision_conflict`",
        "`9f22849`",
        "`drive_folder`",
        "`drive_account`",
        "`drive-revisions`",
        "docs/integrations/google-sources.md",
    ):
        assert token in entry, token
    # Newest entries go first; the new entry sits above the previous top entry.
    assert text.index("### DOCS-005:") < text.index("### BUILD-021:")
    assert "/Users/" not in entry
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest tests/release/test_drive_docs_sets_docs.py -q`

Expected: the first test PASSES (Task 14) and the other three FAIL: the operator test with
`AssertionError: ['## Capture Gemini Meet notes from Drive', ...]`, the contributor-guide test with
`assert 0 == 1`, the gotcha test with `assert 0 == 1`.

- [ ] **Step 3: Edit `docs/integrations/priority-capture-operator.md`**

Edit A. Replace the selection paragraph (currently lines 45-47):

```markdown
Save one selection in a local arguments file. Use the exact connection and resource IDs returned
by discovery. Gmail's date floor is a calendar date. Slack's date floor is RFC 3339 with a timezone.
Drive options are an empty object and its selection is one file, without folder recursion.
```

with:

```markdown
Save one selection in a local arguments file. Use the exact connection and resource IDs returned
by discovery. Gmail's date floor is a calendar date. Slack's date floor is RFC 3339 with a timezone.
A `drive_file` selection takes an empty options object. Drive folder and account sets take
`date_floor` and `title_suffixes`; see the Meet-notes walkthrough below.
```

Edit B. Directly after the paragraph that ends "After cache eviction, the engine still enforces
stable delivery identity." (currently line 76) and before `## Recurring collection`, insert:

````markdown
## Capture Gemini Meet notes from Drive

Gemini writes a notes Doc and a transcript Doc into Drive after a Google Meet call. Notes for
meetings other people organized reach the account as shared files and never sit in the owner's
`Meet Recordings` folder, so the account shape with the two Meet title suffixes captures both the
owner's and the shared Docs. To capture one folder instead, list folders first and use its id:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground resources --provider google_drive --connection-id account:REPLACE_FROM_ACCOUNTS \
  --resource-type drive_folder
```

`--resource-type` accepts `drive_file` (the default) and `drive_folder`, for `google_drive` only.
Each listed folder is `drive_folder:<id>`; use it as the `resource_id` with
`"resource_type": "drive_folder"`. `drive_account:all` is a fixed id and is not listed.

Save the account selection with the two suffixes and a date floor. Nothing modified before the
date floor is listed:

```json
{
  "selection": {
    "connector_name": "google_drive",
    "connection_id": "account:REPLACE_FROM_ACCOUNTS",
    "resource_id": "drive_account:all",
    "resource_type": "drive_account"
  },
  "options": {
    "date_floor": "2026-10-01",
    "title_suffixes": ["Notes by Gemini", "Transcript"]
  }
}
```

Then configure, preview, import and enable with the same commands as any other source:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground configure --arguments-file /absolute/path/to/meet-notes.json
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground preview --source-id REPLACE_FROM_CONFIGURE
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground import --source-id REPLACE_FROM_CONFIGURE --preview-id REPLACE_FROM_PREVIEW
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground control --source-id REPLACE_FROM_CONFIGURE --action enable --interval-seconds 900
```

The first preview backfills from the date floor; later runs are incremental. A transcript over
60,000 characters previews and imports as parts titled `<name> (part <n> of <N>)`. An edited Doc
becomes a new revision of the same record. A `drive_file` selection does not get this behavior:
an edited single file is quarantined with `source_revision_conflict`. Options, the checkpoint,
splitting and the per-file notices are described in [Google setup](google-sources.md).
````

- [ ] **Step 4: Edit `CLAUDE.md`**

In the Product boundary paragraph, replace the line (currently line 33):

```markdown
and the collector CLI exposes the source operations headlessly.
```

with these two lines:

```markdown
and the collector CLI exposes the source operations headlessly.
Drive capture selects a file, a folder of Docs, or account Docs by title suffix.
```

Nothing else in the file changes. Keep the sentence on one line so the docs test and future
greps find it.

- [ ] **Step 5: Add gotcha `DOCS-005`**

In `docs/engineering/gotchas/README.md`, directly after the `## Registry` heading and its blank
line (currently lines 5-6) and before `### BUILD-021: ...`, insert:

```markdown
### DOCS-005: The Drive guide said an edited single file re-imports; the engine quarantines it

Category: Documentation / Google Drive.

Symptom: `docs/integrations/google-sources.md` and the 2026-09-16 live acceptance
record described a Drive body edit as a re-import of the selected file. On the
current engine that is false for the `drive_file` shape. The collector submits a
`drive_file` intake under a stable delivery id with a `Source revision:` text
prefix and no revision observation, so a changed body reaches the engine as a
`DeliveryConflict`; the collector records the receipt as `quarantined` with
`source_revision_conflict` and the first capture stays searchable. The live
"body edit" PASS at `9f22849` ran before the schema moved past `user_version 7`,
where the legacy replacement rule still applied. No Drive test on the current
schema pinned the behavior until the Docs-set work added one.

Action: the Drive guide now states that an edited `drive_file` is quarantined,
and only the `drive_folder` and `drive_account` shapes go through the
collector's revision-aware path (`observe_revision` and `drive-revisions`).
When a guide statement rests on a dated live receipt, re-check it against the
current engine path before repeating it; an acceptance PASS certifies the schema
it ran on, not later ones. Do not route `drive_file` through the revision sink to
"fix" the docs: existing records would turn their first edit into a conflict
against the stored capture.

Discovered: 2026-10-25, Drive Docs sets design review.

```

(The trailing blank line keeps one empty line between this entry and `### BUILD-021`.)

- [ ] **Step 6: Run the docs test and the suites this package touched**

Run:

```bash
uv run --frozen pytest tests/release/test_drive_docs_sets_docs.py -q
uv run --frozen pytest tests/security/test_open_brain_product_split.py tests/release/test_documentation_examples.py -q
uv run --frozen pytest packages/collector/tests/integration/test_live_manager.py packages/collector/tests/integration/test_sources_cli.py packages/collector/tests/integration/test_source_metadata.py -q
```

Expected: PASS for all three commands (4 tests in the docs module; the product-split and
documentation-examples suites unchanged; the collector suites from Tasks 12 and 13).

- [ ] **Step 7: Run the package gates**

Run:

```bash
make lint
make typecheck
git diff --check
uv run --frozen pytest tests/release/test_drive_docs_sets_docs.py -q
```

Expected: `make lint` prints `All checks passed!`; `make typecheck` prints `Success: no issues
found in N source files`; `git diff --check` prints nothing and exits 0; the docs test prints
`4 passed`. If `make lint` reports an `E501` on a test line, wrap that line; do not change the
ruff configuration.

- [ ] **Step 8: Commit**

```
docs: Meet-notes walkthrough, Drive boundary sentence and gotcha DOCS-005

The operator guide walks through listing Drive folders, configuring
drive_account:all with the two Gemini suffixes and a date floor, preview,
import and enable. CLAUDE.md states the three Drive selection shapes.
DOCS-005 records that the guide described a drive_file edit as a re-import
while the current engine quarantines it. The release docs test pins all of it.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

```bash
git add docs/integrations/priority-capture-operator.md CLAUDE.md \
  docs/engineering/gotchas/README.md tests/release/test_drive_docs_sets_docs.py
git commit -F <file>
```
