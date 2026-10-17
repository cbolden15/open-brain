"""Derived owner-local embedding cache ``ob.embedding-cache.v1`` and its foreground rebuild.

Contract: ``docs/plans/2026-10-15-competitive-gaps-g1/EMBEDDING-RESOURCE.md`` sections 7-8
with goal decisions D7 (the helper stage shares this root and its quota), D10 (one
owner-built chunk store; callers match on the exact projected field SHA-256) and D11
(float32 little-endian vectors). The cache is never authoritative, never Portable content
and never grants eligibility: callers authorize and project before using any vector.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
import unicodedata
from array import array
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final, NamedTuple, NoReturn

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.storage.filesystem import StorageError, _open_child_directory, _open_root
from open_brain_engine.storage.locks import LockBusyError

from .capture_lifecycle import open_lifecycle_journal
from .embedding_backend import (
    EMBEDDING_DIMENSIONS,
    EmbeddedItem,
    EmbeddingBackend,
    EmbeddingFailure,
)
from .embedding_policy import EmbeddingPolicy, _require_owner, read_policy
from .mcp_eligibility import install
from .paging import _authorized_retrieval_digest, read_snapshot
from .records import ProjectedRecord, RecordProjector
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .local import BrainEngine

CACHE_SCHEMA: Final = "ob.embedding-cache.v1"
POINTER_SCHEMA: Final = "ob.embedding-cache-pointer.v1"
# Engine-side preprocessing: NFC once, title and body as separate items. The helper's
# tokenizer profile is bound through ``component_id`` (decision D3).
PREPROCESS_ID: Final = "ob.embedding-engine-nfc-fields.v1"
SEGMENTER_VERSION: Final = "ob.embedding-windows-224-192.v1"
PROJECTION_VERSION: Final = "ob.embedding-owner-projection.v1"
FIELDS: Final = ("title", "body")
MAX_CHUNKS = 100_000
MAX_GENERATION_SET_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_BATCH_ITEMS: Final = 4
MAX_BATCH_CHUNKS: Final = 128
MAX_DOCUMENT_BYTES = 65536
MAX_REQUEST_BYTES: Final = 131072
# Parent budget for one rebuild's bounded exchanges (EMBEDDING-RESOURCE section 5).
REBUILD_SECONDS = 600.0
VECTOR_BYTES: Final = EMBEDDING_DIMENSIONS * 4
_WINDOW: Final = 224
_STRIDE: Final = 192
_REQUEST_OVERHEAD: Final = 1024
_PARTS: Final = (".open-brain", "cache", "embedding-v1")
_MANIFEST: Final = "manifest.json"
_INDEX: Final = "index.jsonl"
_VECTORS: Final = "vectors.f32"
_POINTER: Final = "active.json"
_LOCK: Final = ".rebuild.lock"
_MANIFEST_LIMIT: Final = 1024 * 1024
_GENERATION = re.compile(r"[0-9a-f]{32}")
_TEMPORARY = re.compile(r"\.write-[0-9a-f]{32}")
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_MANIFEST_TEXT: Final = frozenset(
    {
        "schema",
        "generation_id",
        "component_id",
        "preprocess_id",
        "segmenter_version",
        "projection_version",
        "eligibility_snapshot_digest",
        "member_digest",
        "uncovered_digest",
        "excluded_oversize_digest",
        "state",
    }
)
_MANIFEST_COUNTS: Final = frozenset(
    {
        "policy_generation",
        "expected_records",
        "actual_records",
        "expected_chunks",
        "actual_chunks",
        "dropped_changed",
        "uncovered_count",
        "excluded_oversize_count",
    }
)
_MANIFEST_KEYS: Final = _MANIFEST_TEXT | _MANIFEST_COUNTS | {"eligibility", "files"}
_UNAVAILABLE_REASONS: Final = frozenset({"consent_required", "cache_invalid", "cache_incomplete"})


class ChunkRef(NamedTuple):
    """Exact owner projection identity a caller must reproduce byte-for-byte (D10)."""

    record_type: str
    result_id: str
    revision_id: str
    field: str
    projection_sha256: str


@dataclass(frozen=True, slots=True)
class ActiveGeneration:
    """One validated complete generation; ``chunks`` lists vectors in chunk ordinal order."""

    generation_id: str
    component_id: str
    policy_generation: int
    eligibility_snapshot_digest: str
    chunks: Mapping[ChunkRef, tuple[array[float], ...]]


@dataclass(frozen=True, slots=True)
class CacheUnavailable:
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in _UNAVAILABLE_REASONS:
            raise ValueError("invalid cache unavailability reason")


@dataclass(frozen=True, slots=True)
class RebuildReceipt:
    generation_id: str
    records: int
    chunks: int
    dropped_changed: int
    uncovered: int
    excluded_oversize: int
    reused_chunks: int
    embedded_chunks: int
    seconds: float


class _Unavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _fail(category: str) -> NoReturn:
    raise EmbeddingFailure(category)


def _invalid() -> NoReturn:
    raise _Unavailable("cache_invalid")


def _digest(domain: str, value: object) -> str:
    return sha256(domain.encode("ascii") + b"\0" + portable_canonical_json_bytes(value)).hexdigest()


def _never() -> bool:
    return False


def _check(cancelled: Callable[[], bool], deadline: float | None = None) -> None:
    if cancelled():
        _fail("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        _fail("deadline")


# ------------------------------------------------------------- projection seam


def field_texts(projected: ProjectedRecord) -> dict[str, str]:
    """NFC title and body exactly as embedded; query-time callers reuse this (D10)."""
    body = projected.text if projected.indexed_text is None else projected.indexed_text
    return {
        "title": unicodedata.normalize("NFC", str(projected.summary["title"])),
        "body": unicodedata.normalize("NFC", body),
    }


def projection_sha256(text: str) -> str:
    """SHA-256 of the exact NFC field bytes; equals the helper's ``text_sha256``."""
    return sha256(text.encode("utf-8")).hexdigest()


