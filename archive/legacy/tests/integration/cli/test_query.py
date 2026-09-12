from __future__ import annotations

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy._compat.open_brain.integrations.ports import (
    RedactedText,
    RetrievalBatch,
    RetrievalHit,
    RetrievalRequest,
    TrustLabel,
)
from open_brain_legacy.cli.query import query_work


class WorkRetrieverFake:
    def __init__(self) -> None:
        self.requests: list[RetrievalRequest] = []

    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        self.requests.append(request)
        return RetrievalBatch(
            retrieval_id="retrieval_synthetic",
            hits=(
                RetrievalHit(
                    result_id="result_top",
                    rank=1,
                    title=RedactedText.redact("Synthetic work note"),
                    excerpt=RedactedText.redact("Relevant work summary"),
                    trust=TrustLabel.VERIFIED_WORK,
                ),
            ),
            truncated=False,
        )


def test_query_is_work_only_and_serializes_ranked_redacted_results() -> None:
    retriever = WorkRetrieverFake()

    result = query_work(retriever=retriever, question="synthetic work question", limit=1)

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "command": "query",
        "retrieval_id": "retrieval_synthetic",
        "results": [
            {
                "excerpt": "Relevant work summary",
                "rank": 1,
                "result_id": "result_top",
                "title": "Synthetic work note",
                "trust": "verified_work",
            }
        ],
        "status": "ok",
        "truncated": False,
    }
    assert retriever.requests == [RetrievalRequest(question="synthetic work question", limit=1)]


def test_query_fails_closed_when_the_work_index_is_unavailable() -> None:
    class UnavailableWorkRetriever:
        available = False

        def search(self, request: RetrievalRequest) -> RetrievalBatch:
            raise AssertionError("unavailable index must not be queried")

    result = query_work(
        retriever=UnavailableWorkRetriever(),
        question="synthetic work question",
        limit=1,
    )

    assert result.exit_code is ExitCode.FAILURE
    assert result.envelope == {
        "command": "query",
        "error": {
            "code": "work_index_unavailable",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "unavailable",
    }
