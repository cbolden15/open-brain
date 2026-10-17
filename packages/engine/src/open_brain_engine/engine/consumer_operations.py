"""Closed accepted-operation proof over parser inputs and prospective custody.

These bytes prove consistency. Only live owner admission can accept an operation;
retained profile records and hashes never activate a provider or grant authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from hashlib import sha256
from types import MappingProxyType
from typing import cast

from .capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleRequest,
    _prefixed_uuid4,
    _uuid4,
    canonical_bytes,
    decode_canonical,
    digest,
    validate_digest,
    validate_session_id,
)
from .consumer_contracts import ConsumerMessage, conversation_key, message_key
from .consumer_intents import PreparedConsumerConversation
from .consumer_journal import CONSUMER_INTAKE_CONTRACT, ConsumerItem
from .consumer_plans import ConsumerImportPlan, validated_scope_label
from .consumer_profiles import ConsumerIntakeError, ConsumerProfileRecord
from .contracts import LocalEngineContext, owner_privacy_for_tier

_FIELDS = frozenset(
    {
        "contract",
        "operation_id",
        "brain_id",
        "issuer_epoch",
        "principal_id",
        "provider",
        "profile_sha256",
        "input_sha256",
        "scope",
        "archive_sha256",
        "privacy_tier",
        "replace_current",
        "session_id",
        "eligibility_generation",
        "control_epoch",
        "items",
    }
)
_ITEM_FIELDS = frozenset(
    {
        "item_token",
        "group_key",
        "revision_sha256",
        "planned_outcome",
        "reference_receipt_sha256",
        "intent_sha256",
        "input_sha256",
        "expected_revision_sha256",
    }
)


def segment_delivery_id(
    brain_id: str,
    operation_id: str,
    group: str,
    revision: str,
    index: int,
    request_sha: str,
) -> str:
    return "consumer." + digest(
        [
            "consumer-segment-delivery-v1",
            brain_id,
            operation_id,
            group,
            revision,
            index,
            request_sha,
        ]
    )


def _object(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ConsumerIntakeError("invalid_evidence")
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class PreparedConsumerOperation:
    raw: bytes
    payloads: Mapping[str, bytes]
    _plan_cache: ConsumerImportPlan | None = dataclass_field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "payloads", MappingProxyType(dict(self.payloads)))
        try:
            self._validate()
        except ValueError, TypeError, KeyError, AttributeError, RecursionError:
            raise ConsumerIntakeError("invalid_evidence") from None

    def value(self) -> dict[str, object]:
        return _object(decode_canonical(self.raw), _FIELDS)

    @property
    def intent_sha256(self) -> str:
        return sha256(self.raw).hexdigest()

    def payload(self, key: object) -> bytes:
        selected = validate_digest(key)
        raw = self.payloads[selected]
        if type(raw) is not bytes or sha256(raw).hexdigest() != selected:
            raise ConsumerIntakeError("invalid_evidence")
        return raw

    @property
    def plan(self) -> ConsumerImportPlan:
        if self._plan_cache is None:
            object.__setattr__(
                self,
                "_plan_cache",
                ConsumerImportPlan.from_bytes(
                    self.payload(self.value()["input_sha256"]),
                    self.payloads,
                ),
            )
        assert self._plan_cache is not None
        return self._plan_cache

    @property
    def items(self) -> tuple[dict[str, object], ...]:
        return tuple(cast(list[dict[str, object]], self.value()["items"]))

    @property
    def scope(self) -> dict[str, object]:
        return cast(dict[str, object], self.value()["scope"])

    def conversation(self, item: Mapping[str, object]) -> PreparedConsumerConversation | None:
        key = item["intent_sha256"]
        if key is None:
            return None
        return PreparedConsumerConversation.from_objects(self.payload(key), self.payloads)

    @property
    def request(self) -> LifecycleRequest:
        root = self.value()
        groups, items = [], []
        for item in self.items:
            items.append(
                ConsumerItem(
                    cast(str, item["group_key"]),
                    cast(str | None, item["revision_sha256"]),
                    cast(str, item["planned_outcome"]),
                    cast(str | None, item["reference_receipt_sha256"]),
                ).value()
            )
            if item["planned_outcome"] in {"new", "replace_current"}:
                prepared = self.conversation(item)
                assert prepared is not None
                groups.append(
                    GroupIntent(
                        prepared.normalized.conversation_key,
                        prepared.normalized.conversation_revision_sha256,
                        cast(str | None, item["expected_revision_sha256"]),
                        tuple(segment.binding for segment in prepared.segments),
                    ).value()
                )
        return LifecycleRequest(
            cast(str, root["operation_id"]),
            "consumer_import",
            cast(str, root["brain_id"]),
            cast(int, root["issuer_epoch"]),
            {
                "groups": groups,
                "consumer": {
                    "contract": CONSUMER_INTAKE_CONTRACT,
                    "intent_sha256": self.intent_sha256,
                    "items": items,
                },
            },
        )

    def inventory(self) -> dict[str, tuple[str, bytes]]:
        """Exactly reachable typed objects, including this accepted root."""
        root, plan = self.value(), self.plan
        result = {
            self.intent_sha256: ("operation", self.raw),
            cast(str, root["input_sha256"]): ("operation", plan.to_bytes()),
            cast(str, root["profile_sha256"]): ("profile", self.payload(root["profile_sha256"])),
        }
        for conversation_input in plan.conversations:
            for kind, raw in (
                ("conversation", conversation_input.to_bytes()),
                ("identity", conversation_input.identity_bytes()),
                ("normalized", conversation_input.content_bytes()),
            ):
                result[sha256(raw).hexdigest()] = (kind, raw)
        for item in self.items:
            prepared = self.conversation(item)
            if prepared is None:
                continue
            result[prepared.intent_sha256] = ("conversation", prepared.to_bytes())
            metadata = prepared.value()
            for field, kind in (
                ("profile_sha256", "profile"),
                ("normalized_sha256", "normalized"),
                ("provider_identity_sha256", "identity"),
            ):
                key = cast(str, metadata[field])
                result[key] = (kind, self.payload(key))
            for key in cast(list[str], metadata["segment_sha256"]):
                result[key] = ("segment", self.payload(key))
        return result

    def validate_profile(self, profile: LocalEngineContext) -> None:
        if self.value()["principal_id"] != profile.owner_actor_id:
            raise ConsumerIntakeError("invalid_evidence")
        for item in self.items:
            prepared = self.conversation(item)
            if prepared is not None:
                prepared.validate_profile(profile)

    def _validate(self) -> None:
        root = self.value()
        if root["contract"] != "ob.consumer-operation-intent.v1":
            raise ValueError()
        _uuid4(root["operation_id"])
        _prefixed_uuid4(root["principal_id"], "actor_")
        validate_session_id(root["session_id"])
        for field in ("eligibility_generation", "control_epoch"):
            value = root[field]
            if type(value) is not int or not 0 <= value <= 9007199254740991:
                raise ValueError()
        plan = self.plan
        if type(root["replace_current"]) is not bool:
            raise ValueError()
        if (
            root["operation_id"],
            root["provider"],
            root["archive_sha256"],
            root["privacy_tier"],
            root["replace_current"],
        ) != (
            plan.operation_id,
            plan.provider,
            plan.archive_sha256,
            plan.privacy_tier.value,
            plan.replace_current,
        ):
            raise ValueError()
        scope = _object(root["scope"], frozenset({"scope_id", "namespace_id", "label", "proposed"}))
        _prefixed_uuid4(scope["scope_id"], "scope_")
        _uuid4(scope["namespace_id"])
        if type(scope["proposed"]) is not bool or scope["proposed"] != (plan.scope_id is None):
            raise ValueError()
        if (plan.scope_id is not None and scope["scope_id"] != plan.scope_id) or (
            plan.new_scope_label is not None and scope["label"] != plan.new_scope_label
        ):
            raise ValueError()
        # Reuse the same label grammar even for an existing-scope projection.
        from .consumer_plans import ConsumerConversationInput

        if validated_scope_label(scope["label"]) != scope["label"]:
            raise ValueError()
        profile = ConsumerProfileRecord.from_bytes(self.payload(root["profile_sha256"]))
        if (profile.provider, profile.profile_id) != (plan.provider, plan.profile_id):
            raise ValueError()
        if type(root["items"]) is not list or len(self.items) != len(plan.conversations):
            raise ValueError()
        keys, tokens, selected = [], [], set()
        inputs: dict[str, ConsumerConversationInput] = {
            item.input_sha256: item for item in plan.conversations
        }
        captures: set[str] = set()
        receipts: set[str] = set()
        sources: set[str] = set()
        deliveries: set[str] = set()
        for item in self.items:
            _object(item, _ITEM_FIELDS)
            token = _prefixed_uuid4(item["item_token"], "citem_")
            tokens.append(token)
            key = validate_digest(item["group_key"])
            keys.append(key)
            raw_input = inputs[cast(str, item["input_sha256"])]
            selected.add(raw_input.input_sha256)
            if key != conversation_key(
                plan.provider, cast(str, scope["namespace_id"]), raw_input.provider_conversation_id
            ):
                raise ValueError()
            if item["expected_revision_sha256"] is not None:
                validate_digest(item["expected_revision_sha256"])
            outcome = ConsumerItem(
                key,
                cast(str | None, item["revision_sha256"]),
                cast(str, item["planned_outcome"]),
                cast(str | None, item["reference_receipt_sha256"]),
            )
            prepared = self.conversation(item)
            if not raw_input.messages:
                if prepared is not None or outcome.planned_outcome != "no_text":
                    raise ValueError()
                continue
            if prepared is None or prepared.profile != profile:
                raise ValueError()
            normalized = prepared.normalized
            expected_messages = tuple(
                ConsumerMessage(
                    message_key(key, message.provider_message_id),
                    message.role,
                    message.text,
                    message.occurred_at,
                )
                for message in raw_input.messages
            )
            if (
                normalized.conversation_key != key
                or normalized.import_namespace_id != scope["namespace_id"]
                or normalized.messages != expected_messages
                or normalized.title != (raw_input.title or "Imported conversation")
                or normalized.omissions != raw_input.omissions
                or prepared.provider_conversation_utf8_hex
                != (raw_input.provider_conversation_id.encode().hex())
                or prepared.provider_message_utf8_hex
                != tuple(
                    message.provider_message_id.encode().hex() for message in raw_input.messages
                )
                or outcome.revision_sha256 != normalized.conversation_revision_sha256
            ):
                raise ValueError()
            if outcome.planned_outcome == "new" and item["expected_revision_sha256"] is not None:
                raise ValueError()
            if outcome.planned_outcome == "replace_current" and (
                item["expected_revision_sha256"] is None or not plan.replace_current
            ):
                raise ValueError()
            for segment in prepared.segments:
                submission = segment.envelope.submission
                if submission.privacy != owner_privacy_for_tier(plan.privacy_tier) or (
                    segment.source.control_epoch != root["control_epoch"]
                    or submission.delivery_id
                    != segment_delivery_id(
                        cast(str, root["brain_id"]),
                        plan.operation_id,
                        key,
                        normalized.conversation_revision_sha256,
                        segment.segment_index,
                        submission.request_sha256(),
                    )
                ):
                    raise ValueError()
                for seen, identity in (
                    (captures, segment.identities.capture_id),
                    (receipts, segment.identities.accepted_receipt_id),
                    (sources, segment.source.source_id),
                    (deliveries, submission.delivery_id),
                ):
                    if identity in seen:
                        raise ValueError()
                    seen.add(identity)
        if keys != sorted(set(keys)) or len(set(tokens)) != len(tokens) or selected != set(inputs):
            raise ValueError()
        _ = self.request  # Prove the shared request schema and Brain/issuer bindings too.
        inventory = self.inventory()
        if set(self.payloads) != set(inventory) - {self.intent_sha256}:
            raise ValueError()
        for key, (_, raw) in inventory.items():
            if sha256(raw).hexdigest() != key:
                raise ValueError()

    @classmethod
    def from_values(
        cls,
        value: Mapping[str, object],
        payloads: Mapping[str, bytes],
    ) -> PreparedConsumerOperation:
        return cls(canonical_bytes(value), payloads)

    @classmethod
    def from_loader(cls, raw: bytes, load: Callable[[str], bytes]) -> PreparedConsumerOperation:
        """Load this operation's graph from shared custody without accepting orphans."""
        retained: dict[str, bytes] = {}

        def fetch(key: object) -> bytes:
            selected = validate_digest(key)
            value = load(selected)
            if type(value) is not bytes or sha256(value).hexdigest() != selected:
                raise ConsumerIntakeError("invalid_evidence")
            retained[selected] = value
            return value

        try:
            root = _object(decode_canonical(raw), _FIELDS)
            fetch(root["profile_sha256"])
            plan = cast(dict[str, object], decode_canonical(fetch(root["input_sha256"])))
            for reference in cast(list[str], plan["conversation_sha256"]):
                conversation = cast(dict[str, object], decode_canonical(fetch(reference)))
                fetch(conversation["provider_identity_sha256"])
                fetch(conversation["content_sha256"])
            for item in cast(list[dict[str, object]], root["items"]):
                if item["intent_sha256"] is None:
                    continue
                prepared = cast(dict[str, object], decode_canonical(fetch(item["intent_sha256"])))
                for field in ("profile_sha256", "normalized_sha256", "provider_identity_sha256"):
                    fetch(prepared[field])
                for reference in cast(list[str], prepared["segment_sha256"]):
                    fetch(reference)
            return cls(raw, retained)
        except ValueError, TypeError, KeyError, AttributeError, RecursionError:
            raise ConsumerIntakeError("invalid_evidence") from None
