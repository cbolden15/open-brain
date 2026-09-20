import json
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError
from hashlib import sha256
from itertools import product

import pytest
from open_brain_engine.core.access_contracts import (
    AggregatedPrivacyDecision,
    BrainIdentity,
    LegacyEpochBindingEvidence,
    aggregate_privacy_decisions,
    derive_brain_id,
    privacy_decision_sha256,
    validate_authorization_generation,
    validate_issuer_epoch,
    validate_stored_privacy_decision,
)
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    ValidationError,
)

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
BRAIN_ID = "brn_ci7ekz7itnbnhjcwijtbif2aaa"


class _BooleanThenIntegerAuthority(Mapping[str, object]):
    def __init__(self) -> None:
        self._reads = {"cloud": 0, "external_egress": 0}

    def __getitem__(self, key: str) -> object:
        reads = self._reads[key]
        self._reads[key] += 1
        return True if reads == 0 else 1

    def __iter__(self) -> Iterator[str]:
        return iter(self._reads)

    def __len__(self) -> int:
        return len(self._reads)


def _privacy(
    tier: PrivacyTier,
    *,
    cloud: bool = False,
    external_egress: bool = False,
    policy_version: str = "privacy-v1",
    confirmation_ref: str | None = None,
) -> PrivacyDecision:
    reasons = {
        PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
        PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
        PrivacyTier.PERSONAL: (
            PrivacyReason.PERSONAL_CONFIRMED
            if confirmation_ref is not None
            else PrivacyReason.PERSONAL_LOCAL_ONLY
        ),
        PrivacyTier.SECRET: PrivacyReason.SECRET_DETECTED,
        PrivacyTier.UNKNOWN: PrivacyReason.CLASSIFICATION_MISSING,
    }
    return PrivacyDecision.create(
        tier=tier,
        reason=reasons[tier],
        policy_version=policy_version,
        authority=Authority(cloud=cloud, external_egress=external_egress),
        confirmation_ref=confirmation_ref,
    )


def test_brain_id_derivation_is_canonical_and_placement_independent(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    first = tmp_path_factory.mktemp("first")
    second = tmp_path_factory.mktemp("second")

    monkeypatch.chdir(first)
    first_id = derive_brain_id(TENANT_ID)
    monkeypatch.chdir(second)

    assert first_id == derive_brain_id(TENANT_ID) == BRAIN_ID
    assert "=" not in first_id


@pytest.mark.parametrize(
    "tenant_id",
    [
        "123e4567-e89b-42d3-a456-426614174000",
        "brain_123e4567-e89b-42d3-a456-426614174000",
        "tenant_123E4567-E89B-42D3-A456-426614174000",
        "tenant_123e4567-e89b-12d3-a456-426614174000",
        "tenant_123e4567-e89b-42d3-c456-426614174000",
        "tenant_not-a-uuid",
        "",
    ],
)
def test_brain_id_derivation_rejects_noncanonical_tenant_ids(tenant_id: str) -> None:
    with pytest.raises(ValidationError, match="tenant ID"):
        derive_brain_id(tenant_id)


def test_brain_identity_derives_matching_id_and_is_immutable() -> None:
    identity = BrainIdentity.create(tenant_id=TENANT_ID, issuer_epoch=7)

    assert identity == BrainIdentity(TENANT_ID, BRAIN_ID, 7)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        identity.issuer_epoch = 8  # type: ignore[misc]


@pytest.mark.parametrize("issuer_epoch", [0, -1, True, 1.0, "1"])
def test_brain_identity_and_issuer_epoch_reject_invalid_values(issuer_epoch: object) -> None:
    with pytest.raises(ValidationError, match="issuer epoch"):
        BrainIdentity.create(tenant_id=TENANT_ID, issuer_epoch=issuer_epoch)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="issuer epoch"):
        validate_issuer_epoch(issuer_epoch)


def test_brain_identity_rejects_mismatched_derived_id() -> None:
    with pytest.raises(ValidationError, match="Brain identity"):
        BrainIdentity(TENANT_ID, "brn_aaaaaaaaabaabaaaaaaaaaaaaa", 1)


@pytest.mark.parametrize("value", [0, 1, 42])
def test_authorization_generation_accepts_nonnegative_integers(value: int) -> None:
    assert validate_authorization_generation(value) == value


@pytest.mark.parametrize("value", [-1, True, False, 1.0, "1", None])
def test_authorization_generation_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError, match="authorization generation"):
        validate_authorization_generation(value)  # type: ignore[arg-type]


