"""Versioned facts preserve continuity and fail closed after eligibility changes."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import TextPayload
from open_brain_engine.engine.consent_contracts import ProviderConsentState
from open_brain_engine.engine.historical_checkpoint import HistoricalBaselineDuplicate
from open_brain_engine.engine.historical_contracts_v2 import HistoricalRevocationRequestV2
from open_brain_engine.engine.historical_tasks import link_historical_copy, revoke_historical_copy
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing import EligibilityMode, sharing_eligible
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.t03_contracts import RecordReadRequest, SearchPageRequest, T03Error

from ._historical_compatibility_fixtures import baseline_v2, portable5_import_engine
from .test_historical_compatibility import linked_v2
from .test_paging import external_authority, wire


def test_v2_checkpoint_and_real_successor_preserve_import_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = portable5_import_engine(tmp_path, monkeypatch)
    relation, _, _ = linked_v2(engine)
    from open_brain_engine.engine.historical_contracts_v2 import HistoricalBaselineRequestV2
    from open_brain_engine.engine.historical_dispatch import VersionedHistoricalTransitionStore

    baseline = (
        VersionedHistoricalTransitionStore(engine.profile.root, engine.profile.root_identity)
        .read("baseline.synthetic.import")
        .request
    )
    assert isinstance(baseline, HistoricalBaselineRequestV2)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    checkpoint = sink.lookup_baseline(
        baseline.observed_delivery, selection_generation="synthetic.current.selection"
    )
    assert isinstance(checkpoint, HistoricalBaselineDuplicate)
    assert checkpoint.receipt.dto_version == 2
    assert checkpoint.capture_id == baseline.retained_original.capture_id
    head = sink.inspect_head()
    assert head.source_id == baseline.source_cas.source_id
    assert head.revision_key == baseline.observed_delivery.submission.revision_key
    with engine._store.connect() as connection:
        original = tuple(
            connection.execute(
                "SELECT * FROM source_revisions WHERE capture_id=?",
                (baseline.retained_original.capture_id,),
            ).fetchone()
        )
    old = baseline.observed_delivery
    capture = replace(
        old.submission.capture,
        payload=TextPayload("Synthetic next revision"),
        delivery_id="synthetic.next.capture",
    )
    successor = replace(
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
            original_sha256=sha256(b"Synthetic next upstream file").hexdigest(),
            transformed_sha256=sha256(b"Synthetic next revision").hexdigest(),
            admitted_payload_sha256=sha256(canonical(capture.payload.to_dict())).hexdigest(),
        ),
    )
    result = sink.submit(successor)
    assert result.source_receipt is not None
    assert result.source_receipt.source_id == baseline.source_cas.source_id
    assert sink.submit(successor) == result
    with engine._store.connect() as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM source_revisions WHERE capture_id=?",
                    (baseline.retained_original.capture_id,),
                ).fetchone()
            )
            == original
        )
        assert not sharing_eligible(
            connection,
            relation.retained_copy.capture_id,
            mode=EligibilityMode.EXTERNAL_READ,
            profile=engine.profile,
            provider_id="openai",
            brain_id=relation.destination.brain_id,
            issuer_epoch=relation.destination.issuer_epoch,
        )


@pytest.mark.parametrize("damage", ["revocation", "projection_loss", "missing_intent"])
def test_v2_external_read_and_search_fail_closed_without_losing_owner_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    engine = portable5_import_engine(tmp_path, monkeypatch)
    relation, _, owner = linked_v2(engine)
    reader = replace(
        external_authority(engine),
        provider_id="openai",
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC}),
    )
    read = RecordReadRequest(
        record_id=relation.retained_copy.capture_id,
        expected_revision_id=relation.retained_copy.capture_id,
    )
    engine.retrieval.read_record(read, authority=reader)
    search = SearchPageRequest(query="Synthetic", limit=10)
    assert relation.retained_copy.capture_id in {
        row["record_id"]
        for row in wire(engine.retrieval.search_page(search, authority=reader))["results"]
    }
    if damage == "revocation":
        request = HistoricalRevocationRequestV2(
            operation_id="revoke.synthetic.copy",
            destination=relation.destination,
            source_cas=relation.source_cas,
            relation_operation_id=relation.operation_id,
            expected_relation_version=1,
            expected_claim_generation=3,
            reason_code="synthetic_withdrawal",
        )
        with exclusive_runtime_admission(engine.profile) as admission:
            revoke_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
    elif damage == "projection_loss":
        from open_brain_engine.engine.historical_recovery import _historical_transaction

        with _historical_transaction(engine.profile, lambda: None) as connection:
            definition = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='historical_relations_delete_immutable'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER historical_relations_delete_immutable")
            connection.execute("DELETE FROM historical_relations")
            connection.execute(definition)
    else:
        from open_brain_engine.engine.historical_transition_v2 import _path

        (engine.profile.root / _path(relation.operation_id)).unlink()
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(read, authority=reader)
    assert relation.retained_copy.capture_id not in {
        row["record_id"]
        for row in wire(engine.retrieval.search_page(search, authority=reader))["results"]
    }
    with engine._store.connect() as connection:
        assert not sharing_eligible(
            connection,
            relation.retained_copy.capture_id,
            mode=EligibilityMode.EXTERNAL_READ,
            profile=engine.profile,
            provider_id="openai",
            brain_id=relation.destination.brain_id,
            issuer_epoch=relation.destination.issuer_epoch,
        )
    engine.history.read_history(read, authority=owner)


@pytest.mark.parametrize(
    "stage",
    [
        "historical_relation_pending",
        "historical_registry_advanced",
        "historical_sql_projected",
        "historical_sql_committed",
        "historical_recovery_complete",
    ],
)
def test_v2_link_recovers_each_boundary_with_fresh_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    engine = portable5_import_engine(tmp_path, monkeypatch)
    relation, snapshot, owner = linked_v2(engine, perform_link=False)

    def crash(observed: str) -> None:
        if observed == stage:
            raise RuntimeError("synthetic link interruption")

    with exclusive_runtime_admission(engine.profile) as admission:
        kwargs: dict[str, Any] = dict(
            authority=owner, admission=admission, validate_before_write=lambda: None
        )
        with pytest.raises(RuntimeError, match="synthetic link interruption"):
            link_historical_copy(
                engine.profile, relation, load_consent=lambda: snapshot, checkpoint=crash, **kwargs
            )
        if stage not in ("historical_sql_committed", "historical_recovery_complete"):
            with pytest.raises(SharingError, match="unsupported_capability"):
                link_historical_copy(
                    engine.profile,
                    relation,
                    load_consent=lambda: replace(snapshot, state=ProviderConsentState()),
                    **kwargs,
                )
        receipt = link_historical_copy(
            engine.profile, relation, load_consent=lambda: snapshot, **kwargs
        )
        assert (
            link_historical_copy(engine.profile, relation, load_consent=lambda: snapshot, **kwargs)
            == receipt
        )
    with engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM historical_operations").fetchone()[0] == 3


@pytest.mark.parametrize(
    "damage", ["private", "unknown", "egress", "payload", "redaction", "too_large"]
)
def test_v2_relation_keeps_privacy_payload_and_bounds_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from open_brain_engine.engine.historical_admission_v2 import verify_historical_relation_payload

    if damage == "too_large":
        with pytest.raises(ValueError):
            TextPayload("x" * 65537)
        return
    engine = portable5_import_engine(tmp_path, monkeypatch)
    baseline = baseline_v2(engine)
    text = baseline.observed_delivery.submission.capture.payload
    privacy = baseline.observed_delivery.submission.capture.privacy.to_dict()
    privacy["authority"] = {"cloud": True, "external_egress": True}
    copy = {"payload": text.to_dict(), "privacy": privacy}
    if damage in ("private", "unknown"):
        privacy = PrivacyDecision.create(
            tier=PrivacyTier.PERSONAL if damage == "private" else PrivacyTier.UNKNOWN,
            reason=PrivacyReason.PERSONAL_LOCAL_ONLY
            if damage == "private"
            else PrivacyReason.CLASSIFICATION_MISSING,
            policy_version="synthetic.local.v1",
            authority=Authority(False, False),
        ).to_dict()
        copy["privacy"] = privacy
    elif damage == "egress":
        authority = privacy["authority"]
        assert isinstance(authority, dict)
        authority["external_egress"] = False
    elif damage == "payload":
        copy["payload"] = TextPayload("Unrelated body").to_dict()
    else:
        value = "secret token ghp_" + "a" * 40 if damage == "redaction" else "😀" * 20000
        capture = replace(baseline.observed_delivery.submission.capture, payload=TextPayload(value))
        baseline = replace(
            baseline,
            observed_delivery=replace(
                baseline.observed_delivery,
                submission=replace(
                    baseline.observed_delivery.submission,
                    capture=capture,
                    canonical_sha256=capture.request_sha256(),
                ),
                observation=replace(
                    baseline.observed_delivery.observation,
                    admitted_payload_sha256=sha256(
                        canonical(capture.payload.to_dict())
                    ).hexdigest(),
                    transformed_sha256=sha256(value.encode()).hexdigest(),
                ),
            ),
        )
        copy["payload"] = capture.payload.to_dict()
    with pytest.raises(SharingError):
        verify_historical_relation_payload(baseline, copy)
