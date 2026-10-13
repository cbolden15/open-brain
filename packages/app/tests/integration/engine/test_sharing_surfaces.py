from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    CaptureSubmission,
    DecisionOutcome,
    ProposalDraft,
    ReferencePayload,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing_contracts import SharingDecisionRequest, SharingRevokeRequest
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.engine.t03_contracts import (
    DecisionHistoryRequest,
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    RelationshipDecideRequest,
    RelationshipListRequest,
    SearchPageRequest,
    SourceRouteRequest,
    T03Error,
)
from open_brain_engine.portable.v1 import validate_portable_write
from open_brain_engine.portable.v4 import canonical_revision_id
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission
from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source
from packages.engine.tests.contract import test_portable_brain_v1 as legacy_fixture


def _external(brain_id: str, issuer_epoch: int, provider_id: str) -> EffectiveAuthority:
    return EffectiveAuthority(
        "synthetic-agent",
        "synthetic-session",
        frozenset({"search", "content-read"}),
        None,
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC}),
        egress_mode=EgressMode.EXTERNAL_PROVIDER,
        provider_id=provider_id,
        consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
    )


def _import_public_legacy_unbound_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[BrainEngine, EffectiveAuthority, RecordReadRequest]:
    def public_policy() -> dict[str, object]:
        return {
            "authority": {"cloud": True, "external_egress": True},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "policy_public",
            "tier": "public",
        }

    producer_decision = legacy_fixture._decision

    def coherent_decision(*args: Any, **kwargs: Any) -> dict[str, object]:
        value = producer_decision(*args, **kwargs)
        value["recorded_at"] = "2026-08-30T12:10:00Z"
        return value

    with monkeypatch.context() as patch:
        patch.setattr(legacy_fixture, "_privacy", public_policy)
        patch.setattr(legacy_fixture, "_decision", coherent_decision)
        archive = legacy_fixture._root(tmp_path / "legacy-producer")
    assert validated_portable_snapshot(archive).manifest["schema_version"] == 1
    control = BrainEngine.open(compile_single_user_local(tmp_path / "control"))
    imported = tmp_path / "imported"
    receipt = control.portability.import_clean(
        archive, imported, import_id="import_" + str(uuid4())
    )
    assert receipt.schema_version == 1
    current = BrainEngine.open(compile_single_user_local(imported))
    with open_local_database_read_only(current.profile) as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        assert (
            connection.execute(
                "SELECT count(*) FROM review_page_heads WHERE page_id=?", (legacy_fixture.PAGE_ID,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM captures WHERE page_id=? AND publication_id IS NOT NULL",
                (legacy_fixture.PAGE_ID,),
            ).fetchone()[0]
            == 0
        )
    external = _external(identity["brain_id"], identity["issuer_epoch"], "openai")
    reading = RecordReadRequest(
        record_id=legacy_fixture.PAGE_ID,
        expected_revision_id=canonical_revision_id(
            "publication_123e4567-e89b-42d3-a456-42661417400a"
        ),
    )
    return current, external, reading


def test_imported_public_legacy_unbound_page_is_searchable_when_directly_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current, external, reading = _import_public_legacy_unbound_page(tmp_path, monkeypatch)
    for engine in (current, BrainEngine.open(current.profile)):
        engine.portability.rebuild_index()
        direct = cast(
            dict[str, Any], engine.retrieval.read_record(reading, authority=external).to_wire()
        )
        assert direct["content"]["text"]
        chunk_request = replace(reading, target_bytes=8)
        first_chunk = cast(
            dict[str, Any],
            engine.retrieval.read_record(chunk_request, authority=external).to_wire(),
        )
        assert first_chunk["next_cursor"] is not None
        second_chunk = cast(
            dict[str, Any],
            engine.retrieval.read_record(
                replace(chunk_request, cursor=first_chunk["next_cursor"]), authority=external
            ).to_wire(),
        )
        assert second_chunk["start_byte"] == first_chunk["end_byte"]
        assert second_chunk["content"]["text"]
        search = SearchPageRequest(query="Synthetic", limit=1)
        first = cast(
            dict[str, Any], engine.retrieval.search_page(search, authority=external).to_wire()
        )
        assert first["next_cursor"] is not None
        found = {row["record_id"] for row in first["results"]}
        cursor = first["next_cursor"]
        for _ in range(10):
            if cursor is None:
                break
            page = cast(
                dict[str, Any],
                engine.retrieval.search_page(
                    replace(search, cursor=cursor), authority=external
                ).to_wire(),
            )
            assert page["results"]
            found.update(row["record_id"] for row in page["results"])
            cursor = page["next_cursor"]
        assert cursor is None
        assert legacy_fixture.PAGE_ID in found


@pytest.mark.parametrize(
    "damage", ["missing_publication", "ambiguous_publication", "denied_secondary"]
)
def test_legacy_current_publication_damage_refuses_without_hidden_cursor_influence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    engine, external, reading = _import_public_legacy_unbound_page(tmp_path, monkeypatch)
    assert engine.retrieval.read_record(reading, authority=external).to_wire()["content"]
    baseline = cast(
        dict[str, Any],
        engine.retrieval.search_page(
            SearchPageRequest(query="Synthetic", limit=10), authority=external
        ).to_wire(),
    )
    assert legacy_fixture.PAGE_ID in {row["record_id"] for row in baseline["results"]}
    denied = None
    if damage == "denied_secondary":
        denied = engine.capture.submit(
            CaptureSubmission.for_local_owner(
                profile=engine.profile,
                payload=TextPayload("Synthetic denied secondary"),
                delivery_id="synthetic.legacy.denied-secondary",
            )
        )
    with engine._store.transaction() as connection:
        original = dict(
            connection.execute(
                "SELECT * FROM decisions WHERE publication_id IS NOT NULL"
            ).fetchone()
        )
        publication_id = original["publication_id"]
        revision_id = canonical_revision_id(publication_id)
        if damage == "missing_publication":
            missing = "history/publications/2026/08/publication_" + str(uuid4()) + ".json"
            assert not (engine.profile.root / missing).exists()
            connection.execute(
                "UPDATE decisions SET publication_path=? WHERE publication_id=?",
                (missing, publication_id),
            )
        elif damage == "ambiguous_publication":
            # A second independently valid retained publication has identical current
            # bytes. This disposable corrupted ledger must not choose either one.
            copied = json.loads((engine.profile.root / original["publication_path"]).read_bytes())
            second_id = "publication_" + str(uuid4())
            second_decision = "decision_" + str(uuid4())
            copied.update(publication_id=second_id, decision_id=second_decision)
            second_path = f"history/publications/2026/08/{second_id}.json"
            raw = portable_canonical_json_bytes(copied)
            validate_portable_write(second_path, raw, engine.profile.tenant_id)
            (engine.profile.root / second_path).write_bytes(raw)
            second = dict(original)
            second.update(
                delivery_id="synthetic.legacy.ambiguous",
                decision_id=second_decision,
                decision_receipt_id="receipt_" + str(uuid4()),
                proposal_id="proposal_" + str(uuid4()),
                publication_id=second_id,
                publication_path=second_path,
            )
            columns = tuple(second)
            placeholders = ",".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO decisions({','.join(columns)}) VALUES({placeholders})",  # noqa: S608
                tuple(second[column] for column in columns),
            )
            second_revision = canonical_revision_id(second_id)
            connection.execute(
                "INSERT INTO canonical_revision_members SELECT ?,page_id,?,ordinal,capture_id "
                "FROM canonical_revision_members WHERE revision_id=?",
                (second_revision, second_id, revision_id),
            )
            connection.execute(
                "INSERT INTO canonical_revision_privacy SELECT ?,effective_privacy_json "
                "FROM canonical_revision_privacy WHERE revision_id=?",
                (second_revision, revision_id),
            )
        else:
            assert denied is not None
            connection.execute(
                "INSERT INTO canonical_revision_members VALUES(?,?,?,?,?)",
                (revision_id, legacy_fixture.PAGE_ID, publication_id, 1, denied.capture_id),
            )
    search = SearchPageRequest(query="Synthetic", limit=1)
    for current in (engine, BrainEngine.open(engine.profile)):
        current.portability.rebuild_index()
        with pytest.raises(T03Error, match="not_found"):
            current.retrieval.read_record(reading, authority=external)
        first = cast(
            dict[str, Any], current.retrieval.search_page(search, authority=external).to_wire()
        )
        assert first["next_cursor"] is not None
        assert first["results"][0]["record_id"] != legacy_fixture.PAGE_ID
        tail = replace(search, cursor=first["next_cursor"])
        expected = cast(
            dict[str, Any], current.retrieval.search_page(tail, authority=external).to_wire()
        )
        assert expected["results"]
        assert all(row["record_id"] != legacy_fixture.PAGE_ID for row in expected["results"])
        with engine._store.transaction() as connection:
            connection.execute(
                "UPDATE search_documents SET title='Synthetic hidden legacy changed' "
                "WHERE result_id=?",
                (legacy_fixture.PAGE_ID,),
            )
        assert current.retrieval.search_page(tail, authority=external).to_wire() == expected


