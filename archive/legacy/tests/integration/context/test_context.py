from __future__ import annotations

from pathlib import Path

import pytest

from open_brain_legacy._compat.open_brain.integrations.ports import (
    RetrievalBatch,
    RetrievalRequest,
    TrustLabel,
)
from open_brain_legacy.integrations.context import (
    ContextRequest,
    ContextStatus,
    WorkContextService,
)
from open_brain_legacy.integrations.repository_identity import (
    RepositoryIdentitySource,
    StableRepoIdentity,
)
from open_brain_legacy.integrations.retrieval import FilesystemWorkRetriever, WorkPageSnapshot


def _page(root: Path, relative: str, *, title: str, body: str) -> None:
    path = root / "pages" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {title}\n"
        "status: adopted\n"
        "---\n\n"
        f"# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def _identity() -> StableRepoIdentity:
    return StableRepoIdentity(
        repository_id="repo_0123456789abcdef0123456789abcdef",
        slug="acme/widget",
        source=RepositoryIdentitySource.ORIGIN,
    )


def test_context_uses_ranked_work_results_and_follows_only_one_link_hop(
    tmp_path: Path,
) -> None:
    _page(
        tmp_path,
        "projects/widget.md",
        title="Widget deployment project",
        body="## Overview\n\nWidget deployment. See [[learning/widget-runbook]].",
    )
    _page(
        tmp_path,
        "learning/widget-runbook.md",
        title="Widget deployment runbook",
        body=(
            "## Gotchas\n\nUse the synthetic release lock for widget deployment. "
            "See [[patterns/deep-note]]."
        ),
    )
    _page(
        tmp_path,
        "patterns/deep-note.md",
        title="Deep note",
        body="## Internal\n\nThis page must not be followed recursively.",
    )
    service = WorkContextService(retriever=FilesystemWorkRetriever(work_root=tmp_path))

    block = service.retrieve(
        ContextRequest(
            topic="widget deployment",
            repository=_identity(),
            limit=3,
            max_bytes=2_000,
        )
    )

    assert block.status is ContextStatus.AVAILABLE
    assert [item.hop for item in block.items] == [0, 1]
    assert [item.title.text for item in block.items] == [
        "Widget deployment project",
        "Widget deployment runbook",
    ]
    assert block.items[1].heading.text == "Gotchas"
    assert block.items[1].trust is TrustLabel.UNREVIEWED_THIRD_PARTY
    assert all(item.title.text != "Deep note" for item in block.items)
    assert all(item.result_id.startswith("result_") for item in block.items)
    assert block.to_dict()["scope"] == "work"
    assert str(tmp_path) not in repr(block.to_dict())
    assert "path" not in repr(block.to_dict())
    assert "unreviewed_third_party" in repr(block.to_dict())


def test_context_enforces_its_byte_cap(tmp_path: Path) -> None:
    _page(
        tmp_path,
        "patterns/widget.md",
        title="Widget",
        body="## Notes\n\n" + ("widget bounded context " * 200),
    )
    service = WorkContextService(retriever=FilesystemWorkRetriever(work_root=tmp_path))

    block = service.retrieve(
        ContextRequest(topic="widget context", limit=2, max_bytes=180)
    )

    assert block.status is ContextStatus.AVAILABLE
    assert block.items
    assert block.bytes_used <= 180
    assert block.truncated
    assert sum(item.public_bytes for item in block.items) == block.bytes_used


def test_unavailable_context_is_empty_and_structural(tmp_path: Path) -> None:
    service = WorkContextService(
        retriever=FilesystemWorkRetriever(work_root=tmp_path / "missing")
    )

    block = service.retrieve(ContextRequest(topic="synthetic", max_bytes=256))

    assert block.status is ContextStatus.UNAVAILABLE
    assert block.items == ()
    assert block.bytes_used == 0
    assert not block.truncated


class _ExplodingRetriever:
    @property
    def available(self) -> bool:
        return True

    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        raise RuntimeError("synthetic retrieval failure")

    def snapshot(self, result_id: str) -> WorkPageSnapshot | None:
        raise RuntimeError("synthetic snapshot failure")


def test_fail_zero_context_mode_never_raises_or_returns_content() -> None:
    service = WorkContextService(retriever=_ExplodingRetriever())

    block = service.retrieve_fail_zero(ContextRequest(topic="synthetic", max_bytes=256))

    assert block.status is ContextStatus.FAILED
    assert block.items == ()
    assert block.bytes_used == 0
    assert block.to_dict()["results"] == []

    with pytest.raises(RuntimeError, match="synthetic retrieval failure"):
        service.retrieve(ContextRequest(topic="synthetic", max_bytes=256))


def test_retrieval_and_context_modules_add_no_task_session_personal_or_network_surface() -> None:
    integrations = Path(__file__).parents[3] / "src" / "open_brain_legacy" / "integrations"
    source = "\n".join(
        (integrations / name).read_text(encoding="utf-8")
        for name in ("retrieval.py", "repository_identity.py", "context.py")
    )

    prohibited = (
        "create_task",
        "upsert_task",
        "enqueue_task",
        "personal_root",
        "session_start",
        "socket",
        "urllib.request",
        "http.client",
    )
    assert all(token not in source for token in prohibited)
