"""Cross-surface sharing custody, continuation and expiry regressions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.engine import (
    CaptureFault,
    DecisionOutcome,
    ManagedInferenceRequest,
    ManagedProvider,
    ManagedWorkspaceFailure,
    ProposalDraft,
    open_local_engine,
)
from open_brain_engine.engine.capture import CaptureTasks
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingError,
    SharingRevokeRequest,
)
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    SourceRouteRequest,
    T03Error,
)
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

import open_brain.services.local_entrypoints as entrypoints
from packages.app.tests.integration.engine.test_portability_v7 import (
    _advance_original,
)
from packages.app.tests.integration.engine.test_sharing_semantic import (
    _BACKENDS,
    _case,
    _grant,
    _prepare,
    _RecordingProvider,
    _refresh,
)
from packages.app.tests.integration.engine.test_sharing_surfaces import _external
from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


@pytest.mark.parametrize("failure", ["journal_commit", "index_update", "lost_response"])
def test_owner_cli_pending_receipt_recovers_one_exact_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    tasks, preview_request, _owner, _text = _managed_source(tmp_path)
    request_file = tmp_path / "request.json"

    def invoke(action: str, value: dict[str, object] | str) -> tuple[int, dict[str, Any]]:
        arguments: tuple[str, ...]
        if isinstance(value, str):
            arguments = (value,)
        else:
            request_file.write_text(json.dumps(value), encoding="utf-8")
            arguments = ("--request-file", str(request_file))
        code = entrypoints.run_cli(
            ("sharing", action, *arguments, "--data-dir", str(tasks.profile.root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        output = capsys.readouterr()
        assert output.err == ""
        return code, json.loads(output.out)

    code, preview = invoke("preview", preview_request.value())
    assert code == 0
    request = SharingDecisionRequest(
        operation_id="sharing.cli.pending",
        preview_id=preview["preview_id"],
        preview_sha256=preview["preview_sha256"],
        brain_id=preview_request.brain_id,
        issuer_epoch=preview_request.issuer_epoch,
        destination_brain_id=preview_request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    submit = CaptureTasks.submit
    admitted: list[str] = []

    def uncertain(self: CaptureTasks, submission: Any) -> Any:
        admitted.append(submission.request_sha256())
        if failure != "lost_response":
            self._engine._faults.add(
                CaptureFault.AFTER_JOURNAL_COMMIT
                if failure == "journal_commit"
                else CaptureFault.AFTER_INDEX_UPDATE
            )
        submit(self, submission)
        if failure == "lost_response":
            raise RuntimeError("synthetic CLI lost copy response")
        raise AssertionError("actual copy fault did not execute")

    with monkeypatch.context() as patch:
        patch.setattr(CaptureTasks, "submit", uncertain)
        code, pending = invoke("approve", request.value())
    assert code == 0 and pending["state"] == "pending"
    assert pending["copy_capture_id"] is None
    assert len(admitted) == 1
    with open_local_database_read_only(tasks.profile) as connection:
        frozen = tuple(
            connection.execute(
                "SELECT copy_delivery_id,copy_submission_sha256,copy_submission_bytes "
                "FROM sharing_decisions"
            ).fetchone()
        )
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0
    code, terminal = invoke("approve", request.value())
    assert code == 0 and terminal["state"] == "captured"
    assert terminal["copy_capture_id"] is not None
    assert terminal["approval_id"] == pending["approval_id"]
    assert terminal["copy_delivery_id"] == pending["copy_delivery_id"]
    assert invoke("approve", request.value()) == (0, terminal)
    code, inspected = invoke("inspect", terminal["approval_id"])
    assert code == 0 and inspected["decision_receipt"] == terminal
    with open_local_database_read_only(tasks.profile) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT copy_delivery_id,copy_submission_sha256,copy_submission_bytes "
                    "FROM sharing_decisions"
                ).fetchone()
            )
            == frozen
        )
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM captures WHERE delivery_id=?", (terminal["copy_delivery_id"],)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("transition", ["revoke", "withdraw", "route", "head"])
@pytest.mark.parametrize("caller_kind", ["external", "local"])
def test_managed_canonical_nonempty_history_cursors_close_on_transition(
    tmp_path: Path, transition: str, caller_kind: str
) -> None:
    tasks, request, owner, _text = _managed_source(tmp_path)
    assert tasks.sharing is not None and tasks.sources is not None and tasks.history is not None
    preview = tasks.sharing.preview(request, authority=owner)
    approved = tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.history.probe.approve",
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
    space = tasks.spaces.create_space(
        "Synthetic History", delivery_id="sharing.history.probe.space"
    )
    tasks.spaces.route(
        approved.copy_capture_id, space.space_id, delivery_id="sharing.history.probe.route"
    )
    page_id = None
    for index in range(2):
        proposal = tasks.review.propose(
            (approved.copy_capture_id,),
            (
                ProposalDraft(
                    f"Synthetic history {index}", f"Synthetic version {index} 漢字🙂\n" * 20
                ),
            ),
            delivery_id=f"sharing.history.probe.proposal.{index}",
            target_page_id=page_id,
        )[0]
        decision = tasks.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id=f"sharing.history.probe.decision.{index}",
            expected_review_digest=proposal.review_digest,
        )
        page_id = decision.page_id
    assert page_id is not None
    caller = (
        replace(
            _external(request.brain_id, request.issuer_epoch, "openai"),
            capabilities=frozenset({"history-read"}),
        )
        if caller_kind == "external"
        else EffectiveAuthority(
            "synthetic-local-history", "synthetic-session", frozenset({"history-read"}), None
        )
    )
    listing = HistoryListRequest(record_id=page_id, limit=1)
    first = tasks.history.list_history(listing, authority=caller).to_wire()
    assert first["next_cursor"] is not None and first["complete"] is False
    history_tail = replace(listing, cursor=first["next_cursor"])
    second = tasks.history.list_history(history_tail, authority=caller).to_wire()
    assert second["entries"] and second["complete"] is True
    reading = RecordReadRequest(
        record_id=page_id,
        expected_revision_id=second["entries"][0]["revision_id"],
        target_bytes=16,
    )
    chunk = tasks.history.read_history(reading, authority=caller).to_wire()
    assert chunk["next_cursor"] is not None and chunk["complete"] is False
    read_tail = replace(reading, cursor=chunk["next_cursor"])
    next_chunk = tasks.history.read_history(read_tail, authority=caller).to_wire()
    assert next_chunk["start_byte"] == chunk["end_byte"]
    assert next_chunk["content"]["text"]
    if transition == "revoke":
        tasks.sharing.revoke(
            SharingRevokeRequest(
                operation_id="sharing.history.probe.revoke",
                approval_id=approved.approval_id,
                expected_approval_version=1,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                reason="owner_choice",
            ),
            authority=owner,
        )
    elif transition == "withdraw":
        tasks.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.sharing.history.probe",
                source_id=request.source_id,
                expected_head=request.expected_head,
                expected_lifecycle_version=0,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                reason_code="owner_choice",
            ),
            authority=owner,
        )
    elif transition == "route":
        destination = tasks.spaces.create_space(
            "Synthetic New Route", delivery_id="sharing.history.probe.destination"
        )
        tasks.sources.route(
            SourceRouteRequest(
                source_id=request.source_id,
                expected_head=request.expected_head,
                expected_route_version=0,
                space_id=destination.space_id,
                operation_id="operation_" + str(uuid4()),
            ),
            authority=owner,
        )
    else:
        _advance_original(tasks, request)
    for current in (tasks, open_local_engine(tasks.profile)):
        assert current.history is not None
        current.portability.rebuild_index()
        with pytest.raises(T03Error, match="not_found|cursor_stale"):
            current.history.list_history(history_tail, authority=caller)
        with pytest.raises(T03Error, match="not_found|cursor_stale"):
            current.history.read_history(read_tail, authority=caller)
        owner_list = cast(
            dict[str, Any],
            current.history.list_history(replace(listing, limit=10), authority=owner).to_wire(),
        )
        assert len(owner_list["entries"]) == 2
        assert cast(
            dict[str, Any], current.history.read_history(reading, authority=owner).to_wire()
        )["content"]["text"]


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
@pytest.mark.parametrize("stage", ["prepare", "release"])
@pytest.mark.parametrize("damage", ["missing_link", "target_mismatch"])
def test_retained_link_custody_damage_refuses_before_semantic_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ManagedProvider,
    provider_id: str,
    stage: str,
    damage: str,
) -> None:
    case = _case(tmp_path, provider_id)
    _grant(case, provider)
    initial = _prepare(case, provider, (case.ordinary_note, case.copy_note))
    case.tasks.managed_inference.release(initial.request_id)
    suggestion = case.tasks.managed_inference.record_suggestion(
        initial.request_id,
        source_note_id=case.ordinary_note,
        target_note_id=case.copy_note,
        source_quote="Synthetic",
        target_quote="Synthetic",
        model="synthetic-v1",
    )
    case.tasks.managed_inference.accept_suggestion(
        case.workspace_id,
        suggestion.suggestion_id,
        operation_id="sharing.semantic.probe.link",
    )
    case.tasks.managed_workspace.materialize(
        case.workspace_id,
        case.ordinary_note,
        operation_id="sharing.semantic.probe.materialize",
    )
    source_path = next(case.workspace.rglob(f"{case.ordinary_note}.md"))
    parsed = parse_markdown(source_path.read_bytes())
    source_path.write_text(
        render_markdown(fields=parsed.fields, body="Synthetic ordinary evidence"), encoding="utf-8"
    )
    observed = case.tasks.managed_workspace.observe(case.workspace_id)
    case.tasks.managed_workspace.accept_observed(
        case.workspace_id,
        case.ordinary_note,
        generation=observed.generation,
        operation_id="sharing.semantic.probe.remove-visible-link",
    )
    positive = _prepare(case, provider, (case.ordinary_note,))
    case.tasks.managed_inference.release(positive.request_id)
    case.tasks.managed_inference.fail(positive.request_id)

    def corrupt() -> None:
        # Independently damage retained custody in this disposable fixture only.
        with sqlite3.connect(case.tasks.profile.root / ".open-brain/state/phase1.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM managed_links").fetchone()[0] == 1
            if damage == "missing_link":
                db.execute("DELETE FROM managed_links")
            else:
                db.execute("UPDATE managed_links SET target_note_id=?", ("note_" + str(uuid4()),))

    prepared_ids: list[str] = []
    if stage == "prepare":
        corrupt()
    else:
        release = case.engine.managed_inference.release

        def damaged_release(request_id: str) -> ManagedInferenceRequest:
            prepared_ids.append(request_id)
            corrupt()
            return release(request_id)

        monkeypatch.setattr(case.engine.managed_inference, "release", damaged_release)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        _refresh(case, provider, recording)
    assert recording.calls == []
    assert len(prepared_ids) == (1 if stage == "release" else 0)


@pytest.mark.parametrize("state", ["undecided", "captured", "rejected", "pending"])
def test_expired_exact_replay_preserves_prior_owner_decision_without_new_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    tasks, request, owner, _text = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    decision = SharingDecisionRequest(
        operation_id="sharing.expired.probe.decision",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="reject" if state == "rejected" else "approve",
    )
    prior = None
    if state == "pending":
        tasks.sharing._engine._faults.add(CaptureFault.AFTER_INDEX_UPDATE)
    if state != "undecided":
        prior = tasks.sharing.decide(decision, authority=owner)
        assert prior.state == state
    before = tasks.sharing._engine._clock()
    with monkeypatch.context() as patch:
        patch.setattr(tasks.sharing._engine, "_clock", lambda: before + timedelta(minutes=16))
        assert tasks.sharing.preview(request, authority=owner) == preview
        with pytest.raises(SharingError, match="invalid_arguments"):
            tasks.sharing.preview(replace(request, provider_ids=("openai",)), authority=owner)
        if state == "undecided":
            with pytest.raises(SharingError, match="preview_expired"):
                tasks.sharing.decide(decision, authority=owner)
        else:
            terminal = tasks.sharing.decide(decision, authority=owner)
            assert terminal.state == ("captured" if state == "pending" else state)
            assert prior is not None and terminal.approval_id == prior.approval_id
            if state != "pending":
                assert terminal == prior
            assert tasks.sharing.decide(decision, authority=owner) == terminal
            with pytest.raises(SharingError, match="invalid_arguments"):
                tasks.sharing.decide(
                    replace(decision, decision="approve" if state == "rejected" else "reject"),
                    authority=owner,
                )
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == int(
            state != "undecided"
        )
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == int(
            state in {"captured", "pending"}
        )
