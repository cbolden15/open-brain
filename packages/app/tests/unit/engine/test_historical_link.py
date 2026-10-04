"""Eligible linking reconciles existing synthetic copies, not new approval events."""

import json
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.consent_contracts import ProviderConsentState
from open_brain_engine.engine.historical_admission import HistoricalConsentSnapshot
from open_brain_engine.engine.historical_contracts import (
    HistoricalClaimRequest,
    HistoricalCopyRelationRequest,
    RetainedCaptureEvidence,
)
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_tasks import (
    adopt_historical_baseline,
    link_historical_copy,
    register_historical_claim,
    revoke_historical_copy,
)
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, RecordReadRequest, T03Error

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission
from packages.app.tests.unit.engine.test_historical_baseline import _baseline
from packages.app.tests.unit.engine.test_historical_revocation import (
    _request as _revocation_request,
)
from packages.app.tests.unit.engine.test_historical_tasks import _current_cas
from packages.app.tests.unit.engine.test_paging import external_authority


def _link_request(
    engine: BrainEngine,
    *,
    copy_text: str = "synthetic retained",
    copy_egress: bool = True,
) -> tuple[HistoricalCopyRelationRequest, HistoricalConsentSnapshot]:
    baseline = _baseline(engine)
    observed = baseline.observed_delivery
    privacy = PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="synthetic.public.v1",
        authority=Authority(cloud=False, external_egress=False),
    )
    capture = replace(observed.submission.capture, privacy=privacy)
    observed = replace(
        observed,
        submission=replace(
            observed.submission, capture=capture, canonical_sha256=capture.request_sha256()
        ),
        observation=replace(
            observed.observation,
            privacy_policy_version=privacy.policy_version,
            privacy_policy_sha256=sha256(canonical(privacy.to_dict())).hexdigest(),
        ),
    )
    baseline = replace(baseline, observed_delivery=observed)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            baseline,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    copy_submission = replace(
        _public_submission(engine.tasks, delivery_id="legacy.public.copy"),
        payload=TextPayload(copy_text),
        privacy=replace(privacy, authority=Authority(cloud=False, external_egress=copy_egress)),
    )
    copy = engine.capture.submit(copy_submission)
    with closing(engine._store.connect()) as connection:
        row = connection.execute(
            "SELECT r.*,c.privacy_json FROM source_revisions r JOIN captures c USING(capture_id) "
            "WHERE r.capture_id=?",
            (copy.capture_id,),
        ).fetchone()
    claim = HistoricalClaimRequest(
        operation_id="claim.copy",
        destination=baseline.destination,
        source_cas=_current_cas(engine, baseline.source_cas.source_id),
        capture_source_cas=_current_cas(engine, row["source_id"]),
        retained_capture=RetainedCaptureEvidence(
            capture_id=copy.capture_id,
            source_sha256=row["source_sha256"],
            retained_delivery_id=copy_submission.delivery_id,
            retained_request_sha256=row["request_sha256"],
            privacy_sha256=sha256(canonical(json.loads(row["privacy_json"]))).hexdigest(),
        ),
        claim_role="historical_copy",
        expected_claim_generation=1,
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        register_historical_claim(
            engine.profile,
            claim,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    request = HistoricalCopyRelationRequest(
        operation_id="relation.synthetic",
        destination=baseline.destination,
        source_cas=baseline.source_cas,
        copy_source_cas=claim.capture_source_cas,
        baseline_operation_id=baseline.operation_id,
        copy_claim_operation_id=claim.operation_id,
        retained_copy=claim.retained_capture,
        approval_evidence_sha256="d" * 64,
        provider_ids=("openai",),
        expected_relation_version=0,
        expected_claim_generation=2,
    )
    state = (
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
    return request, HistoricalConsentSnapshot(request.destination, state)


def test_link_preserves_existing_content_and_only_allows_evidenced_provider(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, consent = _link_request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    external = replace(
        external_authority(engine),
        provider_id="openai",
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC}),
    )
    read = RecordReadRequest(
        record_id=request.retained_copy.capture_id,
        expected_revision_id=request.retained_copy.capture_id,
    )
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(read, authority=external)
    with closing(engine._store.connect()) as connection:
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "captures",
                "source_revisions",
                "source_aliases",
                "logical_sources",
                "historical_claims",
            )
        }
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = link_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            load_consent=lambda: consent,
        )
        assert (
            link_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                load_consent=lambda: consent,
            )
            == receipt
        )
    engine.retrieval.read_record(read, authority=external)
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(read, authority=replace(external, provider_id="anthropic"))
    with closing(engine._store.connect()) as connection:
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        assert connection.execute("SELECT COUNT(*) FROM sharing_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM historical_relations").fetchone()[0] == 1
    assert receipt.outcome == "historical_copy_linked" and receipt.claim_generation == 3