def dependency_sha256(projected: ProjectedRecord) -> str:
    return _digest("ob.embedding-dependency.v1", projected.summary)


@dataclass(frozen=True, slots=True)
class _Field:
    ref: ChunkRef
    dependency_sha256: str
    text: str

    @property
    def key(self) -> tuple[ChunkRef, str]:
        return self.ref, self.dependency_sha256


@dataclass(frozen=True, slots=True)
class _Record:
    fields: tuple[_Field, ...]

    @property
    def signature(self) -> tuple[tuple[ChunkRef, str], ...]:
        return tuple(field.key for field in self.fields)


def _owner_candidates(
    connection: sqlite3.Connection, profile: LocalEngineContext, authority: EffectiveAuthority
) -> dict[tuple[str, str], _Record]:
    """Owner-visible current records under the same clauses owner search uses."""
    install(connection, authority)
    clauses = [
        "mcp_record_visible(d.record_type,d.result_id)=1",
        "(d.record_type='canonical' OR EXISTS (SELECT 1 FROM logical_sources s "
        "WHERE s.head_capture_id=d.result_id AND s.historical_only=0 "
        "AND s.lifecycle='active' AND s.availability='available'))",
    ]
    parameters: list[Any] = []
    tiers = tuple(tier.value for tier in PrivacyTier if authority.permits_read_tier(tier))
    if tiers:
        clauses.append("d.effective_tier IN (" + ",".join("?" for _ in tiers) + ")")
        parameters.extend(tiers)
    else:
        clauses.append("0")
    if authority.space_ids is not None:
        spaces = sorted(authority.space_ids)
        clauses.append("d.space_id IN (" + ",".join("?" for _ in spaces) + ")")
        parameters.extend(spaces)
    rows = connection.execute(
        "SELECT d.record_type,d.result_id FROM search_documents d WHERE "
        + " AND ".join(clauses)
        + " ORDER BY d.record_type COLLATE BINARY,d.result_id COLLATE BINARY",
        parameters,
    ).fetchall()
    projector = RecordProjector(profile, connection, authority)
    records: dict[tuple[str, str], _Record] = {}
    for record_type, result_id in rows:
        try:
            projected = projector.read(result_id)
        except T03Error as error:
            if error.code in {"not_found", "revision_changed"}:
                continue
            raise
        summary = projected.summary
        if (summary["record_type"], summary["record_id"]) != (record_type, result_id):
            continue
        dependency = dependency_sha256(projected)
        texts = field_texts(projected)
        records[(record_type, result_id)] = _Record(
            tuple(
                _Field(
                    ChunkRef(
                        record_type,
                        result_id,
                        summary["revision_id"],
                        name,
                        projection_sha256(texts[name]),
                    ),
                    dependency,
                    texts[name],
                )
                for name in FIELDS
            )
        )
    return records


def _eligibility(
    connection: sqlite3.Connection, authority: EffectiveAuthority, policy: EmbeddingPolicy
) -> dict[str, object]:
    projection = connection.execute(
        "SELECT projection_policy_version FROM engine_generations WHERE singleton=1"
    ).fetchone()
    return {
        "policy_generation": policy.policy_generation,
        "eligibility_generation": open_lifecycle_journal(connection).eligibility_generation,
        "projection_policy_version": None if projection is None else projection[0],
        "authorized_retrieval_sha256": _authorized_retrieval_digest(
            connection, authority, visible_state=None
        ),
    }


def _currentness(
    connection: sqlite3.Connection, authority: EffectiveAuthority, policy: EmbeddingPolicy
) -> tuple[dict[str, object], str]:
    """Eligibility and its digest; revalidation and the fenced check both use this."""
    eligibility = _eligibility(connection, authority, policy)
    return eligibility, _digest("ob.embedding-eligibility.v1", eligibility)


@contextmanager
def _snapshot(engine: BrainEngine) -> Iterator[sqlite3.Connection]:
    with read_snapshot(engine) as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        yield connection


def _require_policy(connection: sqlite3.Connection, component_id: str) -> EmbeddingPolicy:
    policy = read_policy(connection)
    if not policy.enabled:
        _fail("consent_required")
    if policy.component_id != component_id:
        _fail("component_mismatch")
    return policy


