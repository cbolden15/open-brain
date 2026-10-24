"""Synthetic providers enter real engine custody, materialization and reads."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.core.models import ContentOrigin, PrivacyDecision, PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    CaptureFault,
    InjectedFault,
    ProposalDraft,
    TextPayload,
)
from open_brain_engine.engine.capture_lifecycle import open_lifecycle_journal
from open_brain_engine.engine.capture_lifecycle_contracts import LifecycleError, LifecycleRequest
from open_brain_engine.engine.consumer_contracts import OMISSION_KEYS
from open_brain_engine.engine.consumer_intake import _load
from open_brain_engine.engine.consumer_journal import LifecycleJournalV2
from open_brain_engine.engine.consumer_plans import (
    ConsumerConversationInput,
    ConsumerImportPlan,
    ConsumerMessageInput,
)
from open_brain_engine.engine.consumer_profiles import ConsumerIntakeError, ConsumerProfileRecord
from open_brain_engine.engine.consumer_segments import ConsumerSegmentEnvelope
from open_brain_engine.engine.contracts import CaptureSubmissionPath, owner_privacy_for_tier
from open_brain_engine.engine.historical_contracts import HistoricalDestination
from open_brain_engine.engine.historical_fence import HistoricalPendingFence, _FenceHead
from open_brain_engine.engine.historical_registry import (
    _REGISTRY_PATH,
    HistoricalClaimRegistry,
    HistoricalRegistryStore,
)
from open_brain_engine.engine.privacy_projection import (
    effective_privacy_json,
    project_retained_privacy_evidence,
)
from open_brain_engine.engine.sharing import EligibilityMode, sharing_eligible
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    SearchPageRequest,
    T03Error,
)
from open_brain_engine.storage.filesystem import atomic_replace
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.profile import compile_single_user_local


@pytest.fixture
def active_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "open_brain_engine.engine.consumer_profiles._ACTIVE_PROFILES",
        {
            f"synthetic-{provider}": ConsumerProfileRecord(
                provider, f"synthetic-{provider}", "1" * 64
            )
            for provider in ("chatgpt", "claude")
        },
    )


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return EffectiveAuthority(
        engine.profile.owner_actor_id, "consumer-test", frozenset(), None, owner=True
    )


def _conversation(
    identity: str = "exact-conversation-é", text: str = "intakequartz synthetic body"
) -> ConsumerConversationInput:
    return ConsumerConversationInput(
        provider_conversation_id=identity,
        title="protected synthetic title",
        messages=(
            ConsumerMessageInput(provider_message_id="exact-message-é", role="user", text=text),
        ),
        omissions=dict.fromkeys(OMISSION_KEYS, 0),
    )


def _plan(
    provider: str = "chatgpt", conversations: tuple[ConsumerConversationInput, ...] | None = None
) -> ConsumerImportPlan:
    return ConsumerImportPlan(
        operation_id=str(uuid4()),
        provider=provider,
        profile_id=f"synthetic-{provider}",
        archive_sha256="a" * 64,
        conversations=(_conversation(),) if conversations is None else conversations,
        new_scope_label="Personal synthetic account",
    )


def _reimport(
    plan: ConsumerImportPlan, scope: str, *, text: str | None = None, replace_current: bool = False
) -> ConsumerImportPlan:
    return replace(
        plan,
        operation_id=str(uuid4()),
        scope_id=scope,
        new_scope_label=None,
        archive_sha256=uuid4().hex * 2,
        replace_current=replace_current,
        conversations=plan.conversations if text is None else (_conversation(text=text),),
    )


def _commit(
    engine: BrainEngine, plan: ConsumerImportPlan
) -> tuple[dict[str, object], dict[str, object]]:
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    result = engine.consumer_imports.commit_consumer_import(
        cast(str, preview["preview_id"]),
        plan.operation_id,
        plan,
        authority=_owner(engine),
    )
    return preview, result


def _counts(value: dict[str, object]) -> dict[str, int]:
    return cast(dict[str, int], value["counts"])


def _rows(engine: BrainEngine, sql: str) -> list[tuple[object, ...]]:
    with closing(engine._store.connect()) as connection:
        return [tuple(row) for row in connection.execute(sql)]


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_real_preview_commit_keeps_speakers_third_party_and_personal(
    tmp_path: Path,
    active_profiles: None,
    provider: str,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan(provider)
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    assert engine.tasks.consumer_imports is engine.consumer_imports
    assert not _rows(engine, "SELECT capture_id FROM captures")
    assert not _rows(engine, "SELECT scope_id FROM consumer_scopes")
    assert not _rows(engine, "SELECT operation_id FROM lifecycle_journal_requests")
    assert "intakequartz" not in json.dumps(preview)
    assert "protected synthetic title" not in json.dumps(preview)
    body = engine.consumer_imports.consumer_import_preview_body(
        cast(str, preview["preview_id"]),
        0,
        authority=_owner(engine),
    )
    assert "intakequartz synthetic body" in cast(str, body["body"])
    assert (
        engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine)) == preview
    )
    result = engine.consumer_imports.commit_consumer_import(
        cast(str, preview["preview_id"]),
        plan.operation_id,
        plan,
        authority=_owner(engine),
    )
    assert result["status"] == "committed" and _counts(result)["committed"] == 1
    assert _counts(result)["segments_materialized"] == 1
    assert len(engine.retrieval.search("intakequartz")) == 1
    with closing(engine._store.connect()) as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        journal = open_lifecycle_journal(connection)
        assert isinstance(journal, LifecycleJournalV2)
        prepared = _load(journal, plan.operation_id)
        assert prepared is not None
        conversation = prepared.conversation(prepared.items[0])
        assert conversation is not None
        submission = conversation.segments[0].envelope.submission
        assert submission.source_origin is ContentOrigin.THIRD_PARTY
        assert submission.submission_path is CaptureSubmissionPath.PUBLIC_JOB
        assert submission.actor_id != engine.profile.owner_actor_id
        assert submission.role_claim["capabilities"] == ("capture.accept",)
        assert submission.space_id is None
        row = connection.execute("SELECT privacy_json FROM captures").fetchone()
        privacy = PrivacyDecision.from_dict(json.loads(row[0]))
        assert privacy.tier is PrivacyTier.PERSONAL
        assert not privacy.authority.cloud and not privacy.authority.external_egress
    assert "intakequartz" not in json.dumps(result)
    assert (
        engine.consumer_imports.consumer_import_status(plan.operation_id, authority=_owner(engine))
        == result
    )


def test_inactive_profile_refuses_before_any_durable_preview(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    with pytest.raises(ConsumerIntakeError, match="unsupported_profile"):
        engine.consumer_imports.preview_consumer_import(_plan(), authority=_owner(engine))
    assert not _rows(engine, "SELECT preview_id FROM consumer_previews")
    assert not _rows(engine, "SELECT operation_id FROM lifecycle_journal_requests")
    assert not _rows(engine, "SELECT capture_id FROM captures")


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_repackaged_unchanged_import_returns_old_receipt_without_session_membership(
    tmp_path: Path,
    active_profiles: None,
    provider: str,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan(provider)
    preview, first = _commit(engine, plan)
    captures = _rows(engine, "SELECT capture_id FROM captures")
    again = _reimport(plan, cast(str, preview["account_scope_id"]))
    _, second = _commit(engine, again)
    assert _counts(second)["unchanged"] == 1 and second["session_id"] is None
    assert _rows(engine, "SELECT capture_id FROM captures") == captures
    assert _rows(engine, "SELECT count(*) FROM consumer_scopes") == [(1,)]
    assert _rows(engine, "SELECT count(*) FROM consumer_archive_bindings") == [(2,)]
    assert first["session_id"] is not None
    changed = replace(plan, archive_sha256="f" * 64)
    with pytest.raises(ConsumerIntakeError, match="operation_conflict"):
        engine.consumer_imports.preview_consumer_import(changed, authority=_owner(engine))
    # Accepted exact replay is independent of the expired/missing preview token.
    assert (
        engine.consumer_imports.commit_consumer_import(
            "missing",
            plan.operation_id,
            plan,
            authority=_owner(engine),
        )
        == first
    )


def test_changed_revision_needs_explicit_replacement_and_keeps_stable_source(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan()
    preview, _ = _commit(engine, plan)
    original = _rows(engine, "SELECT source_id,head_capture_id FROM logical_sources")
    changed = _reimport(
        plan, cast(str, preview["account_scope_id"]), text="replacementquartz second"
    )
    _, conflict = _commit(engine, changed)
    assert _counts(conflict)["conflict"] == 1
    assert _rows(engine, "SELECT source_id,head_capture_id FROM logical_sources") == original
    replacement = replace(changed, operation_id=str(uuid4()), replace_current=True)
    _, result = _commit(engine, replacement)
    assert _counts(result)["committed"] == 1
    after = _rows(engine, "SELECT source_id,head_capture_id FROM logical_sources")
    assert after[0][0] == original[0][0] and after[0][1] != original[0][1]
    assert _rows(engine, "SELECT count(*) FROM source_revisions") == [(2,)]
    assert not engine.retrieval.search("intakequartz")
    assert len(engine.retrieval.search("replacementquartz")) == 1


def test_empty_operation_preserves_evidence_without_registering_scope(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    empty = replace(_conversation(), messages=())
    plan = _plan(conversations=(empty,))
    _, result = _commit(engine, plan)
    assert result["status"] == "committed" and _counts(result)["no_text"] == 1
    assert result["session_id"] is None
    for table in ("captures", "consumer_scopes", "consumer_archive_bindings", "logical_sources"):
        assert _rows(engine, f"SELECT count(*) FROM {table}") == [(0,)]
    assert _rows(engine, "SELECT count(*) FROM consumer_approvals") == [(1,)]


@pytest.mark.parametrize(
    "point",
    [
        CaptureFault.AFTER_CONSUMER_INTENT,
        CaptureFault.AFTER_CONSUMER_SEGMENT,
        CaptureFault.CONSUMER_READY,
        CaptureFault.AFTER_CONSUMER_ACTIVATION,
    ],
)
def test_explicit_restart_recovery_is_idempotent_and_status_is_read_only(
    tmp_path: Path,
    active_profiles: None,
    point: CaptureFault,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile, faults={point})
    plan = _plan(conversations=(_conversation(text="intakequartz " + "x" * 60010),))
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    with pytest.raises(InjectedFault):
        engine.consumer_imports.commit_consumer_import(
            cast(str, preview["preview_id"]),
            plan.operation_id,
            plan,
            authority=_owner(engine),
        )
    before = _rows(engine, "SELECT sequence,event_sha256 FROM lifecycle_journal_events")
    state = engine.consumer_imports.consumer_import_status(
        plan.operation_id, authority=_owner(engine)
    )
    assert _rows(engine, "SELECT sequence,event_sha256 FROM lifecycle_journal_events") == before
    if point is not CaptureFault.AFTER_CONSUMER_ACTIVATION:
        assert not engine.retrieval.search("intakequartz")
        for (capture,) in _rows(engine, "SELECT capture_id FROM captures"):
            assert engine.capture.get(cast(str, capture)) is None
    engine = BrainEngine.open(profile)
    assert (
        engine.consumer_imports.consumer_import_status(plan.operation_id, authority=_owner(engine))
        == state
    )
    result = engine.consumer_imports.recover_consumer_import(
        plan.operation_id, authority=_owner(engine)
    )
    assert result["status"] == "committed" and _counts(result)["committed"] == 1
    assert _counts(result)["segments_materialized"] == 2
    assert len(engine.retrieval.search("intakequartz")) == 1
    assert (
        engine.consumer_imports.recover_consumer_import(plan.operation_id, authority=_owner(engine))
        == result
    )
    assert _rows(engine, "SELECT count(*) FROM captures") == [(2,)]


def test_halt_and_old_issuer_keep_pending_content_hidden(
    tmp_path: Path, active_profiles: None
) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain"), faults={CaptureFault.AFTER_CONSUMER_SEGMENT}
    )
    plan = _plan()
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    with pytest.raises(InjectedFault):
        engine.consumer_imports.commit_consumer_import(
            cast(str, preview["preview_id"]),
            plan.operation_id,
            plan,
            authority=_owner(engine),
        )
    with engine._store.transaction() as connection:
        # Simulate a new issuer in this disposable fixture. The exact durable
        # guard is restored before any task can observe the changed identity.
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='brain_identity_update_immutable'"
        ).fetchone()[0]
        brain, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
        registry_store = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity)
        prior_registry = registry_store.read(
            HistoricalDestination(brain_id=brain, issuer_epoch=epoch)
        )
        assert not prior_registry.memberships
        registry = HistoricalClaimRegistry.empty(
            HistoricalDestination(brain_id=brain, issuer_epoch=epoch + 1)
        )
        atomic_replace(
            root=engine.profile.root,
            relative=_REGISTRY_PATH,
            data=registry.canonical_bytes(),
            expected_root_identity=engine.profile.root_identity,
        )
        fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
        fence._replace(
            fence._read(), _FenceHead.create(registry=registry, state="idle", record=None)
        )
        connection.execute("DROP TRIGGER brain_identity_update_immutable")
        connection.execute("UPDATE brain_identity SET issuer_epoch=issuer_epoch+1")
        connection.execute(trigger)
        registry_guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='historical_registry_state_update_guard'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER historical_registry_state_update_guard")
        connection.execute(
            "UPDATE historical_registry_state SET issuer_epoch=?,registry_sha256=?",
            (epoch + 1, registry.registry_sha256),
        )
        connection.execute(registry_guard)
    assert (
        engine.consumer_imports.consumer_import_status(plan.operation_id, authority=_owner(engine))[
            "status"
        ]
        == "ready"
    )
    with pytest.raises(ConsumerIntakeError, match="revision_changed"):
        engine.consumer_imports.recover_consumer_import(plan.operation_id, authority=_owner(engine))
    result = engine.consumer_imports.halt_consumer_import(
        plan.operation_id, authority=_owner(engine)
    )
    assert result["status"] == "halted" and _counts(result)["halted"] == 1
    assert (
        engine.consumer_imports.halt_consumer_import(plan.operation_id, authority=_owner(engine))
        == result
    )
    assert not engine.retrieval.search("intakequartz")


def test_cross_scope_archive_rebound_and_retraction_cannot_reimport(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan()
    preview, first = _commit(engine, plan)
    with pytest.raises(ConsumerIntakeError, match="scope_conflict"):
        engine.consumer_imports.preview_consumer_import(
            replace(plan, operation_id=str(uuid4()), new_scope_label="Another account"),
            authority=_owner(engine),
        )
    with closing(engine._store.connect()) as connection:
        brain, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    snapshot = engine.lifecycle.session_snapshot(
        cast(str, first["session_id"]), authority=_owner(engine)
    )
    engine.lifecycle.retract(
        LifecycleRequest(str(uuid4()), "session_retract", brain, epoch, snapshot),
        authority=_owner(engine),
    )
    again = _reimport(
        plan,
        cast(str, preview["account_scope_id"]),
        text="changed after retraction",
        replace_current=True,
    )
    _, result = _commit(engine, again)
    assert _counts(result)["retracted"] == 1
    assert _rows(engine, "SELECT count(*) FROM captures") == [(1,)]
    assert not engine.retrieval.search("intakequartz")
    assert (
        engine.consumer_imports.commit_consumer_import(
            "expired", plan.operation_id, plan, authority=_owner(engine)
        )
        == first
    )


def test_concurrent_exact_commit_reconciles_inside_writer_fence(
    tmp_path: Path,
    active_profiles: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan()
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    original = engine.consumer_imports._accepted
    barrier = Barrier(2)

    def checked(
        plan: ConsumerImportPlan, authority: EffectiveAuthority
    ) -> dict[str, object] | None:
        value = original(plan, authority)
        assert value is None
        barrier.wait(timeout=10)
        return value

    monkeypatch.setattr(engine.consumer_imports, "_accepted", checked)

    def commit() -> dict[str, object]:
        return engine.consumer_imports.commit_consumer_import(
            cast(str, preview["preview_id"]),
            plan.operation_id,
            plan,
            authority=_owner(engine),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        left, right = executor.submit(commit), executor.submit(commit)
        results = (left.result(timeout=20), right.result(timeout=20))
    assert all(value["operation_id"] == plan.operation_id for value in results)
    result = engine.consumer_imports.recover_consumer_import(
        plan.operation_id, authority=_owner(engine)
    )
    assert _counts(result)["committed"] == 1
    assert _rows(engine, "SELECT count(*) FROM captures") == [(1,)]
    assert _rows(engine, "SELECT count(*) FROM consumer_approvals") == [(1,)]


def test_recovery_waits_for_a_writer_held_by_another_thread(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan()
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    first = engine.consumer_imports.commit_consumer_import(
        cast(str, preview["preview_id"]), plan.operation_id, plan, authority=_owner(engine)
    )
    held = Event()

    def hold_writer_briefly() -> None:
        with engine._writer_lease.acquire_shared_writer():
            held.set()
            time.sleep(0.3)

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_writer_briefly)
        assert held.wait(timeout=10)
        # Another runner owns the writer: recovery must wait for the fence, not
        # fail fast on the same-process lease.
        result = engine.consumer_imports.recover_consumer_import(
            plan.operation_id, authority=_owner(engine)
        )
        holder.result(timeout=20)
    assert result == first


def test_recovery_reconciles_another_runner_completed_group(
    tmp_path: Path,
    active_profiles: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain"), faults={CaptureFault.AFTER_CONSUMER_INTENT}
    )
    plan = _plan(conversations=(_conversation("first"), _conversation("second")))
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    with pytest.raises(InjectedFault):
        engine.consumer_imports.commit_consumer_import(
            cast(str, preview["preview_id"]), plan.operation_id, plan, authority=_owner(engine)
        )
    original = engine.lifecycle._stage_consumer_segment
    interleaved = False

    def stage(
        request: LifecycleRequest,
        group_key: str,
        segment: ConsumerSegmentEnvelope,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            engine._faults.add(CaptureFault.AFTER_CONSUMER_ACTIVATION)
            with pytest.raises(InjectedFault):
                engine.consumer_imports.recover_consumer_import(
                    plan.operation_id, authority=authority
                )
            assert (
                _counts(
                    engine.consumer_imports.consumer_import_status(
                        plan.operation_id, authority=authority
                    )
                )["committed"]
                == 1
            )
        return original(request, group_key, segment, authority=authority)

    monkeypatch.setattr(engine.lifecycle, "_stage_consumer_segment", stage)
    result = engine.consumer_imports.recover_consumer_import(
        plan.operation_id, authority=_owner(engine)
    )
    assert result["status"] == "committed" and _counts(result)["committed"] == 2
    assert _rows(engine, "SELECT count(*) FROM captures") == [(2,)]
    assert _rows(engine, "SELECT count(*) FROM consumer_scopes") == [(1,)]


def test_changed_privacy_and_known_historical_revision_are_preview_conflicts(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = replace(_plan(), privacy_tier=PrivacyTier.PUBLIC)
    preview, _ = _commit(engine, plan)
    scope = cast(str, preview["account_scope_id"])
    personal = replace(_reimport(plan, scope), privacy_tier=PrivacyTier.PERSONAL)
    personal_preview, result = _commit(engine, personal)
    assert _counts(personal_preview)["conflict"] == 1 and _counts(result)["conflict"] == 1
    assert _rows(engine, "SELECT count(*) FROM captures") == [(1,)]
    replacement = _reimport(plan, scope, text="newer revision", replace_current=True)
    _commit(engine, replacement)
    old_revision = _reimport(plan, scope, replace_current=True)
    old_preview, result = _commit(engine, old_revision)
    assert _counts(old_preview)["conflict"] == 1 and _counts(result)["conflict"] == 1
    assert _rows(engine, "SELECT count(*) FROM captures") == [(2,)]


def test_profile_deactivation_does_not_destroy_accepted_receipts_or_halt(
    tmp_path: Path,
    active_profiles: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan()
    _, first = _commit(engine, plan)
    monkeypatch.setattr("open_brain_engine.engine.consumer_profiles._ACTIVE_PROFILES", {})
    assert (
        engine.consumer_imports.consumer_import_status(plan.operation_id, authority=_owner(engine))
        == first
    )
    assert (
        engine.consumer_imports.commit_consumer_import(
            "expired", plan.operation_id, plan, authority=_owner(engine)
        )
        == first
    )
    assert (
        engine.consumer_imports.halt_consumer_import(plan.operation_id, authority=_owner(engine))
        == first
    )


def test_accepted_delivery_is_reserved_before_first_custody(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain"), faults={CaptureFault.AFTER_CONSUMER_INTENT}
    )
    plan = _plan()
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    with pytest.raises(InjectedFault):
        engine.consumer_imports.commit_consumer_import(
            cast(str, preview["preview_id"]), plan.operation_id, plan, authority=_owner(engine)
        )
    with closing(engine._store.connect()) as connection:
        journal = open_lifecycle_journal(connection)
        assert isinstance(journal, LifecycleJournalV2)
        prepared = _load(journal, plan.operation_id)
        assert prepared is not None
        conversation = prepared.conversation(prepared.items[0])
        assert conversation is not None
        submission = conversation.segments[0].envelope.submission
    with pytest.raises(ValueError, match="operation_conflict"):
        engine.capture.submit(submission)
    assert not _rows(engine, "SELECT capture_id FROM captures")
    assert (
        engine.consumer_imports.recover_consumer_import(
            plan.operation_id, authority=_owner(engine)
        )["status"]
        == "committed"
    )


def test_one_member_privacy_change_withholds_complete_conversation_from_reader(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan(conversations=(_conversation(text="intakequartz " + "x" * 60010),))
    _commit(engine, plan)
    reader = EffectiveAuthority(
        "reader",
        "consumer-reader",
        frozenset({"search"}),
        None,
        allowed_read_tiers=frozenset({PrivacyTier.PERSONAL}),
    )
    before = engine.retrieval.search_page(SearchPageRequest(query="intakequartz"), authority=reader)
    assert before.to_wire()["results"]
    with engine._store.transaction() as connection:
        second = connection.execute(
            "SELECT capture_id FROM consumer_segment_bindings WHERE segment_index=1"
        ).fetchone()[0]
        narrowed = effective_privacy_json(
            project_retained_privacy_evidence(
                [json.dumps(owner_privacy_for_tier(PrivacyTier.SECRET).to_dict())]
            )
        )
        connection.execute(
            "UPDATE source_revision_privacy SET effective_privacy_json=? WHERE capture_id=?",
            (narrowed, second),
        )
    after = engine.retrieval.search_page(SearchPageRequest(query="intakequartz"), authority=reader)
    assert not after.to_wire()["results"]
    assert engine.retrieval.search("intakequartz")  # Owner retains its existing local authority.


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_generic_lifecycle_cannot_bypass_consumer_approval(
    tmp_path: Path, active_profiles: None, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain"), faults={CaptureFault.CONSUMER_READY}
    )
    plan = _plan(provider)
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    with pytest.raises(InjectedFault):
        engine.consumer_imports.commit_consumer_import(
            cast(str, preview["preview_id"]), plan.operation_id, plan, authority=_owner(engine)
        )
    with closing(engine._store.connect()) as connection:
        journal = open_lifecycle_journal(connection)
        assert isinstance(journal, LifecycleJournalV2)
        prepared = _load(journal, plan.operation_id)
        assert prepared is not None
        conversation = prepared.conversation(prepared.items[0])
        assert conversation is not None
    request, segment = prepared.request, conversation.segments[0]
    key = segment.conversation_key
    with (
        engine.lifecycle._transaction(_owner(engine)) as journal,
        pytest.raises(LifecycleError, match="operation_conflict"),
    ):
        engine.lifecycle._activate_group_in_transaction(
            journal, request, key, session_id="capsess_" + str(uuid4())
        )
    monkeypatch.setattr("open_brain_engine.engine.consumer_profiles._ACTIVE_PROFILES", {})
    before = _rows(engine, "SELECT sequence,event_sha256 FROM lifecycle_journal_events")
    owner = _owner(engine)
    calls: tuple[Callable[[], dict[str, object]], ...] = (
        lambda: engine.lifecycle.begin(request, authority=owner),
        lambda: engine.lifecycle.stage_segment(request, key, segment, authority=owner),
        lambda: engine.lifecycle.recover_pending(request, authority=owner),
        lambda: engine.lifecycle.activate_group(
            request, key, session_id="capsess_" + str(uuid4()), authority=owner
        ),
        lambda: engine.lifecycle.finish(request, authority=owner),
        lambda: engine.lifecycle.halt(request, authority=owner),
    )
    for call in calls:
        with pytest.raises(LifecycleError, match="invalid_arguments"):
            call()
    with pytest.raises(ConsumerIntakeError, match="unsupported_profile"):
        engine.lifecycle._stage_consumer_segment(request, key, segment, authority=owner)
    assert _rows(engine, "SELECT sequence,event_sha256 FROM lifecycle_journal_events") == before
    assert not _rows(engine, "SELECT scope_id FROM consumer_scopes")
    assert not engine.retrieval.search("intakequartz")


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
@pytest.mark.parametrize("boundary", ["before-stage", "ready"])
def test_concurrent_halt_wins_before_next_recovery_mutation(
    tmp_path: Path,
    active_profiles: None,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    boundary: str,
) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain"), faults={CaptureFault.AFTER_CONSUMER_INTENT}
    )
    plan = _plan(provider)
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    with pytest.raises(InjectedFault):
        engine.consumer_imports.commit_consumer_import(
            cast(str, preview["preview_id"]), plan.operation_id, plan, authority=_owner(engine)
        )
    paused, released = Event(), Event()
    original_stage = engine.lifecycle._stage_consumer_segment
    original_fault = engine._fault

    def pause() -> None:
        paused.set()
        assert released.wait(timeout=15)

    def stage(
        request: LifecycleRequest,
        group_key: str,
        segment: ConsumerSegmentEnvelope,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        if boundary == "before-stage":
            pause()
        return original_stage(request, group_key, segment, authority=authority)

    def fault(point: CaptureFault) -> None:
        if boundary == "ready" and point is CaptureFault.CONSUMER_READY:
            pause()
        original_fault(point)

    monkeypatch.setattr(engine.lifecycle, "_stage_consumer_segment", stage)
    monkeypatch.setattr(engine, "_fault", fault)
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(
            engine.consumer_imports.recover_consumer_import,
            plan.operation_id,
            authority=_owner(engine),
        )
        try:
            assert paused.wait(timeout=15)
            halted = engine.consumer_imports.halt_consumer_import(
                plan.operation_id, authority=_owner(engine)
            )
            before = _rows(engine, "SELECT sequence,event_sha256 FROM lifecycle_journal_events")
        finally:
            released.set()
        assert running.result(timeout=15) == halted
    assert halted["status"] == "halted" and _counts(halted)["halted"] == 1
    assert _rows(engine, "SELECT sequence,event_sha256 FROM lifecycle_journal_events") == before
    assert (
        engine.consumer_imports.halt_consumer_import(plan.operation_id, authority=_owner(engine))
        == halted
    )
    assert not engine.retrieval.search("intakequartz")
    assert not _rows(engine, "SELECT scope_id FROM consumer_scopes")


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_consumer_staging_is_denied_by_read_and_release_boundaries(
    tmp_path: Path, active_profiles: None, provider: str
) -> None:
    from open_brain_engine.engine.mcp_eligibility import capture_visible, record_visible

    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain"), faults={CaptureFault.AFTER_CONSUMER_SEGMENT}
    )
    plan = _plan(provider, (_conversation(text="intakequartz " + "x" * 60010),))
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    with pytest.raises(InjectedFault):
        engine.consumer_imports.commit_consumer_import(
            cast(str, preview["preview_id"]), plan.operation_id, plan, authority=_owner(engine)
        )
    capture = cast(str, _rows(engine, "SELECT capture_id FROM captures")[0][0])
    assert _rows(engine, "SELECT stage FROM captures") == [(3,)]
    assert engine.capture.get(capture) is None
    assert engine.retrieval.fetch(capture) is None
    assert engine.retrieval.read_page(capture) is None
    assert not engine.retrieval.search("intakequartz")
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(
            RecordReadRequest(record_id=capture, expected_revision_id=capture),
            authority=_owner(engine),
        )
    with pytest.raises(T03Error, match="not_found"):
        engine.history.list_history(HistoryListRequest(record_id=capture), authority=_owner(engine))
    with pytest.raises(T03Error, match="not_found"):
        engine.history.read_history(
            RecordReadRequest(record_id=capture, expected_revision_id=capture),
            authority=_owner(engine),
        )
    # Review is the only path that can create a canonical/graph derivation;
    # it must reject the staged source before retaining a proposal or body.
    with pytest.raises(T03Error, match="not_found"):
        engine.review.propose(
            (capture,),
            (ProposalDraft("Staged graph", "Staged derived body"),),
            delivery_id="consumer-staging-review",
        )
    assert engine.review.list() == ()
    with pytest.raises(T03Error, match="not_found"):
        engine.capture.retry_enrichment(capture, delivery_id="consumer-staging-provider")
    with closing(engine._store.connect()) as connection:
        for authority in (_owner(engine), None):
            assert not capture_visible(connection, capture, authority)
            assert not capture_visible(connection, capture, authority, history=True)
            assert not record_visible(connection, "source", capture, authority)
        # These are the shared owner/history, sharing, provider and semantic
        # release predicates; no provider/model execution is needed to test them.
        for mode in EligibilityMode:
            assert not sharing_eligible(connection, capture, mode=mode)
    result = engine.consumer_imports.recover_consumer_import(
        plan.operation_id, authority=_owner(engine)
    )
    assert _counts(result)["committed"] == 1
    assert engine.capture.get(capture) is not None
    with closing(engine._store.connect()) as connection:
        assert sharing_eligible(connection, capture, mode=EligibilityMode.OWNER_CURRENT)
        assert sharing_eligible(connection, capture, mode=EligibilityMode.OWNER_HISTORY)


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_retained_graph_provenance_cannot_release_consumer_staging(
    tmp_path: Path, active_profiles: None, provider: str
) -> None:
    from open_brain_engine.engine.mcp_eligibility import graph_visible

    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain"), faults={CaptureFault.AFTER_CONSUMER_SEGMENT}
    )
    space = engine.inbox.create_space("Synthetic", delivery_id="graph-space").space_id
    engine.capture.accept(
        TextPayload("Independent graph control"),
        delivery_id="graph-control",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    engine.managed_workspace.setup(str(workspace), operation_id="graph-setup")
    plan = _plan(provider, (_conversation(text="intakequartz " + "x" * 60010),))
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    with pytest.raises(InjectedFault):
        engine.consumer_imports.commit_consumer_import(
            cast(str, preview["preview_id"]), plan.operation_id, plan, authority=_owner(engine)
        )
    capture = cast(
        str,
        _rows(
            engine,
            "SELECT capture_id FROM consumer_segment_bindings b JOIN captures c USING(capture_id)",
        )[0][0],
    )
    with engine._store.transaction() as connection:
        revision = connection.execute("SELECT * FROM managed_note_revisions").fetchone()
        assert graph_visible(connection, revision["note_id"], None)
        fields = dict(parse_markdown(bytes(revision["body_bytes"])).fields)
        fields["provenance"] = [capture]
        body = render_markdown(fields=fields, body="Synthetic retained graph quote").encode()
        # Simulate a stale retained derived projection; all ordinary canonical
        # source membership remains readable, so only this staging edge hides it.
        connection.execute("SAVEPOINT graph_probe")
        connection.execute(
            "INSERT INTO managed_note_revisions "
            "SELECT ?,note_id,revision_id,'edit',?,?,accepted_by_actor_id,provenance_json,"
            "privacy_json,recorded_at,? FROM managed_note_revisions WHERE revision_id=?",
            (
                "revision_staged",
                body,
                sha256(body).hexdigest(),
                "graph-staged",
                revision["revision_id"],
            ),
        )
        assert not graph_visible(connection, revision["note_id"], None)
        assert not graph_visible(connection, revision["note_id"], _owner(engine))
        connection.execute("ROLLBACK TO graph_probe")
        connection.execute("RELEASE graph_probe")


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_missing_conversation_in_repackaged_archive_has_no_lifecycle_effect(
    tmp_path: Path, active_profiles: None, provider: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan(provider, (_conversation("first"), _conversation("second")))
    preview, first = _commit(engine, plan)
    assert _counts(first)["committed"] == 2
    partial = replace(
        _reimport(plan, cast(str, preview["account_scope_id"])),
        conversations=(plan.conversations[0],),
    )
    _, result = _commit(engine, partial)
    assert _counts(result)["unchanged"] == 1 and result["session_id"] is None
    assert _rows(engine, "SELECT count(*) FROM captures") == [(2,)]
    assert len(engine.retrieval.search("intakequartz")) == 2
