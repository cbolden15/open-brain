"""Independent retained text/import rows, loaded before the real schema-seven cutover."""

import json
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload, local_schema
from open_brain_engine.engine.historical_contracts import (
    HistoricalBaselineRequest,
    HistoricalDestination,
    RetainedCaptureEvidence,
)
from open_brain_engine.engine.historical_contracts_v2 import (
    HistoricalBaselineRequestV2,
    RetainedCaptureEvidenceV2,
)
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.portable.v1 import validate_portable_write
from open_brain_engine.portable.v8_capture_metadata import CAPTURE_COLUMNS

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission
from packages.app.tests.unit.engine.test_historical_tasks import _current_cas
from packages.engine.tests.contract.test_portable_brain_v1 import _capture


def retained_import_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BrainEngine:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])
        with closing(local_schema.open_local_database(profile)) as connection:
            for number, path in ((1, "import"), (2, "public_job")):
                # Construct old rows directly from the frozen Portable record fixture.
                # No active capture operation or current-state relabeling occurs.
                record: dict[str, Any] = _capture("text", number)
                record["tenant_id"] = profile.tenant_id
                record["actor_id"] = profile.owner_actor_id
                record["space_id"] = None
                record["role_claim"].update(
                    tenant_id=profile.tenant_id, actor_id=profile.owner_actor_id
                )
                record["trust"]["assessor_actor_id"] = profile.owner_actor_id
                record["privacy"] = PrivacyDecision.create(
                    tier=PrivacyTier.PUBLIC,
                    reason=PrivacyReason.POLICY_PUBLIC,
                    policy_version="synthetic.retained.v1",
                    authority=Authority(cloud=number == 2, external_egress=number == 2),
                ).to_dict()
                relative = "sources/captures/2026/08/" + record["capture_id"] + ".json"
                raw = canonical(record)
                validate_portable_write(relative, raw, profile.tenant_id)
                target = profile.root / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(raw)
                row = dict.fromkeys(CAPTURE_COLUMNS)
                row.update(
                    delivery_id=f"synthetic.retained.{number}",
                    request_sha256=sha256(f"synthetic.old.request.{number}".encode()).hexdigest(),
                    capture_id=record["capture_id"],
                    accepted_receipt_id=record["receipt_refs"][0]["receipt_id"],
                    payload_family="text",
                    payload_json=canonical(record["payload"]),
                    search_text=record["payload"]["text"],
                    source_origin=record["source"]["origin"],
                    source_reference=record["source"]["reference"],
                    action="quick",
                    accepted_at=record["accepted_at"],
                    stage=3,
                    source_path=relative,
                    enrichment_state="pending_enrichment",
                    actor_id=record["actor_id"],
                    role_claim_json=canonical(record["role_claim"]).decode(),
                    privacy_json=canonical(record["privacy"]).decode(),
                    provenance_json=canonical(record["provenance"]).decode(),
                    submission_path=path,
                )
                connection.execute(
                    f"INSERT INTO captures ({','.join(CAPTURE_COLUMNS)}) "
                    f"VALUES ({','.join('?' for _ in CAPTURE_COLUMNS)})",
                    tuple(row[key] for key in CAPTURE_COLUMNS),
                )
            connection.commit()
    coordinate_local_migration(profile)
    from open_brain_engine.engine.portability_ports import LocalTenantStorage
    from open_brain_engine.engine.portable_v5_restore import _rederive_search

    files = dict(
        LocalTenantStorage(profile.root, profile.tenant_id, profile.root_identity).portable_files()
    )
    with closing(local_schema.open_local_database(profile)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _rederive_search(connection, profile, portable_files=files)
        connection.commit()
    return BrainEngine.open(profile)


def portable5_import_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: str | None = None,
) -> BrainEngine:
    archive = tmp_path / "portable5"
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 9)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:9])
        engine = BrainEngine.open(compile_single_user_local(tmp_path / "old"))
        privacy = PrivacyDecision.create(
            tier=PrivacyTier.PUBLIC,
            reason=PrivacyReason.POLICY_PUBLIC,
            policy_version="synthetic.old.public.v1",
            authority=Authority(False, False),
        )
        submission = _public_submission(engine.tasks)
        engine.capture.submit(
            replace(
                submission,
                privacy=privacy,
                payload=TextPayload(text) if text is not None else submission.payload,
            )
        )
        engine.portability.export(archive, export_id="export_" + str(uuid4()))
    control = BrainEngine.open(compile_single_user_local(tmp_path / "control"))
    control.portability.import_clean(
        archive, tmp_path / "brain", import_id="import_" + str(uuid4())
    )
    from open_brain.profile import open_existing_single_user_local

    return BrainEngine.open(open_existing_single_user_local(tmp_path / "brain"))