# ---------------------------------------------------------- private filesystem


def _private_directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        _invalid()


def _open_directory(parent: int, name: str) -> int:
    descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent)
    try:
        _private_directory(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _private_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
    )


def _read_file(directory: int, name: str, maximum: int) -> bytes:
    """Read one private regular file; missing is incomplete, anything unexpected invalid."""
    return _read_private(directory, name, maximum)[0]


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


def _stat_private(directory: int, name: str) -> os.stat_result:
    """Open and check one private regular file without reading it."""
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        raise _Unavailable("cache_incomplete") from None
    except OSError:
        _invalid()
    try:
        info = os.fstat(descriptor)
        if not _private_file(info):
            _invalid()
        return info
    finally:
        os.close(descriptor)


def _read_private(directory: int, name: str, maximum: int) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        raise _Unavailable("cache_incomplete") from None
    except OSError:
        _invalid()
    try:
        info = os.fstat(descriptor)
        if not _private_file(info) or info.st_size > maximum:
            _invalid()
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining > 0:
            data = os.read(descriptor, min(remaining, 1024 * 1024))
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
        if remaining != 0 or os.read(descriptor, 1):
            _invalid()
        return b"".join(chunks), info
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _write_file(directory: int, name: str, data: bytes) -> None:
    descriptor = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=directory)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tree_bytes(directory: int) -> int:
    total = 0
    for _, _, files, descriptor in os.fwalk(".", dir_fd=directory, follow_symlinks=False):
        for name in files:
            total += os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_size
    return total


def _remove_generation(generations: int, name: str) -> None:
    """Remove one non-active generation only after custody checks; refuse otherwise."""
    if _GENERATION.fullmatch(name) is None:
        _invalid()
    try:
        _remove_checked(generations, name)
    except OSError:
        _invalid()


def _remove_checked(generations: int, name: str) -> None:
    descriptor = _open_directory(generations, name)
    try:
        entries = os.listdir(descriptor)
        for entry in entries:
            info = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
            if (
                entry not in {_MANIFEST, _INDEX, _VECTORS} and _TEMPORARY.fullmatch(entry) is None
            ) or not _private_file(info):
                _invalid()
        for entry in entries:
            os.unlink(entry, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=generations)
    os.fsync(generations)


class _Root:
    """Open descriptors for ``embedding-v1`` and its ``generations`` directory."""

    def __init__(self, root: int, generations: int) -> None:
        self.root = root
        self.generations = generations
        # Set once ``active.json`` names a generation, before its durability fsync.
        self.activated: str | None = None

    @classmethod
    @contextmanager
    def open(cls, profile: LocalEngineContext, *, create: bool) -> Iterator[_Root | None]:
        opened: list[int] = []
        try:
            try:
                opened.append(_open_root(profile.root, profile.root_identity))
                for name in (*_PARTS, "generations"):
                    opened.append(_open_child_directory(opened[-1], name, create=create))
                    _private_directory(opened[-1])
            except FileNotFoundError:
                if create:
                    _invalid()
                yield None
                return
            except OSError, StorageError:
                _invalid()
            yield cls(opened[-2], opened[-1])
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    @contextmanager
    def derivation_lock(self) -> Iterator[None]:
        """One rebuild per Brain; a competing build is refused, never queued."""
        descriptor = os.open(
            _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.root
        )
        try:
            info = os.fstat(descriptor)
            if not _private_file(info):
                _invalid()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockBusyError("embedding cache rebuild in progress") from None
            yield
        finally:
            os.close(descriptor)

    def pointer(self) -> tuple[str, str] | None:
        entry = self.pointer_entry()
        return None if entry is None else entry[0]

    def pointer_entry(self) -> tuple[tuple[str, str], tuple[int, int, int, int]] | None:
        """The pointer with the stat identity of the ``active.json`` it was read from."""
        try:
            raw, info = _read_private(self.root, _POINTER, 4096)
        except _Unavailable as error:
            if error.reason == "cache_incomplete":
                return None
            raise
        try:
            value = json.loads(raw)
        except ValueError, RecursionError:
            _invalid()
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "generation_id", "manifest_sha256"}
            or value["schema"] != POINTER_SCHEMA
            or not isinstance(value["generation_id"], str)
            or _GENERATION.fullmatch(value["generation_id"]) is None
            or not isinstance(value["manifest_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["manifest_sha256"]) is None
            or portable_canonical_json_bytes(value) != raw
        ):
            _invalid()
        return (value["generation_id"], value["manifest_sha256"]), _stat_identity(info)

    def activate(self, generation_id: str, manifest_sha256: str) -> None:
        data = portable_canonical_json_bytes(
            {
                "schema": POINTER_SCHEMA,
                "generation_id": generation_id,
                "manifest_sha256": manifest_sha256,
            }
        )
        temporary = ".write-" + secrets.token_hex(16)
        try:
            _write_file(self.root, temporary, data)
            os.replace(temporary, _POINTER, src_dir_fd=self.root, dst_dir_fd=self.root)
            self.activated = generation_id
            os.fsync(self.root)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self.root)

    def remove_inactive(self, keep: str | None) -> None:
        for name in sorted(os.listdir(self.generations)):
            if name != keep:
                _remove_generation(self.generations, name)
        for name in os.listdir(self.root):
            if _TEMPORARY.fullmatch(name) is not None:
                if not _private_file(os.stat(name, dir_fd=self.root, follow_symlinks=False)):
                    _invalid()
                os.unlink(name, dir_fd=self.root)

    def file_identities(self, generation_id: str) -> tuple[tuple[int, int, int, int], ...]:
        """Cheap custody check of one generation's files; no content is read."""
        try:
            directory = _open_directory(self.generations, generation_id)
        except FileNotFoundError:
            raise _Unavailable("cache_incomplete") from None
        except OSError:
            _invalid()
        try:
            return tuple(
                _stat_identity(_stat_private(directory, name))
                for name in (_MANIFEST, _INDEX, _VECTORS)
            )
        finally:
            os.close(directory)

    def load(self, generation_id: str, manifest_sha256: str) -> _Loaded:
        try:
            directory = _open_directory(self.generations, generation_id)
        except FileNotFoundError:
            raise _Unavailable("cache_incomplete") from None
        except OSError:
            _invalid()
        try:
            return _load_generation(directory, generation_id, manifest_sha256)
        finally:
            os.close(directory)


