from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.engine import LockScope

from .models import DeploymentTarget, HostRole


class IndexError(RuntimeError):
    """An index operation failed without exposing indexed content."""


class IndexOwnershipError(IndexError):
    """A non-canonical target attempted to build the shared index."""


class IndexRootError(IndexError):
    """An injected index root is unsafe or overlaps another root."""


class IndexFormatError(IndexError):
    """An existing or newly built index violates the public schema."""


class IndexPrivacyError(IndexError):
    """An embedding boundary lacks explicit privacy authority."""


INDEX_SCHEMA_VERSION = 1


class IndexLease(Protocol):
    def acquire(self, scope: LockScope) -> AbstractContextManager[None]: ...


class EmbeddingPort(Protocol):
    model_id: str
    requires_cloud_authority: bool
    requires_external_egress: bool

    def embed(self, text: str) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class IndexRoots:
    pages_root: Path
    captures_root: Path
    output_root: Path
    database_name: str = "index.sqlite3"

    def __post_init__(self) -> None:
        roots = (self.pages_root, self.captures_root, self.output_root)
        resolved = tuple(_validate_root(root) for root in roots)
        if any(
            left == right or left in right.parents or right in left.parents
            for index, left in enumerate(resolved)
            for right in resolved[index + 1 :]
        ):
            raise IndexRootError("index roots must be distinct and non-overlapping")
        if (
            not isinstance(self.database_name, str)
            or not self.database_name
            or self.database_name in {".", ".."}
            or "/" in self.database_name
            or "\\" in self.database_name
            or "\x00" in self.database_name
        ):
            raise IndexRootError("invalid index database name")


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    generation_id: str
    database_path: Path
    document_count: int
    chunk_count: int
    embeddings_created: int
    embeddings_reused: int


@dataclass(frozen=True, slots=True)
class IndexCheckResult:
    available: bool
    generation_id: str | None
    document_count: int
    chunk_count: int


@dataclass(frozen=True, slots=True)
class _Document:
    source_path: str
    source_kind: str
    content: str
    content_sha256: str
    chunk_id: str


_SUPPORTED_SUFFIXES = frozenset({".json", ".md", ".txt"})


def build_index(
    *,
    target: DeploymentTarget,
    host_role: HostRole,
    roots: IndexRoots,
    lease: IndexLease,
    embedder: EmbeddingPort,
    privacy: PrivacyDecision,
) -> IndexBuildResult:
    if target is not DeploymentTarget.CANONICAL_WRITER or host_role is not HostRole.WRITER:
        raise IndexOwnershipError("index builds require the canonical writer target and role")
    if not isinstance(roots, IndexRoots):
        raise IndexRootError("invalid index roots")
    if not isinstance(privacy, PrivacyDecision):
        raise IndexPrivacyError("invalid embedding privacy decision")
    if (
        not isinstance(embedder.requires_cloud_authority, bool)
        or not isinstance(embedder.requires_external_egress, bool)
        or embedder.requires_cloud_authority
        and not privacy.authority.cloud
        or embedder.requires_external_egress
        and not privacy.authority.external_egress
    ):
        raise IndexPrivacyError("embedding authority denied")
    model_id = _validate_model_id(embedder.model_id)
    database_path = roots.output_root / roots.database_name

    with lease.acquire(LockScope.INDEX):
        documents = tuple(_iter_documents(roots))
        generation_id = _generation_id(documents, model_id)
        embedding_cache = _read_embedding_cache(database_path)
        temp_path = roots.output_root / (f".{roots.database_name}.{secrets.token_hex(16)}.tmp")
        try:
            created, reused = _build_temporary_database(
                temp_path=temp_path,
                documents=documents,
                generation_id=generation_id,
                model_id=model_id,
                embedding_cache=embedding_cache,
                embedder=embedder,
            )
            _fsync_file(temp_path)
            _replace_index(temp_path, database_path)
            _fsync_directory(roots.output_root)
        finally:
            _cleanup_temporary_files(temp_path)

    return IndexBuildResult(
        generation_id=generation_id,
        database_path=database_path,
        document_count=len(documents),
        chunk_count=len(documents),
        embeddings_created=created,
        embeddings_reused=reused,
    )


def check_index(*, target: DeploymentTarget, roots: IndexRoots) -> IndexCheckResult:
    if not isinstance(target, DeploymentTarget):
        raise IndexOwnershipError("invalid index check target")
    if not isinstance(roots, IndexRoots):
        raise IndexRootError("invalid index roots")
    database_path = roots.output_root / roots.database_name
    if not database_path.exists():
        return IndexCheckResult(False, None, 0, 0)
    if database_path.is_symlink() or not database_path.is_file():
        raise IndexRootError("unsafe index database path")

    try:
        with _connect_read_only(database_path) as connection:
            generation_row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'generation_id'"
            ).fetchone()
            document_count = int(connection.execute("SELECT count(*) FROM documents").fetchone()[0])
            chunk_count = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise IndexFormatError("index check failed") from None
    if generation_row is None or not _valid_generation_id(generation_row[0]):
        raise IndexFormatError("index generation marker is invalid")
    return IndexCheckResult(True, str(generation_row[0]), document_count, chunk_count)


def _validate_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise IndexRootError("index roots must be safe directories")
    try:
        metadata = os.lstat(root)
    except OSError:
        raise IndexRootError("index roots must be safe directories") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise IndexRootError("index roots must be safe directories")
    return root.resolve(strict=True)