def test_real_link_revocation_denies_copy_and_old_link_replay_cannot_regrant(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, consent = _link_request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    external = replace(
        external_authority(engine),
        provider_id="openai",
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC}),
    )
    read = RecordReadRequest(
        record_id=request.retained_copy.capture_id,
        expected_revision_id=request.retained_copy.capture_id,
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = link_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            load_consent=lambda: consent,
        )
        engine.retrieval.read_record(read, authority=external)
        revoked = revoke_historical_copy(
            engine.profile,
            _revocation_request(engine, request),
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        assert revoked.relation_version == 2

        def forbidden() -> HistoricalConsentSnapshot:
            raise AssertionError("completed historical replay loaded current consent")

        assert (
            link_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                load_consent=forbidden,
            )
            == receipt
        )
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(read, authority=external)
    engine.retrieval.read_record(read, authority=owner)
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM historical_claims").fetchone()[0] == 2


@pytest.mark.parametrize(
    "stage",
    [
        "historical_relation_validated",
        "historical_relation_pending",
        "historical_registry_advanced",
        "historical_sql_projected",
        "historical_sql_committed",
    ],
)
def test_link_exact_retry_after_each_interruption(tmp_path: Path, stage: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, consent = _link_request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def crash(point: str) -> None:
        if point == stage:
            raise RuntimeError("synthetic interruption")

    with exclusive_runtime_admission(engine.profile) as admission:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            link_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                load_consent=lambda: consent,
                checkpoint=crash,
            )
        assert (
            link_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                load_consent=lambda: consent,
            ).claim_generation
            == 3
        )


@pytest.mark.parametrize(
    "damage",
    [
        "copy_text",
        "copy_egress",
        "consent",
        "provider",
        "destination",
        "narrow_consent",
        "ambiguous_consent",
        "baseline",
        "claim",
        "generation",
        "original_cas",
        "copy_cas",
        "nonowner",
    ],
)
def test_link_refuses_false_correspondence_or_current_consent_before_pending(
    tmp_path: Path,
    damage: str,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, consent = _link_request(
        engine,
        copy_text="changed" if damage == "copy_text" else "synthetic retained",
        copy_egress=damage != "copy_egress",
    )
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    code = (
        "binding_mismatch"
        if damage in ("copy_text", "destination", "baseline", "claim")
        else "revision_changed"
        if damage in ("generation", "original_cas", "copy_cas")
        else "unsupported_capability"
    )
    if damage == "consent":
        consent = replace(consent, state=ProviderConsentState())
    elif damage == "provider":
        request = replace(request, provider_ids=("anthropic",))
    elif damage == "destination":
        consent = replace(consent, destination=replace(consent.destination, issuer_epoch=2))
    elif damage == "narrow_consent":
        consent = replace(
            consent,
            state=ProviderConsentState()
            .grant(
                owner=True,
                provider_id="openai",
                allowed_tiers=frozenset({PrivacyTier.WORK}),
                operation_id="consent.narrow",
                decided_at="2026-10-03T00:00:00Z",
                consent_id_factory=lambda: "consent_" + "a" * 32,
            )
            .state,
        )
    elif damage == "ambiguous_consent":
        consent = replace(consent, state=replace(consent.state, records=consent.state.records * 2))
    elif damage == "baseline":
        request = replace(request, baseline_operation_id="absent.baseline")
    elif damage == "claim":
        request = replace(request, copy_claim_operation_id=request.baseline_operation_id)
    elif damage == "generation":
        request = replace(request, expected_claim_generation=1)
    elif damage == "original_cas":
        request = replace(request, source_cas=replace(request.source_cas, expected_head_version=99))
    elif damage == "copy_cas":
        request = replace(
            request, copy_source_cas=replace(request.copy_source_cas, expected_head_version=99)
        )
    elif damage == "nonowner":
        owner = replace(owner, owner=False, capabilities=frozenset({"admin"}))
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match=code),
    ):
        link_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            load_consent=lambda: consent,
        )
    assert (
        HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending() is None
    )
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM historical_relations").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["historical_relation_pending", "historical_sql_projected"])
def test_consent_revocation_before_commit_retains_pending_intent_without_link(
    tmp_path: Path,
    stage: str,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, consent = _link_request(engine)
    current = consent
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def revoke(point: str) -> None:
        nonlocal current
        if point == stage:
            current = replace(
                consent,
                state=consent.state.revoke(
                    owner=True,
                    consent_id="consent_" + "a" * 32,
                    operation_id="consent.revoke",
                    decided_at="2026-10-03T00:01:00Z",
                ).state,
            )

    with exclusive_runtime_admission(engine.profile) as admission:
        with pytest.raises(SharingError, match="unsupported_capability"):
            link_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                load_consent=lambda: current,
                checkpoint=revoke,
            )
        pending = HistoricalPendingFence(
            engine.profile.root, engine.profile.root_identity
        ).pending()
        assert pending is not None
        with closing(engine._store.connect()) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM historical_relations").fetchone()[0] == 0
            )
        fresh = current.state.grant(
            owner=True,
            provider_id="openai",
            allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
            operation_id="consent.fresh",
            decided_at="2026-10-03T00:02:00Z",
            consent_id_factory=lambda: "consent_" + "b" * 32,
        ).state
        current = replace(current, state=fresh)
        assert (
            link_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                load_consent=lambda: current,
            )
            == pending.receipt
        )
