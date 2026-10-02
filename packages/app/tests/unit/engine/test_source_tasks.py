from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import (
    ReferencePayload,
    SourceInspectRequest,
    SourceWithdrawRequest,
    TextPayload,
    local_schema,
    open_local_engine,
)
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    SourceRouteRequest,
    T03Error,
    response_to_wire,
)
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH, manifest_v4
from open_brain_engine.portable.v5 import V5_SIDECAR_PATHS
from open_brain_engine.portable.v6 import V6_SIDECAR_PATHS
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


def test_source_route_cas_preserves_capture_and_exports_v6(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    receipt = tasks.capture.accept(
        TextPayload("synthetic immutable source"), delivery_id="manual.one"
    )
    space = tasks.inbox.create_space("Synthetic", delivery_id="space.one")
    with open_local_database_read_only(profile) as connection:
        row = connection.execute(
            "SELECT source_id,source_path FROM source_revisions WHERE capture_id=?",
            (receipt.capture_id,),
        ).fetchone()
        source_id, path = row["source_id"], row["source_path"]
    before = (profile.root / path).read_bytes()
    request = SourceRouteRequest(
        source_id=source_id,
        expected_head=receipt.capture_id,
        expected_route_version=0,
        space_id=space.space_id,
        operation_id="operation_" + str(uuid4()),
    )
    authority = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    assert tasks.sources is not None
    result = response_to_wire("source.route", tasks.sources.route(request, authority=authority))
    assert result["route_version"] == 1
    assert (
        response_to_wire("source.route", tasks.sources.route(request, authority=authority))
        == result
    )
    with pytest.raises(T03Error, match="revision_changed"):
        tasks.sources.route(
            SourceRouteRequest(**dict(request.to_wire(), operation_id="operation_" + str(uuid4()))),
            authority=authority,
        )
    assert (profile.root / path).read_bytes() == before
    metadata = json.loads((profile.root / SOURCE_METADATA_PATH).read_bytes())
    assert metadata["sources"][0]["head_capture_id"] == receipt.capture_id
    export = tmp_path / "export"
    exported = tasks.portability.export(export, export_id="export_" + str(uuid4()))
    assert exported.schema_version == 6
    snapshot = validated_portable_snapshot(export)
    assert snapshot.manifest["schema_version"] == 6
    assert snapshot.files.keys() >= V5_SIDECAR_PATHS | V6_SIDECAR_PATHS
    assert snapshot.files[path] == before
    imported = tmp_path / "imported"
    import_receipt = tasks.portability.import_clean(
        export, imported, import_id="import_" + str(uuid4())
    )
    assert import_receipt.schema_version == 6
    assert imported.is_dir()


def test_source_withdrawal_replays_and_retains_owner_evidence(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    capture = tasks.capture.accept(
        TextPayload("retained withdrawal evidence"), delivery_id="withdraw.one"
    )
    with open_local_database_read_only(profile) as connection:
        source_id = connection.execute(
            "SELECT source_id FROM source_revisions WHERE capture_id=?", (capture.capture_id,)
        ).fetchone()[0]
        brain_id, issuer_epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
        ).fetchone()
    authority = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    assert tasks.sources is not None
    inspected = tasks.sources.inspect(
        SourceInspectRequest(source_id=source_id), authority=authority
    )
    request = SourceWithdrawRequest(
        operation_id="withdraw." + str(uuid4()),
        source_id=source_id,
        expected_head=capture.capture_id,
        expected_lifecycle_version=inspected.lifecycle_version,
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
        reason_code="complete_scan_absence",
    )
    retired = tasks.sources.withdraw(request, authority=authority)
    assert tasks.sources.withdraw(request, authority=authority) == retired
    with pytest.raises(T03Error, match="revision_changed"):
        tasks.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw." + str(uuid4()),
                source_id=source_id,
                expected_head=capture.capture_id,
                expected_lifecycle_version=0,
                brain_id=brain_id,
                issuer_epoch=issuer_epoch,
                reason_code="stale",
            ),
            authority=authority,
        )
    assert tasks.retrieval.search("withdrawal evidence") == ()
    with open_local_database_read_only(profile) as connection:
        revisions = connection.execute(
            "SELECT count(*) FROM source_revisions WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        captures = connection.execute(
            "SELECT count(*) FROM captures WHERE capture_id=?", (capture.capture_id,)
        ).fetchone()[0]
    assert revisions == 1
    assert captures == 1


