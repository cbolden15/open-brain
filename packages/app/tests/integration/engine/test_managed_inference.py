from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Never

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    ManagedAccessMode,
    ManagedInferenceRequest,
    ManagedProvider,
    ManagedWorkspaceFailure,
    TextPayload,
)
from open_brain_engine.engine.managed_inference import recover_inference_sessions
from open_brain_engine.storage.locks import LockBusyError
from open_brain_engine.storage.sqlite import SchemaError

from open_brain.profile import compile_single_user_local


def _engine(root: Path) -> BrainEngine:
    return BrainEngine.open(
        compile_single_user_local(root, starter_spaces=("Notes",))
    )


def _capture(engine: BrainEngine, text: str, delivery_id: str) -> str:
    engine.capture.accept(
        TextPayload(text),
        delivery_id=delivery_id,
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    return engine.retrieval.search(text, record_type="canonical")[0].result_id


def _setup(engine: BrainEngine, path: Path, *bodies: str) -> tuple[str, tuple[str, ...]]:
    note_ids = tuple(
        _capture(engine, body, f"managed.inference.capture.{index}")
        for index, body in enumerate(bodies)
    )
    path.mkdir(mode=0o700)
    receipt = engine.managed_workspace.setup(
        str(path), operation_id="managed.inference.setup"
    )
    return receipt.workspace_id, note_ids


def _grant(engine: BrainEngine, workspace_id: str) -> None:
    engine.managed_policy.grant_consent(
        workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        operation_id="managed.inference.consent",
    )


def _prepare(
    engine: BrainEngine,
    workspace_id: str,
    note_ids: tuple[str, ...],
    request_id: str,
) -> ManagedInferenceRequest:
    return engine.managed_inference.prepare(
        workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        "openai_api:synthetic-v1",
        note_ids,
        request_id=request_id,
        max_output_bytes=4096,
        timeout_seconds=30,
    )


def test_inference_is_revision_bound_and_link_acceptance_requires_explicit_materialization(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "brain")
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "Solar generation peaks at midday.",
        "Battery storage supplies power after sunset.",
    )
    _grant(engine, workspace_id)
    request_id = "request_00000000-0000-4000-8000-000000000101"

    prepared = _prepare(engine, workspace_id, note_ids, request_id)
    released = engine.managed_inference.release(request_id)
    suggestion = engine.managed_inference.record_suggestion(
        request_id,
        source_note_id=note_ids[0],
        target_note_id=note_ids[1],
        source_quote="peaks at midday",
        target_quote="after sunset",
        model="synthetic-graph-v1",
    )
    source_path = next((tmp_path / "workspace").rglob(f"{note_ids[0]}.md"))
    before_accept = source_path.read_bytes()

    accepted = engine.managed_inference.accept_suggestion(
        workspace_id,
        suggestion.suggestion_id,
        operation_id="managed.inference.accept-link",
    )

    assert prepared == released
    assert prepared.effective_privacy.reason.value == "personal_confirmed"
    assert prepared.effective_privacy.authority.cloud
    assert suggestion.source_revision_id == prepared.sources[0].revision_id
    assert suggestion.target_revision_id == prepared.sources[1].revision_id
    assert all(note_id not in prepared.prompt for note_id in note_ids)
    assert source_path.read_bytes() == before_accept
    assert accepted.status == "suggestion_accepted"

    engine.managed_workspace.materialize(
        workspace_id,
        note_ids[0],
        operation_id="managed.inference.materialize-link",
    )

    assert source_path.read_bytes() != before_accept
    assert f"[[{note_ids[1]}]]" in source_path.read_text(encoding="utf-8")
    with sqlite3.connect(tmp_path / "brain/.open-brain/state/phase1.sqlite3") as connection:
        link = connection.execute(
            "SELECT provenance_json FROM managed_links WHERE source_note_id = ?",
            (note_ids[0],),
        ).fetchone()
    assert link is not None
    assert json.loads(link[0]) == {
        "model": "synthetic-graph-v1",
        "provider": "openai_api",
        "request_id": request_id,
        "suggestion_id": suggestion.suggestion_id,
    }


