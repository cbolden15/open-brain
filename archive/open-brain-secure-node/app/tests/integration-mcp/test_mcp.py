from __future__ import annotations

from dataclasses import fields
from typing import cast

import pytest

from open_brain.integrations.mcp import LocalStdioMcpAdapter, McpCallError
from open_brain.integrations.ports import (
    FeedbackOutcome,
    IntegrationScope,
    RedactedText,
    RetrievalBatch,
    RetrievalFeedbackReceipt,
    RetrievalFeedbackRequest,
    RetrievalHit,
    RetrievalRequest,
    TrustLabel,
)


class _SyntheticRetriever:
    def __init__(self) -> None:
        self.request: RetrievalRequest | None = None

    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        self.request = request
        return RetrievalBatch(
            retrieval_id="retrieval_fixture",
            hits=(
                RetrievalHit(
                    result_id="result_fixture",
                    rank=1,
                    title=RedactedText.redact("Synthetic work note"),
                    excerpt=RedactedText.redact(
                        "Unreviewed result with credential: synthetic-secret"
                    ),
                    trust=TrustLabel.UNREVIEWED_THIRD_PARTY,
                ),
            ),
            truncated=False,
        )


class _SyntheticFeedback:
    def __init__(self) -> None:
        self.request: RetrievalFeedbackRequest | None = None

    def record(self, request: RetrievalFeedbackRequest) -> RetrievalFeedbackReceipt:
        self.request = request
        return RetrievalFeedbackReceipt(
            retrieval_id=request.retrieval_id,
            outcome=request.outcome,
            result_count=len(request.result_ids),
        )


class _OverReturningRetriever:
    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        del request
        hits = tuple(
            RetrievalHit(
                result_id=f"result_{index}",
                rank=index,
                title=RedactedText.redact(f"Synthetic work note {index}"),
                excerpt=RedactedText.redact(f"Synthetic excerpt {index}"),
                trust=TrustLabel.UNREVIEWED_THIRD_PARTY,
            )
            for index in range(1, 4)
        )
        return RetrievalBatch(
            retrieval_id="retrieval_overreturn",
            hits=hits,
            truncated=False,
        )


def test_local_stdio_mcp_is_bounded_work_only_and_metadata_only() -> None:
    retriever = _SyntheticRetriever()
    feedback = _SyntheticFeedback()
    adapter = LocalStdioMcpAdapter(retriever=retriever, feedback=feedback)

    tools = adapter.list_tools()
    assert adapter.transport == "stdio"
    assert adapter.scope is IntegrationScope.WORK
    assert {tool["name"] for tool in tools} == {
        "brain_query",
        "brain_retrieval_feedback",
    }
    feedback_schema = next(
        tool["inputSchema"]
        for tool in tools
        if tool["name"] == "brain_retrieval_feedback"
    )
    assert set(feedback_schema["properties"]) == {
        "retrieval_id",
        "outcome",
        "result_ids",
    }
    retrieval_id_schema = cast(
        dict[str, object], feedback_schema["properties"]["retrieval_id"]
    )
    assert retrieval_id_schema["maxLength"] == 128
    assert "pattern" in retrieval_id_schema
    assert all("personal" not in repr(tool).casefold() for tool in tools)
    with pytest.raises(AttributeError):
        object.__setattr__(adapter, "scope", IntegrationScope.PERSONAL)

    result = adapter.call_tool(
        "brain_query",
        {"question": "synthetic work topic", "limit": 8},
    )

    assert retriever.request == RetrievalRequest(
        question="synthetic work topic",
        limit=8,
        scope=IntegrationScope.WORK,
    )
    assert result["scope"] == "work"
    results = cast(list[dict[str, object]], result["results"])
    assert len(results) <= 8
    assert results[0]["trust"] == "unreviewed_third_party"
    assert "synthetic-secret" not in repr(result)
    assert "[redacted]" in repr(result)

    with pytest.raises(McpCallError, match="invalid tool arguments"):
        adapter.call_tool(
            "brain_query",
            {"question": "synthetic work topic", "limit": 9},
        )
    with pytest.raises(McpCallError, match="invalid tool arguments"):
        adapter.call_tool(
            "brain_query",
            {"question": "synthetic work topic", "scope": "personal"},
        )

    receipt = adapter.call_tool(
        "brain_retrieval_feedback",
        {
            "retrieval_id": "retrieval_fixture",
            "outcome": "cited",
            "result_ids": ["result_fixture"],
        },
    )

    assert receipt == {
        "retrieval_id": "retrieval_fixture",
        "outcome": "cited",
        "recorded": True,
        "result_count": 1,
    }
    assert feedback.request is not None
    assert {field.name for field in fields(feedback.request)} == {
        "retrieval_id",
        "outcome",
        "result_ids",
    }
    assert feedback.request.outcome is FeedbackOutcome.CITED
    with pytest.raises(McpCallError, match="invalid tool arguments"):
        adapter.call_tool(
            "brain_retrieval_feedback",
            {
                "retrieval_id": "retrieval_fixture",
                "outcome": "used",
                "result_text": "synthetic result content",
            },
        )
    for invalid_id in ("contains private content", "x" * 129):
        with pytest.raises(McpCallError, match="invalid tool arguments"):
            adapter.call_tool(
                "brain_retrieval_feedback",
                {
                    "retrieval_id": invalid_id,
                    "outcome": "used",
                    "result_ids": [],
                },
            )


def test_query_clamps_a_faulty_backend_to_the_requested_limit() -> None:
    adapter = LocalStdioMcpAdapter(
        retriever=_OverReturningRetriever(),
        feedback=_SyntheticFeedback(),
    )

    result = adapter.call_tool(
        "brain_query",
        {"question": "synthetic work topic", "limit": 1},
    )
    results = cast(list[dict[str, object]], result["results"])

    assert len(results) == 1
    assert result["truncated"] is True
