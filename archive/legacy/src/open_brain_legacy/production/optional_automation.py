"""Owner-bound production inputs for local planning and messaging automation."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import Intent
from open_brain_engine.core.ports import Clock
from open_brain_engine.review.models import ReviewState

from open_brain_legacy.integrations.life_os import ReviewGatedActionCandidate
from open_brain_legacy.review.store import SqliteReviewStore

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_CANDIDATES = 1_000
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class OptionalAutomationConfigError(ValueError):
    """A private planning or messaging input contract is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class LifeOSAutomationConfig:
    """Bound the canonical approved-action set used by one planning run."""

    candidate_limit: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidate_limit, int)
            or isinstance(self.candidate_limit, bool)
            or not 1 <= self.candidate_limit <= _MAX_CANDIDATES
        ):
            raise OptionalAutomationConfigError("invalid private LifeOS config")

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> LifeOSAutomationConfig:
        value = _decode_mapping(payload, label="LifeOS")
        if set(value) != {"schema_version", "candidate_limit"} or value.get(
            "schema_version"
        ) != 1:
            raise OptionalAutomationConfigError("invalid private LifeOS config")
        result = cls(candidate_limit=value["candidate_limit"])  # type: ignore[arg-type]
        if result.canonical_bytes() != payload:
            raise OptionalAutomationConfigError("invalid private LifeOS config")
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {"schema_version": 1, "candidate_limit": self.candidate_limit}
        )


@dataclass(frozen=True, slots=True)
class MessagesAutomationConfig:
    """Select one opaque source-owned inbox stream without exposing message content."""

    resource_ref: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.resource_ref, str) or _OPAQUE_ID.fullmatch(
            self.resource_ref
        ) is None:
            raise OptionalAutomationConfigError("invalid private messages config")

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> MessagesAutomationConfig:
        value = _decode_mapping(payload, label="messages")
        if set(value) != {"schema_version", "resource_ref"} or value.get(
            "schema_version"
        ) != 1:
            raise OptionalAutomationConfigError("invalid private messages config")
        result = cls(resource_ref=value["resource_ref"])  # type: ignore[arg-type]
        if result.canonical_bytes() != payload:
            raise OptionalAutomationConfigError("invalid private messages config")
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {"schema_version": 1, "resource_ref": self.resource_ref}
        )


def load_private_life_os_config(path: Path) -> LifeOSAutomationConfig:
    return LifeOSAutomationConfig.from_canonical_bytes(_read_owner_file(path))


def load_private_messages_config(path: Path) -> MessagesAutomationConfig:
    return MessagesAutomationConfig.from_canonical_bytes(_read_owner_file(path))


def approved_life_os_candidates(
    *,
    root: Path,
    clock: Clock,
    config: LifeOSAutomationConfig,
) -> tuple[ReviewGatedActionCandidate, ...]:
    """Return stable opaque bindings for owner-applied action reviews only."""

    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or not isinstance(config, LifeOSAutomationConfig)
        or not callable(getattr(clock, "now", None))
    ):
        raise OptionalAutomationConfigError("invalid private LifeOS config")
    candidates: list[ReviewGatedActionCandidate] = []
    with SqliteReviewStore(
        root=root,
        database_name="review/review.sqlite3",
        clock=clock,
    ) as reviews:
        for aggregate in reviews.active_reviews():
            approved = aggregate.approved_record
            if (
                aggregate.proposal.state is not ReviewState.APPLIED
                or approved is None
                or approved.intent is not Intent.ACTION_CANDIDATE
            ):
                continue
            candidates.append(
                ReviewGatedActionCandidate(
                    candidate_id=approved.record_id,
                    review_id=str(approved.review_id),
                )
            )
            if len(candidates) == config.candidate_limit:
                break
    return tuple(candidates)


def _read_owner_file(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise OptionalAutomationConfigError("invalid private automation config")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= _MAX_CONFIG_BYTES
        ):
            raise OptionalAutomationConfigError("invalid private automation config")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_CONFIG_BYTES + 1)
    except OptionalAutomationConfigError:
        raise
    except OSError as error:
        raise OptionalAutomationConfigError("invalid private automation config") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > _MAX_CONFIG_BYTES:
        raise OptionalAutomationConfigError("invalid private automation config")
    return payload


def _decode_mapping(payload: bytes, *, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if isinstance(error, OptionalAutomationConfigError):
            raise
        raise OptionalAutomationConfigError(
            f"invalid private {label} config"
        ) from error
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise OptionalAutomationConfigError(f"invalid private {label} config")
    return cast(Mapping[str, object], value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise OptionalAutomationConfigError("invalid private automation config")
        value[key] = item
    return value


__all__ = [
    "LifeOSAutomationConfig",
    "MessagesAutomationConfig",
    "OptionalAutomationConfigError",
    "approved_life_os_candidates",
    "load_private_life_os_config",
    "load_private_messages_config",
]