def test_stored_privacy_validation_and_digest_cover_the_complete_decision() -> None:
    decision = _privacy(
        PrivacyTier.PERSONAL,
        cloud=True,
        external_egress=True,
        confirmation_ref="confirmation.synthetic-001",
    )

    assert validate_stored_privacy_decision(decision.to_dict()) == decision
    assert privacy_decision_sha256(decision) == sha256(
        portable_canonical_json_bytes(decision.to_dict())
    ).hexdigest()
    changed = _privacy(
        PrivacyTier.PERSONAL,
        cloud=True,
        external_egress=False,
        confirmation_ref="confirmation.synthetic-001",
    )
    assert privacy_decision_sha256(changed) != privacy_decision_sha256(decision)


def test_stored_privacy_validation_detaches_stateful_authority_mapping() -> None:
    stored: dict[str, object] = {
        "tier": "public",
        "reason": "policy_public",
        "policy_version": "privacy-v1",
        "authority": _BooleanThenIntegerAuthority(),
        "confirmation_ref": None,
    }

    decision = validate_stored_privacy_decision(stored)

    assert type(decision.authority.cloud) is bool
    assert type(decision.authority.external_egress) is bool


@pytest.mark.parametrize(
    "stored",
    [
        {},
        {
            "tier": "public",
            "reason": "policy_public",
            "policy_version": "privacy-v1",
            "authority": {"cloud": True, "external_egress": True},
            "confirmation_ref": None,
            "extra": True,
        },
        {
            "tier": "secret",
            "reason": "policy_public",
            "policy_version": "privacy-v1",
            "authority": {"cloud": False, "external_egress": False},
            "confirmation_ref": None,
        },
        {
            "tier": "secret",
            "reason": "secret_detected",
            "policy_version": "privacy-v1",
            "authority": {"cloud": True, "external_egress": False},
            "confirmation_ref": None,
        },
        {
            "tier": "personal",
            "reason": "personal_confirmed",
            "policy_version": "privacy-v1",
            "authority": {"cloud": True, "external_egress": True},
            "confirmation_ref": None,
        },
        {
            "tier": "public",
            "reason": "policy_public",
            "policy_version": "privacy-v1",
            "authority": {"cloud": 1, "external_egress": True},
            "confirmation_ref": None,
        },
    ],
)
def test_stored_privacy_validation_rejects_malformed_decisions(
    stored: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        validate_stored_privacy_decision(stored)
    with pytest.raises(ValidationError):
        privacy_decision_sha256(stored)


@pytest.mark.parametrize("left", list(PrivacyTier))
@pytest.mark.parametrize("right", list(PrivacyTier))
def test_privacy_aggregation_obeys_every_tier_precedence_pair(
    left: PrivacyTier, right: PrivacyTier
) -> None:
    precedence = {
        PrivacyTier.PUBLIC: 0,
        PrivacyTier.WORK: 1,
        PrivacyTier.PERSONAL: 2,
        PrivacyTier.UNKNOWN: 3,
        PrivacyTier.SECRET: 4,
    }

    combined = aggregate_privacy_decisions([_privacy(left), _privacy(right)])

    assert combined.tier is max((left, right), key=precedence.__getitem__)


@pytest.mark.parametrize("left", list(product((False, True), repeat=2)))
@pytest.mark.parametrize("right", list(product((False, True), repeat=2)))
def test_privacy_aggregation_intersects_cloud_and_external_egress(
    left: tuple[bool, bool], right: tuple[bool, bool]
) -> None:
    combined = aggregate_privacy_decisions(
        [
            _privacy(PrivacyTier.PUBLIC, cloud=left[0], external_egress=left[1]),
            _privacy(PrivacyTier.WORK, cloud=right[0], external_egress=right[1]),
        ]
    )

    assert combined.authority == Authority(
        cloud=left[0] and right[0], external_egress=left[1] and right[1]
    )


def test_privacy_aggregation_deduplicates_and_sorts_lineage_evidence() -> None:
    second = _privacy(
        PrivacyTier.PERSONAL,
        cloud=True,
        external_egress=True,
        policy_version="privacy-v2",
        confirmation_ref="confirmation.synthetic-002",
    )
    first = _privacy(
        PrivacyTier.PERSONAL,
        cloud=True,
        external_egress=True,
        policy_version="privacy-v1",
        confirmation_ref="confirmation.synthetic-001",
    )

    combined = aggregate_privacy_decisions([second, first.to_dict(), second])
    reversed_combined = aggregate_privacy_decisions([first, second.to_dict()])

    assert combined.source_decision_sha256s == tuple(
        sorted({privacy_decision_sha256(first), privacy_decision_sha256(second)})
    )
    assert combined.confirmation_refs == (
        "confirmation.synthetic-001",
        "confirmation.synthetic-002",
    )
    assert reversed_combined == combined


def test_privacy_aggregation_canonicalizes_nfc_equivalent_confirmation_refs() -> None:
    composed = _privacy(
        PrivacyTier.PERSONAL,
        confirmation_ref="confirmation.synthetic-caf\u00e9",
    )
    decomposed = _privacy(
        PrivacyTier.PERSONAL,
        confirmation_ref="confirmation.synthetic-cafe\u0301",
    )

    combined = aggregate_privacy_decisions([decomposed, composed])

    assert combined.source_decision_sha256s == (privacy_decision_sha256(composed),)
    assert combined.confirmation_refs == ("confirmation.synthetic-caf\u00e9",)
    serialized = json.loads(
        portable_canonical_json_bytes(
            {
                "authority": {
                    "cloud": combined.authority.cloud,
                    "external_egress": combined.authority.external_egress,
                },
                "confirmation_refs": combined.confirmation_refs,
                "source_decision_sha256s": combined.source_decision_sha256s,
                "tier": combined.tier.value,
            }
        )
    )
    reconstructed = AggregatedPrivacyDecision(
        tier=PrivacyTier(serialized["tier"]),
        authority=Authority(**serialized["authority"]),
        source_decision_sha256s=tuple(serialized["source_decision_sha256s"]),
        confirmation_refs=tuple(serialized["confirmation_refs"]),
    )
    assert reconstructed == combined


def test_privacy_aggregation_rejects_an_empty_source_set() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        aggregate_privacy_decisions(iter(()))


def test_aggregated_privacy_contract_is_immutable() -> None:
    combined = aggregate_privacy_decisions([_privacy(PrivacyTier.PUBLIC)])

    assert isinstance(combined, AggregatedPrivacyDecision)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        combined.tier = PrivacyTier.WORK  # type: ignore[misc]


def test_legacy_epoch_binding_accepts_artifact_and_jsonl_row_evidence() -> None:
    artifact = LegacyEpochBindingEvidence(
        artifact_path="history/publications/publication_123e4567-e89b-42d3-a456-426614174000.json",
        jsonl_ordinal=None,
        payload_sha256="a" * 64,
        issuer_epoch=1,
    )
    row = LegacyEpochBindingEvidence(
        artifact_path="sources/events/2026-09-19.jsonl",
        jsonl_ordinal=0,
        payload_sha256="b" * 64,
        issuer_epoch=2,
    )

    assert artifact.jsonl_ordinal is None
    assert row.jsonl_ordinal == 0
    with pytest.raises((FrozenInstanceError, AttributeError)):
        row.issuer_epoch = 3  # type: ignore[misc]


@pytest.mark.parametrize(
    "artifact_path",
    [
        "",
        ".",
        "/absolute/file.json",
        "../escape.json",
        "safe/../escape.json",
        "safe//file.json",
        "safe\\file.json",
        ".open-brain/state.json",
        "safe/file.json\x00",
    ],
)
def test_legacy_epoch_binding_rejects_unsafe_artifact_paths(artifact_path: str) -> None:
    with pytest.raises(ValidationError, match="artifact path"):
        LegacyEpochBindingEvidence(artifact_path, None, "a" * 64, 1)


@pytest.mark.parametrize("ordinal", [-1, True, 1.0, "0"])
def test_legacy_epoch_binding_rejects_invalid_jsonl_ordinals(ordinal: object) -> None:
    with pytest.raises(ValidationError, match="JSONL ordinal"):
        LegacyEpochBindingEvidence(
            "sources/events/2026-09-19.jsonl",
            ordinal,  # type: ignore[arg-type]
            "a" * 64,
            1,
        )


@pytest.mark.parametrize(
    ("artifact_path", "ordinal"),
    [("sources/events/2026-09-19.jsonl", None), ("brain.toml", 0)],
)
def test_legacy_epoch_binding_requires_ordinals_only_for_jsonl_rows(
    artifact_path: str, ordinal: int | None
) -> None:
    with pytest.raises(ValidationError, match="JSONL ordinal"):
        LegacyEpochBindingEvidence(artifact_path, ordinal, "a" * 64, 1)


@pytest.mark.parametrize("digest", ["a" * 63, "A" * 64, "g" * 64, "sha256:" + "a" * 64])
def test_legacy_epoch_binding_rejects_invalid_exact_payload_digests(digest: str) -> None:
    with pytest.raises(ValidationError, match="payload SHA-256"):
        LegacyEpochBindingEvidence("brain.toml", None, digest, 1)


@pytest.mark.parametrize("issuer_epoch", [0, -1, True, 1.0])
def test_legacy_epoch_binding_rejects_invalid_issuer_epochs(issuer_epoch: object) -> None:
    with pytest.raises(ValidationError, match="issuer epoch"):
        LegacyEpochBindingEvidence(
            "brain.toml", None, "a" * 64, issuer_epoch  # type: ignore[arg-type]
        )