# --------------------------------------------------------------- generations


@dataclass(frozen=True, slots=True)
class _Chunk:
    line: bytes
    vector: bytes


@dataclass(frozen=True, slots=True)
class _Loaded:
    manifest: dict[str, Any]
    items: dict[tuple[ChunkRef, str], tuple[_Chunk, ...]]


def _identity(manifest: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        manifest["component_id"],
        manifest["preprocess_id"],
        manifest["segmenter_version"],
        manifest["projection_version"],
    )


def _chunk_key(component_id: str, field: _Field, ranges: tuple[int, int, int, int]) -> str:
    ref = field.ref
    return _digest(
        "ob.embedding-chunk-key.v1",
        {
            "component_id": component_id,
            "preprocess_id": PREPROCESS_ID,
            "segmenter_version": SEGMENTER_VERSION,
            "record_type": ref.record_type,
            "result_id": ref.result_id,
            "revision_id": ref.revision_id,
            "field": ref.field,
            "projection_sha256": ref.projection_sha256,
            "dependency_sha256": field.dependency_sha256,
            "token_start": ranges[0],
            "token_end": ranges[1],
            "byte_start": ranges[2],
            "byte_end": ranges[3],
        },
    )


def _line(component_id: str, field: _Field, ordinal: int, ranges: tuple[int, ...]) -> bytes:
    ref = field.ref
    token_start, token_end, byte_start, byte_end = ranges
    return (
        portable_canonical_json_bytes(
            {
                "chunk_key": _chunk_key(
                    component_id, field, (token_start, token_end, byte_start, byte_end)
                ),
                "record_type": ref.record_type,
                "result_id": ref.result_id,
                "revision_id": ref.revision_id,
                "field": ref.field,
                "projection_sha256": ref.projection_sha256,
                "dependency_sha256": field.dependency_sha256,
                "ordinal": ordinal,
                "token_start": token_start,
                "token_end": token_end,
                "byte_start": byte_start,
                "byte_end": byte_end,
            }
        )
        + b"\n"
    )


def _vector_bytes(vector: Sequence[float]) -> bytes:
    """Canonical IEEE754 float32 little endian, rechecked after conversion (section 6)."""
    try:
        converted = array("f", vector)
    except OverflowError, TypeError:
        _fail("protocol_invalid")
    canonical = array("f", (value + 0.0 for value in converted))
    if (
        len(canonical) != EMBEDDING_DIMENSIONS
        or not all(math.isfinite(value) and -1.0001 <= value <= 1.0001 for value in canonical)
        or not 0.999 <= math.sqrt(math.sumprod(canonical, canonical)) <= 1.001
    ):
        _fail("protocol_invalid")
    if sys.byteorder != "little":
        canonical.byteswap()
    return canonical.tobytes()


def _decode(data: bytes) -> array[float]:
    vector = array("f")
    vector.frombytes(data)
    if sys.byteorder != "little":
        vector.byteswap()
    return vector


def _members(items: Mapping[tuple[ChunkRef, str], Sequence[_Chunk]]) -> list[dict[str, object]]:
    return [
        {**ref._asdict(), "dependency_sha256": dependency, "chunks": len(items[(ref, dependency)])}
        for ref, dependency in sorted(items)
    ]


