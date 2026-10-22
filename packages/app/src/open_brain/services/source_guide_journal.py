"""App-local guide workflow journal v1 (RECOVERY.md "Journal v1").

The journal is private orchestration metadata outside the Brain and collector
stores. It records exact canonical non-content request and intent preimages,
owner confirmation state and bounded receipts so a later explicit resume can
reconcile instead of guessing. It is never a second truth store: authority
services keep every receipt, and saved evidence never manufactures success.

Two locks protect it. A short nonblocking CAS lock (`<journal>.lock`) covers
only read/compare/write of a revision. A separate operation-lifetime flock
guard (`<journal>.guard`) is held, and inherited by a collector child, until
all potential authority work has stopped. Neither is ever stolen, unlinked or
repaired automatically.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final, cast

__all__ = [
    "ACTIONS",
    "MAX_JOURNAL_BYTES",
    "JournalError",
    "WorkflowJournal",
    "canonical_json",
    "new_journal",
    "operation_record",
    "sha256_text",
    "strict_json_object",
    "validate_journal",
]

MAX_JOURNAL_BYTES: Final = 262_144
_MAX_OPERATIONS: Final = 1000
_MAX_RECEIPTS: Final = 1000
_MAX_DEPTH: Final = 16
_DEFAULT_RESERVE: Final = 16_384
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
ACTIONS: Final = (
    "start",
    "preflight",
    "accounts",
    "resources",
    "choose",
    "configure",
    "preview",
    "import",
    "inspect",
    "acknowledge-inspection",
    "destination",
    "create-destination",
    "route",
    "consent",
    "select",
    "release-preview",
    "approve",
    "policy-preview",
    "policy-apply",
    "setup-preview",
    "setup-apply",
    "reconcile",
    "cancel",
)
_CHOICE_IDS: Final = (
    "connection_id",
    "resource_id",
    "resource_type",
    "destination_space_id",
    "provider_id",
)
_CHOICE_TEXT: Final = (
    "date_floor",
    "client_kind",
    "context_path",
    "consent_state",
    "policy_path",
    "runtime_path",
    "scope",
    "client_version",
)
_CHOICE_KEYS: Final = frozenset({*_CHOICE_IDS, *_CHOICE_TEXT, "account_alias", "launch_context"})
_LAUNCH_KEYS: Final = frozenset(
    {"surface", "cwd", "profile", "setting_sources", "settings_file", "overrides", "managed_state"}
)
_VERSION_KINDS: Final = frozenset(
    {"source", "route", "policy", "consent", "selection", "configuration", "collector"}
)


class JournalError(Exception):
    """Closed guide code; the journal never reports raw OS or parser detail."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_json(value: object) -> str:
    """UTF-8 canonical JSON: sorted keys, compact separators, no nonfinite numbers."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate key")
    return value


def _reject_constant(_value: str) -> object:
    raise ValueError("nonfinite number")


def _depth(value: object, level: int = 1) -> None:
    if level > _MAX_DEPTH:
        raise ValueError("nesting")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def strict_json_object(raw: bytes) -> dict[str, object]:
    """Parse one strict JSON object or raise ValueError/UnicodeError/RecursionError."""
    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant
    )
    _depth(value)
    if not isinstance(value, dict):
        raise ValueError("object required")
    return cast(dict[str, object], value)


def _is_id(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 512
        and not value.startswith("-")
        and not any(ord(item) < 32 or 127 <= ord(item) < 160 for item in value)
    )


def _is_text(value: object, maximum: int = 4096) -> bool:
    return (
        type(value) is str
        and len(value) <= maximum
        and not any(ord(item) < 32 or 127 <= ord(item) < 160 for item in value)
    )


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_hex(value: object) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _ids(value: object, maximum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(_is_id(item) for item in value)
        and len(set(cast(list[str], value))) == len(value)
    )


def _nullable_id(value: object) -> bool:
    return value is None or _is_id(value)


def _executable(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"path", "sha256", "device", "inode"}
        and _is_text(value["path"])
        and _is_hex(value["sha256"])
        and _is_count(value["device"])
        and _is_count(value["inode"])
    )


def _launch_context(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _LAUNCH_KEYS
        and value["surface"] == "standalone-cli"
        and _is_text(value["cwd"])
        and (value["profile"] is None or _is_text(value["profile"], 512))
        and value["setting_sources"] == ["user", "project", "local"]
        and value["settings_file"] is None
        and value["overrides"] == []
        and value["managed_state"] == "owner-declared-absent"
    )


_EVIDENCE: Final[dict[str, dict[str, Callable[[object], bool]]]] = {
    "collector": {
        "source_id": _is_id,
        "preview_id": _nullable_id,
        "receipt_ids": lambda value: _ids(value, 256),
    },
    "space": {"delivery_id": _is_id, "space_id": _is_id},
    "route": {
        "source_id": _is_id,
        "head": _is_id,
        "route_version": _is_count,
        "operation_id": _is_id,
        "request_sha256": _is_hex,
    },
    "consent": {"operation_id": _is_id, "consent_id": _is_id, "request_sha256": _is_hex},
    "selection": {"selection_id": _is_id, "member_source_ids": lambda value: _ids(value, 24)},
    "sharing": {
        "operation_id": _is_id,
        "preview_id": _nullable_id,
        "approval_id": _nullable_id,
        "copy_capture_id": _nullable_id,
        "state": lambda value: value is None or _is_id(value),
        "receipt_sha256": lambda value: value is None or _is_hex(value),
    },
    "policy": {"policy_sha256": _is_hex, "path": _is_text},
    "setup": {"preview_id": _is_id, "status": _is_id, "changed_path_count": _is_count},
    "observation": {"projection_sha256": _is_hex, "revision_id": _is_id},
}
_EVIDENCE_KIND: Final = {
    "configure": "collector",
    "preview": "collector",
    "import": "collector",
    "inspect": "observation",
    "acknowledge-inspection": "observation",
    "create-destination": "space",
    "route": "route",
    "consent": "consent",
    "select": "selection",
    "release-preview": "sharing",
    "approve": "sharing",
    "policy-preview": "policy",
    "policy-apply": "policy",
    "setup-preview": "setup",
    "setup-apply": "setup",
}


def _evidence_ok(action: str, value: object) -> bool:
    if value is None:
        return True
    kind = _EVIDENCE_KIND.get(action)
    if kind is None or not isinstance(value, dict):
        return False
    schema = _EVIDENCE[kind]
    return set(value) == set(schema) and all(check(value[key]) for key, check in schema.items())


def _version_ref(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"kind", "id", "version"}
        and value["kind"] in _VERSION_KINDS
        and _is_id(value["id"])
        and type(value["version"]) is str
        and len(value["version"]) <= 512
    )


def _execution_binding(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value)
        == {
            "brain_id",
            "issuer_epoch",
            "collector_binding",
            "source_generation",
            "control_epoch",
            "executable",
        }
        and _is_id(value["brain_id"])
        and _is_count(value["issuer_epoch"])
        and (value["collector_binding"] is None or _is_text(value["collector_binding"], 512))
        and (value["source_generation"] is None or _is_id(value["source_generation"]))
        and (value["control_epoch"] is None or _is_count(value["control_epoch"]))
        and _executable(value["executable"])
    )


def _intent_matches(operation: Mapping[str, object]) -> bool:
    """The saved preimage must parse to exactly this action/request and its digest."""
    text = operation["canonical_intent_utf8"]
    if type(text) is not str or sha256_text(text) != operation["intent_sha256"]:
        return False
    try:
        intent = strict_json_object(text.encode("utf-8"))
    except ValueError, UnicodeError, RecursionError:
        return False
    versions = intent.get("expected_versions")
    return (
        set(intent)
        == {
            "action",
            "canonical_request_utf8",
            "request_sha256",
            "expected_versions",
            "execution_binding",
        }
        and canonical_json(intent) == text
        and intent["action"] == operation["action"]
        and intent["canonical_request_utf8"] == operation["canonical_request_utf8"]
        and intent["request_sha256"] == operation["request_sha256"]
        and isinstance(versions, list)
        and len(versions) <= 24
        and all(_version_ref(item) for item in versions)
        and _execution_binding(intent["execution_binding"])
    )


def _operation_ok(value: object, sequence: int) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "sequence",
        "action",
        "intent_sha256",
        "request_sha256",
        "canonical_request_utf8",
        "canonical_intent_utf8",
        "confirmation",
        "state",
        "executing_pid",
        "evidence",
    }:
        return False
    request = value["canonical_request_utf8"]
    if (
        value["sequence"] != sequence
        or type(value["sequence"]) is not int
        or value["action"] not in ACTIONS
        or value["action"] == "start"
        or not _is_hex(value["intent_sha256"])
        or not _is_hex(value["request_sha256"])
        or type(request) is not str
        or len(request.encode("utf-8")) > 65_536
        or sha256_text(request) != value["request_sha256"]
        or value["confirmation"] not in {"unconfirmed", "confirmed"}
        or value["state"] not in {"prepared", "executing", "completed", "unknown", "refused"}
        or not (value["executing_pid"] is None or _is_count(value["executing_pid"]))
    ):
        return False
    try:
        if canonical_json(strict_json_object(request.encode("utf-8"))) != request:
            return False
    except ValueError, UnicodeError, RecursionError:
        return False
    return _intent_matches(value) and _evidence_ok(cast(str, value["action"]), value["evidence"])


def validate_journal(value: object) -> dict[str, object]:
    """Validate the closed v1 shape; any difference is `journal_incompatible`, never repair."""
    try:
        ok = _journal_ok(value)
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise JournalError("journal_incompatible")
    return cast(dict[str, object], value)


def _journal_ok(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "workflow_id",
        "revision",
        "status",
        "binding",
        "choices",
        "operations",
        "retained_receipt_ids",
    }:
        return False
    binding, choices, operations = value["binding"], value["choices"], value["operations"]
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not _is_id(value["workflow_id"])
        or not _is_count(value["revision"])
        or value["status"] not in {"active", "cancelled", "completed", "halted"}
        or not isinstance(binding, dict)
        or set(binding)
        != {
            "brain_id",
            "issuer_epoch",
            "brain_root",
            "collector_state",
            "collector_binding",
            "executable",
        }
        or not _is_id(binding["brain_id"])
        or not _is_count(binding["issuer_epoch"])
        or not all(
            _is_text(binding[key]) and binding[key]
            for key in ("brain_root", "collector_state", "collector_binding")
        )
        or not _executable(binding["executable"])
        or not isinstance(choices, dict)
        or set(choices) != _CHOICE_KEYS
        or not all(_nullable_id(choices[key]) for key in _CHOICE_IDS)
        or not all(choices[key] is None or _is_text(choices[key]) for key in _CHOICE_TEXT)
        or not (choices["account_alias"] is None or _is_text(choices["account_alias"], 80))
        or not (choices["launch_context"] is None or _launch_context(choices["launch_context"]))
        or not isinstance(operations, list)
        or len(operations) > _MAX_OPERATIONS
        or not _ids(value["retained_receipt_ids"], _MAX_RECEIPTS)
    ):
        return False
    return all(_operation_ok(item, index) for index, item in enumerate(operations))


def new_journal(workflow_id: str, binding: Mapping[str, object]) -> dict[str, object]:
    return validate_journal(
        {
            "schema_version": 1,
            "workflow_id": workflow_id,
            "revision": 1,
            "status": "active",
            "binding": copy.deepcopy(dict(binding)),
            "choices": dict.fromkeys(sorted(_CHOICE_KEYS)),
            "operations": [],
            "retained_receipt_ids": [],
        }
    )


def operation_record(
    *,
    sequence: int,
    action: str,
    request: Mapping[str, object],
    expected_versions: Sequence[Mapping[str, object]],
    execution_binding: Mapping[str, object],
) -> dict[str, object]:
    """Build one prepared operation with its exact request and intent preimages."""
    canonical_request = canonical_json(dict(request))
    request_sha256 = sha256_text(canonical_request)
    canonical_intent = canonical_json(
        {
            "action": action,
            "canonical_request_utf8": canonical_request,
            "request_sha256": request_sha256,
            "expected_versions": [dict(item) for item in expected_versions],
            "execution_binding": dict(execution_binding),
        }
    )
    return {
        "sequence": sequence,
        "action": action,
        "intent_sha256": sha256_text(canonical_intent),
        "request_sha256": request_sha256,
        "canonical_request_utf8": canonical_request,
        "canonical_intent_utf8": canonical_intent,
        "confirmation": "unconfirmed",
        "state": "prepared",
        "executing_pid": None,
        "evidence": None,
    }


def _private_file(fd: int) -> bool:
    info = os.fstat(fd)
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
    )


class WorkflowJournal:
    """One journal path plus its sibling CAS lock and lifetime guard."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self.guard_path = path.with_name(path.name + ".guard")

    @contextmanager
    def _parent(self, *, missing_ok: bool = False) -> Iterator[int | None]:
        try:
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if missing_ok:
                yield None
                return
            raise JournalError("unsafe_path") from None
        except OSError:
            raise JournalError("unsafe_path") from None
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise JournalError("unsafe_path")
            yield fd
        finally:
            os.close(fd)

    def _open(self, parent: int, name: str, flags: int) -> int:
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except OSError as error:
            if error.errno == errno.ENOENT:
                raise
            raise JournalError("unsafe_path") from None
        if not _private_file(fd):
            os.close(fd)
            raise JournalError("unsafe_path")
        return fd

    def _read_at(self, parent: int) -> dict[str, object] | None:
        try:
            fd = self._open(parent, self.path.name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            raw = b""
            while len(raw) <= MAX_JOURNAL_BYTES and (chunk := os.read(fd, 65_536)):
                raw += chunk
        finally:
            os.close(fd)
        if len(raw) > MAX_JOURNAL_BYTES:
            raise JournalError("journal_incompatible")
        try:
            value = strict_json_object(raw)
        except ValueError, UnicodeError, RecursionError:
            raise JournalError("journal_incompatible") from None
        return validate_journal(value)

    def read(self) -> dict[str, object] | None:
        """Read the existing journal without locks or writes; absent is None."""
        with self._parent(missing_ok=True) as parent:
            return None if parent is None else self._read_at(parent)

    @staticmethod
    def _write_new(parent: int, name: str, payload: bytes) -> None:
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _temporary(self, parent: int, payload: bytes) -> str:
        name = f".{self.path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        self._write_new(parent, name, payload)
        return name

    @staticmethod
    def _guard_bytes(name: str, workflow_id: str) -> bytes:
        return canonical_json({"journal": name, "workflow_id": workflow_id}).encode("utf-8")

    def create(self, document: Mapping[str, object]) -> None:
        """Create-only: an existing journal is never replaced; lost create reads it back."""
        value = validate_journal(copy.deepcopy(dict(document)))
        payload = canonical_json(value).encode("utf-8")
        if len(payload) > MAX_JOURNAL_BYTES:
            raise JournalError("capacity_reached")
        with self._parent() as parent:
            assert parent is not None
            if self._read_at(parent) is not None:
                raise JournalError("revision_conflict")
            try:
                guard = self._guard_bytes(self.path.name, cast(str, value["workflow_id"]))
                self._write_new(parent, self.guard_path.name, guard)
                self._write_new(parent, self.lock_path.name, b"")
            except FileExistsError:
                # A guard or lock without a valid journal is unavailable, never repaired.
                raise JournalError("journal_busy") from None
            except OSError:
                raise JournalError("unsafe_path") from None
            temporary = self._temporary(parent, payload)
            try:
                os.link(temporary, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
            except FileExistsError:
                raise JournalError("revision_conflict") from None
            finally:
                os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)

    @contextmanager
    def cas_lock(self) -> Iterator[int]:
        """Short nonblocking lock for one read/compare/write; never held across a child."""
        with self._parent() as parent:
            assert parent is not None
            try:
                fd = self._open(parent, self.lock_path.name, os.O_RDWR)
            except (FileNotFoundError, JournalError):
                raise JournalError("journal_busy") from None
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise JournalError("journal_busy") from None
                yield parent
            finally:
                os.close(fd)

    def write(
        self,
        document: Mapping[str, object],
        *,
        expected_revision: int,
        reserve_bytes: int = _DEFAULT_RESERVE,
    ) -> dict[str, object]:
        """Compare-and-swap one revision; capacity is reserved before any authority call."""
        with self.cas_lock() as parent:
            current = self._read_at(parent)
            if current is None:
                raise JournalError("state_uninitialized")
            if current["revision"] != expected_revision:
                raise JournalError("revision_conflict")
            if (
                current["workflow_id"] != document["workflow_id"]
                or current["binding"] != document["binding"]
            ):
                raise JournalError("binding_mismatch")
            value = copy.deepcopy(dict(document))
            value["revision"] = expected_revision + 1
            if (
                len(cast(list[object], value["operations"])) > _MAX_OPERATIONS
                or len(cast(list[object], value["retained_receipt_ids"])) > _MAX_RECEIPTS
            ):
                raise JournalError("capacity_reached")
            validate_journal(value)
            payload = canonical_json(value).encode("utf-8")
            if len(payload) + reserve_bytes > MAX_JOURNAL_BYTES:
                raise JournalError("capacity_reached")
            temporary = self._temporary(parent, payload)
            os.replace(temporary, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            return value

    def acquire_guard(self, workflow_id: str) -> int:
        """Take the operation-lifetime guard nonblocking; busy/unsafe/forged is `journal_busy`.

        The returned open file description is passed to a collector child, which
        keeps the lock held even if this supervisor is killed.
        """
        try:
            with self._parent() as parent:
                assert parent is not None
                fd = self._open(parent, self.guard_path.name, os.O_RDONLY)
        except (FileNotFoundError, JournalError):
            raise JournalError("journal_busy") from None
        try:
            expected = self._guard_bytes(self.path.name, workflow_id)
            if os.read(fd, len(expected) + 1) != expected:
                raise JournalError("journal_busy")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise JournalError("journal_busy") from None
        except BaseException:
            os.close(fd)
            raise
        return fd

    def guard_free(self, workflow_id: str) -> bool:
        """Probe the guard without keeping it; a held, unsafe or forged guard is not free."""
        try:
            fd = self.acquire_guard(workflow_id)
        except JournalError:
            return False
        os.close(fd)
        return True