@pytest.mark.parametrize("boundary", ["before_receipt_insert", "after_commit_before_metadata"])
def test_withdrawal_crash_is_atomic_and_exact_retry_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from open_brain_engine.engine import source_store

    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    capture = engine.capture.accept(TextPayload("withdrawal atomic evidence"), delivery_id="atomic")
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    with open_local_database_read_only(profile) as connection:
        row = connection.execute(
            "SELECT source_id,source_path FROM source_revisions WHERE capture_id=?",
            (capture.capture_id,),
        ).fetchone()
        source_id, source_path = row["source_id"], row["source_path"]
    original_bytes = (profile.root / source_path).read_bytes()
    before = engine.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    request = SourceWithdrawRequest(
        operation_id="withdraw.atomic",
        source_id=source_id,
        expected_head=capture.capture_id,
        expected_lifecycle_version=before.lifecycle_version,
        brain_id=before.destination_brain_id,
        issuer_epoch=before.issuer_epoch,
        reason_code="owner_choice",
    )

    def crash(*_args: object) -> None:
        raise RuntimeError("synthetic withdrawal crash")

    with monkeypatch.context() as fault:
        if boundary == "before_receipt_insert":
            fault.setattr(engine, "_clock", crash)
        else:
            fault.setattr(source_store, "publish_source_metadata", crash)
        with pytest.raises(RuntimeError, match="synthetic withdrawal crash"):
            engine.sources.withdraw(request, authority=owner)

    # Observe durable authority before allowing startup recovery to touch views.
    committed = boundary == "after_commit_before_metadata"
    with open_local_database_read_only(profile) as connection:
        state = connection.execute(
            "SELECT lifecycle,availability,lifecycle_version FROM logical_sources "
            "JOIN source_lifecycle_state USING(source_id) WHERE source_id=?",
            (source_id,),
        ).fetchone()
        expected_state = ("retired", "missing", 1) if committed else ("active", "available", 0)
        assert tuple(state) == expected_state
        assert connection.execute(
            "SELECT count(*) FROM source_lifecycle_operations WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()[0] == int(committed)
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1
    assert (profile.root / source_path).read_bytes() == original_bytes
    reopened = BrainEngine.open(profile)
    receipt = reopened.sources.withdraw(request, authority=owner)
    assert reopened.sources.withdraw(request, authority=owner) == receipt
    assert receipt.lifecycle_version == 1 and receipt.lifecycle == "retired"
    assert reopened.retrieval.search("withdrawal atomic") == ()
    with open_local_database_read_only(profile) as connection:
        stored = connection.execute(
            "SELECT receipt_json FROM source_lifecycle_operations WHERE operation_id=?",
            (request.operation_id,),
        ).fetchall()
        assert len(stored) == 1
        assert json.loads(stored[0]["receipt_json"])["receipt_sha256"] == receipt.receipt_sha256
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1
    assert (profile.root / source_path).read_bytes() == original_bytes
    metadata = json.loads((profile.root / SOURCE_METADATA_PATH).read_bytes())
    assert metadata["sources"][0]["lifecycle"] == "retired"


@pytest.mark.parametrize("state", ["stale_lifecycle", "retired", "stale_control"])
def test_revision_sink_refuses_stale_lifecycle_before_any_admission(
    tmp_path: Path, state: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    capture = _public_submission(engine.tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="lifecycle", resource_id="one", external_id="one"
    )
    first = SourceRevisionSubmission(
        capture=capture,
        namespace=namespace,
        revision_key="first",
        canonical_sha256=capture.request_sha256(),
        expected_head=None,
        ordering={"kind": "unordered"},
        expected_control_epoch=0,
    )
    accepted = engine.sources.submit_revision(first)
    assert accepted.source_id is not None and accepted.capture_id is not None
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    inspection = engine.sources.inspect(
        SourceInspectRequest(source_id=accepted.source_id), authority=owner
    )
    if state == "retired":
        engine.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.admission",
                source_id=accepted.source_id,
                expected_head=accepted.capture_id,
                expected_lifecycle_version=0,
                brain_id=inspection.destination_brain_id,
                issuer_epoch=inspection.issuer_epoch,
                reason_code="owner_choice",
            ),
            authority=owner,
        )
    elif state == "stale_control":
        assert engine.sources.fence_intake(expected_epoch=0, authority=owner) == 1
    before = engine.sources.inspect(
        SourceInspectRequest(source_id=accepted.source_id), authority=owner
    )
    changed = replace(capture, payload=TextPayload("changed after lifecycle observation"))
    submission = replace(
        first,
        capture=changed,
        canonical_sha256=changed.request_sha256(),
        revision_key="changed",
        expected_head=accepted.capture_id,
        ordering={"kind": "predecessor", "revision_key": "first"},
    )
    binding = SourceRevisionBinding(
        destination_brain_id=inspection.destination_brain_id,
        issuer_epoch=inspection.issuer_epoch,
        root_fingerprint="synthetic-root",
        accepted_source_id="synthetic-selection",
        namespace=namespace,
    )
    delivery = SourceRevisionDelivery(
        binding=binding,
        submission=submission,
        expected_lifecycle_version=99 if state == "stale_lifecycle" else 0,
        delivery_id="synthetic.lifecycle.changed",
    )
    with pytest.raises(T03Error, match="revision_changed"):
        engine.sources.public_revision_sink(binding).submit(delivery)
    with open_local_database_read_only(engine.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 0
        )
    assert (
        engine.sources.inspect(SourceInspectRequest(source_id=accepted.source_id), authority=owner)
        == before
    )
    reopened = BrainEngine.open(engine.profile)
    if state == "stale_control":
        with pytest.raises(T03Error, match="revision_changed"):
            reopened.sources.submit_revision(first)
    else:
        assert reopened.sources.submit_revision(first) == accepted
    assert (
        reopened.sources.inspect(
            SourceInspectRequest(source_id=accepted.source_id), authority=owner
        )
        == before
    )


