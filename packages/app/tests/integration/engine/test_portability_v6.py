"""Schema-eleven lifecycle and admission archive acceptance with synthetic sources."""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import BrainEngine, EngineTaskSet, TextPayload, open_local_engine
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.materializer import _profile
from open_brain_engine.engine.portable_v5_restore import restore_portable_v5_root
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionDelivery,
    SourceRevisionDeliveryReceipt,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_lifecycle_contracts import (
    SourceInspectRequest,
    SourceWithdrawRequest,
)
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    SearchPageRequest,
    T03Error,
)
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v5 import validate_portable_file_set_v5
from open_brain_engine.portable.v6 import (
    SOURCE_ADMISSION_PATH,
    SOURCE_LIFECYCLE_PATH,
    V6_SIDECAR_PATHS,
    validate_portable_file_set_v6,
)
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


def _revision(tasks: EngineTaskSet) -> SourceRevisionSubmission:
    capture = _public_submission(tasks)
    return SourceRevisionSubmission(
        capture=capture,
        namespace=dict(
            connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
        ),
        revision_key="revision-one",
        canonical_sha256=capture.request_sha256(),
        expected_head=None,
        ordering={"kind": "unordered"},
        expected_control_epoch=0,
    )


def _wire(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value.to_wire())


def _read_exact(
    engine: BrainEngine,
    request: RecordReadRequest,
    authority: EffectiveAuthority,
    expected: bytes,
    *,
    history: bool,
) -> None:
    chunks: list[bytes] = []
    offset = 0
    read = engine.history.read_history if history else engine.retrieval.read_record
    for _ in range(20):
        response = _wire(read(request, authority=authority))
        assert response["record"]["revision_id"] == request.expected_revision_id
        assert response["start_byte"] == offset
        chunk = response["content"]["text"].encode("utf-8")
        assert 0 < len(chunk) <= request.target_bytes
        chunks.append(chunk)
        offset += len(chunk)
        assert response["end_byte"] == offset
        if response["complete"]:
            assert response["next_cursor"] is None
            break
        assert response["next_cursor"] is not None
        request = replace(request, cursor=response["next_cursor"])
    else:
        pytest.fail("retained fixture did not finish within twenty chunks")
    assert b"".join(chunks) == expected
    if len(expected) > request.target_bytes:
        assert len(chunks) > 1


def _retained_source_evidence(
    engine: BrainEngine, source_id: str
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], dict[str, bytes]]:
    with open_local_database_read_only(engine.profile) as connection:
        revisions = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM source_revisions WHERE source_id=? ORDER BY sequence",
                (source_id,),
            )
        ]
        privacy = [
            tuple(row)
            for row in connection.execute(
                "SELECT r.capture_id,c.privacy_json,p.effective_privacy_json "
                "FROM source_revisions r JOIN captures c USING(capture_id) "
                "JOIN source_revision_privacy p USING(capture_id) "
                "WHERE r.source_id=? ORDER BY r.sequence",
                (source_id,),
            )
        ]
        evidence = {
            row["source_path"]: bytes(row["source_bytes"])
            for row in connection.execute(
                "SELECT source_path,source_bytes FROM source_revisions WHERE source_id=?",
                (source_id,),
            )
        }
    assert all((engine.profile.root / path).read_bytes() == data for path, data in evidence.items())
    return revisions, privacy, evidence