def _iter_documents(roots: IndexRoots) -> Iterator[_Document]:
    sources = (("pages", roots.pages_root), ("captures", roots.captures_root))
    candidates: list[tuple[str, Path, Path]] = []
    for source_kind, source_root in sources:
        for path in source_root.rglob("*"):
            if path.is_symlink():
                continue
            if path.is_file() and path.suffix.lower() in _SUPPORTED_SUFFIXES:
                candidates.append((source_kind, source_root, path))

    for source_kind, source_root, path in sorted(
        candidates,
        key=lambda candidate: (
            candidate[0],
            candidate[2].relative_to(candidate[1]).as_posix(),
        ),
    ):
        try:
            raw_content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise IndexFormatError("index source could not be read") from None
        content = unicodedata.normalize(
            "NFC", raw_content.replace("\r\n", "\n").replace("\r", "\n")
        )
        relative = path.relative_to(source_root).as_posix()
        source_path = f"{source_kind}/{relative}"
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        identity = {
            "identity_version": 1,
            "source_path": source_path,
            "ordinal": 0,
            "content_sha256": content_hash,
        }
        yield _Document(
            source_path=source_path,
            source_kind=source_kind,
            content=content,
            content_sha256=content_hash,
            chunk_id="chunk_" + sha256(canonical_json_bytes(identity)).hexdigest(),
        )


def _generation_id(documents: tuple[_Document, ...], model_id: str) -> str:
    value = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "embedding_model": model_id,
        "documents": [
            {
                "source_path": document.source_path,
                "content_sha256": document.content_sha256,
                "chunk_id": document.chunk_id,
            }
            for document in documents
        ],
    }
    return "index_" + sha256(canonical_json_bytes(value)).hexdigest()


def _valid_generation_id(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("index_"):
        return False
    digest = value.removeprefix("index_")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _validate_model_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise IndexFormatError("invalid embedding model identifier")
    return value


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _read_embedding_cache(database_path: Path) -> dict[tuple[str, str], str]:
    if not database_path.exists():
        return {}
    if database_path.is_symlink() or not database_path.is_file():
        raise IndexRootError("unsafe index database path")
    try:
        with _connect_read_only(database_path) as connection:
            rows = connection.execute(
                "SELECT model_id, content_sha256, vector_json FROM embeddings"
            ).fetchall()
    except sqlite3.Error:
        raise IndexFormatError("existing index embedding cache is invalid") from None
    cache: dict[tuple[str, str], str] = {}
    for row in rows:
        model_id = _validate_model_id(row["model_id"])
        content_hash = row["content_sha256"]
        vector_json = row["vector_json"]
        if not isinstance(content_hash, str) or not isinstance(vector_json, str):
            raise IndexFormatError("existing index embedding cache is invalid")
        _decode_vector(vector_json)
        cache[(model_id, content_hash)] = vector_json
    return cache


def _build_temporary_database(
    *,
    temp_path: Path,
    documents: tuple[_Document, ...],
    generation_id: str,
    model_id: str,
    embedding_cache: dict[tuple[str, str], str],
    embedder: EmbeddingPort,
) -> tuple[int, int]:
    created = 0
    reused = 0
    connection = sqlite3.connect(temp_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE documents (
                source_path TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL REFERENCES documents(source_path),
                ordinal INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(source_path, ordinal)
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                chunk_id UNINDEXED,
                source_path UNINDEXED,
                content
            );
            CREATE TABLE embeddings (
                chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id),
                model_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                vector_json TEXT NOT NULL
            );
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (
                ("schema_version", str(INDEX_SCHEMA_VERSION)),
                ("generation_id", generation_id),
                ("embedding_model", model_id),
            ),
        )
        for document in documents:
            connection.execute(
                "INSERT INTO documents(source_path, source_kind, content_sha256, content) "
                "VALUES (?, ?, ?, ?)",
                (
                    document.source_path,
                    document.source_kind,
                    document.content_sha256,
                    document.content,
                ),
            )
            connection.execute(
                "INSERT INTO chunks(chunk_id, source_path, ordinal, content_sha256, content) "
                "VALUES (?, ?, 0, ?, ?)",
                (
                    document.chunk_id,
                    document.source_path,
                    document.content_sha256,
                    document.content,
                ),
            )
            connection.execute(
                "INSERT INTO chunks_fts(chunk_id, source_path, content) VALUES (?, ?, ?)",
                (document.chunk_id, document.source_path, document.content),
            )
            cache_key = (model_id, document.content_sha256)
            vector_json = embedding_cache.get(cache_key)
            if vector_json is None:
                vector_json = _encode_vector(embedder.embed(document.content))
                embedding_cache[cache_key] = vector_json
                created += 1
            else:
                reused += 1
            connection.execute(
                "INSERT INTO embeddings(chunk_id, model_id, content_sha256, vector_json) "
                "VALUES (?, ?, ?, ?)",
                (document.chunk_id, model_id, document.content_sha256, vector_json),
            )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return created, reused


def _encode_vector(vector: tuple[float, ...]) -> str:
    if (
        not isinstance(vector, tuple)
        or not vector
        or any(
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            for value in vector
        )
    ):
        raise IndexFormatError("embedding vector is invalid")
    return canonical_json_bytes([float(value) for value in vector]).decode("utf-8")


def _decode_vector(vector_json: str) -> tuple[float, ...]:
    try:
        value = json.loads(vector_json)
    except (TypeError, json.JSONDecodeError):
        raise IndexFormatError("embedding vector is invalid") from None
    if not isinstance(value, list):
        raise IndexFormatError("embedding vector is invalid")
    vector = tuple(value)
    _encode_vector(vector)
    return tuple(float(item) for item in vector)


def _fsync_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise IndexError("index file durability failed") from None


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise IndexError("index directory durability failed") from None


def _replace_index(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _cleanup_temporary_files(temp_path: Path) -> None:
    for candidate in temp_path.parent.glob(temp_path.name + "*"):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise IndexError("temporary index cleanup failed") from None