def test_revision_sink_recovers_accepted_response_loss_before_withdrawal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    capture = _public_submission(engine.tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="response", resource_id="one", external_id="one"
    )
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    with open_local_database_read_only(engine.profile) as connection:
        brain_id, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    binding = SourceRevisionBinding(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        root_fingerprint="synthetic-root",
        accepted_source_id="synthetic-selection",
        namespace=namespace,
    )
    submission = SourceRevisionSubmission(
        capture=capture,
        namespace=namespace,
        revision_key="first",
        canonical_sha256=capture.request_sha256(),
        expected_head=None,
        ordering={"kind": "unordered"},
        expected_control_epoch=0,
    )
    delivery = SourceRevisionDelivery(
        binding=binding,
        submission=submission,
        expected_lifecycle_version=0,
        delivery_id="synthetic.accepted.response-loss",
    )
    sink = engine.sources.public_revision_sink(binding)
    original_submit = engine.sources.submit_revision
    accepted = []

    def lose_response(value: SourceRevisionSubmission) -> object:
        accepted.append(original_submit(value))
        raise RuntimeError("synthetic accepted response loss")

    with monkeypatch.context() as fault:
        fault.setattr(engine.sources, "submit_revision", lose_response)
        with pytest.raises(RuntimeError, match="synthetic accepted response loss"):
            sink.submit(delivery)
    assert len(accepted) == 1
    source = accepted[0]
    assert source.source_id is not None and source.capture_id is not None
    inspection = engine.sources.inspect(
        SourceInspectRequest(source_id=source.source_id), authority=owner
    )
    withdrawal = SourceWithdrawRequest(
        operation_id="withdraw.response-loss",
        source_id=source.source_id,
        expected_head=source.capture_id,
        expected_lifecycle_version=inspection.lifecycle_version,
        brain_id=brain_id,
        issuer_epoch=epoch,
        reason_code="owner_choice",
    )
    with pytest.raises(T03Error, match="operation_pending"):
        engine.sources.withdraw(withdrawal, authority=owner)
    reopened = BrainEngine.open(engine.profile)
    recovered = reopened.sources.public_revision_sink(binding).submit(delivery)
    assert recovered.source_receipt == source
    assert recovered.envelope_sha256 == delivery.envelope_sha256
    withdrawn = reopened.sources.withdraw(withdrawal, authority=owner)
    assert reopened.sources.public_revision_sink(binding).submit(delivery) == recovered
    reopened.sources.public_revision_sink(binding).verify_receipt(delivery, recovered)
    final = reopened.sources.inspect(
        SourceInspectRequest(source_id=source.source_id), authority=owner
    )
    assert final.lifecycle == "retired" and final.lifecycle_version == withdrawn.lifecycle_version
    assert final.head_capture_id == source.capture_id
    with open_local_database_read_only(engine.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1
        row = connection.execute(
            "SELECT envelope_bytes,receipt_json FROM managed_source_deliveries WHERE delivery_id=?",
            (delivery.delivery_id,),
        ).fetchone()
        assert bytes(row["envelope_bytes"]) == delivery.custody_bytes()
        assert json.loads(row["receipt_json"])["source_receipt"]["capture_id"] == source.capture_id


@pytest.mark.parametrize("interleaving", ["fence", "withdraw"])
def test_managed_reservation_blocks_overtaking_owner_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interleaving: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    capture = _public_submission(engine.tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="race", resource_id="one", external_id="one"
    )
    first = SourceRevisionSubmission(
        capture=capture,
        namespace=namespace,
        revision_key="one",
        canonical_sha256=capture.request_sha256(),
        expected_head=None,
        ordering={"kind": "unordered"},
        expected_control_epoch=0,
    )
    accepted = engine.sources.submit_revision(first)
    owner = EffectiveAuthority("synthetic", "session", frozenset(), None, owner=True)
    source_id, capture_id = accepted.source_id, accepted.capture_id
    assert source_id is not None and capture_id is not None
    with open_local_database_read_only(engine.profile) as connection:
        brain_id, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    binding = SourceRevisionBinding(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        root_fingerprint="synthetic-root",
        accepted_source_id="synthetic-selection",
        namespace=namespace,
    )
    changed = replace(
        capture,
        payload=ReferencePayload(
            url="https://example.invalid/source", supplied_text="synthetic second revision"
        ),
        delivery_id="synthetic.race.second",
    )
    second = SourceRevisionSubmission(
        capture=changed,
        namespace=namespace,
        revision_key="two",
        canonical_sha256=changed.request_sha256(),
        expected_head=accepted.capture_id,
        ordering={"kind": "predecessor", "revision_key": "one"},
        expected_control_epoch=0,
    )
    delivery = SourceRevisionDelivery(
        binding=binding,
        submission=second,
        expected_lifecycle_version=0,
        delivery_id="synthetic.race.delivery",
    )
    original = engine.sources.submit_revision
    observed = []

    def overtake(value: SourceRevisionSubmission) -> object:
        with pytest.raises(T03Error, match="operation_pending"):
            if interleaving == "fence":
                engine.sources.fence_intake(expected_epoch=0, authority=owner)
            else:
                engine.sources.withdraw(
                    SourceWithdrawRequest(
                        operation_id="withdraw.overtake",
                        source_id=source_id,
                        expected_head=capture_id,
                        expected_lifecycle_version=0,
                        brain_id=brain_id,
                        issuer_epoch=epoch,
                        reason_code="owner_choice",
                    ),
                    authority=owner,
                )
        observed.append(True)
        return original(value)

    with monkeypatch.context() as fault:
        fault.setattr(engine.sources, "submit_revision", overtake)
        receipt = engine.sources.public_revision_sink(binding).submit(delivery)
    assert observed == [True] and receipt.outcome == "captured"
    reopened = BrainEngine.open(engine.profile)
    assert reopened.sources.public_revision_sink(binding).submit(delivery) == receipt
    with open_local_database_read_only(engine.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM managed_source_deliveries WHERE receipt_json IS NULL"
            ).fetchone()[0]
            == 0
        )
    assert reopened.sources.fence_intake(expected_epoch=0, authority=owner) == 1


