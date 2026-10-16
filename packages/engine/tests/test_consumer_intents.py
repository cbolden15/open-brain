"""Joint normalized/custody preimages are stronger than either decoder alone."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.capture_recovery import CaptureReservationIdentity
from open_brain_engine.engine.consumer_contracts import OMISSION_KEYS, build_conversation
from open_brain_engine.engine.consumer_intents import PreparedConsumerConversation
from open_brain_engine.engine.consumer_profiles import (
    ConsumerIntakeError,
    ConsumerProfileRecord,
    consumer_capture_context,
)
from open_brain_engine.engine.consumer_segments import (
    ConsumerSegmentEnvelope,
    ConsumerSourceBinding,
)
from open_brain_engine.engine.contracts import (
    CaptureSubmission,
    JournalEnvelope,
    LocalEngineContext,
)

from open_brain.profile import compile_single_user_local
from packages.engine.tests.test_consumer_contracts import _factory, _message


def _prepared(
    profile: LocalEngineContext, provider: str = "chatgpt",
    messages: Sequence[Mapping[str, object]] | None = None,
) -> PreparedConsumerConversation:
    context = consumer_capture_context(profile)

    def factory(index: int, body: str, reference: str, title: str) -> CaptureSubmission:
        return replace(_factory(index, body, reference, title), tenant_id=context.tenant_id,
                       actor_id=context.actor_id, role_claim=context.role_claim)

    inputs = [_message("exact-message-é")] if messages is None else messages
    normalized = build_conversation(
        provider=provider, profile_id="synthetic-" + provider,
        import_namespace_id="cns_" + str(uuid4()), provider_conversation_id="exact-conversation-é",
        title=None, messages=inputs,
        omissions=dict.fromkeys(OMISSION_KEYS, 0), submission_factory=factory,
    )
    segments = []
    for index, descriptor in enumerate(normalized.segments):
        reference = f"urn:open-brain:consumer-chat:{provider}:{normalized.conversation_key}:{index}"
        submission = factory(index, normalized.segment_bodies[index], reference, normalized.title)
        source = ConsumerSourceBinding(
            namespace_id=normalized.import_namespace_id,
            conversation_key=normalized.conversation_key,
            segment_key=descriptor.segment_key, source_id="source_" + str(uuid4()),
            expected_head=None, head_version=0, route_version=0, lifecycle_version=0,
            lifecycle="active", availability="available", space_id=None,
            effective_privacy_sha256=None, control_epoch=0,
        )
        segments.append(ConsumerSegmentEnvelope(
            normalized.conversation_key, normalized.conversation_revision_sha256,
            sha256(normalized.to_bytes()).hexdigest(), index, descriptor.segment_key,
            JournalEnvelope(submission, submission.privacy), CaptureReservationIdentity.allocate(
                canonical=False, accepted_at="2026-10-16T12:00:00Z",
            ), source,
        ))
    return PreparedConsumerConversation(
        ConsumerProfileRecord(provider, normalized.profile_id, "a" * 64), normalized,
        "exact-conversation-é".encode().hex(),
        tuple(cast(str, message["provider_message_id"]).encode().hex() for message in inputs),
        tuple(segments),
    )


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_complete_object_graph_reopens_with_actual_engine_identity(
    tmp_path: Path, provider: str,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    value = _prepared(profile, provider)
    value.validate_profile(profile)
    restored = PreparedConsumerConversation.from_objects(value.to_bytes(), value.objects())
    assert restored == value
    restored.validate_profile(profile)
    assert value.intent_sha256 == sha256(value.to_bytes()).hexdigest()
    assert b"exact-message" not in value.to_bytes()
    assert b"exact-conversation" not in value.to_bytes()
    objects = value.objects()
    objects.clear()
    assert PreparedConsumerConversation.from_objects(value.to_bytes(), value.objects()) == value
    with pytest.raises(ValueError):
        value.validate_profile(compile_single_user_local(tmp_path / "different-brain"))


def test_consistent_dto_with_wrong_capture_preimage_is_rejected(tmp_path: Path) -> None:
    value = _prepared(compile_single_user_local(tmp_path / "brain"))
    descriptor = replace(value.normalized.segments[0], capture_request_sha256="0" * 64)
    membership = sha256(canonical(["consumer-membership-v1", [descriptor.value()]])).hexdigest()
    normalized = replace(value.normalized, segments=(descriptor,), membership_sha256=membership)
    envelope = replace(
        value.segments[0], normalized_sha256=sha256(normalized.to_bytes()).hexdigest()
    )
    with pytest.raises(ConsumerIntakeError, match="^invalid_evidence$"):
        replace(value, normalized=normalized, segments=(envelope,))


@pytest.mark.parametrize("change", ["identity", "message", "namespace", "provider", "normalized"])
def test_joint_identity_namespace_and_profile_bindings(tmp_path: Path, change: str) -> None:
    value = _prepared(compile_single_user_local(tmp_path / "brain"))
    with pytest.raises(ValueError):
        if change == "identity":
            replace(value, provider_conversation_utf8_hex=b"other-conversation".hex())
        elif change == "message":
            replace(value, provider_message_utf8_hex=(b"other-message".hex(),))
        elif change == "provider":
            replace(value, profile=ConsumerProfileRecord("claude", "synthetic-claude", "a" * 64))
        elif change == "normalized":
            replace(value, segments=(replace(value.segments[0], normalized_sha256="0" * 64),))
        else:
            source = replace(value.segments[0].source, namespace_id="other-namespace")
            replace(value, segments=(replace(value.segments[0], source=source),))


@pytest.mark.parametrize(
    "change", ["missing", "corrupt", "root-extra", "identity-hex", "duplicate-json"]
)
def test_retained_object_graph_corruption_refuses(tmp_path: Path, change: str) -> None:
    value = _prepared(compile_single_user_local(tmp_path / "brain"))
    root, objects = value.value(), value.objects()
    normalized_hash = sha256(value.normalized.to_bytes()).hexdigest()
    if change == "missing":
        del objects[normalized_hash]
    elif change == "corrupt":
        objects[normalized_hash] += b" "
    elif change == "root-extra":
        root["extra"] = True
    elif change == "identity-hex":
        identity = json.loads(value.identity_bytes())
        identity["conversation_utf8_hex"] = "FF"
        raw = canonical(identity)
        objects[sha256(raw).hexdigest()] = raw
        root["provider_identity_sha256"] = sha256(raw).hexdigest()
    else:
        raw = value.to_bytes().replace(b'{', b'{"contract":"forged",', 1)
        with pytest.raises(ConsumerIntakeError):
            PreparedConsumerConversation.from_objects(raw, objects)
        return
    with pytest.raises(ConsumerIntakeError):
        PreparedConsumerConversation.from_objects(canonical(root), objects)


def test_maximum_provider_id_evidence_uses_separate_bounded_object(tmp_path: Path) -> None:
    messages = [_message(f"{index:05d}" + "x" * 1019, "x") for index in range(10_000)]
    value = _prepared(compile_single_user_local(tmp_path / "brain"), messages=messages)
    assert 16 * 1024 * 1024 < len(value.identity_bytes()) <= 32 * 1024 * 1024
    assert len(value.to_bytes()) < 16 * 1024
    assert PreparedConsumerConversation.from_objects(value.to_bytes(), value.objects()) == value