def test_sharing_provider_consent_surface_matrix(tmp_path: Path) -> None:
    tasks, request, owner, text = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    approved = tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.surface.approve",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="approve",
        ),
        authority=owner,
    )
    assert approved.copy_capture_id is not None
    openai = _external(request.brain_id, request.issuer_epoch, "openai")
    gemini = _external(request.brain_id, request.issuer_epoch, "gemini")
    search = SearchPageRequest(query="Synthetic retained body", limit=10)
    allowed = tasks.retrieval.search_page(search, authority=openai).to_wire()["results"]
    assert [row["record_id"] for row in allowed] == [approved.copy_capture_id]
    assert tasks.retrieval.search_page(search, authority=gemini).to_wire()["results"] == []
    assert (
        tasks.retrieval.search_page(
            search, authority=_external(request.brain_id, request.issuer_epoch, "openai_api")
        ).to_wire()["results"]
        == []
    )
    for narrowed in (
        replace(openai, brain_id="brn_" + "b" * 26),
        replace(openai, issuer_epoch=request.issuer_epoch + 1),
        replace(openai, allowed_read_tiers=frozenset({PrivacyTier.PERSONAL})),
        replace(openai, space_ids=frozenset()),
    ):
        assert tasks.retrieval.search_page(search, authority=narrowed).to_wire()["results"] == []
    assert (
        tasks.retrieval.read_record(
            RecordReadRequest(
                record_id=approved.copy_capture_id,
                expected_revision_id=approved.copy_capture_id,
            ),
            authority=openai,
        ).to_wire()["content"]["text"]
        == text
    )
    with pytest.raises(T03Error, match="not_found"):
        tasks.retrieval.read_record(
            RecordReadRequest(
                record_id=request.expected_head,
                expected_revision_id=request.expected_head,
            ),
            authority=openai,
        )
    tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="sharing.surface.revoke",
            approval_id=approved.approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        ),
        authority=owner,
    )
    assert tasks.retrieval.search_page(search, authority=openai).to_wire()["results"] == []
    with pytest.raises(T03Error, match="not_found"):
        tasks.retrieval.read_record(
            RecordReadRequest(
                record_id=approved.copy_capture_id,
                expected_revision_id=approved.copy_capture_id,
            ),
            authority=openai,
        )