@pytest.mark.parametrize("fault_name", ["AFTER_CAPTURE_RESERVATION", "AFTER_SOURCE_WRITE"])
def test_managed_fenced_intake_retains_terminal_custody_on_exact_replay(
    tmp_path: Path, fault_name: str
) -> None:
    from open_brain_engine.engine import CaptureFault, InjectedFault

    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile, faults={CaptureFault[fault_name]})
    capture = _public_submission(engine.tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="fenced", resource_id="one", external_id="one"
    )
    with open_local_database_read_only(profile) as connection:
        brain_id, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    binding = SourceRevisionBinding(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        root_fingerprint="synthetic-root",
        accepted_source_id="synthetic-selection",
        namespace=namespace,
    )
    request = SourceRevisionSubmission(
        capture=capture,
        namespace=namespace,
        revision_key="one",
        canonical_sha256=capture.request_sha256(),
        expected_head=None,
        ordering={"kind": "unordered"},
        expected_control_epoch=0,
    )
    delivery = SourceRevisionDelivery(
        binding=binding,
        submission=request,
        expected_lifecycle_version=0,
        delivery_id="synthetic.fenced.managed",
    )
    with pytest.raises(InjectedFault):
        engine.sources.public_revision_sink(binding).submit(delivery)
    before = {
        path.relative_to(profile.root): path.read_bytes()
        for path in (profile.root / "sources").rglob("*")
        if path.is_file() and path.name != "logical-sources.json"
    }
    owner = EffectiveAuthority("synthetic", "session", frozenset(), None, owner=True)
    assert engine.sources.fence_intake(expected_epoch=0, authority=owner) == 1
    reopened = BrainEngine.open(profile)
    receipt = reopened.sources.public_revision_sink(binding).submit(delivery)
    assert receipt.outcome == "quarantined" and receipt.source_receipt is not None
    assert receipt.source_receipt.custody_id is not None
    assert receipt.source_receipt.control_epoch == 1
    assert reopened.sources.public_revision_sink(binding).submit(delivery) == receipt
    after = {
        path.relative_to(profile.root): path.read_bytes()
        for path in (profile.root / "sources").rglob("*")
        if path.is_file() and path.name != "logical-sources.json"
    }
    assert after == before
    with open_local_database_read_only(profile) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM managed_source_deliveries WHERE receipt_json IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM source_quarantine").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM captures WHERE stage<3").fetchone()[0] == 0
    with pytest.raises(ValueError, match="^ingestion_pending$"):
        reopened.tasks.portability.export(tmp_path / "blocked", export_id="export_" + str(uuid4()))