def test_policy_change_invalidates_pending_work_before_adapter_release(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "First policy source.",
        "Second policy source.",
    )
    _grant(engine, workspace_id)
    request_id = "request_00000000-0000-4000-8000-000000000102"
    _prepare(engine, workspace_id, note_ids, request_id)

    engine.managed_policy.set_exclusion(
        workspace_id,
        "note",
        note_ids[0],
        excluded=True,
        operation_id="managed.inference.exclude",
    )

    with pytest.raises(ManagedWorkspaceFailure, match="stale_request"):
        engine.managed_inference.release(request_id)
    with sqlite3.connect(tmp_path / "brain/.open-brain/state/phase1.sqlite3") as connection:
        request = connection.execute(
            "SELECT status FROM managed_inference_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        budget = connection.execute(
            """SELECT reserved_requests, used_requests, uncertain_requests
            FROM managed_inference_budgets WHERE workspace_id = ?""",
            (workspace_id,),
        ).fetchone()
    assert request == ("cancelled",)
    assert budget == (0, 0, 0)


def test_materialization_conflict_preserves_both_versions_until_owner_resolution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    workspace = tmp_path / "workspace"
    engine = _engine(root)
    workspace_id, note_ids = _setup(
        engine,
        workspace,
        "First conflict source.",
        "Second conflict source.",
    )
    _grant(engine, workspace_id)
    request_id = "request_00000000-0000-4000-8000-000000000109"
    _prepare(engine, workspace_id, note_ids, request_id)
    engine.managed_inference.release(request_id)
    suggestion = engine.managed_inference.record_suggestion(
        request_id,
        source_note_id=note_ids[0],
        target_note_id=note_ids[1],
        source_quote="First conflict",
        target_quote="Second conflict",
        model="synthetic-graph-v1",
    )
    engine.managed_inference.accept_suggestion(
        workspace_id,
        suggestion.suggestion_id,
        operation_id="managed.conflict.accept-link",
    )
    source_path = next(workspace.rglob(f"{note_ids[0]}.md"))
    external = source_path.read_bytes() + b"\nOwner changed this after inference.\n"
    source_path.write_bytes(external)

    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.materialize(
            workspace_id,
            note_ids[0],
            operation_id="managed.conflict.first-materialize",
        )
    assert source_path.read_bytes() == external
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        conflict = connection.execute(
            "SELECT * FROM managed_conflicts WHERE note_id = ? AND status = 'open'",
            (note_ids[0],),
        ).fetchone()
        accepted = connection.execute(
            """SELECT body_bytes FROM managed_note_revisions
            WHERE revision_id = (
                SELECT accepted_revision_id FROM managed_notes WHERE note_id = ?
            )""",
            (note_ids[0],),
        ).fetchone()[0]
    assert conflict is not None
    assert conflict["candidate_body_bytes"] == external
    assert accepted != external

    summaries = engine.managed_workspace.open_conflicts(workspace_id)
    assert len(summaries) == 1
    assert summaries[0].note_id == note_ids[0]
    review = engine.managed_workspace.review_conflict(workspace_id, note_ids[0])
    assert review.conflict_id == summaries[0].conflict_id
    assert review.accepted_body.encode("utf-8") == accepted
    assert review.workspace_body.encode("utf-8") == external

    source_path.write_bytes(external + b"Changed while review was open.\n")
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.resolve_conflict(
            workspace_id,
            note_ids[0],
            "accepted",
            conflict_id=review.conflict_id,
            operation_id="managed.conflict.stale-resolution",
        )
    source_path.write_bytes(external)

    resolved = engine.managed_workspace.resolve_conflict(
        workspace_id,
        note_ids[0],
        "accepted",
        conflict_id=review.conflict_id,
        operation_id="managed.conflict.resolve",
    )
    replayed = engine.managed_workspace.resolve_conflict(
        workspace_id,
        note_ids[0],
        "accepted",
        conflict_id=review.conflict_id,
        operation_id="managed.conflict.resolve",
    )
    assert resolved.duplicate is False
    assert replayed.duplicate is True
    assert source_path.read_bytes() == external
    engine.managed_workspace.materialize(
        workspace_id,
        note_ids[0],
        operation_id="managed.conflict.final-materialize",
    )
    assert source_path.read_bytes() == accepted