@pytest.mark.parametrize("recovery", ["reopen", "rebuild", "portable6"])
def test_withdrawn_source_history_and_visibility_survive_recovery(
    tmp_path: Path, recovery: str
) -> None:
    tmp_path.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    owner = EffectiveAuthority("synthetic-owner", "owner-session", frozenset(), None, owner=True)
    original = replace(
        _public_submission(engine.tasks),
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.WORK,
            reason=PrivacyReason.POLICY_WORK,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=True),
        ),
    )
    texts = [
        "recovery nebula first retained 漢字🙂\n" * 500,
        "recovery nebula second retained café\n",
        "recovery nebula third retained Ω\n",
    ]
    ids: list[str] = []
    head = None
    for sequence, text in enumerate(texts):
        capture = replace(original, payload=TextPayload(text))
        accepted = engine.sources.submit_revision(
            SourceRevisionSubmission(
                capture=capture,
                namespace=dict(
                    connector_name="synthetic",
                    connection_id="recovery",
                    resource_id="work",
                    external_id="target",
                ),
                revision_key=str(sequence),
                canonical_sha256=capture.request_sha256(),
                expected_head=head,
                ordering={"kind": "unordered"}
                if sequence == 0
                else {"kind": "predecessor", "revision_key": str(sequence - 1)},
                expected_control_epoch=0,
            )
        )
        assert accepted.outcome == "captured"
        assert accepted.capture_id is not None
        head = accepted.capture_id
        ids.append(head)
    assert accepted.source_id is not None
    source_id = accepted.source_id
    control_text = "recovery nebula unrelated visible control 漢字"
    control = engine.capture.submit(
        replace(
            original,
            payload=TextPayload(control_text),
            delivery_id="delivery.recovery.control",
        )
    ).capture_id
    inspection = engine.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    local = EffectiveAuthority(
        "synthetic-local-agent",
        "local-agent-session",
        frozenset({"search", "content-read", "history-read"}),
        None,
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
    )
    external = replace(
        local,
        principal_id="synthetic-external-agent",
        session_id="external-agent-session",
        egress_mode=EgressMode.EXTERNAL_PROVIDER,
        provider_id="synthetic-provider",
        consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        brain_id=inspection.destination_brain_id,
        issuer_epoch=inspection.issuer_epoch,
    )
    search = SearchPageRequest(query="recovery nebula", limit=10)
    current = RecordReadRequest(record_id=ids[0], expected_revision_id=ids[-1], target_bytes=4096)
    history = HistoryListRequest(record_id=ids[0])
    for reader in (owner, local, external):
        _read_exact(engine, current, reader, texts[-1].encode("utf-8"), history=False)
        entries = _wire(engine.history.list_history(history, authority=reader))["entries"]
        assert [entry["revision_id"] for entry in entries] == ids[::-1]
        assert [entry["predecessor_revision_id"] for entry in entries] == [ids[1], ids[0], None]
        for capture_id, text in zip(ids, texts, strict=True):
            _read_exact(
                engine,
                replace(current, expected_revision_id=capture_id),
                reader,
                text.encode("utf-8"),
                history=True,
            )
        assert {
            row["record_id"]
            for row in _wire(engine.retrieval.search_page(search, authority=reader))["results"]
        } == {ids[-1], control}
    retained = _retained_source_evidence(engine, source_id)
    assert all(len(part) == 3 for part in retained)
    request = SourceWithdrawRequest(
        operation_id="withdraw.recovery",
        source_id=source_id,
        expected_head=ids[-1],
        expected_lifecycle_version=inspection.lifecycle_version,
        brain_id=inspection.destination_brain_id,
        issuer_epoch=inspection.issuer_epoch,
        reason_code="owner_choice",
    )
    receipt = engine.sources.withdraw(request, authority=owner)
    retired = engine.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
    assert retired.lifecycle == "retired"
    assert retired.availability == "missing"
    assert retired.lifecycle_version == inspection.lifecycle_version + 1

    if recovery == "reopen":
        engine = BrainEngine.open(engine.profile)
    elif recovery == "rebuild":
        assert engine.portability.rebuild_index().status == "rebuilt"
    else:
        export, restored = tmp_path / "export", tmp_path / "restored"
        assert (
            engine.portability.export(export, export_id="export_" + str(uuid4())).schema_version
            == 6
        )
        assert (
            engine.portability.import_clean(
                export, restored, import_id="import_" + str(uuid4())
            ).schema_version
            == 6
        )
        engine = BrainEngine.open(_profile(restored, validated_portable_snapshot(restored)))

    assert (
        engine.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)
        == retired
    )
    assert engine.sources.withdraw(request, authority=owner) == receipt
    listing = replace(history, limit=1)
    entries = []
    for index in range(3):
        page = _wire(engine.history.list_history(listing, authority=owner))
        assert len(page["entries"]) == 1
        entries.extend(page["entries"])
        assert page["complete"] is (index == 2)
        if index < 2:
            assert page["next_cursor"] is not None
            listing = replace(listing, cursor=page["next_cursor"])
        else:
            assert page["next_cursor"] is None
    assert [entry["revision_id"] for entry in entries] == ids[::-1]
    assert [entry["predecessor_revision_id"] for entry in entries] == [ids[1], ids[0], None]
    assert all(entry["lifecycle"] == "retired" for entry in entries)
    assert all(entry["availability"] == "missing" for entry in entries)
    for capture_id, text in zip(ids, texts, strict=True):
        reading = replace(current, expected_revision_id=capture_id)
        _read_exact(engine, reading, owner, text.encode("utf-8"), history=True)
        for reader in (local, external):
            with pytest.raises(T03Error, match="^not_found$"):
                engine.history.read_history(reading, authority=reader)
    for reader in (owner, local, external):
        for capture_id in ids:
            with pytest.raises(T03Error, match="^not_found$"):
                engine.retrieval.read_record(
                    replace(current, record_id=capture_id), authority=reader
                )
        assert {
            row["record_id"]
            for row in _wire(engine.retrieval.search_page(search, authority=reader))["results"]
        } == {control}
        control_read = RecordReadRequest(record_id=control, expected_revision_id=control)
        _read_exact(engine, control_read, reader, control_text.encode("utf-8"), history=False)
    for reader in (local, external):
        with pytest.raises(T03Error, match="^not_found$"):
            engine.history.list_history(history, authority=reader)
    assert {row.result_id for row in engine.retrieval.search(search.query)} == {control}
    assert all(engine.retrieval.fetch(capture_id) is None for capture_id in ids)
    assert all(engine.retrieval.read_page(capture_id) is None for capture_id in ids)
    assert _retained_source_evidence(engine, source_id) == retained