def test_damaged_sharing_link_has_no_search_rank_count_or_cursor_influence(
    tmp_path: Path,
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    ordinary = replace(
        _public_submission(tasks),
        payload=TextPayload("Synthetic retained body ordinary"),
        delivery_id="synthetic.sharing.visible",
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.PUBLIC,
            reason=PrivacyReason.POLICY_PUBLIC,
            policy_version="synthetic-public-v1",
            authority=Authority(cloud=False, external_egress=True),
        ),
    )
    ordinary_receipt = tasks.capture.submit(ordinary)
    preview = tasks.sharing.preview(request, authority=owner)
    approval = tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.damaged.approve",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="approve",
        ),
        authority=owner,
    )
    external = _external(request.brain_id, request.issuer_epoch, "openai")
    query = SearchPageRequest(query="Synthetic retained body", limit=1)
    assert approval.copy_capture_id is not None
    with tasks.sharing._engine._store.transaction() as connection:
        connection.execute(
            "UPDATE source_revisions SET source_bytes=? WHERE capture_id=?",
            (b"{}", approval.copy_capture_id),
        )
    page = tasks.retrieval.search_page(query, authority=external).to_wire()
    assert [item["record_id"] for item in page["results"]] == [ordinary_receipt.capture_id]
    assert page["next_cursor"] is None


