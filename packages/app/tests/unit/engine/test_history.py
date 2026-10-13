from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    DecisionOutcome,
    ProposalDraft,
    ReferencePayload,
    SourceInspectRequest,
    SourceWithdrawRequest,
    TextPayload,
)
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.source_intake import SourceRevisionSubmission
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    SearchPageRequest,
    T03Error,
)

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission
from packages.app.tests.unit.engine.test_paging import (
    capture_work,
    external_authority,
    scoped_authority,
)


def wire(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value.to_wire())


def authority() -> EffectiveAuthority:
    return EffectiveAuthority("owner", "history", frozenset({"history-read"}), None)


def test_source_history_exact_revision_grant_and_current_scope(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    original = _public_submission(engine.tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
    )
    head = None
    ids = []
    texts = ["First historical 漢字" * 2000, "Second source", "Third source"]
    for sequence, text in enumerate(texts):
        capture = replace(original, payload=ReferencePayload(original.source_reference, text))
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
        head = receipt.capture_id
        assert head is not None
        ids.append(head)
    request = HistoryListRequest(record_id=ids[0], limit=2)
    page = wire(engine.history.list_history(request, authority=authority()))
    assert [entry["revision_id"] for entry in page["entries"]] == ids[::-1][:2]
    assert [entry["is_current"] for entry in page["entries"]] == [True, False]
    engine.capture.accept(
        TextPayload("hidden history mutation"),
        delivery_id="history.hidden.secret",
        privacy_tier=PrivacyTier.SECRET,
    )
    tail = wire(
        engine.history.list_history(
            replace(request, cursor=page["next_cursor"]), authority=authority()
        )
    )
    assert tail["entries"][0]["revision_id"] == ids[0]
    assert tail["complete"]
    read = RecordReadRequest(record_id=ids[-1], expected_revision_id=ids[0], target_bytes=4096)
    chunks = []
    while True:
        response = wire(engine.history.read_history(read, authority=authority()))
        chunks.append(response["content"]["text"])
        if response["complete"]:
            break
        read = replace(read, cursor=response["next_cursor"])
    assert texts[0] in "".join(chunks)
    with pytest.raises(T03Error, match="unsupported_capability"):
        engine.retrieval.read_record(read, authority=authority())
    with pytest.raises(T03Error, match="unsupported_capability"):
        engine.history.read_history(
            read, authority=replace(authority(), capabilities=frozenset({"content-read"}))
        )
    with pytest.raises(T03Error, match="not_found"):
        engine.history.list_history(request, authority=replace(authority(), space_ids=frozenset()))
    other = engine.capture.accept(TextPayload(texts[0]), delivery_id="independent")
    with pytest.raises(T03Error, match="not_found"):
        engine.history.read_history(
            RecordReadRequest(record_id=other.capture_id, expected_revision_id=ids[0]),
            authority=authority(),
        )


def test_withdrawn_source_retains_three_owner_history_revisions(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    owner = replace(authority(), owner=True)
    original = _public_submission(engine.tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
    )
    texts = ["First retained revision 漢字🙂\n" * 500, "Second retained revision", "Third revision"]
    ids: list[str] = []
    head = None
    for sequence, text in enumerate(texts):
        capture = replace(original, payload=TextPayload(text))
        receipt = engine.sources.submit_revision(
            SourceRevisionSubmission(
                capture=capture,
                namespace=namespace,
                revision_key=str(sequence),
                canonical_sha256=capture.request_sha256(),
                expected_head=head,
                ordering={"kind": "unordered"}
                if sequence == 0
                else {"kind": "predecessor", "revision_key": str(sequence - 1)},
                expected_control_epoch=0,
            )
        )
        head = receipt.capture_id
        assert head is not None
        ids.append(head)
    assert receipt.source_id is not None
    source_id = receipt.source_id
    with open_local_database_read_only(profile) as connection:
        retained = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM source_revisions WHERE source_id=? ORDER BY sequence", (source_id,)
            )
        ]
        evidence = {
            row["source_path"]: bytes(row["source_bytes"])
            for row in connection.execute(
                "SELECT source_path,source_bytes FROM source_revisions WHERE source_id=?",
                (source_id,),
            )
        }
    assert len(retained) == len(evidence) == 3
    assert all((profile.root / path).read_bytes() == data for path, data in evidence.items())
    inspection = engine.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    withdrawal = engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.owner-history",
            source_id=source_id,
            expected_head=ids[-1],
            expected_lifecycle_version=inspection.lifecycle_version,
            brain_id=inspection.destination_brain_id,
            issuer_epoch=inspection.issuer_epoch,
            reason_code="owner_choice",
        ),
        authority=owner,
    )
    assert withdrawal.lifecycle == "retired"

    listing = HistoryListRequest(record_id=ids[0], limit=1)
    entries = []
    for index in range(3):
        page = wire(engine.history.list_history(listing, authority=owner))
        assert len(page["entries"]) == 1
        entries.extend(page["entries"])
        assert page["complete"] is (index == 2)
        if index < 2:
            assert page["next_cursor"] is not None
            listing = replace(listing, cursor=page["next_cursor"])
        else:
            assert page["next_cursor"] is None
    assert [entry["revision_id"] for entry in entries] == ids[::-1]
    assert [entry["predecessor_revision_id"] for entry in entries] == [ids[1], ids[0], None]
    assert all(entry["lifecycle"] == "retired" for entry in entries)
    assert all(entry["availability"] == "missing" for entry in entries)

    for capture_id, text in zip(ids, texts, strict=True):
        reading = RecordReadRequest(
            record_id=ids[-1], expected_revision_id=capture_id, target_bytes=4096
        )
        chunks: list[bytes] = []
        offset = 0
        for _ in range(20):
            response = wire(engine.history.read_history(reading, authority=owner))
            assert response["record"]["revision_id"] == capture_id
            assert response["record"]["source_id"] == source_id
            assert response["start_byte"] == offset
            chunk = response["content"]["text"].encode("utf-8")
            assert 0 < len(chunk) <= 4096
            chunks.append(chunk)
            offset += len(chunk)
            assert response["end_byte"] == offset
            if response["complete"]:
                assert response["next_cursor"] is None
                break
            assert response["next_cursor"] is not None
            reading = replace(reading, cursor=response["next_cursor"])
        else:
            pytest.fail("retained history did not complete within its bounded fixture")
        assert b"".join(chunks) == text.encode("utf-8")
        if capture_id == ids[0]:
            assert len(chunks) > 1

    with open_local_database_read_only(profile) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM source_revisions WHERE source_id=? ORDER BY sequence", (source_id,)
            )
        ] == retained
    assert all((profile.root / path).read_bytes() == data for path, data in evidence.items())