def test_standalone_v4_import_refusal_uses_valid_v4_fixture(tmp_path: Path) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    tasks.capture.accept(TextPayload("synthetic legacy fixture"), delivery_id="legacy.v4")
    current = tmp_path / "current-v5"
    tasks.portability.export(current, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(current)
    files = {
        relative: payload
        for relative, payload in snapshot.files.items()
        if relative != "portable-manifest.json"
        and relative not in V5_SIDECAR_PATHS
        and relative not in V6_SIDECAR_PATHS
    }
    legacy = tmp_path / "legacy-v4"
    for relative, payload in files.items():
        destination = legacy / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    manifest = manifest_v4(
        files,
        tenant_id=str(snapshot.manifest["tenant_id"]),
        export_id="export_" + str(uuid4()),
        created_at=str(snapshot.manifest["created_at"]),
    )
    (legacy / "portable-manifest.json").write_bytes(portable_canonical_json_bytes(manifest))
    assert validated_portable_snapshot(legacy).manifest["schema_version"] == 4
    with pytest.raises(ValueError, match="Portable v4 import is not supported"):
        tasks.portability.import_clean(
            legacy, tmp_path / "refused", import_id="import_" + str(uuid4())
        )
    assert not (tmp_path / "refused").exists()


def test_explicit_revision_order_replay_history_route_and_control(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    assert tasks.sources is not None
    original = _public_submission(tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
    )

    def submit(key: str, sequence: int, head: str | None, text: str) -> SourceRevisionSubmission:
        capture = replace(original, payload=ReferencePayload(original.source_reference, text))
        return SourceRevisionSubmission(
            capture=capture,
            namespace=namespace,
            revision_key=key,
            canonical_sha256=capture.request_sha256(),
            expected_head=head,
            ordering={
                "kind": "monotonic",
                "provider_namespace": "synthetic",
                "epoch": "one",
                "sequence": sequence,
            },
            expected_control_epoch=0,
        )

    first_request = submit("a1", 1, None, "first")
    first = tasks.sources.submit_revision(first_request)
    assert first.source_id is not None and first.capture_id is not None
    space = tasks.inbox.create_space("Synthetic", delivery_id="space.one")
    authority = EffectiveAuthority("synthetic", "session", frozenset(), None, owner=True)
    tasks.sources.route(
        SourceRouteRequest(
            source_id=first.source_id,
            expected_head=first.capture_id,
            expected_route_version=0,
            space_id=space.space_id,
            operation_id="operation_" + str(uuid4()),
        ),
        authority=authority,
    )
    newest = tasks.sources.submit_revision(submit("a3", 3, first.capture_id, "newest"))
    late = tasks.sources.submit_revision(submit("a2", 2, newest.capture_id, "late"))
    assert newest.capture_id is not None and late.capture_id is not None
    assert late.outcome == "history_only"
    assert [item.capture_id for item in tasks.inbox.list()] == [newest.capture_id]
    assert tasks.sources.submit_revision(first_request) == first
    assert tasks.retrieval.fetch(first.capture_id) is None
    assert tasks.retrieval.fetch(late.capture_id) is None
    fetched = tasks.retrieval.fetch(newest.capture_id)
    assert fetched is not None and fetched.space_id == space.space_id
    with pytest.raises(T03Error, match="source_revision_conflict"):
        tasks.sources.submit_revision(submit("a3", 3, newest.capture_id, "different"))
    with open_local_database_read_only(profile) as connection:
        assert connection.execute("SELECT count(*) FROM source_quarantine").fetchone()[0] == 1
        assert (
            connection.execute("SELECT head_capture_id FROM logical_sources").fetchone()[0]
            == newest.capture_id
        )
    held = submit("a4", 4, newest.capture_id, "held")
    assert tasks.sources.fence_intake(expected_epoch=0, authority=authority) == 1
    with pytest.raises(T03Error, match="revision_changed"):
        tasks.sources.submit_revision(held)


@pytest.mark.parametrize(
    "fault_name", ["AFTER_CAPTURE_RESERVATION", "AFTER_SOURCE_WRITE", "AFTER_BLOB_WRITE"]
)
def test_fenced_reservation_enters_custody_without_new_source_writes_or_startup_poison(
    tmp_path: Path,
    fault_name: str,
) -> None:
    from open_brain_engine.engine import CaptureFault, FilePayload, InjectedFault

    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile, faults={CaptureFault[fault_name]})
    assert tasks.sources is not None
    capture = _public_submission(tasks)
    if fault_name == "AFTER_BLOB_WRITE":
        capture = replace(capture, payload=FilePayload("synthetic.txt", "text/plain", b"synthetic"))
    request = SourceRevisionSubmission(
        capture=capture,
        namespace=dict(
            connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
        ),
        revision_key="one",
        canonical_sha256=capture.request_sha256(),
        expected_head=None,
        ordering={"kind": "unordered"},
        expected_control_epoch=0,
    )
    with pytest.raises(InjectedFault):
        tasks.sources.submit_revision(request)
    before = {
        p.relative_to(profile.root): p.read_bytes()
        for p in (profile.root / "sources").rglob("*")
        if p.is_file() and p.name != "logical-sources.json"
    }
    authority = EffectiveAuthority("synthetic", "session", frozenset(), None, owner=True)
    assert tasks.sources.fence_intake(expected_epoch=0, authority=authority) == 1
    after = {
        p.relative_to(profile.root): p.read_bytes()
        for p in (profile.root / "sources").rglob("*")
        if p.is_file() and p.name != "logical-sources.json"
    }
    assert after == before
    terminal = tasks.sources.submit_revision(replace(request, expected_control_epoch=1))
    assert terminal.outcome == "quarantined" and terminal.custody_id is not None
    reopened = open_local_engine(profile)
    assert reopened.inbox.list() == ()
    reopened.capture.accept(TextPayload("unrelated capture"), delivery_id="unrelated")
    with open_local_database_read_only(profile) as connection:
        assert connection.execute("SELECT count(*) FROM source_quarantine").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM captures WHERE stage<3").fetchone()[0] == 0
    exported = tmp_path / "export"
    with pytest.raises(ValueError, match="^ingestion_pending$"):
        reopened.portability.export(exported, export_id="export_" + str(uuid4()))
    assert not exported.exists()


@pytest.mark.parametrize("schema", [5, 6])
def test_migrated_alias_adoption_and_automatic_publication_preserve_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema: int,
) -> None:
    from open_brain_engine.engine import CaptureAction

    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", schema)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:schema])
        old = open_local_engine(profile)
        capture = _public_submission(old)
        receipt = old.capture.submit(capture)
        space = old.inbox.create_space("Synthetic", delivery_id="space")
        old.capture.accept(
            TextPayload("Automatic publication"),
            action=CaptureAction.CANONICAL_NOTE,
            delivery_id="automatic",
            space_id=space.space_id,
        )
    from open_brain_engine.engine import coordinate_local_migration

    coordinate_local_migration(profile)
    tasks = open_local_engine(profile)
    assert tasks.sources is not None
    adopted = tasks.sources.submit_revision(
        SourceRevisionSubmission(
            capture=capture,
            namespace=dict(
                connector_name="synthetic",
                connection_id="one",
                resource_id="one",
                external_id="one",
            ),
            revision_key="baseline",
            canonical_sha256=capture.request_sha256(),
            expected_head=receipt.capture_id,
            ordering={"kind": "unordered"},
            expected_control_epoch=0,
        )
    )
    assert adopted.capture_id == receipt.capture_id
    assert len(tasks.retrieval.search("Automatic publication", record_type="canonical")) == 1


