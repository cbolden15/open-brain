"""A session sees a follow-up only when it can read every cited revision right now."""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.followup_contracts import (
    EvidenceRef,
    EvidenceRole,
    FollowupItem,
    FollowupKind,
    ListFilter,
    OwnerKind,
    ProposeRequest,
)
from open_brain_engine.engine.followup_visibility import (
    evidence_retracted,
    head_record,
    locate_quote,
    rebuild_quote,
    revision_text,
    visible_to,
)
from open_brain_engine.engine.owner_egress_policy import OwnerEgressPolicyTasks
from open_brain_engine.engine.records import RecordProjector
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import owner_authority
from packages.app.tests.unit.engine.test_followups import _new_source, _revise_source


def _engine(tmp_path: Path, *spaces: str) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=spaces))


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return owner_authority(engine.tasks, session_id="owner-test")


def _session(*tiers: PrivacyTier, space_ids: frozenset[str] | None = None) -> EffectiveAuthority:
    return EffectiveAuthority(
        "synthetic-client-principal",
        "synthetic-client-session",
        frozenset({"search", "content-read", "followup-read"}),
        space_ids,
        allowed_read_tiers=frozenset(tiers),
    )


def _external(engine: BrainEngine, *consented: PrivacyTier) -> EffectiveAuthority:
    return EffectiveAuthority(
        "synthetic-external-principal",
        "synthetic-external-session",
        frozenset({"search", "content-read", "followup-read"}),
        None,
        allowed_read_tiers=frozenset(consented),
        egress_mode=EgressMode.EXTERNAL_PROVIDER,
        provider_id="synthetic-provider",
        consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        brain_id=derive_brain_id(engine.profile.tenant_id),
        issuer_epoch=1,
    )


def _capture(engine: BrainEngine, tier: PrivacyTier, text: str) -> str:
    return engine.capture.accept(
        TextPayload(text), delivery_id="followup." + str(uuid4()), privacy_tier=tier
    ).capture_id


def _ref(
    engine: BrainEngine, capture_id: str, *, role: EvidenceRole = EvidenceRole.ORIGIN
) -> EvidenceRef:
    with closing(engine._store.connect()) as connection:
        text = (
            RecordProjector(engine.profile, connection, _owner(engine))
            .source(capture_id, expected=capture_id, history=True)
            .text
        )
    end = min(9, len(text))
    return EvidenceRef(
        capture_id, capture_id, 0, end, sha256(text[:end].encode("utf-8")).hexdigest(), role
    )


def _propose(engine: BrainEngine, *refs: EvidenceRef) -> FollowupItem:
    return engine.followups.propose(
        ProposeRequest(
            operation_id=str(uuid4()),
            kind=FollowupKind.COMMITMENT,
            summary="Synthetic follow-up summary",
            project="synthetic",
            owner=OwnerKind.SELF,
            owner_name=None,
            due=None,
            due_evidence_index=None,
            evidence=refs,
        ),
        authority=_owner(engine),
    )


def _visible(
    engine: BrainEngine,
    item: FollowupItem,
    refs: tuple[EvidenceRef, ...],
    authority: EffectiveAuthority,
) -> bool:
    with closing(engine._store.connect()) as connection:
        projector = RecordProjector(engine.profile, connection, authority)
        return visible_to(item, refs, authority=authority, projector=projector)


def test_rebuild_quote_requires_matching_position_and_hash() -> None:
    text = "Synthetic revision body for a quote"
    cap = "capture_synthetic"
    good = EvidenceRef(cap, cap, 10, 18, sha256(b"revision").hexdigest(), EvidenceRole.ORIGIN)
    assert rebuild_quote(text, good) == "revision"
    # Reversed, empty and negative positions are refused by EvidenceRef itself.
    for ref in (
        EvidenceRef(cap, cap, 10, 18, sha256(b"REVISION").hexdigest(), EvidenceRole.ORIGIN),
        EvidenceRef(cap, cap, 11, 19, good.quote_sha256, EvidenceRole.ORIGIN),
        EvidenceRef(cap, cap, 10, 99, good.quote_sha256, EvidenceRole.ORIGIN),
    ):
        assert rebuild_quote(text, ref) is None


