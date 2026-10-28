"""Explicit compact search capability, separate from frozen T03 wire contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from open_brain_engine.engine.compact_search_contracts import (
    CompactSearchPageRequest,
    CompactSearchPageResponse,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.services.t03_adapters import (
    MAX_CONTENT_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    SAFE_ERROR_CODES,
    T03AppAdapter,
    T03AppError,
    T03SessionBudget,
)


class CompactSearchOperation(Protocol):
    def __call__(
        self,
        request: CompactSearchPageRequest,
        *,
        authority: EffectiveAuthority,
        maximum_response_bytes: int | None = None,
        encoded_size: Callable[[Mapping[str, object]], int] | None = None,
    ) -> CompactSearchPageResponse: ...


@dataclass(slots=True)
class CompactSearchAppAdapter:
    operation: CompactSearchOperation
    authority: EffectiveAuthority
    budget: T03SessionBudget

    def __post_init__(self) -> None:
        if not callable(self.operation) or not isinstance(self.authority, EffectiveAuthority):
            raise ValueError("invalid compact search capability")

    @property
    def available(self) -> bool:
        return self.authority.owner or "search" in self.authority.capabilities

    def describe(self, arguments: Mapping[str, object]) -> dict[str, object]:
        if dict(arguments) != {"extensions": ["compact_search_v1"]}:
            raise T03AppError("invalid_arguments")
        if not self.available:
            raise T03AppError("unsupported_capability")
        return {
            "status": "ok",
            "contract_version": "compact-search.v1",
            "operations": [
                {"name": "search.compact.page", "dto_version": 1, "required_grants": ["search"]}
            ],
            "limits": {
                "request_bytes": 65_536,
                "default_limit": 20,
                "maximum_limit": 100,
                "default_target_bytes": 4096,
                "minimum_target_bytes": 2048,
                "maximum_target_bytes": 8192,
                "cursor_ttl_seconds": 900,
            },
        }

    def invoke(
        self,
        arguments: Mapping[str, object],
        *,
        maximum_response_bytes: int = MAX_RESPONSE_BYTES,
        encoded_size: Callable[[Mapping[str, object]], int] | None = None,
        encoded_request_size: int | None = None,
    ) -> dict[str, object]:
        if not self.available:
            raise T03AppError("unsupported_capability")
        if type(maximum_response_bytes) is not int or maximum_response_bytes < 1:
            raise T03AppError("response_too_large")
        try:
            request_size = (
                encoded_request_size
                if encoded_request_size is not None
                else len(
                    json.dumps(dict(arguments), ensure_ascii=True, separators=(",", ":")).encode()
                )
            )
            if request_size > MAX_REQUEST_BYTES:
                raise T03AppError("invalid_arguments")
            request = CompactSearchPageRequest.from_wire(dict(arguments))
            self.budget.begin("search.compact.page")
            remaining = MAX_CONTENT_BYTES - self.budget.content_bytes
            if remaining < 1:
                raise T03AppError("response_too_large")
            response = self.operation(
                request,
                authority=self.authority,
                maximum_response_bytes=min(maximum_response_bytes, MAX_RESPONSE_BYTES, remaining),
                encoded_size=encoded_size,
            )
            result = cast(dict[str, object], response.to_wire())
        except T03AppError:
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            if isinstance(code, str) and code in SAFE_ERROR_CODES:
                raise T03AppError(code) from None
            raise
        size = T03AppAdapter._check_size(result, maximum_response_bytes, encoded_size)
        self.budget.commit("search.compact.page", size)
        return result
