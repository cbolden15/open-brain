"""Capped, rebuildable per-key brief log: refs, reason codes, evidence IDs and ack fingerprints."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    RootConfinementError,
    StorageError,
    atomic_replace,
    confined_unlink,
    read_confined,
)

from open_brain.services.brief_contracts import (
    BRIEF_LOG_MAX_BRIEFS,
    BRIEF_LOG_MAX_FILE_BYTES,
    BRIEF_LOG_MAX_KEYS,
    BRIEF_MAX_ITEMS,
    BriefChange,
    BriefItemKind,
    BriefReasonCode,
)

BRIEF_LOG_PROTOCOL = "open-brain-brief-log-v1"
BRIEF_LOG_DIRECTORY = "briefs"
LOG_ITEM_KEYS = frozenset({"changed", "evidence", "kind", "rank", "ref", "reasons"})

_KEY = re.compile(r"^[0-9a-f]{64}$")
_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_EVIDENCE_ID = re.compile(r"^evidence_" + _UUID4 + "$")
# Family IDs are "consumer:<connector_name>/<connection_id>:<space_id>" or "markdown:<space_id>"
# (Package 1). Connector names may hold spaces and non-ASCII text, so the bound is the contract's
# (brief_contracts._FAMILY_ID): 1..2200 non-control characters, here without a leading space.
_FAMILY_REF = re.compile(r"^family:[^\x00-\x20\x7f][^\x00-\x1f\x7f]{0,2199}$")
_REF = re.compile(r"^(followup|family):[^\x00-\x20\x7f][^\x00-\x1f\x7f]{0,2199}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_KINDS = frozenset(kind.value for kind in BriefItemKind)
_CHANGES = frozenset(change.value for change in BriefChange)
_REASONS = frozenset(reason.value for reason in BriefReasonCode)
_COVERAGE_WORDS = frozenset({"read", "not_granted", "failed", "not_read"})
_FILE_KEYS = frozenset({"acks", "briefs", "key", "protocol", "written_at"})
_ENTRY_KEYS = frozenset({"coverage", "generated_at", "items"})
_FINGERPRINT_KEYS = frozenset({"newest_recorded_at", "unavailable_count"})


@dataclass(frozen=True)
class FamilyFingerprint:
    newest_recorded_at: str
    unavailable_count: int

    def to_wire(self) -> dict[str, object]:
        return {
            "newest_recorded_at": self.newest_recorded_at,
            "unavailable_count": self.unavailable_count,
        }


@dataclass(frozen=True)
class BriefLogEntry:
    generated_at: str
    items: tuple[dict[str, object], ...]
    coverage: dict[str, object]


@dataclass(frozen=True)
class BriefLogFile:
    key: str
    briefs: tuple[BriefLogEntry, ...]
    acks: dict[str, FamilyFingerprint]
    sha256: str | None


@dataclass(frozen=True)
class BriefLogRead:
    file: BriefLogFile | None
    unavailable: bool
    entries: int
    bytes: int


class BriefLogWriteError(Exception):
    """The log file could not be written; the caller still returns its brief with saved=False."""


class _Corrupt(ValueError):
    """Internal: the stored bytes failed validation."""


class BriefLogStore:
    """One JSON file per visibility key under ``<state_dir>/briefs``; rebuildable, not Portable."""

    def __init__(self, state_dir: Path, *, clock: Callable[[], datetime]) -> None:
        if not isinstance(state_dir, Path) or not state_dir.is_absolute():
            raise ValueError("invalid brief log state directory")
        if not callable(clock):
            raise ValueError("invalid brief log clock")
        self._state_dir = state_dir
        self._clock = clock

    def read(self, key: str) -> BriefLogRead:
        _require_key(key)
        try:
            payload = read_confined(
                root=self._state_dir,
                relative=_relative(key),
                maximum_bytes=BRIEF_LOG_MAX_FILE_BYTES,
            )
        except RootConfinementError, StorageError:
            self._set_aside(key)
            return BriefLogRead(None, True, 0, 0)
        if payload is None:
            return BriefLogRead(None, False, 0, 0)
        try:
            file = _parse(key, payload)
        except _Corrupt:
            self._set_aside(key)
            return BriefLogRead(None, True, 0, 0)
        return BriefLogRead(file, False, len(file.briefs), len(payload))

    def append(self, key: str, entry: BriefLogEntry, *, expected_sha256: str | None) -> bool:
        _require_entry(entry)
        current = self.read(key)
        if _digest(current.file) != expected_sha256:
            return False
        briefs = (*(current.file.briefs if current.file is not None else ()), entry)
        acks = dict(current.file.acks) if current.file is not None else {}
        return self._write(
            key,
            briefs[-BRIEF_LOG_MAX_BRIEFS:],
            acks,
            existing=current.file is not None,
            expected_sha256=expected_sha256,
        )

    def ack(
        self,
        key: str,
        family_ref: str,
        fingerprint: FamilyFingerprint,
        *,
        expected_sha256: str | None,
    ) -> bool:
        if not isinstance(family_ref, str) or _FAMILY_REF.fullmatch(family_ref) is None:
            raise ValueError("invalid family ref")
        _require_fingerprint(fingerprint.to_wire())
        current = self.read(key)
        if _digest(current.file) != expected_sha256:
            return False
        briefs = current.file.briefs if current.file is not None else ()
        acks = dict(current.file.acks) if current.file is not None else {}
        acks[family_ref] = fingerprint
        return self._write(
            key, briefs, acks, existing=current.file is not None, expected_sha256=expected_sha256
        )

    def last(self, key: str) -> BriefLogEntry | None:
        current = self.read(key)
        if current.file is None or not current.file.briefs:
            return None
        return current.file.briefs[-1]

    # -- internals ---------------------------------------------------------------

    def _write(
        self,
        key: str,
        briefs: tuple[BriefLogEntry, ...],
        acks: dict[str, FamilyFingerprint],
        *,
        existing: bool,
        expected_sha256: str | None,
    ) -> bool:
        written_at = _timestamp(self._clock())
        payload = _render(key, briefs, acks, written_at=written_at)
        while len(payload) > BRIEF_LOG_MAX_FILE_BYTES and len(briefs) > 1:
            briefs = briefs[1:]
            payload = _render(key, briefs, acks, written_at=written_at)
        if len(payload) > BRIEF_LOG_MAX_FILE_BYTES:
            raise BriefLogWriteError("brief log entry exceeds the file cap")
        try:
            if not existing:
                self._evict_for(key)
            atomic_replace(
                root=self._state_dir,
                relative=_relative(key),
                data=payload,
                require_existing=existing,
                expected_existing_sha256=expected_sha256,
            )
        except DuplicateConflictError:
            return False
        except RootConfinementError, StorageError:
            raise BriefLogWriteError("brief log write failed") from None
        return True

    def _evict_for(self, key: str) -> None:
        directory = self._state_dir / BRIEF_LOG_DIRECTORY
        try:
            names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
        except FileNotFoundError:
            return
        others = [name for name in names if name != key + ".json"]
        while len(others) >= BRIEF_LOG_MAX_KEYS:
            victim = min(others, key=lambda name: (self._written_at(name), name))
            confined_unlink(root=self._state_dir, relative=f"{BRIEF_LOG_DIRECTORY}/{victim}")
            others.remove(victim)

    def _written_at(self, name: str) -> str:
        try:
            payload = read_confined(
                root=self._state_dir,
                relative=f"{BRIEF_LOG_DIRECTORY}/{name}",
                maximum_bytes=BRIEF_LOG_MAX_FILE_BYTES,
            )
            if payload is None:
                return ""
            value = json.loads(payload)
        except RootConfinementError, StorageError, UnicodeDecodeError, json.JSONDecodeError:
            return ""
        written = value.get("written_at") if isinstance(value, dict) else None
        return written if isinstance(written, str) else ""

    def _set_aside(self, key: str) -> None:
        directory = self._state_dir / BRIEF_LOG_DIRECTORY
        try:
            os.replace(directory / (key + ".json"), directory / (key + ".json.aside"))
        except OSError:
            # Nothing readable to preserve (or the rename itself failed); the key stays
            # unavailable for this brief and the next write starts a fresh file.
            return


def _require_key(key: str) -> None:
    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        raise ValueError("invalid brief log key")


def _relative(key: str) -> str:
    return f"{BRIEF_LOG_DIRECTORY}/{key}.json"


def _digest(file: BriefLogFile | None) -> str | None:
    return None if file is None else file.sha256


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _render(
    key: str,
    briefs: tuple[BriefLogEntry, ...],
    acks: Mapping[str, FamilyFingerprint],
    *,
    written_at: str,
) -> bytes:
    return portable_canonical_json_bytes(
        {
            "acks": {ref: acks[ref].to_wire() for ref in sorted(acks)},
            "briefs": [
                {
                    "coverage": entry.coverage,
                    "generated_at": entry.generated_at,
                    "items": list(entry.items),
                }
                for entry in briefs
            ],
            "key": key,
            "protocol": BRIEF_LOG_PROTOCOL,
            "written_at": written_at,
        }
    )


def _parse(key: str, payload: bytes) -> BriefLogFile:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise _Corrupt()
            result[name] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=unique, parse_constant=_reject)
    except _Corrupt:
        raise
    except UnicodeDecodeError, json.JSONDecodeError, TypeError, RecursionError:
        raise _Corrupt() from None
    if (
        not isinstance(value, dict)
        or set(value) != _FILE_KEYS
        or value["protocol"] != BRIEF_LOG_PROTOCOL
        or value["key"] != key
        or not isinstance(value["written_at"], str)
        or not isinstance(value["briefs"], list)
        or len(cast(list[object], value["briefs"])) > BRIEF_LOG_MAX_BRIEFS
        or not isinstance(value["acks"], dict)
        or portable_canonical_json_bytes(value) != payload
    ):
        raise _Corrupt()
    try:
        briefs = tuple(_entry_from(item) for item in cast(list[object], value["briefs"]))
        acks: dict[str, FamilyFingerprint] = {}
        for ref, raw in cast(dict[str, object], value["acks"]).items():
            if _FAMILY_REF.fullmatch(ref) is None:
                raise _Corrupt()
            _require_fingerprint(raw)
            fingerprint = cast(dict[str, object], raw)
            acks[ref] = FamilyFingerprint(
                cast(str, fingerprint["newest_recorded_at"]),
                cast(int, fingerprint["unavailable_count"]),
            )
    except ValueError:
        raise _Corrupt() from None
    return BriefLogFile(key, briefs, acks, sha256(payload).hexdigest())


def _entry_from(value: object) -> BriefLogEntry:
    if not isinstance(value, dict) or set(value) != _ENTRY_KEYS:
        raise ValueError("invalid brief log entry")
    raw = cast(dict[str, object], value)
    if not isinstance(raw["items"], list) or not isinstance(raw["coverage"], dict):
        raise ValueError("invalid brief log entry")
    entry = BriefLogEntry(
        generated_at=cast(str, raw["generated_at"]),
        items=tuple(cast(list[dict[str, object]], raw["items"])),
        coverage=cast(dict[str, object], raw["coverage"]),
    )
    _require_entry(entry)
    return entry


def _require_entry(entry: BriefLogEntry) -> None:
    if (
        not isinstance(entry, BriefLogEntry)
        or not isinstance(entry.generated_at, str)
        or _TIMESTAMP.fullmatch(entry.generated_at) is None
        or not isinstance(entry.items, tuple)
        or len(entry.items) > BRIEF_MAX_ITEMS
    ):
        raise ValueError("invalid brief log entry")
    for item in entry.items:
        _require_item(item)
    _require_coverage(entry.coverage, depth=0)


def _require_item(item: object) -> None:
    if not isinstance(item, dict) or set(item) != LOG_ITEM_KEYS:
        raise ValueError("brief log item must hold exactly " + ", ".join(sorted(LOG_ITEM_KEYS)))
    row = cast(dict[str, object], item)
    reasons = row["reasons"]
    evidence = row["evidence"]
    if (
        row["kind"] not in _KINDS
        or not isinstance(row["ref"], str)
        or _REF.fullmatch(row["ref"]) is None
        or not isinstance(reasons, list)
        or not 1 <= len(reasons) <= len(_REASONS)
        or any(reason not in _REASONS for reason in reasons)
        or len(set(cast(list[str], reasons))) != len(reasons)
        or not isinstance(evidence, list)
        or len(evidence) > 64
        or any(
            not isinstance(identifier, str) or _EVIDENCE_ID.fullmatch(identifier) is None
            for identifier in evidence
        )
        or row["changed"] not in _CHANGES
        or type(row["rank"]) is not int
        or not 1 <= row["rank"] <= BRIEF_MAX_ITEMS
    ):
        raise ValueError("invalid brief log item")


def _require_coverage(value: object, *, depth: int) -> None:
    """Counts, flags and closed status words only; nested once for ``log``."""
    if not isinstance(value, dict) or depth > 1 or len(cast(dict[str, object], value)) > 16:
        raise ValueError("invalid brief log coverage")
    for name, child in cast(dict[str, object], value).items():
        if not isinstance(name, str) or len(name) > 32:
            raise ValueError("invalid brief log coverage")
        if isinstance(child, dict):
            _require_coverage(child, depth=depth + 1)
        elif isinstance(child, str):
            if child not in _COVERAGE_WORDS:
                raise ValueError("brief log coverage carries free text")
        elif type(child) not in {int, bool} and child is not None:
            raise ValueError("invalid brief log coverage")


def _require_fingerprint(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _FINGERPRINT_KEYS
        or not isinstance(value["newest_recorded_at"], str)
        or _TIMESTAMP.fullmatch(value["newest_recorded_at"]) is None
        or type(value["unavailable_count"]) is not int
        or value["unavailable_count"] < 0
    ):
        raise ValueError("invalid family fingerprint")


def _reject(_constant: str) -> None:
    raise _Corrupt()


__all__ = [
    "BRIEF_LOG_DIRECTORY",
    "BRIEF_LOG_PROTOCOL",
    "LOG_ITEM_KEYS",
    "BriefLogEntry",
    "BriefLogFile",
    "BriefLogRead",
    "BriefLogStore",
    "BriefLogWriteError",
    "FamilyFingerprint",
]
