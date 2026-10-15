"""The Slack branch must independently prove every retained intake binding."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine.sharing_contracts import SharingDecisionRequest, SharingError

from packages.app.tests.integration.engine.test_sharing_source_evidence import (
    SOURCE_CASES,
    _counts,
    _damage,
    _source_damage,
)
from packages.app.tests.integration.engine.test_slack_sharing import _single


@pytest.mark.parametrize("case", SOURCE_CASES)
def test_slack_receipt_corruption_refuses_preview_and_approval(tmp_path: Path, case: str) -> None:
    journey = _single(tmp_path)
    sharing = journey.tasks.sharing
    assert sharing is not None
    engine = sharing._engine
    request = journey.request(journey.sources()[0])
    preview = sharing.preview(request, authority=journey.owner)
    decision = SharingDecisionRequest(
        operation_id="approve.corruption",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=journey.brain_id,
        issuer_epoch=journey.epoch,
        destination_brain_id=journey.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    table, changes = _source_damage(engine, case)
    counts = _counts(engine)
    with _damage(engine, table, changes):
        with pytest.raises(SharingError, match="binding_mismatch|revision_changed|not_found"):
            sharing.preview(
                replace(request, operation_id="preview.corrupted"), authority=journey.owner
            )
        with pytest.raises(SharingError, match="binding_mismatch|revision_changed|not_found"):
            sharing.decide(decision, authority=journey.owner)
        assert _counts(engine) == counts


@pytest.mark.parametrize("transition", ["edit", "route", "withdraw"])
def test_v2_source_transition_after_preview_reserves_no_copy(
    tmp_path: Path, transition: str
) -> None:
    from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest

    from packages.app.tests.integration.services.test_slack_decision_recall import _message

    journey = _single(tmp_path)
    sharing = journey.tasks.sharing
    assert sharing is not None
    source = journey.sources()[0]
    preview = sharing.preview(journey.request(source), authority=journey.owner)
    if transition == "edit":
        journey.scan(
            [
                _message(
                    "1760000001.000001", "Changed onboarding rationale", edited="1760000301.000001"
                )
            ]
        )
    elif transition == "route":
        journey.route(source)
    else:
        journey.tasks.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.after-preview",
                source_id=source["source_id"],
                expected_head=source["head_capture_id"],
                expected_lifecycle_version=source["lifecycle_version"],
                brain_id=journey.brain_id,
                issuer_epoch=journey.epoch,
                reason_code="owner_choice",
            ),
            authority=journey.owner,
        )
    decision = SharingDecisionRequest(
        operation_id="approve.after-transition",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=journey.brain_id,
        issuer_epoch=journey.epoch,
        destination_brain_id=journey.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    with pytest.raises(SharingError, match="revision_changed"):
        sharing.decide(decision, authority=journey.owner)
    with sharing._engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0


def test_corrupted_independent_original_receipt_withholds_existing_copy(tmp_path: Path) -> None:
    from packages.app.tests.integration.engine.test_slack_sharing import _hidden

    journey = _single(tmp_path)
    sharing = journey.tasks.sharing
    assert sharing is not None
    approved = journey.approve(journey.sources()[0])[3]
    assert approved.copy_capture_id is not None
    table, changes = _source_damage(sharing._engine, "intake-receipt-head")
    with _damage(sharing._engine, table, changes):
        _hidden(journey, approved.copy_capture_id)
    assert journey.read(approved.copy_capture_id)["complete"]
