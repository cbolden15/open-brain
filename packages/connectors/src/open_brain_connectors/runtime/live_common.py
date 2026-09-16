"""Bounded contracts for optional live source capture."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol, cast

from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.source_intake import SourceRecordIntake


class LiveSourceError(RuntimeError):
    """Safe metadata-only error; provider payloads must never enter exceptions."""

    def __init__(self, code: str, *, retry_after_seconds: int | None = None) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code):
            code = "source_failed"
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = (
            min(86_400, max(1, retry_after_seconds)) if type(retry_after_seconds) is int else None
        )


def bounded_json(value: object, maximum: int = 131_072) -> bytes:
    """Reject non-JSON values, nonfinite numbers and oversized state."""
    try:
        raw = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    except ValueError, TypeError, RecursionError:
        raise LiveSourceError("source_invalid_state") from None
    if len(raw) > maximum:
        raise LiveSourceError("source_state_too_large")
    return raw


def safe_text(value: object, *, maximum: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise LiveSourceError("source_invalid_value")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class LiveBatch:
    intakes: tuple[SourceRecordIntake, ...]
    checkpoint: dict[str, object] = field(repr=False)
    has_more: bool = False
    notices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.intakes) is not tuple
            or len(self.intakes) > 25
            or any(type(item) is not SourceRecordIntake for item in self.intakes)
            or type(self.checkpoint) is not dict
            or type(self.has_more) is not bool
            or type(self.notices) is not tuple
            or len(self.notices) > 25
            or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", n) for n in self.notices)
        ):
            raise LiveSourceError("source_invalid_batch")
        bounded_json(self.checkpoint)
        keys = [item.key.delivery_id() for item in self.intakes]
        if len(keys) != len(set(keys)):
            raise LiveSourceError("source_duplicate_batch_identity")


@dataclass(frozen=True, slots=True)
class LiveResource:
    resource_id: str
    name: str
    resource_type: str

    def __post_init__(self) -> None:
        safe_text(self.resource_id)
        safe_text(self.name)
        safe_text(self.resource_type, maximum=64)

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "name": self.name,
            "resource_type": self.resource_type,
        }


@dataclass(frozen=True, slots=True)
class LiveResourcePage:
    resources: tuple[LiveResource, ...]
    next_cursor: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.resources) is not tuple
            or len(self.resources) > 100
            or any(type(item) is not LiveResource for item in self.resources)
        ):
            raise LiveSourceError("source_invalid_resources")
        if self.next_cursor is not None:
            safe_text(self.next_cursor, maximum=8192)


@dataclass(frozen=True, slots=True)
class LiveAccount:
    provider: str
    connection_id: str
    display_name: str
    scopes: tuple[str, ...]
    expires_at_epoch: int | None = None

    def __post_init__(self) -> None:
        if (
            self.provider not in {"gmail", "google_drive", "slack"}
            or not re.fullmatch(r"account:[a-zA-Z0-9._:-]{1,121}", self.connection_id)
            or type(self.scopes) is not tuple
            or not 1 <= len(self.scopes) <= 16
            or len(set(self.scopes)) != len(self.scopes)
            or (
                self.expires_at_epoch is not None
                and (type(self.expires_at_epoch) is not int or self.expires_at_epoch <= 0)
            )
        ):
            raise LiveSourceError("source_invalid_account")
        safe_text(self.display_name)
        for scope in self.scopes:
            safe_text(scope)

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "connection_id": self.connection_id,
            "display_name": self.display_name,
            "scopes": list(self.scopes),
            "expires_at_epoch": self.expires_at_epoch,
        }

    @classmethod
    def from_dict(cls, value: object) -> LiveAccount:
        if (
            not isinstance(value, dict)
            or set(value)
            != {"provider", "connection_id", "display_name", "scopes", "expires_at_epoch"}
            or not isinstance(value["scopes"], list)
        ):
            raise LiveSourceError("source_invalid_account")
        return cls(
            safe_text(value["provider"]),
            safe_text(value["connection_id"]),
            safe_text(value["display_name"]),
            tuple(safe_text(scope) for scope in value["scopes"]),
            cast(int | None, value["expires_at_epoch"]),
        )


class LiveCredentialStore(Protocol):
    def get(self, reference: str) -> dict[str, object] | None: ...

    def set(self, reference: str, value: dict[str, object]) -> None: ...

    def delete(self, reference: str) -> None: ...

    def status(self, reference: str) -> str: ...


def local_source_privacy() -> PrivacyDecision:
    """Selected personal sources never authorize cloud retrieval or onward egress."""
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": False},
            "confirmation_ref": None,
            "policy_version": "priority-capture-v1",
            "reason": "personal_local_only",
            "tier": "personal",
        }
    )
