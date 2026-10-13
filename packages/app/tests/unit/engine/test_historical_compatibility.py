"""Frozen refusals and versioned retained-import compatibility."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.historical_admission import (
    HistoricalConsentSnapshot,
    verify_historical_baseline_evidence,
    verify_historical_relation_payload,
    verify_retained_capture,
)
from open_brain_engine.engine.historical_contracts_v2 import HistoricalCopyRelationRequestV2
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from ._historical_compatibility_fixtures import (
    baseline_v1,
    baseline_v2,
    evidence_v2,
    portable5_import_engine,
    retained_import_engine,
)


def test_supported_portable5_import_has_no_transport_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hashlib import sha256

    from open_brain_engine.engine.historical_contracts import RetainedCaptureEvidence

    engine = portable5_import_engine(tmp_path, monkeypatch)
    with engine._store.connect() as connection:
        row = connection.execute(
            "SELECT r.*,c.delivery_id,c.request_sha256 AS capture_request "
            "FROM source_revisions r JOIN captures c USING(capture_id)"
        ).fetchone()
        assert row["request_sha256"] is None
        assert row["revision_key"] is None
        assert row["ordering_json"] is None
        assert connection.execute("SELECT count(*) FROM source_aliases").fetchone()[0] == 0
        assert (
            row["delivery_id"] == "import.capture." + sha256(row["capture_id"].encode()).hexdigest()
        )
        assert row["capture_request"] == row["source_sha256"]
        witness = RetainedCaptureEvidence(
            capture_id=row["capture_id"],
            source_sha256=row["source_sha256"],
            retained_delivery_id=row["delivery_id"],
            retained_request_sha256=row["capture_request"],
            privacy_sha256="0" * 64,
        )
        with pytest.raises(SharingError, match="binding_mismatch"):
            verify_retained_capture(connection, engine.profile, witness, row["source_id"])


def test_v2_supported_portable5_projection_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hashlib import sha256

    from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
    from open_brain_engine.engine.historical_admission_v2 import (
        verify_retained_capture as verify_v2,
    )
    from open_brain_engine.engine.historical_contracts_v2 import RetainedCaptureEvidenceV2

    engine = portable5_import_engine(tmp_path, monkeypatch)
    with engine._store.connect() as connection:
        row = connection.execute(
            "SELECT r.*,c.delivery_id,c.request_sha256 AS capture_request,"
            "c.accepted_receipt_id,c.privacy_json FROM source_revisions r "
            "JOIN captures c USING(capture_id)"
        ).fetchone()
        evidence = RetainedCaptureEvidenceV2(
            capture_id=row["capture_id"],
            source_sha256=row["source_sha256"],
            retained_delivery_id=row["delivery_id"],
            capture_request_sha256=row["capture_request"],
            revision_request_sha256=None,
            alias_evidence_sha256=None,
            correspondence_scheme="portable_import_projection_v1",
            accepted_receipt_id=row["accepted_receipt_id"],
            privacy_sha256=sha256(canonical(json.loads(row["privacy_json"]))).hexdigest(),
        )
        assert (
            verify_v2(connection, engine.profile, evidence, row["source_id"])["capture_id"]
            == row["capture_id"]
        )


def linked_v2(
    engine: BrainEngine, *, perform_link: bool = True,
) -> tuple[HistoricalCopyRelationRequestV2, HistoricalConsentSnapshot, EffectiveAuthority]:
    from open_brain_engine.core.models import PrivacyTier
    from open_brain_engine.engine.consent_contracts import ProviderConsentState
    from open_brain_engine.engine.historical_admission import HistoricalConsentSnapshot
    from open_brain_engine.engine.historical_contracts_v2 import (
        HistoricalClaimRequestV2,
        HistoricalCopyRelationRequestV2,
    )
    from open_brain_engine.engine.historical_tasks import (
        adopt_historical_baseline,
        link_historical_copy,
        register_historical_claim,
    )
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    from .test_historical_tasks import _current_cas

    request = baseline_v2(engine)
    with engine._store.connect() as connection:
        generation = connection.execute(
            "SELECT generation FROM historical_registry_state"
        ).fetchone()[0]
        row = connection.execute(
            "SELECT r.source_id,c.capture_id FROM source_revisions r "
            "JOIN captures c USING(capture_id) WHERE c.submission_path='public_job'"
        ).fetchone()
    request = replace(request, expected_claim_generation=generation)
    if row is None:
        from open_brain_engine.core.models import Authority

        from .test_foundation_contracts import _public_submission

        privacy = replace(
            request.observed_delivery.submission.capture.privacy,
            authority=Authority(cloud=True, external_egress=True),
        )
        copy = engine.capture.submit(
            replace(
                _public_submission(engine.tasks, delivery_id="synthetic.approved.copy"),
                payload=request.observed_delivery.submission.capture.payload,
                privacy=privacy,
            )
        )
        with engine._store.connect() as connection:
            row = connection.execute(
                "SELECT source_id,capture_id FROM source_revisions WHERE capture_id=?",
                (copy.capture_id,),
            ).fetchone()
    claim = HistoricalClaimRequestV2(
        operation_id="claim.synthetic.copy",
        destination=request.destination,
        source_cas=request.source_cas,
        capture_source_cas=_current_cas(engine, row["source_id"]),
        retained_capture=evidence_v2(engine, row["capture_id"]),
        claim_role="historical_copy",
        expected_claim_generation=generation + 1,
    )
    relation = HistoricalCopyRelationRequestV2(
        operation_id="relation.synthetic.copy",
        destination=request.destination,
        source_cas=request.source_cas,
        copy_source_cas=claim.capture_source_cas,
        baseline_operation_id=request.operation_id,
        copy_claim_operation_id=claim.operation_id,
        retained_copy=claim.retained_capture,
        approval_evidence_sha256="d" * 64,
        provider_ids=("openai",),
        expected_relation_version=0,
        expected_claim_generation=generation + 2,
    )
    consent = (
        ProviderConsentState()
        .grant(
            owner=True,
            provider_id="openai",
            allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
            operation_id="consent.synthetic",
            decided_at="2026-10-03T00:00:00Z",
            consent_id_factory=lambda: "consent_" + "a" * 32,
        )
        .state
    )
    snapshot = HistoricalConsentSnapshot(request.destination, consent)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "owner", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        kwargs: dict[str, Any] = dict(
            authority=owner, admission=admission, validate_before_write=lambda: None
        )
        adopt_historical_baseline(engine.profile, request, **kwargs)
        register_historical_claim(engine.profile, claim, **kwargs)
        if perform_link:
            receipt = link_historical_copy(
                engine.profile, relation, load_consent=lambda: snapshot, **kwargs
            )
            assert (
                link_historical_copy(
                    engine.profile, relation, load_consent=lambda: snapshot, **kwargs
                )
                == receipt
            )
    return relation, snapshot, owner


def test_v2_links_cloud_copy_without_recapture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.sharing import EligibilityMode, sharing_eligible

    engine = retained_import_engine(tmp_path, monkeypatch)
    with engine._store.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM captures")]
    relation, _, _ = linked_v2(engine)
    with engine._store.connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM captures")] == before
        assert sharing_eligible(
            connection,
            relation.retained_copy.capture_id,
            mode=EligibilityMode.EXTERNAL_READ,
            profile=engine.profile,
            provider_id="openai",
            brain_id=relation.destination.brain_id,
            issuer_epoch=relation.destination.issuer_epoch,
        )
        assert not sharing_eligible(
            connection,
            relation.retained_copy.capture_id,
            mode=EligibilityMode.EXTERNAL_READ,
            profile=engine.profile,
            provider_id="anthropic",
            brain_id=relation.destination.brain_id,
            issuer_epoch=relation.destination.issuer_epoch,
        )


@pytest.mark.parametrize("direct_alias", [False, True])
def test_v2_admits_retained_import_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direct_alias: bool
) -> None:
    from open_brain_engine.engine.historical_admission_v2 import (
        verify_historical_baseline_evidence as verify_v2,
    )

    engine = retained_import_engine(tmp_path, monkeypatch)
    request = baseline_v2(engine, direct_alias=direct_alias)
    with engine._store.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM captures")]
        verify_v2(connection, engine.profile, request)
        assert [tuple(row) for row in connection.execute("SELECT * FROM captures")] == before


def test_frozen_v1_refuses_genuine_retained_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = retained_import_engine(tmp_path, monkeypatch)
    request = baseline_v1(engine, direct_alias=True)
    with engine._store.connect() as connection:
        verify_retained_capture(
            connection, engine.profile, request.retained_original, request.source_cas.source_id
        )
        with pytest.raises(SharingError, match="binding_mismatch"):
            verify_historical_baseline_evidence(connection, engine.profile, request)


def test_frozen_v1_refuses_schema_seven_alias_transform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = retained_import_engine(tmp_path, monkeypatch)
    request = baseline_v1(engine)
    with (
        engine._store.connect() as connection,
        pytest.raises(SharingError, match="binding_mismatch"),
    ):
        verify_retained_capture(
            connection, engine.profile, request.retained_original, request.source_cas.source_id
        )


def test_frozen_v1_refuses_cloud_enabled_retained_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = retained_import_engine(tmp_path, monkeypatch)
    request = baseline_v1(engine)
    with engine._store.connect() as connection:
        copy = json.loads(
            connection.execute(
                "SELECT source_bytes FROM source_revisions r JOIN captures c USING(capture_id) "
                "WHERE c.submission_path='public_job'"
            ).fetchone()[0]
        )
    with pytest.raises(SharingError, match="unsupported_capability"):
        verify_historical_relation_payload(request, copy)


@pytest.mark.parametrize(
    "damage",
    [
        "stage",
        "submission_path",
        "delivery_id",
        "request_sha256",
        "source_path",
        "payload_json",
        "privacy_json",
        "provenance_json",
        "role_claim_json",
        "actor_id",
        "intent",
        "capture_why",
        "accepted_at",
        "source_origin",
        "source_reference",
        "accepted_receipt_id",
        "revision_request",
        "revision_key",
        "ordering_json",
        "alias",
        "source_id",
        "noncanonical",
        "receipt_missing",
        "receipt_duplicate",
        "receipt_subject",
    ],
)
def test_import_projection_refuses_each_independent_witness_damage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    from hashlib import sha256

    from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
    from open_brain_engine.engine.historical_admission_v2 import verify_portable_import_projection

    engine = portable5_import_engine(tmp_path, monkeypatch)
    with engine._store.connect() as connection:
        capture = dict(connection.execute("SELECT * FROM captures").fetchone())
        revision = dict(connection.execute("SELECT * FROM source_revisions").fetchone())
    evidence = evidence_v2(engine, capture["capture_id"])
    raw = bytes(revision["source_bytes"])
    capture_source_id = revision["source_id"]
    alias = damage == "alias"
    if damage in ("revision_request", "revision_key", "ordering_json"):
        revision["request_sha256" if damage == "revision_request" else damage] = "0" * 64
    elif damage == "source_id":
        revision["source_id"] = "source_other"
    elif damage in ("noncanonical", "receipt_missing", "receipt_duplicate", "receipt_subject"):
        record = json.loads(raw)
        if damage == "receipt_missing":
            record["receipt_refs"] = []
        elif damage == "receipt_duplicate":
            record["receipt_refs"].append(dict(record["receipt_refs"][0]))
        elif damage == "receipt_subject":
            record["receipt_refs"][0]["subject_id"] = "capture_other"
        raw = (
            json.dumps(record, indent=2).encode() if damage == "noncanonical" else canonical(record)
        )
        digest = sha256(raw).hexdigest()
        evidence = replace(evidence, source_sha256=digest, capture_request_sha256=digest)
        capture["request_sha256"] = revision["source_sha256"] = digest
    elif damage != "alias":
        capture[damage] = (
            2 if damage == "stage" else (b"{}" if damage == "payload_json" else "wrong")
        )
    with pytest.raises(SharingError, match="binding_mismatch"):
        verify_portable_import_projection(
            evidence,
            capture,
            revision,
            alias,
            raw,
            engine.profile.tenant_id,
            capture_source_id,
        )


def test_wrong_direct_scheme_does_not_fall_back_to_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.historical_admission_v2 import (
        verify_retained_capture as verify_v2,
    )

    engine = portable5_import_engine(tmp_path, monkeypatch)
    with engine._store.connect() as connection:
        row = connection.execute("SELECT * FROM source_revisions").fetchone()
        evidence = evidence_v2(engine, row["capture_id"])
        wrong = replace(
            evidence,
            correspondence_scheme="direct_revision_alias",
            revision_request_sha256=evidence.capture_request_sha256,
            alias_evidence_sha256=evidence.capture_request_sha256,
            accepted_receipt_id=None,
        )
        with pytest.raises(SharingError, match="binding_mismatch"):
            verify_v2(connection, engine.profile, wrong, row["source_id"])


@pytest.mark.parametrize(
    "stage",
    [
        "historical_baseline_pending",
        "historical_registry_advanced",
        "historical_sql_projected",
        "historical_sql_committed",
        "historical_recovery_complete",
    ],
)
def test_import_projection_baseline_recovers_each_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from open_brain_engine.engine.historical_tasks import adopt_historical_baseline
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    engine = portable5_import_engine(tmp_path, monkeypatch)
    request = baseline_v2(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "owner", frozenset(), None, owner=True
    )

    def crash(observed: str) -> None:
        if observed == stage:
            raise RuntimeError("synthetic interruption")

    with exclusive_runtime_admission(engine.profile) as admission:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            adopt_historical_baseline(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                checkpoint=crash,
            )
        first = adopt_historical_baseline(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        assert (
            adopt_historical_baseline(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == first
        )
    with engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_aliases").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM historical_operations").fetchone()[0] == 1
