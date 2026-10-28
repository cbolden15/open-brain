"""Closed compact-search v1 values, independent of the frozen T03 grammar."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .t03_contracts import SearchPageRequest, T03Error, _freeze, _thaw, parse_wire, validate_wire

_FIELDS = frozenset(
    {
        "dto_version",
        "query",
        "filters",
        "mode",
        "group_by",
        "limit",
        "target_bytes",
        "count_only",
        "cursor",
    }
)
_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactSearchPageRequest:
    query: str
    dto_version: int = 1
    filters: Mapping[str, tuple[str, ...] | list[str]] = field(
        default_factory=lambda: {"space_ids": [], "payload_families": [], "record_types": []}
    )
    mode: str = "lexical"
    group_by: str = "title"
    limit: int = 20
    target_bytes: int = 4096
    count_only: bool = False
    cursor: str | None = None

    def __post_init__(self) -> None:
        # Reuse the unchanged query, filters, modes, identifiers and integer rules.
        SearchPageRequest(
            dto_version=self.dto_version,
            query=self.query,
            filters=self.filters,
            mode=self.mode,
            limit=self.limit,
            cursor=self.cursor,
        )
        if (
            self.group_by not in ("title", "none")
            or type(self.group_by) is not str
            or type(self.target_bytes) is not int
            or not 2048 <= self.target_bytes <= 8192
            or type(self.count_only) is not bool
            or self.count_only
            and self.cursor is not None
        ):
            raise T03Error("invalid_arguments")
        object.__setattr__(self, "filters", _freeze(self.filters))
        try:
            if len(portable_canonical_json_bytes(self.to_wire())) > 65536:
                raise T03Error("invalid_arguments")
        except UnicodeError, TypeError, ValueError:
            raise T03Error("invalid_arguments") from None

    @classmethod
    def from_wire(cls, value: object) -> CompactSearchPageRequest:
        if (
            not isinstance(value, Mapping)
            or set(value) - _FIELDS
            or not {"dto_version", "query"} <= set(value)
        ):
            raise T03Error("invalid_arguments")
        try:
            return cls(**dict(value))
        except TypeError, ValueError, UnicodeError:
            raise T03Error("invalid_arguments") from None

    def to_wire(self) -> dict[str, Any]:
        return {
            "dto_version": self.dto_version,
            "query": self.query,
            "filters": _thaw(self.filters),
            "mode": self.mode,
            "group_by": self.group_by,
            "limit": self.limit,
            "target_bytes": self.target_bytes,
            "count_only": self.count_only,
            "cursor": self.cursor,
        }


def _validate_response(value: object) -> None:
    def require(condition: bool) -> None:
        if not condition:
            raise T03Error("invalid_arguments")

    def integer(number: object, minimum: int = 0) -> bool:
        return type(number) is int and minimum <= number <= _SAFE_INTEGER

    require(type(value) is dict)
    assert isinstance(value, dict)
    require(
        set(value)
        == {
            "status",
            "dto_version",
            "view",
            "group_by",
            "count_only",
            "mode_used",
            "count_scope",
            "total_matches",
            "total_groups",
            "results",
            "complete",
            "next_cursor",
            "warnings",
        }
    )
    require(
        value["status"] == "ok"
        and type(value["dto_version"]) is int
        and value["dto_version"] == 1
        and value["view"] == "compact"
    )
    require(value["group_by"] in ("title", "none"))
    require(type(value["count_only"]) is bool and type(value["complete"]) is bool)
    require(value["mode_used"] in ("lexical", "hybrid"))
    require(
        value["count_scope"]
        == ("hybrid_candidates" if value["mode_used"] == "hybrid" else "lexical_matches")
    )
    require(integer(value["total_matches"]) and integer(value["total_groups"]))
    require(value["total_groups"] <= value["total_matches"])
    require(value["warnings"] in ([], ["model_unavailable"]))
    require(value["mode_used"] != "hybrid" or not value["warnings"])
    validate_wire(
        "search.page.request",
        {"dto_version": 1, "query": "validation", "cursor": value["next_cursor"]},
    )
    require(value["complete"] == (value["next_cursor"] is None))
    require(type(value["results"]) is list and len(value["results"]) <= 100)
    references = 0
    for row in value["results"]:
        require(
            type(row) is dict
            and set(row)
            == {
                "group_id",
                "title",
                "title_truncated",
                "record_type",
                "payload_family",
                "space_id",
                "match_count",
                "member_offset",
                "members",
                "members_complete",
            }
        )
        require(
            type(row["group_id"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", row["group_id"]) is not None
        )
        require(
            type(row["title"]) is str
            and len(row["title"].encode("utf-8")) <= 256
            and "\0" not in row["title"]
        )
        require(type(row["title_truncated"]) is bool)
        require(type(row["members_complete"]) is bool)
        require(integer(row["match_count"], 1) and integer(row["member_offset"]))
        require(type(row["members"]) is list and 1 <= len(row["members"]) <= 100)
        require(row["member_offset"] + len(row["members"]) <= row["match_count"])
        require(
            row["members_complete"]
            == (row["member_offset"] + len(row["members"]) == row["match_count"])
        )
        if value["group_by"] == "none":
            require(row["match_count"] == 1 and row["member_offset"] == 0)
        validate_wire(
            "filters",
            {
                "space_ids": [] if row["space_id"] is None else [row["space_id"]],
                "record_types": [row["record_type"]],
                "payload_families": [row["payload_family"]],
            },
        )
        for member in row["members"]:
            require(type(member) is dict and set(member) == {"record_id", "revision_id"})
            validate_wire(
                "record.read.request",
                {
                    "dto_version": 1,
                    "record_id": member["record_id"],
                    "expected_revision_id": member["revision_id"],
                },
            )
            require(
                member["record_id"].startswith("capture_")
                and member["record_id"] == member["revision_id"]
                if row["record_type"] == "source"
                else member["record_id"].startswith("page_")
                and member["revision_id"].startswith("revision_")
            )
        references += len(row["members"])
    require(references <= 100 and references <= value["total_matches"])
    require(
        not value["count_only"]
        or (not value["results"] and value["complete"] and value["next_cursor"] is None)
    )
    require(value["complete"] or references > 0)
    require(
        value["total_matches"] != 0
        or (not value["results"] and value["complete"] and value["total_groups"] == 0)
    )


@dataclass(frozen=True, slots=True)
class CompactSearchPageResponse:
    encoded: bytes

    def __post_init__(self) -> None:
        try:
            _validate_response(parse_wire(self.encoded))
        except ValueError, TypeError, KeyError, UnicodeError:
            raise T03Error("invalid_arguments") from None

    @classmethod
    def from_wire(cls, value: object) -> CompactSearchPageResponse:
        try:
            _validate_response(value)
            return cls(portable_canonical_json_bytes(value))
        except ValueError, TypeError, KeyError, UnicodeError:
            raise T03Error("invalid_arguments") from None

    def to_wire(self) -> dict[str, Any]:
        return dict(json.loads(self.encoded))