def test_missing_retained_copy_link_stays_closed_through_reopen_and_rebuild(
    tmp_path: Path,
) -> None:
    tasks, request, owner, _text = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    approval = tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.missing-link.approve",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="approve",
        ),
        authority=owner,
    )
    assert approval.copy_capture_id is not None
    external = _external(request.brain_id, request.issuer_epoch, "openai")
    reading = RecordReadRequest(
        record_id=approval.copy_capture_id, expected_revision_id=approval.copy_capture_id
    )
    assert tasks.retrieval.read_record(reading, authority=external).to_wire()["content"]
    controls = set()
    for index in range(2):
        accepted = tasks.capture.submit(
            replace(
                _public_submission(tasks),
                payload=TextPayload(f"Synthetic eligible control {index}"),
                delivery_id=f"synthetic.missing-link.control.{index}",
                privacy=PrivacyDecision.create(
                    tier=PrivacyTier.PUBLIC,
                    reason=PrivacyReason.POLICY_PUBLIC,
                    policy_version="synthetic-public-v1",
                    authority=Authority(cloud=False, external_egress=True),
                ),
            )
        )
        controls.add(accepted.capture_id)
    # This disposable corruption fixture restores the exact immutability guard
    # before any product call. Production deletion remains forbidden.
    with tasks.sharing._engine._store.transaction() as connection:
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name='sharing_links' AND sql LIKE '%BEFORE DELETE%'"
        ).fetchall()
        assert triggers
        for name, _ddl in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')  # noqa: S608
        connection.execute("DELETE FROM sharing_links WHERE approval_id=?", (approval.approval_id,))
        for _name, ddl in triggers:
            connection.execute(ddl)
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0
    search = SearchPageRequest(query="Synthetic", limit=1)
    for current in (tasks, open_local_engine(tasks.profile)):
        assert current.sharing is not None
        with pytest.raises(T03Error, match="not_found"):
            current.retrieval.read_record(reading, authority=external)
        first = cast(
            dict[str, Any], current.retrieval.search_page(search, authority=external).to_wire()
        )
        assert first["next_cursor"] is not None
        assert first["results"][0]["record_id"] in controls
        continuation = replace(search, cursor=first["next_cursor"])
        expected = current.retrieval.search_page(continuation, authority=external).to_wire()
        with tasks.sharing._engine._store.transaction() as connection:
            connection.execute(
                "UPDATE search_documents SET title='Synthetic hidden changed' WHERE result_id=?",
                (approval.copy_capture_id,),
            )
        assert current.retrieval.search_page(continuation, authority=external).to_wire() == expected
        current.portability.rebuild_index()
        with pytest.raises(T03Error, match="not_found"):
            current.retrieval.read_record(reading, authority=external)
        results = cast(
            dict[str, Any],
            current.retrieval.search_page(replace(search, limit=10), authority=external).to_wire(),
        )["results"]
        assert {row["record_id"] for row in results} == controls


@pytest.mark.parametrize("revoked", [False, True])
@pytest.mark.parametrize(
    "damaged_field", ["source_reference", "delivery_id", "provenance_json", "source_bytes"]
)
def test_damaged_copy_identity_never_falls_back_to_unrelated_public_policy(
    tmp_path: Path, revoked: bool, damaged_field: str
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    approval = tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.marker.approve",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="approve",
        ),
        authority=owner,
    )
    assert approval.copy_capture_id is not None
    controls = set()
    for index in range(2):
        control = tasks.capture.submit(
            replace(
                _public_submission(tasks),
                payload=TextPayload(f"Synthetic eligible marker control {index}"),
                delivery_id=f"synthetic.marker.control.{index}",
                privacy=PrivacyDecision.create(
                    tier=PrivacyTier.PUBLIC,
                    reason=PrivacyReason.POLICY_PUBLIC,
                    policy_version="synthetic-public-v1",
                    authority=Authority(cloud=False, external_egress=True),
                ),
            )
        )
        controls.add(control.capture_id)
    if revoked:
        tasks.sharing.revoke(
            SharingRevokeRequest(
                operation_id="sharing.marker.revoke",
                approval_id=approval.approval_id,
                expected_approval_version=1,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                reason="owner_choice",
            ),
            authority=owner,
        )
    damaged_value: str | bytes = (
        b"{}"
        if damaged_field == "source_bytes"
        else "{}"
        if damaged_field == "provenance_json"
        else "synthetic-unrelated"
    )
    table = "source_revisions" if damaged_field == "source_bytes" else "captures"
    with tasks.sharing._engine._store.transaction() as connection:
        connection.execute(
            f"UPDATE {table} SET {damaged_field}=? WHERE capture_id=?",  # noqa: S608
            (damaged_value, approval.copy_capture_id),
        )
    external = replace(
        _external(request.brain_id, request.issuer_epoch, "openai"),
        capabilities=frozenset({"search", "content-read", "history-read"}),
    )
    read = RecordReadRequest(
        record_id=approval.copy_capture_id, expected_revision_id=approval.copy_capture_id
    )
    search = SearchPageRequest(query="Synthetic", limit=1)
    for current in (tasks, open_local_engine(tasks.profile)):
        assert current.history is not None
        current.portability.rebuild_index()
        for reader in (current.retrieval.read_record, current.history.read_history):
            with pytest.raises(T03Error, match="not_found"):
                reader(read, authority=external)
        first = cast(
            dict[str, Any], current.retrieval.search_page(search, authority=external).to_wire()
        )
        assert first["next_cursor"] is not None
        assert first["results"][0]["record_id"] in controls
        tail = replace(search, cursor=first["next_cursor"])
        expected = cast(
            dict[str, Any], current.retrieval.search_page(tail, authority=external).to_wire()
        )
        assert {first["results"][0]["record_id"], expected["results"][0]["record_id"]} == controls
        assert expected["next_cursor"] is None
        with tasks.sharing._engine._store.transaction() as connection:
            connection.execute(
                "UPDATE search_documents SET title='Synthetic hidden marker changed' "
                "WHERE result_id=?",
                (approval.copy_capture_id,),
            )
        assert current.retrieval.search_page(tail, authority=external).to_wire() == expected


