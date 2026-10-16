"""Synthetic parser values never activate consumer profiles or grant capture authority."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from typing import cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.consumer_contracts import (
    MAX_CONVERSATION_DTO_BYTES,
    MAX_MESSAGE_BYTES,
    OMISSION_KEYS,
)
from open_brain_engine.engine.consumer_plans import (
    MAX_CONVERSATIONS,
    MAX_PLAN_MESSAGES,
    MAX_TRANSCRIPT_BYTES,
    ConsumerConversationInput,
    ConsumerImportPlan,
    ConsumerMessageInput,
    validated_scope_label,
)
from open_brain_engine.engine.consumer_profiles import ConsumerIntakeError

OPERATION = "00000000-0000-4000-8000-000000000001"
SCOPE = "scope_00000000-0000-4000-8000-000000000002"


def _message(
    identity: str = "message", text: str = "Synthetic input", role: str = "user"
) -> ConsumerMessageInput:
    return ConsumerMessageInput(provider_message_id=identity, role=role, text=text)


def _conversation(
    identity: str = "conversation",
    messages: tuple[ConsumerMessageInput, ...] | None = None,
    *,
    title: str | None = "Synthetic title",
) -> ConsumerConversationInput:
    return ConsumerConversationInput(
        provider_conversation_id=identity,
        title=title,
        messages=(_message(),) if messages is None else messages,
        omissions=dict.fromkeys(OMISSION_KEYS, 0),
    )


def _plan(
    conversations: tuple[ConsumerConversationInput, ...] | None = None, *, provider: str = "chatgpt"
) -> ConsumerImportPlan:
    return ConsumerImportPlan(
        operation_id=OPERATION,
        provider=provider,
        profile_id=f"synthetic-{provider}-internal-v1",
        archive_sha256="1" * 64,
        conversations=(_conversation(),) if conversations is None else conversations,
        new_scope_label="Synthetic account",
    )


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_complete_detached_graph_round_trips_for_both_providers(provider: str) -> None:
    plan = _plan(provider=provider)
    assert plan.privacy_tier is PrivacyTier.PERSONAL
    assert plan.replace_current is False
    assert ConsumerImportPlan.from_bytes(plan.to_bytes(), plan.objects()) == plan
    assert ConsumerImportPlan.from_value(plan.value(), plan.objects()) == plan
    assert ConsumerImportPlan.from_objects(plan.to_bytes(), plan.objects()) == plan
    conversation = plan.conversations[0]
    assert (
        ConsumerConversationInput.from_bytes(conversation.to_bytes(), conversation.objects())
        == conversation
    )
    assert (
        ConsumerConversationInput.from_objects(conversation.to_bytes(), conversation.objects())
        == conversation
    )
    message = conversation.messages[0]
    assert ConsumerMessageInput.from_bytes(message.to_bytes()) == message
    assert len(plan.objects()) == 3
    assert plan.input_sha256 == sha256(plan.to_bytes()).hexdigest()
    assert conversation.input_sha256 in plan.objects()
    assert all(sha256(raw).hexdigest() == key for key, raw in plan.objects().items())


def test_empty_conversation_retains_protected_identity_and_omissions_without_capture() -> None:
    conversation = _conversation("empty", (), title=None)
    plan = _plan((conversation,))
    assert conversation.segment_count == 0
    assert conversation.transcript_bytes == 0
    assert conversation.messages == () and conversation.title is None
    assert ConsumerImportPlan.from_bytes(plan.to_bytes(), plan.objects()) == plan
    identity = json.loads(conversation.identity_bytes())
    assert identity["provider_conversation_id"] == {"utf8_hex": b"empty".hex()}
    assert identity["provider_message_ids"] == []
    content = json.loads(conversation.content_bytes())
    assert content["messages"] == [] and content["omissions"] == dict.fromkeys(OMISSION_KEYS, 0)
    assert (
        ConsumerConversationInput(
            provider_conversation_id="empty", messages=(), omissions=dict.fromkeys(OMISSION_KEYS, 0)
        ).title
        is None
    )


def test_normalization_is_separate_from_exact_identity_bytes() -> None:
    raw = "e\u0301\r\n\x1b[31m\u202e"
    message = _message("e\u0301", raw)
    conversation = _conversation("e\u0301", (message,), title="e\u0301\rtitle")
    assert message.text == "é\n\\u001B[31m\\u202E"
    assert conversation.title == "é\ntitle"
    assert message.provider_message_id == "e\u0301"
    assert conversation.provider_conversation_id == "e\u0301"
    assert message.value()["provider_message_id"] == {"utf8_hex": "e\u0301".encode().hex()}
    assert _message("é", raw).input_sha256 != message.input_sha256
    assert (
        _conversation("é", (message,), title=conversation.title).input_sha256
        != conversation.input_sha256
    )
    assert _message(text="e\u0301") == _message(text="é")
    assert ConsumerMessageInput.from_bytes(message.to_bytes()) == message
    assert (
        ConsumerConversationInput.from_bytes(conversation.to_bytes(), conversation.objects())
        == conversation
    )


def test_conversation_order_is_exact_utf8_hex_and_input_hash_binds_every_choice() -> None:
    first, second = _conversation("é"), _conversation("e\u0301")
    plan = _plan((first, second))
    assert plan.conversations == (second, first)
    assert plan.input_sha256 == _plan((second, first)).input_sha256
    for changed in (
        replace(plan, operation_id="00000000-0000-4000-8000-000000000003"),
        replace(plan, provider="claude"),
        replace(plan, profile_id="synthetic-other-v1"),
        replace(plan, archive_sha256="2" * 64),
        replace(plan, new_scope_label="Other account"),
        replace(plan, scope_id=SCOPE, new_scope_label=None),
        replace(plan, privacy_tier=PrivacyTier.WORK),
        replace(plan, replace_current=True),
        replace(plan, conversations=(_conversation("changed"),)),
    ):
        assert changed.input_sha256 != plan.input_sha256
    root = plan.value()
    cast(list[object], root["conversation_sha256"]).reverse()
    with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
        ConsumerImportPlan.from_value(root, plan.objects())


def test_strict_scope_selectors_and_uuid4_grammar() -> None:
    plan = _plan()
    assert replace(plan, scope_id=SCOPE, new_scope_label=None).scope_id == SCOPE
    assert replace(plan, new_scope_label="e\u0301").new_scope_label == "é"
    assert len(cast(str, replace(plan, new_scope_label="x" * 120).new_scope_label)) == 120
    for options in (
        {"scope_id": SCOPE},
        {"new_scope_label": None},
        {"scope_id": "scope_bad", "new_scope_label": None},
        {"scope_id": OPERATION, "new_scope_label": None},
        {"new_scope_label": ""},
        {"new_scope_label": "x" * 121},
        {"new_scope_label": "line\nbreak"},
        {"new_scope_label": "bidi\u202e"},
        {"operation_id": "00000000-0000-1000-8000-000000000001"},
        {"operation_id": "00000000-0000-4000-8000-00000000000A"},
    ):
        value = {**plan.value(), **options}
        with pytest.raises(ConsumerIntakeError):
            ConsumerImportPlan.from_value(value, plan.objects())


@pytest.mark.parametrize(
    "value", [None, True, "", "x" * 121, "\x00", "\n", "\x7f", "\x85", "\u202e", "\ud800"]
)
def test_shared_scope_label_validator_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ConsumerIntakeError):
        validated_scope_label(value)
    assert validated_scope_label("e\u0301") == "é"
    assert validated_scope_label("x" * 120) == "x" * 120


@pytest.mark.parametrize("invalid", [True, False, -1, 1.0, "1", None])
def test_omission_counts_are_exact_nonnegative_integers(invalid: object) -> None:
    omissions: dict[str, object] = dict.fromkeys(OMISSION_KEYS, 0)
    omissions["tool_messages"] = invalid
    with pytest.raises(ConsumerIntakeError):
        replace(_conversation(), omissions=cast(Mapping[str, int], omissions))


@pytest.mark.parametrize("invalid", ["", "\x00", "\x7f", "\x85", "\u200e", "\ud800", "é" * 513])
def test_invalid_provider_ids_refuse_without_normalizing_them(invalid: str) -> None:
    with pytest.raises(ConsumerIntakeError):
        _message(invalid)
    with pytest.raises(ConsumerIntakeError):
        _conversation(invalid)
    assert _message("é" * 512).provider_message_id == "é" * 512
    assert _message(" ID ").provider_message_id == " ID "


def test_bad_text_role_and_guessed_or_non_utc_dates_refuse() -> None:
    for options in (
        {"text": ""},
        {"text": "\0"},
        {"text": "\ud800"},
        {"role": "system"},
        {"occurred_at": "yesterday"},
        {"occurred_at": "2026-02-30T00:00:00Z"},
        {"occurred_at": "2026-01-01T00:00:00+00:00"},
    ):
        with pytest.raises(ConsumerIntakeError):
            replace(_message(), **options)
    assert replace(_message(), occurred_at="2026-01-01T00:00:00.123Z").occurred_at
    assert _message(text=" \t\n").text == " \t\n"


def test_duplicates_refuse_even_when_content_is_identical() -> None:
    with pytest.raises(ConsumerIntakeError):
        _conversation(messages=(_message(), _message()))
    with pytest.raises(ConsumerIntakeError):
        _plan((_conversation(), _conversation()))
    assert _conversation(messages=(_message("é"), _message("e\u0301")))


def test_defensive_values_and_objects_have_no_mutable_aliases() -> None:
    counts = dict.fromkeys(OMISSION_KEYS, 0)
    conversation = replace(_conversation(), omissions=counts)
    plan = _plan((conversation,))
    before = plan.input_sha256
    counts["tool_messages"] = 9
    objects = plan.objects()
    objects.clear()
    value = plan.value()
    cast(list[object], value["conversation_sha256"]).clear()
    assert plan.input_sha256 == before
    with pytest.raises(TypeError):
        cast(dict[str, int], conversation.omissions)["tool_messages"] = 9
    with pytest.raises(FrozenInstanceError):
        plan.operation_id = "changed"  # type: ignore[misc]


def test_wire_extra_fields_and_authority_fields_refuse() -> None:
    plan = _plan()
    for field in ("namespace_id", "source_id", "capture_id", "role_claim", "session_id"):
        with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
            ConsumerImportPlan.from_value({**plan.value(), field: "injected"}, plan.objects())
    with pytest.raises(ConsumerIntakeError):
        ConsumerMessageInput.from_value({**_message().value(), "owner": True})
    conversation = plan.conversations[0]
    for key in ("provider_identity_sha256", "content_sha256"):
        for extra in ("extra", "namespace_id"):
            root, objects = conversation.value(), conversation.objects()
            payload = json.loads(objects[cast(str, root[key])])
            payload[extra] = "injected"
            raw = canonical(payload)
            root[key] = sha256(raw).hexdigest()
            objects[sha256(raw).hexdigest()] = raw
            with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
                ConsumerConversationInput.from_value(root, objects)


def test_graph_missing_wrong_or_noncanonical_objects_and_ref_duplicates_refuse() -> None:
    plan = _plan()
    for key in plan.objects():
        objects = plan.objects()
        del objects[key]
        with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
            ConsumerImportPlan.from_bytes(plan.to_bytes(), objects)
        objects[key] = b"{}"
        with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
            ConsumerImportPlan.from_bytes(plan.to_bytes(), objects)
    root = plan.value()
    refs = cast(list[object], root["conversation_sha256"])
    refs.append(refs[0])
    with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
        ConsumerImportPlan.from_value(root, plan.objects())
    conversation = plan.conversations[0]
    root, objects = conversation.value(), conversation.objects()
    content = json.loads(conversation.content_bytes())
    raw = json.dumps(content).encode()
    root["content_sha256"] = sha256(raw).hexdigest()
    objects[sha256(raw).hexdigest()] = raw
    with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
        ConsumerConversationInput.from_value(root, objects)
    for raw in (plan.to_bytes() + b"\n", b'{"contract":1,"contract":2}', b"\xff"):
        with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
            ConsumerImportPlan.from_bytes(raw, plan.objects())


def test_32_mib_objects_preserve_maximum_ids_and_large_control_text() -> None:
    # Combined hexadecimal IDs and escaped LF text exceed32MiB; each detached object fits.
    messages = tuple(_message("x" * 1019 + f"{i:05}", "\n" * 700) for i in range(10_000))
    conversation = _conversation(messages=messages)
    identity, content = conversation.identity_bytes(), conversation.content_bytes()
    assert len(identity) + len(content) > MAX_CONVERSATION_DTO_BYTES
    assert len(identity) <= MAX_CONVERSATION_DTO_BYTES
    assert len(content) <= MAX_CONVERSATION_DTO_BYTES
    plan = _plan((conversation,))
    assert len(plan.to_bytes()) < 1024
    restored = ConsumerImportPlan.from_bytes(plan.to_bytes(), plan.objects())
    assert restored.conversations == plan.conversations


def test_message_bytes_and_128_segments_exact_and_one_past() -> None:
    assert _conversation(messages=(_message(text="😀" * (MAX_MESSAGE_BYTES // 4)),))
    with pytest.raises(ConsumerIntakeError, match="conversation_limit"):
        _message(text="😀" * (MAX_MESSAGE_BYTES // 4) + "x")
    conversation = _conversation(messages=(_message(text="x" * (128 * 60_000 - 8)),))
    assert conversation.segment_count == 128
    assert conversation.transcript_bytes == 128 * 60_000 + sum(
        len(f"Transcript segment {i}:\n") for i in range(128)
    )
    with pytest.raises(ConsumerIntakeError, match="conversation_limit"):
        _conversation(messages=(_message(text="x" * (128 * 60_000 - 7)),))
    with pytest.raises(ConsumerIntakeError, match="conversation_limit"):
        _conversation(messages=tuple(_message(str(i)) for i in range(10_001)))
    assert _conversation(title="x" * 200)
    with pytest.raises(ConsumerIntakeError):
        _conversation(title="x" * 201)


def test_aggregate_transcript_limit_counts_labels_and_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert MAX_TRANSCRIPT_BYTES == 64 * 1024 * 1024
    first, second = (
        _conversation("a", (_message(text="x"),)),
        _conversation("b", (_message(text="x"),)),
    )
    total = first.transcript_bytes + second.transcript_bytes
    monkeypatch.setattr("open_brain_engine.engine.consumer_plans.MAX_TRANSCRIPT_BYTES", total)
    assert _plan((first, second))
    with pytest.raises(ConsumerIntakeError, match="conversation_limit"):
        _plan((first, _conversation("b", (_message(text="xx"),))))


def test_plan_conversation_and_message_count_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_CONVERSATIONS == 10_000 and MAX_PLAN_MESSAGES == 100_000
    conversations = tuple(_conversation(str(i), ()) for i in range(MAX_CONVERSATIONS))
    assert len(_plan(conversations).conversations) == MAX_CONVERSATIONS
    with pytest.raises(ConsumerIntakeError, match="conversation_limit"):
        _plan(conversations + (_conversation("one-more", ()),))
    monkeypatch.setattr("open_brain_engine.engine.consumer_plans.MAX_PLAN_MESSAGES", 2)
    assert _plan((_conversation(messages=(_message("1"), _message("2"))),))
    with pytest.raises(ConsumerIntakeError, match="conversation_limit"):
        _plan((_conversation(messages=(_message("1"), _message("2"), _message("3"))),))


def test_strict_tuples_replace_flag_profile_and_privacy() -> None:
    plan = _plan()
    for value in (0, 1, "true", None):
        with pytest.raises(ConsumerIntakeError):
            ConsumerImportPlan.from_value(
                {**plan.value(), "replace_current": value}, plan.objects()
            )
    with pytest.raises(ConsumerIntakeError):
        ConsumerImportPlan.from_value({**plan.value(), "privacy_tier": "cloud"}, plan.objects())
    with pytest.raises(ConsumerIntakeError):
        replace(plan, profile_id="MixedCase")
    with pytest.raises(ConsumerIntakeError):
        replace(
            plan,
            conversations=cast(tuple[ConsumerConversationInput, ...], list(plan.conversations)),
        )
    with pytest.raises(ConsumerIntakeError):
        replace(_conversation(), messages=cast(tuple[ConsumerMessageInput, ...], [_message()]))
