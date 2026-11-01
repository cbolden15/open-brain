"""Engine record export: head captures an external-provider authority may read, as plain records."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine import (
    BrainEngine,
    CaptureReceipt,
    CaptureSubmission,
    PublicJobCaptureContext,
    SourceInspectRequest,
    SourceWithdrawRequest,
    TextPayload,
)
from open_brain_engine.engine.record_export import (
    TRUNCATION_MARKER,
    ExportedRecord,
    RecordExportTasks,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import owner_authority
from packages.app.tests.unit.engine.test_paging import external_authority

START = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _engine(tmp_path: Path, clock: MutableClock | None = None) -> BrainEngine:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    return BrainEngine.open(profile, clock=clock or MutableClock(START))


def _capture(
    engine: BrainEngine,
    *,
    delivery_id: str,
    text: str,
    external_egress: bool = True,
) -> str:
    """A third-party work capture with an explicit per-record ``external_egress`` decision."""
    actor_id = "actor_00000000-0000-4000-8000-000000000701"
    context = PublicJobCaptureContext.create(
        profile=engine.profile,
        actor_id=actor_id,
        role_claim={
            "actor_id": actor_id,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000702",
            "role_id": "role_00000000-0000-4000-8000-000000000703",
            "tenant_id": engine.profile.tenant_id,
        },
    )
    source_reference = f"https://example.test/private-reference/{delivery_id}"
    receipt = engine.capture.submit(
        CaptureSubmission.for_public_job(
            context=context,
            payload=TextPayload(text),
            delivery_id=delivery_id,
            source_origin=ContentOrigin.THIRD_PARTY,
            source_reference=source_reference,
            provenance=Provenance.create(
                source_ref=source_reference,
                content_origin=ContentOrigin.THIRD_PARTY,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            ),
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.WORK,
                reason=PrivacyReason.POLICY_WORK,
                policy_version="privacy-v1",
                authority=Authority(cloud=external_egress, external_egress=external_egress),
            ),
        )
    )
    assert isinstance(receipt, CaptureReceipt)
    return receipt.capture_id


def _stored_tiers(engine: BrainEngine) -> dict[str, str]:
    connection = sqlite3.connect(engine.profile.root / ".open-brain/state/phase1.sqlite3")
    try:
        return {
            capture_id: json.loads(raw)["tier"]
            for capture_id, raw in connection.execute(
                "SELECT capture_id, effective_privacy_json FROM source_revision_privacy"
            )
        }
    finally:
        connection.close()


def _authority(engine: BrainEngine, *tiers: PrivacyTier) -> EffectiveAuthority:
    base = external_authority(engine)
    return replace(
        base, allowed_read_tiers=frozenset(tiers or (PrivacyTier.PUBLIC, PrivacyTier.WORK))
    )


def _export(
    engine: BrainEngine,
    authority: EffectiveAuthority,
    *,
    since: datetime = START - timedelta(days=1),
    until: datetime = START + timedelta(days=30),
    limit: int = 100,
    max_bytes_per_record: int = 4096,
) -> tuple[ExportedRecord, ...]:
    tasks: RecordExportTasks = engine.tasks.record_export
    return tasks.export(
        authority,
        since=since,
        until=until,
        limit=limit,
        max_bytes_per_record=max_bytes_per_record,
    )


def test_engine_exposes_record_export_tasks(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert isinstance(engine.tasks.record_export, RecordExportTasks)
    assert engine.tasks.record_export is engine.record_export


def test_export_emits_only_the_authority_tiers(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    owner = owner_authority(engine.tasks, session_id="owner-test")
    emitted = {
        tier: engine.capture.accept(
            TextPayload(f"synthetic {tier.value} note"),
            delivery_id=f"tier.{tier.value}",
            privacy_tier=tier,
        ).capture_id
        for tier in PrivacyTier
    }
    # Personal is egress-eligible here; only the authority's read tiers keep it out.
    engine.owner_egress_policy.set(
        frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        operation_id="op-share-all",
        authority=owner,
    )
    stored = _stored_tiers(engine)
    assert {stored[emitted[tier]] for tier in PrivacyTier} == {tier.value for tier in PrivacyTier}
    records = _export(engine, _authority(engine))
    assert {record.capture_id for record in records} == {
        emitted[PrivacyTier.PUBLIC],
        emitted[PrivacyTier.WORK],
    }
    assert {record.privacy_tier for record in records} == {"public", "work"}


def test_export_skips_work_records_without_external_egress(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    allowed = _capture(engine, delivery_id="egress.yes", text="synthetic shareable work")
    _capture(engine, delivery_id="egress.no", text="synthetic local work", external_egress=False)
    assert [record.capture_id for record in _export(engine, _authority(engine))] == [allowed]


def test_owner_egress_policy_share_work_controls_owner_captures(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    owner = owner_authority(engine.tasks, session_id="owner-test")
    receipt = engine.capture.accept(
        TextPayload("synthetic owner work note"),
        delivery_id="owner.work",
        privacy_tier=PrivacyTier.WORK,
    )
    authority = _authority(engine)
    assert _export(engine, authority) == ()  # share_work = 0
    engine.owner_egress_policy.set(
        frozenset({PrivacyTier.WORK}), operation_id="op-share-work", authority=owner
    )
    assert [record.capture_id for record in _export(engine, authority)] == [receipt.capture_id]


def test_export_window_is_inclusive_since_and_exclusive_until(tmp_path: Path) -> None:
    clock = MutableClock(START)
    engine = _engine(tmp_path, clock)
    stamps = [START, START + timedelta(days=1), START + timedelta(days=2)]
    ids = []
    for index, stamp in enumerate(stamps):
        clock.now = stamp
        ids.append(
            _capture(engine, delivery_id=f"window.{index}", text=f"synthetic window {index}")
        )
    records = _export(engine, _authority(engine), since=stamps[1], until=stamps[2])
    assert [record.capture_id for record in records] == [ids[1]]
    assert records[0].captured_at == "2026-10-21T12:00:00Z"


def test_export_is_ordered_and_stable(tmp_path: Path) -> None:
    clock = MutableClock(START)
    engine = _engine(tmp_path, clock)
    for index in range(4):
        clock.now = START + timedelta(minutes=index)
        _capture(engine, delivery_id=f"order.{index}", text=f"synthetic ordered {index}")
    first = _export(engine, _authority(engine))
    second = _export(engine, _authority(engine))
    assert [record.to_wire() for record in first] == [record.to_wire() for record in second]
    assert [record.captured_at for record in first] == sorted(r.captured_at for r in first)
    assert len(first) == 4


def test_export_never_carries_the_raw_source_reference(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _capture(engine, delivery_id="reference.check", text="synthetic reference body")
    (record,) = _export(engine, _authority(engine))
    wire = record.to_wire()
    assert set(wire) == {"id", "captured_at", "privacy_tier", "source", "text", "truncated"}
    assert set(wire["source"]) == {"origin", "payload_family", "source_id"}
    assert "private-reference" not in json.dumps(wire)
    assert wire["text"] == "synthetic reference body"
    assert wire["truncated"] is False


def test_export_truncates_text_on_a_utf8_boundary(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _capture(engine, delivery_id="long.text", text="é" * 50)
    (record,) = _export(engine, _authority(engine), max_bytes_per_record=11)
    assert record.truncated is True
    assert record.text == "é" * 5 + TRUNCATION_MARKER


def test_export_refuses_more_records_than_the_limit(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    for index in range(3):
        _capture(engine, delivery_id=f"limit.{index}", text=f"synthetic limit {index}")
    assert len(_export(engine, _authority(engine), limit=3)) == 3
    with pytest.raises(T03Error, match="response_too_large"):
        _export(engine, _authority(engine), limit=2)


def test_export_requires_an_external_provider_authority(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _capture(engine, delivery_id="owner.refused", text="synthetic owner refused")
    owner = owner_authority(engine.tasks, session_id="owner-test")
    with pytest.raises(T03Error, match="unsupported_capability"):
        _export(engine, owner)


def test_export_requires_content_read(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    authority = replace(_authority(engine), capabilities=frozenset({"search"}))
    with pytest.raises(T03Error, match="unsupported_capability"):
        _export(engine, authority)


def test_export_with_an_empty_space_scope_emits_nothing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _capture(engine, delivery_id="space.empty", text="synthetic unscoped")
    assert _export(engine, replace(_authority(engine), space_ids=frozenset())) == ()


@pytest.mark.parametrize(
    ("since", "until", "limit", "max_bytes"),
    [
        (START, START, 10, 100),  # empty window
        (START, START - timedelta(days=1), 10, 100),  # reversed window
        (START, START + timedelta(days=1), 0, 100),
        (START, START + timedelta(days=1), 10, 0),
        (START.replace(tzinfo=None), START + timedelta(days=1), 10, 100),
    ],
)
def test_export_rejects_invalid_arguments(
    tmp_path: Path, since: datetime, until: datetime, limit: int, max_bytes: int
) -> None:
    engine = _engine(tmp_path)
    with pytest.raises(T03Error, match="invalid_arguments"):
        _export(
            engine,
            _authority(engine),
            since=since,
            until=until,
            limit=limit,
            max_bytes_per_record=max_bytes,
        )


def _source_path(engine: BrainEngine, capture_id: str) -> Path:
    connection = sqlite3.connect(engine.profile.root / ".open-brain/state/phase1.sqlite3")
    try:
        (relative,) = connection.execute(
            "SELECT source_path FROM source_revisions WHERE capture_id=?", (capture_id,)
        ).fetchone()
    finally:
        connection.close()
    return engine.profile.root / str(relative)


def test_export_honours_a_non_empty_space_scope(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _capture(engine, delivery_id="space.other", text="synthetic unassigned")
    other = frozenset({"space_00000000-0000-4000-8000-000000000999"})
    assert len(_export(engine, _authority(engine))) == 1
    assert _export(engine, replace(_authority(engine), space_ids=other)) == ()


def test_export_skips_a_withdrawn_source(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    kept = _capture(engine, delivery_id="withdraw.kept", text="synthetic kept")
    withdrawn = _capture(engine, delivery_id="withdraw.gone", text="synthetic withdrawn")
    owner = owner_authority(engine.tasks, session_id="owner-test")
    connection = sqlite3.connect(engine.profile.root / ".open-brain/state/phase1.sqlite3")
    try:
        (source_id,) = connection.execute(
            "SELECT source_id FROM source_revisions WHERE capture_id=?", (withdrawn,)
        ).fetchone()
    finally:
        connection.close()
    inspection = engine.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    receipt = engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.export",
            source_id=source_id,
            expected_head=withdrawn,
            expected_lifecycle_version=inspection.lifecycle_version,
            brain_id=inspection.destination_brain_id,
            issuer_epoch=inspection.issuer_epoch,
            reason_code="owner_choice",
        ),
        authority=owner,
    )
    assert receipt.lifecycle == "retired"
    assert [record.capture_id for record in _export(engine, _authority(engine))] == [kept]


def test_export_orders_by_parsed_time_across_fractional_seconds(tmp_path: Path) -> None:
    clock = MutableClock(START)
    engine = _engine(tmp_path, clock)
    first = _capture(engine, delivery_id="fraction.whole", text="synthetic whole second")
    clock.now = START + timedelta(microseconds=500_000)
    second = _capture(engine, delivery_id="fraction.half", text="synthetic half second")
    records = _export(engine, _authority(engine))
    assert [record.capture_id for record in records] == [first, second]


def test_one_damaged_record_does_not_block_the_rest(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    good = _capture(engine, delivery_id="damage.good", text="synthetic good")
    bad = _capture(engine, delivery_id="damage.bad", text="synthetic bad")
    path = _source_path(engine, bad)
    copy = tmp_path / "copy.bin"
    copy.write_bytes(path.read_bytes())
    path.chmod(0o600)
    path.unlink()
    path.symlink_to(copy)  # confined reads refuse a symlinked source file
    assert [record.capture_id for record in _export(engine, _authority(engine))] == [good]


def test_export_rejects_a_window_at_the_edge_of_the_calendar(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with pytest.raises(T03Error, match="invalid_arguments"):
        _export(
            engine,
            _authority(engine),
            since=datetime(9999, 12, 30, tzinfo=UTC),
            until=datetime(9999, 12, 31, tzinfo=UTC),
        )
