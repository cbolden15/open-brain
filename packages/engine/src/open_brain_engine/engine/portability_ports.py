"""Tenant-bound local ports used only by Portable Brain tasks."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.review_binding import validate_review_binding
from open_brain_engine.portable.v1 import validate_portable_write
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    RootIdentity,
    WriteState,
    assert_root_identity,
    atomic_replace,
    atomic_write_new,
    capture_root_identity,
    open_root_descriptor,
    read_confined,
)
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown, render_markdown

_IDENTIFIER = re.compile(
    r"^(?P<prefix>[a-z][a-z0-9_]*)_"
    r"(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$")


class SourceRecordPort(Protocol):
    tenant_id: str

    def records(self) -> Iterable[tuple[str, bytes]]: ...

    def put_capture(self, payload: bytes) -> WriteState: ...


class BatchPort(Protocol):
    tenant_id: str

    def batches(self) -> Iterable[tuple[str, bytes]]: ...

    def put_batch(self, payload: bytes) -> WriteState: ...


class BlobPort(Protocol):
    tenant_id: str

    def blobs(self) -> Iterable[tuple[str, bytes]]: ...

    def put_blob(self, payload: bytes) -> WriteState: ...


class PortableHistoryPort(Protocol):
    tenant_id: str

    def history(self) -> Iterable[tuple[str, bytes]]: ...

    def put_history(self, family: str, payload: bytes) -> WriteState: ...


class TenantStoragePort(Protocol):
    tenant_id: str

    def portable_files(self) -> Iterable[tuple[str, bytes]]: ...


class PortableWritePort(Protocol):
    """Typed Portable writers injected into local engine durable operations."""

    def put_capture(self, payload: bytes) -> WriteState: ...

    def replace_capture(self, payload: bytes) -> WriteState: ...

    def put_blob(self, payload: bytes) -> WriteState: ...

    def put_history(self, family: str, payload: bytes) -> WriteState: ...

    def put_page(self, relative: str, payload: bytes) -> WriteState: ...

    def replace_page(
        self, relative: str, payload: bytes, *, expected_sha256: str
    ) -> WriteState: ...

    def put_space(self, payload: bytes, *, replace: bool) -> WriteState: ...


@dataclass(frozen=True, slots=True)
class ContentProtectionDeclaration:
    scheme: str
    encrypted: bool

    def __post_init__(self) -> None:
        if self.scheme != "none" or self.encrypted is not False:
            raise ValueError("local content protection declaration is invalid")


class ContentProtectionPort(Protocol):
    tenant_id: str

    def declaration(self) -> ContentProtectionDeclaration: ...


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_relative(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or "\\" in relative
        or any(part in {"", ".", "..", ".open-brain"} for part in path.parts)
    ):
        raise ValueError("unsafe Portable path")
    return path


def _canonical_json_object(payload: bytes, label: str) -> dict[str, object]:
    if not isinstance(payload, bytes):
        raise ValueError(f"invalid Portable {label}")
    try:
        value = json.loads(payload)
        if not isinstance(value, dict) or portable_canonical_json_bytes(value) != payload:
            raise ValueError
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError(f"invalid Portable {label}") from error
    return cast(dict[str, object], value)


def _canonical_jsonl_objects(payload: bytes, label: str) -> list[dict[str, object]]:
    if not isinstance(payload, bytes) or not payload or not payload.endswith(b"\n"):
        raise ValueError(f"invalid Portable {label}")
    records: list[dict[str, object]] = []
    for line in payload.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            raise ValueError(f"invalid Portable {label}")
        records.append(_canonical_json_object(line[:-1], label))
    return records


def _identifier(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid Portable {label}")
    match = _IDENTIFIER.fullmatch(value)
    if match is None or match.group("prefix") != prefix:
        raise ValueError(f"invalid Portable {label}")
    return value


def _date_partition(value: object, label: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"invalid Portable {label}")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError(f"invalid Portable {label}") from error
    return f"{value[:4]}/{value[5:7]}"


def _record_relative(
    payload: bytes,
    *,
    tenant_id: str,
    label: str,
    directory: str,
    identifier_key: str,
    identifier_prefix: str,
    timestamp_key: str,
) -> str:
    record = _canonical_json_object(payload, label)
    if record.get("tenant_id") != tenant_id:
        raise ValueError(f"invalid Portable {label} tenant")
    identifier = _identifier(record.get(identifier_key), identifier_prefix, label)
    partition = _date_partition(record.get(timestamp_key), label)
    return f"{directory}/{partition}/{identifier}.json"


def _immutable_put(
    root: Path,
    root_identity: RootIdentity,
    relative: str,
    payload: bytes,
    label: str,
) -> WriteState:
    try:
        return atomic_write_new(
            root=root,
            relative=relative,
            data=payload,
            expected_root_identity=root_identity,
        )
    except DuplicateConflictError as error:
        raise ValueError(f"Portable {label} conflict") from error


def _canonical_markdown(payload: bytes, label: str) -> dict[str, object]:
    if not isinstance(payload, bytes):
        raise ValueError(f"invalid Portable {label}")
    try:
        parsed = parse_markdown(payload)
        if render_markdown(fields=parsed.fields, body=parsed.body).encode() != payload:
            raise MarkdownFormatError("noncanonical Portable Markdown")
    except MarkdownFormatError as error:
        raise ValueError(f"invalid Portable {label}") from error
    return dict(parsed.fields)


def _validate_write(relative: str, payload: bytes, tenant_id: str) -> None:
    try:
        validate_portable_write(relative, payload, tenant_id)
    except ValueError as error:
        raise ValueError("invalid Portable typed record") from error


def _open_portable_file(directory_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )


@dataclass(frozen=True, slots=True)
class LocalTenantStorage:
    """Root-confined local storage, bound to exactly one profile tenant."""

    root: Path
    tenant_id: str
    root_identity: RootIdentity | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.root, Path)
            or not self.root.is_absolute()
            or not isinstance(self.tenant_id, str)
        ):
            raise ValueError("invalid local Portable storage")
        identity = (
            capture_root_identity(self.root)
            if self.root_identity is None
            else self.root_identity
        )
        assert_root_identity(self.root, identity)
        object.__setattr__(self, "root_identity", identity)

    @property
    def bound_root_identity(self) -> RootIdentity:
        identity = self.root_identity
        assert identity is not None
        return identity

    def portable_files(self) -> Iterator[tuple[str, bytes]]:
        try:
            root_fd = open_root_descriptor(self.root, self.bound_root_identity)
        except OSError as error:
            raise ValueError("unsafe Portable source file") from error
        try:
            yield from self._portable_files_from_descriptor(root_fd, ())
        finally:
            os.close(root_fd)

    def _portable_files_from_descriptor(
        self, directory_fd: int, parts: tuple[str, ...]
    ) -> Iterator[tuple[str, bytes]]:
        try:
            entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
        except OSError as error:
            raise ValueError("unsafe Portable source file") from error
        for entry in entries:
            if not parts and entry.name == ".open-brain":
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ValueError("unsafe Portable source file") from error
            if stat.S_ISLNK(metadata.st_mode) or not (
                stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
            ):
                raise ValueError("unsafe Portable source file")
            if stat.S_ISDIR(metadata.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                    current = os.fstat(child_fd)
                except OSError as error:
                    raise ValueError("unsafe Portable source file") from error
                try:
                    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise ValueError("unsafe Portable source file")
                    yield from self._portable_files_from_descriptor(child_fd, (*parts, entry.name))
                finally:
                    os.close(child_fd)
                continue
            if metadata.st_nlink != 1:
                raise ValueError("unsafe Portable source file")
            try:
                file_fd = _open_portable_file(directory_fd, entry.name)
                current = os.fstat(file_fd)
            except OSError as error:
                raise ValueError("unsafe Portable source file") from error
            try:
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
                ):
                    raise ValueError("unsafe Portable source file")
                chunks: list[bytes] = []
                while chunk := os.read(file_fd, 64 * 1024):
                    chunks.append(chunk)
                after = os.fstat(file_fd)
                payload = b"".join(chunks)
                if (
                    (
                        after.st_dev,
                        after.st_ino,
                        after.st_nlink,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    )
                    != (
                        current.st_dev,
                        current.st_ino,
                        current.st_nlink,
                        current.st_size,
                        current.st_mtime_ns,
                        current.st_ctime_ns,
                    )
                    or len(payload) != current.st_size
                ):
                    raise ValueError("unsafe Portable source file")
                relative = "/".join((*parts, entry.name))
                _safe_relative(relative)
                yield relative, payload
            except OSError as error:
                raise ValueError("unsafe Portable source file") from error
            finally:
                os.close(file_fd)

    def _family(self, prefix: str) -> Iterator[tuple[str, bytes]]:
        for relative, payload in self.portable_files():
            if relative.startswith(prefix):
                yield relative, payload


class LocalSourceRecords(LocalTenantStorage):
    def records(self) -> Iterable[tuple[str, bytes]]:
        return self._family("sources/captures/")

    def put_capture(self, payload: bytes) -> WriteState:
        relative = _record_relative(
            payload,
            tenant_id=self.tenant_id,
            label="capture",
            directory="sources/captures",
            identifier_key="capture_id",
            identifier_prefix="capture",
            timestamp_key="accepted_at",
        )
        _validate_write(relative, payload, self.tenant_id)
        return _immutable_put(
            self.root, self.bound_root_identity, relative, payload, "capture"
        )


class LocalBatches(LocalTenantStorage):
    def batches(self) -> Iterable[tuple[str, bytes]]:
        return self._family("sources/batches/")

    def put_batch(self, payload: bytes) -> WriteState:
        records = _canonical_jsonl_objects(payload, "batch")
        first = records[0]
        if first.get("tenant_id") != self.tenant_id:
            raise ValueError("invalid Portable batch tenant")
        batch_id = _identifier(first.get("batch_id"), "batch", "batch")
        partition = _date_partition(first.get("recorded_at"), "batch")
        for record in records:
            if (
                record.get("tenant_id") != self.tenant_id
                or _identifier(record.get("batch_id"), "batch", "batch") != batch_id
                or _date_partition(record.get("recorded_at"), "batch") != partition
                or not isinstance(record.get("record_id"), str)
                or not record["record_id"]
            ):
                raise ValueError("invalid Portable batch")
        relative = f"sources/batches/{partition}/{batch_id}.jsonl"
        _validate_write(relative, payload, self.tenant_id)
        return _immutable_put(self.root, self.bound_root_identity, relative, payload, "batch")


class LocalBlobs(LocalTenantStorage):
    def blobs(self) -> Iterable[tuple[str, bytes]]:
        return self._family("sources/blobs/")

    def put_blob(self, payload: bytes) -> WriteState:
        if not isinstance(payload, bytes):
            raise ValueError("invalid Portable blob")
        digest = sha256(payload).hexdigest()
        relative = f"sources/blobs/sha256/{digest[:2]}/{digest}"
        _validate_write(relative, payload, self.tenant_id)
        return _immutable_put(self.root, self.bound_root_identity, relative, payload, "blob")


class LocalPortableHistory(LocalTenantStorage):
    def history(self) -> Iterable[tuple[str, bytes]]:
        return self._family("history/")

    def put_history(self, family: str, payload: bytes) -> WriteState:
        specifications = {
            "proposal": ("proposals", "proposal_id", "proposal"),
            "decision": ("decisions", "decision_id", "decision"),
            "publication": ("publications", "publication_id", "publication"),
            "action": ("actions", "action_id", "action"),
            "routing": ("routes", "route_id", "route"),
            "review_binding": ("review-bindings", "proposal_id", "proposal"),
        }
        specification = specifications.get(family)
        if specification is None:
            raise ValueError("invalid Portable history family")
        directory, identifier_key, identifier_prefix = specification
        relative = _record_relative(
            payload,
            tenant_id=self.tenant_id,
            label=family,
            directory=f"history/{directory}",
            identifier_key=identifier_key,
            identifier_prefix=identifier_prefix,
            timestamp_key="recorded_at",
        )
        if family == "review_binding":
            binding = validate_review_binding(_canonical_json_object(payload, family))
            if binding["tenant_id"] != self.tenant_id:
                raise ValueError("invalid Portable review binding tenant")
        else:
            _validate_write(relative, payload, self.tenant_id)
        return _immutable_put(self.root, self.bound_root_identity, relative, payload, family)


@dataclass(frozen=True, slots=True)
class LocalPortableWrites:
    """Typed, conflict-safe Portable persistence for local engine writers."""

    root: Path
    tenant_id: str
    root_identity: RootIdentity | None = None

    def __post_init__(self) -> None:
        storage = LocalTenantStorage(
            root=self.root,
            tenant_id=self.tenant_id,
            root_identity=self.root_identity,
        )
        object.__setattr__(self, "root_identity", storage.bound_root_identity)

    @property
    def bound_root_identity(self) -> RootIdentity:
        identity = self.root_identity
        assert identity is not None
        return identity

    def put_capture(self, payload: bytes) -> WriteState:
        return LocalSourceRecords(
            root=self.root,
            tenant_id=self.tenant_id,
            root_identity=self.bound_root_identity,
        ).put_capture(payload)

    def replace_capture(self, payload: bytes) -> WriteState:
        return self.put_capture(payload)

    def put_blob(self, payload: bytes) -> WriteState:
        return LocalBlobs(
            root=self.root,
            tenant_id=self.tenant_id,
            root_identity=self.bound_root_identity,
        ).put_blob(payload)

    def put_history(self, family: str, payload: bytes) -> WriteState:
        return LocalPortableHistory(
            root=self.root,
            tenant_id=self.tenant_id,
            root_identity=self.bound_root_identity,
        ).put_history(family, payload)

    def put_page(self, relative: str, payload: bytes) -> WriteState:
        _safe_relative(relative)
        fields = _canonical_markdown(payload, "page")
        if fields.get("tenant_id") != self.tenant_id:
            raise ValueError("invalid Portable page tenant")
        page_id = _identifier(fields.get("page_id"), "page", "page")
        parts = PurePosixPath(relative).parts
        if (
            len(parts) != 5
            or parts[:2] != ("content", "spaces")
            or _SLUG.fullmatch(parts[2]) is None
            or parts[3] != "notes"
            or parts[4] != f"{page_id}.md"
        ):
            raise ValueError("invalid Portable page path")
        _validate_write(relative, payload, self.tenant_id)
        return _immutable_put(
            self.root, self.bound_root_identity, relative, payload, "page"
        )

    def replace_page(
        self, relative: str, payload: bytes, *, expected_sha256: str
    ) -> WriteState:
        _safe_relative(relative)
        if not isinstance(expected_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ) is None:
            raise ValueError("invalid expected page digest")
        fields = _canonical_markdown(payload, "page")
        if fields.get("tenant_id") != self.tenant_id:
            raise ValueError("invalid Portable page tenant")
        page_id = _identifier(fields.get("page_id"), "page", "page")
        parts = PurePosixPath(relative).parts
        if (
            len(parts) != 5
            or parts[:2] != ("content", "spaces")
            or _SLUG.fullmatch(parts[2]) is None
            or parts[3] != "notes"
            or parts[4] != f"{page_id}.md"
        ):
            raise ValueError("invalid Portable page path")
        _validate_write(relative, payload, self.tenant_id)
        current = read_confined(
            root=self.root,
            relative=relative,
            expected_root_identity=self.bound_root_identity,
        )
        if current is None:
            raise ValueError("canonical page revision conflict")
        if current == payload:
            return WriteState.ALREADY_EXISTS
        try:
            atomic_replace(
                root=self.root,
                relative=relative,
                data=payload,
                require_existing=True,
                expected_existing_sha256=expected_sha256,
                expected_root_identity=self.bound_root_identity,
            )
        except DuplicateConflictError as error:
            raise ValueError("canonical page revision conflict") from error
        return WriteState.CREATED

    def put_space(self, payload: bytes, *, replace: bool) -> WriteState:
        if type(replace) is not bool:
            raise ValueError("invalid Portable space write")
        fields = _canonical_markdown(payload, "space")
        if fields.get("tenant_id") != self.tenant_id:
            raise ValueError("invalid Portable space tenant")
        slug = fields.get("slug")
        _identifier(fields.get("space_id"), "space", "space")
        if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
            raise ValueError("invalid Portable space")
        relative = f"content/spaces/{slug}/_space.md"
        _validate_write(relative, payload, self.tenant_id)
        if replace:
            atomic_replace(
                root=self.root,
                relative=relative,
                data=payload,
                expected_root_identity=self.bound_root_identity,
            )
            return WriteState.CREATED
        return _immutable_put(
            self.root, self.bound_root_identity, relative, payload, "space"
        )


def portable_write_port(engine: object) -> PortableWritePort:
    """Return the explicit writer dependency installed by the portability task set."""
    port = getattr(engine, "_portable_writes", None)
    if port is None:
        raise RuntimeError("Portable write port is unavailable")
    return cast(PortableWritePort, port)


@dataclass(frozen=True, slots=True)
class LocalContentProtection:
    tenant_id: str

    def declaration(self) -> ContentProtectionDeclaration:
        return ContentProtectionDeclaration(scheme="none", encrypted=False)


def local_portability_ports(
    root: Path,
    tenant_id: str,
    root_identity: RootIdentity | None = None,
) -> tuple[
    SourceRecordPort,
    BatchPort,
    BlobPort,
    PortableHistoryPort,
    TenantStoragePort,
    ContentProtectionPort,
]:
    storage = LocalTenantStorage(
        root=root,
        tenant_id=tenant_id,
        root_identity=root_identity,
    )
    bound_identity = storage.bound_root_identity
    return cast(
        tuple[
            SourceRecordPort,
            BatchPort,
            BlobPort,
            PortableHistoryPort,
            TenantStoragePort,
            ContentProtectionPort,
        ],
        (
            LocalSourceRecords(root=root, tenant_id=tenant_id, root_identity=bound_identity),
            LocalBatches(root=root, tenant_id=tenant_id, root_identity=bound_identity),
            LocalBlobs(root=root, tenant_id=tenant_id, root_identity=bound_identity),
            LocalPortableHistory(root=root, tenant_id=tenant_id, root_identity=bound_identity),
            storage,
            LocalContentProtection(tenant_id=tenant_id),
        ),
    )
