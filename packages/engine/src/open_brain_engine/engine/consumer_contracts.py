"""Internal consumer-chat-v1 values, never an external provider parser.

Identity preimages wrap raw provider IDs as {"utf8_hex": <exact UTF-8 hex>}.
Portable canonical JSON NFC-folds strings; the ASCII wrapper preserves distinct
provider scalar sequences while retaining that existing canonical hash encoder.
DTO serialization uses compact exact UTF-8 JSON and never silently folds IDs.
No frozen Portable/journal encoding is changed.
Profile names here identify internal plans and do not activate external profiles.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import CaptureWhyOrigin, ContentOrigin, Intent
from open_brain_engine.engine.contracts import (
    CaptureAction,
    CaptureSubmission,
    CaptureSubmissionPath,
    TextPayload,
)

CONTRACT_VERSION = "consumer-chat-v1"
NORMALIZATION_VERSION = "chat-normalize-v1"
SEGMENTATION_VERSION = "chat-segment-v1"
MAX_MESSAGES = 10_000
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_SEGMENTS = 128
SEGMENT_SCALARS = 60_000
MAX_CONVERSATION_DTO_BYTES = 32 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
OMISSION_KEYS = (
    "nonselected_branch_messages",
    "system_messages",
    "developer_messages",
    "tool_messages",
    "attachment_parts",
    "unsupported_parts",
)


def _encode(value: object) -> bytes:
    data = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8", "strict")
    if len(data) > MAX_CONVERSATION_DTO_BYTES:
        raise ValueError("conversation DTO byte limit exceeded")
    return data


def _digest(value: object) -> str:
    return sha256(portable_canonical_json_bytes(value)).hexdigest()


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected string")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as error:
        raise ValueError("invalid Unicode") from error
    return value


def _identifier(value: object) -> str:
    text = _string(value)
    if not 1 <= len(text.encode("utf-8")) <= 1024 or any(
        ord(c) < 32 or 127 <= ord(c) <= 159 or unicodedata.category(c) == "Cf" for c in text
    ):
        raise ValueError("invalid identity")
    return text


def namespace_identifier(value: object) -> str:
    """Engine-issued opaque ASCII identity, shared with consumer source custody."""
    text = _string(value)
    if not 1 <= len(text) <= 128 or any(not 33 <= ord(c) <= 126 for c in text):
        raise ValueError("invalid namespace identity")
    return text


def _profile_identifier(value: object) -> str:
    text = _string(value)
    if re.fullmatch(r"[a-z][a-z0-9-]{0,95}", text) is None:
        raise ValueError("invalid profile identity")
    return text


def _hash(value: object) -> str:
    text = _string(value)
    if _HEX.fullmatch(text) is None:
        raise ValueError("invalid digest")
    return text


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected nonnegative integer")
    return value


def _object(value: object, keys: Sequence[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError("invalid fields")
    return cast(Mapping[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected array")
    return value


def _decode(data: bytes) -> object:
    if not isinstance(data, bytes) or len(data) > MAX_CONVERSATION_DTO_BYTES:
        raise ValueError("invalid DTO bytes")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(data.decode("utf-8", "strict"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid DTO JSON") from error


def normalize_text(value: str | bytes) -> str:
    """Normalize content once, escaping executable controls as visible ASCII."""
    try:
        text = value.decode("utf-8", "strict") if isinstance(value, bytes) else _string(value)
    except UnicodeError as error:
        raise ValueError("invalid Unicode") from error
    if "\x00" in text:
        raise ValueError("NUL is forbidden")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "".join(
        (f"\\u{ord(c):04X}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08X}")
        if (ord(c) < 32 and c not in "\n\t")
        or 127 <= ord(c) <= 159
        or unicodedata.category(c) == "Cf"
        else c
        for c in text
    )


def conversation_key(provider: str, import_namespace_id: str, provider_conversation_id: str) -> str:
    if provider not in ("chatgpt", "claude"):
        raise ValueError("invalid provider")
    return _digest(
        [
            CONTRACT_VERSION,
            provider,
            namespace_identifier(import_namespace_id),
            {"utf8_hex": _identifier(provider_conversation_id).encode("utf-8").hex()},
        ]
    )


def message_key(conversation: str, provider_message_id: str) -> str:
    return _digest(
        [
            _hash(conversation),
            {"utf8_hex": _identifier(provider_message_id).encode("utf-8").hex()},
        ]
    )


def segment_key(conversation: str, segment_index: int) -> str:
    return _digest([_hash(conversation), SEGMENTATION_VERSION, _count(segment_index)])


@dataclass(frozen=True, slots=True)
class ConsumerMessage:
    message_key: str
    role: str
    text: str
    occurred_at: str | None

    def __post_init__(self) -> None:
        _hash(self.message_key)
        _string(self.role)
        _string(self.text)
        if self.role not in ("user", "assistant"):
            raise ValueError("invalid role")
        if not self.text or normalize_text(self.text) != self.text:
            raise ValueError("message must be normalized nonempty text")
        if self.occurred_at is not None:
            timestamp = _string(self.occurred_at)
            if _UTC.fullmatch(timestamp) is None:
                raise ValueError("timestamp must be UTC RFC3339")
            try:
                datetime.fromisoformat(timestamp)
            except ValueError as error:
                raise ValueError("invalid timestamp") from error

    def value(self) -> dict[str, object]:
        return {
            "message_key": self.message_key,
            "role": self.role,
            "text": self.text,
            "occurred_at": self.occurred_at,
        }

    def to_bytes(self) -> bytes:
        return _encode(self.value())

    @classmethod
    def from_value(cls, value: object) -> ConsumerMessage:
        data = _object(value, ("message_key", "role", "text", "occurred_at"))
        occurred = data["occurred_at"]
        return cls(
            _hash(data["message_key"]),
            _string(data["role"]),
            _string(data["text"]),
            None if occurred is None else _string(occurred),
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ConsumerMessage:
        message = cls.from_value(_decode(data))
        if message.to_bytes() != data:
            raise ValueError("noncanonical consumer message")
        return message


@dataclass(frozen=True, slots=True)
class MessageSpan:
    message_key: str
    start: int
    end: int

    def __post_init__(self) -> None:
        _hash(self.message_key)
        if _count(self.start) >= _count(self.end):
            raise ValueError("invalid message span")

    def value(self) -> dict[str, object]:
        return {"message_key": self.message_key, "start": self.start, "end": self.end}

    @classmethod
    def from_value(cls, value: object) -> MessageSpan:
        data = _object(value, ("message_key", "start", "end"))
        return cls(_hash(data["message_key"]), _count(data["start"]), _count(data["end"]))


@dataclass(frozen=True, slots=True)
class ConsumerSegment:
    segment_index: int
    segment_key: str
    body_sha256: str
    message_spans: tuple[MessageSpan, ...]
    capture_request_sha256: str

    def __post_init__(self) -> None:
        _count(self.segment_index)
        for value in (self.segment_key, self.body_sha256, self.capture_request_sha256):
            _hash(value)
        object.__setattr__(self, "message_spans", tuple(self.message_spans))
        if any(not isinstance(span, MessageSpan) for span in self.message_spans):
            raise ValueError("invalid message spans")

    def value(self) -> dict[str, object]:
        return {
            "segment_index": self.segment_index,
            "segment_key": self.segment_key,
            "body_sha256": self.body_sha256,
            "message_spans": [span.value() for span in self.message_spans],
            "capture_request_sha256": self.capture_request_sha256,
        }

    @classmethod
    def from_value(cls, value: object) -> ConsumerSegment:
        data = _object(
            value,
            (
                "segment_index",
                "segment_key",
                "body_sha256",
                "message_spans",
                "capture_request_sha256",
            ),
        )
        return cls(
            _count(data["segment_index"]),
            _hash(data["segment_key"]),
            _hash(data["body_sha256"]),
            tuple(MessageSpan.from_value(span) for span in _array(data["message_spans"])),
            _hash(data["capture_request_sha256"]),
        )


def _pieces(
    messages: tuple[ConsumerMessage, ...],
) -> tuple[tuple[str, tuple[MessageSpan, ...]], ...]:
    parts: list[str] = []
    ranges: list[tuple[ConsumerMessage, int, int]] = []
    offset = 0
    for message in messages:
        label = "User:\n" if message.role == "user" else "Assistant:\n"
        start = offset + len(label)
        ranges.append((message, start, start + len(message.text)))
        part = label + message.text + "\n\n"
        parts.append(part)
        offset += len(part)
    if offset > MAX_SEGMENTS * SEGMENT_SCALARS:
        raise ValueError("segment limit exceeded")
    transcript = "".join(parts)
    pieces: list[tuple[str, tuple[MessageSpan, ...]]] = []
    cursor = 0
    for start in range(0, len(transcript), SEGMENT_SCALARS):
        end = min(start + SEGMENT_SCALARS, len(transcript))
        while cursor < len(ranges) and ranges[cursor][2] <= start:
            cursor += 1
        spans: list[MessageSpan] = []
        for message, text_start, text_end in ranges[cursor:]:
            if text_start >= end:
                break
            left, right = max(start, text_start), min(end, text_end)
            if left < right:
                spans.append(
                    MessageSpan(message.message_key, left - text_start, right - text_start)
                )
        body = f"Transcript segment {len(pieces)}:\n" + transcript[start:end]
        if TextPayload(body).text != body:
            raise ValueError("segment normalization changed transcript")
        pieces.append((body, tuple(spans)))
    return tuple(pieces)


_CONVERSATION_KEYS = (
    "contract_version",
    "normalization_version",
    "segmentation_version",
    "provider",
    "profile_id",
    "import_namespace_id",
    "conversation_key",
    "title",
    "messages",
    "omissions",
    "conversation_revision_sha256",
    "segments",
    "membership_sha256",
)


@dataclass(frozen=True, slots=True)
class ConsumerConversation:
    provider: str
    profile_id: str
    import_namespace_id: str
    conversation_key: str
    title: str
    messages: tuple[ConsumerMessage, ...]
    omissions: Mapping[str, int]
    conversation_revision_sha256: str
    segments: tuple[ConsumerSegment, ...]
    membership_sha256: str
    contract_version: str = CONTRACT_VERSION
    normalization_version: str = NORMALIZATION_VERSION
    segmentation_version: str = SEGMENTATION_VERSION

    def __post_init__(self) -> None:
        from types import MappingProxyType

        if (self.contract_version, self.normalization_version, self.segmentation_version) != (
            CONTRACT_VERSION,
            NORMALIZATION_VERSION,
            SEGMENTATION_VERSION,
        ):
            raise ValueError("unsupported consumer contract")
        if self.provider not in ("chatgpt", "claude"):
            raise ValueError("invalid provider")
        _profile_identifier(self.profile_id)
        namespace_identifier(self.import_namespace_id)
        _hash(self.conversation_key)
        _string(self.title)
        if (
            not self.title.strip()
            or len(self.title) > 200
            or normalize_text(self.title) != self.title
        ):
            raise ValueError("invalid title")
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "segments", tuple(self.segments))
        omissions = _object(self.omissions, OMISSION_KEYS)
        object.__setattr__(
            self,
            "omissions",
            MappingProxyType({key: _count(omissions[key]) for key in OMISSION_KEYS}),
        )
        if not 1 <= len(self.messages) <= MAX_MESSAGES or any(
            not isinstance(message, ConsumerMessage) for message in self.messages
        ):
            raise ValueError("invalid messages")
        if len({message.message_key for message in self.messages}) != len(self.messages):
            raise ValueError("duplicate message identity")
        if sum(len(message.text.encode("utf-8")) for message in self.messages) > MAX_MESSAGE_BYTES:
            raise ValueError("conversation byte limit exceeded")
        pieces = _pieces(self.messages)
        if not 1 <= len(self.segments) <= MAX_SEGMENTS or len(pieces) != len(self.segments):
            raise ValueError("invalid segment count")
        for index, (segment, (body, spans)) in enumerate(zip(self.segments, pieces, strict=True)):
            if not isinstance(segment, ConsumerSegment) or (
                segment.segment_index != index
                or segment.segment_key != segment_key(self.conversation_key, index)
                or segment.body_sha256 != sha256(body.encode("utf-8")).hexdigest()
                or segment.message_spans != spans
            ):
                raise ValueError("segment manifest mismatch")
        if self.conversation_revision_sha256 != _revision(self):
            raise ValueError("conversation revision mismatch")
        if self.membership_sha256 != _membership(self.segments):
            raise ValueError("membership mismatch")

    @property
    def segment_bodies(self) -> tuple[str, ...]:
        return tuple(body for body, _ in _pieces(self.messages))

    def value(self) -> dict[str, object]:
        return {
            **_revision_value(self),
            "segments": [segment.value() for segment in self.segments],
            "conversation_revision_sha256": self.conversation_revision_sha256,
            "membership_sha256": self.membership_sha256,
        }

    def to_bytes(self) -> bytes:
        return _encode(self.value())

    @classmethod
    def from_value(cls, value: object) -> ConsumerConversation:
        data = _object(value, _CONVERSATION_KEYS)
        return cls(
            provider=_string(data["provider"]),
            profile_id=_string(data["profile_id"]),
            import_namespace_id=_string(data["import_namespace_id"]),
            conversation_key=_hash(data["conversation_key"]),
            title=_string(data["title"]),
            messages=tuple(ConsumerMessage.from_value(v) for v in _array(data["messages"])),
            omissions=cast(Mapping[str, int], _object(data["omissions"], OMISSION_KEYS)),
            conversation_revision_sha256=_hash(data["conversation_revision_sha256"]),
            segments=tuple(ConsumerSegment.from_value(v) for v in _array(data["segments"])),
            membership_sha256=_hash(data["membership_sha256"]),
            contract_version=_string(data["contract_version"]),
            normalization_version=_string(data["normalization_version"]),
            segmentation_version=_string(data["segmentation_version"]),
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ConsumerConversation:
        conversation = cls.from_value(_decode(data))
        if conversation.to_bytes() != data:
            raise ValueError("noncanonical consumer conversation")
        return conversation


def _revision_value(conversation: ConsumerConversation) -> dict[str, object]:
    return {
        "contract_version": conversation.contract_version,
        "provider": conversation.provider,
        "profile_id": conversation.profile_id,
        "normalization_version": conversation.normalization_version,
        "segmentation_version": conversation.segmentation_version,
        "import_namespace_id": conversation.import_namespace_id,
        "conversation_key": conversation.conversation_key,
        "title": conversation.title,
        "messages": [message.value() for message in conversation.messages],
        "omissions": dict(conversation.omissions),
    }


def _revision(conversation: ConsumerConversation) -> str:
    return _digest(["consumer-conversation-revision-v1", _revision_value(conversation)])


def _membership(segments: tuple[ConsumerSegment, ...]) -> str:
    return _digest(["consumer-membership-v1", [segment.value() for segment in segments]])


SubmissionFactory = Callable[[int, str, str, str], CaptureSubmission]


def build_conversation(
    *,
    provider: str,
    profile_id: str,
    import_namespace_id: str,
    provider_conversation_id: str,
    title: str | bytes | None,
    messages: Sequence[Mapping[str, object]],
    omissions: Mapping[str, int],
    submission_factory: SubmissionFactory,
) -> ConsumerConversation:
    """Build an internal plan; the trusted caller supplies real capture envelopes.

    Input message keys are exactly provider_message_id, role, text, occurred_at.
    The factory receives (index, body, inert_source_reference, normalized_title).
    Its request digest is retained, never a substitute body-only digest.
    """
    key = conversation_key(provider, import_namespace_id, provider_conversation_id)
    _profile_identifier(profile_id)
    normalized_title = "Imported conversation" if title is None else normalize_text(title)
    if not normalized_title.strip() or len(normalized_title) > 200:
        raise ValueError("invalid title")
    omission_data = _object(omissions, OMISSION_KEYS)
    checked_omissions = {key: _count(omission_data[key]) for key in OMISSION_KEYS}
    if not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("invalid message count")
    normalized: list[ConsumerMessage] = []
    for value in messages:
        data = _object(value, ("provider_message_id", "role", "text", "occurred_at"))
        occurred = data["occurred_at"]
        raw_text = data["text"]
        if not isinstance(raw_text, str | bytes):
            raise ValueError("invalid message text")
        normalized.append(
            ConsumerMessage(
                message_key(key, _identifier(data["provider_message_id"])),
                _string(data["role"]),
                normalize_text(raw_text),
                None if occurred is None else _string(occurred),
            )
        )
    message_tuple = tuple(normalized)
    if len({message.message_key for message in message_tuple}) != len(message_tuple):
        raise ValueError("duplicate message identity")
    if sum(len(m.text.encode("utf-8")) for m in message_tuple) > MAX_MESSAGE_BYTES:
        raise ValueError("conversation byte limit exceeded")
    pieces = _pieces(message_tuple)
    segments: list[ConsumerSegment] = []
    for index, (body, spans) in enumerate(pieces):
        reference = f"urn:open-brain:consumer-chat:{provider}:{key}:{index}"
        submission = submission_factory(index, body, reference, normalized_title)
        if not isinstance(submission, CaptureSubmission) or (
            submission.payload != TextPayload(body)
            or submission.source_reference != reference
            or submission.title != normalized_title
            or submission.action is not CaptureAction.QUICK
            or submission.submission_path is not CaptureSubmissionPath.PUBLIC_JOB
            or submission.source_origin is not ContentOrigin.THIRD_PARTY
            or submission.space_id is not None
            or submission.intent not in (None, Intent.REFERENCE)
            or submission.capture_why is not None
            or submission.capture_why_origin is not CaptureWhyOrigin.AUTOMATION_ABSENT
            or submission.role_claim["capabilities"] != ("capture.accept",)
            or submission.privacy.authority.cloud
            or submission.privacy.authority.external_egress
            or submission.provenance.content_origin is not ContentOrigin.THIRD_PARTY
            or submission.provenance.owner_context is not CaptureWhyOrigin.AUTOMATION_ABSENT
            or submission.provenance.source_ref != reference
        ):
            raise ValueError("capture factory changed segment")
        segments.append(
            ConsumerSegment(
                index,
                segment_key(key, index),
                sha256(body.encode("utf-8")).hexdigest(),
                spans,
                submission.request_sha256(),
            )
        )
    # Compute the preimage before construction so the public constructor always validates.
    revision_value: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "provider": provider,
        "profile_id": profile_id,
        "normalization_version": NORMALIZATION_VERSION,
        "segmentation_version": SEGMENTATION_VERSION,
        "import_namespace_id": import_namespace_id,
        "conversation_key": key,
        "title": normalized_title,
        "messages": [message.value() for message in message_tuple],
        "omissions": checked_omissions,
    }
    return ConsumerConversation(
        provider,
        profile_id,
        import_namespace_id,
        key,
        normalized_title,
        message_tuple,
        checked_omissions,
        _digest(["consumer-conversation-revision-v1", revision_value]),
        tuple(segments),
        _membership(tuple(segments)),
    )
