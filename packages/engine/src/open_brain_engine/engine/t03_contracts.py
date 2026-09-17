"""Engine-owned T03 values. Authority is supplied only by trusted session code."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, ClassVar, cast

from .t03_validation import CONTRACT, parse, validate


class T03Error(ValueError):
    def __init__(self, code: str) -> None:
        codes = CONTRACT["definitions"]["error"]["properties"]["error"]["properties"]["code"][
            "enum"
        ]
        if code not in codes:
            raise ValueError("invalid error code")
        self.code = code
        super().__init__(code)


def validate_wire(name: str, value: object) -> Any:
    try:
        return validate(name, value)
    except ValueError, TypeError, KeyError, UnicodeError:
        raise T03Error("invalid_arguments") from None


def parse_wire(raw: str | bytes) -> Any:
    try:
        if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > 65536:
            raise ValueError("request too large")
        return parse(raw)
    except ValueError, TypeError, UnicodeError:
        raise T03Error("invalid_arguments") from None


@dataclass(frozen=True, slots=True)
class EffectiveAuthority:
    principal_id: str
    session_id: str
    capabilities: frozenset[str]
    space_ids: frozenset[str] | None
    authorization_epoch: int = 0
    owner: bool = False

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not str
                or not 1 <= len(value) <= 256
                or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
                for value in (self.principal_id, self.session_id)
            )
            or type(self.capabilities) is not frozenset
            or self.space_ids is not None
            and type(self.space_ids) is not frozenset
            or type(self.authorization_epoch) is not int
            or self.authorization_epoch < 0
            or type(self.owner) is not bool
        ):
            raise ValueError("invalid effective authority")
        if any(
            type(capability) is not str or re.fullmatch(r"[a-z][a-z-]{0,63}", capability) is None
            for capability in self.capabilities
        ):
            raise ValueError("invalid effective authority")
        if self.space_ids is not None:
            validate_wire(
                "filters",
                {
                    "space_ids": sorted(self.space_ids)
                    if all(type(x) is str for x in self.space_ids)
                    else list(self.space_ids),
                    "payload_families": [],
                    "record_types": [],
                },
            )

    def require(self, capability: str) -> None:
        if not self.owner and capability not in self.capabilities:
            raise T03Error("unsupported_capability")

    def permits_space(self, space_id: str | None) -> bool:
        return self.space_ids is None or space_id in self.space_ids


@dataclass(frozen=True, slots=True, kw_only=True)
class _Request:
    dto_version: int = 1
    operation: ClassVar[str]

    def __post_init__(self) -> None:
        validate_wire(self.operation + ".request", self.to_wire())
        for item in fields(self):
            object.__setattr__(self, item.name, _freeze(getattr(self, item.name)))

    def to_wire(self) -> dict[str, Any]:
        return {item.name: _thaw(getattr(self, item.name)) for item in fields(self)}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(child) for child in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchPageRequest(_Request):
    operation: ClassVar[str] = "search.page"
    query: str
    filters: Mapping[str, tuple[str, ...] | list[str]] = field(
        default_factory=lambda: {
            "space_ids": [],
            "payload_families": [],
            "record_types": [],
        }
    )
    mode: str = "lexical"
    limit: int = 10
    cursor: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordReadRequest(_Request):
    operation: ClassVar[str] = "record.read"
    record_id: str
    expected_revision_id: str
    target_bytes: int = 32768
    cursor: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryListRequest(_Request):
    operation: ClassVar[str] = "history.list"
    record_id: str
    limit: int = 10
    cursor: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRouteRequest(_Request):
    operation: ClassVar[str] = "source.route"
    source_id: str
    space_id: str
    expected_head: str
    expected_route_version: int
    operation_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RelationshipDecideRequest(_Request):
    operation: ClassVar[str] = "relationship.decide"
    left: Mapping[str, str]
    right: Mapping[str, str]
    kind: str
    decision: str
    expected_relationship_version: int
    operation_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RelationshipListRequest(HistoryListRequest):
    operation: ClassVar[str] = "relationship.list"


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionHistoryRequest(HistoryListRequest):
    operation: ClassVar[str] = "decision.history"


@dataclass(frozen=True, slots=True)
class _Response:
    """Validated wire value retained as immutable encoded JSON, never caller-owned data."""

    encoded: str
    operation: ClassVar[str]

    def __post_init__(self) -> None:
        validate_wire(self.operation + ".response", parse(self.encoded))

    @classmethod
    def from_wire(cls, value: object) -> _Response:
        validate_wire(cls.operation + ".response", value)
        return cls(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def to_wire(self) -> dict[str, object]:
        return cast(dict[str, object], parse(self.encoded))


class SearchPageResponse(_Response):
    operation = "search.page"


class RecordReadResponse(_Response):
    operation = "record.read"


class HistoryListResponse(_Response):
    operation = "history.list"


class SourceRouteResponse(_Response):
    operation = "source.route"


class RelationshipDecideResponse(_Response):
    operation = "relationship.decide"


class RelationshipListResponse(_Response):
    operation = "relationship.list"


class DecisionHistoryResponse(_Response):
    operation = "decision.history"


_REQUESTS: dict[str, type[_Request]] = {
    cls.operation: cls
    for cls in (
        SearchPageRequest,
        RecordReadRequest,
        HistoryListRequest,
        SourceRouteRequest,
        RelationshipDecideRequest,
        RelationshipListRequest,
        DecisionHistoryRequest,
    )
}
_REQUESTS["history.show"] = RecordReadRequest


def request_from_wire(operation: str, arguments: object) -> _Request:
    validated = validate_wire(operation + ".request", arguments)
    try:
        return _REQUESTS[operation](**validated)
    except TypeError, KeyError:
        raise T03Error("invalid_arguments") from None


def response_to_wire(operation: str, response: object) -> dict[str, object]:
    value = response.to_wire() if isinstance(response, _Response) else response
    validate_wire(operation + ".response", value)
    return cast(dict[str, object], value)
