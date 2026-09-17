"""Negotiated app adapters for the frozen ``t03.v1`` task family.

Authority is constructed by trusted app/session code and is never accepted from wire
arguments.  Engine request parsing and response serialization stay engine-owned.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

CONTRACT_VERSION = "t03.v1"
DTO_VERSION = 1
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 1_048_576
MAX_CONTENT_CALLS = 500
MAX_CONTENT_BYTES = 16 * 1024 * 1024
MAX_HISTORY_CALLS = 500
MAX_HISTORY_BYTES = 16 * 1024 * 1024

SAFE_ERROR_CODES = frozenset(
    {
        "not_found",
        "cursor_invalid",
        "cursor_stale",
        "revision_changed",
        "preview_stale",
        "operation_pending",
        "source_revision_conflict",
        "model_unavailable",
        "projection_stale",
        "invalid_arguments",
        "unsupported_capability",
        "incompatible_runtime",
        "response_too_large",
    }
)

_NEGOTIATED: dict[str, tuple[str, str, str]] = {
    "search.page": ("search", "retrieval", "search_page"),
    "record.read": ("content-read", "retrieval", "read_record"),
    "history.list": ("history-read", "history", "list_history"),
    "history.show": ("history-read", "history", "read_history"),
    "source.route": ("organize", "sources", "route"),
}

_OWNER_ONLY: dict[str, tuple[str, str]] = {
    "relationship.decide": ("relationships", "decide"),
    "relationship.list": ("relationships", "list_relationships"),
    "decision.history": ("relationships", "list_decisions"),
}


class _WireParser(Protocol):
    def __call__(self, operation: str, arguments: object) -> object: ...


class _WireSerializer(Protocol):
    def __call__(self, operation: str, response: object) -> dict[str, object]: ...


class T03AppError(ValueError):
    """One frozen, path-free application error code."""

    def __init__(self, code: str) -> None:
        if code not in SAFE_ERROR_CODES:
            raise ValueError("invalid t03 app error")
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class T03SessionBudget:
    """Independent bounded output custody for content and history calls."""

    content_calls: int = 0
    content_bytes: int = 0
    history_calls: int = 0
    history_bytes: int = 0

    def begin(self, operation: str) -> None:
        if operation in {"search.page", "record.read"}:
            if self.content_calls >= MAX_CONTENT_CALLS:
                raise T03AppError("operation_pending")
            self.content_calls += 1
        elif operation in {"history.list", "history.show"}:
            if self.history_calls >= MAX_HISTORY_CALLS:
                raise T03AppError("operation_pending")
            self.history_calls += 1

    def commit(self, operation: str, encoded_bytes: int) -> None:
        if operation in {"search.page", "record.read"}:
            if self.content_bytes + encoded_bytes > MAX_CONTENT_BYTES:
                raise T03AppError("response_too_large")
            self.content_bytes += encoded_bytes
        elif operation in {"history.list", "history.show"}:
            if self.history_bytes + encoded_bytes > MAX_HISTORY_BYTES:
                raise T03AppError("response_too_large")
            self.history_bytes += encoded_bytes


@dataclass(slots=True)
class T03AppAdapter:
    """Grant-filtered dispatch over engine-owned typed task methods."""

    tasks: object
    authority: object
    grants: frozenset[str]
    owner: bool = False
    budget: T03SessionBudget = field(default_factory=T03SessionBudget)
    parse_request: _WireParser | None = None
    serialize_response: _WireSerializer | None = None

    def __post_init__(self) -> None:
        if self.parse_request is None or self.serialize_response is None:
            try:
                from open_brain_engine.engine.t03_contracts import (
                    request_from_wire,
                    response_to_wire,
                )
            except ImportError:
                return
            self.parse_request = request_from_wire
            self.serialize_response = response_to_wire

    def available_operations(self) -> tuple[str, ...]:
        operations = [
            operation
            for operation, (grant, component, method) in _NEGOTIATED.items()
            if grant in self.grants and self._method(component, method) is not None
        ]
        if self.owner:
            operations.extend(
                operation
                for operation, (component, method) in _OWNER_ONLY.items()
                if self._method(component, method) is not None
            )
        return tuple(operations)

    def describe(self, arguments: Mapping[str, object]) -> dict[str, object]:
        if arguments:
            raise T03AppError("invalid_arguments")
        available = set(self.available_operations())
        return {
            "status": "ok",
            "contract_version": CONTRACT_VERSION,
            "operations": [
                {
                    "name": operation,
                    "dto_version": DTO_VERSION,
                    "required_grants": [grant],
                }
                for operation, (grant, _component, _method) in _NEGOTIATED.items()
                if operation in available
            ],
            "limits": {
                "request_bytes": MAX_REQUEST_BYTES,
                "response_bytes": MAX_RESPONSE_BYTES,
                "content_calls": MAX_CONTENT_CALLS,
                "content_bytes": MAX_CONTENT_BYTES,
                "history_calls": MAX_HISTORY_CALLS,
                "history_bytes": MAX_HISTORY_BYTES,
            },
        }

    def invoke(
        self,
        operation: str,
        arguments: Mapping[str, object],
        *,
        maximum_response_bytes: int = MAX_RESPONSE_BYTES,
        encoded_size: Callable[[Mapping[str, object]], int] | None = None,
    ) -> dict[str, object]:
        if operation == "contract.describe":
            result = self.describe(arguments)
            self._check_size(result, maximum_response_bytes, encoded_size)
            return result
        if operation not in self.available_operations():
            raise T03AppError("unsupported_capability")
        if self.parse_request is None or self.serialize_response is None:
            raise T03AppError("unsupported_capability")
        if operation in _NEGOTIATED:
            _grant, component, method_name = _NEGOTIATED[operation]
        else:
            component, method_name = _OWNER_ONLY[operation]
        method = self._method(component, method_name)
        if method is None:
            raise T03AppError("unsupported_capability")
        self.budget.begin(operation)
        try:
            request = self.parse_request(operation, dict(arguments))
            response = method(request, authority=self.authority)
            result = self.serialize_response(operation, response)
        except T03AppError:
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            if isinstance(code, str) and code in SAFE_ERROR_CODES:
                raise T03AppError(code) from None
            raise
        encoded_bytes = self._check_size(result, maximum_response_bytes, encoded_size)
        self.budget.commit(operation, encoded_bytes)
        return result

    def _method(self, component: str, method: str) -> Callable[..., object] | None:
        task = getattr(self.tasks, component, None)
        candidate = getattr(task, method, None)
        return candidate if callable(candidate) else None

    @staticmethod
    def _check_size(
        result: Mapping[str, object],
        maximum_response_bytes: int,
        encoded_size: Callable[[Mapping[str, object]], int] | None,
    ) -> int:
        if type(maximum_response_bytes) is not int or maximum_response_bytes < 1:
            raise T03AppError("response_too_large")
        size = (
            encoded_size(result)
            if encoded_size is not None
            else len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        )
        if size > min(maximum_response_bytes, MAX_RESPONSE_BYTES):
            raise T03AppError("response_too_large")
        return size


def owner_authority(tasks: object, *, session_id: str) -> object:
    """Construct trusted owner authority without accepting any wire-supplied fields."""
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    profile = cast(Any, tasks).profile
    return EffectiveAuthority(
        principal_id=str(profile.owner_actor_id),
        session_id=session_id,
        capabilities=frozenset({"search", "content-read", "history-read", "organize"}),
        space_ids=None,
        owner=True,
    )


def error_result(code: str) -> dict[str, object]:
    if code not in SAFE_ERROR_CODES:
        raise ValueError("invalid t03 error result")
    return {"status": "error", "error": {"code": code}}


__all__ = [
    "CONTRACT_VERSION",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "SAFE_ERROR_CODES",
    "T03AppAdapter",
    "T03AppError",
    "T03SessionBudget",
    "error_result",
    "owner_authority",
]
