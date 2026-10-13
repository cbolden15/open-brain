"""A real successor uses explicit baseline evidence, never rewrites old history."""

import json
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, CaptureFault, InjectedFault, ReferencePayload
from open_brain_engine.engine.historical_contracts import HistoricalBaselineRequest
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.historical_tasks import (
    adopt_historical_baseline,
    link_historical_copy,
)
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.source_intake import SourceRevisionObservedDelivery
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    SearchPageRequest,
    T03Error,
)

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_baseline import _baseline
from packages.app.tests.unit.engine.test_historical_link import _link_request
from packages.app.tests.unit.engine.test_paging import external_authority


def _adopt(engine: BrainEngine) -> HistoricalBaselineRequest:
    request = _baseline(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    return request


def _successor(baseline: HistoricalBaselineRequest) -> SourceRevisionObservedDelivery:
    old = baseline.observed_delivery
    capture = replace(
        old.submission.capture,
        payload=ReferencePayload(
            old.submission.capture.source_reference, "synthetic next revision"
        ),
        delivery_id="synthetic.next.capture",
    )
    return replace(
        old,
        delivery_id="synthetic.next.observed",
        submission=replace(
            old.submission,
            capture=capture,
            canonical_sha256=capture.request_sha256(),
            revision_key="synthetic.next.key",
            ordering={"kind": "predecessor", "revision_key": old.submission.revision_key},
        ),
        observation=replace(
            old.observation,
            original_sha256=sha256(b"synthetic next file").hexdigest(),
            transformed_sha256=sha256(b"synthetic next revision").hexdigest(),
            admitted_payload_sha256=sha256(canonical(capture.payload.to_dict())).hexdigest(),
        ),
    )


def test_head_resolves_baseline_without_rewriting_retained_revision(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    head = engine.sources.public_revision_sink(baseline.observed_delivery.binding).inspect_head()
    assert head.source_id == baseline.source_cas.source_id
    assert head.capture_id == baseline.retained_original.capture_id
    assert head.revision_key == baseline.observed_delivery.submission.revision_key
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT revision_key FROM source_revisions").fetchone()[0] is None
        assert connection.execute("SELECT COUNT(*) FROM source_namespaces").fetchone()[0] == 0


@pytest.mark.parametrize("managed", [False, True])
def test_two_real_successors_hide_old_copy_without_rewriting_baseline(
    tmp_path: Path, managed: bool
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    relation, consent = _link_request(engine)
    owner = EffectiveAuthority(profile.owner_actor_id, "session", frozenset(), None, owner=True)
    with exclusive_runtime_admission(profile) as admission:
        link_receipt = link_historical_copy(
            profile,
            relation,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            load_consent=lambda: consent,
        )
    with closing(engine._store.connect()) as connection:
        baseline = HistoricalBaselineRequest.from_value(
            json.loads(
                connection.execute(
                    "SELECT request_json FROM historical_operations WHERE operation_id=?",
                    (relation.baseline_operation_id,),
                ).fetchone()[0]
            )
        )
        old = tuple(
            connection.execute(
                "SELECT * FROM source_revisions WHERE capture_id=?",
                (baseline.retained_original.capture_id,),
            ).fetchone()
        )
    reader = replace(
        external_authority(engine),
        provider_id="openai",
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC}),
    )
    reader = replace(reader, capabilities=reader.capabilities | {"history-read"})
    read = RecordReadRequest(
        record_id=relation.retained_copy.capture_id,
        expected_revision_id=relation.retained_copy.capture_id,
    )
    engine.retrieval.read_record(read, authority=reader)
    first = _successor(baseline)
    sink = engine.sources.public_revision_sink(first.binding)
    if managed:
        first_receipt = sink.submit(first).source_receipt
    else:
        first_receipt = engine.sources.submit_revision(first.submission)
    assert first_receipt is not None
    second_capture = replace(first.submission.capture, delivery_id="synthetic.second.capture")
    second = replace(
        first,
        delivery_id="synthetic.second.observed",
        submission=replace(
            first.submission,
            capture=second_capture,
            canonical_sha256=second_capture.request_sha256(),
            revision_key="synthetic.second.key",
            expected_head=first_receipt.capture_id,
            ordering={"kind": "predecessor", "revision_key": first.submission.revision_key},
        ),
    )
    if managed:
        result = sink.submit(second)
        second_receipt = result.source_receipt
        assert sink.submit(second) == result
    else:
        second_receipt = engine.sources.submit_revision(second.submission)
        assert engine.sources.submit_revision(second.submission) == second_receipt
    assert second_receipt is not None
    assert first_receipt.source_id == second_receipt.source_id == baseline.source_cas.source_id
    assert sink.inspect_head().capture_id == second_receipt.capture_id
    for current in (engine, BrainEngine.open(profile)):
        with pytest.raises(T03Error, match="not_found"):
            current.retrieval.read_record(read, authority=reader)
        with pytest.raises(T03Error, match="not_found"):
            current.history.list_history(
                HistoryListRequest(record_id=relation.retained_copy.capture_id), authority=reader
            )
        assert (
            current.retrieval.search_page(
                SearchPageRequest(query="synthetic retained"), authority=reader
            ).to_wire()["results"]
            == []
        )
        current.history.read_history(read, authority=owner)
        current.portability.rebuild_index()
        with pytest.raises(T03Error, match="not_found"):
            current.retrieval.read_record(read, authority=reader)
    with exclusive_runtime_admission(profile) as admission:
        assert (
            link_historical_copy(
                profile,
                relation,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                load_consent=lambda: consent,
            )
            == link_receipt
        )
    with closing(engine._store.connect()) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM source_revisions WHERE capture_id=?",
                    (baseline.retained_original.capture_id,),
                ).fetchone()
            )
            == old
        )
        assert (
            connection.execute(
                "SELECT predecessor_capture_id FROM source_revisions WHERE capture_id=?",
                (second_receipt.capture_id,),
            ).fetchone()[0]
            == first_receipt.capture_id
        )
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM logical_sources").fetchone()[0] == 2