def test_schema11_runtime6_portable6_lifecycle_admission_roundtrip(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    assert tasks.sources is not None
    submission = _revision(tasks)
    accepted = tasks.sources.submit_revision(submission)
    assert accepted.source_id is not None and accepted.capture_id is not None
    owner = EffectiveAuthority("synthetic-owner", "owner-session", frozenset(), None, owner=True)
    inspection = tasks.sources.inspect(
        SourceInspectRequest(source_id=accepted.source_id), authority=owner
    )
    withdrawal = SourceWithdrawRequest(
        operation_id="withdraw.synthetic",
        source_id=accepted.source_id,
        expected_head=accepted.capture_id,
        expected_lifecycle_version=inspection.lifecycle_version,
        brain_id=inspection.destination_brain_id,
        issuer_epoch=inspection.issuer_epoch,
        reason_code="owner_choice",
    )
    receipt = tasks.sources.withdraw(withdrawal, authority=owner)
    export, restored, again = (tmp_path / name for name in ("export", "restored", "again"))
    assert tasks.portability.export(export, export_id="export_" + str(uuid4())).schema_version == 6
    snapshot = validated_portable_snapshot(export)
    assert snapshot.manifest["schema_version"] == 6
    assert snapshot.files.keys() >= V6_SIDECAR_PATHS
    assert (
        tasks.portability.import_clean(
            export, restored, import_id="import_" + str(uuid4())
        ).schema_version
        == 6
    )
    engine = BrainEngine.open(_profile(restored, validated_portable_snapshot(restored)))
    assert engine.sources.withdraw(withdrawal, authority=owner) == receipt
    assert engine.sources.submit_revision(submission) == accepted
    assert (
        engine.sources.inspect(
            SourceInspectRequest(source_id=accepted.source_id), authority=owner
        ).lifecycle
        == "retired"
    )
    engine.portability.export(again, export_id="export_" + str(uuid4()))
    second = validated_portable_snapshot(again)
    assert all(second.files[path] == snapshot.files[path] for path in V6_SIDECAR_PATHS)
    for path, data in snapshot.files.items():
        if path != "portable-manifest.json":
            assert second.files[path] == data


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "bool-version", "foreign-source", "revision-missing", "namespace-digest"],
)
def test_portable6_closed_authority_refuses_semantic_tampering(
    tmp_path: Path, mutation: str
) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    assert tasks.sources is not None
    tasks.sources.submit_revision(_revision(tasks))
    export = tmp_path / "export"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(export)
    files = {
        path: data for path, data in snapshot.files.items() if path != "portable-manifest.json"
    }
    path = (
        SOURCE_ADMISSION_PATH
        if mutation in {"revision-missing", "namespace-digest"}
        else SOURCE_LIFECYCLE_PATH
    )
    value = json.loads(files[path])
    if mutation == "missing":
        files.pop(path)
    else:
        if mutation == "extra":
            value["unexpected"] = True
        elif mutation == "bool-version":
            value["source_lifecycle_state"][0]["lifecycle_version"] = False
        elif mutation == "foreign-source":
            value["source_lifecycle_state"][0]["source_id"] = "source_foreign"
        elif mutation == "revision-missing":
            value["revision_admission"] = []
        elif mutation == "namespace-digest":
            value["source_namespaces"][0]["namespace_sha256"] = "0" * 64
        files[path] = canonical(value)
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v6(files, tenant_id=tasks.profile.tenant_id)


