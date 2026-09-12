"""Owner-configured production retention with a dry-run-only public boundary."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.ports import Clock

from open_brain_legacy._compat.open_brain.config import AppConfig
from open_brain_legacy.operations.retention import (
    RetentionArtifactKind,
    RetentionCandidate,
    plan_retention,
    run_retention,
)

_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_CANDIDATES = 10_000


class ProductionRetentionError(RuntimeError):
    """The production retention policy or requested operation is invalid."""


_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """Redacted retention outcome owned by the production service."""

    candidate_count: int
    manifest_digest: str
    protected_count: int
    removed_count: int
    replayed: bool

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (self.candidate_count, self.protected_count, self.removed_count)
            )
            or self.protected_count > self.candidate_count
            or self.removed_count > self.candidate_count - self.protected_count
            or _SHA256.fullmatch(self.manifest_digest) is None
            or not isinstance(self.replayed, bool)
        ):
            raise ValueError("invalid retention report")


class ProductionRetentionRoot(StrEnum):
    BACKUP = "backup"
    CAPTURE = "capture"
    SAVED_CONTENT = "saved_content"
    STATE = "state"


@dataclass(frozen=True, slots=True)
class ProductionRetentionConfig:
    root: ProductionRetentionRoot
    candidates: tuple[RetentionCandidate, ...] = field(repr=False)

    def __post_init__(self) -> None:
        identities = tuple(
            (candidate.artifact_id, candidate.relative_path) for candidate in self.candidates
        )
        if (
            not isinstance(self.root, ProductionRetentionRoot)
            or not isinstance(self.candidates, tuple)
            or len(self.candidates) > _MAX_CANDIDATES
            or any(
                not isinstance(candidate, RetentionCandidate)
                for candidate in self.candidates
            )
            or tuple(sorted(identities)) != identities
            or len(set(identities)) != len(identities)
        ):
            raise ProductionRetentionError("invalid private retention config")

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> ProductionRetentionConfig:
        value = _decode_mapping(payload)
        raw_candidates = value.get("candidates")
        if (
            set(value) != {"schema_version", "root", "candidates"}
            or value.get("schema_version") != 1
            or not isinstance(raw_candidates, list)
            or len(raw_candidates) > _MAX_CANDIDATES
        ):
            raise ProductionRetentionError("invalid private retention config")
        try:
            candidates = tuple(_candidate(item) for item in raw_candidates)
            raw_root = value["root"]
            if not isinstance(raw_root, str):
                raise ProductionRetentionError("invalid private retention config")
            root = ProductionRetentionRoot(raw_root)
            result = cls(root=root, candidates=candidates)
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ProductionRetentionError):
                raise
            raise ProductionRetentionError("invalid private retention config") from error
        if result.canonical_bytes() != payload:
            raise ProductionRetentionError("invalid private retention config")
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "root": self.root.value,
                "candidates": [
                    {
                        "artifact_id": candidate.artifact_id,
                        "relative_path": candidate.relative_path,
                        "expires_at": _timestamp(candidate.expires_at),
                        "kind": candidate.kind.value,
                    }
                    for candidate in self.candidates
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class ProductionRetentionService:
    root: Path = field(repr=False)
    candidates: tuple[RetentionCandidate, ...] = field(repr=False)
    clock: Clock = field(repr=False)

    def retain(self, *, dry_run: bool) -> RetentionReport:
        if type(dry_run) is not bool:
            raise ProductionRetentionError("invalid retention request")
        if not dry_run:
            raise ProductionRetentionError(
                "retention apply requires a separate exact plan approval"
            )
        cutoff = self.clock.now()
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ProductionRetentionError("invalid retention clock")
        try:
            plan = plan_retention(
                root=self.root,
                cutoff=cutoff,
                candidates=self.candidates,
            )
            receipt = run_retention(
                root=self.root,
                plan=plan,
                replay_key="job-024-" + cutoff.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
            )
        except Exception as error:
            raise ProductionRetentionError("retention dry run failed") from error
        return RetentionReport(
            candidate_count=len(self.candidates),
            manifest_digest=plan.digest_sha256,
            protected_count=receipt.protected_count,
            removed_count=receipt.deleted_count,
            replayed=receipt.replayed,
        )


def load_private_retention_config(path: Path) -> ProductionRetentionConfig:
    return ProductionRetentionConfig.from_canonical_bytes(_read_owner_file(path))


def compose_production_retention_service(
    *,
    app_config: AppConfig,
    config_path: Path,
    clock: Clock,
) -> ProductionRetentionService:
    if not isinstance(app_config, AppConfig) or not callable(getattr(clock, "now", None)):
        raise ProductionRetentionError("invalid retention composition")
    config = load_private_retention_config(config_path)
    roots = {
        ProductionRetentionRoot.BACKUP: app_config.backup_root,
        ProductionRetentionRoot.CAPTURE: app_config.capture_root,
        ProductionRetentionRoot.SAVED_CONTENT: app_config.saved_content_root,
        ProductionRetentionRoot.STATE: app_config.state_root,
    }
    return ProductionRetentionService(
        root=roots[config.root],
        candidates=config.candidates,
        clock=clock,
    )


def _candidate(value: object) -> RetentionCandidate:
    item = _mapping(value)
    if set(item) != {"artifact_id", "relative_path", "expires_at", "kind"}:
        raise ProductionRetentionError("invalid private retention config")
    artifact_id = item["artifact_id"]
    relative_path = item["relative_path"]
    raw_kind = item["kind"]
    if not all(
        isinstance(candidate, str)
        for candidate in (artifact_id, relative_path, raw_kind)
    ):
        raise ProductionRetentionError("invalid private retention config")
    assert isinstance(artifact_id, str)
    assert isinstance(relative_path, str)
    assert isinstance(raw_kind, str)
    expires_at = _parse_timestamp(item["expires_at"])
    return RetentionCandidate(
        artifact_id=artifact_id,
        relative_path=relative_path,
        expires_at=expires_at,
        kind=RetentionArtifactKind(raw_kind),
    )


def _read_owner_file(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ProductionRetentionError("invalid private retention config")
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
            raise ProductionRetentionError("invalid private retention config")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_CONFIG_BYTES + 1)
    except ProductionRetentionError:
        raise
    except OSError as error:
        raise ProductionRetentionError("invalid private retention config") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > _MAX_CONFIG_BYTES:
        raise ProductionRetentionError("invalid private retention config")
    return payload


def _decode_mapping(payload: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if isinstance(error, ProductionRetentionError):
            raise
        raise ProductionRetentionError("invalid private retention config") from error
    return _mapping(value)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ProductionRetentionError("invalid private retention config")
    return cast(Mapping[str, object], value)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProductionRetentionError("invalid private retention config")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProductionRetentionError("invalid private retention config") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductionRetentionError("invalid private retention config")
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(UTC)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ProductionRetentionError("invalid private retention config")
        value[key] = item
    return value


__all__ = [
    "ProductionRetentionConfig",
    "ProductionRetentionError",
    "ProductionRetentionRoot",
    "ProductionRetentionService",
    "RetentionReport",
    "compose_production_retention_service",
    "load_private_retention_config",
]