def _load_generation(directory: int, generation_id: str, manifest_sha256: str) -> _Loaded:
    raw = _read_file(directory, _MANIFEST, _MANIFEST_LIMIT)
    if sha256(raw).hexdigest() != manifest_sha256:
        _invalid()
    try:
        manifest = json.loads(raw)
    except ValueError, RecursionError:
        _invalid()
    if not isinstance(manifest, dict) or manifest.get("schema") != CACHE_SCHEMA:
        _invalid()
    if manifest.get("state") != "complete":
        raise _Unavailable("cache_incomplete")
    try:
        files = manifest["files"]
        if (
            set(manifest) != _MANIFEST_KEYS
            or not all(isinstance(manifest[key], str) for key in _MANIFEST_TEXT)
            or not all(type(manifest[key]) is int for key in _MANIFEST_COUNTS)
            or manifest["generation_id"] != generation_id
            or portable_canonical_json_bytes(manifest) != raw
            or set(files) != {_INDEX, _VECTORS}
        ):
            _invalid()
        index = _read_file(directory, _INDEX, files[_INDEX]["bytes"])
        vectors = _read_file(directory, _VECTORS, files[_VECTORS]["bytes"])
        for name, data in ((_INDEX, index), (_VECTORS, vectors)):
            if files[name] != {"bytes": len(data), "sha256": sha256(data).hexdigest()}:
                _invalid()
        lines = index.splitlines(keepends=True)
        if (
            len(lines) != manifest["actual_chunks"]
            or manifest["actual_chunks"] != manifest["expected_chunks"]
            or len(vectors) != len(lines) * VECTOR_BYTES
            or manifest["actual_records"] != manifest["expected_records"]
        ):
            raise _Unavailable("cache_incomplete")
        items: dict[tuple[ChunkRef, str], list[_Chunk]] = {}
        for row, line in enumerate(lines):
            value = json.loads(line)
            ref = ChunkRef(
                value["record_type"],
                value["result_id"],
                value["revision_id"],
                value["field"],
                value["projection_sha256"],
            )
            field = _Field(ref, value["dependency_sha256"], "")
            chunks = items.setdefault(field.key, [])
            ranges = (
                value["token_start"],
                value["token_end"],
                value["byte_start"],
                value["byte_end"],
            )
            if (
                value["ordinal"] != len(chunks)
                or _line(manifest["component_id"], field, len(chunks), ranges) != line
            ):
                _invalid()
            chunks.append(_Chunk(line, vectors[row * VECTOR_BYTES : (row + 1) * VECTOR_BYTES]))
        loaded = {key: tuple(value) for key, value in items.items()}
        records = {(ref.record_type, ref.result_id) for ref, _ in loaded}
        if (
            len(records) != manifest["actual_records"]
            or _digest("ob.embedding-members.v1", _members(loaded)) != manifest["member_digest"]
        ):
            _invalid()
        return _Loaded(manifest, loaded)
    except _Unavailable:
        raise
    except ValueError, TypeError, KeyError, AttributeError, RecursionError:
        _invalid()


def _write_generation(
    root: _Root,
    *,
    generation_id: str,
    manifest: dict[str, Any],
    items: Mapping[tuple[ChunkRef, str], Sequence[_Chunk]],
) -> str:
    """Write files, then the manifest last, fsynced with the parent; return its digest."""
    os.mkdir(generation_id, 0o700, dir_fd=root.generations)
    directory = _open_directory(root.generations, generation_id)
    try:
        ordered = [chunk for key in sorted(items) for chunk in items[key]]
        index = b"".join(chunk.line for chunk in ordered)
        vectors = b"".join(chunk.vector for chunk in ordered)
        _write_file(directory, _VECTORS, vectors)
        _write_file(directory, _INDEX, index)
        records = {(ref.record_type, ref.result_id) for ref, _ in items}
        if (
            len(ordered) != manifest["expected_chunks"]
            or len(records) != manifest["expected_records"]
        ):
            _fail("cache_incomplete")
        manifest = {
            **manifest,
            "actual_chunks": len(ordered),
            "actual_records": len(records),
            "member_digest": _digest("ob.embedding-members.v1", _members(items)),
            "files": {
                _INDEX: {"bytes": len(index), "sha256": sha256(index).hexdigest()},
                _VECTORS: {"bytes": len(vectors), "sha256": sha256(vectors).hexdigest()},
            },
            "state": "complete",
        }
        raw = portable_canonical_json_bytes(manifest)
        _write_file(directory, _MANIFEST, raw)
        os.fsync(directory)
    finally:
        os.close(directory)
    os.fsync(root.generations)
    return sha256(raw).hexdigest()


def _activate(root: _Root, generation_id: str, manifest_sha256: str) -> None:
    root.activate(generation_id, manifest_sha256)


# -------------------------------------------------------------------- rebuild


