"""Synthetic custody contracts preserve consumer authority and frozen owner semantics."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from typing import Any

import pytest
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    Intent,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine.capture_lifecycle import LifecycleSegment
from open_brain_engine.engine.capture_lifecycle_contracts import LifecycleError, canonical_bytes
from open_brain_engine.engine.capture_recovery import CaptureReservationIdentity
from open_brain_engine.engine.consumer_contracts import segment_key
from open_brain_engine.engine.consumer_segments import (
    MAX_CONSUMER_SEGMENT_BYTES,
    ConsumerSegmentEnvelope,
    ConsumerSourceBinding,
)
from open_brain_engine.engine.contracts import (
    CaptureAction,
    CaptureSubmission,
    CaptureSubmissionPath,
    JournalEnvelope,
    PublicJobCaptureContext,
    TextPayload,
)

_UUID = "12345678-1234-4234-8234-123456789abc"
_CONVERSATION = "a" * 64
_SEGMENT = segment_key(_CONVERSATION, 0)


def _privacy(*, cloud: bool = False, external: bool = False) -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="synthetic-policy-v1",
        authority=Authority(cloud=cloud, external_egress=external),
    )


def _source() -> ConsumerSourceBinding:
    return ConsumerSourceBinding(
        namespace_id="consumer_namespace_" + _UUID,
        conversation_key=_CONVERSATION,
        segment_key=_SEGMENT,
        source_id="source_" + _UUID,
        expected_head=None,
        head_version=0,
        route_version=0,
        lifecycle_version=0,
        lifecycle="active",
        availability="available",
        space_id=None,
        effective_privacy_sha256=None,
        control_epoch=0,
    )


def _envelope(provider: str = "chatgpt") -> ConsumerSegmentEnvelope:
    actor, tenant = "actor_" + _UUID, "tenant_" + _UUID
    context = PublicJobCaptureContext(
        tenant_id=tenant,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "tenant_id": tenant,
            "role_id": "role_" + _UUID,
            "role_claim_id": "role_claim_" + _UUID,
            "capabilities": ["capture.accept"],
        },
    )
    reference = f"urn:open-brain:consumer-chat:{provider}:{_CONVERSATION}:0"
    submission = CaptureSubmission.for_public_job(
        context=context,
        payload=TextPayload("Synthetic user: café\nSynthetic assistant: response"),
        delivery_id="consumer.synthetic.0",
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=reference,
        provenance=Provenance.create(
            source_ref=reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=_privacy(),
        title="Synthetic title",
    )
    return ConsumerSegmentEnvelope(
        conversation_key=_CONVERSATION,
        conversation_revision_sha256="c" * 64,
        normalized_sha256="d" * 64,
        segment_index=0,
        segment_key=_SEGMENT,
        envelope=JournalEnvelope(submission, submission.privacy),
        identities=CaptureReservationIdentity.allocate(
            canonical=False, accepted_at="2026-10-16T00:00:00Z"
        ),
        source=_source(),
    )


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_exact_roundtrip_retains_body_title_nonowner_and_hashes(provider: str) -> None:
    envelope = _envelope(provider)
    raw = envelope.to_bytes()
    restored = ConsumerSegmentEnvelope.from_bytes(raw)
    assert restored == envelope
    assert restored.envelope.submission.title == "Synthetic title"
    assert restored.body == "Synthetic user: café\nSynthetic assistant: response".encode()
    assert restored.envelope.submission.submission_path is CaptureSubmissionPath.PUBLIC_JOB
    assert restored.binding.capture_id == restored.identities.capture_id
    assert restored.binding.body_sha256 == sha256(restored.body).hexdigest()
    assert restored.binding.request_sha256 == sha256(raw).hexdigest()
    assert json.loads(raw)["contract"] == "engine.consumer-segment.v1"


def test_source_value_is_detached_and_immutable() -> None:
    value = _source().value()
    source = ConsumerSourceBinding.from_value(value)
    value["namespace_id"] = "changed"
    exported = source.value()
    exported["control_epoch"] = 1
    assert source.namespace_id == "consumer_namespace_" + _UUID
    assert source.control_epoch == 0
    field = "control_epoch"
    with pytest.raises(FrozenInstanceError):
        setattr(source, field, 1)


@pytest.mark.parametrize("field,value", [
    ("namespace_id", ""), ("namespace_id", "x" * 129), ("namespace_id", "café"),
    ("namespace_id", "with space"), ("namespace_id", "bad\x00"), ("namespace_id", True),
    ("conversation_key", "A" * 64), ("segment_key", "b" * 63),
    ("source_id", "source_" + _UUID.upper()), ("source_id", "capture_" + _UUID),
    ("expected_head", "capture_invalid"), ("head_version", True), ("route_version", -1),
    ("lifecycle_version", 0.0), ("control_epoch", 9007199254740992),
    ("lifecycle", "withdrawn"), ("availability", "yes"),
    ("space_id", "space_invalid"), ("effective_privacy_sha256", False),
])
def test_source_rejects_invalid_typed_fields(field: str, value: Any) -> None:
    with pytest.raises(LifecycleError, match="^invalid_arguments$"):
        replace(_source(), **{field: value})


@pytest.mark.parametrize("field,value", [
    ("head_version", 1), ("route_version", 1), ("lifecycle_version", 1),
    ("lifecycle", "retired"), ("availability", "missing"),
    ("space_id", "space_" + _UUID), ("effective_privacy_sha256", "e" * 64),
])
def test_new_source_requires_empty_preimage(field: str, value: Any) -> None:
    with pytest.raises(LifecycleError):
        replace(_source(), **{field: value})


def test_existing_source_requires_version_and_privacy_digest() -> None:
    source = replace(
        _source(), expected_head="capture_" + _UUID,
        head_version=1, effective_privacy_sha256="e" * 64,
        route_version=2, lifecycle_version=3, lifecycle="retired", availability="missing",
        space_id="space_" + _UUID,
    )
    assert ConsumerSourceBinding.from_value(source.value()) == source
    for changes in ({"head_version": 0}, {"effective_privacy_sha256": None}):
        with pytest.raises(LifecycleError):
            replace(source, **changes)


@pytest.mark.parametrize("field,value", [
    ("segment_index", True), ("segment_index", -1), ("segment_index", 128),
    ("conversation_revision_sha256", "bad"), ("normalized_sha256", "D" * 64),
    ("conversation_key", "e" * 64), ("segment_key", "f" * 64),
])
def test_segment_refuses_invalid_hashes_index_and_source_mismatch(
    field: str, value: Any
) -> None:
    with pytest.raises(LifecycleError, match="^invalid_arguments$"):
        replace(_envelope(), **{field: value})


@pytest.mark.parametrize("provider", ["other", "ChatGPT", "claude:extra"])
def test_unrecognized_provider_refused(provider: str) -> None:
    with pytest.raises(LifecycleError):
        _envelope(provider)


@pytest.mark.parametrize("cloud,external", [(True, False), (False, True), (True, True)])
def test_no_requested_or_admitted_egress(cloud: bool, external: bool) -> None:
    segment = _envelope()
    privacy = _privacy(cloud=cloud, external=external)
    for envelope in (
        replace(segment.envelope, admitted_privacy=privacy),
        replace(segment.envelope, submission=replace(segment.envelope.submission, privacy=privacy)),
    ):
        with pytest.raises(LifecycleError):
            replace(segment, envelope=envelope)


@pytest.mark.parametrize("change", ["owner", "unknown", "canonical", "route", "reference"])
def test_decoder_refuses_authority_provenance_or_routing_substitution(change: str) -> None:
    value: dict[str, Any] = json.loads(_envelope().to_bytes())
    capture = value["capture"]["submission"]
    if change == "owner":
        capture["submission_path"] = "owner"
    elif change == "unknown":
        capture["source_origin"] = "unknown"
        capture["provenance"]["content_origin"] = "unknown"
    elif change == "canonical":
        capture["action"] = CaptureAction.CANONICAL_NOTE.value
    elif change == "route":
        capture["space_id"] = "space_" + _UUID
    else:
        capture["source_reference"] += "0"
        capture["provenance"]["source_ref"] = capture["source_reference"]
    with pytest.raises(LifecycleError, match="^invalid_evidence$"):
        ConsumerSegmentEnvelope.from_bytes(canonical_bytes(value))


@pytest.mark.parametrize("change", ["extra", "missing", "bool", "reservation", "source"])
def test_decoder_refuses_closed_schema_and_typed_tampering(change: str) -> None:
    value: dict[str, Any] = json.loads(_envelope().to_bytes())
    if change == "extra":
        value["extra"] = True
    elif change == "missing":
        del value["normalized_sha256"]
    elif change == "bool":
        value["segment_index"] = False
    elif change == "reservation":
        value["reservation"]["canonical"] = True
    else:
        value["source"]["segment_key"] = "f" * 64
    with pytest.raises(LifecycleError, match="^invalid_evidence$"):
        ConsumerSegmentEnvelope.from_bytes(canonical_bytes(value))


def test_decoder_refuses_noncanonical_duplicate_utf8_and_oversize_bytes() -> None:
    raw = _envelope().to_bytes()
    for damaged in (
        b" " + raw, raw + b"\n", b"\xff", b"", b'{"x":1,"x":1}',
        b"x" * (MAX_CONSUMER_SEGMENT_BYTES + 1),
    ):
        with pytest.raises(LifecycleError, match="^invalid_evidence$"):
            ConsumerSegmentEnvelope.from_bytes(damaged)


def test_oversize_encoded_envelope_refused_at_construction() -> None:
    segment = _envelope()
    privacy = replace(_privacy(), policy_version="p" * MAX_CONSUMER_SEGMENT_BYTES)
    with pytest.raises(LifecycleError, match="^invalid_arguments$"):
        replace(segment, envelope=replace(segment.envelope, admitted_privacy=privacy))


def test_frozen_owner_segment_decoder_refuses_consumer_and_public_job_envelopes() -> None:
    segment = _envelope()
    with pytest.raises(LifecycleError):
        LifecycleSegment(segment.envelope, segment.identities)
    with pytest.raises(LifecycleError):
        LifecycleSegment.from_bytes(segment.to_bytes())


def test_nested_values_returned_by_journal_do_not_alias_custody() -> None:
    segment = _envelope()
    before = segment.to_bytes()
    value: dict[str, Any] = json.loads(before)
    value["capture"] = segment.envelope.to_dict()
    value["source"] = segment.source.value()
    value["capture"]["submission"]["role_claim"]["capabilities"].append("owner.capture")
    value["source"]["namespace_id"] = "replaced"
    assert segment.to_bytes() == before
    with pytest.raises(LifecycleError):
        ConsumerSegmentEnvelope.from_bytes(canonical_bytes(value))


def test_segment_key_must_derive_from_conversation_and_ordinal() -> None:
    segment = _envelope()
    arbitrary_key = "b" * 64
    with pytest.raises(LifecycleError, match="^invalid_arguments$"):
        replace(segment, segment_key=arbitrary_key,
                source=replace(segment.source, segment_key=arbitrary_key))
    value: dict[str, Any] = json.loads(segment.to_bytes())
    value["segment_key"] = arbitrary_key
    value["source"]["segment_key"] = arbitrary_key
    with pytest.raises(LifecycleError, match="^invalid_evidence$"):
        ConsumerSegmentEnvelope.from_bytes(canonical_bytes(value))


@pytest.mark.parametrize("intent", [None, Intent.REFERENCE])
def test_reference_or_absent_intent_roundtrips(intent: Intent | None) -> None:
    segment = _envelope()
    updated = replace(segment, envelope=replace(
        segment.envelope, submission=replace(segment.envelope.submission, intent=intent)
    ))
    assert ConsumerSegmentEnvelope.from_bytes(updated.to_bytes()) == updated


@pytest.mark.parametrize("intent", [Intent.HOLD, Intent.IDEA, Intent.ACTION_CANDIDATE])
def test_other_intents_refused_at_construction_and_decode(intent: Intent) -> None:
    segment = _envelope()
    if intent is Intent.HOLD:
        with pytest.raises(LifecycleError, match="^invalid_arguments$"):
            replace(segment, envelope=replace(
                segment.envelope, submission=replace(segment.envelope.submission, intent=intent)
            ))
    value: dict[str, Any] = json.loads(segment.to_bytes())
    value["capture"]["submission"]["intent"] = intent.value
    with pytest.raises(LifecycleError, match="^invalid_evidence$"):
        ConsumerSegmentEnvelope.from_bytes(canonical_bytes(value))


def test_owner_authored_capture_reason_refused() -> None:
    value: dict[str, Any] = json.loads(_envelope().to_bytes())
    submission = value["capture"]["submission"]
    submission["capture_why"] = "Synthetic owner reason"
    submission["capture_why_origin"] = CaptureWhyOrigin.OWNER_AUTHORED.value
    submission["provenance"]["owner_context"] = CaptureWhyOrigin.OWNER_AUTHORED.value
    with pytest.raises(LifecycleError, match="^invalid_evidence$"):
        ConsumerSegmentEnvelope.from_bytes(canonical_bytes(value))