def baseline_v1(engine: BrainEngine, *, direct_alias: bool = False) -> HistoricalBaselineRequest:
    with engine._store.transaction() as connection:
        row = connection.execute(
            "SELECT r.*,c.privacy_json FROM source_revisions r "
            "JOIN captures c USING(capture_id) "
            "WHERE c.submission_path='import'"
        ).fetchone()
        delivery = "synthetic.retained.1"
        if direct_alias:
            # A distinct capture commitment is already supported by the frozen V1 helper.
            connection.execute(
                "UPDATE source_revisions SET request_sha256=? WHERE capture_id=?",
                ("a" * 64, row["capture_id"]),
            )
            connection.execute(
                "UPDATE source_aliases SET evidence_sha256=? WHERE delivery_id=?",
                ("a" * 64, delivery),
            )
            row = connection.execute(
                "SELECT r.*,c.privacy_json FROM source_revisions r "
                "JOIN captures c USING(capture_id) "
                "WHERE c.submission_path='import'"
            ).fetchone()
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    source = _current_cas(engine, row["source_id"])
    destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
    observed = _baseline_observation(engine, row, source, destination)
    privacy = PrivacyDecision.from_dict(json.loads(row["privacy_json"]))
    return HistoricalBaselineRequest(
        operation_id="baseline.synthetic.import",
        destination=destination,
        source_cas=source,
        retained_original=RetainedCaptureEvidence(
            capture_id=row["capture_id"],
            source_sha256=row["source_sha256"],
            retained_delivery_id=delivery,
            retained_request_sha256=row["request_sha256"],
            privacy_sha256=sha256(canonical(privacy.to_dict())).hexdigest(),
        ),
        observed_delivery=observed,
        expected_claim_generation=0,
    )


def _baseline_observation(
    engine: BrainEngine, row: Any, source: Any, destination: HistoricalDestination
) -> SourceRevisionObservedDelivery:
    payload = json.loads(row["source_bytes"])["payload"]
    privacy = PrivacyDecision.from_dict(json.loads(row["privacy_json"]))
    capture = replace(
        _public_submission(engine.tasks), payload=TextPayload(payload["text"]), privacy=privacy
    )
    namespace = dict(
        connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
    )
    observed = SourceRevisionObservedDelivery(
        binding=SourceRevisionBinding(
            destination_brain_id=destination.brain_id,
            issuer_epoch=destination.issuer_epoch,
            root_fingerprint="synthetic.root",
            accepted_source_id="synthetic.selection",
            namespace=namespace,
        ),
        submission=SourceRevisionSubmission(
            namespace=namespace,
            capture=capture,
            revision_key="synthetic.revision",
            canonical_sha256=capture.request_sha256(),
            expected_head=source.expected_head,
            ordering={"kind": "unordered"},
            expected_control_epoch=source.expected_control_epoch,
        ),
        expected_lifecycle_version=source.expected_lifecycle_version,
        delivery_id="synthetic.observation",
        observation=SourceRevisionObservation(
            original_sha256=sha256(b"synthetic.raw").hexdigest(),
            transformed_sha256=sha256(payload["text"].encode()).hexdigest(),
            normalization_version="synthetic.text.v1",
            privacy_policy_version=privacy.policy_version,
            privacy_policy_sha256=sha256(canonical(privacy.to_dict())).hexdigest(),
            admitted_payload_sha256=sha256(canonical(payload)).hexdigest(),
        ),
    )
    return observed


def evidence_v2(engine: BrainEngine, capture_id: str) -> RetainedCaptureEvidenceV2:
    with engine._store.connect() as connection:
        row = connection.execute(
            "SELECT r.*,c.delivery_id,c.request_sha256 AS capture_request,c.privacy_json,"
            "c.accepted_receipt_id,a.evidence_sha256 FROM source_revisions r "
            "JOIN captures c USING(capture_id) "
            "LEFT JOIN source_aliases a ON a.delivery_id=c.delivery_id WHERE r.capture_id=?",
            (capture_id,),
        ).fetchone()
    return RetainedCaptureEvidenceV2(
        capture_id=capture_id,
        source_sha256=row["source_sha256"],
        retained_delivery_id=row["delivery_id"],
        capture_request_sha256=row["capture_request"],
        revision_request_sha256=row["request_sha256"],
        alias_evidence_sha256=row["evidence_sha256"],
        correspondence_scheme=(
            "portable_import_projection_v1"
            if row["request_sha256"] is None and row["evidence_sha256"] is None
            else "direct_revision_alias"
            if row["request_sha256"] == row["evidence_sha256"]
            else "schema_seven_alias"
        ),
        privacy_sha256=sha256(canonical(json.loads(row["privacy_json"]))).hexdigest(),
        accepted_receipt_id=row["accepted_receipt_id"] if row["request_sha256"] is None else None,
    )


def baseline_v2(engine: BrainEngine, *, direct_alias: bool = False) -> HistoricalBaselineRequestV2:
    if direct_alias:
        baseline_v1(engine, direct_alias=True)
    with engine._store.connect() as connection:
        row = connection.execute(
            "SELECT r.*,c.privacy_json FROM source_revisions r "
            "JOIN captures c USING(capture_id) WHERE c.submission_path='import'"
        ).fetchone()
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    source = _current_cas(engine, row["source_id"])
    destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
    return HistoricalBaselineRequestV2(
        operation_id="baseline.synthetic.import",
        destination=destination,
        source_cas=source,
        observed_delivery=_baseline_observation(engine, row, source, destination),
        retained_original=evidence_v2(engine, row["capture_id"]),
        expected_claim_generation=0,
    )