@pytest.mark.parametrize("managed", [False, True])
def test_first_successor_keeps_source_and_old_null_key_with_exact_replay(
    tmp_path: Path, managed: bool
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    next_delivery = _successor(baseline)
    sink = engine.sources.public_revision_sink(next_delivery.binding)
    with closing(engine._store.connect()) as connection:
        old = tuple(connection.execute("SELECT * FROM source_revisions").fetchone())
    if managed:
        result = sink.submit(next_delivery)
        receipt = result.source_receipt
        assert sink.submit(next_delivery) == result
    else:
        receipt = engine.sources.submit_revision(next_delivery.submission)
        assert engine.sources.submit_revision(next_delivery.submission) == receipt
    assert receipt is not None
    assert receipt.source_id == baseline.source_cas.source_id
    assert receipt.capture_id != baseline.retained_original.capture_id
    assert sink.inspect_head().revision_key == next_delivery.submission.revision_key
    with closing(engine._store.connect()) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM source_revisions WHERE capture_id=?",
                    (baseline.retained_original.capture_id,),
                ).fetchone()
            )
            == old
        )
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM logical_sources").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_namespaces").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT predecessor_capture_id FROM source_revisions WHERE capture_id=?",
                (receipt.capture_id,),
            ).fetchone()[0]
            == baseline.retained_original.capture_id
        )


@pytest.mark.parametrize("managed", [False, True])
def test_withdrawn_baseline_identity_is_not_missing_or_a_new_source(
    tmp_path: Path, managed: bool
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.baseline",
            source_id=baseline.source_cas.source_id,
            expected_head=baseline.source_cas.expected_head,
            expected_lifecycle_version=baseline.source_cas.expected_lifecycle_version,
            brain_id=baseline.destination.brain_id,
            issuer_epoch=baseline.destination.issuer_epoch,
            reason_code="synthetic_withdrawal",
        ),
        authority=owner,
    )
    successor = _successor(baseline)
    sink = engine.sources.public_revision_sink(successor.binding)
    head = sink.inspect_head()
    assert head.source_id == baseline.source_cas.source_id and head.lifecycle == "retired"
    # A caller omitting the old head cannot evade the independently managed identity.
    successor = replace(successor, submission=replace(successor.submission, expected_head=None))
    with pytest.raises(T03Error, match="revision_changed"):
        if managed:
            sink.submit(successor)
        else:
            engine.sources.submit_revision(successor.submission)
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM logical_sources").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_intakes").fetchone()[0] == 0


