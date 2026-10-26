"""Selection, checkpoint, listing-query and splitting rules for Google Drive Docs sets."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast

from open_brain_connectors.runtime.live_common import LiveSourceError, safe_text
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = [
    "DOCUMENT_MIME_TYPE",
    "FOLDER_MIME_TYPE",
    "INCREMENTAL_OVERLAP_SECONDS",
    "MAX_KNOWN",
    "MAX_PARTS",
    "MAX_PART_CHARS",
    "MAX_PART_JSON_BYTES",
    "MAX_SUFFIXES",
    "MAX_SUFFIX_CHARS",
    "MAX_TITLE_CHARS",
    "SET_RESOURCE_TYPES",
    "DriveKnownEntry",
    "DriveSetCheckpoint",
    "DriveSetSelection",
    "advance_watermark",
    "build_list_query",
    "escape_query_value",
    "evict_known",
    "format_timestamp",
    "parse_drive_set",
    "parse_timestamp",
    "part_title",
    "query_bound",
    "require_file_id",
    "split_document",
]

MAX_PART_CHARS = 60_000
# The collector attests each intake within a 131,072-byte JSON bound; leave room for the rest.
MAX_PART_JSON_BYTES = 120_000
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
        if type(mode) is not str or mode not in {"backfill", "incremental"}:
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
    timespec = "milliseconds" if moment.microsecond else "seconds"
    return moment.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


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


def _json_size(text: str) -> int:
    # The collector's bounded_json encodes with ASCII escapes; a string's size ignores separators.
    return len(json.dumps(text, ensure_ascii=True))


def _window_length(text: str) -> int:
    """Longest prefix within MAX_PART_CHARS whose JSON encoding fits MAX_PART_JSON_BYTES."""
    low, high = 1, min(len(text), MAX_PART_CHARS)
    if _json_size(text[:high]) <= MAX_PART_JSON_BYTES:
        return high
    while low < high:
        middle = (low + high + 1) // 2
        if _json_size(text[:middle]) <= MAX_PART_JSON_BYTES:
            low = middle
        else:
            high = middle - 1
    return low


def split_document(text: str) -> list[str]:
    """Split one exported Doc into at most MAX_PARTS parts within both part bounds."""
    parts: list[str] = []
    remaining = text.replace("\r\n", "\n").replace("\r", "\n")
    while remaining:
        if len(parts) == MAX_PARTS:
            raise LiveSourceError("drive_content_too_large")
        limit = _window_length(remaining)
        if limit == len(remaining):
            part, remaining = remaining, ""
        else:
            window = remaining[:limit]
            cut = window.rfind("\n\n")
            if cut <= 0:
                cut = window.rfind("\n")
            if cut <= 0:
                cut = limit
            part, remaining = window[:cut].rstrip("\n"), remaining[cut:].lstrip("\n")
        if part.strip():
            parts.append(part)
    return parts


def part_title(name: str, index: int, total: int) -> str:
    """`<name> (part <index> of <total>)` within MAX_TITLE_CHARS, suffix preserved."""
    suffix = f" (part {index} of {total})"
    return f"{name[: MAX_TITLE_CHARS - len(suffix)].rstrip()}{suffix}"


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
