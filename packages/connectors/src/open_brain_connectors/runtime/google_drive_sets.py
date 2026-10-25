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
MAX_KNOWN = 2_000
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
