# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Framework-neutral, work-only tool adapter for a local stdio MCP server."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict

from open_brain_engine.engine import RetrievalResult, ScopedRetrievalTask

from .ports import (
    FeedbackOutcome,
    IntegrationScope,
    RetrievalBatch,
    RetrievalFeedback,
    RetrievalFeedbackReceipt,
    RetrievalFeedbackRequest,
    RetrievalRequest,
    WorkRetriever,
)


class McpCallError(ValueError):
    """Safe adapter error that omits raw service or argument details."""


_OPAQUE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_OPAQUE_ID_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
}


class McpInputSchema(TypedDict):
    type: Literal["object"]
    properties: dict[str, object]
    required: list[str]
    additionalProperties: Literal[False]


class McpToolDefinition(TypedDict):
    name: str
    description: str
    inputSchema: McpInputSchema


@dataclass(slots=True)
class LocalStdioMcpAdapter:
    """Expose bounded work retrieval and metadata-only feedback as MCP tools."""

    retriever: WorkRetriever
    feedback: RetrievalFeedback

    @property
    def transport(self) -> Literal["stdio"]:
        return "stdio"

    @property
    def scope(self) -> IntegrationScope:
        return IntegrationScope.WORK

    def list_tools(self) -> tuple[McpToolDefinition, ...]:
        return (
            {
                "name": "brain_query",
                "description": "Search bounded, redacted work context.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                    },
                    "required": ["question"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "brain_retrieval_feedback",
                "description": "Record allow-listed retrieval outcome metadata.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "retrieval_id": dict(_OPAQUE_ID_SCHEMA),
                        "outcome": {
                            "type": "string",
                            "enum": ["used", "cited", "ignored", "empty"],
                        },
                        "result_ids": {
                            "type": "array",
                            "items": dict(_OPAQUE_ID_SCHEMA),
                            "maxItems": 8,
                            "uniqueItems": True,
                        },
                    },
                    "required": ["retrieval_id", "outcome"],
                    "additionalProperties": False,
                },
            },
        )

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        try:
            if name == "brain_query":
                return self._query(arguments)
            if name == "brain_retrieval_feedback":
                return self._record_feedback(arguments)
            raise McpCallError("unknown tool")
        except McpCallError:
            raise
        except (KeyError, TypeError, ValueError):
            raise McpCallError("invalid tool arguments") from None
        except Exception:
            raise McpCallError("tool call failed") from None

    def _query(self, arguments: Mapping[str, object]) -> dict[str, object]:
        _require_keys(arguments, required={"question"}, optional={"limit"})
        question = arguments["question"]
        limit = arguments.get("limit", 5)
        if not isinstance(question, str):
            raise ValueError("invalid question")
        request = RetrievalRequest(question=question, limit=limit)  # type: ignore[arg-type]
        batch = self.retriever.search(request)
        if not isinstance(batch, RetrievalBatch):
            raise TypeError("invalid retrieval response")
        hits = batch.hits[: request.limit]
        return {
            "retrieval_id": batch.retrieval_id,
            "scope": IntegrationScope.WORK.value,
            "results": [hit.to_dict() for hit in hits],
            "truncated": batch.truncated or len(batch.hits) > request.limit,
        }

    def _record_feedback(self, arguments: Mapping[str, object]) -> dict[str, object]:
        _require_keys(
            arguments,
            required={"retrieval_id", "outcome"},
            optional={"result_ids"},
        )
        retrieval_id = arguments["retrieval_id"]
        outcome = arguments["outcome"]
        result_ids = arguments.get("result_ids", [])
        if (
            not isinstance(retrieval_id, str)
            or not isinstance(outcome, str)
            or not isinstance(result_ids, list)
            or any(not isinstance(result_id, str) for result_id in result_ids)
        ):
            raise ValueError("invalid feedback")
        _require_opaque_id(retrieval_id)
        for result_id in result_ids:
            _require_opaque_id(result_id)
        request = RetrievalFeedbackRequest(
            retrieval_id=retrieval_id,
            outcome=FeedbackOutcome(outcome),
            result_ids=tuple(result_ids),
        )
        receipt = self.feedback.record(request)
        if not isinstance(receipt, RetrievalFeedbackReceipt):
            raise TypeError("invalid feedback response")
        return receipt.to_dict()