@pytest.mark.parametrize("managed", [False, True])
def test_baseline_envelope_is_not_an_ordinary_new_capture(tmp_path: Path, managed: bool) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    with pytest.raises(T03Error, match="invalid_arguments"):
        if managed:
            engine.sources.public_revision_sink(baseline.observed_delivery.binding).submit(
                baseline.observed_delivery
            )
        else:
            engine.sources.submit_revision(baseline.observed_delivery.submission)
    with closing(engine._store.connect()) as connection:
        for table in ("source_intakes", "managed_source_deliveries", "source_namespaces"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1


@pytest.mark.parametrize("damage", ["unavailable", "historical"])
@pytest.mark.parametrize("managed", [False, True])
def test_reserved_successor_recovery_rechecks_baseline_availability(
    tmp_path: Path, damage: str, managed: bool
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    baseline = _adopt(BrainEngine.open(profile))
    successor = _successor(baseline)
    engine = BrainEngine.open(profile, faults={CaptureFault.AFTER_CAPTURE_RESERVATION})
    with pytest.raises(InjectedFault):
        if managed:
            engine.sources.public_revision_sink(successor.binding).submit(successor)
        else:
            engine.sources.submit_revision(successor.submission)
    with closing(engine._store.connect()) as connection:
        retained_envelope = bytes(
            connection.execute("SELECT submission_json FROM source_intakes").fetchone()[0]
        )
    with _historical_transaction(profile, lambda: None) as connection:
        if damage == "unavailable":
            connection.execute(
                "UPDATE logical_sources SET availability='missing' WHERE source_id=?",
                (baseline.source_cas.source_id,),
            )
        else:
            connection.execute(
                "UPDATE logical_sources SET historical_only=1 WHERE source_id=?",
                (baseline.source_cas.source_id,),
            )
    with pytest.raises(T03Error, match="revision_changed"):
        BrainEngine.open(profile)
    with closing(engine._store.connect()) as connection:
        intake = connection.execute("SELECT * FROM source_intakes").fetchone()
        assert bytes(intake["submission_json"]) == retained_envelope
        assert intake["receipt_json"] is None
        assert connection.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_namespaces").fetchone()[0] == 0
        if managed:
            retained = connection.execute("SELECT * FROM managed_source_deliveries").fetchone()
            assert bytes(retained["envelope_bytes"]) == successor.custody_bytes()
            assert retained["receipt_json"] is None
    with _historical_transaction(profile, lambda: None) as connection:
        connection.execute(
            "UPDATE logical_sources SET availability='available',historical_only=0 "
            "WHERE source_id=?",
            (baseline.source_cas.source_id,),
        )
    reopened = BrainEngine.open(profile)
    if managed:
        sink = reopened.sources.public_revision_sink(successor.binding)
        result = sink.submit(successor)
        assert sink.submit(successor) == result
        receipt = result.source_receipt
    else:
        receipt = reopened.sources.submit_revision(successor.submission)
    assert receipt is not None
    assert receipt.source_id == baseline.source_cas.source_id
    with closing(reopened._store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM source_namespaces").fetchone()[0] == 1


@pytest.mark.parametrize("damage", ["binding", "registry", "unavailable", "historical"])
def test_head_or_admission_refuses_invalid_authority_before_reservation(
    tmp_path: Path, damage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    successor = _successor(baseline)
    if damage == "binding":
        binding = replace(successor.binding, accepted_source_id="different.selection")
        with pytest.raises(SharingError, match="binding_mismatch"):
            engine.sources.public_revision_sink(binding).inspect_head()
    elif damage == "registry":
        path = engine.profile.root / ".open-brain/historical-authority/historical-claims.v1.json"
        path.rename(path.with_suffix(".retained"))
        with pytest.raises(SharingError, match="binding_mismatch"):
            engine.sources.public_revision_sink(successor.binding).inspect_head()
    else:
        with _historical_transaction(engine.profile, lambda: None) as connection:
            if damage == "unavailable":
                connection.execute("UPDATE logical_sources SET availability='missing'")
            else:
                connection.execute("UPDATE logical_sources SET historical_only=1")
        with pytest.raises(T03Error, match="revision_changed"):
            engine.sources.public_revision_sink(successor.binding).submit(successor)
        with pytest.raises(T03Error, match="revision_changed"):
            engine.sources.submit_revision(successor.submission)
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_intakes").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM managed_source_deliveries").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1