def test_portable5_cannot_silently_accept_source_authority(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    export = tmp_path / "export"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    files = {
        path: data
        for path, data in validated_portable_snapshot(export).files.items()
        if path != "portable-manifest.json"
    }
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v5(files, tenant_id=tasks.profile.tenant_id)


def test_portable6_import_retry_audits_lifecycle_authority(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    assert tasks.sources is not None
    tasks.sources.submit_revision(_revision(tasks))
    export, restored = tmp_path / "export", tmp_path / "restored"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    import_id = "import_" + str(uuid4())
    tasks.portability.import_clean(export, restored, import_id=import_id)
    assert tasks.portability.import_clean(export, restored, import_id=import_id).duplicate
    engine = BrainEngine.open(_profile(restored, validated_portable_snapshot(restored)))
    with engine._store.transaction() as connection:
        connection.execute("UPDATE source_lifecycle_state SET lifecycle_version=1")
    with pytest.raises(ValueError, match="source authority mismatch"):
        tasks.portability.import_clean(export, restored, import_id=import_id)


def test_portable6_managed_delivery_exact_replay_after_clean_restore(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    tasks = open_local_engine(profile)
    submission = _revision(tasks)
    with engine._store.connect() as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    binding = SourceRevisionBinding(
        destination_brain_id=identity["brain_id"],
        issuer_epoch=identity["issuer_epoch"],
        root_fingerprint="synthetic-root",
        accepted_source_id="synthetic-selection",
        namespace=submission.namespace,
    )
    delivery = SourceRevisionDelivery(
        binding=binding,
        submission=submission,
        expected_lifecycle_version=0,
        delivery_id="synthetic.delivery.one",
    )
    accepted = engine.sources.public_revision_sink(binding).submit(delivery)
    export, restored = tmp_path / "export", tmp_path / "restored"
    engine.portability.export(export, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(export)
    admission = json.loads(snapshot.files[SOURCE_ADMISSION_PATH])
    assert len(admission["managed_source_deliveries"]) == 1
    engine.portability.import_clean(export, restored, import_id="import_" + str(uuid4()))
    reopened = BrainEngine.open(_profile(restored, validated_portable_snapshot(restored)))
    assert reopened.sources.public_revision_sink(binding).submit(delivery) == accepted


@pytest.mark.parametrize(
    "mutation",
    [
        "submission-body",
        "submission-revision-key",
        "submission-dto-bool",
        "submission-control-float",
        "submission-order",
        "submission-delivery",
        "submission-extra",
        "submission-head",
        "binding-namespace",
        "binding-extra",
        "binding-missing",
        "binding-root-type",
        "binding-source-type",
        "binding-namespace-type",
        "binding-namespace-extra",
        "binding-epoch-bool",
        "binding-epoch-float",
        "envelope-lifecycle-bool",
        "envelope-lifecycle-float",
        "envelope-dto-bool",
        "envelope-dto-float",
        "receipt-extra",
        "receipt-control-bool",
        "receipt-control-float",
        "receipt-outcome",
        "source-delivery-link",
    ],
)
def test_portable6_managed_envelope_requires_exact_validated_intake_linkage(
    tmp_path: Path, mutation: str
) -> None:
    tmp_path.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    submission = _revision(engine.tasks)
    with open_local_database_read_only(engine.profile) as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    binding = SourceRevisionBinding(
        destination_brain_id=identity["brain_id"],
        issuer_epoch=identity["issuer_epoch"],
        root_fingerprint="synthetic-root",
        accepted_source_id="synthetic-selection",
        namespace=submission.namespace,
    )
    delivery = SourceRevisionDelivery(
        binding=binding,
        submission=submission,
        expected_lifecycle_version=0,
        delivery_id="synthetic.portable.linkage",
    )
    accepted = engine.sources.public_revision_sink(binding).submit(delivery)
    assert accepted.outcome == "captured"
    export = tmp_path / "export"
    engine.portability.export(export, export_id="export_" + str(uuid4()))
    files = {
        path: data
        for path, data in validated_portable_snapshot(export).files.items()
        if path != "portable-manifest.json"
    }
    validate_portable_file_set_v6(files, tenant_id=engine.profile.tenant_id)
    admission = json.loads(files[SOURCE_ADMISSION_PATH])
    managed = admission["managed_source_deliveries"][0]
    envelope = json.loads(base64.b64decode(managed["envelope_bytes"], validate=True))
    retained = json.loads(managed["receipt_json"])
    if mutation == "submission-body":
        envelope["submission"]["capture"]["payload"]["text"] = "forged synthetic retained body"
        envelope["submission"]["canonical_sha256"] = sha256(
            canonical(envelope["submission"]["capture"])
        ).hexdigest()
    elif mutation == "submission-revision-key":
        envelope["submission"]["revision_key"] = "forged-revision"
    elif mutation == "submission-dto-bool":
        envelope["submission"]["dto_version"] = True
    elif mutation == "submission-control-float":
        envelope["submission"]["expected_control_epoch"] = 0.0
    elif mutation == "submission-order":
        envelope["submission"]["ordering"] = {"kind": "predecessor", "revision_key": "forged"}
    elif mutation == "submission-delivery":
        envelope["submission"]["delivery_id"] = "forged-delivery"
    elif mutation == "submission-extra":
        envelope["submission"]["unexpected"] = "forged"
    elif mutation == "submission-head":
        forged = "capture_00000000-0000-4000-8000-000000000001"
        envelope["submission"]["expected_head"] = managed["expected_head"] = forged
    elif mutation == "binding-namespace":
        envelope["binding"]["namespace"]["external_id"] = "forged-item"
    elif mutation == "binding-extra":
        envelope["binding"]["unexpected"] = "forged"
    elif mutation == "binding-missing":
        envelope["binding"].pop("accepted_source_id")
    elif mutation == "binding-root-type":
        envelope["binding"]["root_fingerprint"] = False
    elif mutation == "binding-source-type":
        envelope["binding"]["accepted_source_id"] = 7
    elif mutation == "binding-namespace-type":
        envelope["binding"]["namespace"]["resource_id"] = 7
    elif mutation == "binding-namespace-extra":
        envelope["binding"]["namespace"]["unexpected"] = "forged"
    elif mutation == "binding-epoch-bool":
        envelope["binding"]["issuer_epoch"] = True
    elif mutation == "binding-epoch-float":
        envelope["binding"]["issuer_epoch"] = 1.0
    elif mutation == "envelope-lifecycle-bool":
        envelope["expected_lifecycle_version"] = False
    elif mutation == "envelope-lifecycle-float":
        envelope["expected_lifecycle_version"] = 0.0
    elif mutation == "envelope-dto-bool":
        envelope["dto_version"] = True
    elif mutation == "envelope-dto-float":
        envelope["dto_version"] = 1.0
    elif mutation == "receipt-extra":
        retained["source_receipt"]["unexpected"] = "forged"
    elif mutation == "receipt-control-bool":
        retained["source_receipt"]["control_epoch"] = False
    elif mutation == "receipt-control-float":
        retained["source_receipt"]["control_epoch"] = 0.0
    elif mutation == "receipt-outcome":
        retained["source_receipt"]["outcome"] = "history_only"
    elif mutation == "source-delivery-link":
        managed["source_delivery_id"] = admission["source_intakes"][0]["delivery_id"]
    else:
        pytest.fail("unhandled synthetic mutation")
    managed["receipt_json"] = json.dumps(retained, sort_keys=True)
    # Preserve numeric JSON types in adversarial bytes instead of normalizing
    # integral floats into integers through the canonical encoder.
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    managed["envelope_bytes"] = base64.b64encode(raw).decode("ascii")
    managed["envelope_sha256"] = sha256(raw).hexdigest()
    files[SOURCE_ADMISSION_PATH] = canonical(admission)
    # The envelope and archive sidecar hashes are valid; semantic linkage must
    # reject the forgery independently of those attacker-recomputed digests.
    with pytest.raises(PortableValidationError, match="source authority invalid"):
        validate_portable_file_set_v6(files, tenant_id=engine.profile.tenant_id)


def _observed_archive(
    tmp_path: Path,
) -> tuple[BrainEngine, SourceRevisionObservedDelivery, SourceRevisionDeliveryReceipt, Path]:
    tmp_path.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    submission = _revision(engine.tasks)
    capture = submission.capture.request_value()
    with open_local_database_read_only(engine.profile) as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    binding = SourceRevisionBinding(
        destination_brain_id=identity["brain_id"],
        issuer_epoch=identity["issuer_epoch"],
        root_fingerprint="synthetic-observed-root",
        accepted_source_id="synthetic-observed-selection",
        namespace=submission.namespace,
    )
    observation = SourceRevisionObservation(
        original_sha256=sha256(b"synthetic original bytes").hexdigest(),
        transformed_sha256=sha256(b"synthetic transformed bytes").hexdigest(),
        normalization_version="synthetic-normalization-v1",
        privacy_policy_version=submission.capture.privacy.policy_version,
        privacy_policy_sha256=sha256(canonical(capture["privacy"])).hexdigest(),
        admitted_payload_sha256=sha256(canonical(capture["payload"])).hexdigest(),
    )
    delivery = SourceRevisionObservedDelivery(
        binding=binding,
        submission=submission,
        expected_lifecycle_version=0,
        delivery_id="synthetic.portable.observed",
        observation=observation,
    )
    accepted = engine.sources.public_revision_sink(binding).submit(delivery)
    assert accepted.outcome == "captured"
    export = tmp_path / "export"
    engine.portability.export(export, export_id="export_" + str(uuid4()))
    return engine, delivery, accepted, export


def test_portable6_observed_delivery_exact_replay_after_clean_restore(tmp_path: Path) -> None:
    engine, delivery, accepted, export = _observed_archive(tmp_path)
    snapshot = validated_portable_snapshot(export)
    admission = json.loads(snapshot.files[SOURCE_ADMISSION_PATH])
    retained = admission["managed_source_deliveries"][0]
    assert base64.b64decode(retained["envelope_bytes"], validate=True) == delivery.custody_bytes()
    assert json.loads(delivery.custody_bytes())["dto_version"] == 2
    restored = tmp_path / "restored"
    engine.portability.import_clean(export, restored, import_id="import_" + str(uuid4()))
    reopened = BrainEngine.open(_profile(restored, validated_portable_snapshot(restored)))
    assert reopened.sources.public_revision_sink(delivery.binding).submit(delivery) == accepted
    again = tmp_path / "again"
    reopened.portability.export(again, export_id="export_" + str(uuid4()))
    assert (
        validated_portable_snapshot(again).files[SOURCE_ADMISSION_PATH]
        == snapshot.files[SOURCE_ADMISSION_PATH]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "observation-extra",
        "observation-missing",
        "observation-not-object",
        "observation-dto-bool",
        "observation-dto-unsupported",
        "original-type",
        "original-digest",
        "transformed-digest",
        "normalization-empty",
        "normalization-type",
        "normalization-nul",
        "normalization-oversized",
        "privacy-version-type",
        "privacy-version-mismatch",
        "privacy-digest-mismatch",
        "payload-digest-mismatch",
        "envelope-extra",
        "envelope-missing-observation",
        "envelope-downgrade",
        "envelope-unsupported-version",
        "submission-body",
    ],
)
def test_portable6_observed_envelope_rejects_invalid_attestation_and_linkage(
    tmp_path: Path, mutation: str
) -> None:
    engine, _delivery, _accepted, export = _observed_archive(tmp_path)
    files = {
        path: data
        for path, data in validated_portable_snapshot(export).files.items()
        if path != "portable-manifest.json"
    }
    admission = json.loads(files[SOURCE_ADMISSION_PATH])
    managed = admission["managed_source_deliveries"][0]
    envelope = json.loads(base64.b64decode(managed["envelope_bytes"], validate=True))
    observation = envelope["observation"]
    if mutation == "observation-extra":
        observation["unexpected"] = "forged"
    elif mutation == "observation-missing":
        observation.pop("original_sha256")
    elif mutation == "observation-not-object":
        envelope["observation"] = []
    elif mutation == "observation-dto-bool":
        observation["dto_version"] = True
    elif mutation == "observation-dto-unsupported":
        observation["dto_version"] = 2
    elif mutation == "original-type":
        observation["original_sha256"] = False
    elif mutation == "original-digest":
        observation["original_sha256"] = "G" * 64
    elif mutation == "transformed-digest":
        observation["transformed_sha256"] = "not-a-digest"
    elif mutation == "normalization-empty":
        observation["normalization_version"] = ""
    elif mutation == "normalization-type":
        observation["normalization_version"] = 7
    elif mutation == "normalization-nul":
        observation["normalization_version"] = "synthetic\x00version"
    elif mutation == "normalization-oversized":
        observation["normalization_version"] = "n" * 1025
    elif mutation == "privacy-version-type":
        observation["privacy_policy_version"] = 7
    elif mutation == "privacy-version-mismatch":
        observation["privacy_policy_version"] = "forged-policy"
    elif mutation == "privacy-digest-mismatch":
        observation["privacy_policy_sha256"] = "0" * 64
    elif mutation == "payload-digest-mismatch":
        observation["admitted_payload_sha256"] = "0" * 64
    elif mutation == "envelope-extra":
        envelope["unexpected"] = "forged"
    elif mutation == "envelope-missing-observation":
        envelope.pop("observation")
    elif mutation == "envelope-downgrade":
        envelope["dto_version"] = 1
    elif mutation == "envelope-unsupported-version":
        envelope["dto_version"] = 3
    elif mutation == "submission-body":
        envelope["submission"]["capture"]["payload"]["text"] = "forged observed synthetic body"
        envelope["submission"]["canonical_sha256"] = sha256(
            canonical(envelope["submission"]["capture"])
        ).hexdigest()
        observation["admitted_payload_sha256"] = sha256(
            canonical(envelope["submission"]["capture"]["payload"])
        ).hexdigest()
    else:
        pytest.fail("unhandled synthetic observed mutation")
    raw = canonical(envelope)
    managed["envelope_bytes"] = base64.b64encode(raw).decode("ascii")
    managed["envelope_sha256"] = sha256(raw).hexdigest()
    files[SOURCE_ADMISSION_PATH] = canonical(admission)
    with pytest.raises(PortableValidationError, match="source authority invalid"):
        validate_portable_file_set_v6(files, tenant_id=engine.profile.tenant_id)


def test_portable6_pending_managed_envelope_refuses_export(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    with engine._store.transaction() as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        connection.execute(
            "INSERT INTO managed_source_deliveries("
            "delivery_id,envelope_sha256,envelope_bytes,source_id,destination_brain_id,issuer_epoch,"
            "expected_head,expected_lifecycle_version,source_delivery_id,receipt_json) "
            "VALUES(?,?,?,NULL,?,?,NULL,0,?,NULL)",
            (
                "synthetic.pending",
                "0" * 64,
                b"{}",
                identity["brain_id"],
                identity["issuer_epoch"],
                "synthetic.source.delivery",
            ),
        )
    with pytest.raises(ValueError, match="^ingestion_pending$"):
        engine.portability.export(tmp_path / "refused", export_id="export_" + str(uuid4()))
    assert not (tmp_path / "refused").exists()


@pytest.mark.parametrize(
    "stage",
    [
        "base_materialized",
        "source_history_restored",
        "privacy_evidence_restored",
        "search_rederived",
    ],
)
def test_portable6_restore_crash_rolls_back_authority(tmp_path: Path, stage: str) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    assert tasks.sources is not None
    submission = _revision(tasks)
    accepted = tasks.sources.submit_revision(submission)
    export, restore = tmp_path / "export", tmp_path / "restore"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    shutil.copytree(export, restore)
    snapshot = validated_portable_snapshot(restore)

    def crash(actual: str) -> None:
        if actual == stage:
            raise RuntimeError("synthetic restore crash")

    with pytest.raises(RuntimeError, match="synthetic restore crash"):
        restore_portable_v5_root(
            restore,
            snapshot=snapshot,
            expected_root_identity=snapshot.root_identity,
            checkpoint=crash,
        )
    with sqlite3.connect(restore / ".open-brain/state/phase1.sqlite3") as connection:
        for table in (
            "captures",
            "logical_sources",
            "source_lifecycle_state",
            "source_intakes",
            "source_namespaces",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    # The orchestrator retries into a fresh hidden stage. It never reseeds an
    # existing SQLite issuer identity, even when its content transaction rolled back.
    retry = tmp_path / "retry"
    shutil.copytree(export, retry)
    retry_snapshot = validated_portable_snapshot(retry)
    restored = restore_portable_v5_root(
        retry, snapshot=retry_snapshot, expected_root_identity=retry_snapshot.root_identity
    )
    engine = BrainEngine.open(restored.profile)
    assert engine.sources.submit_revision(submission) == accepted