@pytest.mark.parametrize("caller_kind", ["owner", "external", "local_agent"])
def test_withdrawal_invalidates_prevalidated_search_and_history_cursors(
    tmp_path: Path, caller_kind: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    owner = replace(authority(), owner=True)
    original = replace(
        _public_submission(engine.tasks),
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.WORK,
            reason=PrivacyReason.POLICY_WORK,
            policy_version="privacy-v1",
            authority=Authority(cloud=True, external_egress=True),
        ),
    )
    texts = [
        "withdrawal cursor nebula 漢字🙂\n" * 500,
        "Second revision",
        "withdrawal cursor nebula head",
    ]
    ids: list[str] = []
    head = None
    namespace = dict(
        connector_name="synthetic",
        connection_id="cursor",
        resource_id="cursor",
        external_id="cursor",
    )
    for index, text in enumerate(texts):
        capture = replace(original, payload=TextPayload(text))
        receipt = engine.sources.submit_revision(
            SourceRevisionSubmission(
                capture=capture,
                namespace=namespace,
                revision_key=str(index),
                canonical_sha256=capture.request_sha256(),
                expected_head=head,
                ordering={"kind": "unordered"}
                if index == 0
                else {"kind": "predecessor", "revision_key": str(index - 1)},
                expected_control_epoch=0,
            )
        )
        head = receipt.capture_id
        assert head is not None
        ids.append(head)
    assert receipt.source_id is not None
    control = capture_work(
        engine,
        delivery_id="withdrawal.cursor.control",
        text="withdrawal cursor nebula control",
        external_egress=True,
    )
    caller = (
        owner
        if caller_kind == "owner"
        else replace(
            external_authority(engine)
            if caller_kind == "external"
            else scoped_authority(PrivacyTier.WORK),
            capabilities=frozenset({"search", "content-read", "history-read"}),
        )
    )
    search = SearchPageRequest(query="withdrawal cursor nebula", limit=1)
    listing = HistoryListRequest(record_id=ids[-1], limit=1)
    reading = RecordReadRequest(record_id=ids[-1], expected_revision_id=ids[0], target_bytes=4096)
    first_search = wire(engine.retrieval.search_page(search, authority=caller))
    first_history = wire(engine.history.list_history(listing, authority=caller))
    first_read = wire(engine.history.read_history(reading, authority=caller))
    assert not first_search["complete"] and first_search["next_cursor"] is not None
    assert not first_history["complete"] and first_history["next_cursor"] is not None
    assert not first_read["complete"] and first_read["next_cursor"] is not None
    search_tail = replace(search, cursor=first_search["next_cursor"])
    history_tail = replace(listing, cursor=first_history["next_cursor"])
    read_tail = replace(reading, cursor=first_read["next_cursor"])

    # Prove every saved cursor is usable after all setup mutations are finished.
    second_search = wire(engine.retrieval.search_page(search_tail, authority=caller))
    assert {row["record_id"] for row in first_search["results"] + second_search["results"]} == {
        ids[-1],
        control,
    }
    assert (
        wire(engine.history.list_history(history_tail, authority=caller))["entries"][0][
            "revision_id"
        ]
        == ids[1]
    )
    second_read = wire(engine.history.read_history(read_tail, authority=caller))
    assert second_read["start_byte"] == first_read["end_byte"]
    assert second_read["content"]["text"]
    inspection = engine.sources.inspect(
        SourceInspectRequest(source_id=receipt.source_id), authority=owner
    )
    engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.cursor",
            source_id=receipt.source_id,
            expected_head=ids[-1],
            expected_lifecycle_version=inspection.lifecycle_version,
            brain_id=inspection.destination_brain_id,
            issuer_epoch=inspection.issuer_epoch,
            reason_code="owner_choice",
        ),
        authority=owner,
    )

    with pytest.raises(T03Error, match="cursor_stale"):
        engine.retrieval.search_page(search_tail, authority=caller)
    history_error = "cursor_stale" if caller_kind == "owner" else "not_found"
    with pytest.raises(T03Error, match=history_error):
        engine.history.list_history(history_tail, authority=caller)
    with pytest.raises(T03Error, match=history_error):
        engine.history.read_history(read_tail, authority=caller)
    assert [
        entry["revision_id"]
        for entry in wire(engine.history.list_history(replace(listing, limit=10), authority=owner))[
            "entries"
        ]
    ] == ids[::-1]
    assert (
        wire(engine.history.read_history(reading, authority=owner))["content"]["text"]
        == first_read["content"]["text"]
    )
    assert {
        row["record_id"]
        for row in wire(engine.retrieval.search_page(search, authority=caller))["results"]
    } == {control}


