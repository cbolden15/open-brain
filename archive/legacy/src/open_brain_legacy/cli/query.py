"""Deterministic, work-only query adapter for public CLI callers."""

from __future__ import annotations

import json
from dataclasses import dataclass

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
from open_brain_legacy._compat.open_brain.integrations.ports import (
    RetrievalBatch,
    RetrievalRequest,
    WorkRetriever,
)


@dataclass(frozen=True, slots=True)
class QueryCliResult:
    """A public query result containing only redacted work retrieval fields."""

    exit_code: ExitCode
    envelope: dict[str, object]

    def to_json(self) -> str:
        """Serialize the stable automation response."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


def query_work(*, retriever: WorkRetriever, question: str, limit: int = 5) -> QueryCliResult:
    """Run one bounded work-scoped query without exposing unavailable-index details."""
    if getattr(retriever, "available", True) is False:
        return _unavailable()
    try:
        batch = retriever.search(RetrievalRequest(question=question, limit=limit))
        if not isinstance(batch, RetrievalBatch):
            raise ValueError("invalid retrieval batch")
    except Exception:
        return _failed()
    return QueryCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "command": "query",
            "retrieval_id": batch.retrieval_id,
            "results": [
                {
                    "excerpt": hit.excerpt.text,
                    "rank": hit.rank,
                    "result_id": hit.result_id,
                    "title": hit.title.text,
                    "trust": hit.trust.value,
                }
                for hit in batch.hits
            ],
            "status": "ok",
            "truncated": batch.truncated,
        },
    )


def _unavailable() -> QueryCliResult:
    return QueryCliResult(
        exit_code=ExitCode.FAILURE,
        envelope={
            "command": "query",
            "error": redacted_error("work_index_unavailable"),
            "status": "unavailable",
        },
    )


def _failed() -> QueryCliResult:
    return QueryCliResult(
        exit_code=ExitCode.FAILURE,
        envelope={
            "command": "query",
            "error": redacted_error("query_operation_failed"),
            "status": "failed",
        },
    )
