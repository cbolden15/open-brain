"""Engine follow-up tasks: propose, evidence checks, dedupe, capacity, replay."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload, local_schema
from open_brain_engine.engine.followup_contracts import (
    AddEvidenceRequest,
    DecideRequest,
    DecisionAction,
    EvidenceRef,
    EvidenceRole,
    FollowupError,
    FollowupItem,
    FollowupKind,
    FollowupState,
    ListFilter,
    OwnerKind,
    ProposeRequest,
)
from open_brain_engine.engine.followups import FollowupTasks
from open_brain_engine.engine.paging import read_snapshot
from open_brain_engine.engine.records import RecordProjector
from open_brain_engine.engine.source_intake import SourceRevisionSubmission
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import agent_authority, owner_authority
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission

FROZEN = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


@dataclass
class SyntheticSource:
    """One logical source created through the source intake so its head can move."""

    namespace: dict[str, str]
    head: str
    revisions: int


def _privacy(tier: PrivacyTier) -> PrivacyDecision:
    reason = {
        PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
        PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
        PrivacyTier.PERSONAL: PrivacyReason.PERSONAL_LOCAL_ONLY,
    }[tier]
    return PrivacyDecision.create(
        tier=tier,
        reason=reason,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _new_source(
    engine: BrainEngine, text: str, *, tier: PrivacyTier = PrivacyTier.WORK
) -> SyntheticSource:
    """Create a logical source whose first revision is ``text`` (synthetic public-job intake)."""
    namespace = dict(
        connector_name="synthetic",
        connection_id="followup",
        resource_id=str(uuid4()),
        external_id="one",
    )
    source = SyntheticSource(namespace=namespace, head="", revisions=0)
    _revise_source(engine, source, text, tier=tier)
    return source


def _revise_source(
    engine: BrainEngine, source: SyntheticSource, text: str, *, tier: PrivacyTier = PrivacyTier.WORK
) -> str:
    """Advance ``source`` to a new head revision with ``text``; returns the new head capture id.

    This is the real mechanism the engine offers (`engine.sources.submit_revision` with
    `expected_head` and a `predecessor` ordering), as `test_source_tasks.py` uses it.
    """
    base = _public_submission(engine.tasks, delivery_id="followup.rev." + str(uuid4()))
    submission = replace(base, payload=TextPayload(text), privacy=_privacy(tier))
    receipt = engine.sources.submit_revision(
        SourceRevisionSubmission(
            capture=submission,
            namespace=source.namespace,
            revision_key=f"rev-{source.revisions}",
            canonical_sha256=submission.request_sha256(),
            expected_head=source.head or None,
            ordering=(
                {"kind": "unordered"}
                if source.revisions == 0
                else {"kind": "predecessor", "revision_key": f"rev-{source.revisions - 1}"}
            ),
            expected_control_epoch=0,
        )
    )
    assert receipt.capture_id is not None, receipt
    source.head = receipt.capture_id
    source.revisions += 1
    return receipt.capture_id


class Clock:
    def __init__(self, now: datetime = FROZEN) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _engine(tmp_path: Path, clock: Clock | None = None) -> BrainEngine:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    return BrainEngine.open(profile, clock=clock or Clock())


def _connect(engine: BrainEngine) -> sqlite3.Connection:
    connection = sqlite3.connect(engine.profile.root / local_schema.PHASE1_STATE_DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return owner_authority(engine.tasks, session_id="owner-test")


def _agent(*grants: str) -> EffectiveAuthority:
    return agent_authority(
        principal_id="agent-1", session_id="agent-session", grants=frozenset(grants)
    )


def _capture(engine: BrainEngine, text: str, *, delivery: str, tier: PrivacyTier) -> str:
    return engine.capture.accept(
        TextPayload(text), delivery_id=delivery, privacy_tier=tier
    ).capture_id


def _projected_text(engine: BrainEngine, capture_id: str) -> str:
    with read_snapshot(engine) as connection:
        return RecordProjector(engine.profile, connection, _owner(engine)).read(capture_id).text


def _ref(
    engine: BrainEngine,
    capture_id: str,
    quote: str,
    *,
    role: EvidenceRole = EvidenceRole.ORIGIN,
) -> EvidenceRef:
    text = _projected_text(engine, capture_id)
    start = text.index(quote)
    end = start + len(quote)
    digest = sha256(text[start:end].encode("utf-8")).hexdigest()
    return EvidenceRef(capture_id, capture_id, start, end, digest, role)


def _propose(
    *refs: EvidenceRef,
    operation_id: str = "op-propose-1",
    summary: str = "Send the Atlas deck to Dana",
    owner: OwnerKind = OwnerKind.SELF,
    owner_name: str | None = None,
    due: date | None = None,
    due_evidence_index: int | None = None,
    kind: FollowupKind = FollowupKind.COMMITMENT,
) -> ProposeRequest:
    return ProposeRequest(
        operation_id=operation_id,
        kind=kind,
        summary=summary,
        project="Atlas",
        owner=owner,
        owner_name=owner_name,
        due=due,
        due_evidence_index=due_evidence_index,
        evidence=tuple(refs),
    )


def test_propose_creates_suggested_item_fixing_tier_from_origin(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine,
        "I will send the Atlas deck to Dana by Friday",
        delivery="d1",
        tier=PrivacyTier.WORK,
    )
    item = tasks.propose(
        _propose(_ref(engine, capture, "send the Atlas deck")), authority=_owner(engine)
    )
    assert item.item_id.startswith("followup_")
    assert (item.kind, item.state, item.version, item.owner, item.owner_confirmed) == (
        FollowupKind.COMMITMENT,
        FollowupState.SUGGESTED,
        1,
        OwnerKind.SELF,
        False,
    )
    assert (item.privacy_tier, item.due, item.summary, item.project) == (
        "work",
        None,
        "Send the Atlas deck to Dana",
        "Atlas",
    )
    assert item.created_at == item.updated_at == "2026-10-23T09:00:00Z"
    with _connect(engine) as connection:
        evidence = connection.execute(
            "SELECT role,capture_id,revision_id FROM followup_evidence WHERE item_id=?",
            (item.item_id,),
        ).fetchall()
        assert [tuple(row) for row in evidence] == [("origin", capture, capture)]
        log = connection.execute(
            "SELECT action,prior_state,new_state,prior_version,new_version,principal_id,"
            "operation_id FROM followup_decisions"
        ).fetchall()
        assert [tuple(row) for row in log] == [
            ("propose", None, "suggested", 0, 1, engine.profile.owner_actor_id, "op-propose-1")
        ]


def test_propose_due_binds_to_the_stating_quote(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine, "Deck is due 2026-11-01 for Dana", delivery="d1", tier=PrivacyTier.WORK
    )
    stating = _ref(engine, capture, "due 2026-11-01", role=EvidenceRole.SUPPORTING)
    item = tasks.propose(
        _propose(
            _ref(engine, capture, "Deck is due"),
            stating,
            due=date(2026, 11, 1),
            due_evidence_index=1,
        ),
        authority=_owner(engine),
    )
    assert item.due == date(2026, 11, 1)
    with _connect(engine) as connection:
        stated = connection.execute(
            "SELECT evidence_id FROM followup_evidence WHERE item_id=? AND quote_start=?",
            (item.item_id, stating.quote_start),
        ).fetchone()[0]
    assert item.due_evidence_id == stated


@pytest.mark.parametrize("mutation", ["shift", "hash", "range"])
def test_propose_refuses_quotes_that_do_not_match_the_revision(
    tmp_path: Path, mutation: str
) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    good = _ref(engine, capture, "Ship the report")
    if mutation == "shift":
        bad = EvidenceRef(
            capture,
            capture,
            good.quote_start + 1,
            good.quote_end + 1,
            good.quote_sha256,
            EvidenceRole.ORIGIN,
        )
    elif mutation == "hash":
        bad = EvidenceRef(
            capture, capture, good.quote_start, good.quote_end, "0" * 64, EvidenceRole.ORIGIN
        )
    else:
        bad = EvidenceRef(capture, capture, 0, 10_000, good.quote_sha256, EvidenceRole.ORIGIN)
    with pytest.raises(FollowupError, match="evidence_mismatch"):
        tasks.propose(_propose(bad), authority=_owner(engine))
    with _connect(engine) as connection:
        assert connection.execute("SELECT count(*) FROM followup_items").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM followup_decisions").fetchone()[0] == 0


def test_propose_refuses_a_superseded_source_revision(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    source = _new_source(engine, "Ship the report on Monday")
    ref = _ref(engine, source.head, "Ship the report")
    # A second accepted revision of the same logical source moves the head; a new proposal
    # must cite the current head (read-time projection of existing items is Task 10's job).
    _revise_source(engine, source, "Ship the report on Tuesday")
    with pytest.raises(FollowupError, match="evidence_mismatch"):
        tasks.propose(_propose(ref), authority=_owner(engine))
    assert (
        tasks.propose(
            _propose(_ref(engine, source.head, "Ship the report"), operation_id="op-head"),
            authority=_owner(engine),
        ).version
        == 1
    )


def test_propose_refuses_unreadable_evidence_as_not_found(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    personal = _capture(
        engine, "Call the dentist tomorrow", delivery="d1", tier=PrivacyTier.PERSONAL
    )
    ref = _ref(engine, personal, "Call the dentist")
    work_only = EffectiveAuthority(
        principal_id="agent-1",
        session_id="agent-session",
        capabilities=frozenset({"followup-propose", "content-read"}),
        space_ids=None,
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
    )
    with pytest.raises(FollowupError, match="not_found"):
        tasks.propose(_propose(ref), authority=work_only)
    with pytest.raises(FollowupError, match="not_found"):
        tasks.propose(
            _propose(
                EvidenceRef(
                    "capture_missing", "capture_missing", 0, 3, "b" * 64, EvidenceRole.ORIGIN
                )
            ),
            authority=_owner(engine),
        )


def test_propose_requires_followup_propose_grant_or_owner(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    ref = _ref(engine, capture, "Ship the report")
    with pytest.raises(T03Error, match="unsupported_capability"):
        tasks.propose(_propose(ref), authority=_agent("followup-read"))
    item = tasks.propose(_propose(ref), authority=_agent("followup-propose"))
    assert item.state is FollowupState.SUGGESTED


def test_propose_refuses_mixed_tier_evidence(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    work = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    personal = _capture(engine, "Also book the dentist", delivery="d2", tier=PrivacyTier.PERSONAL)
    with pytest.raises(FollowupError, match="tier_mismatch"):
        tasks.propose(
            _propose(
                _ref(engine, work, "Ship the report"),
                _ref(engine, personal, "book the dentist", role=EvidenceRole.SUPPORTING),
            ),
            authority=_owner(engine),
        )


def test_propose_refuses_a_duplicate_origin_with_the_existing_id(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    ref = _ref(engine, capture, "Ship the report")
    first = tasks.propose(_propose(ref), authority=_owner(engine))
    with pytest.raises(FollowupError, match="duplicate_item") as info:
        tasks.propose(
            _propose(ref, operation_id="op-propose-2", summary="Other words"),
            authority=_owner(engine),
        )
    assert info.value.existing_item_id == first.item_id
    # A different quote in the same revision is a different item.
    other = tasks.propose(
        _propose(_ref(engine, capture, "on Monday"), operation_id="op-propose-3"),
        authority=_owner(engine),
    )
    assert other.item_id != first.item_id


def test_propose_replays_the_same_operation_and_refuses_a_changed_one(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    request = _propose(_ref(engine, capture, "Ship the report"))
    first = tasks.propose(request, authority=_owner(engine))
    assert tasks.propose(request, authority=_owner(engine)) == first
    with pytest.raises(FollowupError, match="operation_conflict"):
        tasks.propose(
            _propose(_ref(engine, capture, "Ship the report"), summary="Changed summary"),
            authority=_owner(engine),
        )
    with _connect(engine) as connection:
        assert connection.execute("SELECT count(*) FROM followup_decisions").fetchone()[0] == 1


def test_propose_enforces_item_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import open_brain_engine.engine.followups as module

    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    monkeypatch.setattr(module, "MAX_ITEMS", 1)
    tasks.propose(_propose(_ref(engine, capture, "Ship the report")), authority=_owner(engine))
    with pytest.raises(FollowupError, match="capacity_reached"):
        tasks.propose(
            _propose(_ref(engine, capture, "on Monday"), operation_id="op-propose-2"),
            authority=_owner(engine),
        )


def _item(
    engine: BrainEngine,
    tasks: FollowupTasks,
    capture: str,
    quote: str,
    *,
    operation_id: str = "op-propose-1",
) -> FollowupItem:
    return tasks.propose(
        _propose(_ref(engine, capture, quote), operation_id=operation_id),
        authority=_owner(engine),
    )


def test_add_evidence_appends_a_row_and_bumps_version(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine, "Ship the report on Monday. Dana agreed.", delivery="d1", tier=PrivacyTier.WORK
    )
    item = _item(engine, tasks, capture, "Ship the report")
    updated = tasks.add_evidence(
        AddEvidenceRequest(
            "op-ev-1",
            item.item_id,
            _ref(engine, capture, "Dana agreed", role=EvidenceRole.SUPPORTING),
        ),
        authority=_owner(engine),
    )
    assert (updated.version, updated.state, updated.new_evidence_since_decision) == (
        2,
        FollowupState.SUGGESTED,
        False,
    )
    with _connect(engine) as connection:
        roles = [
            row[0]
            for row in connection.execute(
                "SELECT role FROM followup_evidence WHERE item_id=? ORDER BY ordinal",
                (item.item_id,),
            )
        ]
        assert roles == ["origin", "supporting"]
        log = connection.execute(
            "SELECT action,prior_version,new_version,evidence_ids_json FROM followup_decisions "
            "ORDER BY sequence"
        ).fetchall()
        assert [row["action"] for row in log] == ["propose", "add_evidence"]
        assert (log[1]["prior_version"], log[1]["new_version"]) == (1, 2)
        assert len(json.loads(log[1]["evidence_ids_json"])) == 1


def test_add_evidence_refuses_other_tier_and_hidden_items(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    work = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    personal = _capture(engine, "Book the dentist", delivery="d2", tier=PrivacyTier.PERSONAL)
    item = _item(engine, tasks, work, "Ship the report")
    with pytest.raises(FollowupError, match="tier_mismatch"):
        tasks.add_evidence(
            AddEvidenceRequest(
                "op-ev-1",
                item.item_id,
                _ref(engine, personal, "Book the dentist", role=EvidenceRole.SUPPORTING),
            ),
            authority=_owner(engine),
        )
    personal_item = _item(engine, tasks, personal, "Book the dentist", operation_id="op-propose-2")
    work_only = EffectiveAuthority(
        principal_id="agent-1",
        session_id="s",
        space_ids=None,
        capabilities=frozenset({"followup-propose", "content-read"}),
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
    )
    with pytest.raises(FollowupError, match="not_found"):
        tasks.add_evidence(
            AddEvidenceRequest(
                "op-ev-2",
                personal_item.item_id,
                _ref(engine, personal, "dentist", role=EvidenceRole.SUPPORTING),
            ),
            authority=work_only,
        )
    with pytest.raises(FollowupError, match="not_found"):
        tasks.add_evidence(
            AddEvidenceRequest(
                "op-ev-3",
                "followup_missing",
                _ref(engine, work, "on Monday", role=EvidenceRole.SUPPORTING),
            ),
            authority=_owner(engine),
        )


def test_add_evidence_marks_closed_items_and_enforces_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_brain_engine.engine.followups as module

    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine, "Ship the report on Monday. Done now.", delivery="d1", tier=PrivacyTier.WORK
    )
    item = _item(engine, tasks, capture, "Ship the report")
    dismissed = tasks.decide(
        DecideRequest("op-d1", item.item_id, 1, DecisionAction.DISMISS), authority=_owner(engine)
    )
    assert dismissed.state is FollowupState.DISMISSED
    flagged = tasks.add_evidence(
        AddEvidenceRequest(
            "op-ev-1",
            item.item_id,
            _ref(engine, capture, "Done now", role=EvidenceRole.RESOLUTION),
        ),
        authority=_owner(engine),
    )
    assert (flagged.state, flagged.new_evidence_since_decision, flagged.version) == (
        FollowupState.DISMISSED,
        True,
        3,
    )
    monkeypatch.setattr(module, "MAX_EVIDENCE_PER_ITEM", 2)
    with pytest.raises(FollowupError, match="capacity_reached"):
        tasks.add_evidence(
            AddEvidenceRequest(
                "op-ev-2",
                item.item_id,
                _ref(engine, capture, "on Monday", role=EvidenceRole.SUPPORTING),
            ),
            authority=_owner(engine),
        )


def test_add_evidence_replays_and_detects_conflicts(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine, "Ship the report on Monday. Dana agreed.", delivery="d1", tier=PrivacyTier.WORK
    )
    item = _item(engine, tasks, capture, "Ship the report")
    request = AddEvidenceRequest(
        "op-ev-1", item.item_id, _ref(engine, capture, "Dana agreed", role=EvidenceRole.SUPPORTING)
    )
    first = tasks.add_evidence(request, authority=_owner(engine))
    assert tasks.add_evidence(request, authority=_owner(engine)) == first
    with pytest.raises(FollowupError, match="operation_conflict"):
        tasks.add_evidence(
            AddEvidenceRequest(
                "op-ev-1",
                item.item_id,
                _ref(engine, capture, "on Monday", role=EvidenceRole.SUPPORTING),
            ),
            authority=_owner(engine),
        )


def _decide(
    tasks: FollowupTasks,
    engine: BrainEngine,
    item_id: str,
    version: int,
    action: DecisionAction,
    *,
    operation_id: str,
    authority: EffectiveAuthority | None = None,
    **fields: object,
) -> FollowupItem:
    return tasks.decide(
        DecideRequest(operation_id, item_id, version, action, **fields),  # type: ignore[arg-type]
        authority=authority or _owner(engine),
    )


def test_decide_confirm_dismiss_reopen_record_each_transition(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    item = _item(engine, tasks, capture, "Ship the report")
    confirmed = _decide(tasks, engine, item.item_id, 1, DecisionAction.CONFIRM, operation_id="c1")
    assert (confirmed.state, confirmed.owner_confirmed, confirmed.version) == (
        FollowupState.CONFIRMED,
        True,
        2,
    )
    dismissed = _decide(tasks, engine, item.item_id, 2, DecisionAction.DISMISS, operation_id="c2")
    assert (dismissed.state, dismissed.version) == (FollowupState.DISMISSED, 3)
    reopened = _decide(tasks, engine, item.item_id, 3, DecisionAction.REOPEN, operation_id="c3")
    assert (reopened.state, reopened.version) == (FollowupState.CONFIRMED, 4)
    with pytest.raises(FollowupError, match="invalid_request"):
        _decide(tasks, engine, item.item_id, 4, DecisionAction.REOPEN, operation_id="c4")
    with _connect(engine) as connection:
        rows = connection.execute(
            "SELECT action,prior_state,new_state,prior_version,new_version FROM "
            "followup_decisions ORDER BY sequence"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("propose", None, "suggested", 0, 1),
        ("confirm", "suggested", "confirmed", 1, 2),
        ("dismiss", "confirmed", "dismissed", 2, 3),
        ("reopen", "dismissed", "confirmed", 3, 4),
    ]


def test_decide_reopen_of_never_confirmed_item_returns_to_suggested(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    item = _item(engine, tasks, capture, "Ship the report")
    _decide(tasks, engine, item.item_id, 1, DecisionAction.DISMISS, operation_id="c1")
    reopened = _decide(tasks, engine, item.item_id, 2, DecisionAction.REOPEN, operation_id="c2")
    assert (reopened.state, reopened.owner_confirmed) == (FollowupState.SUGGESTED, False)


def test_decide_snooze_returns_to_prior_state_when_the_clock_passes(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    item = _item(engine, tasks, capture, "Ship the report")
    _decide(tasks, engine, item.item_id, 1, DecisionAction.CONFIRM, operation_id="c1")
    with pytest.raises(FollowupError, match="invalid_request"):
        _decide(
            tasks,
            engine,
            item.item_id,
            2,
            DecisionAction.SNOOZE,
            operation_id="c2",
            snoozed_until=date(2026, 10, 23),
        )
    snoozed = _decide(
        tasks,
        engine,
        item.item_id,
        2,
        DecisionAction.SNOOZE,
        operation_id="c3",
        snoozed_until=date(2026, 10, 30),
    )
    assert (snoozed.state, snoozed.snoozed_until, snoozed.snooze_return_state) == (
        FollowupState.SNOOZED,
        date(2026, 10, 30),
        FollowupState.CONFIRMED,
    )
    shown, _evidence, _history = tasks.show(item.item_id, authority=_owner(engine))
    assert shown.state is FollowupState.SNOOZED
    clock.now = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
    shown, _evidence, _history = tasks.show(item.item_id, authority=_owner(engine))
    assert (shown.state, shown.snoozed_until, shown.version) == (FollowupState.CONFIRMED, None, 3)
    # Deciding after expiry acts on the returned state and records it as prior.
    dismissed = _decide(tasks, engine, item.item_id, 3, DecisionAction.DISMISS, operation_id="c4")
    assert (dismissed.state, dismissed.snoozed_until) == (FollowupState.DISMISSED, None)
    with _connect(engine) as connection:
        prior = connection.execute(
            "SELECT prior_state FROM followup_decisions WHERE operation_id='c4'"
        ).fetchone()[0]
    assert prior == "confirmed"


def test_decide_correct_link_resolve_and_supersede(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine, "Ship the report by 2026-11-05. Shipped it.", delivery="d1", tier=PrivacyTier.WORK
    )
    item = _item(engine, tasks, capture, "Ship the report")
    with_date = tasks.add_evidence(
        AddEvidenceRequest(
            "e1",
            item.item_id,
            _ref(engine, capture, "by 2026-11-05", role=EvidenceRole.SUPPORTING),
        ),
        authority=_owner(engine),
    )
    with _connect(engine) as connection:
        date_evidence = connection.execute(
            "SELECT evidence_id FROM followup_evidence WHERE item_id=? AND role='supporting'",
            (item.item_id,),
        ).fetchone()[0]
    with pytest.raises(FollowupError, match="invalid_request"):
        _decide(
            tasks,
            engine,
            item.item_id,
            with_date.version,
            DecisionAction.CORRECT,
            operation_id="c0",
            corrections={"due": "2026-11-05", "due_evidence_id": "evidence_other"},
        )
    corrected = _decide(
        tasks,
        engine,
        item.item_id,
        with_date.version,
        DecisionAction.CORRECT,
        operation_id="c1",
        corrections={
            "owner": "other",
            "owner_name": "Dana",
            "due": "2026-11-05",
            "due_evidence_id": date_evidence,
            "project": "Atlas v2",
            "summary": "Dana ships the report",
        },
    )
    assert (
        corrected.owner,
        corrected.owner_name,
        corrected.due,
        corrected.project,
        corrected.summary,
        corrected.due_evidence_id,
        corrected.state,
    ) == (
        OwnerKind.OTHER,
        "Dana",
        date(2026, 11, 5),
        "Atlas v2",
        "Dana ships the report",
        date_evidence,
        FollowupState.SUGGESTED,
    )
    linked = _decide(
        tasks,
        engine,
        item.item_id,
        corrected.version,
        DecisionAction.LINK,
        operation_id="c2",
        ticket_key="ATL-7",
        ticket_url="https://t.example/ATL-7",
    )
    assert (linked.ticket_key, linked.ticket_url) == ("ATL-7", "https://t.example/ATL-7")
    with pytest.raises(FollowupError, match="invalid_request"):
        _decide(
            tasks,
            engine,
            item.item_id,
            linked.version,
            DecisionAction.RESOLVE,
            operation_id="c3",
            resolution="evidence",
        )
    tasks.add_evidence(
        AddEvidenceRequest(
            "e2",
            item.item_id,
            _ref(engine, capture, "Shipped it", role=EvidenceRole.RESOLUTION),
        ),
        authority=_owner(engine),
    )
    resolved = _decide(
        tasks,
        engine,
        item.item_id,
        linked.version + 1,
        DecisionAction.RESOLVE,
        operation_id="c4",
        resolution="evidence",
    )
    assert (resolved.state, resolved.resolution) == (FollowupState.RESOLVED, "evidence")
    with _connect(engine) as connection:
        resolution_ids = json.loads(
            connection.execute(
                "SELECT evidence_ids_json FROM followup_decisions WHERE operation_id='c4'"
            ).fetchone()[0]
        )
    assert len(resolution_ids) == 1

    other = tasks.propose(
        _propose(
            _ref(engine, capture, "Shipped it"),
            operation_id="p2",
            summary="Ship the report (duplicate wording)",
        ),
        authority=_owner(engine),
    )
    with pytest.raises(FollowupError, match="not_found"):
        _decide(
            tasks,
            engine,
            other.item_id,
            1,
            DecisionAction.SUPERSEDE,
            operation_id="s0",
            superseded_by="followup_missing",
        )
    superseded = _decide(
        tasks,
        engine,
        other.item_id,
        1,
        DecisionAction.SUPERSEDE,
        operation_id="s1",
        superseded_by=item.item_id,
    )
    assert (superseded.state, superseded.superseded_by) == (FollowupState.SUPERSEDED, item.item_id)
    with pytest.raises(FollowupError, match="invalid_request"):
        _decide(
            tasks,
            engine,
            superseded.item_id,
            superseded.version,
            DecisionAction.LINK,
            operation_id="s2",
            ticket_key="X-1",
            ticket_url="https://t.example/X-1",
        )


def test_decide_resolve_by_owner_statement_needs_no_evidence(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    item = _item(engine, tasks, capture, "Ship the report")
    resolved = _decide(
        tasks,
        engine,
        item.item_id,
        1,
        DecisionAction.RESOLVE,
        operation_id="r1",
        resolution="owner_statement",
    )
    assert (resolved.state, resolved.resolution) == (FollowupState.RESOLVED, "owner_statement")


def test_decide_version_conflict_reports_current_version_and_writes_nothing(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    item = _item(engine, tasks, capture, "Ship the report")
    _decide(tasks, engine, item.item_id, 1, DecisionAction.CONFIRM, operation_id="c1")
    with pytest.raises(FollowupError, match="version_conflict") as info:
        _decide(tasks, engine, item.item_id, 1, DecisionAction.DISMISS, operation_id="c2")
    assert info.value.current_version == 2
    with _connect(engine) as connection:
        assert connection.execute("SELECT count(*) FROM followup_decisions").fetchone()[0] == 2


def test_decide_replays_same_request_and_refuses_changed_request(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    item = _item(engine, tasks, capture, "Ship the report")
    request = DecideRequest("c1", item.item_id, 1, DecisionAction.CONFIRM)
    first = tasks.decide(request, authority=_owner(engine))
    assert tasks.decide(request, authority=_owner(engine)) == first
    with pytest.raises(FollowupError, match="operation_conflict"):
        tasks.decide(
            DecideRequest("c1", item.item_id, 1, DecisionAction.DISMISS),
            authority=_owner(engine),
        )


def test_decide_requires_followup_decide_grant(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    item = _item(engine, tasks, capture, "Ship the report")
    with pytest.raises(T03Error, match="unsupported_capability"):
        _decide(
            tasks,
            engine,
            item.item_id,
            1,
            DecisionAction.CONFIRM,
            operation_id="c1",
            authority=_agent("followup-propose"),
        )
    confirmed = _decide(
        tasks,
        engine,
        item.item_id,
        1,
        DecisionAction.CONFIRM,
        operation_id="c1",
        authority=_agent("followup-decide", "content-read"),
    )
    assert confirmed.state is FollowupState.CONFIRMED


def test_list_defaults_to_open_items_and_filters(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine,
        "Ship the report. Ask about the budget. Call Dana. Close ticket.",
        delivery="d1",
        tier=PrivacyTier.WORK,
    )
    owner = _owner(engine)
    a = tasks.propose(
        _propose(_ref(engine, capture, "Ship the report"), operation_id="p1"), authority=owner
    )
    clock.now = datetime(2026, 10, 23, 9, 1, tzinfo=UTC)
    b = tasks.propose(
        _propose(
            _ref(engine, capture, "Ask about the budget"),
            operation_id="p2",
            kind=FollowupKind.QUESTION,
            summary="Budget?",
            owner=OwnerKind.OTHER,
            owner_name="Dana",
        ),
        authority=owner,
    )
    clock.now = datetime(2026, 10, 23, 9, 2, tzinfo=UTC)
    c = tasks.propose(
        _propose(
            _ref(engine, capture, "Call Dana"),
            operation_id="p3",
            due=date(2026, 10, 28),
            due_evidence_index=0,
        ),
        authority=owner,
    )
    clock.now = datetime(2026, 10, 23, 9, 3, tzinfo=UTC)
    d = tasks.propose(
        _propose(_ref(engine, capture, "Close ticket"), operation_id="p4"), authority=owner
    )
    _decide(tasks, engine, d.item_id, 1, DecisionAction.DISMISS, operation_id="c1")
    _decide(
        tasks,
        engine,
        c.item_id,
        1,
        DecisionAction.SNOOZE,
        operation_id="c2",
        snoozed_until=date(2026, 10, 25),
    )

    items, cursor = tasks.list(ListFilter(), authority=owner)
    assert [item.item_id for item in items] == [a.item_id, b.item_id]
    assert cursor is None
    items, _ = tasks.list(ListFilter(include_closed=True), authority=owner)
    assert [item.item_id for item in items] == [a.item_id, b.item_id, c.item_id, d.item_id]
    items, _ = tasks.list(ListFilter(states=frozenset({FollowupState.DISMISSED})), authority=owner)
    assert [item.item_id for item in items] == [d.item_id]
    items, _ = tasks.list(ListFilter(kinds=frozenset({FollowupKind.QUESTION})), authority=owner)
    assert [item.item_id for item in items] == [b.item_id]
    items, _ = tasks.list(ListFilter(owner=OwnerKind.OTHER), authority=owner)
    assert [item.item_id for item in items] == [b.item_id]
    items, _ = tasks.list(ListFilter(project="Nope"), authority=owner)
    assert items == ()
    # Snooze lapses at read time; c returns with its due date and is listed by due_before.
    clock.now = datetime(2026, 10, 25, 7, 0, tzinfo=UTC)
    items, _ = tasks.list(ListFilter(due_before=date(2026, 10, 29)), authority=owner)
    assert [(item.item_id, item.state) for item in items] == [(c.item_id, FollowupState.SUGGESTED)]


def test_list_returns_a_closed_item_with_new_evidence_by_default(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine, "Ship the report on Monday. Shipped it.", delivery="d1", tier=PrivacyTier.WORK
    )
    item = _item(engine, tasks, capture, "Ship the report")
    _decide(
        tasks,
        engine,
        item.item_id,
        1,
        DecisionAction.RESOLVE,
        operation_id="r1",
        resolution="owner_statement",
    )
    assert tasks.list(ListFilter(), authority=_owner(engine))[0] == ()
    tasks.add_evidence(
        AddEvidenceRequest(
            "e1",
            item.item_id,
            _ref(engine, capture, "Shipped it", role=EvidenceRole.RESOLUTION),
        ),
        authority=_owner(engine),
    )
    items, _ = tasks.list(ListFilter(), authority=_owner(engine))
    assert [(i.item_id, i.state, i.new_evidence_since_decision) for i in items] == [
        (item.item_id, FollowupState.RESOLVED, True)
    ]


def test_list_pages_with_a_cursor(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "one two three", delivery="d1", tier=PrivacyTier.WORK)
    owner = _owner(engine)
    ids = []
    for index, word in enumerate(["one", "two", "three"]):
        clock.now = datetime(2026, 10, 23, 9, index, tzinfo=UTC)
        ids.append(
            tasks.propose(
                _propose(_ref(engine, capture, word), operation_id=f"p{index}"), authority=owner
            ).item_id
        )
    first, cursor = tasks.list(ListFilter(limit=2), authority=owner)
    assert [item.item_id for item in first] == ids[:2]
    assert cursor == ids[1]
    rest, end = tasks.list(ListFilter(limit=2, cursor=cursor), authority=owner)
    assert ([item.item_id for item in rest], end) == ([ids[2]], None)
    with pytest.raises(FollowupError, match="invalid_request"):
        tasks.list(ListFilter(cursor="followup_unknown"), authority=owner)


def test_list_and_show_hide_items_a_session_cannot_read(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    work = _capture(engine, "Ship the report", delivery="d1", tier=PrivacyTier.WORK)
    personal = _capture(engine, "Book the dentist", delivery="d2", tier=PrivacyTier.PERSONAL)
    owner = _owner(engine)
    w = tasks.propose(
        _propose(_ref(engine, work, "Ship the report"), operation_id="p1"), authority=owner
    )
    p = tasks.propose(
        _propose(_ref(engine, personal, "Book the dentist"), operation_id="p2"), authority=owner
    )
    work_only = EffectiveAuthority(
        principal_id="agent-1",
        session_id="s",
        space_ids=None,
        capabilities=frozenset({"followup-read", "content-read"}),
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
    )
    items, _ = tasks.list(ListFilter(), authority=work_only)
    assert [item.item_id for item in items] == [w.item_id]
    with pytest.raises(FollowupError, match="not_found"):
        tasks.show(p.item_id, authority=work_only)
    with pytest.raises(T03Error, match="unsupported_capability"):
        tasks.list(ListFilter(), authority=_agent("content-read"))


def test_show_rebuilds_quotes_and_returns_history(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine, "Ship the report on Monday. Dana agreed.", delivery="d1", tier=PrivacyTier.WORK
    )
    item = _item(engine, tasks, capture, "Ship the report")
    tasks.add_evidence(
        AddEvidenceRequest(
            "e1",
            item.item_id,
            _ref(engine, capture, "Dana agreed", role=EvidenceRole.SUPPORTING),
        ),
        authority=_owner(engine),
    )
    shown, evidence, history = tasks.show(item.item_id, authority=_owner(engine))
    assert shown.version == 2
    assert [(view.role, view.quote) for view in evidence] == [
        (EvidenceRole.ORIGIN, "Ship the report"),
        (EvidenceRole.SUPPORTING, "Dana agreed"),
    ]
    assert [(row.action, row.new_version) for row in history] == [
        ("propose", 1),
        ("add_evidence", 2),
    ]
    assert history[1].evidence_ids == (evidence[1].evidence_id,)


def test_engine_exposes_followup_tasks(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert isinstance(engine.tasks.followups, FollowupTasks)
    assert engine.tasks.followups is engine.followups
    capture = _capture(engine, "Ship the report", delivery="d1", tier=PrivacyTier.WORK)
    item = engine.tasks.followups.propose(
        _propose(_ref(engine, capture, "Ship the report")), authority=_owner(engine)
    )
    items, _ = engine.tasks.followups.list(ListFilter(), authority=_owner(engine))
    assert [i.item_id for i in items] == [item.item_id]


def test_propose_replay_after_source_revised_returns_original_receipt(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    source = _new_source(engine, "Ship the report on Monday")
    request = _propose(_ref(engine, source.head, "Ship the report"))
    first = tasks.propose(request, authority=_owner(engine))
    _revise_source(engine, source, "Ship the report on Tuesday")
    assert tasks.propose(request, authority=_owner(engine)) == first


def test_add_evidence_replay_after_source_revised_returns_original_receipt(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    capture = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    item = _item(engine, tasks, capture, "Ship the report")
    source = _new_source(engine, "Dana agreed to review")
    request = AddEvidenceRequest(
        "op-ev-1",
        item.item_id,
        _ref(engine, source.head, "Dana agreed", role=EvidenceRole.SUPPORTING),
    )
    first = tasks.add_evidence(request, authority=_owner(engine))
    _revise_source(engine, source, "Dana agreed to review it later")
    assert tasks.add_evidence(request, authority=_owner(engine)) == first


def _hidden_from_work_session(
    engine: BrainEngine, tasks: FollowupTasks
) -> tuple[str, FollowupItem, EffectiveAuthority]:
    """An item whose origin a Work session reads but whose other source head turned Personal."""
    work = _capture(engine, "Ship the report on Monday", delivery="d1", tier=PrivacyTier.WORK)
    source = _new_source(engine, "Dana agreed to review")
    item = tasks.propose(
        _propose(
            _ref(engine, work, "Ship the report"),
            _ref(engine, source.head, "Dana agreed", role=EvidenceRole.SUPPORTING),
        ),
        authority=_owner(engine),
    )
    _revise_source(engine, source, "Dana agreed to review", tier=PrivacyTier.PERSONAL)
    work_only = EffectiveAuthority(
        principal_id="agent-1",
        session_id="s",
        space_ids=None,
        capabilities=frozenset({"followup-propose", "followup-read", "content-read"}),
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
    )
    return work, item, work_only


def test_propose_duplicate_of_a_hidden_item_is_not_found(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    work, _hidden, work_only = _hidden_from_work_session(engine, tasks)
    with pytest.raises(FollowupError, match="not_found") as info:
        tasks.propose(
            _propose(_ref(engine, work, "Ship the report"), operation_id="op-propose-2"),
            authority=work_only,
        )
    assert info.value.existing_item_id is None


def test_list_refuses_a_hidden_item_as_cursor(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = FollowupTasks(engine)
    _work, hidden, work_only = _hidden_from_work_session(engine, tasks)
    with pytest.raises(FollowupError, match="invalid_request"):
        tasks.list(ListFilter(cursor=hidden.item_id), authority=work_only)


def test_add_evidence_logs_the_state_after_snooze_expiry(tmp_path: Path) -> None:
    clock = Clock()
    engine = _engine(tmp_path, clock)
    tasks = FollowupTasks(engine)
    capture = _capture(
        engine, "Ship the report on Monday. Dana agreed.", delivery="d1", tier=PrivacyTier.WORK
    )
    item = _item(engine, tasks, capture, "Ship the report")
    _decide(
        tasks,
        engine,
        item.item_id,
        1,
        DecisionAction.SNOOZE,
        operation_id="c1",
        snoozed_until=date(2026, 10, 25),
    )
    clock.now = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    updated = tasks.add_evidence(
        AddEvidenceRequest(
            "op-ev-1",
            item.item_id,
            _ref(engine, capture, "Dana agreed", role=EvidenceRole.SUPPORTING),
        ),
        authority=_owner(engine),
    )
    assert (updated.state, updated.snoozed_until) == (FollowupState.SUGGESTED, None)
    with _connect(engine) as connection:
        row = connection.execute(
            "SELECT prior_state,new_state FROM followup_decisions WHERE operation_id='op-ev-1'"
        ).fetchone()
    assert tuple(row) == ("suggested", "suggested")
