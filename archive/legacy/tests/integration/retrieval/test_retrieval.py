from __future__ import annotations

from pathlib import Path

import pytest

from open_brain_legacy._compat.open_brain.integrations.ports import (
    FeedbackOutcome,
    RetrievalFeedbackRequest,
    RetrievalRequest,
    TrustLabel,
)
from open_brain_legacy.integrations.retrieval import (
    FilesystemWorkRetriever,
    MetadataOnlyRetrievalFeedback,
)


def _page(root: Path, relative: str, *, title: str, body: str, status: str = "adopted") -> None:
    path = root / "pages" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {title}\n"
        f"status: {status}\n"
        "---\n\n"
        f"# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def _content_bytes(batch: object) -> int:
    hits = batch.hits  # type: ignore[attr-defined]
    return sum(
        len(hit.title.text.encode("utf-8")) + len(hit.excerpt.text.encode("utf-8"))
        for hit in hits
    )


def test_work_retrieval_ranks_deterministically_and_exposes_only_opaque_ids(
    tmp_path: Path,
) -> None:
    _page(
        tmp_path,
        "projects/widget.md",
        title="Widget deployment",
        body="Production widget deployment uses the release runbook.",
    )
    _page(
        tmp_path,
        "patterns/widget-release.md",
        title="Release pattern",
        body="The widget deployment checklist is deterministic.",
    )
    _page(
        tmp_path,
        "tools/database.md",
        title="Database notes",
        body="Postgres indexing and query plans.",
    )
    retriever = FilesystemWorkRetriever(work_root=tmp_path)

    first = retriever.search(RetrievalRequest(question="widget deployment", limit=3))
    second = retriever.search(RetrievalRequest(question="widget deployment", limit=3))

    assert retriever.available
    assert [hit.title.text for hit in first.hits] == [
        "Widget deployment",
        "Release pattern",
    ]
    assert [hit.result_id for hit in first.hits] == [hit.result_id for hit in second.hits]
    assert first.to_dict()["scope"] == "work"
    assert all(hit.result_id.startswith("result_") for hit in first.hits)
    assert str(tmp_path) not in repr(first.to_dict())
    assert all("path" not in hit.to_dict() for hit in first.hits)


def test_low_trust_survives_results_and_archived_pages_are_excluded(tmp_path: Path) -> None:
    _page(
        tmp_path,
        "learning/widget-video.md",
        title="Widget agent notes",
        body="Widget agent claim from a synthetic third-party summary.",
    )
    _page(
        tmp_path,
        "decisions/widget.md",
        title="Widget agent decision",
        body="Reviewed widget agent decision.",
    )
    _page(
        tmp_path,
        "archive/old-widget.md",
        title="Widget agent archive",
        body="Old widget agent material.",
    )
    _page(
        tmp_path,
        "projects/retired-widget.md",
        title="Widget agent retired",
        body="Retired widget agent material.",
        status="archived",
    )

    batch = FilesystemWorkRetriever(work_root=tmp_path).search(
        RetrievalRequest(question="widget agent", limit=8)
    )

    assert {hit.title.text for hit in batch.hits} == {
        "Widget agent notes",
        "Widget agent decision",
    }
    trusts = {hit.title.text: hit.trust for hit in batch.hits}
    assert trusts["Widget agent notes"] is TrustLabel.UNREVIEWED_THIRD_PARTY
    assert trusts["Widget agent decision"] is TrustLabel.VERIFIED_WORK
    serialized_results = batch.to_dict()["results"]
    assert isinstance(serialized_results, list)
    assert all(isinstance(item, dict) for item in serialized_results)
    assert {item["trust"] for item in serialized_results} == {
        "unreviewed_third_party",
        "verified_work",
    }


def test_result_count_and_public_text_bytes_are_bounded(tmp_path: Path) -> None:
    for index in range(12):
        _page(
            tmp_path,
            f"patterns/widget-{index:02d}.md",
            title=f"Widget {index:02d}",
            body="widget " * 200,
        )
    retriever = FilesystemWorkRetriever(work_root=tmp_path, max_bytes=180)

    batch = retriever.search(RetrievalRequest(question="widget", limit=8))

    assert 0 < len(batch.hits) <= 8
    assert _content_bytes(batch) <= 180
    assert batch.truncated


def test_unavailable_work_index_returns_an_empty_structural_batch(tmp_path: Path) -> None:
    retriever = FilesystemWorkRetriever(work_root=tmp_path / "missing")

    batch = retriever.search(RetrievalRequest(question="synthetic query", limit=5))

    assert not retriever.available
    assert batch.hits == ()
    assert not batch.truncated
    assert batch.retrieval_id.startswith("retrieval_")
    assert batch.to_dict()["scope"] == "work"


def test_feedback_records_only_allowlisted_metadata(tmp_path: Path) -> None:
    _page(
        tmp_path,
        "projects/widget.md",
        title="Widget",
        body="RAW_RESULT_SENTINEL widget context.",
    )
    feedback = MetadataOnlyRetrievalFeedback()
    retriever = FilesystemWorkRetriever(work_root=tmp_path, feedback=feedback)
    batch = retriever.search(RetrievalRequest(question="RAW_QUERY_SENTINEL widget", limit=2))
    result_id = batch.hits[0].result_id

    receipt = feedback.record(
        RetrievalFeedbackRequest(
            retrieval_id=batch.retrieval_id,
            outcome=FeedbackOutcome.CITED,
            result_ids=(result_id,),
        )
    )

    assert receipt.recorded
    assert feedback.records[0].to_dict() == {
        "retrieval_id": batch.retrieval_id,
        "outcome": "cited",
        "result_ids": [result_id],
    }
    serialized = repr(feedback.records)
    assert "RAW_QUERY_SENTINEL" not in serialized
    assert "RAW_RESULT_SENTINEL" not in serialized
    assert str(tmp_path) not in serialized

    with pytest.raises(ValueError, match="unknown result"):
        feedback.record(
            RetrievalFeedbackRequest(
                retrieval_id=batch.retrieval_id,
                outcome=FeedbackOutcome.USED,
                result_ids=("result_unknown",),
            )
        )


def test_disabled_feedback_is_a_metadata_free_noop(tmp_path: Path) -> None:
    feedback = MetadataOnlyRetrievalFeedback(enabled=False)

    receipt = feedback.record(
        RetrievalFeedbackRequest(
            retrieval_id="retrieval_fixture",
            outcome=FeedbackOutcome.IGNORED,
        )
    )

    assert not receipt.recorded
    assert feedback.records == ()