def test_nonowner_canonical_history_cannot_use_retained_owner_exception(tmp_path: Path) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    space = tasks.spaces.create_space("Synthetic Notes", delivery_id="sharing.history.space")
    tasks.spaces.route(request.expected_head, space.space_id, delivery_id="sharing.history.route")
    proposal = tasks.review.propose(
        request.expected_head,
        (ProposalDraft("Retained canonical", "Synthetic retained canonical body"),),
        delivery_id="sharing.history.proposal",
    )[0]
    decision = tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="sharing.history.decision",
        expected_review_digest=proposal.review_digest,
    )
    assert decision.page_id is not None
    nonowner = EffectiveAuthority(
        "synthetic-local-agent", "synthetic-history", frozenset({"history-read"}), None
    )
    history = HistoryListRequest(record_id=decision.page_id)
    before = tasks.history.list_history(history, authority=nonowner).to_wire()
    reading = RecordReadRequest(
        record_id=decision.page_id, expected_revision_id=before["entries"][0]["revision_id"]
    )
    assert (
        "Synthetic retained canonical body"
        in tasks.history.read_history(reading, authority=nonowner).to_wire()["content"]["text"]
    )
    tasks.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.sharing.history",
            source_id=request.source_id,
            expected_head=request.expected_head,
            expected_lifecycle_version=0,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            reason_code="owner_choice",
        ),
        authority=owner,
    )
    assert (
        "Synthetic retained canonical body"
        in tasks.history.read_history(reading, authority=owner).to_wire()["content"]["text"]
    )
    with pytest.raises(T03Error, match="not_found"):
        tasks.history.read_history(reading, authority=nonowner)


