"""Joint consumer plan/custody proof over bounded content-addressed objects.

This value proves internal consistency, not active profile/owner approval or
source CAS. Live admission checks those authorities before reserving work.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .capture_lifecycle_contracts import canonical_bytes, decode_canonical, validate_digest
from .consumer_contracts import ConsumerConversation, conversation_key, message_key
from .consumer_profiles import (
    ConsumerIntakeError,
    ConsumerProfileRecord,
    consumer_capture_context,
)
from .consumer_segments import ConsumerSegmentEnvelope
from .contracts import LocalEngineContext

_FIELDS = {
    "contract", "profile_sha256", "normalized_sha256", "provider_identity_sha256", "segment_sha256"
}


def _provider_identifier(raw: object) -> str:
    if type(raw) is not str or not 2 <= len(raw) <= 2048:
        raise ConsumerIntakeError("invalid_evidence")
    try:
        data = bytes.fromhex(raw)
        if data.hex() != raw:
            raise ValueError()
        return data.decode("utf-8", "strict")
    except ValueError, UnicodeError:
        raise ConsumerIntakeError("invalid_evidence") from None


@dataclass(frozen=True, slots=True)
class PreparedConsumerConversation:
    profile: ConsumerProfileRecord
    normalized: ConsumerConversation
    provider_conversation_utf8_hex: str
    provider_message_utf8_hex: tuple[str, ...]
    segments: tuple[ConsumerSegmentEnvelope, ...]

    def __post_init__(self) -> None:
        if type(self.profile) is not ConsumerProfileRecord or (
            type(self.normalized) is not ConsumerConversation
        ):
            raise ConsumerIntakeError("invalid_evidence")
        object.__setattr__(self, "provider_message_utf8_hex", tuple(self.provider_message_utf8_hex))
        object.__setattr__(self, "segments", tuple(self.segments))
        conversation = self.normalized
        if (conversation.provider, conversation.profile_id) != (
            self.profile.provider, self.profile.profile_id
        ) or len(self.provider_message_utf8_hex) != len(conversation.messages):
            raise ConsumerIntakeError("invalid_evidence")
        try:
            raw_id = _provider_identifier(self.provider_conversation_utf8_hex)
            computed_key = conversation_key(
                conversation.provider, conversation.import_namespace_id, raw_id
            )
            if computed_key != conversation.conversation_key:
                raise ValueError()
            for raw, message in zip(
                self.provider_message_utf8_hex, conversation.messages, strict=True
            ):
                if message_key(conversation.conversation_key, _provider_identifier(raw)) != (
                    message.message_key
                ):
                    raise ValueError()
            normalized_hash = sha256(conversation.to_bytes()).hexdigest()
            if len(self.segments) != len(conversation.segments):
                raise ValueError()
            bodies = conversation.segment_bodies
            capture_ids: set[str] = set()
            receipt_ids: set[str] = set()
            source_ids: set[str] = set()
            for index, (segment, descriptor) in enumerate(zip(
                self.segments, conversation.segments, strict=True
            )):
                if type(segment) is not ConsumerSegmentEnvelope:
                    raise ValueError()
                submission = segment.envelope.submission
                if (
                    segment.conversation_key != conversation.conversation_key
                    or segment.conversation_revision_sha256 != (
                        conversation.conversation_revision_sha256
                    )
                    or segment.normalized_sha256 != normalized_hash
                    or segment.segment_index != index
                    or segment.segment_key != descriptor.segment_key
                    or segment.source.namespace_id != conversation.import_namespace_id
                    or submission.title != conversation.title
                    or submission.source_reference != (
                        f"urn:open-brain:consumer-chat:{conversation.provider}:"
                        f"{conversation.conversation_key}:{index}"
                    )
                    or segment.body != bodies[index].encode("utf-8")
                    or submission.request_sha256() != descriptor.capture_request_sha256
                    or segment.source.source_id in source_ids
                    or segment.identities.capture_id in capture_ids
                    or segment.identities.accepted_receipt_id in receipt_ids
                ):
                    raise ValueError()
                capture_ids.add(segment.identities.capture_id)
                receipt_ids.add(segment.identities.accepted_receipt_id)
                source_ids.add(segment.source.source_id)
        except ValueError, TypeError, AttributeError, RecursionError:
            raise ConsumerIntakeError("invalid_evidence") from None

    def validate_profile(self, profile: LocalEngineContext) -> None:
        """Live/restore caller must bind evidence to the actual engine-owned identity."""
        expected = consumer_capture_context(profile)
        for segment in self.segments:
            submission = segment.envelope.submission
            submission.validate_profile(profile)
            if (submission.tenant_id, submission.actor_id, submission.role_claim) != (
                expected.tenant_id, expected.actor_id, expected.role_claim
            ):
                raise ConsumerIntakeError("invalid_evidence")

    def value(self) -> dict[str, object]:
        return {
            "contract": "ob.consumer-conversation-intent.v1",
            "profile_sha256": self.profile.record_sha256,
            "normalized_sha256": sha256(self.normalized.to_bytes()).hexdigest(),
            "provider_identity_sha256": sha256(self.identity_bytes()).hexdigest(),
            "segment_sha256": [sha256(segment.to_bytes()).hexdigest() for segment in self.segments],
        }

    def to_bytes(self) -> bytes:
        return canonical_bytes(self.value())

    @property
    def intent_sha256(self) -> str:
        return sha256(self.to_bytes()).hexdigest()

    def identity_bytes(self) -> bytes:
        # Ten thousand1024-byte provider IDs need up to20MiB after hex encoding;
        # store that bounded object separately from the16MiB journal root.
        raw = canonical({
            "contract": "ob.consumer-provider-identity.v1",
            "conversation_utf8_hex": self.provider_conversation_utf8_hex,
            "message_utf8_hex": list(self.provider_message_utf8_hex),
        })
        if len(raw) > 32 * 1024 * 1024:
            raise ConsumerIntakeError("conversation_limit")
        return raw

    def objects(self) -> dict[str, bytes]:
        """Detached referenced bytes; no whole-conversation envelope duplication."""
        payloads = [self.profile.to_bytes(), self.normalized.to_bytes(), self.identity_bytes(),
                    *(segment.to_bytes() for segment in self.segments)]
        return {sha256(raw).hexdigest(): raw for raw in payloads}

    @classmethod
    def from_objects(cls, raw: bytes, objects: Mapping[str, bytes]) -> PreparedConsumerConversation:
        try:
            value = decode_canonical(raw)
            if type(value) is not dict or set(value) != _FIELDS or (
                value["contract"] != "ob.consumer-conversation-intent.v1"
            ):
                raise ValueError()
            segments = value["segment_sha256"]
            if type(segments) is not list or not 1 <= len(segments) <= 128:
                raise ValueError()

            def payload(key: object) -> bytes:
                digest = validate_digest(key)
                retained = objects[digest]
                if type(retained) is not bytes or sha256(retained).hexdigest() != digest:
                    raise ValueError()
                return retained

            identity_raw = payload(value["provider_identity_sha256"])
            if len(identity_raw) > 32 * 1024 * 1024:
                raise ValueError()
            identity = json.loads(identity_raw.decode("utf-8", "strict"))
            if type(identity) is not dict or set(identity) != {
                "contract", "conversation_utf8_hex", "message_utf8_hex"
            } or (identity["contract"] != "ob.consumer-provider-identity.v1"
                  or type(identity["message_utf8_hex"]) is not list
                  or canonical(identity) != identity_raw):
                raise ValueError()
            result = cls(
                ConsumerProfileRecord.from_bytes(payload(value["profile_sha256"])),
                ConsumerConversation.from_bytes(payload(value["normalized_sha256"])),
                cast(str, identity["conversation_utf8_hex"]),
                tuple(identity["message_utf8_hex"]),
                tuple(ConsumerSegmentEnvelope.from_bytes(payload(key)) for key in segments),
            )
            if result.to_bytes() != raw:
                raise ValueError()
            return result
        except ValueError, TypeError, KeyError, AttributeError, RecursionError:
            raise ConsumerIntakeError("invalid_evidence") from None
