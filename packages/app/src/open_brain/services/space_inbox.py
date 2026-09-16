"""Shared, bounded owner organization operations for the CLI and granted MCP tools."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256
from typing import cast

from open_brain_engine.engine.contracts import InboxSpaceTask

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_PAGE_OFFSET = 1_000_000
MAX_SPACE_NAME_CHARACTERS = 120
MAX_IDEMPOTENCY_KEY_CHARACTERS = 128
# MCP emits both structured data and an escaped JSON text copy. Even a threefold
# expansion of this ASCII JSON budget fits its 1 MiB response envelope.
MAX_PAGE_JSON_BYTES = 250_000
_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_REQUIRED = {
    "inbox_list": set(),
    "space_list": set(),
    "space_create": {"name"},
    "space_rename": {"space_id", "name"},
    "inbox_route": {"capture_id", "space_id"},
}


class SpaceInboxError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_space_inbox_arguments(operation: str, arguments: Mapping[str, object]) -> None:
    required = _REQUIRED.get(operation)
    if required is None:
        raise SpaceInboxError("invalid_arguments")
    optional = {"limit", "offset"} if operation.endswith("_list") else {"idempotency_key"}
    if operation == "inbox_list":
        optional.add("unassigned_only")
    if required - set(arguments) or set(arguments) - (required | optional):
        raise SpaceInboxError("invalid_arguments")
    for field, value in arguments.items():
        if field in {"limit", "offset"}:
            minimum, maximum = (1, MAX_PAGE_LIMIT) if field == "limit" else (0, MAX_PAGE_OFFSET)
            if type(value) is not int or not minimum <= value <= maximum:
                raise SpaceInboxError("invalid_arguments")
        elif field == "unassigned_only":
            if type(value) is not bool:
                raise SpaceInboxError("invalid_arguments")
        elif field in {"space_id", "capture_id"}:
            prefix = field.removesuffix("_id")
            if not isinstance(value, str) or not re.fullmatch(prefix + "_" + _UUID4, value):
                raise SpaceInboxError("invalid_arguments")
        else:
            maximum = (
                MAX_SPACE_NAME_CHARACTERS if field == "name" else MAX_IDEMPOTENCY_KEY_CHARACTERS
            )
            normalized = _name(value) if field == "name" and isinstance(value, str) else value
            if (
                not isinstance(normalized, str)
                or not 1 <= len(normalized) <= maximum
                or not normalized.strip()
                or "\x00" in normalized
                or (field == "name" and any(
                    unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}
                    for character in normalized
                ))
            ):
                raise SpaceInboxError("invalid_arguments")
            try:
                normalized.encode("utf-8")
            except UnicodeError:
                raise SpaceInboxError("invalid_arguments") from None


class SpaceInboxService:
    def __init__(self, task: InboxSpaceTask) -> None:
        self._task = task

    def inbox_list(self, arguments: Mapping[str, object]) -> dict[str, object]:
        validate_space_inbox_arguments("inbox_list", arguments)
        limit = cast(int, arguments.get("limit", DEFAULT_PAGE_LIMIT))
        offset = cast(int, arguments.get("offset", 0))
        items = self._task.list(
            unassigned_only=cast(bool, arguments.get("unassigned_only", False)),
            limit=limit + 1,
            offset=offset,
        )
        return _page_result("items", [asdict(item) for item in items], limit, offset)

    def space_list(self, arguments: Mapping[str, object]) -> dict[str, object]:
        validate_space_inbox_arguments("space_list", arguments)
        limit = cast(int, arguments.get("limit", DEFAULT_PAGE_LIMIT))
        offset = cast(int, arguments.get("offset", 0))
        spaces = self._task.spaces(limit=limit + 1, offset=offset)
        return _page_result("spaces", [asdict(space) for space in spaces], limit, offset)

    def space_create(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._write("space_create", arguments)

    def space_rename(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._write("space_rename", arguments)

    def inbox_route(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._write("inbox_route", arguments)

    def _write(self, operation: str, arguments: Mapping[str, object]) -> dict[str, object]:
        validate_space_inbox_arguments(operation, arguments)
        key = arguments.get("idempotency_key")
        delivery_id = (
            "organization." + operation + "." + sha256(cast(str, key).encode("utf-8")).hexdigest()
            if key is not None else "delivery." + str(uuid.uuid4())
        )
        try:
            if operation == "inbox_route":
                routed = self._task.route(
                    cast(str, arguments["capture_id"]), cast(str, arguments["space_id"]),
                    delivery_id=delivery_id,
                )
                return {"status": "routed", **asdict(routed)}
            name = _name(cast(str, arguments["name"]))
            space = (
                self._task.create_space(name, delivery_id=delivery_id)
                if operation == "space_create"
                else self._task.rename_space(
                    cast(str, arguments["space_id"]), name, delivery_id=delivery_id
                )
            )
            return {"status": "created" if operation == "space_create" else "renamed",
                    "space": asdict(space)}
        except ValueError as error:
            code = {
                "conflicting delivery": "idempotency_conflict",
                "unknown space": "unknown_space",
                "unknown route target": "unknown_route_target",
                "published capture cannot be rerouted": "published_capture",
            }.get(str(error))
            if code is None:
                raise
            raise SpaceInboxError(code) from None


def _name(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _page_result(
    key: str, rows: list[dict[str, object]], limit: int, offset: int
) -> dict[str, object]:
    selected: list[dict[str, object]] = []
    size = 1024  # Leave space for keys, commas and page metadata.
    for row in rows[:limit]:
        row_size = len(json.dumps(row, ensure_ascii=True, separators=(",", ":"))) + 1
        if size + row_size > MAX_PAGE_JSON_BYTES:
            break
        selected.append(row)
        size += row_size
    more = len(rows) > len(selected)
    following = offset + len(selected)
    capped = more and following > MAX_PAGE_OFFSET
    result: dict[str, object] = {
        "status": "listed", key: selected, "offset": offset,
        "next_offset": following if more and not capped else None,
    }
    if capped:
        result["offset_limit_reached"] = True
    return result
