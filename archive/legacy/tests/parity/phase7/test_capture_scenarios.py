from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.models import (
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    Intent,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.core.policy import IntentPolicyReason, classify_privacy, route_intent

from open_brain_legacy import parity

_FIXTURES = Path(__file__).with_name("capture_scenarios.json")
_CAPTURED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
_ARTIFACT = parity.BuiltArtifactIdentity(
    version="0.1.0",
    digest_sha256="a" * 64,
)
_ATTESTATION = object()
_EXPECTED_NAMES = {
    "youtube_playlist_hold",
    "social_reference",
    "saved_web_reference",
    "idea_candidate",
    "third_party_action_candidate",
}


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _opaque_id(prefix: str, value: str) -> str:
    return f"{prefix}_{_digest(value)[:16]}"


class _ArtifactVerifier:
    def verify_artifact_attestation(
        self,
        artifact_attestation: object,
        *,
        evaluated_at: datetime,
    ) -> parity.ArtifactAttestationEvidence:
        assert artifact_attestation is _ATTESTATION
        return parity.ArtifactAttestationEvidence(
            verifier_id=_opaque_id("verifier", "phase7-capture-scenarios"),
            attestation_id=_opaque_id("attestation", "phase7-capture-scenarios"),
            attestation_digest_sha256=_digest("phase7-capture-scenarios-attestation"),
            artifact=_ARTIFACT,
            manifest_version=parity.PARITY_HARNESS_VERSION,
            schema_digest_sha256=parity.PARITY_SCHEMA_DIGEST_SHA256,
            scope=parity.EvidenceScope.SYNTHETIC,
            evaluated_at=evaluated_at,
            expires_at=evaluated_at + timedelta(hours=1),
        )


def _scenarios() -> list[Mapping[str, object]]:
    decoded = json.loads(_FIXTURES.read_text(encoding="utf-8"))
    assert isinstance(decoded, list)
    assert all(isinstance(item, dict) for item in decoded)
    return cast(list[Mapping[str, object]], decoded)


def _text(scenario: Mapping[str, object], key: str) -> str:
    value = scenario[key]
    assert isinstance(value, str)
    return value


def _capture(scenario: Mapping[str, object]) -> CaptureEnvelope:
    source_url = _text(scenario, "source_url")
    why_origin = CaptureWhyOrigin(_text(scenario, "capture_why_origin"))
    return CaptureEnvelope.create(
        source_type=SourceType(_text(scenario, "source_type")),
        content_kind=ContentKind(_text(scenario, "content_kind")),
        source_url=source_url,
        title=_text(scenario, "title"),
        shared_text=_text(scenario, "shared_text"),
        captured_at=_CAPTURED_AT,
        capture_why=_text(scenario, "capture_why"),
        capture_why_origin=why_origin,
        capture_source=CaptureSource(_text(scenario, "capture_source")),
        provenance=Provenance.create(
            source_ref=source_url,
            content_origin=ContentOrigin(_text(scenario, "content_origin")),
            owner_context=why_origin,
        ),
        raw_assets=(),
        privacy_decision=classify_privacy(
            PrivacyTier(_text(scenario, "privacy_tier")),
            policy_version="phase7-fixture-v1",
        ),
    )


def _routing_destination(
    route_intent_value: Intent,
    privacy_tier: PrivacyTier,
) -> parity.RoutingDestination:
    if route_intent_value is Intent.REFERENCE:
        return (
            parity.RoutingDestination.PERSONAL
            if privacy_tier is PrivacyTier.PERSONAL
            else parity.RoutingDestination.WORK
        )
    return {
        Intent.HOLD: parity.RoutingDestination.HOLD,
        Intent.IDEA: parity.RoutingDestination.REVIEW,
        Intent.ACTION_CANDIDATE: parity.RoutingDestination.REVIEW,
    }[route_intent_value]


def _review_metadata(
    scenario: Mapping[str, object],
    capture: CaptureEnvelope,
    *,
    intent: Intent,
    has_review_proposal: bool,
    proposal_reason: str,
    capture_why: str,
) -> parity.ReviewProposalsMetadata:
    source_ref_digest = _digest(capture.provenance.source_ref)
    capture_id = _opaque_id("capture", str(capture.capture_id))
    review_proposals: tuple[parity.ReviewProposal, ...] = ()
    if has_review_proposal:
        review_proposals = (
            parity.ReviewProposal(
                schema_version=1,
                review_id=_opaque_id("review", _text(scenario, "name")),
                capture_id=capture_id,
                source_ref_digest_sha256=source_ref_digest,
                privacy_tier=parity.PrivacyTier(capture.privacy_decision.tier.value),
                proposed_intent=parity.ReviewIntent(intent.value),
                proposal_reason_digest_sha256=_digest(proposal_reason),
                capture_why_digest_sha256=_digest(capture_why),
                state=parity.ReviewProposalState.OPEN,
                created_at=_CAPTURED_AT,
                actor_kind=parity.ReviewActorKind.OWNER,
                actor_label_digest_sha256=_digest("owner"),
            ),
        )
    return parity.ReviewProposalsMetadata(proposals=review_proposals)


def _parity_metadata(
    scenario: Mapping[str, object],
    capture: CaptureEnvelope,
    *,
    intent: Intent,
    review_metadata: parity.ReviewProposalsMetadata,
    content_kind: ContentKind,
    privacy_tier: PrivacyTier,
    source_type: SourceType,
    content_origin: ContentOrigin,
    owner_context: CaptureWhyOrigin,
) -> dict[parity.ParityFacet, parity.FacetMetadata]:
    source_ref_digest = _digest(capture.provenance.source_ref)
    capture_id = _opaque_id("capture", str(capture.capture_id))
    return {
        parity.ParityFacet.REQUEST_CONTENT: parity.RequestContentMetadata(
            request_status=parity.RequestStatus.COMPLETED,
            request_id=_opaque_id("request", _text(scenario, "name")),
            content_ids=(_opaque_id("content", str(capture.capture_id)),),
        ),
        parity.ParityFacet.RAW_FILE_SET: parity.RawFileSetMetadata(
            file_digests_sha256=(source_ref_digest,),
        ),
        parity.ParityFacet.QUEUE_RETRY: parity.QueueRetryMetadata(
            transitions=(
                parity.QueueTransition(
                    parity.QueueState.PENDING,
                    parity.QueueState.PROCESSING,
                    0,
                    None,
                ),
                parity.QueueTransition(
                    parity.QueueState.PROCESSING,
                    parity.QueueState.ACKNOWLEDGED,
                    0,
                    None,
                ),
            ),
        ),
        parity.ParityFacet.FRONTMATTER_PROVENANCE: parity.FrontmatterProvenanceMetadata(
            schema_version=1,
            content_kind=parity.ContentKind(content_kind.value),
            privacy_tier=parity.PrivacyTier(privacy_tier.value),
            source_kind=parity.SourceKind(source_type.value),
            source_ref_digest_sha256=source_ref_digest,
            content_origin=parity.ContentOrigin(content_origin.value),
            owner_context=parity.OwnerContext(owner_context.value),
            redaction_policy_version=1,
        ),
        parity.ParityFacet.ROUTING: parity.RoutingMetadata(
            destination=_routing_destination(intent, privacy_tier),
        ),
        parity.ParityFacet.LEDGER_CITATIONS: parity.LedgerCitationMetadata(
            ledger_item_ids=(),
            citation_ids=(),
        ),
        parity.ParityFacet.REVIEW_PROPOSALS: review_metadata,
        parity.ParityFacet.CLI_JSON: parity.CliJsonMetadata(
            profile=parity.CliProfile.OPEN_BRAIN_STATUS,
            command=parity.CliCommand.STATUS,
            status=parity.CliStatus.COMPLETED,
            exit_class=parity.CliExitClass.SUCCESS,
            field_digests=tuple(
                (field, _digest(f"{capture_id}:{field}"))
                for field in ("command", "metrics", "schema_version", "status", "strict")
            ),
        ),
        parity.ParityFacet.HEALTH_DOCTOR: parity.HealthDoctorMetadata(
            outcome=parity.HealthOutcome.HEALTHY,
            findings=(),
        ),
    }


def _legacy_parity_metadata(
    scenario: Mapping[str, object],
    capture: CaptureEnvelope,
) -> dict[parity.ParityFacet, parity.FacetMetadata]:
    expected_intent = Intent(_text(scenario, "expected_intent"))
    expected_review = scenario["expected_review_proposal"]
    assert isinstance(expected_review, bool)
    return _parity_metadata(
        scenario,
        capture,
        intent=expected_intent,
        review_metadata=_review_metadata(
            scenario,
            capture,
            intent=expected_intent,
            has_review_proposal=expected_review,
            proposal_reason=_text(scenario, "proposal_reason"),
            capture_why=_text(scenario, "capture_why"),
        ),
        content_kind=ContentKind(_text(scenario, "content_kind")),
        privacy_tier=PrivacyTier(_text(scenario, "privacy_tier")),
        source_type=SourceType(_text(scenario, "source_type")),
        content_origin=ContentOrigin(_text(scenario, "content_origin")),
        owner_context=CaptureWhyOrigin(_text(scenario, "capture_why_origin")),
    )


def _open_brain_parity_metadata(
    scenario: Mapping[str, object],
    capture: CaptureEnvelope,
) -> dict[parity.ParityFacet, parity.FacetMetadata]:
    route = route_intent(
        capture,
        cast(str | None, scenario["proposed_intent"]),
        _text(scenario, "proposal_reason"),
        now=_CAPTURED_AT,
    )
    return _parity_metadata(
        scenario,
        capture,
        intent=route.intent,
        review_metadata=_review_metadata(
            scenario,
            capture,
            intent=route.intent,
            has_review_proposal=route.review_proposal is not None,
            proposal_reason=(
                route.review_proposal.proposal_reason
                if route.review_proposal is not None
                else _text(scenario, "proposal_reason")
            ),
            capture_why=(
                route.review_proposal.capture_why
                if route.review_proposal is not None
                else capture.capture_why
            ),
        ),
        content_kind=capture.content_kind,
        privacy_tier=capture.privacy_decision.tier,
        source_type=capture.source_type,
        content_origin=capture.provenance.content_origin,
        owner_context=capture.capture_why_origin,
    )


def _parity_input(
    side: parity.ParitySide,
    scenario: Mapping[str, object],
    capture: CaptureEnvelope,
) -> parity.SyntheticParityInput:
    metadata = (
        _legacy_parity_metadata(scenario, capture)
        if side is parity.ParitySide.LEGACY
        else _open_brain_parity_metadata(scenario, capture)
    )
    return parity.SyntheticParityInput(
        side=side,
        artifact=_ARTIFACT,
        facets=tuple(
            parity.SyntheticFacetSnapshot(
                facet=facet,
                artifact=_ARTIFACT,
                metadata=metadata[facet],
            )
            for facet in parity.P7_W0_FACETS
        ),
    )


def test_fixture_inventory_covers_the_five_representative_capture_paths() -> None:
    scenarios = _scenarios()

    assert len(scenarios) == 5
    assert {_text(item, "name") for item in scenarios} == _EXPECTED_NAMES


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda item: str(item["name"]))
def test_capture_context_and_review_gates_survive_each_scenario(
    scenario: Mapping[str, object],
) -> None:
    capture = _capture(scenario)
    restored = CaptureEnvelope.from_canonical_bytes(capture.canonical_bytes())
    proposed_intent = scenario["proposed_intent"]
    assert proposed_intent is None or isinstance(proposed_intent, str)

    route = route_intent(
        restored,
        proposed_intent,
        _text(scenario, "proposal_reason"),
        now=_CAPTURED_AT,
    )

    assert restored.capture_why == _text(scenario, "capture_why")
    assert restored.capture_why_origin.value == _text(scenario, "capture_why_origin")
    assert restored.provenance.owner_context is restored.capture_why_origin
    assert restored.provenance.content_origin.value == _text(scenario, "content_origin")
    assert route.intent is Intent(_text(scenario, "expected_intent"))
    assert (route.review_proposal is not None) is scenario["expected_review_proposal"]

    if _text(scenario, "name") == "youtube_playlist_hold":
        assert route.reason is IntentPolicyReason.OWNER_CONTEXT_REQUIRED

    result = parity.compare_synthetic_parity(
        _parity_input(parity.ParitySide.LEGACY, scenario, restored),
        _parity_input(parity.ParitySide.OPEN_BRAIN, scenario, restored),
        evaluated_at=_CAPTURED_AT,
        artifact_attestation=_ATTESTATION,
        artifact_verifier=_ArtifactVerifier(),
    )

    assert result.resolved is True
    for facet in (
        parity.ParityFacet.FRONTMATTER_PROVENANCE,
        parity.ParityFacet.ROUTING,
        parity.ParityFacet.REVIEW_PROPOSALS,
    ):
        assert result.for_facet(facet).outcome is parity.ComparisonOutcome.MATCH
    rendered = json.dumps(result.to_dict(), allow_nan=False, sort_keys=True)
    assert _text(scenario, "source_url") not in rendered
    assert _text(scenario, "shared_text") not in rendered
    capture_why = _text(scenario, "capture_why")
    if capture_why:
        assert capture_why not in rendered

    if route.review_proposal is not None:
        assert route.review_proposal.capture_why == restored.capture_why
        assert route.review_proposal.capture_id == str(restored.capture_id)


def test_third_party_advice_cannot_silently_become_a_task() -> None:
    scenario = next(
        item for item in _scenarios() if item["name"] == "third_party_action_candidate"
    )

    route = route_intent(
        _capture(scenario),
        "action_candidate",
        _text(scenario, "proposal_reason"),
        now=_CAPTURED_AT,
    )

    assert route.intent is Intent.ACTION_CANDIDATE
    assert route.review_proposal is not None
    assert route.review_proposal.proposed_intent is Intent.ACTION_CANDIDATE
    metadata = _open_brain_parity_metadata(scenario, _capture(scenario))
    review_metadata = metadata[parity.ParityFacet.REVIEW_PROPOSALS]
    assert isinstance(review_metadata, parity.ReviewProposalsMetadata)
    assert review_metadata.proposals[0].state is parity.ReviewProposalState.OPEN


def test_personal_reference_keeps_its_personal_destination() -> None:
    scenario = next(item for item in _scenarios() if item["name"] == "saved_web_reference")
    metadata = _open_brain_parity_metadata(scenario, _capture(scenario))
    routing = metadata[parity.ParityFacet.ROUTING]

    assert isinstance(routing, parity.RoutingMetadata)
    assert routing.destination is parity.RoutingDestination.PERSONAL
