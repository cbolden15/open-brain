from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.cursors import binding_digest
from open_brain_engine.engine.paging import authority_binding
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


def scoped_authority(
    *tiers: PrivacyTier, session: str = "scoped-session"
) -> EffectiveAuthority:
    return EffectiveAuthority(
        "synthetic-principal",
        session,
        frozenset({"search", "content-read"}),
        None,
        allowed_read_tiers=frozenset(tiers),
    )


def test_cursor_authority_binding_names_and_binds_every_policy_dimension() -> None:
    brain_id = derive_brain_id("tenant_00000000-0000-4000-8000-000000000001")
    other_brain_id = derive_brain_id("tenant_00000000-0000-4000-8000-000000000002")
    baseline = EffectiveAuthority(
        "synthetic-principal",
        "synthetic-session",
        frozenset({"search"}),
        None,
        authorization_generation=1,
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK}),
        allowed_capture_tiers=frozenset({PrivacyTier.PERSONAL}),
        brain_id=brain_id,
        issuer_epoch=7,
    )
    binding = authority_binding(baseline)
    assert set(binding) == {
        "principal_id",
        "session_id",
        "capabilities",
        "owner",
        "space_ids",
        "allowed_read_tiers",
        "allowed_capture_tiers",
        "egress_mode",
        "provider_id",
        "consent_id",
        "authorization_generation",
        "brain_id",
        "issuer_epoch",
    }
    assert "epoch" not in binding
    external = replace(
        baseline,
        egress_mode=EgressMode.EXTERNAL_PROVIDER,
        provider_id="synthetic-provider",
        consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    variants = {
        "principal_id": replace(baseline, principal_id="other-principal"),
        "session_id": replace(baseline, session_id="other-session"),
        "capabilities": replace(baseline, capabilities=frozenset({"content-read"})),
        "owner": replace(baseline, owner=True),
        "space_ids": replace(
            baseline, space_ids=frozenset({"space_00000000-0000-4000-8000-000000000001"})
        ),
        "allowed_read_tiers": replace(
            baseline, allowed_read_tiers=frozenset({PrivacyTier.PUBLIC})
        ),
        "allowed_capture_tiers": replace(
            baseline, allowed_capture_tiers=frozenset({PrivacyTier.WORK})
        ),
        "egress_mode": external,
        "provider_id": replace(external, provider_id="other-provider"),
        "consent_id": replace(
            external, consent_id="consent_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        ),
        "authorization_generation": replace(baseline, authorization_generation=2),
        "brain_id": replace(baseline, brain_id=other_brain_id),
        "issuer_epoch": replace(baseline, issuer_epoch=8),
    }
    baseline_digest = binding_digest(binding)
    for field, variant in variants.items():
        changed = authority_binding(variant)
        assert changed[field] != binding[field]
        assert binding_digest(changed) != baseline_digest


def test_paged_search_enforces_the_principal_tier_matrix_and_owner_access(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    captured = {
        tier: engine.capture.accept(
            TextPayload(f"tier matrix nebula {tier.value}"),
            delivery_id=f"tier.matrix.{tier.value}",
            title=f"Tier {tier.value}",
            privacy_tier=tier,
        ).capture_id
        for tier in PrivacyTier
    }
    request = SearchPageRequest(query="tier matrix nebula", limit=100)
    permitted_tiers = (PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL)

    for mask in range(1 << len(permitted_tiers)):
        allowed = tuple(
            tier for index, tier in enumerate(permitted_tiers) if mask & (1 << index)
        )
        results = wire(
            engine.retrieval.search_page(request, authority=scoped_authority(*allowed))
        )["results"]
        assert {row["record_id"] for row in results} == {captured[tier] for tier in allowed}

    owner = EffectiveAuthority(
        "synthetic-owner",
        "owner-session",
        frozenset({"search", "content-read"}),
        None,
        owner=True,
    )
    assert {
        row["record_id"]
        for row in wire(engine.retrieval.search_page(request, authority=owner))["results"]
    } == set(captured.values())


def test_hidden_tiers_do_not_change_visible_ranking_or_page_boundaries(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    visible = {
        engine.capture.accept(
            TextPayload(term),
            delivery_id=f"ranking.visible.{term}",
            title=term,
            privacy_tier=PrivacyTier.PUBLIC,
        ).capture_id: term
        for term in ("alpha", "beta")
    }
    request = SearchPageRequest(query="alpha beta", limit=1)
    public = scoped_authority(PrivacyTier.PUBLIC)

    first = wire(engine.retrieval.search_page(request, authority=public))
    second = wire(
        engine.retrieval.search_page(
            replace(request, cursor=first["next_cursor"]), authority=public
        )
    )
    baseline = [first["results"], second["results"]]
    flooded_term = visible[first["results"][0]["record_id"]]

    for index in range(20):
        engine.capture.accept(
            TextPayload(" ".join([flooded_term] * 4)),
            delivery_id=f"ranking.hidden.{index}",
            title=flooded_term,
            privacy_tier=PrivacyTier.PERSONAL,
        )

    paired_first = wire(engine.retrieval.search_page(request, authority=public))
    paired_second = wire(
        engine.retrieval.search_page(
            replace(request, cursor=paired_first["next_cursor"]), authority=public
        )
    )
    assert [paired_first["results"], paired_second["results"]] == baseline
    assert [paired_first["complete"], paired_second["complete"]] == [False, True]


def test_scoped_cursor_survives_hidden_mutation_but_stales_on_visible_mutation(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Visible", "Hidden"))
    )
    spaces = {space.name: space.space_id for space in engine.inbox.spaces()}
    for index in range(2):
        engine.capture.accept(
            TextPayload("cursor visibility nebula"),
            delivery_id=f"cursor.visible.{index}",
            space_id=spaces["Visible"],
            privacy_tier=PrivacyTier.PUBLIC,
        )
    request = SearchPageRequest(query="cursor visibility nebula", limit=1)
    public = replace(
        scoped_authority(PrivacyTier.PUBLIC), space_ids=frozenset({spaces["Visible"]})
    )

    scoped_first = wire(engine.retrieval.search_page(request, authority=public))
    expected_second = wire(
        engine.retrieval.search_page(
            replace(request, cursor=scoped_first["next_cursor"]), authority=public
        )
    )
    owner = replace(authority(), owner=True)
    owner_first = wire(engine.retrieval.search_page(request, authority=owner))

    engine.capture.accept(
        TextPayload("cursor visibility nebula"),
        delivery_id="cursor.hidden.personal",
        space_id=spaces["Visible"],
        privacy_tier=PrivacyTier.PERSONAL,
    )
    engine.capture.accept(
        TextPayload("cursor visibility nebula"),
        delivery_id="cursor.hidden.space",
        space_id=spaces["Hidden"],
        privacy_tier=PrivacyTier.PUBLIC,
    )

    assert wire(
        engine.retrieval.search_page(
            replace(request, cursor=scoped_first["next_cursor"]), authority=public
        )
    ) == expected_second
    with pytest.raises(T03Error, match="cursor_stale"):
        engine.retrieval.search_page(
            replace(request, cursor=owner_first["next_cursor"]), authority=owner
        )

    refreshed = wire(engine.retrieval.search_page(request, authority=public))
    engine.capture.accept(
        TextPayload("cursor visibility nebula"),
        delivery_id="cursor.visible.new",
        space_id=spaces["Visible"],
        privacy_tier=PrivacyTier.PUBLIC,
    )
    with pytest.raises(T03Error, match="cursor_stale"):
        engine.retrieval.search_page(
            replace(request, cursor=refreshed["next_cursor"]), authority=public
        )


def test_retrieval_readers_overlap_and_fence_writer_through_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import CaptureCustodyReceipt
    from open_brain_engine.engine import capture as capture_module
    from open_brain_engine.engine import records as records_module
    from open_brain_engine.storage.filesystem import read_confined as original_read

    monkeypatch.setattr(capture_module, "_WRITER_WAIT_TIMEOUT_SECONDS", 0.05)

    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    capture = engine.capture.accept(
        TextPayload("reader snapshot nebula"), delivery_id="reader.snapshot"
    )
    request = RecordReadRequest(
        record_id=capture.capture_id,
        expected_revision_id=capture.capture_id,
    )
    readers_entered = threading.Barrier(3)
    release_readers = threading.Event()
    results: list[dict[str, Any]] = []
    failures: list[BaseException] = []

    def paused_read(*args: Any, **kwargs: Any) -> bytes | None:
        readers_entered.wait(timeout=3)
        if not release_readers.wait(timeout=3):
            raise AssertionError("reader release timed out")
        return original_read(*args, **kwargs)

    def read() -> None:
        try:
            results.append(wire(engine.retrieval.read_record(request, authority=authority())))
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    monkeypatch.setattr(records_module, "read_confined", paused_read)
    threads = [threading.Thread(target=read) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        readers_entered.wait(timeout=3)
        queued = engine.capture.accept(
            TextPayload("must wait for readers"), delivery_id="reader.blocked.writer"
        )
        assert isinstance(queued, CaptureCustodyReceipt)
    finally:
        release_readers.set()
        for thread in threads:
            thread.join(timeout=3)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert [result["content"]["text"] for result in results] == [
        "reader snapshot nebula",
        "reader snapshot nebula",
    ]
    assert engine.capture.accept(
        TextPayload("must wait for readers"), delivery_id="reader.blocked.writer"
    ).duplicate is False


def test_concurrent_cursor_allocation_is_unique(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    for index in range(3):
        engine.capture.accept(
            TextPayload("concurrent cursor nebula"), delivery_id=f"cursor.concurrent.{index}"
        )
    request = SearchPageRequest(query="concurrent cursor nebula", limit=1)
    start = threading.Barrier(9)
    cursors: list[str] = []
    failures: list[BaseException] = []

    def search() -> None:
        try:
            start.wait(timeout=3)
            response = wire(engine.retrieval.search_page(request, authority=authority()))
            cursors.append(cast(str, response["next_cursor"]))
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    threads = [threading.Thread(target=search) for _ in range(8)]
    for thread in threads:
        thread.start()
    start.wait(timeout=3)
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert len(cursors) == len(set(cursors)) == 8


def test_record_projector_denies_disallowed_source_and_canonical_before_content_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import CaptureAction
    from open_brain_engine.engine import records as records_module

    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    )
    space = engine.inbox.spaces()[0]
    capture = engine.capture.accept(
        TextPayload("classified canonical nebula"),
        delivery_id="tier.projector.personal",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
        privacy_tier=PrivacyTier.PERSONAL,
    )
    personal = scoped_authority(PrivacyTier.PERSONAL)
    canonical = wire(
        engine.retrieval.search_page(
            SearchPageRequest(
                query="classified canonical nebula",
                filters={"space_ids": [], "payload_families": [], "record_types": ["canonical"]},
            ),
            authority=personal,
        )
    )["results"][0]
    requests = (
        RecordReadRequest(
            record_id=capture.capture_id,
            expected_revision_id=capture.capture_id,
        ),
        RecordReadRequest(
            record_id=canonical["record_id"],
            expected_revision_id=canonical["revision_id"],
        ),
    )
    for request in requests:
        assert wire(engine.retrieval.read_record(request, authority=personal))["content"]["text"]

    reads: list[str] = []

    def read_if_called(*_args: object, **_kwargs: object) -> None:
        reads.append("content read")
        return None

    monkeypatch.setattr(records_module, "read_confined", read_if_called)
    public = scoped_authority(PrivacyTier.PUBLIC)
    for request in requests:
        with pytest.raises(T03Error, match="not_found"):
            engine.retrieval.read_record(request, authority=public)
    assert reads == []


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
        if len(chunks) == 1:
            engine.capture.accept(
                TextPayload("hidden chunk mutation"),
                delivery_id="unicode.hidden.secret",
                privacy_tier=PrivacyTier.SECRET,
            )
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


def test_same_inode_directory_rename_invalidates_cursor(tmp_path: Path) -> None:
    import json

    old_root = tmp_path / "before"
    engine = BrainEngine.open(compile_single_user_local(old_root))
    for index in range(2):
        engine.capture.accept(TextPayload("relocation nebula"), delivery_id=str(index))
    request = SearchPageRequest(query="nebula", limit=1)
    cursor = wire(engine.retrieval.search_page(request, authority=authority()))["next_cursor"]
    before = json.loads((old_root / ".open-brain/cursors/identity.json").read_bytes())
    inode = old_root.stat().st_ino
    new_root = tmp_path / "after"
    old_root.rename(new_root)
    assert new_root.stat().st_ino == inode
    moved = BrainEngine.open(compile_single_user_local(new_root))
    after = json.loads((new_root / ".open-brain/cursors/identity.json").read_bytes())
    assert after["incarnation"] != before["incarnation"]
    with pytest.raises(T03Error, match="cursor_stale"):
        moved.retrieval.search_page(replace(request, cursor=cursor), authority=authority())
    assert wire(moved.retrieval.search_page(request, authority=authority()))["results"]


def test_cursor_custody_failure_preserves_legacy_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.cursors import CursorStore

    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    engine.capture.accept(TextPayload("legacy nebula"), delivery_id="legacy")
    directory = profile.root / ".open-brain/cursors"
    directory.chmod(0o755)
    reopened = BrainEngine.open(profile)
    assert reopened.retrieval.search("nebula")
    with pytest.raises(T03Error, match="operation_pending"):
        reopened.retrieval.search_page(SearchPageRequest(query="nebula"), authority=authority())
    directory.chmod(0o700)

    def unavailable(_store: CursorStore, _name: str, _data: bytes) -> None:
        raise OSError("synthetic cursor filesystem unavailable")

    monkeypatch.setattr(CursorStore, "_write", unavailable)
    reopened = BrainEngine.open(profile)
    assert reopened.retrieval.search("nebula")
    with pytest.raises(T03Error, match="operation_pending"):
        reopened.retrieval.search_page(SearchPageRequest(query="nebula"), authority=authority())


@pytest.mark.parametrize(
    "producer", ["reviewed", "automatic", "imported_reviewed", "imported_automatic"]
)
def test_canonical_read_checks_independent_registered_publication_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, producer: str
) -> None:
    import base64
    import json
    from hashlib import sha256
    from uuid import uuid4

    from open_brain_engine.core.ids import portable_canonical_json_bytes
    from open_brain_engine.engine import CaptureAction, DecisionOutcome, ProposalDraft, local_schema
    from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS

    with monkeypatch.context() as historical:
        if producer.startswith("imported_"):
            historical.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
            historical.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])
        engine = BrainEngine.open(
            compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
        )
        space = engine.inbox.spaces()[0]
        capture = engine.capture.accept(
            TextPayload("Original publication body"),
            delivery_id="source",
            space_id=space.space_id,
            action=CaptureAction.CANONICAL_NOTE
            if producer.endswith("automatic")
            else CaptureAction.QUICK,
        )
        if producer.endswith("reviewed"):
            proposal = engine.review.propose(
                (capture.capture_id,),
                (ProposalDraft("Publication", "Original publication body"),),
                delivery_id="proposal",
            )[0]
            engine.review.decide(
                proposal.proposal_id,
                DecisionOutcome.APPROVED,
                delivery_id="decision",
                expected_review_digest=proposal.review_digest,
            )
        if producer.startswith("imported_"):
            export = tmp_path / "export"
            engine.portability.export(export, export_id="export_" + str(uuid4()))
    if producer.startswith("imported_"):
        control = BrainEngine.open(compile_single_user_local(tmp_path / "control"))
        control.portability.import_clean(
            tmp_path / "export", tmp_path / "imported", import_id="import_" + str(uuid4())
        )
        engine = BrainEngine.open(compile_single_user_local(tmp_path / "imported"))
    hit = wire(
        engine.retrieval.search_page(
            SearchPageRequest(
                query="publication",
                filters={"space_ids": [], "payload_families": [], "record_types": ["canonical"]},
            ),
            authority=authority(),
        )
    )["results"][0]
    request = RecordReadRequest(record_id=hit["record_id"], expected_revision_id=hit["revision_id"])
    assert (
        "Original publication body"
        in wire(engine.retrieval.read_record(request, authority=authority()))["content"]["text"]
    )
    publication_path = next((engine.profile.root / "history/publications").rglob("*.json"))
    publication = json.loads(publication_path.read_bytes())
    changed = base64.b64decode(publication["published_bytes_base64"]).replace(
        b"Original publication body", b"Tampered publication body"
    )
    publication["published_bytes_base64"] = base64.b64encode(changed).decode()
    publication["published_sha256"] = sha256(changed).hexdigest()
    publication_path.write_bytes(portable_canonical_json_bytes(publication))
    (engine.profile.root / publication["published_path"]).write_bytes(changed)
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(request, authority=authority())
