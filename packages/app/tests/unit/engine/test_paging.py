from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    RecordReadRequest,
    SearchPageRequest,
    T03Error,
)

from open_brain.profile import compile_single_user_local


def wire(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value.to_wire())


def authority(session: str = "session") -> EffectiveAuthority:
    return EffectiveAuthority("owner", session, frozenset({"search", "content-read"}), None)


def test_equal_score_keyset_traversal_and_restart(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    captured = [
        engine.capture.accept(TextPayload("identical nebula"), delivery_id=f"row.{i}")
        for i in range(201)
    ]
    request = SearchPageRequest(query="nebula", limit=100)
    first = wire(engine.retrieval.search_page(request, authority=authority()))
    assert len(first["results"]) == 100
    engine = BrainEngine.open(profile)
    second = wire(
        engine.retrieval.search_page(
            replace(request, cursor=first["next_cursor"]), authority=authority()
        )
    )
    third = wire(
        engine.retrieval.search_page(
            replace(request, cursor=second["next_cursor"]), authority=authority()
        )
    )
    ids = [row["record_id"] for page in (first, second, third) for row in page["results"]]
    assert ids == sorted(row.capture_id for row in captured)
    assert third["complete"] and third["next_cursor"] is None
    repeated = wire(
        engine.retrieval.search_page(
            replace(request, cursor=first["next_cursor"]), authority=authority()
        )
    )
    assert repeated["results"] == second["results"]
    with pytest.raises(T03Error, match="cursor_invalid"):
        engine.retrieval.search_page(
            replace(request, cursor=first["next_cursor"]), authority=authority("other")
        )
    engine.capture.accept(TextPayload("another nebula"), delivery_id="changed")
    with pytest.raises(T03Error, match="cursor_stale"):
        engine.retrieval.search_page(
            replace(request, cursor=first["next_cursor"]), authority=authority()
        )


def test_full_unicode_chunk_reconstruction_and_grant(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    body = "✨漢字 café 👨‍👩‍👧‍👦 \n" * 9
    capture = engine.capture.accept(TextPayload(body), delivery_id="unicode")
    request = RecordReadRequest(
        record_id=capture.capture_id, expected_revision_id=capture.capture_id, target_bytes=1
    )
    chunks = []
    offset = 0
    while True:
        response = wire(engine.retrieval.read_record(request, authority=authority()))
        assert response["start_byte"] == offset
        text = response["content"]["text"]
        assert response["end_byte"] == offset + len(text.encode())
        chunks.append(text)
        offset = response["end_byte"]
        if response["complete"]:
            break
        request = replace(request, cursor=response["next_cursor"])
    assert "".join(chunks) == body
    with pytest.raises(T03Error, match="unsupported_capability"):
        engine.retrieval.read_record(
            request, authority=replace(authority(), capabilities=frozenset())
        )


def test_canonical_revision_members_and_full_secondary_projection(tmp_path: Path) -> None:
    from open_brain_engine.engine import DecisionOutcome, ProposalDraft, ReferencePayload

    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    )
    space = engine.inbox.spaces()[0]
    captures = [
        engine.capture.accept(TextPayload("canonical nebula"), delivery_id="primary"),
        engine.capture.accept(
            ReferencePayload("https://synthetic.example/private/secret", "secondary evidence"),
            delivery_id="secondary",
        ),
    ]
    for capture in captures:
        engine.inbox.route(capture.capture_id, space.space_id, delivery_id=capture.capture_id)
    proposal = engine.review.propose(
        tuple(capture.capture_id for capture in captures),
        (
            ProposalDraft(
                "Canonical nebula",
                "Full canonical nebula https://synthetic.example/private/secret\n漢字",
            ),
        ),
        delivery_id="proposal",
    )[0]
    decision = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="decision",
        expected_review_digest=proposal.review_digest,
    )
    page = wire(
        engine.retrieval.search_page(
            SearchPageRequest(
                query="nebula",
                filters={"space_ids": [], "payload_families": [], "record_types": ["canonical"]},
            ),
            authority=authority(),
        )
    )["results"][0]
    assert page["record_id"] == decision.page_id
    assert page["revision_id"].startswith("revision_")
    assert page["provenance"]["capture_ids"] == [capture.capture_id for capture in captures]
    response = wire(
        engine.retrieval.read_record(
            RecordReadRequest(
                record_id=page["record_id"], expected_revision_id=page["revision_id"]
            ),
            authority=authority(),
        )
    )
    assert "https://synthetic.example/private/secret" not in response["content"]["text"]
    assert "漢字" in response["content"]["text"]
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(
            RecordReadRequest(
                record_id=page["record_id"], expected_revision_id=page["revision_id"]
            ),
            authority=replace(authority(), space_ids=frozenset()),
        )