@pytest.mark.parametrize("revoked_member", [0, 1])
def test_canonical_relationship_evidence_requires_every_current_approved_member(
    tmp_path: Path, revoked_member: int
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    approvals = []
    for index in range(2):
        preview = tasks.sharing.preview(
            replace(request, operation_id=f"sharing.members.preview.{index}"), authority=owner
        )
        approvals.append(
            tasks.sharing.decide(
                SharingDecisionRequest(
                    operation_id=f"sharing.members.approve.{index}",
                    preview_id=preview.preview_id,
                    preview_sha256=preview.preview_sha256,
                    brain_id=request.brain_id,
                    issuer_epoch=request.issuer_epoch,
                    destination_brain_id=request.brain_id,
                    expected_decision_version=0,
                    decision="approve",
                ),
                authority=owner,
            )
        )
    copies = tuple(approval.copy_capture_id for approval in approvals)
    assert all(isinstance(capture_id, str) for capture_id in copies)
    space = tasks.spaces.create_space("Synthetic Members", delivery_id="sharing.members.space")
    for index, capture_id in enumerate(copies):
        assert capture_id is not None
        tasks.spaces.route(capture_id, space.space_id, delivery_id=f"sharing.members.route.{index}")
    proposal = tasks.review.propose(
        copies,
        (ProposalDraft("Synthetic member canonical", "Synthetic retained body canonical"),),
        delivery_id="sharing.members.propose",
    )[0]
    decision = tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="sharing.members.decide",
        expected_review_digest=proposal.review_digest,
    )
    assert decision.page_id is not None
    peer = tasks.capture.submit(
        replace(
            _public_submission(tasks),
            payload=TextPayload("Synthetic retained body peer"),
            delivery_id="sharing.members.peer",
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.PUBLIC,
                reason=PrivacyReason.POLICY_PUBLIC,
                policy_version="synthetic-public-v1",
                authority=Authority(cloud=False, external_egress=True),
            ),
        )
    ).capture_id
    assert peer is not None
    external = replace(
        _external(request.brain_id, request.issuer_epoch, "openai"),
        capabilities=frozenset({"search", "content-read", "history-read"}),
    )
    history = tasks.history.list_history(
        HistoryListRequest(record_id=decision.page_id), authority=external
    ).to_wire()
    revision_id = history["entries"][0]["revision_id"]
    tasks.relationships.decide(
        RelationshipDecideRequest(
            left={"record_id": decision.page_id, "revision_id": revision_id},
            right={"record_id": peer, "revision_id": peer},
            kind="duplicate_of",
            decision="accept",
            expected_relationship_version=0,
            operation_id="operation_" + str(uuid4()),
        ),
        authority=owner,
    )
    listing = RelationshipListRequest(record_id=peer)
    decisions = DecisionHistoryRequest(record_id=peer)
    assert (
        len(
            tasks.relationships.list_relationships(listing, authority=external).to_wire()["entries"]
        )
        == 1
    )
    assert (
        len(tasks.relationships.list_decisions(decisions, authority=external).to_wire()["entries"])
        == 1
    )
    tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="sharing.members.revoke",
            approval_id=approvals[revoked_member].approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        ),
        authority=owner,
    )
    reading = RecordReadRequest(record_id=decision.page_id, expected_revision_id=revision_id)
    for current in (tasks, open_local_engine(tasks.profile)):
        assert current.history is not None and current.relationships is not None
        current.portability.rebuild_index()
        with pytest.raises(T03Error, match="not_found"):
            current.retrieval.read_record(reading, authority=external)
        with pytest.raises(T03Error, match="not_found"):
            current.history.read_history(reading, authority=external)
        assert (
            current.relationships.list_relationships(listing, authority=external).to_wire()[
                "entries"
            ]
            == []
        )
        assert (
            current.relationships.list_decisions(decisions, authority=external).to_wire()["entries"]
            == []
        )
        assert (
            "Synthetic retained body canonical"
            in cast(
                dict[str, Any], current.history.read_history(reading, authority=owner).to_wire()
            )["content"]["text"]
        )
        assert decision.page_id not in {
            row["record_id"]
            for row in cast(
                dict[str, Any],
                current.retrieval.search_page(
                    SearchPageRequest(query="Synthetic retained body", limit=10), authority=external
                ).to_wire(),
            )["results"]
        }


def test_hidden_canonical_member_cannot_change_visible_nonempty_cursor(tmp_path: Path) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    copies = []
    for index, provider in enumerate(("openai", "gemini")):
        preview = tasks.sharing.preview(
            replace(
                request, operation_id=f"sharing.canonical.preview.{index}", provider_ids=(provider,)
            ),
            authority=owner,
        )
        approval = tasks.sharing.decide(
            SharingDecisionRequest(
                operation_id=f"sharing.canonical.approve.{index}",
                preview_id=preview.preview_id,
                preview_sha256=preview.preview_sha256,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                expected_decision_version=0,
                decision="approve",
            ),
            authority=owner,
        )
        assert approval.copy_capture_id is not None
        copies.append(approval.copy_capture_id)
    space = tasks.spaces.create_space("Synthetic Notes", delivery_id="sharing.canonical.space")
    for index, capture_id in enumerate(copies):
        tasks.spaces.route(
            capture_id, space.space_id, delivery_id=f"sharing.canonical.route.{index}"
        )
    proposal = tasks.review.propose(
        tuple(copies),
        (ProposalDraft("Synthetic retained body canonical", "Synthetic retained body"),),
        delivery_id="sharing.canonical.proposal",
    )[0]
    decision = tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="sharing.canonical.decision",
        expected_review_digest=proposal.review_digest,
    )
    assert decision.page_id is not None
    for index in range(2):
        tasks.capture.submit(
            replace(
                _public_submission(tasks),
                payload=TextPayload(f"Synthetic retained body control {index}"),
                delivery_id=f"sharing.canonical.control.{index}",
                privacy=PrivacyDecision.create(
                    tier=PrivacyTier.PUBLIC,
                    reason=PrivacyReason.POLICY_PUBLIC,
                    policy_version="synthetic-public-v1",
                    authority=Authority(cloud=False, external_egress=True),
                ),
            )
        )
    external = _external(request.brain_id, request.issuer_epoch, "openai")
    query = SearchPageRequest(query="Synthetic retained body", limit=1)
    first = tasks.retrieval.search_page(query, authority=external).to_wire()
    assert first["next_cursor"] is not None
    assert all(row["record_id"] != decision.page_id for row in first["results"])
    with tasks.sharing._engine._store.transaction() as connection:
        connection.execute(
            "UPDATE search_documents SET title=? WHERE result_id=?",
            ("Synthetic retained body hidden change", decision.page_id),
        )
    tail = tasks.retrieval.search_page(
        replace(query, cursor=first["next_cursor"]), authority=external
    ).to_wire()
    assert tail["results"]
    assert all(row["record_id"] != decision.page_id for row in tail["results"])


