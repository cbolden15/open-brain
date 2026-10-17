"""Profile activation is a trusted engine decision, not transcript metadata."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.engine import consumer_profiles as profiles
from open_brain_engine.engine.consumer_profiles import (
    ConsumerIntakeError,
    ConsumerProfileRecord,
    consumer_capture_context,
    require_active_consumer_profile,
)

from open_brain.profile import compile_single_user_local


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_official_and_unknown_profiles_remain_inactive(provider: str) -> None:
    for profile in (provider + "-consumer-v1", "unknown", "synthetic-" + provider):
        with pytest.raises(ConsumerIntakeError, match="^unsupported_profile$"):
            require_active_consumer_profile(provider, profile)


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_synthetic_activation_is_only_an_explicit_test_registry(
    provider: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = ConsumerProfileRecord(provider, "synthetic-" + provider, "a" * 64)
    assert ConsumerProfileRecord.from_bytes(record.to_bytes()) == record
    assert record.record_sha256 == sha256(record.to_bytes()).hexdigest()
    with pytest.raises(ConsumerIntakeError):
        require_active_consumer_profile(provider, record.profile_id)
    monkeypatch.setattr(profiles, "_ACTIVE_PROFILES", {record.profile_id: record})
    assert require_active_consumer_profile(provider, record.profile_id) is record
    other = "claude" if provider == "chatgpt" else "chatgpt"
    with pytest.raises(ConsumerIntakeError):
        require_active_consumer_profile(other, record.profile_id)
    field_name = "provider"
    with pytest.raises(FrozenInstanceError):
        setattr(record, field_name, other)
    with pytest.raises(ValueError):
        ConsumerProfileRecord.from_bytes(record.to_bytes() + b"\n")


def test_capture_context_is_stable_capture_only_and_not_owner(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "synthetic-brain")
    context = consumer_capture_context(profile)
    assert context == consumer_capture_context(profile)
    assert context.actor_id != profile.owner_actor_id
    assert context.role_claim["role_id"] != profile.owner_role_claim["role_id"]
    assert context.role_claim["role_claim_id"] != profile.owner_role_claim["role_claim_id"]
    assert context.role_claim["capabilities"] == ("capture.accept",)
    with pytest.raises(ValueError):
        replace(context, actor_id=profile.owner_actor_id).validate_profile(profile)


@pytest.mark.parametrize("value", ["", "ChatGPT", "synthetic_chatgpt", "café", "x" * 97])
def test_profile_ids_have_a_bounded_explicit_grammar(value: str) -> None:
    with pytest.raises(ConsumerIntakeError):
        ConsumerProfileRecord("chatgpt", value, "a" * 64)


def test_retained_profile_record_does_not_implicitly_register() -> None:
    record = ConsumerProfileRecord("chatgpt", "synthetic-chatgpt", "a" * 64)
    raw = record.to_bytes().replace(b'"ob.consumer-profile.v1"', b'"ob.consumer-profile.v2"')
    with pytest.raises(ConsumerIntakeError):
        ConsumerProfileRecord.from_bytes(raw)
    with pytest.raises(ValueError):
        ConsumerProfileRecord.from_bytes(record.to_bytes().replace(b'"provider"', b'"active"'))
    with pytest.raises(ConsumerIntakeError):
        require_active_consumer_profile(record.provider, record.profile_id)
