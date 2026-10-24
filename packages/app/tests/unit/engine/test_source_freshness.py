"""Capture-family freshness: one family per namespace and per Markdown space, scoped like reads."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload, historical_visibility
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.normalization import _timestamp
from open_brain_engine.engine.source_freshness import (
    BRIEF_READ_CAPABILITY,
    UNASSIGNED_SPACE,
    FamilyFreshness,
    FreshnessReport,
    SourceFreshnessTasks,
    family_id,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import agent_authority, owner_authority
from packages.app.tests.unit.engine.test_followups import (
    FROZEN,
    Clock,
    SyntheticSource,
    _connect,
    _revise_source,
)


def _engine(tmp_path: Path, clock: Clock, *spaces: str) -> BrainEngine:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=spaces)
    return BrainEngine.open(profile, clock=clock)


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return owner_authority(engine.tasks, session_id="owner-test")


def _agent(
    *grants: str,
    tiers: frozenset[PrivacyTier] = frozenset({PrivacyTier.WORK}),
    space_ids: frozenset[str] | None = None,
) -> EffectiveAuthority:
    authority = agent_authority(
        principal_id="agent-1", session_id="agent-session", grants=frozenset(grants)
    )
    return replace(authority, allowed_read_tiers=tiers, space_ids=space_ids)


def _external(engine: BrainEngine) -> EffectiveAuthority:
    return EffectiveAuthority(
        "synthetic-external-principal",
        "synthetic-external-session",
        frozenset({"search", "content-read", BRIEF_READ_CAPABILITY}),
        None,
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
        egress_mode=EgressMode.EXTERNAL_PROVIDER,
        provider_id="synthetic-provider",
        consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        brain_id=derive_brain_id(engine.profile.tenant_id),
        issuer_epoch=1,
    )


def _namespace_source(
    engine: BrainEngine, connection_id: str, text: str, *, tier: PrivacyTier = PrivacyTier.WORK
) -> SyntheticSource:
    """One logical source in connector namespace ``synthetic/<connection_id>``."""
    source = SyntheticSource(
        namespace=dict(
            connector_name="synthetic",
            connection_id=connection_id,
            resource_id=str(uuid4()),
            external_id="one",
        ),
        head="",
        revisions=0,
    )
    _revise_source(engine, source, text, tier=tier)
    return source


def _import_vault(
    engine: BrainEngine, vault: Path, notes: dict[str, str], *, tier: PrivacyTier = PrivacyTier.WORK
) -> None:
    vault.mkdir(exist_ok=True)
    for name, body in notes.items():
        (vault / name).write_text(f"# {name}\n\n{body}\n")
    engine.markdown_import.import_directory(str(vault), confirm=lambda _v: True, privacy_tier=tier)


def _markdown_heads(engine: BrainEngine) -> list[str]:
    with _connect(engine) as connection:
        rows = connection.execute(
            "SELECT s.head_capture_id FROM logical_sources s "
            "JOIN markdown_import_revisions m ON m.capture_id = s.head_capture_id "
            "ORDER BY s.source_id"
        ).fetchall()
    return [row["head_capture_id"] for row in rows]


def _sql(engine: BrainEngine, statement: str, *params: object) -> None:
    connection: sqlite3.Connection = _connect(engine)
    try:
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()


def _freshness(
    engine: BrainEngine,
    authority: EffectiveAuthority,
    *,
    space_ids: frozenset[str] | None = None,
    stale_days: int = 7,
) -> FreshnessReport:
    return SourceFreshnessTasks(engine).freshness(
        authority, space_ids=space_ids, stale_days=stale_days
    )


def _by_id(report: FreshnessReport) -> dict[str, FamilyFreshness]:
    return {family.family_id: family for family in report.families}


def test_one_family_per_namespace_and_markdown_space_and_none_for_manual(tmp_path: Path) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock, "Notes")
    (notes,) = engine.inbox.spaces()
    _namespace_source(engine, "alpha", "Synthetic alpha session one")
    _namespace_source(engine, "alpha", "Synthetic alpha session two")
    _namespace_source(engine, "beta", "Synthetic beta session")
    engine.capture.accept(
        TextPayload("Synthetic manual note"), delivery_id="manual.1", privacy_tier=PrivacyTier.WORK
    )
    _import_vault(
        engine,
        tmp_path / "vault",
        {"a.md": "Synthetic a", "b.md": "Synthetic b", "c.md": "Synthetic c"},
    )
    heads = _markdown_heads(engine)
    engine.inbox.route(heads[0], notes.space_id, delivery_id="route.a")
    engine.inbox.route(heads[1], notes.space_id, delivery_id="route.b")

    report = _freshness(engine, _owner(engine))
    assert report.stale_days == 7
    assert report.generated_at == _timestamp(FROZEN)
    assert [family.family_id for family in report.families] == sorted(
        [
            family_id("consumer_namespace", "synthetic/alpha", None),
            family_id("consumer_namespace", "synthetic/beta", None),
            family_id("markdown_import", None, notes.space_id),
            family_id("markdown_import", None, None),
        ]
    )
    families = _by_id(report)
    alpha = families["consumer:synthetic/alpha:unassigned"]
    assert (alpha.kind, alpha.space_id, alpha.capture_count, alpha.unavailable_count) == (
        "consumer_namespace",
        UNASSIGNED_SPACE,
        2,
        0,
    )
    assert families["consumer:synthetic/beta:unassigned"].capture_count == 1
    routed = families[f"markdown:{notes.space_id}"]
    assert (routed.kind, routed.space_id, routed.capture_count) == (
        "markdown_import",
        notes.space_id,
        2,
    )
    assert families["markdown:unassigned"].capture_count == 1
    assert all(family.newest_recorded_at == _timestamp(FROZEN) for family in report.families)
    assert not any(family.stale for family in report.families)


def test_same_namespace_in_two_spaces_is_two_families(tmp_path: Path) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock, "Notes", "Archive")
    notes, archive = engine.inbox.spaces()
    first = _namespace_source(engine, "alpha", "Synthetic alpha in notes")
    second = _namespace_source(engine, "alpha", "Synthetic alpha in archive")
    engine.inbox.route(first.head, notes.space_id, delivery_id="route.1")
    engine.inbox.route(second.head, archive.space_id, delivery_id="route.2")
    families = _by_id(_freshness(engine, _owner(engine)))
    assert set(families) == {
        f"consumer:synthetic/alpha:{notes.space_id}",
        f"consumer:synthetic/alpha:{archive.space_id}",
    }
    assert families[f"consumer:synthetic/alpha:{notes.space_id}"].space_id == notes.space_id


def test_newest_recorded_at_is_the_latest_head_and_moves_with_revisions(tmp_path: Path) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock)
    older = _namespace_source(engine, "alpha", "Synthetic alpha first")
    clock.now = FROZEN + timedelta(days=2)
    _namespace_source(engine, "alpha", "Synthetic alpha second")
    family = _by_id(_freshness(engine, _owner(engine)))["consumer:synthetic/alpha:unassigned"]
    assert family.newest_recorded_at == _timestamp(FROZEN + timedelta(days=2))
    clock.now = FROZEN + timedelta(days=5)
    _revise_source(engine, older, "Synthetic alpha first, revised")
    family = _by_id(_freshness(engine, _owner(engine)))["consumer:synthetic/alpha:unassigned"]
    assert family.newest_recorded_at == _timestamp(FROZEN + timedelta(days=5))
    assert family.capture_count == 2


def test_stale_cutoff_under_a_frozen_clock(tmp_path: Path) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock)
    _namespace_source(engine, "alpha", "Synthetic alpha")
    alpha = "consumer:synthetic/alpha:unassigned"
    clock.now = FROZEN + timedelta(days=7)
    assert _by_id(_freshness(engine, _owner(engine), stale_days=7))[alpha].stale is False
    clock.now = FROZEN + timedelta(days=7, seconds=1)
    report = _freshness(engine, _owner(engine), stale_days=7)
    assert _by_id(report)[alpha].stale is True
    assert report.generated_at == _timestamp(clock.now)
    assert _by_id(_freshness(engine, _owner(engine), stale_days=8))[alpha].stale is False
    assert _by_id(_freshness(engine, _owner(engine), stale_days=1))[alpha].stale is True


def test_counts_missing_and_inaccessible_and_skips_retired(tmp_path: Path) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock)
    vault = tmp_path / "vault"
    _import_vault(
        engine, vault, {"a.md": "Synthetic a", "b.md": "Synthetic b", "c.md": "Synthetic c"}
    )
    assert len(_markdown_heads(engine)) == 3
    (vault / "a.md").unlink()
    engine.markdown_import.import_directory(
        str(vault), confirm=lambda _v: True, privacy_tier=PrivacyTier.WORK
    )
    with _connect(engine) as connection:
        missing = connection.execute(
            "SELECT count(*) FROM logical_sources WHERE availability='missing'"
        ).fetchone()[0]
    assert missing == 1  # the real Markdown re-import path marks the deleted note missing
    # No engine path writes 'inaccessible' yet; set it on one still-available imported note.
    _sql(
        engine,
        "UPDATE logical_sources SET availability='inaccessible' WHERE source_id = ("
        "SELECT s.source_id FROM logical_sources s "
        "JOIN markdown_import_revisions m ON m.capture_id = s.head_capture_id "
        "WHERE s.availability='available' ORDER BY s.source_id LIMIT 1)",
    )
    retired = _namespace_source(engine, "gone", "Synthetic retired session")
    kept = _namespace_source(engine, "kept", "Synthetic kept session")
    # Same row state SourceLifecycleTasks.withdraw writes (source_lifecycle.py:142).
    _sql(
        engine,
        "UPDATE logical_sources SET lifecycle='retired', availability='missing' "
        "WHERE head_capture_id=?",
        retired.head,
    )
    report = _freshness(engine, _owner(engine))
    families = _by_id(report)
    assert set(families) == {"markdown:unassigned", "consumer:synthetic/kept:unassigned"}
    markdown = families["markdown:unassigned"]
    assert (markdown.capture_count, markdown.unavailable_count) == (3, 2)
    assert families["consumer:synthetic/kept:unassigned"].unavailable_count == 0
    assert kept.head  # fixture sanity


def test_tier_space_and_egress_rules_match_record_projector(tmp_path: Path) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock, "Notes", "Archive")
    notes, archive = engine.inbox.spaces()
    work = _namespace_source(engine, "work", "Synthetic work session")
    personal = _namespace_source(
        engine, "home", "Synthetic personal session", tier=PrivacyTier.PERSONAL
    )
    engine.inbox.route(work.head, notes.space_id, delivery_id="route.work")
    engine.inbox.route(personal.head, archive.space_id, delivery_id="route.home")
    _import_vault(engine, tmp_path / "vault", {"a.md": "Synthetic a"}, tier=PrivacyTier.PERSONAL)

    everything = set(_by_id(_freshness(engine, _owner(engine))))
    assert everything == {
        f"consumer:synthetic/work:{notes.space_id}",
        f"consumer:synthetic/home:{archive.space_id}",
        "markdown:unassigned",
    }
    work_only = _agent(BRIEF_READ_CAPABILITY)
    assert set(_by_id(_freshness(engine, work_only))) == {
        f"consumer:synthetic/work:{notes.space_id}"
    }
    both_tiers = _agent(
        BRIEF_READ_CAPABILITY, tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL})
    )
    assert set(_by_id(_freshness(engine, both_tiers))) == everything
    archive_only = _agent(
        BRIEF_READ_CAPABILITY,
        tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        space_ids=frozenset({archive.space_id}),
    )
    assert set(_by_id(_freshness(engine, archive_only))) == {
        f"consumer:synthetic/home:{archive.space_id}"
    }
    # An explicit selection narrows the owner too, and never matches the unassigned family.
    selected = _freshness(engine, _owner(engine), space_ids=frozenset({notes.space_id}))
    assert set(_by_id(selected)) == {f"consumer:synthetic/work:{notes.space_id}"}
    unknown_space = "space_" + "0" * 8 + "-0000-0000-0000-" + "0" * 12
    assert _freshness(engine, _owner(engine), space_ids=frozenset({unknown_space})).families == ()
    # A selection outside the authority's spaces yields nothing rather than widening scope.
    assert _freshness(engine, archive_only, space_ids=frozenset({notes.space_id})).families == ()
    # Local captures carry external_egress=False, so an external-provider session sees no family.
    assert _freshness(engine, _external(engine)).families == ()
    # An unavailable head skips only sharing eligibility (which requires 'available'); the tier
    # rule still hides the Personal note from a Work-only session.
    _sql(
        engine,
        "UPDATE logical_sources SET availability='missing' WHERE head_capture_id = ("
        "SELECT capture_id FROM markdown_import_revisions LIMIT 1)",
    )
    assert set(_by_id(_freshness(engine, work_only))) == {
        f"consumer:synthetic/work:{notes.space_id}"
    }
    assert _by_id(_freshness(engine, both_tiers))["markdown:unassigned"].unavailable_count == 1
    assert _freshness(engine, _external(engine)).families == ()


def test_requires_brief_read_and_valid_arguments(tmp_path: Path) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock)
    _namespace_source(engine, "alpha", "Synthetic alpha")
    tasks = SourceFreshnessTasks(engine)
    with pytest.raises(T03Error) as refused:
        tasks.freshness(_agent("search", "content-read"), space_ids=None, stale_days=7)
    assert refused.value.code == "unsupported_capability"
    # The capability check runs before argument validation.
    with pytest.raises(T03Error) as refused:
        tasks.freshness(_agent("search"), space_ids=None, stale_days=0)
    assert refused.value.code == "unsupported_capability"
    assert tasks.freshness(_owner(engine), space_ids=None, stale_days=365).stale_days == 365
    granted = _agent(BRIEF_READ_CAPABILITY)
    assert tasks.freshness(granted, space_ids=None, stale_days=1).stale_days == 1
    bad_days: tuple[object, ...] = (0, 366, True, "7", 7.0, None)
    for stale_days in bad_days:
        with pytest.raises(T03Error) as invalid:
            tasks.freshness(_owner(engine), space_ids=None, stale_days=stale_days)  # type: ignore[arg-type]
        assert invalid.value.code == "invalid_arguments"
    bad_spaces: tuple[object, ...] = (
        {"space_x"},
        ["space_x"],
        frozenset({""}),
        frozenset({7}),
        "space_x",
    )
    for space_ids in bad_spaces:
        with pytest.raises(T03Error) as invalid:
            tasks.freshness(_owner(engine), space_ids=space_ids, stale_days=7)  # type: ignore[arg-type]
        assert invalid.value.code == "invalid_arguments"


def test_report_is_frozen_and_sorted(tmp_path: Path) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock)
    _namespace_source(engine, "zeta", "Synthetic zeta")
    _namespace_source(engine, "alpha", "Synthetic alpha")
    report = _freshness(engine, _owner(engine))
    assert [family.family_id for family in report.families] == [
        "consumer:synthetic/alpha:unassigned",
        "consumer:synthetic/zeta:unassigned",
    ]
    with pytest.raises(AttributeError):
        report.families = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        report.families[0].stale = True  # type: ignore[misc]


def _copy_marked_source(engine: BrainEngine, connection_id: str, text: str) -> str:
    """A namespace source whose capture carries a sharing-copy custody signal (sharing.py).

    The intake refuses reserved sharing-copy references, so the marker is written by SQL on an
    ordinary head, the same way the other row-state fixtures here are written.
    """
    head = _namespace_source(engine, connection_id, text).head
    _sql(
        engine,
        "UPDATE captures SET source_reference=? WHERE capture_id=?",
        "urn:open-brain:sharing-copy:v1:synthetic",
        head,
    )
    return head


def _mark_missing(engine: BrainEngine, head: str) -> None:
    _sql(engine, "UPDATE logical_sources SET availability='missing' WHERE head_capture_id=?", head)


def test_unavailable_managed_heads_stay_hidden_from_non_owner_sessions(tmp_path: Path) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock)
    copy_head = _copy_marked_source(engine, "copy", "Synthetic shared copy")
    original = _namespace_source(engine, "original", "Synthetic managed original")
    plain = _namespace_source(engine, "plain", "Synthetic ordinary session")
    # A managed-source delivery receipt for the head makes it a managed original (sharing.py).
    _sql(
        engine,
        "INSERT INTO managed_source_deliveries(delivery_id,envelope_sha256,envelope_bytes,"
        "source_id,destination_brain_id,issuer_epoch,expected_head,expected_lifecycle_version,"
        "source_delivery_id,receipt_json) SELECT 'synthetic.managed.1',?,?,source_id,?,1,NULL,0,"
        "'synthetic.managed.source.1',? FROM logical_sources WHERE head_capture_id=?",
        "0" * 64,
        b"synthetic",
        derive_brain_id(engine.profile.tenant_id),
        json.dumps({"source_receipt": {"capture_id": original.head}}),
        original.head,
    )
    work_only = _agent(BRIEF_READ_CAPABILITY)
    available = set(_by_id(_freshness(engine, work_only)))
    assert "consumer:synthetic/copy:unassigned" not in available  # sharing eligibility denies it
    for head in (copy_head, original.head, plain.head):
        _mark_missing(engine, head)
    # Unavailable managed heads fail closed for a non-owner; an ordinary one is still counted.
    hidden = _by_id(_freshness(engine, work_only))
    assert set(hidden) == {"consumer:synthetic/plain:unassigned"}
    assert hidden["consumer:synthetic/plain:unassigned"].unavailable_count == 1
    # The owner counts every unavailable head.
    owned = _by_id(_freshness(engine, _owner(engine)))
    assert set(owned) == {
        "consumer:synthetic/copy:unassigned",
        "consumer:synthetic/original:unassigned",
        "consumer:synthetic/plain:unassigned",
    }
    assert all(family.unavailable_count == 1 for family in owned.values())


def test_unavailable_heads_respect_the_historical_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(FROZEN)
    engine = _engine(tmp_path, clock)
    plain = _namespace_source(engine, "plain", "Synthetic ordinary session")
    _mark_missing(engine, plain.head)
    work_only = _agent(BRIEF_READ_CAPABILITY)
    assert set(_by_id(_freshness(engine, work_only))) == {"consumer:synthetic/plain:unassigned"}
    calls: list[str] = []

    def deny(_connection: object, _profile: object, capture_id: str, **_kwargs: object) -> bool:
        calls.append(capture_id)
        return False

    monkeypatch.setattr(historical_visibility, "historical_capture_visibility", deny)
    assert _freshness(engine, work_only).families == ()
    assert calls == [plain.head]
    # Owner current reads never consult the non-owner historical fence.
    assert set(_by_id(_freshness(engine, _owner(engine)))) == {
        "consumer:synthetic/plain:unassigned"
    }
    assert calls == [plain.head]