def test_cursor_tamper_expiry_rotation_and_copied_root(tmp_path: Path) -> None:
    import json
    import shutil
    from datetime import datetime

    from open_brain_engine.engine import cursors

    clock = [datetime(2026, 9, 1, tzinfo=UTC)]
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile, clock=lambda: clock[0])
    for index in range(2):
        engine.capture.accept(TextPayload("cursor nebula"), delivery_id=str(index))
    request = SearchPageRequest(query="nebula", limit=1)
    first = wire(engine.retrieval.search_page(request, authority=authority()))
    cursor = first["next_cursor"]
    with pytest.raises(T03Error, match="cursor_invalid"):
        engine.retrieval.search_page(
            replace(
                request, cursor=cursor[:50] + ("A" if cursor[50] != "A" else "B") + cursor[51:]
            ),
            authority=authority(),
        )
    identity = json.loads((profile.root / ".open-brain/cursors/identity.json").read_bytes())
    copied = tmp_path / "copy"
    shutil.copytree(profile.root, copied)
    new_engine = BrainEngine.open(compile_single_user_local(copied), clock=lambda: clock[0])
    new_identity = json.loads((copied / ".open-brain/cursors/identity.json").read_bytes())
    assert new_identity["incarnation"] != identity["incarnation"]
    assert not tuple((copied / ".open-brain/cursors").glob("?" * 64 + ".json"))
    with pytest.raises(T03Error, match="cursor_stale"):
        new_engine.retrieval.search_page(replace(request, cursor=cursor), authority=authority())
    assert wire(new_engine.retrieval.search_page(request, authority=authority()))["results"]
    engine.retrieval.rotate_cursors(authority=replace(authority(), owner=True))
    with pytest.raises(T03Error, match="cursor_stale"):
        engine.retrieval.search_page(replace(request, cursor=cursor), authority=authority())
    fresh = wire(engine.retrieval.search_page(request, authority=authority()))["next_cursor"]
    clock[0] = datetime.fromtimestamp(clock[0].timestamp() + cursors.TTL_SECONDS + 1, UTC)
    with pytest.raises(T03Error, match="cursor_stale"):
        engine.retrieval.search_page(replace(request, cursor=fresh), authority=authority())


def test_cursor_capacity_and_key_custody(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from open_brain_engine.engine import cursors

    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    for index in range(2):
        engine.capture.accept(TextPayload("nebula"), delivery_id=str(index))
    request = SearchPageRequest(query="nebula", limit=1)
    monkeypatch.setattr(cursors, "MAX_HANDLES", 1)
    first = wire(engine.retrieval.search_page(request, authority=authority()))
    with pytest.raises(T03Error, match="operation_pending"):
        engine.retrieval.search_page(request, authority=authority())
    key = engine.profile.root / ".open-brain/cursors/key.json"
    assert key.stat().st_mode & 0o777 == 0o600
    key.chmod(0o644)
    with pytest.raises(T03Error, match="operation_pending"):
        engine.retrieval.search_page(
            replace(request, cursor=first["next_cursor"]), authority=authority()
        )
    key.chmod(0o600)
    os.link(key, key.parent / "key-link")
    with pytest.raises(T03Error, match="operation_pending"):
        engine.retrieval.search_page(
            replace(request, cursor=first["next_cursor"]), authority=authority()
        )


def test_cross_process_restart_and_rotation_crash_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import os
    import subprocess
    import sys

    from open_brain_engine.engine.cursors import CursorStore

    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    for index in range(3):
        engine.capture.accept(TextPayload("restart nebula"), delivery_id=str(index))
    request = SearchPageRequest(query="nebula", limit=1)
    first = wire(engine.retrieval.search_page(request, authority=authority()))
    child_request = replace(request, cursor=first["next_cursor"])
    script = """
import json,sys
from pathlib import Path
from open_brain.profile import compile_single_user_local
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, SearchPageRequest
engine=BrainEngine.open(compile_single_user_local(Path(sys.argv[1])))
response=engine.retrieval.search_page(SearchPageRequest(**json.loads(sys.argv[2])),
 authority=EffectiveAuthority('owner','session',frozenset({'search','content-read'}),None))
print(json.dumps(response.to_wire()))
"""
    root = Path(__file__).resolve().parents[5]
    environment = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(
            (str(root / "packages/app/src"), str(root / "packages/engine/src"))
        ),
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(profile.root), json.dumps(child_request.to_wire())],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    expected = wire(engine.retrieval.search_page(child_request, authority=authority()))
    assert json.loads(child.stdout)["results"] == expected["results"]
    original = CursorStore._write

    def interrupted(store: CursorStore, name: str, data: bytes) -> None:
        if name == "key.json":
            raise OSError("synthetic rotation crash")
        original(store, name, data)

    with monkeypatch.context() as patch:
        patch.setattr(CursorStore, "_write", interrupted)
        with pytest.raises(T03Error, match="operation_pending"):
            engine.retrieval.rotate_cursors(authority=replace(authority(), owner=True))
    restarted = BrainEngine.open(profile)
    with pytest.raises(T03Error, match="cursor_stale"):
        restarted.retrieval.search_page(child_request, authority=authority())
    assert not (profile.root / ".open-brain/cursors/rotation.pending").exists()
    (profile.root / ".open-brain/cursors/key.json").write_bytes(b"corrupt")
    reopened = BrainEngine.open(profile)
    with pytest.raises(T03Error, match="cursor_invalid"):
        reopened.retrieval.search_page(child_request, authority=authority())
    reopened.retrieval.rotate_cursors(authority=replace(authority(), owner=True))
    assert wire(reopened.retrieval.search_page(request, authority=authority()))["results"]


