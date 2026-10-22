"""Schema22 owner egress policy: migration from schema21, monotone history and fencing."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    LocalEngineContext,
    StateSchemaUnavailableError,
    TextPayload,
    local_schema,
)
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.owner_egress_policy import (
    SHAREABLE_TIERS as SHAREABLE,
)
from open_brain_engine.engine.owner_egress_policy import (
    OwnerEgressPolicyError,
    OwnerEgressPolicyTasks,
    active_egress_policy,
    history_rows,
    read_owner_egress_policy,
)
from open_brain_engine.engine.owner_egress_policy_schema import (
    OWNER_EGRESS_POLICY_MIGRATION,
    migrate_owner_egress_policy,
)
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.storage.migrations import SchemaError

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import agent_authority, owner_authority

WORK = frozenset({PrivacyTier.WORK})


def _clock() -> datetime:
    return datetime(2026, 10, 22, tzinfo=UTC)


def _connect(profile: LocalEngineContext) -> sqlite3.Connection:
    connection = sqlite3.connect(profile.root / local_schema.PHASE1_STATE_DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def _schema21(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalEngineContext:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 21)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:21])
        engine = BrainEngine.open(profile)
        engine.capture.accept(TextPayload("Synthetic schema21 evidence"), delivery_id="prior.21")
    return profile


def _engine(tmp_path: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return owner_authority(engine.tasks, session_id="owner-test")


def test_fresh_schema22_starts_with_no_shareable_tiers(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert local_schema.inspect_phase1_state(engine.profile) == local_schema.SchemaState(
        "current", 22
    )
    assert LOCAL_MIGRATIONS[21] == OWNER_EGRESS_POLICY_MIGRATION
    with _connect(engine.profile) as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT * FROM runtime_compatibility"
        )] == [(1, 16, 22)]
        policy = read_owner_egress_policy(connection)
        assert active_egress_policy(connection) is None
        assert history_rows(connection) == []
    assert (policy.version, policy.tiers, policy.recorded_at, policy.operation_id) == (
        0,
        frozenset(),
        None,
        None,
    )
    assert policy.status() == {
        "schema": "ob.owner-egress-policy-status.v1",
        "version": 0,
        "tiers": [],
        "recorded_at": None,
        "operation_id": None,
    }


def test_set_is_monotone_append_only_and_idempotent_on_operation(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = OwnerEgressPolicyTasks(engine)
    owner = _owner(engine)
    first = tasks.set({PrivacyTier.WORK}, operation_id="op-1", authority=owner)
    assert (first.version, first.tiers, first.operation_id) == (1, WORK, "op-1")
    assert tasks.set({PrivacyTier.WORK}, operation_id="op-1", authority=owner) == first
    with pytest.raises(OwnerEgressPolicyError, match="operation_conflict"):
        tasks.set({PrivacyTier.PUBLIC}, operation_id="op-1", authority=owner)
    every = frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL})
    second = tasks.set(every, operation_id="op-2", authority=owner)
    third = tasks.set(set(), operation_id="op-3", authority=owner)
    assert (second.version, second.tiers) == (2, every)
    assert (third.version, third.tiers) == (3, frozenset())
    assert tasks.status(authority=owner) == third
    with _connect(engine.profile) as connection:
        assert active_egress_policy(connection) is None
        assert [(row["version"], row["tiers"], row["operation_id"]) for row in history_rows(
            connection
        )] == [
            (1, ["work"], "op-1"),
            (2, ["personal", "public", "work"], "op-2"),
            (3, [], "op-3"),
        ]
        assert all(str(row["recorded_at"]).endswith("Z") for row in history_rows(connection))


@pytest.mark.parametrize("tier", ["secret", "unknown", "WORK", "", None, 3])
def test_set_rejects_non_shareable_tiers_before_mutation(tmp_path: Path, tier: object) -> None:
    engine = _engine(tmp_path)
    tasks = OwnerEgressPolicyTasks(engine)
    with pytest.raises(OwnerEgressPolicyError, match="invalid_tier"):
        tasks.set({PrivacyTier.WORK, tier}, operation_id="op-x", authority=_owner(engine))
    assert tasks.status(authority=_owner(engine)).version == 0


@pytest.mark.parametrize("operation_id", ["", None, "x" * 129, 7])
def test_set_rejects_invalid_operation_ids(tmp_path: Path, operation_id: object) -> None:
    engine = _engine(tmp_path)
    with pytest.raises(OwnerEgressPolicyError, match="invalid_operation_id"):
        OwnerEgressPolicyTasks(engine).set(
            WORK,
            operation_id=operation_id,  # type: ignore[arg-type]
            authority=_owner(engine),
        )


def test_non_owner_authority_is_refused(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    tasks = OwnerEgressPolicyTasks(engine)
    agent = agent_authority(
        principal_id="synthetic-agent", session_id="agent", grants=frozenset({"search"})
    )
    with pytest.raises(OwnerEgressPolicyError, match="owner_required"):
        tasks.set(WORK, operation_id="op-1", authority=agent)
    with pytest.raises(OwnerEgressPolicyError, match="owner_required"):
        tasks.status(authority=agent)
    assert tasks.status(authority=_owner(engine)).version == 0


def test_policy_and_history_reject_direct_rewrites(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    OwnerEgressPolicyTasks(engine).set(WORK, operation_id="op-1", authority=_owner(engine))
    with _connect(engine.profile) as connection:
        for statement in (
            "UPDATE owner_egress_policy_history SET share_public=1",
            "DELETE FROM owner_egress_policy_history",
            "DELETE FROM owner_egress_policy",
            "UPDATE owner_egress_policy SET share_public=1",
            "UPDATE owner_egress_policy SET version=0",
            "UPDATE owner_egress_policy SET share_secret=1",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(statement)


def test_classifier_refuses_policy_that_disagrees_with_history(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    OwnerEgressPolicyTasks(engine).set(WORK, operation_id="op-1", authority=_owner(engine))
    with _connect(engine.profile) as connection:
        guard = next(
            statement
            for statement in OWNER_EGRESS_POLICY_MIGRATION.statements
            if "owner_egress_policy_update_guard" in statement
        )
        connection.execute("DROP TRIGGER owner_egress_policy_update_guard")
        connection.execute("UPDATE owner_egress_policy SET share_personal=1")
        connection.execute(guard)
        assert local_schema.classify_local_schema(connection) == local_schema.SchemaState(
            "invalid", 22
        )


def test_schema21_migrates_without_sharing_and_preserves_predecessor_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _schema21(tmp_path, monkeypatch)
    with _connect(profile) as connection:
        migrations = connection.execute("SELECT * FROM schema_migrations").fetchall()
        captures = connection.execute("SELECT * FROM captures").fetchall()
        projections = connection.execute(
            "SELECT * FROM source_revision_privacy ORDER BY capture_id"
        ).fetchall()
        search = connection.execute(
            "SELECT result_id,effective_tier,effective_cloud,effective_external_egress "
            "FROM search_documents ORDER BY result_id"
        ).fetchall()
    with pytest.raises((SchemaError, StateSchemaUnavailableError)):
        BrainEngine.open(profile)
    coordinate_local_migration(profile, clock=_clock)
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 22)
    with _connect(profile) as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT * FROM schema_migrations"
        ).fetchall()[:21]] == [tuple(row) for row in migrations]
        assert [tuple(row) for row in connection.execute("SELECT * FROM captures")] == [
            tuple(row) for row in captures
        ]
        assert [tuple(row) for row in connection.execute(
            "SELECT * FROM source_revision_privacy ORDER BY capture_id"
        )] == [tuple(row) for row in projections]
        assert [tuple(row) for row in connection.execute(
            "SELECT result_id,effective_tier,effective_cloud,effective_external_egress "
            "FROM search_documents ORDER BY result_id"
        )] == [tuple(row) for row in search]
        assert [tuple(row) for row in connection.execute(
            "SELECT * FROM owner_egress_policy"
        )] == [(1, 0, 0, 0, 0, None, None)]
        assert connection.execute("SELECT count(*) FROM owner_egress_policy_history").fetchone()[
            0
        ] == 0
        assert [tuple(row) for row in connection.execute(
            "SELECT * FROM runtime_compatibility"
        )] == [(1, 16, 22)]
    coordinate_local_migration(profile, clock=_clock)
    engine = BrainEngine.open(profile)
    assert OwnerEgressPolicyTasks(engine).set(
        WORK, operation_id="op-1", authority=_owner(engine)
    ).version == 1


@pytest.mark.parametrize("opener", ["engine", "read_only"])
def test_schema21_engine_refuses_schema22_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    profile = _schema21(tmp_path, monkeypatch)
    coordinate_local_migration(profile, clock=_clock)
    database = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = database.read_bytes()
    with monkeypatch.context() as previous:
        previous.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 21)
        previous.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:21])
        with _connect(profile) as connection:
            assert local_schema.classify_local_schema(connection) == local_schema.SchemaState(
                "newer", 22
            )
        with pytest.raises((SchemaError, StateSchemaUnavailableError), match="schema is newer"):
            if opener == "engine":
                BrainEngine.open(profile)
            else:
                local_schema.open_local_database_read_only(profile)
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "stage,version",
    [
        ("owner_egress_policy_preflight", 21),
        ("owner_egress_policy_before_commit", 21),
        ("owner_egress_policy_after_commit", 22),
    ],
)
def test_interrupted_cutover_is_atomic_and_rerun_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, version: int
) -> None:
    profile = _schema21(tmp_path, monkeypatch)

    def interrupt(actual: str) -> None:
        if actual == stage:
            raise RuntimeError("synthetic egress policy interruption")

    expected = local_schema.LocalSchemaUpgradeCommittedError if version == 22 else RuntimeError
    with exclusive_runtime_admission(profile) as admission, pytest.raises(expected):
        migrate_owner_egress_policy(
            profile, admission=admission, clock=_clock, checkpoint=interrupt
        )
    assert local_schema.inspect_phase1_state(profile).version == version
    coordinate_local_migration(profile, clock=_clock)
    coordinate_local_migration(profile, clock=_clock)
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 22)
    with _connect(profile) as connection:
        assert connection.execute("SELECT count(*) FROM owner_egress_policy").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=22"
        ).fetchone()[0] == 1


TIERS = (
    PrivacyTier.PUBLIC,
    PrivacyTier.WORK,
    PrivacyTier.PERSONAL,
    PrivacyTier.SECRET,
    PrivacyTier.UNKNOWN,
)


def _captured(engine: BrainEngine) -> dict[PrivacyTier, str]:
    return {
        tier: engine.capture.accept(
            TextPayload(f"Synthetic {tier.value} egress evidence"),
            delivery_id=f"egress.{tier.value}",
            privacy_tier=tier,
        ).capture_id
        for tier in TIERS
    }


def _projections(
    engine: BrainEngine, captures: dict[PrivacyTier, str]
) -> dict[PrivacyTier, tuple[object, ...]]:
    with _connect(engine.profile) as connection:
        result: dict[PrivacyTier, tuple[object, ...]] = {}
        for tier, capture_id in captures.items():
            search = connection.execute(
                "SELECT effective_tier,effective_cloud,effective_external_egress "
                "FROM search_documents WHERE capture_id=? AND record_type='source'",
                (capture_id,),
            ).fetchone()
            revision = connection.execute(
                "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
            result[tier] = (tuple(search), revision[0])
        return result


def _egress(
    projections: dict[PrivacyTier, tuple[object, ...]],
) -> dict[PrivacyTier, tuple[int, int, bool, bool, list[str]]]:
    import json

    shaped: dict[PrivacyTier, tuple[int, int, bool, bool, list[str]]] = {}
    for tier, (search, revision) in projections.items():
        effective = json.loads(str(revision))
        _tier, cloud, egress = cast(tuple[str, int, int], search)
        shaped[tier] = (
            cloud,
            egress,
            effective["authority"]["cloud"],
            effective["authority"]["external_egress"],
            effective["confirmation_refs"],
        )
    return shaped


def _retained(engine: BrainEngine) -> list[tuple[object, ...]]:
    with _connect(engine.profile) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT capture_id,privacy_json FROM captures ORDER BY capture_id"
            )
        ]


def test_policy_off_projections_equal_the_retained_evidence(tmp_path: Path) -> None:
    import json

    from open_brain_engine.engine.privacy_projection import (
        effective_privacy_json,
        project_retained_privacy_evidence,
    )

    engine = _engine(tmp_path)
    captures = _captured(engine)
    before = _projections(engine, captures)
    with _connect(engine.profile) as connection:
        for tier, capture_id in captures.items():
            raw = connection.execute(
                "SELECT privacy_json FROM captures WHERE capture_id=?", (capture_id,)
            ).fetchone()[0]
            assert before[tier][1] == effective_privacy_json(
                project_retained_privacy_evidence(raw)
            )
            assert json.loads(str(before[tier][1]))["authority"]["external_egress"] is False
    OwnerEgressPolicyTasks(engine).set(set(), operation_id="op-off", authority=_owner(engine))
    assert _projections(engine, captures) == before


def test_work_policy_exposes_only_work_and_never_grants_cloud(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    captures = _captured(engine)
    retained = _retained(engine)
    OwnerEgressPolicyTasks(engine).set(WORK, operation_id="op-work", authority=_owner(engine))
    shaped = _egress(_projections(engine, captures))
    assert shaped[PrivacyTier.WORK] == (0, 1, False, True, [])
    for tier in (PrivacyTier.PUBLIC, PrivacyTier.PERSONAL, PrivacyTier.SECRET, PrivacyTier.UNKNOWN):
        assert shaped[tier] == (0, 0, False, False, []), tier
    assert _retained(engine) == retained
    later = engine.capture.accept(
        TextPayload("Synthetic later work capture"),
        delivery_id="egress.work.later",
        privacy_tier=PrivacyTier.WORK,
    ).capture_id
    assert _egress(_projections(engine, {PrivacyTier.WORK: later}))[PrivacyTier.WORK][:2] == (0, 1)


def test_removing_tiers_retracts_egress_and_personal_carries_confirmation(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    captures = _captured(engine)
    before = _projections(engine, captures)
    retained = _retained(engine)
    tasks, owner = OwnerEgressPolicyTasks(engine), _owner(engine)
    every = {PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL}
    assert tasks.set(every, operation_id="op-every", authority=owner).version == 1
    shaped = _egress(_projections(engine, captures))
    assert shaped[PrivacyTier.PUBLIC] == (0, 1, False, True, [])
    assert shaped[PrivacyTier.WORK] == (0, 1, False, True, [])
    assert shaped[PrivacyTier.PERSONAL] == (0, 1, False, True, ["owner-egress-policy:1"])
    assert shaped[PrivacyTier.SECRET] == (0, 0, False, False, [])
    assert shaped[PrivacyTier.UNKNOWN] == (0, 0, False, False, [])
    tasks.set({PrivacyTier.PUBLIC}, operation_id="op-public", authority=owner)
    shaped = _egress(_projections(engine, captures))
    assert shaped[PrivacyTier.PUBLIC] == (0, 1, False, True, [])
    for tier in (PrivacyTier.WORK, PrivacyTier.PERSONAL, PrivacyTier.SECRET, PrivacyTier.UNKNOWN):
        assert shaped[tier] == (0, 0, False, False, []), tier
    tasks.set(set(), operation_id="op-none", authority=owner)
    assert _projections(engine, captures) == before
    assert _retained(engine) == retained


def test_personal_overlay_decision_is_confirmed_and_local_tiers_are_refused() -> None:
    from open_brain_engine.core.models import PrivacyReason
    from open_brain_engine.engine.owner_egress_policy import OwnerEgressPolicy, overlay_decision

    policy = OwnerEgressPolicy(
        4, frozenset({PrivacyTier.PERSONAL, PrivacyTier.WORK}), "2026-10-22T00:00:00Z", "op"
    )
    personal = overlay_decision(PrivacyTier.PERSONAL, policy)
    assert personal is not None
    assert personal.reason is PrivacyReason.PERSONAL_CONFIRMED
    assert personal.confirmation_ref == "owner-egress-policy:4"
    assert (personal.authority.cloud, personal.authority.external_egress) == (False, True)
    work = overlay_decision(PrivacyTier.WORK, policy)
    assert work is not None and work.reason is PrivacyReason.POLICY_WORK
    assert work.authority.cloud is False
    for tier in (PrivacyTier.PUBLIC, PrivacyTier.SECRET, PrivacyTier.UNKNOWN):
        assert overlay_decision(tier, policy) is None


def test_mixed_member_overlay_requires_every_member_tier_to_be_shared(tmp_path: Path) -> None:
    from open_brain_engine.engine.owner_egress_policy import OwnerEgressPolicy
    from open_brain_engine.engine.privacy_projection import project_retained_privacy_evidence

    engine = _engine(tmp_path)
    captures = _captured(engine)
    with _connect(engine.profile) as connection:
        raw = {
            tier: connection.execute(
                "SELECT privacy_json FROM captures WHERE capture_id=?", (capture_id,)
            ).fetchone()[0]
            for tier, capture_id in captures.items()
        }

    def egress(members: tuple[PrivacyTier, ...], *shared: PrivacyTier) -> bool:
        policy = OwnerEgressPolicy(1, frozenset(shared), "2026-10-22T00:00:00Z", "op")
        evidence = project_retained_privacy_evidence(
            tuple(raw[tier] for tier in members), egress_policy=policy
        )
        assert evidence.authority.cloud is False
        return evidence.authority.external_egress

    mixed = (PrivacyTier.WORK, PrivacyTier.PERSONAL)
    assert not egress(mixed, PrivacyTier.PERSONAL)
    assert not egress(mixed, PrivacyTier.WORK)
    assert egress(mixed, PrivacyTier.WORK, PrivacyTier.PERSONAL)
    assert not egress((PrivacyTier.PUBLIC, PrivacyTier.WORK), PrivacyTier.WORK)
    assert egress((PrivacyTier.PUBLIC, PrivacyTier.WORK), PrivacyTier.PUBLIC, PrivacyTier.WORK)
    assert not egress((PrivacyTier.WORK, PrivacyTier.UNKNOWN), *SHAREABLE)
    assert not egress((PrivacyTier.PERSONAL, PrivacyTier.SECRET), *SHAREABLE)


def test_overlay_never_applies_to_invalid_evidence() -> None:
    from open_brain_engine.engine.owner_egress_policy import OwnerEgressPolicy
    from open_brain_engine.engine.privacy_projection import project_retained_privacy_evidence

    policy = OwnerEgressPolicy(1, frozenset(SHAREABLE), "2026-10-22T00:00:00Z", "op")
    for raw in (None, "not json", 7):
        base = project_retained_privacy_evidence(raw)
        assert project_retained_privacy_evidence(raw, egress_policy=policy) == base
        assert base.authority.external_egress is False


def test_audit_accepts_overlay_and_rejects_unpermitted_egress(tmp_path: Path) -> None:
    from open_brain_engine.engine.privacy_repairs import audit_privacy_repair_ledger
    from open_brain_engine.engine.t03_contracts import T03Error

    engine = _engine(tmp_path)
    captures = _captured(engine)
    OwnerEgressPolicyTasks(engine).set(WORK, operation_id="op-work", authority=_owner(engine))
    with _connect(engine.profile) as connection:
        audit_privacy_repair_ledger(connection)
        connection.execute(
            "UPDATE search_documents SET effective_external_egress=1 WHERE capture_id=?",
            (captures[PrivacyTier.PERSONAL],),
        )
        with pytest.raises(T03Error):
            audit_privacy_repair_ledger(connection)
        connection.rollback()


def _external(engine: BrainEngine, *consented: PrivacyTier) -> EffectiveAuthority:
    from open_brain_engine.core.access_contracts import derive_brain_id
    from open_brain_engine.engine.consent_contracts import EgressMode

    return EffectiveAuthority(
        "synthetic-external-principal",
        "synthetic-external-session",
        frozenset({"search", "content-read"}),
        None,
        allowed_read_tiers=frozenset(consented),
        egress_mode=EgressMode.EXTERNAL_PROVIDER,
        provider_id="synthetic-provider",
        consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        brain_id=derive_brain_id(engine.profile.tenant_id),
        issuer_epoch=1,
    )


def test_external_provider_reads_work_only_when_policy_and_consent_both_allow(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine.t03_contracts import (
        RecordReadRequest,
        SearchPageRequest,
        T03Error,
    )

    engine = _engine(tmp_path)
    work, unknown = (
        engine.capture.accept(
            TextPayload("shared egress quasar evidence"),
            delivery_id=f"external.{tier.value}",
            privacy_tier=tier,
        ).capture_id
        for tier in (PrivacyTier.WORK, PrivacyTier.UNKNOWN)
    )
    request = SearchPageRequest(query="shared egress quasar evidence", limit=10)

    def found(*consented: PrivacyTier) -> set[str]:
        page = engine.retrieval.search_page(request, authority=_external(engine, *consented))
        rows = cast(list[dict[str, object]], page.to_wire()["results"])
        return {str(row["record_id"]) for row in rows}

    def readable(capture_id: str, *consented: PrivacyTier) -> bool:
        try:
            engine.retrieval.read_record(
                RecordReadRequest(record_id=capture_id, expected_revision_id=capture_id),
                authority=_external(engine, *consented),
            )
        except T03Error:
            return False
        return True

    tasks, owner = OwnerEgressPolicyTasks(engine), _owner(engine)
    every_consent = (PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL)
    assert found(*every_consent) == set()
    assert not readable(work, PrivacyTier.WORK)
    tasks.set({PrivacyTier.PERSONAL}, operation_id="op-personal", authority=owner)
    assert found(*every_consent) == set()
    tasks.set(WORK, operation_id="op-work", authority=owner)
    assert found(PrivacyTier.WORK) == {work}
    assert found(*every_consent) == {work}
    assert readable(work, PrivacyTier.WORK)
    assert not readable(unknown, *every_consent)
    assert found(PrivacyTier.PERSONAL) == set()
    assert not readable(work, PrivacyTier.PERSONAL)
    tasks.set(set(every_consent), operation_id="op-every", authority=owner)
    assert found(*every_consent) == {work}
    assert not readable(unknown, *every_consent)
    tasks.set(set(), operation_id="op-none", authority=owner)
    assert found(*every_consent) == set()
    assert not readable(work, PrivacyTier.WORK)