def test_work_session_never_sees_a_personal_item(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    work = _capture(engine, PrivacyTier.WORK, "Synthetic work promise to ship the report")
    personal = _capture(engine, PrivacyTier.PERSONAL, "Synthetic personal promise to call home")
    work_ref, personal_ref = _ref(engine, work), _ref(engine, personal)
    work_item = _propose(engine, work_ref)
    personal_item = _propose(engine, personal_ref)
    work_session = _session(PrivacyTier.WORK)
    assert _visible(engine, work_item, (work_ref,), work_session)
    assert not _visible(engine, personal_item, (personal_ref,), work_session)
    assert _visible(engine, personal_item, (personal_ref,), _session(PrivacyTier.PERSONAL))
    assert _visible(engine, work_item, (work_ref,), _owner(engine))
    assert _visible(engine, personal_item, (personal_ref,), _owner(engine))
    assert not _visible(engine, work_item, (), work_session)


def test_item_hidden_once_any_evidence_becomes_unreadable(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "Notes", "Archive")
    notes, archive = engine.inbox.spaces()
    first = _capture(engine, PrivacyTier.WORK, "Synthetic work promise one")
    second = _capture(engine, PrivacyTier.WORK, "Synthetic work promise two")
    engine.inbox.route(first, notes.space_id, delivery_id="route.first")
    engine.inbox.route(second, notes.space_id, delivery_id="route.second")
    refs = (_ref(engine, first), _ref(engine, second, role=EvidenceRole.SUPPORTING))
    item = _propose(engine, *refs)
    scoped = _session(PrivacyTier.WORK, space_ids=frozenset({notes.space_id}))
    assert _visible(engine, item, refs, scoped)
    engine.inbox.route(second, archive.space_id, delivery_id="route.second.archive")
    assert not _visible(engine, item, refs, scoped)
    assert _visible(engine, item, refs, _session(PrivacyTier.WORK))
    assert _visible(engine, item, refs, _owner(engine))


def test_retracted_evidence_hides_item_from_clients_and_flags_it(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    kept = _capture(engine, PrivacyTier.WORK, "Synthetic kept promise")
    gone = _capture(engine, PrivacyTier.WORK, "PRIVATE_SYNTHETIC_RETRACTED promise")
    refs = (_ref(engine, kept), _ref(engine, gone, role=EvidenceRole.SUPPORTING))
    item = _propose(engine, *refs)
    session = _session(PrivacyTier.WORK)
    with closing(engine._store.connect()) as connection:
        assert not evidence_retracted(connection, refs)
    assert _visible(engine, item, refs, session)
    preview = engine.lifecycle.preview_retraction(capture_id=gone, authority=_owner(engine))
    engine.lifecycle.commit_retraction(
        str(preview["preview_id"]), str(uuid4()), authority=_owner(engine)
    )
    with closing(engine._store.connect()) as connection:
        assert evidence_retracted(connection, refs)
        assert not evidence_retracted(connection, refs[:1])
        projector = RecordProjector(engine.profile, connection, session)
        assert revision_text(projector, refs[1]) is None
        assert revision_text(projector, refs[0]) is not None
    assert not _visible(engine, item, refs, session)
    assert _visible(engine, item, refs, _owner(engine))


def test_external_provider_session_sees_only_egress_eligible_items(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    work = _capture(engine, PrivacyTier.WORK, "Synthetic shareable work promise")
    personal = _capture(engine, PrivacyTier.PERSONAL, "Synthetic personal promise")
    work_ref, personal_ref = _ref(engine, work), _ref(engine, personal)
    work_item, personal_item = _propose(engine, work_ref), _propose(engine, personal_ref)
    every = (PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL)
    assert not _visible(engine, work_item, (work_ref,), _external(engine, *every))
    OwnerEgressPolicyTasks(engine).set(
        {PrivacyTier.WORK}, operation_id="followup.egress.work", authority=_owner(engine)
    )
    assert _visible(engine, work_item, (work_ref,), _external(engine, *every))
    assert not _visible(engine, personal_item, (personal_ref,), _external(engine, *every))
    assert not _visible(engine, work_item, (work_ref,), _external(engine, PrivacyTier.PERSONAL))
    items, _cursor = engine.followups.list(ListFilter(), authority=_owner(engine))
    assert {item.item_id for item in items} >= {work_item.item_id, personal_item.item_id}


def _head_text(engine: BrainEngine, capture_id: str) -> str:
    with closing(engine._store.connect()) as connection:
        projector = RecordProjector(engine.profile, connection, _owner(engine))
        record = head_record(
            projector, EvidenceRef(capture_id, capture_id, 0, 1, "0" * 64, EvidenceRole.ORIGIN)
        )
        assert record is not None
        return record.text


def test_edited_source_with_quote_still_present_stays_visible(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = _new_source(engine, "Synthetic promise: send the deck by Friday")
    ref = _ref(engine, source.head)  # cites the first revision while it is the head
    item = _propose(engine, ref)
    original = _head_text(engine, source.head)[ref.quote_start : ref.quote_end]
    _revise_source(engine, source, "Edited intro added. Synthetic promise: send the deck by Friday")
    session = _session(PrivacyTier.WORK)
    assert _visible(engine, item, (ref,), session)
    head = _head_text(engine, source.head)
    assert locate_quote(head, original) == (
        head.index(original),
        head.index(original) + len(original),
    )
    assert locate_quote(head, original) != (ref.quote_start, ref.quote_end)


def test_edited_source_without_the_quote_stays_visible_as_source_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = _new_source(engine, "Synthetic promise: send the deck by Friday")
    ref = _ref(engine, source.head)
    item = _propose(engine, ref)
    original = _head_text(engine, source.head)[ref.quote_start : ref.quote_end]
    _revise_source(engine, source, "Edited: the deck goes out on Monday instead")
    assert _visible(engine, item, (ref,), _session(PrivacyTier.WORK))
    assert locate_quote(_head_text(engine, source.head), original) is None
    # An ambiguous quote (present twice) is also source_changed, never a guess.
    _revise_source(engine, source, "Synthetic twice. Synthetic twice.")
    assert locate_quote(_head_text(engine, source.head), "Synthetic") is None


def test_edited_source_head_in_an_unreadable_space_hides_the_item(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "Notes", "Archive")
    notes, archive = engine.inbox.spaces()
    source = _new_source(engine, "Synthetic promise: send the deck by Friday")
    engine.inbox.route(source.head, notes.space_id, delivery_id="route.edited.first")
    ref = _ref(engine, source.head)
    item = _propose(engine, ref)
    scoped = _session(PrivacyTier.WORK, space_ids=frozenset({notes.space_id}))
    assert _visible(engine, item, (ref,), scoped)
    head = _revise_source(
        engine, source, "Edited intro added. Synthetic promise: send the deck by Friday"
    )
    engine.inbox.route(head, archive.space_id, delivery_id="route.edited.second")
    assert not _visible(engine, item, (ref,), scoped)
    assert _visible(engine, item, (ref,), _session(PrivacyTier.WORK))
    assert _visible(engine, item, (ref,), _owner(engine))


def test_edited_source_head_that_changes_tier_hides_the_item(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = _new_source(engine, "Synthetic promise: send the deck by Friday")
    ref = _ref(engine, source.head)
    item = _propose(engine, ref)
    assert _visible(engine, item, (ref,), _session(PrivacyTier.WORK))
    _revise_source(engine, source, "Synthetic promise, now personal", tier=PrivacyTier.PERSONAL)
    # The head must keep the item's tier: a Work item never leaks through a Personal head,
    # and a Personal-only session never sees a Work item through it either.
    assert not _visible(engine, item, (ref,), _session(PrivacyTier.WORK))
    assert not _visible(engine, item, (ref,), _session(PrivacyTier.PERSONAL))
    assert _visible(engine, item, (ref,), _owner(engine))