def test_external_edit_cancels_reserved_request_without_releasing_content(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "First exact source.",
        "Second exact source.",
    )
    _grant(engine, workspace_id)
    request_id = "request_00000000-0000-4000-8000-000000000103"
    _prepare(engine, workspace_id, note_ids, request_id)
    source_path = next((tmp_path / "workspace").rglob(f"{note_ids[0]}.md"))
    source_path.write_bytes(source_path.read_bytes() + b"external edit\n")

    with pytest.raises(ManagedWorkspaceFailure, match="ineligible_source"):
        engine.managed_inference.release(request_id)

    with sqlite3.connect(tmp_path / "brain/.open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM managed_inference_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone() == ("cancelled",)


def test_restart_cancels_reserved_marks_dispatch_uncertain_and_deactivates_consent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "First restart source.",
        "Second restart source.",
    )
    _grant(engine, workspace_id)
    reserved_id = "request_00000000-0000-4000-8000-000000000104"
    dispatch_id = "request_00000000-0000-4000-8000-000000000105"
    _prepare(engine, workspace_id, note_ids, reserved_id)
    _prepare(engine, workspace_id, note_ids, dispatch_id)
    engine.managed_inference.release(dispatch_id)

    reopened = _engine(root)

    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        statuses = dict(
            connection.execute(
                "SELECT request_id, status FROM managed_inference_requests ORDER BY request_id"
            )
        )
        consent = connection.execute(
            "SELECT active, revoked_at FROM managed_consents WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        budget = connection.execute(
            """SELECT reserved_requests, used_requests, uncertain_requests
            FROM managed_inference_budgets WHERE workspace_id = ?""",
            (workspace_id,),
        ).fetchone()
    assert statuses == {reserved_id: "cancelled", dispatch_id: "uncertain"}
    assert consent is not None and consent[0] == 0 and consent[1] is not None
    assert budget == (0, 0, 1)
    with pytest.raises(ManagedWorkspaceFailure, match="active_consent_required"):
        _prepare(
            reopened,
            workspace_id,
            note_ids,
            "request_00000000-0000-4000-8000-000000000106",
        )


