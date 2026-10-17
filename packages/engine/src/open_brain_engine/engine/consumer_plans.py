"""Normalized parser input, without engine-issued admission or source authority.

Constructors normalize content; serialized evidence must already be canonical.
Exact provider identity bytes are represented as hexadecimal before canonical
JSON encoding, so NFC content normalization never merges provider identities.
The small plan root references separate bounded conversation-input objects.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from uuid import UUID

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier

from .capture_lifecycle_contracts import MAX_CANONICAL_BYTES
from .consumer_contracts import (
    MAX_CONVERSATION_DTO_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES,
    MAX_SEGMENTS,
    OMISSION_KEYS,
    SEGMENT_SCALARS,
    ConsumerMessage,
    normalize_text,
)
from .consumer_profiles import ConsumerIntakeError

MESSAGE_INPUT_CONTRACT = "ob.consumer-message-input.v1"
CONVERSATION_INPUT_CONTRACT = "ob.consumer-conversation-input.v1"
CONVERSATION_IDENTITY_CONTRACT = "ob.consumer-conversation-input-identity.v1"
CONVERSATION_CONTENT_CONTRACT = "ob.consumer-conversation-input-content.v1"
IMPORT_PLAN_CONTRACT = "ob.consumer-import-plan.v1"
MAX_CONVERSATIONS = 10_000
MAX_PLAN_MESSAGES = 100_000
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024


def _string(value: object) -> str:
    if type(value) is not str:
        raise ConsumerIntakeError()
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise ConsumerIntakeError() from None
    return value


def _provider_id(value: object) -> str:
    text = _string(value)
    if not 1 <= len(text.encode("utf-8")) <= 1024 or any(
        ord(c) < 32 or 127 <= ord(c) <= 159 or unicodedata.category(c) == "Cf" for c in text
    ):
        raise ConsumerIntakeError()
    return text


def validated_scope_label(value: object) -> str:
    """Return an NFC owner label; retained evidence callers also require exact equality."""
    label = unicodedata.normalize("NFC", _string(value))
    if not 1 <= len(label) <= 120 or any(
        ord(c) < 32 or 127 <= ord(c) <= 159 or unicodedata.category(c) == "Cf" for c in label
    ):
        raise ConsumerIntakeError()
    return label


def _identity(value: str) -> dict[str, object]:
    return {"utf8_hex": value.encode("utf-8").hex()}


def _fields(value: object, expected: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ConsumerIntakeError("invalid_evidence")
    return cast(dict[str, object], value)


def _raw_identity(value: object) -> str:
    data = _fields(value, {"utf8_hex"})
    encoded = _string(data["utf8_hex"])
    if not 2 <= len(encoded) <= 2048:
        raise ConsumerIntakeError("invalid_evidence")
    try:
        raw = bytes.fromhex(encoded)
        if raw.hex() != encoded:
            raise ValueError()
        return _provider_id(raw.decode("utf-8", "strict"))
    except ValueError, UnicodeError:
        raise ConsumerIntakeError("invalid_evidence") from None


def _uuid(value: object, prefix: str = "") -> str:
    text = _string(value)
    if not text.startswith(prefix):
        raise ConsumerIntakeError()
    try:
        parsed = UUID(text[len(prefix) :])
    except ValueError:
        raise ConsumerIntakeError() from None
    if parsed.version != 4 or prefix + str(parsed) != text:
        raise ConsumerIntakeError()
    return text


def _digest(value: object) -> str:
    text = _string(value)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ConsumerIntakeError()
    return text


def _encode(value: object, maximum: int = MAX_CONVERSATION_DTO_BYTES) -> bytes:
    try:
        raw = portable_canonical_json_bytes(value)
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise ConsumerIntakeError() from None
    if len(raw) > maximum:
        raise ConsumerIntakeError("conversation_limit")
    return raw


def _decode(raw: bytes, maximum: int = MAX_CONVERSATION_DTO_BYTES) -> object:
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ConsumerIntakeError("invalid_evidence")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ConsumerIntakeError("invalid_evidence")
            result[key] = value
        return result

    try:
        value: object = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=unique)
        if _encode(value, maximum) != raw:
            raise ValueError()
        return value
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise ConsumerIntakeError("invalid_evidence") from None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumerMessageInput:
    provider_message_id: str
    role: str
    text: str
    occurred_at: str | None = None

    def __post_init__(self) -> None:
        _provider_id(self.provider_message_id)
        _string(self.role)
        _string(self.text)
        try:
            text = normalize_text(self.text)
            # Reuse the normalized contract's role and verified-UTC timestamp rules.
            ConsumerMessage("0" * 64, self.role, text, self.occurred_at)
        except ValueError, TypeError:
            raise ConsumerIntakeError() from None
        if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ConsumerIntakeError("conversation_limit")
        object.__setattr__(self, "text", text)

    def value(self) -> dict[str, object]:
        return {
            "contract": MESSAGE_INPUT_CONTRACT,
            "provider_message_id": _identity(self.provider_message_id),
            "role": self.role,
            "text": self.text,
            "occurred_at": self.occurred_at,
        }

    def to_bytes(self) -> bytes:
        return _encode(self.value())

    @property
    def input_sha256(self) -> str:
        return sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_value(cls, value: object) -> ConsumerMessageInput:
        try:
            data = _fields(
                value, {"contract", "provider_message_id", "role", "text", "occurred_at"}
            )
            if data["contract"] != MESSAGE_INPUT_CONTRACT:
                raise ValueError()
            result = cls(
                provider_message_id=_raw_identity(data["provider_message_id"]),
                role=_string(data["role"]),
                text=_string(data["text"]),
                occurred_at=None if data["occurred_at"] is None else _string(data["occurred_at"]),
            )
            if result.value() != value:
                raise ValueError()
            return result
        except ValueError, TypeError, KeyError, RecursionError:
            raise ConsumerIntakeError("invalid_evidence") from None

    @classmethod
    def from_bytes(cls, raw: bytes) -> ConsumerMessageInput:
        return cls.from_value(_decode(raw))


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumerConversationInput:
    provider_conversation_id: str
    title: str | None = None
    messages: tuple[ConsumerMessageInput, ...]
    omissions: Mapping[str, int]

    def __post_init__(self) -> None:
        _provider_id(self.provider_conversation_id)
        if self.title is not None:
            _string(self.title)
            try:
                title = normalize_text(self.title)
            except ValueError:
                raise ConsumerIntakeError() from None
            if not title.strip() or len(title) > 200:
                raise ConsumerIntakeError()
            object.__setattr__(self, "title", title)
        if (
            type(self.messages) is not tuple
            or len(self.messages) > MAX_MESSAGES
            or any(type(message) is not ConsumerMessageInput for message in self.messages)
        ):
            raise ConsumerIntakeError("conversation_limit")
        ids = [message.provider_message_id for message in self.messages]
        if len(set(ids)) != len(ids):
            raise ConsumerIntakeError()
        if not isinstance(self.omissions, Mapping) or set(self.omissions) != set(OMISSION_KEYS):
            raise ConsumerIntakeError()
        counts: dict[str, int] = {}
        for key in OMISSION_KEYS:
            count = self.omissions[key]
            if type(count) is not int or count < 0:
                raise ConsumerIntakeError()
            counts[key] = count
        object.__setattr__(self, "omissions", MappingProxyType(counts))
        if self.normalized_message_bytes > MAX_MESSAGE_BYTES or self.segment_count > MAX_SEGMENTS:
            raise ConsumerIntakeError("conversation_limit")
        self.to_bytes()

    @property
    def normalized_message_bytes(self) -> int:
        return sum(len(message.text.encode("utf-8")) for message in self.messages)

    @property
    def segment_count(self) -> int:
        scalars = sum(
            len(message.text) + (8 if message.role == "user" else 13) for message in self.messages
        )
        return (scalars + SEGMENT_SCALARS - 1) // SEGMENT_SCALARS

    @property
    def transcript_bytes(self) -> int:
        """Exact admitted transcript bytes, including speaker/separator and segment labels."""
        return (
            self.normalized_message_bytes
            + sum(8 if message.role == "user" else 13 for message in self.messages)
            + sum(
                len(f"Transcript segment {index}:\n".encode())
                for index in range(self.segment_count)
            )
        )

    def value(self) -> dict[str, object]:
        return {
            "contract": CONVERSATION_INPUT_CONTRACT,
            "provider_identity_sha256": sha256(self.identity_bytes()).hexdigest(),
            "content_sha256": sha256(self.content_bytes()).hexdigest(),
        }

    def identity_bytes(self) -> bytes:
        return _encode(
            {
                "contract": CONVERSATION_IDENTITY_CONTRACT,
                "provider_conversation_id": _identity(self.provider_conversation_id),
                "provider_message_ids": [
                    _identity(message.provider_message_id) for message in self.messages
                ],
            }
        )

    def content_bytes(self) -> bytes:
        return _encode(
            {
                "contract": CONVERSATION_CONTENT_CONTRACT,
                "title": self.title,
                "messages": [
                    {"role": message.role, "text": message.text, "occurred_at": message.occurred_at}
                    for message in self.messages
                ],
                "omissions": dict(self.omissions),
            }
        )

    def objects(self) -> dict[str, bytes]:
        payloads = (self.identity_bytes(), self.content_bytes())
        return {sha256(raw).hexdigest(): raw for raw in payloads}

    def to_bytes(self) -> bytes:
        return _encode(self.value())

    @property
    def input_sha256(self) -> str:
        return sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_value(cls, value: object, objects: Mapping[str, bytes]) -> ConsumerConversationInput:
        try:
            data = _fields(value, {"contract", "provider_identity_sha256", "content_sha256"})
            if data["contract"] != CONVERSATION_INPUT_CONTRACT:
                raise ValueError()

            def payload(reference: object) -> bytes:
                key = _digest(reference)
                raw = objects[key]
                if type(raw) is not bytes or sha256(raw).hexdigest() != key:
                    raise ValueError()
                return raw

            identity_raw = payload(data["provider_identity_sha256"])
            content_raw = payload(data["content_sha256"])
            identity = _fields(
                _decode(identity_raw),
                {
                    "contract",
                    "provider_conversation_id",
                    "provider_message_ids",
                },
            )
            content = _fields(_decode(content_raw), {"contract", "title", "messages", "omissions"})
            ids, values = identity["provider_message_ids"], content["messages"]
            if identity["contract"] != CONVERSATION_IDENTITY_CONTRACT or (
                content["contract"] != CONVERSATION_CONTENT_CONTRACT
                or type(ids) is not list
                or type(values) is not list
                or len(ids) != len(values)
                or len(values) > MAX_MESSAGES
            ):
                raise ValueError()
            messages: list[ConsumerMessageInput] = []
            for raw_id, message_value in zip(ids, values, strict=True):
                message = _fields(message_value, {"role", "text", "occurred_at"})
                messages.append(
                    ConsumerMessageInput(
                        provider_message_id=_raw_identity(raw_id),
                        role=_string(message["role"]),
                        text=_string(message["text"]),
                        occurred_at=None
                        if message["occurred_at"] is None
                        else _string(message["occurred_at"]),
                    )
                )
            result = cls(
                provider_conversation_id=_raw_identity(identity["provider_conversation_id"]),
                title=None if content["title"] is None else _string(content["title"]),
                messages=tuple(messages),
                omissions=cast(Mapping[str, int], content["omissions"]),
            )
            if (
                result.value() != value
                or result.identity_bytes() != identity_raw
                or (result.content_bytes() != content_raw)
            ):
                raise ValueError()
            return result
        except ValueError, TypeError, KeyError, RecursionError:
            raise ConsumerIntakeError("invalid_evidence") from None

    @classmethod
    def from_bytes(cls, raw: bytes, objects: Mapping[str, bytes]) -> ConsumerConversationInput:
        return cls.from_value(_decode(raw), objects)

    @classmethod
    def from_objects(cls, raw: bytes, objects: Mapping[str, bytes]) -> ConsumerConversationInput:
        return cls.from_bytes(raw, objects)


_PLAN_FIELDS = {
    "contract",
    "operation_id",
    "provider",
    "profile_id",
    "archive_sha256",
    "scope_id",
    "new_scope_label",
    "privacy_tier",
    "replace_current",
    "conversation_sha256",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumerImportPlan:
    operation_id: str
    provider: str
    profile_id: str
    archive_sha256: str
    conversations: tuple[ConsumerConversationInput, ...]
    scope_id: str | None = None
    new_scope_label: str | None = None
    privacy_tier: PrivacyTier = PrivacyTier.PERSONAL
    replace_current: bool = False

    def __post_init__(self) -> None:
        _uuid(self.operation_id)
        if type(self.provider) is not str or self.provider not in {"chatgpt", "claude"}:
            raise ConsumerIntakeError()
        if re.fullmatch(r"[a-z][a-z0-9-]{0,95}", _string(self.profile_id)) is None:
            raise ConsumerIntakeError()
        _digest(self.archive_sha256)
        if (self.scope_id is None) == (self.new_scope_label is None):
            raise ConsumerIntakeError()
        if self.scope_id is not None:
            _uuid(self.scope_id, "scope_")
        if self.new_scope_label is not None:
            object.__setattr__(self, "new_scope_label", validated_scope_label(self.new_scope_label))
        if not isinstance(self.privacy_tier, str) or type(self.replace_current) is not bool:
            raise ConsumerIntakeError()
        try:
            object.__setattr__(self, "privacy_tier", PrivacyTier(self.privacy_tier))
        except ValueError:
            raise ConsumerIntakeError() from None
        if (
            type(self.conversations) is not tuple
            or not 1 <= len(self.conversations) <= MAX_CONVERSATIONS
            or any(
                type(conversation) is not ConsumerConversationInput
                for conversation in self.conversations
            )
        ):
            raise ConsumerIntakeError("conversation_limit")
        ordered = tuple(
            sorted(
                self.conversations,
                key=lambda conversation: conversation.provider_conversation_id.encode(
                    "utf-8"
                ).hex(),
            )
        )
        if len({conversation.provider_conversation_id for conversation in ordered}) != len(ordered):
            raise ConsumerIntakeError()
        object.__setattr__(self, "conversations", ordered)
        if (
            sum(len(conversation.messages) for conversation in ordered) > MAX_PLAN_MESSAGES
            or sum(conversation.transcript_bytes for conversation in ordered) > MAX_TRANSCRIPT_BYTES
        ):
            raise ConsumerIntakeError("conversation_limit")
        self.to_bytes()

    def value(self) -> dict[str, object]:
        return {
            "contract": IMPORT_PLAN_CONTRACT,
            "operation_id": self.operation_id,
            "provider": self.provider,
            "profile_id": self.profile_id,
            "archive_sha256": self.archive_sha256,
            "scope_id": self.scope_id,
            "new_scope_label": self.new_scope_label,
            "privacy_tier": self.privacy_tier.value,
            "replace_current": self.replace_current,
            "conversation_sha256": [
                conversation.input_sha256 for conversation in self.conversations
            ],
        }

    def to_bytes(self) -> bytes:
        return _encode(self.value(), MAX_CANONICAL_BYTES)

    @property
    def input_sha256(self) -> str:
        return sha256(self.to_bytes()).hexdigest()

    def objects(self) -> dict[str, bytes]:
        """Return only referenced input objects, detached from engine custody authority."""
        result: dict[str, bytes] = {}
        for conversation in self.conversations:
            raw = conversation.to_bytes()
            result[sha256(raw).hexdigest()] = raw
            result.update(conversation.objects())
        return result

    @classmethod
    def from_value(cls, value: object, objects: Mapping[str, bytes]) -> ConsumerImportPlan:
        """Validate reachable inputs; enclosing custody validates its complete object union."""
        try:
            data = _fields(value, _PLAN_FIELDS)
            references = data["conversation_sha256"]
            if (
                data["contract"] != IMPORT_PLAN_CONTRACT
                or type(references) is not list
                or not (1 <= len(references) <= MAX_CONVERSATIONS)
            ):
                raise ValueError()
            conversations: list[ConsumerConversationInput] = []
            for reference in references:
                key = _digest(reference)
                raw = objects[key]
                if type(raw) is not bytes or sha256(raw).hexdigest() != key:
                    raise ValueError()
                conversations.append(ConsumerConversationInput.from_bytes(raw, objects))
            result = cls(
                operation_id=_string(data["operation_id"]),
                provider=_string(data["provider"]),
                profile_id=_string(data["profile_id"]),
                archive_sha256=_digest(data["archive_sha256"]),
                conversations=tuple(conversations),
                scope_id=None if data["scope_id"] is None else _string(data["scope_id"]),
                new_scope_label=None
                if data["new_scope_label"] is None
                else _string(data["new_scope_label"]),
                privacy_tier=PrivacyTier(_string(data["privacy_tier"])),
                replace_current=cast(bool, data["replace_current"]),
            )
            if result.value() != value:
                raise ValueError()
            return result
        except ValueError, TypeError, KeyError, RecursionError:
            raise ConsumerIntakeError("invalid_evidence") from None

    @classmethod
    def from_bytes(cls, raw: bytes, objects: Mapping[str, bytes]) -> ConsumerImportPlan:
        return cls.from_value(_decode(raw, MAX_CANONICAL_BYTES), objects)

    @classmethod
    def from_objects(cls, raw: bytes, objects: Mapping[str, bytes]) -> ConsumerImportPlan:
        return cls.from_bytes(raw, objects)