@pytest.mark.parametrize("version", [1, 2, 3])
def test_schema_seven_imports_legacy_portable_without_changing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    from open_brain_engine.engine import DecisionOutcome, ProposalDraft

    from packages.app.tests.integration.engine.test_managed_portability import _setup_two

    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])
        historical = BrainEngine.open(
            compile_single_user_local(tmp_path / "historical", starter_spaces=("Notes",))
        )
        if version == 2:
            _setup_two(historical, tmp_path / "vault")
        else:
            capture = historical.capture.accept(
                TextPayload("Synthetic legacy evidence"),
                delivery_id="legacy.one",
                space_id=historical.inbox.spaces()[0].space_id,
            )
            if version == 3:
                proposal = historical.review.propose(
                    (capture.capture_id,),
                    (ProposalDraft("Synthetic", "Synthetic publication"),),
                    delivery_id="legacy.proposal",
                )[0]
                historical.review.decide(
                    proposal.proposal_id,
                    DecisionOutcome.APPROVED,
                    delivery_id="legacy.approval",
                    expected_review_digest=proposal.review_digest,
                )
        export = tmp_path / "legacy-export"
        assert (
            historical.portability.export(export, export_id="export_" + str(uuid4())).schema_version
            == version
        )
    snapshot = validated_portable_snapshot(export)
    control = open_local_engine(compile_single_user_local(tmp_path / "control"))
    destination = tmp_path / "imported"
    import_id = "import_" + str(uuid4())
    assert (
        control.portability.import_clean(export, destination, import_id=import_id).schema_version
        == version
    )
    assert control.portability.import_clean(export, destination, import_id=import_id).duplicate
    assert validated_portable_snapshot(destination).files == snapshot.files
    imported = open_local_engine(compile_single_user_local(destination))
    with open_local_database_read_only(imported.profile) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] > 0


def test_schema_seven_owner_recovery_retains_current_writer_floor(tmp_path: Path) -> None:
    from open_brain_engine.engine.managed_recovery import (
        abandon_managed_write,
        inspect_managed_recovery,
    )

    from packages.app.tests.integration.engine.test_managed_recovery import _legacy_fixture

    engine, _, _, moved = _legacy_fixture(tmp_path)
    before = moved.read_bytes()
    entry = inspect_managed_recovery(engine.profile, operation_id="legacy.pending").entries[0]
    assert entry.preview_digest is not None
    receipt = abandon_managed_write(
        engine.profile,
        operation_id="legacy.pending",
        expected_digest=entry.preview_digest,
        request_id="source.history.owner.recovery",
        validate_before_write=engine._assert_root,
    )
    assert not receipt.schema_upgraded
    assert moved.read_bytes() == before
    reopened = open_local_engine(engine.profile)
    with open_local_database_read_only(reopened.profile) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert tuple(connection.execute("SELECT * FROM runtime_compatibility").fetchone()) == (
            1,
                6,
                11,
        )
