"""Synthetic internal contracts, with no activated external provider profile."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
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
from open_brain_engine.engine.consumer_contracts import (
    MAX_CONVERSATION_DTO_BYTES,
    MAX_MESSAGE_BYTES,
    OMISSION_KEYS,
    ConsumerConversation,
    ConsumerMessage,
    ConsumerSegment,
    MessageSpan,
    build_conversation,
    conversation_key,
    message_key,
    normalize_text,
    segment_key,
)
from open_brain_engine.engine.contracts import (
    CaptureSubmission,
    PublicJobCaptureContext,
    TextPayload,
)


def _factory(index: int, body: str, reference: str, title: str) -> CaptureSubmission:
    tenant = "tenant_00000000-0000-4000-8000-000000000001"
    actor = "actor_00000000-0000-4000-8000-000000000002"
    context = PublicJobCaptureContext(
        tenant_id=tenant,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "tenant_id": tenant,
            "role_id": "role_00000000-0000-4000-8000-000000000003",
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000004",
            "capabilities": ["capture.accept"],
        },
    )
    return CaptureSubmission.for_public_job(
        context=context,
        payload=TextPayload(body),
        title=title,
        delivery_id=f"synthetic.consumer.segment.{index}",
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=reference,
        provenance=Provenance.create(
            source_ref=reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.PERSONAL,
            reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
            policy_version="synthetic-consumer-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )


def _message(identity: str = "message-1", text: str = "What did we decide?") -> dict[str, object]:
    return {"provider_message_id": identity, "role": "user", "text": text, "occurred_at": None}


def _build(
    messages: Sequence[Mapping[str, object]] | None = None,
    *,
    provider: str = "chatgpt",
    title: str | bytes | None = "Synthetic discussion",
    omissions: Mapping[str, int] | None = None,
    conversation_id: str = "conversation-1",
) -> ConsumerConversation:
    return build_conversation(
        provider=provider,
        profile_id=f"synthetic-{provider}-internal-v1",
        import_namespace_id="synthetic-import-namespace",
        provider_conversation_id=conversation_id,
        title=title,
        messages=[
            _message(),
            {**_message("message-2", "Keep the first draft."), "role": "assistant"},
        ]
        if messages is None
        else messages,
        omissions=dict.fromkeys(OMISSION_KEYS, 0) if omissions is None else omissions,
        submission_factory=_factory,
    )


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_round_trip_and_exact_capture_request(provider: str) -> None:
    conversation = _build(provider=provider)
    assert ConsumerConversation.from_bytes(conversation.to_bytes()) == conversation
    assert ConsumerConversation.from_value(conversation.value()) == conversation
    assert conversation.segment_bodies == (
        "Transcript segment 0:\nUser:\nWhat did we decide?\n\n"
        "Assistant:\nKeep the first draft.\n\n",
    )
    segment = conversation.segments[0]
    body = conversation.segment_bodies[0]
    reference = f"urn:open-brain:consumer-chat:{provider}:{conversation.conversation_key}:0"
    assert (
        segment.capture_request_sha256
        == _factory(0, body, reference, conversation.title).request_sha256()
    )
    assert segment.body_sha256 == sha256(body.encode()).hexdigest()
    assert (
        ConsumerMessage.from_bytes(conversation.messages[0].to_bytes()) == conversation.messages[0]
    )


def test_frozen_golden_internal_only_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures" / "consumer-chat-v1.json"
    assert fixture.read_bytes() == _build().to_bytes()
    assert ConsumerConversation.from_bytes(fixture.read_bytes()) == _build()


def test_normalization_is_visible_and_idempotent() -> None:
    original = "e\u0301\r\na\rb\t\x1b[31m\x7f\x85\u202e\U000e0001 <b>&amp;</b>"
    expected = "é\na\nb\t\\u001B[31m\\u007F\\u0085\\u202E\\U000E0001 <b>&amp;</b>"
    assert normalize_text(original.encode()) == expected
    assert normalize_text(expected) == expected
    assert _build([_message(text=original)]).messages[0].text == expected


@pytest.mark.parametrize("invalid", ["\0", "\ud800", b"\xff", b"\xed\xa0\x80"])
def test_invalid_unicode_and_nul_refuse(invalid: str | bytes) -> None:
    with pytest.raises(ValueError):
        normalize_text(invalid)


def test_ids_preserve_exact_utf8_scalars_and_are_domain_bound() -> None:
    composed, decomposed = "é", "e\u0301"
    assert portable_canonical_json_bytes([composed]) == portable_canonical_json_bytes([decomposed])
    a = conversation_key("chatgpt", "ns", composed)
    b = conversation_key("chatgpt", "ns", decomposed)
    assert a != b
    assert a != conversation_key("claude", "ns", composed)
    assert a != conversation_key("chatgpt", "another", composed)
    assert message_key(a, composed) != message_key(a, decomposed)
    assert (
        a
        == sha256(
            portable_canonical_json_bytes(
                [
                    "consumer-chat-v1",
                    "chatgpt",
                    "ns",
                    {"utf8_hex": composed.encode().hex()},
                ]
            )
        ).hexdigest()
    )
    assert (
        message_key(a, decomposed)
        == sha256(
            portable_canonical_json_bytes(
                [
                    a,
                    {"utf8_hex": decomposed.encode().hex()},
                ]
            )
        ).hexdigest()
    )
    assert message_key(a, "id") != segment_key(a, 0)
    assert (
        _build(conversation_id=composed).conversation_key
        != _build(conversation_id=decomposed).conversation_key
    )
    assert conversation_key("chatgpt", "ns", " ID ") != conversation_key("chatgpt", "ns", "ID")


@pytest.mark.parametrize(
    "invalid", ["", "x" * 1025, "\0", "\x1f", "\x7f", "\x85", "\u200e", "\ud800"]
)
def test_bad_provider_ids_refuse(invalid: str) -> None:
    with pytest.raises(ValueError):
        conversation_key("chatgpt", "ns", invalid)
    with pytest.raises(ValueError):
        message_key("1" * 64, invalid)


@pytest.mark.parametrize("namespace", ["é", "e\u0301", "x" * 129, "", "has space", "\x00"])
def test_nonopaque_namespaces_refuse_before_identity_hash(namespace: str) -> None:
    with pytest.raises(ValueError, match="namespace"):
        conversation_key("chatgpt", namespace, "id")
    raw = _build().value()
    raw["import_namespace_id"] = namespace
    with pytest.raises(ValueError, match="namespace"):
        ConsumerConversation.from_value(raw)


def test_mutable_inputs_and_outputs_never_alias_contract() -> None:
    raw = _build().value()
    conversation = ConsumerConversation.from_value(raw)
    before = conversation.to_bytes()
    cast(list[dict[str, object]], raw["messages"])[0]["text"] = "changed"
    cast(dict[str, int], raw["omissions"])["tool_messages"] = 99
    cast(list[dict[str, object]], raw["segments"])[0]["message_spans"] = []
    assert conversation.to_bytes() == before
    returned = conversation.value()
    cast(list[object], returned["messages"]).clear()
    assert conversation.to_bytes() == before
    with pytest.raises(TypeError):
        cast(dict[str, int], conversation.omissions)["tool_messages"] = 7
    with pytest.raises(FrozenInstanceError):
        conversation.title = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "field", ["contract_version", "normalization_version", "segmentation_version"]
)
def test_unknown_versions_refuse(field: str) -> None:
    raw = _build().value()
    raw[field] = "next"
    with pytest.raises(ValueError):
        ConsumerConversation.from_value(raw)


@pytest.mark.parametrize("bad_count", [True, False, -1, 1.0, "1", None])
def test_omission_counts_require_exact_nonnegative_int(bad_count: object) -> None:
    raw = _build().value()
    cast(dict[str, object], raw["omissions"])["tool_messages"] = bad_count
    with pytest.raises(ValueError):
        ConsumerConversation.from_value(raw)


def test_extra_fields_and_wrong_collection_types_refuse() -> None:
    raw = _build().value()
    for target in (
        raw,
        cast(list[dict[str, object]], raw["messages"])[0],
        cast(list[dict[str, object]], raw["segments"])[0],
        cast(dict[str, object], raw["omissions"]),
    ):
        target["extra"] = "forbidden"
        with pytest.raises(ValueError):
            ConsumerConversation.from_value(raw)
        del target["extra"]
    raw["messages"] = tuple(cast(list[object], raw["messages"]))
    with pytest.raises(ValueError):
        ConsumerConversation.from_value(raw)


def test_duplicate_json_keys_and_invalid_json_utf8_refuse() -> None:
    for raw in (b'{"provider":"chatgpt","provider":"claude"}', b"\xff", b"{}", b"null"):
        with pytest.raises(ValueError):
            ConsumerConversation.from_bytes(raw)


def test_noncanonical_and_oversized_dto_bytes_refuse() -> None:
    conversation = _build()
    for raw in (conversation.to_bytes() + b"\n", json.dumps(conversation.value()).encode()):
        with pytest.raises(ValueError, match="noncanonical"):
            ConsumerConversation.from_bytes(raw)
    assert MAX_CONVERSATION_DTO_BYTES == 32 * 1024 * 1024
    with pytest.raises(ValueError, match="invalid DTO bytes"):
        ConsumerConversation.from_bytes(b" " * (MAX_CONVERSATION_DTO_BYTES + 1))


def test_dto_output_bound_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = _build()
    monkeypatch.setattr(
        "open_brain_engine.engine.consumer_contracts.MAX_CONVERSATION_DTO_BYTES",
        len(conversation.to_bytes()) - 1,
    )
    with pytest.raises(ValueError, match="DTO byte limit"):
        conversation.to_bytes()


def test_revisions_change_on_edits_but_stable_keys_do_not() -> None:
    first = _build([_message(text="first")])
    edited = _build([_message(text="edited")])
    assert first.conversation_key == edited.conversation_key
    assert first.messages[0].message_key == edited.messages[0].message_key
    assert first.segments[0].segment_key == edited.segments[0].segment_key
    assert first.conversation_revision_sha256 != edited.conversation_revision_sha256
    assert first.membership_sha256 != edited.membership_sha256
    assert _build([_message(text="e\u0301")]) == _build([_message(text="é")])
    assert (
        first.conversation_revision_sha256
        != _build([_message(text="first")], title="Changed title").conversation_revision_sha256
    )


def test_complete_transcript_split_and_message_spans() -> None:
    messages = [
        _message(text="😀" * 60_001),
        {**_message("second", " \t\n" * 20_001), "role": "assistant"},
    ]
    conversation = _build(messages)
    transcript = "User:\n" + "😀" * 60_001 + "\n\nAssistant:\n" + " \t\n" * 20_001 + "\n\n"
    pieces = [
        body.removeprefix(f"Transcript segment {i}:\n")
        for i, body in enumerate(conversation.segment_bodies)
    ]
    assert "".join(pieces) == transcript
    assert all(0 < len(piece) <= 60_000 for piece in pieces)
    for message in conversation.messages:
        spans = [
            span
            for segment in conversation.segments
            for span in segment.message_spans
            if span.message_key == message.message_key
        ]
        assert spans[0].start == 0
        assert spans[-1].end == len(message.text)
        assert all(left.end == right.start for left, right in zip(spans, spans[1:], strict=False))
        assert "".join(message.text[span.start : span.end] for span in spans) == message.text


def test_whitespace_tail_is_admissible_and_has_no_invented_spans() -> None:
    conversation = _build([_message(text="x" * 59_994)])
    assert conversation.segment_bodies[1] == "Transcript segment 1:\n\n\n"
    assert conversation.segments[1].message_spans == ()


def test_exact_128_segment_limit_and_one_past() -> None:
    conversation = _build([_message(text="x" * (128 * 60_000 - 8))])
    assert len(conversation.segments) == 128
    assert len(conversation.segment_bodies[-1]) == len("Transcript segment 127:\n") + 60_000
    with pytest.raises(ValueError, match="segment limit"):
        _build([_message(text="x" * (128 * 60_000 - 7))])


def test_other_conversation_bounds_refuse() -> None:
    assert _build([_message(text="😀" * (MAX_MESSAGE_BYTES // 4))])
    with pytest.raises(ValueError, match="byte limit"):
        _build([_message(text="😀" * (MAX_MESSAGE_BYTES // 4) + "x")])
    assert len(_build([_message(str(i)) for i in range(10_000)]).messages) == 10_000
    with pytest.raises(ValueError):
        _build([_message(str(i)) for i in range(10_001)])
    with pytest.raises(ValueError):
        _build([])
    with pytest.raises(ValueError):
        _build(title="x" * 201)
    assert _build(title="x" * 200).title == "x" * 200


def test_duplicates_empty_text_roles_and_unverified_dates_refuse() -> None:
    for messages in (
        [_message(), _message()],
        [_message(text="")],
        [{**_message(), "role": "system"}],
        [{**_message(), "occurred_at": "yesterday"}],
        [{**_message(), "occurred_at": "2026-02-30T00:00:00Z"}],
        [{**_message(), "occurred_at": "2026-01-01T00:00:00+00:00"}],
    ):
        with pytest.raises(ValueError):
            _build(messages)
    assert _build([{**_message(), "occurred_at": "2026-01-01T00:00:00Z"}])


def test_manifest_digests_spans_and_indices_are_revalidated() -> None:
    conversation = _build()
    with pytest.raises(ValueError):
        replace(conversation, conversation_revision_sha256="0" * 64)
    with pytest.raises(ValueError):
        replace(conversation, membership_sha256="0" * 64)
    segment = conversation.segments[0]
    for bad in (
        replace(segment, segment_index=1),
        replace(segment, body_sha256="0" * 64),
        replace(segment, message_spans=()),
        replace(segment, segment_key="0" * 64),
    ):
        with pytest.raises(ValueError):
            replace(conversation, segments=(bad,))
    with pytest.raises(ValueError):
        MessageSpan("0" * 64, False, 1)
    with pytest.raises(ValueError):
        ConsumerSegment(True, "0" * 64, "0" * 64, (), "0" * 64)


def test_callback_must_preserve_capture_body_title_and_provenance_boundary() -> None:
    def bad_factory(index: int, body: str, reference: str, title: str) -> CaptureSubmission:
        return replace(_factory(index, body, reference, title), payload=TextPayload("changed"))

    with pytest.raises(ValueError, match="factory changed"):
        build_conversation(
            provider="chatgpt",
            profile_id="synthetic-internal-v1",
            import_namespace_id="namespace",
            provider_conversation_id="conversation",
            title=None,
            messages=[_message()],
            omissions=dict.fromkeys(OMISSION_KEYS, 0),
            submission_factory=bad_factory,
        )


def test_serialized_json_is_plain_utf8_and_has_no_provider_id_fields() -> None:
    conversation = _build([_message("private-message-id", "é")], conversation_id="private-id")
    data = conversation.to_bytes()
    assert b"private-id" not in data and b"private-message-id" not in data
    assert "é" in data.decode()
    assert json.loads(data) == conversation.value()


@pytest.mark.parametrize("mode", ["capabilities", "egress", "provenance", "intent"])
def test_factory_cannot_supply_broader_authority(mode: str) -> None:
    def factory(index: int, body: str, reference: str, title: str) -> CaptureSubmission:
        original = _factory(index, body, reference, title)
        if mode == "capabilities":
            return replace(original, role_claim={**dict(original.role_claim),
                            "capabilities": ["capture.accept", "owner.capture"]})
        if mode == "egress":
            return replace(original, privacy=PrivacyDecision.create(
                tier=PrivacyTier.WORK, reason=PrivacyReason.POLICY_WORK,
                policy_version="synthetic", authority=Authority(cloud=True, external_egress=True),
            ))
        if mode == "provenance":
            return replace(original, provenance=Provenance.create(
                source_ref=reference, content_origin=ContentOrigin.UNKNOWN,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            ))
        return replace(original, intent=Intent.HOLD)

    with pytest.raises(ValueError):
        build_conversation(
            provider="chatgpt", profile_id="synthetic-internal-v1",
            import_namespace_id="namespace", provider_conversation_id="conversation", title=None,
            messages=[_message()], omissions=dict.fromkeys(OMISSION_KEYS, 0),
            submission_factory=factory,
        )


def test_reference_intent_is_allowed_without_additional_authority() -> None:
    def factory(index: int, body: str, reference: str, title: str) -> CaptureSubmission:
        return replace(_factory(index, body, reference, title), intent=Intent.REFERENCE)

    assert build_conversation(
        provider="claude", profile_id="synthetic-internal-v1", import_namespace_id="namespace",
        provider_conversation_id="conversation", title=None, messages=[_message()],
        omissions=dict.fromkeys(OMISSION_KEYS, 0), submission_factory=factory,
    ).segments