def test_journal_only_recovery_cancels_reserved_and_marks_dispatch_uncertain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "First direct cleanup source.",
        "Second direct cleanup source.",
    )
    _grant(engine, workspace_id)
    reserved_id = "request_00000000-0000-4000-8000-000000000111"
    dispatch_id = "request_00000000-0000-4000-8000-000000000112"
    _prepare(engine, workspace_id, note_ids, reserved_id)
    _prepare(engine, workspace_id, note_ids, dispatch_id)
    engine.managed_inference.release(dispatch_id)
    validations: list[str] = []

    recovered = recover_inference_sessions(
        engine.profile,
        validate_before_write=lambda: validations.append("validated"),
    )

    assert recovered == 3
    assert len(validations) >= 2
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        statuses = dict(
            connection.execute(
                "SELECT request_id, status FROM managed_inference_requests ORDER BY request_id"
            )
        )
        budget = connection.execute(
            """SELECT reserved_requests, used_requests, uncertain_requests
            FROM managed_inference_budgets WHERE workspace_id = ?""",
            (workspace_id,),
        ).fetchone()
        consent = connection.execute(
            "SELECT active FROM managed_consents WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
    assert statuses == {reserved_id: "cancelled", dispatch_id: "uncertain"}
    assert budget == (0, 0, 1)
    assert consent == (0,)


def test_journal_only_recovery_does_not_initialize_absent_state(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    profile = compile_single_user_local(root)

    with pytest.raises(SchemaError, match="schema|state"):
        recover_inference_sessions(profile, validate_before_write=lambda: None)

    assert not (root / ".open-brain/state/phase1.sqlite3").exists()


def test_journal_only_recovery_uses_the_engine_writer_lease(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")

    with (
        engine._writer_lease.acquire_shared_writer(),
        pytest.raises(LockBusyError, match="already held"),
    ):
        recover_inference_sessions(
            engine.profile,
            validate_before_write=lambda: None,
        )


def test_shared_budget_rejects_before_a_request_is_reserved(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "First budget source.",
        "Second budget source.",
    )
    _grant(engine, workspace_id)
    first_id = "request_00000000-0000-4000-8000-000000000107"
    _prepare(engine, workspace_id, note_ids, first_id)
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.execute(
            """UPDATE managed_inference_budgets SET request_limit = 1
            WHERE workspace_id = ? AND provider = 'openai_api'""",
            (workspace_id,),
        )

    with pytest.raises(ManagedWorkspaceFailure, match="budget_exhausted"):
        _prepare(
            engine,
            workspace_id,
            note_ids,
            "request_00000000-0000-4000-8000-000000000108",
        )

    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM managed_inference_requests"
        ).fetchone() == (1,)


def _public_member(engine: BrainEngine, note_id: str | None, *, restricted: bool) -> str:
    from open_brain_engine.core.models import (
        Authority,
        CaptureWhyOrigin,
        ContentOrigin,
        PrivacyDecision,
        PrivacyReason,
        PrivacyTier,
        Provenance,
    )
    from open_brain_engine.engine import DecisionOutcome, ProposalDraft, PublicJobCaptureContext

    from open_brain.services.space_inbox import SpaceInboxService

    actor = "actor_00000000-0000-4000-8000-000000000951"
    context = PublicJobCaptureContext.create(
        profile=engine.profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000952",
            "role_id": "role_00000000-0000-4000-8000-000000000953",
            "tenant_id": engine.profile.tenant_id,
        },
    )
    reference = "urn:synthetic:retained-member"
    capture = engine.capture.public_job_sink(context).submit(
        TextPayload("Synthetic coral member evidence"),
        delivery_id="privacy.member",
        source_origin=ContentOrigin.UNKNOWN,
        source_reference=reference,
        provenance=Provenance.create(
            source_ref=reference,
            content_origin=ContentOrigin.UNKNOWN,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.PERSONAL,
            reason=PrivacyReason.EXPLICIT_LOCAL_ONLY
            if restricted
            else PrivacyReason.PERSONAL_LOCAL_ONLY,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )
    SpaceInboxService(engine.inbox).inbox_route(
        {
            "capture_id": capture.capture_id,
            "space_id": engine.inbox.spaces()[0].space_id,
        }
    )
    proposal = engine.review.propose(
        (capture.capture_id,),
        (ProposalDraft("Reviewed page", "Synthetic coral member evidence"),),
        delivery_id="privacy.proposal",
        target_page_id=note_id,
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="privacy.approve",
        expected_review_digest=proposal.review_digest,
    )
    return engine.retrieval.search("coral", record_type="canonical")[0].result_id


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("restricted", [False, True])
def test_retained_public_job_privacy_gates_provider_callback(
    tmp_path: Path,
    primary: bool,
    restricted: bool,
) -> None:
    from open_brain.services.local_operations import refresh_graph

    engine = _engine(tmp_path / "brain")
    if primary:
        note_id = _public_member(engine, None, restricted=restricted)
        vault = tmp_path / "workspace"
        vault.mkdir(mode=0o700)
        workspace = engine.managed_workspace.setup(
            str(vault), operation_id="privacy.setup"
        ).workspace_id
    else:
        workspace, notes = _setup(engine, tmp_path / "workspace", "Ordinary primary evidence")
        note_id = _public_member(engine, notes[0], restricted=restricted)
        engine.managed_workspace.refresh(workspace, operation_id="privacy.refresh")
    _grant(engine, workspace)
    calls: list[str] = []

    def invoke(prompt: str, maximum: int, timeout: int) -> Never:
        calls.append(prompt)
        raise RuntimeError("synthetic callback reached")

    if restricted:
        with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
            refresh_graph(
                engine.tasks,
                provider=ManagedProvider.OPENAI_API,
                access_mode=ManagedAccessMode.API_KEY,
                adapter_identity="openai_api:synthetic-v1",
                request_id="request_00000000-0000-4000-8000-000000000955",
                invoke=invoke,
                remaining_attempts=1,
                remaining_input_bytes=16384,
            )
        assert calls == []
    else:
        request = _prepare(
            engine, workspace, (note_id,), "request_00000000-0000-4000-8000-000000000955"
        )
        assert (
            "Synthetic coral member evidence"
            in engine.managed_inference.release(request.request_id).prompt
        )


@pytest.mark.parametrize(
    "membership",
    [None, [], ["urn:unsupported:member"], ["capture_00000000-0000-4000-8000-000000000999"]],
)
def test_accepted_unverifiable_membership_fails_closed(tmp_path: Path, membership: object) -> None:
    from open_brain_engine.storage.markdown import parse_markdown, render_markdown

    engine = _engine(tmp_path / "brain")
    workspace, notes = _setup(engine, tmp_path / "workspace", "Ordinary evidence")
    page = next((tmp_path / "workspace").rglob("*.md"))
    parsed = parse_markdown(page.read_bytes())
    fields = dict(parsed.fields)
    fields["provenance"] = membership
    page.write_text(render_markdown(fields=fields, body=parsed.body))
    observation = engine.managed_workspace.observe(workspace)
    engine.managed_workspace.accept_observed(
        workspace,
        notes[0],
        generation=observation.generation,
        operation_id="privacy.accept-membership",
    )
    _grant(engine, workspace)
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        _prepare(engine, workspace, notes, "request_00000000-0000-4000-8000-000000000956")


def test_retained_membership_release_revalidates_supported_refresh(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    workspace, notes = _setup(engine, tmp_path / "workspace", "Ordinary primary evidence")
    _grant(engine, workspace)
    request = _prepare(engine, workspace, notes, "request_00000000-0000-4000-8000-000000000957")
    _public_member(engine, notes[0], restricted=True)
    engine.managed_workspace.refresh(workspace, operation_id="privacy.intervening-refresh")
    with pytest.raises(ManagedWorkspaceFailure, match="^(ineligible_source|stale_request)$"):
        engine.managed_inference.release(request.request_id)
    with engine._store.transaction() as connection:
        assert (
            connection.execute(
                "SELECT status FROM managed_inference_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()[0]
            == "cancelled"
        )


def test_accepted_edit_cannot_drop_retained_restricted_member(tmp_path: Path) -> None:
    from open_brain_engine.storage.markdown import parse_markdown, render_markdown

    engine = _engine(tmp_path / "brain")
    workspace, notes = _setup(engine, tmp_path / "workspace", "Ordinary primary evidence")
    _public_member(engine, notes[0], restricted=True)
    engine.managed_workspace.refresh(workspace, operation_id="privacy.refresh")
    page = next((tmp_path / "workspace").rglob("*.md"))
    parsed = parse_markdown(page.read_bytes())
    fields = dict(parsed.fields)
    members = fields["provenance"]
    assert isinstance(members, list) and len(members) == 2
    fields["provenance"] = members[:1]
    page.write_text(render_markdown(fields=fields, body=parsed.body))
    observed = engine.managed_workspace.observe(workspace)
    engine.managed_workspace.accept_observed(
        workspace, notes[0], generation=observed.generation, operation_id="privacy.remove-member"
    )
    _grant(engine, workspace)
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        _prepare(engine, workspace, notes, "request_00000000-0000-4000-8000-000000000960")


@pytest.mark.parametrize("raw", ["{}", "null", "not-json"])
def test_corrupt_retained_privacy_is_rejected_at_release(tmp_path: Path, raw: str) -> None:
    """Integrity negative only: inject malformed immutable source metadata."""
    engine = _engine(tmp_path / "brain")
    workspace, notes = _setup(engine, tmp_path / "workspace", "Ordinary primary evidence")
    _public_member(engine, notes[0], restricted=False)
    engine.managed_workspace.refresh(workspace, operation_id="privacy.refresh")
    _grant(engine, workspace)
    request = _prepare(engine, workspace, notes, "request_00000000-0000-4000-8000-000000000961")
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE captures SET privacy_json=? WHERE delivery_id='privacy.member'", (raw,)
        )
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        engine.managed_inference.release(request.request_id)
    with engine._store.transaction() as connection:
        assert (
            connection.execute(
                "SELECT status FROM managed_inference_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()[0]
            == "cancelled"
        )


@pytest.mark.parametrize("stage", ["after_target_write", "after_operation_promoted"])
def test_standalone_link_materialization_identical_acceptance(tmp_path: Path, stage: str) -> None:
    from open_brain_engine.engine import InjectedFault, ManagedWorkspaceFault

    engine = _engine(tmp_path / "brain")
    workspace, notes = _setup(engine, tmp_path / "workspace", "Alpha evidence", "Beta evidence")
    _grant(engine, workspace)
    request = _prepare(engine, workspace, notes, "request_00000000-0000-4000-8000-000000000962")
    engine.managed_inference.release(request.request_id)
    suggestion = engine.managed_inference.record_suggestion(
        request.request_id,
        source_note_id=notes[0],
        target_note_id=notes[1],
        source_quote="Alpha",
        target_quote="Beta",
        model="synthetic-v1",
    )
    engine.managed_inference.accept_suggestion(
        workspace, suggestion.suggestion_id, operation_id="standalone.link"
    )
    engine._faults.add(ManagedWorkspaceFault(stage))
    with pytest.raises(InjectedFault):
        engine.managed_workspace.materialize(workspace, notes[0], operation_id="standalone.pending")
    observation = engine.managed_workspace.observe(workspace)
    receipt = engine.managed_workspace.accept_observed(
        workspace,
        notes[0],
        generation=observation.generation,
        operation_id="standalone.same-head",
    )
    assert receipt.duplicate
    page = next((tmp_path / "workspace").rglob(f"{notes[0]}.md"))
    body = page.read_bytes()
    stat = page.stat()
    for _ in range(2):
        engine = _engine(engine.profile.root)
        assert engine.managed_workspace.materialize(
            workspace, notes[0], operation_id="standalone.pending"
        ).duplicate
        assert engine.managed_workspace.recover() == 0
        assert page.read_bytes() == body
        assert page.stat().st_mtime_ns == stat.st_mtime_ns
        with engine._store.transaction() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM managed_note_revisions WHERE note_id=?", (notes[0],)
                ).fetchone()[0]
                == 2
            )
