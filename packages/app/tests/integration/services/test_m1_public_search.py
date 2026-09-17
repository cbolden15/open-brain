from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import DecisionOutcome, ProposalDraft, TextPayload, open_local_engine
from open_brain_engine.storage.markdown import parse_markdown

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_operations import search_brain


@pytest.mark.parametrize("updated", (False, True), ids=("two-sources", "third-source-update"))
@pytest.mark.parametrize("query", ("nebula", "unrelatedquasar"))
def test_public_search_after_multi_source_publication(
    tmp_path: Path, updated: bool, query: str
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    space = tasks.inbox.create_space("Synthetic", delivery_id="m1.space")
    captures = tuple(
        tasks.capture.accept(
            TextPayload(f"Synthetic evidence {index}"),
            delivery_id=f"m1.source.{index}",
            space_id=space.space_id,
        ).capture_id
        for index in range(3)
    )
    proposal = tasks.review.propose(
        captures[:2],
        (ProposalDraft("Nebula", "Nebula first publication"),),
        delivery_id="m1.proposal",
    )[0]
    assert proposal.page_id is not None
    tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="m1.approve",
        expected_review_digest=proposal.review_digest,
    )
    if updated:
        update = tasks.review.propose(
            captures[2:],
            (ProposalDraft("Nebula", "Nebula cumulative update"),),
            delivery_id="m1.update",
            target_page_id=proposal.page_id,
        )[0]
        tasks.review.decide(
            update.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="m1.update.approve",
            expected_review_digest=update.review_digest,
        )

    results = search_brain(tasks.retrieval, tasks.reconciliation, query, limit=10)

    assert [result.result_id for result in results] == (
        [proposal.page_id] if query == "nebula" else []
    )
    page = tasks.retrieval.fetch(proposal.page_id)
    assert page is not None
    assert page.provenance.capture_ids == captures[: 3 if updated else 2]


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def test_empty_brain_cli_publication_and_mcp_search_journey(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = home / "brain"
    assert not root.exists()

    def cli(*arguments: str) -> dict[str, object]:
        assert (
            run_cli(
                (*arguments, "--data-dir", str(root), "--json"),
                environment={"HOME": str(home)},
                platform_name="darwin",
                filesystem_type_probe=_filesystem,
            )
            == 0
        )
        return cast(dict[str, object], json.loads(capsys.readouterr().out))

    def mcp_search() -> list[dict[str, object]]:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
            },
            *(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": "brain_search", "arguments": {"query": query, "limit": 10}},
                }
                for index, query in enumerate(("nebula", "unrelatedquasar"), start=2)
            ),
        ]
        incoming = io.TextIOWrapper(
            io.BytesIO(b"".join(json.dumps(message).encode() + b"\n" for message in messages))
        )
        outgoing = io.TextIOWrapper(io.BytesIO(), write_through=True)
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdin", incoming)
            patch.setattr(sys, "stdout", outgoing)
            assert (
                run_cli(
                    ("mcp", "--allow-search", "--data-dir", str(root)),
                    environment={"HOME": str(home)},
                    platform_name="darwin",
                    filesystem_type_probe=_filesystem,
                )
                == 0
            )
        outgoing.buffer.seek(0)
        responses = [json.loads(line) for line in outgoing.buffer.readlines()]
        assert len(responses) == 3
        assert all(not response["result"].get("isError") for response in responses)
        return [response["result"]["structuredContent"] for response in responses[1:]]

    space = cast(dict[str, object], cli("space", "create", "Synthetic")["space"])
    captures = []
    for index in range(3):
        captured = cli("capture", f"Synthetic evidence {index}")
        capture_id = str(captured["capture_id"])
        captures.append(capture_id)
        assert cli("inbox", "route", capture_id, str(space["space_id"]))["status"] == "routed"
    draft = home / "draft.md"
    page_id = ""
    original_path: Path | None = None
    for stage, selected in enumerate((captures[:2], captures[2:]), start=1):
        draft.write_text(f"Nebula publication revision {stage}\n", encoding="utf-8")
        arguments = ["review", "propose", "--title", "Nebula", "--markdown-file", str(draft)]
        for capture_id in selected:
            arguments.extend(("--capture-id", capture_id))
        if page_id:
            arguments.extend(("--target-page-id", page_id))
        proposed = cli(*arguments)
        shown = cli("review", "show", str(proposed["proposal_id"]))
        expected_sources = captures[: stage + 1]
        assert shown["capture_ids"] == expected_sources
        assert shown["selected_capture_ids"] == selected
        approved = cli(
            "review",
            "approve",
            str(proposed["proposal_id"]),
            "--review-token",
            str(shown["review_token"]),
        )
        assert approved["status"] == "approved"
        assert not page_id or page_id == approved["page_id"]
        page_id = str(approved["page_id"])
        page_path = next((root / "content/spaces").rglob(f"{page_id}.md"))
        assert original_path is None or page_path == original_path
        original_path = page_path
        page = parse_markdown(page_path.read_bytes())
        assert page.fields["provenance"] == expected_sources
        assert f"revision {stage}" in page.body
        for related, unrelated in (
            (cli("search", "nebula"), cli("search", "unrelatedquasar")),
            tuple(mcp_search()),
        ):
            hits = cast(list[dict[str, object]], related["results"])
            assert [hit["result_id"] for hit in hits] == [page_id]
            assert hits[0]["capture_id"] == captures[0]
            assert hits[0]["trust"] == "reviewed"
            assert unrelated == {"results": [], "status": "ok"}
        assert cli("doctor", "--check", "search-index") == {"check": "search-index", "status": "ok"}
        destination = home / f"export-{stage}"
        exported = cli("export", str(destination), "--verify")
        assert exported["verification"] == "verified"
        assert exported["captures"] == 3
        exported_page = next(destination.rglob(f"{page_id}.md"))
        assert exported_page.read_bytes() == page_path.read_bytes()