@pytest.mark.parametrize("transition", ["revoke", "withdraw", "route", "head"])
@pytest.mark.parametrize("pending", [False, True])
def test_sharing_head_change_withdrawal_revocation_and_old_cursors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str, pending: bool
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    ordinary = replace(
        _public_submission(tasks),
        payload=TextPayload("Synthetic retained body ordinary"),
        delivery_id="synthetic.sharing.ordinary",
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.PUBLIC,
            reason=PrivacyReason.POLICY_PUBLIC,
            policy_version="synthetic-public-v1",
            authority=Authority(cloud=False, external_egress=True),
        ),
    )
    tasks.capture.submit(ordinary)
    preview = tasks.sharing.preview(request, authority=owner)
    decision = SharingDecisionRequest(
        operation_id="sharing.cursor.approve",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    if pending:

        def blocked(_submission: object) -> None:
            raise RuntimeError("synthetic pending sharing copy")

        with monkeypatch.context() as patch:
            patch.setattr(tasks.sharing._engine.capture, "submit", blocked)
            approval = tasks.sharing.decide(decision, authority=owner)
        assert approval.state == "pending"
    else:
        approval = tasks.sharing.decide(decision, authority=owner)
    external = _external(request.brain_id, request.issuer_epoch, "openai")
    query = SearchPageRequest(query="Synthetic retained body", limit=1)
    cursor = None
    read_tail = None
    if not pending:
        first = tasks.retrieval.search_page(query, authority=external).to_wire()
        cursor = first["next_cursor"]
        assert cursor is not None
        assert approval.copy_capture_id is not None
        reading = RecordReadRequest(
            record_id=approval.copy_capture_id,
            expected_revision_id=approval.copy_capture_id,
            target_bytes=8,
        )
        first_read = tasks.retrieval.read_record(reading, authority=external).to_wire()
        assert first_read["next_cursor"] is not None
        read_tail = replace(reading, cursor=first_read["next_cursor"])
        assert (
            tasks.retrieval.read_record(read_tail, authority=external).to_wire()["start_byte"]
            == first_read["end_byte"]
        )
    if transition == "revoke":
        tasks.sharing.revoke(
            SharingRevokeRequest(
                operation_id="sharing.cursor.revoke",
                approval_id=approval.approval_id,
                expected_approval_version=1,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                reason="owner_choice",
            ),
            authority=owner,
        )
    elif transition == "withdraw":
        assert tasks.sources is not None
        tasks.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.sharing.cursor",
                source_id=request.source_id,
                expected_head=request.expected_head,
                expected_lifecycle_version=request.expected_lifecycle_version,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                reason_code="owner_choice",
            ),
            authority=owner,
        )
    elif transition == "route":
        assert tasks.sources is not None
        space = tasks.inbox.create_space(
            "Synthetic sharing route", delivery_id="sharing.route.space"
        )
        tasks.sources.route(
            SourceRouteRequest(
                source_id=request.source_id,
                expected_head=request.expected_head,
                expected_route_version=request.expected_route_version,
                space_id=space.space_id,
                operation_id="operation_" + str(uuid4()),
            ),
            authority=owner,
        )
    else:
        assert tasks.sources is not None
        privacy = PrivacyDecision.create(
            tier=PrivacyTier.PUBLIC,
            reason=PrivacyReason.POLICY_PUBLIC,
            policy_version="policy-v1",
            authority=Authority(cloud=False, external_egress=False),
        )
        capture = replace(
            _public_submission(tasks),
            payload=ReferencePayload("https://example.test/synthetic", "Synthetic next head"),
            privacy=privacy,
            delivery_id="synthetic.saved.next",
        )
        namespace = {
            "connector_name": "saved_markdown",
            "connection_id": "synthetic-connection",
            "resource_id": "synthetic-resource",
            "external_id": "item:synthetic",
        }
        revision = SourceRevisionSubmission(
            capture=capture,
            namespace=namespace,
            revision_key="synthetic-next",
            canonical_sha256=capture.request_sha256(),
            expected_head=request.expected_head,
            ordering={"kind": "predecessor", "revision_key": "synthetic-revision"},
            expected_control_epoch=0,
        )
        binding = SourceRevisionBinding(
            destination_brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            root_fingerprint="synthetic-root",
            accepted_source_id="synthetic-selection",
            namespace=namespace,
        )
        observation = SourceRevisionObservation(
            original_sha256="a" * 64,
            transformed_sha256="b" * 64,
            normalization_version="saved-markdown-continuous.v1",
            privacy_policy_version=privacy.policy_version,
            privacy_policy_sha256=sha256(
                portable_canonical_json_bytes(privacy.to_dict())
            ).hexdigest(),
            admitted_payload_sha256=sha256(
                portable_canonical_json_bytes(capture.payload.to_dict())
            ).hexdigest(),
        )
        tasks.sources.public_revision_sink(binding).submit(
            SourceRevisionObservedDelivery(
                binding=binding,
                submission=revision,
                expected_lifecycle_version=0,
                delivery_id="synthetic.saved.next.delivery",
                observation=observation,
            )
        )
    if pending:
        reopened = open_local_engine(tasks.profile)
        assert reopened.sharing is not None
        terminal = reopened.sharing.decide(decision, authority=owner)
        assert terminal.state == "history_only" and terminal.copy_capture_id is not None
        copy_capture_id = terminal.copy_capture_id
    else:
        with pytest.raises(T03Error, match="cursor_stale"):
            tasks.retrieval.search_page(replace(query, cursor=cursor), authority=external)
        assert read_tail is not None
        with pytest.raises(T03Error, match="not_found|cursor_stale"):
            tasks.retrieval.read_record(read_tail, authority=external)
        assert approval.copy_capture_id is not None
        copy_capture_id = approval.copy_capture_id
    visible = tasks.retrieval.search_page(query, authority=external).to_wire()["results"]
    assert approval.copy_capture_id not in {row["record_id"] for row in visible}
    nonowner_history = EffectiveAuthority(
        "synthetic-local-history", "synthetic-session", frozenset({"history-read"}), None
    )
    external_history = replace(
        external, capabilities=frozenset({"search", "content-read", "history-read"})
    )
    # The retained-owner exception survives reopen and derived-index rebuild.
    # Neither step is allowed to resurrect an ineligible copy for other readers.
    for current in (tasks, open_local_engine(tasks.profile)):
        assert current.history is not None
        current.portability.rebuild_index()
        for capture_id in (request.expected_head, copy_capture_id):
            reading = RecordReadRequest(record_id=capture_id, expected_revision_id=capture_id)
            retained = cast(
                dict[str, Any], current.history.read_history(reading, authority=owner).to_wire()
            )
            assert "Synthetic retained body" in retained["content"]["text"]
            entries = cast(
                dict[str, Any],
                current.history.list_history(
                    HistoryListRequest(record_id=capture_id), authority=owner
                ).to_wire(),
            )["entries"]
            assert any(entry["revision_id"] == capture_id for entry in entries)
        copy_read = RecordReadRequest(
            record_id=copy_capture_id, expected_revision_id=copy_capture_id
        )
        for narrowed in (nonowner_history, external_history):
            with pytest.raises(T03Error, match="not_found"):
                current.history.read_history(copy_read, authority=narrowed)
            with pytest.raises(T03Error, match="not_found"):
                current.history.list_history(
                    HistoryListRequest(record_id=copy_capture_id), authority=narrowed
                )
        assert copy_capture_id not in {
            row["record_id"]
            for row in cast(
                dict[str, Any], current.retrieval.search_page(query, authority=external).to_wire()
            )["results"]
        }