def test_canonical_history_causal_predecessor_and_exact_bytes(tmp_path: Path) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    )
    space = engine.inbox.spaces()[0]
    capture = engine.capture.accept(
        TextPayload("Historical source"), delivery_id="source", space_id=space.space_id
    )
    first = engine.review.propose(
        (capture.capture_id,),
        (ProposalDraft("Initial title", "Initial publication 漢字"),),
        delivery_id="first",
    )[0]
    decision = engine.review.decide(
        first.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="first.decision",
        expected_review_digest=first.review_digest,
    )
    second = engine.review.propose(
        (capture.capture_id,),
        (ProposalDraft("Updated title", "Updated publication"),),
        delivery_id="second",
        target_page_id=decision.page_id,
    )[0]
    engine.review.decide(
        second.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="second.decision",
        expected_review_digest=second.review_digest,
    )
    assert decision.page_id is not None
    page = wire(
        engine.history.list_history(
            HistoryListRequest(record_id=decision.page_id), authority=authority()
        )
    )
    entries = page["entries"]
    assert len(entries) == 2 and entries[0]["is_current"] and not entries[1]["is_current"]
    assert entries[0]["predecessor_revision_id"] == entries[1]["revision_id"]
    read = wire(
        engine.history.read_history(
            RecordReadRequest(
                record_id=decision.page_id, expected_revision_id=entries[1]["revision_id"]
            ),
            authority=authority(),
        )
    )
    assert "Initial publication 漢字" in read["content"]["text"]
    assert read["record"]["title"] == "Initial title"


def test_migrated_ungrouped_orphan_is_history_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import local_schema
    from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS

    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])
        old = BrainEngine.open(profile)
        submission = _public_submission(old.tasks)
        submission = replace(
            submission,
            payload=ReferencePayload(submission.source_reference, "Synthetic public-job capture"),
        )
        orphan = old.capture.submit(submission)
        old.capture.submit(
            replace(
                submission,
                payload=ReferencePayload(
                    submission.source_reference, "Changed old writer delivery"
                ),
            )
        )
    from open_brain_engine.engine import coordinate_local_migration

    coordinate_local_migration(profile)
    current = BrainEngine.open(profile)
    owner = replace(authority(), owner=True)
    page = wire(
        current.history.list_history(
            HistoryListRequest(record_id=orphan.capture_id), authority=owner
        )
    )
    assert len(page["entries"]) == 1 and not page["entries"][0]["is_current"]
    assert page["entries"][0]["predecessor_revision_id"] is None
    assert "Ungrouped" in page["entries"][0]["reason"]
    request = RecordReadRequest(record_id=orphan.capture_id, expected_revision_id=orphan.capture_id)
    assert (
        "Synthetic public-job capture"
        in wire(current.history.read_history(request, authority=owner))["content"]["text"]
    )
    with pytest.raises(T03Error, match="not_found"):
        current.retrieval.read_record(
            request, authority=replace(authority(), capabilities=frozenset({"content-read"}))
        )