def test_projection_precedes_chunks_and_encoded_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from open_brain_engine.engine import paging
    from open_brain_engine.engine.search_projection import public_search_text

    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    body = 'Visible "quoted"\ttext token=synthetic-secret /private/synthetic/file\n' * 100
    capture = engine.capture.accept(TextPayload(body), delivery_id="large")
    monkeypatch.setattr(paging, "MAX_RESPONSE_BYTES", 4000)
    request = RecordReadRequest(
        record_id=capture.capture_id, expected_revision_id=capture.capture_id, target_bytes=65536
    )
    texts = []
    while True:
        response = wire(engine.retrieval.read_record(request, authority=authority()))
        texts.append(response["content"]["text"])
        assert len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()) < 4200
        if response["complete"]:
            break
        request = replace(request, cursor=response["next_cursor"])
    assert "".join(texts) == public_search_text(body, protected_source_reference="manual")
    assert "synthetic-secret" not in "".join(texts)
    assert "/private/synthetic/file" not in "".join(texts)


def test_source_anchor_resolves_current_head_after_authorization(tmp_path: Path) -> None:
    from open_brain_engine.engine import ReferencePayload
    from open_brain_engine.engine.source_intake import SourceRevisionSubmission

    from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission

    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    original = _public_submission(engine.tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
    )

    def submit(sequence: int, head: str | None) -> str:
        capture = replace(
            original,
            payload=ReferencePayload(original.source_reference, f"source revision {sequence}"),
        )
        receipt = engine.sources.submit_revision(
            SourceRevisionSubmission(
                capture=capture,
                namespace=namespace,
                revision_key=str(sequence),
                canonical_sha256=capture.request_sha256(),
                expected_head=head,
                ordering={
                    "kind": "monotonic",
                    "provider_namespace": "synthetic",
                    "epoch": "one",
                    "sequence": sequence,
                },
                expected_control_epoch=0,
            )
        )
        assert receipt.capture_id is not None
        return receipt.capture_id

    first = submit(1, None)
    second = submit(2, first)
    with pytest.raises(T03Error, match="revision_changed"):
        engine.retrieval.read_record(
            RecordReadRequest(record_id=first, expected_revision_id=first), authority=authority()
        )
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(
            RecordReadRequest(record_id=first, expected_revision_id=first),
            authority=replace(authority(), space_ids=frozenset()),
        )
    refreshed = wire(
        engine.retrieval.read_record(
            RecordReadRequest(record_id=first, expected_revision_id=second), authority=authority()
        )
    )
    assert refreshed["record"]["record_id"] == second
    assert refreshed["record"]["revision_id"] == second
    search = wire(
        engine.retrieval.search_page(SearchPageRequest(query="revision"), authority=authority())
    )
    assert [record["record_id"] for record in search["results"]] == [second]


def test_cursor_special_file_refuses_without_blocking(tmp_path: Path) -> None:
    import os

    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    for index in range(2):
        engine.capture.accept(TextPayload("nebula"), delivery_id=str(index))
    request = SearchPageRequest(query="nebula", limit=1)
    first = wire(engine.retrieval.search_page(request, authority=authority()))
    key = engine.profile.root / ".open-brain/cursors/key.json"
    key.unlink()
    os.mkfifo(key, 0o600)
    with pytest.raises(T03Error, match="operation_pending"):
        engine.retrieval.search_page(
            replace(request, cursor=first["next_cursor"]), authority=authority()
        )


def test_markdown_import_paging_reads_validated_full_blob(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    vault = tmp_path / "vault"
    vault.mkdir()
    original = "# Imported title\n\nFull synthetic launch content 漢字\n"
    (vault / "one.md").write_text(original)
    engine.markdown_import.import_directory(str(vault), confirm=lambda _value: True)
    result = wire(
        engine.retrieval.search_page(
            SearchPageRequest(query="synthetic launch"), authority=authority()
        )
    )["results"][0]
    request = RecordReadRequest(
        record_id=result["record_id"], expected_revision_id=result["revision_id"]
    )
    read = wire(engine.retrieval.read_record(request, authority=authority()))
    assert original in read["content"]["text"]
    blob = next((engine.profile.root / "sources/blobs").rglob("?" * 64))
    blob.write_bytes(b"tampered synthetic blob")
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(request, authority=authority())