def _estimated_chunks(field: _Field) -> int:
    # Every content token covers at least one byte, so bytes bound tokens and windows.
    size = len(field.text.encode("utf-8"))
    return 1 if size <= _WINDOW else 1 + -(-(size - _WINDOW) // _STRIDE)


def _request_bytes(field: _Field) -> int:
    return len(json.dumps(field.text, ensure_ascii=False).encode("utf-8"))


def _batches(fields: Sequence[_Field]) -> Iterator[list[_Field]]:
    """At most four items and an upper bound of 128 chunks per exchange."""
    batch: list[_Field] = []
    chunks = size = 0
    for field in fields:
        estimate, request = _estimated_chunks(field), _request_bytes(field)
        if batch and (
            len(batch) == MAX_BATCH_ITEMS
            or chunks + estimate > MAX_BATCH_CHUNKS
            or size + request + _REQUEST_OVERHEAD > MAX_REQUEST_BYTES
        ):
            yield batch
            batch, chunks, size = [], 0, 0
        batch.append(field)
        chunks += estimate
        size += request
    if batch:
        yield batch


def _validated(
    items: object, batch: Sequence[_Field], component_id: str
) -> list[tuple[_Field, tuple[_Chunk, ...]]]:
    if type(items) is not list or len(items) != len(batch):
        _fail("protocol_invalid")
    accepted: list[tuple[_Field, tuple[_Chunk, ...]]] = []
    total = 0
    for slot, (item, field) in enumerate(zip(items, batch, strict=True)):
        size = len(field.text.encode("utf-8"))
        if (
            not isinstance(item, EmbeddedItem)
            or item.slot != slot
            or item.text_sha256 != field.ref.projection_sha256
            or [chunk.ordinal for chunk in item.chunks] != list(range(len(item.chunks)))
            or any(chunk.byte_end > size for chunk in item.chunks)
        ):
            _fail("protocol_invalid")
        total += len(item.chunks)
        accepted.append(
            (
                field,
                tuple(
                    _Chunk(
                        _line(
                            component_id,
                            field,
                            chunk.ordinal,
                            (chunk.token_start, chunk.token_end, chunk.byte_start, chunk.byte_end),
                        ),
                        _vector_bytes(chunk.vector),
                    )
                    for chunk in item.chunks
                ),
            )
        )
    if total > MAX_BATCH_CHUNKS:
        _fail("protocol_invalid")
    return accepted


def _embed(
    backend: EmbeddingBackend,
    batch: list[_Field],
    component_id: str,
    checkpoint: Callable[[], None],
) -> tuple[list[tuple[_Field, tuple[_Chunk, ...]]], list[_Field]]:
    """Accepted items and the items the helper refused with ``input_limit`` (D12)."""
    try:
        return _validated(
            backend.embed("document", [field.text for field in batch]), batch, component_id
        ), []
    except EmbeddingFailure as error:
        if error.category != "input_limit":
            raise
        if len(batch) == 1:
            # A refused item is excluded, never silently indexed as a prefix.
            return [], list(batch)
    # Isolate the refused item: exactly one more single-item exchange per batch item.
    accepted: list[tuple[_Field, tuple[_Chunk, ...]]] = []
    refused: list[_Field] = []
    for field in batch:
        checkpoint()
        single, rejected = _embed(backend, [field], component_id, checkpoint)
        accepted.extend(single)
        refused.extend(rejected)
    return accepted, refused


def _refs_digest(
    domain: str, records: Mapping[tuple[str, str], _Record], identities: set[tuple[str, str]]
) -> str:
    """Domain-separated digest of sorted ``(record_type, result_id, revision_id, field)``."""
    return _digest(
        domain,
        sorted(
            [field.ref.record_type, field.ref.result_id, field.ref.revision_id, field.ref.field]
            for identity in identities
            for field in records[identity].fields
        ),
    )


class _Quota:
    def __init__(self, root: _Root) -> None:
        self.set_bytes = _tree_bytes(root.generations)
        self.total_bytes = _tree_bytes(root.root)  # includes the D7 helper stage
        self.chunks = 0
        self.added = 0
        self.check()

    def add(self, chunks: Sequence[_Chunk]) -> None:
        self.chunks += len(chunks)
        self.added += sum(len(chunk.line) + len(chunk.vector) for chunk in chunks)
        self.check()

    def check(self) -> None:
        if (
            self.chunks > MAX_CHUNKS
            or self.set_bytes + self.added > MAX_GENERATION_SET_BYTES
            or self.total_bytes + self.added > MAX_TOTAL_BYTES
        ):
            _fail("cache_incomplete")


def rebuild(
    engine: BrainEngine,
    *,
    authority: EffectiveAuthority,
    backend: EmbeddingBackend,
    cancelled: Callable[[], bool] = _never,
) -> RebuildReceipt:
    """Owner-only foreground rebuild; the previous generation stays active on any failure."""
    started = time.monotonic()
    component_id = backend.component_id
    _check(cancelled)
    with _snapshot(engine) as connection:
        _require_owner(connection, authority)
        policy = _require_policy(connection, component_id)
    try:
        with _Root.open(engine.profile, create=True) as root, _locked(root):
            assert root is not None
            try:
                pointer = root.pointer()
            except _Unavailable:
                pointer = None
            root.remove_inactive(None if pointer is None else pointer[0])
            previous: _Loaded | None = None
            if pointer is not None:
                with suppress(_Unavailable):
                    previous = root.load(*pointer)
            return _rebuild_locked(
                engine,
                root,
                authority=authority,
                backend=backend,
                cancelled=cancelled,
                policy=policy,
                previous=previous,
                started=started,
            )
    except _Unavailable as error:
        _fail(error.reason)
    except OSError:
        _fail("cache_incomplete")


@contextmanager
def _locked(root: _Root | None) -> Iterator[None]:
    if root is None:
        _invalid()
    with root.derivation_lock():
        yield


def _rebuild_locked(
    engine: BrainEngine,
    root: _Root,
    *,
    authority: EffectiveAuthority,
    backend: EmbeddingBackend,
    cancelled: Callable[[], bool],
    policy: EmbeddingPolicy,
    previous: _Loaded | None,
    started: float,
) -> RebuildReceipt:
    component_id = backend.component_id
    deadline = started + REBUILD_SECONDS

    def checkpoint() -> None:
        _check(cancelled, deadline)

    # 1. Collect owner-visible projected items under one ordinary read snapshot.
    with _snapshot(engine) as connection:
        _require_owner(connection, authority)
        policy = _require_policy(connection, component_id)
        planned = _owner_candidates(connection, engine.profile, authority)
    checkpoint()
    # D12: a field over the per-item limit excludes its whole record, never a prefix.
    excluded = {
        identity
        for identity, record in planned.items()
        if any(len(field.text.encode("utf-8")) > MAX_DOCUMENT_BYTES for field in record.fields)
    }
    fields = [
        field
        for identity, record in planned.items()
        if identity not in excluded
        for field in record.fields
        if field.text.strip()
    ]
    if len(fields) > MAX_CHUNKS:
        _fail("cache_incomplete")
    quota = _Quota(root)
    reusable: Mapping[tuple[ChunkRef, str], tuple[_Chunk, ...]] = {}
    if previous is not None and _identity(previous.manifest) == (
        component_id,
        PREPROCESS_ID,
        SEGMENTER_VERSION,
        PROJECTION_VERSION,
    ):
        reusable = previous.items
    built: dict[tuple[ChunkRef, str], tuple[_Chunk, ...]] = {}
    pending: list[_Field] = []
    reused = 0
    for field in fields:
        if field.key in reusable:
            built[field.key] = reusable[field.key]
            reused += len(reusable[field.key])
            quota.add(reusable[field.key])
        else:
            pending.append(field)
    # 2. Bounded inference outside every engine lock and snapshot.
    for batch in _batches(pending):
        checkpoint()
        accepted, refused = _embed(backend, batch, component_id, checkpoint)
        for field, chunks in accepted:
            quota.add(chunks)
            built[field.key] = chunks
        excluded.update((field.ref.record_type, field.ref.result_id) for field in refused)
    checkpoint()
    # 3. Reacquire read admission and revalidate every record. Changed planned records are
    # dropped; records created or changed since the plan are uncovered (lexical-only).
    with _snapshot(engine) as connection:
        _require_owner(connection, authority)
        current_policy = read_policy(connection)
        if not current_policy.enabled:
            _fail("consent_required")
        if (
            current_policy.component_id != component_id
            or current_policy.policy_generation != policy.policy_generation
        ):
            _fail("currentness_changed")
        current = _owner_candidates(connection, engine.profile, authority)
        eligibility, digest = _currentness(connection, authority, current_policy)
    dropped = {
        identity
        for identity, record in planned.items()
        if identity not in current or current[identity].signature != record.signature
    }
    uncovered = {
        identity
        for identity, record in current.items()
        if identity not in planned or planned[identity].signature != record.signature
    }
    excluded &= current.keys() - uncovered
    # Expected coverage comes from the revalidation-time current set, not the plan.
    kept: dict[tuple[ChunkRef, str], tuple[_Chunk, ...]] = {}
    for identity, record in current.items():
        if identity in uncovered or identity in excluded:
            continue
        for field in record.fields:
            if field.text.strip():
                if field.key not in built:
                    _fail("cache_incomplete")
                kept[field.key] = built[field.key]
    checkpoint()
    generation_id = secrets.token_hex(16)
    try:
        manifest_sha256 = _write_generation(
            root,
            generation_id=generation_id,
            manifest={
                "schema": CACHE_SCHEMA,
                "generation_id": generation_id,
                "component_id": component_id,
                "preprocess_id": PREPROCESS_ID,
                "segmenter_version": SEGMENTER_VERSION,
                "projection_version": PROJECTION_VERSION,
                "policy_generation": current_policy.policy_generation,
                "eligibility": eligibility,
                "eligibility_snapshot_digest": digest,
                "expected_records": len({(ref.record_type, ref.result_id) for ref, _ in kept}),
                "expected_chunks": sum(len(chunks) for chunks in kept.values()),
                "dropped_changed": len(dropped),
                "uncovered_count": len(uncovered),
                "uncovered_digest": _refs_digest("ob.embedding-uncovered.v1", current, uncovered),
                "excluded_oversize_count": len(excluded),
                "excluded_oversize_digest": _refs_digest(
                    "ob.embedding-excluded-oversize.v1", current, excluded
                ),
            },
            items=kept,
        )
        checkpoint()
        # 4. Final eligibility check and pointer swap under the exclusive writer lease. The
        # check borrows that lease into the same authorized snapshot revalidation used.
        with engine._writer_lease.hold_writer_transaction() as held:
            with held.borrow(), _snapshot(engine) as connection:
                final_policy = read_policy(connection)
                if not final_policy.enabled:
                    _fail("consent_required")
                if (
                    final_policy.component_id != component_id
                    or _currentness(connection, authority, final_policy)[1] != digest
                ):
                    _fail("currentness_changed")
            try:
                _activate(root, generation_id, manifest_sha256)
            except OSError:
                # Translated here so the lease release cannot recast a swap failure.
                _fail("cache_incomplete")
    except Exception as error:
        # Never remove the generation ``active.json`` already names.
        if root.activated != generation_id:
            with suppress(OSError, _Unavailable):
                _remove_generation(root.generations, generation_id)
        if isinstance(error, OSError):
            _fail("cache_incomplete")
        raise
    root.remove_inactive(generation_id)
    return RebuildReceipt(
        generation_id=generation_id,
        records=len({(ref.record_type, ref.result_id) for ref, _ in kept}),
        chunks=sum(len(chunks) for chunks in kept.values()),
        dropped_changed=len(dropped),
        uncovered=len(uncovered),
        excluded_oversize=len(excluded),
        reused_chunks=sum(
            len(chunks) for key, chunks in kept.items() if reusable.get(key) is chunks
        ),
        embedded_chunks=sum(
            len(chunks) for key, chunks in kept.items() if reusable.get(key) is not chunks
        ),
        seconds=round(time.monotonic() - started, 3),
    )


# ---------------------------------------------------------------------- load


def _policy(engine: BrainEngine, connection: sqlite3.Connection | None) -> EmbeddingPolicy:
    if connection is not None:
        return read_policy(connection)
    with _snapshot(engine) as owned:
        return read_policy(owned)


def active_status(
    engine: BrainEngine, *, connection: sqlite3.Connection | None = None
) -> ActiveGeneration | CacheUnavailable:
    """Validated active generation for the current policy generation, or a typed reason."""
    policy = _policy(engine, connection)
    if not policy.enabled:
        # Revocation is immediate; garbage collection never gates it.
        return CacheUnavailable("consent_required")
    try:
        with _Root.open(engine.profile, create=False) as root:
            if root is None:
                return CacheUnavailable("cache_incomplete")
            entry = root.pointer_entry()
            if entry is None:
                return CacheUnavailable("cache_incomplete")
            try:
                identity, active = _decoded(root, *entry)
            except _Unavailable:
                # A concurrent activation may have retired the generation just read.
                retry = root.pointer_entry()
                if retry is None or retry[0] == entry[0]:
                    raise
                identity, active = _decoded(root, *retry)
    except _Unavailable as error:
        return CacheUnavailable(error.reason)
    except OSError:
        return CacheUnavailable("cache_invalid")
    if (
        active.component_id != policy.component_id
        or active.policy_generation != policy.policy_generation
        or identity[1:] != (PREPROCESS_ID, SEGMENTER_VERSION, PROJECTION_VERSION)
    ):
        return CacheUnavailable("cache_incomplete")
    return active


# One decoded generation per process, keyed by its pointer and the stat identities of
# ``active.json`` and the generation files, so repeat loads skip the full re-hash and
# decode. Custody checks and the policy check above still run on every call.
_recent: tuple[tuple[object, ...], tuple[object, ...], ActiveGeneration] | None = None


def _decoded(
    root: _Root, pointer: tuple[str, str], pointer_identity: tuple[int, int, int, int]
) -> tuple[tuple[object, ...], ActiveGeneration]:
    global _recent
    key = (*pointer, pointer_identity, root.file_identities(pointer[0]))
    recent = _recent
    if recent is not None and recent[0] == key:
        return recent[1], recent[2]
    loaded = root.load(*pointer)
    manifest = loaded.manifest
    active = ActiveGeneration(
        generation_id=manifest["generation_id"],
        component_id=manifest["component_id"],
        policy_generation=manifest["policy_generation"],
        eligibility_snapshot_digest=manifest["eligibility_snapshot_digest"],
        chunks={
            ref: tuple(_decode(chunk.vector) for chunk in chunks)
            for (ref, _), chunks in loaded.items.items()
        },
    )
    _recent = (key, _identity(manifest), active)
    return _recent[1], active


def load_active(
    engine: BrainEngine, *, connection: sqlite3.Connection | None = None
) -> ActiveGeneration | None:
    status = active_status(engine, connection=connection)
    return status if isinstance(status, ActiveGeneration) else None


__all__ = [
    "CACHE_SCHEMA",
    "FIELDS",
    "MAX_CHUNKS",
    "MAX_GENERATION_SET_BYTES",
    "MAX_TOTAL_BYTES",
    "ActiveGeneration",
    "CacheUnavailable",
    "ChunkRef",
    "RebuildReceipt",
    "active_status",
    "dependency_sha256",
    "field_texts",
    "load_active",
    "projection_sha256",
    "rebuild",
]
