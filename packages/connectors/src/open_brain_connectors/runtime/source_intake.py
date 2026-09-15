"""Validated source-record intake shared by provider adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256

from open_brain_engine.engine import (
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    Provenance,
    ReferencePayload,
)

from open_brain_connectors.runtime.connectors import ConnectorContractError

__all__ = [
    "SourceRecordIntake",
    "SourceRecordKey",
]

_CONNECTOR_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_MAX_TEXT = 65_536
_MAX_TITLE = 200


@dataclass(frozen=True, slots=True)
class SourceRecordKey:
    """Stable external identity used to derive a durable delivery key."""

    connector_name: str
    connection_id: str
    resource_id: str
    external_id: str
    revision_id: str

    def __post_init__(self) -> None:
        if _CONNECTOR_NAME.fullmatch(self.connector_name) is None:
            raise ConnectorContractError("invalid source record key")
        for value in (
            self.connection_id,
            self.resource_id,
            self.external_id,
            self.revision_id,
        ):
            if not isinstance(value, str) or _SOURCE_ID.fullmatch(value) is None:
                raise ConnectorContractError("invalid source record key")

    def delivery_id(self) -> str:
        digest = sha256(
            "\x1f".join(
                (
                    self.connector_name,
                    self.connection_id,
                    self.resource_id,
                    self.external_id,
                )
            ).encode("utf-8")
        ).hexdigest()
        return f"connector.{self.connector_name}.{digest}"

    def revision_identity(self) -> str:
        return sha256(
            "\x1f".join(
                (
                    self.connector_name,
                    self.connection_id,
                    self.resource_id,
                    self.external_id,
                    self.revision_id,
                )
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceRecordIntake:
    """A third-party record after provider parsing and before engine capture."""

    key: SourceRecordKey
    url: str
    text: str
    privacy: PrivacyDecision
    title: str | None = None

    def __post_init__(self) -> None:
        if type(self.key) is not SourceRecordKey or not isinstance(self.privacy, PrivacyDecision):
            raise ConnectorContractError("invalid source record")
        try:
            payload = ReferencePayload(url=self.url, supplied_text=self.text)
        except ValueError as error:
            raise ConnectorContractError("invalid source record") from error
        object.__setattr__(self, "url", payload.url)
        object.__setattr__(self, "text", payload.supplied_text)
        if not isinstance(self.text, str) or len(self.text) > _MAX_TEXT:
            raise ConnectorContractError("invalid source record")
        if self.title is not None:
            if type(self.title) is not str:
                raise ConnectorContractError("invalid source record")
            title = self.title.strip()
            if not title or "\x00" in title or len(title) > _MAX_TITLE:
                raise ConnectorContractError("invalid source record")
            object.__setattr__(self, "title", title)

    @property
    def source_reference(self) -> str:
        return self.url

    def payload(self) -> ReferencePayload:
        return ReferencePayload(url=self.url, supplied_text=self.text)

    def provenance(self) -> Provenance:
        return Provenance.create(
            source_ref=self.source_reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        )

    def capture_kwargs(self) -> dict[str, object]:
        return {
            "payload": self.payload(),
            "delivery_id": self.key.delivery_id(),
            "source_origin": ContentOrigin.THIRD_PARTY,
            "source_reference": self.source_reference,
            "provenance": self.provenance(),
            "privacy": self.privacy,
            "intent": "reference",
            "title": self.title,
        }
