"""Engine follow-up tasks: propose, evidence checks, dedupe, capacity, replay."""

from __future__ import annotations

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
    EvidenceRef,
    EvidenceRole,
    FollowupError,
    FollowupKind,
    FollowupState,
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