def _require_keys(
    arguments: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str],
) -> None:
    keys = set(arguments)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise ValueError("invalid arguments")


def _require_opaque_id(value: str) -> None:
    if _OPAQUE_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid opaque identifier")


@dataclass(slots=True)
class EngineMcpAdapter:
    """Read-only MCP representation over an injected engine retrieval capability."""

    retrieval: ScopedRetrievalTask
    feedback: RetrievalFeedback

    def __post_init__(self) -> None:
        if (
            not callable(getattr(self.retrieval, "search", None))
            or not callable(getattr(self.retrieval, "fetch", None))
            or callable(getattr(self.retrieval, "scoped", None))
            or not callable(getattr(self.feedback, "record", None))
        ):
            raise ValueError("invalid engine MCP capabilities")

    @property
    def transport(self) -> Literal["stdio"]:
        return "stdio"

    @property
    def scope(self) -> IntegrationScope:
        return IntegrationScope.WORK

    def list_tools(self) -> tuple[McpToolDefinition, ...]:
        return (
            {
                "name": "brain_query",
                "description": "Search caller-allowed local spaces.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "minLength": 1, "maxLength": 500},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                    },
                    "required": ["question"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "brain_fetch",
                "description": "Fetch one caller-allowed retrieval result.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"result_id": dict(_OPAQUE_ID_SCHEMA)},
                    "required": ["result_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "brain_retrieval_feedback",
                "description": "Record retrieval outcome metadata.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "retrieval_id": dict(_OPAQUE_ID_SCHEMA),
                        "outcome": {
                            "type": "string",
                            "enum": ["used", "cited", "ignored", "empty"],
                        },
                        "result_ids": {
                            "type": "array",
                            "items": dict(_OPAQUE_ID_SCHEMA),
                            "maxItems": 8,
                            "uniqueItems": True,
                        },
                    },
                    "required": ["retrieval_id", "outcome"],
                    "additionalProperties": False,
                },
            },
        )

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        try:
            if name == "brain_query":
                return self._query(arguments)
            if name == "brain_fetch":
                return self._fetch(arguments)
            if name == "brain_retrieval_feedback":
                return self._feedback(arguments)
            raise McpCallError("unknown tool")
        except McpCallError:
            raise
        except (KeyError, TypeError, ValueError):
            raise McpCallError("invalid tool arguments") from None
        except Exception:
            raise McpCallError("tool call failed") from None

    def _query(self, arguments: Mapping[str, object]) -> dict[str, object]:
        _require_keys(arguments, required={"question"}, optional={"limit"})
        question = arguments["question"]
        limit = arguments.get("limit", 5)
        if not isinstance(question, str) or type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("invalid query")
        results = self.retrieval.search(question, limit=limit)
        return {
            "retrieval_id": "retrieval." + secrets.token_hex(16),
            "scope": IntegrationScope.WORK.value,
            "results": [_engine_result(result) for result in results],
            "truncated": len(results) == limit,
        }

    def _fetch(self, arguments: Mapping[str, object]) -> dict[str, object]:
        _require_keys(arguments, required={"result_id"}, optional=set())
        result_id = arguments["result_id"]
        if not isinstance(result_id, str):
            raise ValueError("invalid result")
        _require_opaque_id(result_id)
        result = self.retrieval.fetch(result_id)
        return {"result": None if result is None else _engine_result(result)}

    def _feedback(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return LocalStdioMcpAdapter(
            retriever=_FeedbackOnlyRetriever(), feedback=self.feedback
        )._record_feedback(arguments)


class _FeedbackOnlyRetriever:
    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        del request
        raise ValueError("feedback-only retriever")


def _engine_result(result: RetrievalResult) -> dict[str, object]:
    return {
        "capture_id": result.capture_id,
        "excerpt": result.excerpt,
        "explanation": result.explanation,
        "payload_family": result.payload_family,
        "provenance": result.provenance.as_dict(),
        "record_type": result.record_type,
        "result_id": result.result_id,
        "space_id": result.space_id,
        "title": result.title,
        "trust": result.trust,
    }
