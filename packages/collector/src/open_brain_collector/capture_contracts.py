"""Versioned source observations and evidence-bound development contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import cast

from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json

KINDS = frozenset({"requested", "proposed", "committed", "approved", "decision", "correction"})


def _text(value: object, maximum: int = 4096, *, empty: bool = False) -> None:
    if type(value) is not str or len(value) > maximum or (not empty and not value):
        raise LiveSourceError("processing_invalid_contract")


def digest(value: object) -> str:
    return sha256(bounded_json(value, 2_097_152)).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceObservation:
    workspace_id: str
    conversation_id: str
    message_ts: str
    revision_id: str
    observed_at: str
    source_link: str
    text: str
    thread_context: str
    capture_id: str
    privacy: PrivacyDecision
    ai_excluded: bool = False

    def __post_init__(self) -> None:
        for value in (self.workspace_id, self.conversation_id, self.message_ts,
                      self.revision_id, self.observed_at, self.source_link, self.capture_id):
            _text(value, 512)
        _text(self.text, 131_072)
        _text(self.thread_context, 131_072, empty=True)
        if not isinstance(self.privacy, PrivacyDecision) or type(self.ai_excluded) is not bool:
            raise LiveSourceError("processing_invalid_contract")

    @property
    def observation_id(self) -> str:
        return "observation:" + digest([self.workspace_id, self.conversation_id,
                                        self.message_ts, self.revision_id])

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["privacy"] = self.privacy.to_dict()
        return {"schema_version": 1, **value}

    @classmethod
    def from_dict(cls, value: object) -> SourceObservation:
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise LiveSourceError("processing_invalid_contract")
        data = dict(value)
        data.pop("schema_version")
        try:
            data["privacy"] = PrivacyDecision.from_dict(data["privacy"])
            return cls(**data)
        except (TypeError, KeyError, ValueError):
            raise LiveSourceError("processing_invalid_contract") from None


@dataclass(frozen=True, slots=True)
class Development:
    claim: str
    kind: str
    actor: str
    subject: str
    environment: str
    event_time: str
    supporting_capture_ids: tuple[str, ...]
    supporting_revision_ids: tuple[str, ...]
    existing_facet_ids: tuple[str, ...] = ()
    uncertainty: str = ""
    supports_development_ids: tuple[str, ...] = ()
    supersedes_development_ids: tuple[str, ...] = ()
    quote: str = ""

    def __post_init__(self) -> None:
        for value in (self.claim, self.kind, self.actor, self.subject, self.event_time, self.quote):
            _text(value)
        for value in (self.environment, self.uncertainty):
            _text(value, empty=True)
        if self.kind not in KINDS:
            raise LiveSourceError("processing_invalid_contract")
        for values in (self.supporting_capture_ids, self.supporting_revision_ids,
                       self.existing_facet_ids, self.supports_development_ids,
                       self.supersedes_development_ids):
            if type(values) is not tuple or len(values) > 32:
                raise LiveSourceError("processing_invalid_contract")
            for value in values:
                _text(value, 512)
        if not self.supporting_capture_ids or not self.supporting_revision_ids:
            raise LiveSourceError("processing_invalid_contract")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, value: object) -> Development:
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise LiveSourceError("processing_invalid_contract")
        data = dict(value)
        data.pop("schema_version")
        for key in ("supporting_capture_ids", "supporting_revision_ids", "existing_facet_ids",
                    "supports_development_ids", "supersedes_development_ids"):
            if not isinstance(data.get(key), (list, tuple)):
                raise LiveSourceError("processing_invalid_contract")
            data[key] = tuple(cast(list[str], data[key]))
        try:
            return cls(**data)
        except (TypeError, KeyError, ValueError):
            raise LiveSourceError("processing_invalid_contract") from None
