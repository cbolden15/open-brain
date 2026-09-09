"""Descriptor-confined discovery and stable reads for Markdown import."""

from __future__ import annotations

import errno
import os
import stat
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from open_brain_engine.storage.filesystem import RootIdentity

MAX_VISITED_ENTRIES = 100_000
MAX_SELECTED_FILES = 10_000
MAX_AGGREGATE_BYTES = 536_870_912
MAX_FILE_BYTES = 1_048_576
MAX_ENCODED_RELATIVE_PATH = 65_456

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class ImportDirectoryUnavailable(RuntimeError):
    """The selected root could not be pinned without following child links."""


class ImportScanIncomplete(RuntimeError):
    """Traversal could not prove that it observed the complete selected tree."""


class ImportInterrupted(RuntimeError):
    """The caller requested interruption at an enumeration safe point."""


class ScanLimitExceeded(RuntimeError):
    def __init__(
        self,
        exceeded: tuple[str, ...],
        visited_entries: int,
        selected_files: int,
        aggregate_bytes: int,
    ) -> None:
        super().__init__(*exceeded)
        self.exceeded = exceeded
        self.visited_entries = visited_entries
        self.selected_files = selected_files
        self.aggregate_bytes = aggregate_bytes


@dataclass(frozen=True, slots=True)
class RootSnapshot:
    canonical_path: Path
    identity: RootIdentity
    ancestor_identities: tuple[RootIdentity, ...]


@dataclass(slots=True)
class PinnedImportRoot:
    descriptor: int
    snapshot: RootSnapshot

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> PinnedImportRoot:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class ScanLimits:
    visited_entries: int = MAX_VISITED_ENTRIES
    selected_files: int = MAX_SELECTED_FILES
    aggregate_bytes: int = MAX_AGGREGATE_BYTES
    file_bytes: int = MAX_FILE_BYTES

    def __post_init__(self) -> None:
        for value in (
            self.visited_entries,
            self.selected_files,
            self.aggregate_bytes,
            self.file_bytes,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("invalid Markdown scan limit")


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> FileSnapshot:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            link_count=value.st_nlink,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class MarkdownCandidate:
    raw_parts: tuple[bytes, ...]
    relative_path: str
    parent_identities: tuple[RootIdentity, ...]
    observed: FileSnapshot


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    path: str
    reason: str
    raw_sort_key: bytes
    failed: bool


@dataclass(frozen=True, slots=True)
class ScanInventory:
    candidates: tuple[MarkdownCandidate, ...]
    outcomes: tuple[ScanOutcome, ...]
    visited_entries: int
    selected_files: int
    aggregate_bytes: int


@dataclass(slots=True)
class _Frame:
    descriptor: int
    raw_parts: tuple[bytes, ...]
    normalized_parts: tuple[str, ...]
    parent_identities: tuple[RootIdentity, ...]
    entries: tuple[bytes, ...]
    position: int = 0
    normalized_directories: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _ScanState:
    discovered_entries: int = 0
    visited_entries: int = 0
    selected_files: int = 0
    aggregate_bytes: int = 0


def pin_import_root(argument: str) -> PinnedImportRoot:
    """Resolve and independently descriptor-open an import root twice."""
    _validate_raw_argument(argument)
    first_fd = -1
    second_fd = -1
    try:
        first_fd, first = _resolve_and_open(argument)
        second_fd, second = _resolve_and_open(argument)
        if first != second:
            raise ImportDirectoryUnavailable
        os.close(second_fd)
        second_fd = -1
        return PinnedImportRoot(first_fd, first)
    except ImportDirectoryUnavailable:
        if first_fd >= 0:
            os.close(first_fd)
        if second_fd >= 0:
            os.close(second_fd)
        raise
    except OSError, RuntimeError, UnicodeError, ValueError:
        if first_fd >= 0:
            os.close(first_fd)
        if second_fd >= 0:
            os.close(second_fd)
        raise ImportDirectoryUnavailable from None


def snapshot_directory(path: Path) -> RootSnapshot:
    """Descriptor-open one canonical operational path and return its ancestry."""
    try:
        if not path.is_absolute() or path == Path(path.anchor):
            raise ValueError
        descriptor, snapshot = _open_canonical(path)
        os.close(descriptor)
        return snapshot
    except OSError, RuntimeError, UnicodeError, ValueError:
        raise ImportDirectoryUnavailable from None


def revalidate_import_root(snapshot: RootSnapshot) -> None:
    current = snapshot_directory(snapshot.canonical_path)
    if current != snapshot:
        raise ImportDirectoryUnavailable


def roots_overlap(left: RootSnapshot, right: RootSnapshot) -> bool:
    return left.identity in right.ancestor_identities or right.identity in left.ancestor_identities


def enumerate_markdown(
    root_fd: int,
    *,
    limits: ScanLimits,
    allow_large_vault: bool,
    interrupted: Callable[[], bool],
    progress: Callable[[int], None] | None = None,
) -> ScanInventory:
    """Enumerate a complete tree without reading selected file content."""
    if type(allow_large_vault) is not bool or not callable(interrupted):
        raise ValueError("invalid Markdown scan request")
    root_copy = os.dup(root_fd)
    state = _ScanState()
    try:
        root_entries = _directory_entries(
            root_copy,
            state=state,
            limits=limits,
            allow_large_vault=allow_large_vault,
            interrupted=interrupted,
        )
        stack = [
            _Frame(
                descriptor=root_copy,
                raw_parts=(),
                normalized_parts=(),
                parent_identities=(),
                entries=root_entries,
            )
        ]
        root_copy = -1
        candidates: list[MarkdownCandidate] = []
        outcomes: list[tuple[str | None, str, bytes, bool]] = []
        collision_members: dict[str, list[bytes]] = defaultdict(list)
        while stack:
            frame = stack[-1]
            if frame.position >= len(frame.entries):
                os.close(frame.descriptor)
                stack.pop()
                continue
            raw_name = frame.entries[frame.position]
            frame.position += 1
            try:
                metadata = os.stat(raw_name, dir_fd=frame.descriptor, follow_symlinks=False)
            except OSError:
                raise ImportScanIncomplete from None
            raw_parts = (*frame.raw_parts, raw_name)
            raw_key = b"/".join(raw_parts)
            normalized = _normalized_component(raw_name)
            normalized_path = (
                None if normalized is None else "/".join((*frame.normalized_parts, normalized))
            )
            mode = metadata.st_mode
            if (
                not stat.S_ISDIR(mode)
                and raw_name.endswith(b".md")
                and normalized_path is not None
            ):
                collision_members[normalized_path].append(raw_key)
            if stat.S_ISDIR(mode):
                if normalized is None:
                    outcomes.append((None, "invalid_path", raw_key, True))
                elif normalized in frame.normalized_directories:
                    raise ImportScanIncomplete
                elif normalized.startswith("."):
                    frame.normalized_directories.add(normalized)
                    outcomes.append((normalized_path, "dot_directory", raw_key, False))
                else:
                    frame.normalized_directories.add(normalized)
                    try:
                        child_fd = os.open(raw_name, _DIRECTORY_FLAGS, dir_fd=frame.descriptor)
                        opened = os.fstat(child_fd)
                    except OSError:
                        raise ImportScanIncomplete from None
                    if _identity(opened) != _identity(metadata):
                        os.close(child_fd)
                        raise ImportScanIncomplete
                    try:
                        child_entries = _directory_entries(
                            child_fd,
                            state=state,
                            limits=limits,
                            allow_large_vault=allow_large_vault,
                            interrupted=interrupted,
                        )
                    except BaseException:
                        os.close(child_fd)
                        raise
                    stack.append(
                        _Frame(
                            descriptor=child_fd,
                            raw_parts=raw_parts,
                            normalized_parts=(*frame.normalized_parts, normalized),
                            parent_identities=(*frame.parent_identities, _identity(opened)),
                            entries=child_entries,
                        )
                    )
            elif stat.S_ISLNK(mode):
                outcomes.append((normalized_path, "symlink", raw_key, False))
            elif stat.S_ISREG(mode):
                if not raw_name.endswith(b".md"):
                    outcomes.append((normalized_path, "non_markdown", raw_key, False))
                elif metadata.st_nlink != 1:
                    outcomes.append((normalized_path, "hardlink", raw_key, False))
                else:
                    state.selected_files += 1
                    state.aggregate_bytes += max(metadata.st_size, 0)
                    if normalized_path is None:
                        outcomes.append((None, "invalid_path", raw_key, True))
                    else:
                        candidates.append(
                            MarkdownCandidate(
                                raw_parts=raw_parts,
                                relative_path=normalized_path,
                                parent_identities=frame.parent_identities,
                                observed=FileSnapshot.from_stat(metadata),
                            )
                        )
            else:
                outcomes.append((normalized_path, "special_file", raw_key, False))
            state.visited_entries += 1
            _check_aggregate_limits(
                limits,
                allow_large_vault=allow_large_vault,
                visited=state.visited_entries,
                selected=state.selected_files,
                aggregate=state.aggregate_bytes,
            )
            if progress is not None and state.visited_entries % 10_000 == 0:
                progress(state.visited_entries)
            if interrupted():
                raise ImportInterrupted

        collisions = {
            path: raw_keys for path, raw_keys in collision_members.items() if len(raw_keys) > 1
        }
        if collisions:
            collision_keys = {
                (path, raw_key)
                for path, raw_keys in collisions.items()
                for raw_key in raw_keys
            }
            candidates = [
                candidate
                for candidate in candidates
                if (
                    candidate.relative_path,
                    b"/".join(candidate.raw_parts),
                )
                not in collision_keys
            ]
            outcomes = [
                outcome for outcome in outcomes if (outcome[0], outcome[2]) not in collision_keys
            ]
            outcomes.extend(
                (path, "path_collision", raw_key, True)
                for path, raw_keys in collisions.items()
                for raw_key in raw_keys
            )
        candidates, candidate_outcomes = _classify_candidates(
            candidates,
            maximum_bytes=limits.file_bytes,
        )
        outcomes.extend(candidate_outcomes)
        rendered = _render_outcomes(outcomes)
        return ScanInventory(
            candidates=tuple(sorted(candidates, key=lambda item: item.relative_path)),
            outcomes=rendered,
            visited_entries=state.visited_entries,
            selected_files=state.selected_files,
            aggregate_bytes=state.aggregate_bytes,
        )
    except ImportInterrupted, ImportScanIncomplete, ScanLimitExceeded:
        raise
    except OSError:
        raise ImportScanIncomplete from None
    finally:
        if root_copy >= 0:
            os.close(root_copy)
        if "stack" in locals():
            for frame in stack:
                with suppress(OSError):
                    os.close(frame.descriptor)


def read_markdown_candidate(
    root_fd: int,
    candidate: MarkdownCandidate,
    *,
    maximum_bytes: int = MAX_FILE_BYTES,
) -> bytes:
    """Read one candidate only if its descriptor metadata remains stable."""
    current_fd = os.dup(root_fd)
    file_fd = -1
    try:
        for index, raw_part in enumerate(candidate.raw_parts[:-1]):
            next_fd = os.open(raw_part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            observed = _identity(os.fstat(next_fd))
            if observed != candidate.parent_identities[index]:
                os.close(next_fd)
                raise ValueError("file_changed")
            os.close(current_fd)
            current_fd = next_fd
        try:
            file_fd = os.open(candidate.raw_parts[-1], _FILE_FLAGS, dir_fd=current_fd)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
                raise ValueError("file_changed") from None
            raise ValueError("unreadable") from None
        before_stat = os.fstat(file_fd)
        before = FileSnapshot.from_stat(before_stat)
        if not stat.S_ISREG(before.mode) or before.link_count != 1 or before != candidate.observed:
            raise ValueError("file_changed")
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = os.read(file_fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum_bytes:
                    raise ValueError("file_changed")
        except ValueError:
            raise
        except OSError:
            raise ValueError("unreadable") from None
        payload = b"".join(chunks)
        after = FileSnapshot.from_stat(os.fstat(file_fd))
        if after != before or len(payload) != before.size:
            raise ValueError("file_changed")
        return payload
    except ImportScanIncomplete:
        raise
    except IndexError, OSError:
        raise ValueError("file_changed") from None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(current_fd)


def _validate_raw_argument(argument: str) -> None:
    if not isinstance(argument, str) or not argument or "\x00" in argument:
        raise ImportDirectoryUnavailable
    try:
        argument.encode("utf-8")
    except UnicodeEncodeError:
        raise ImportDirectoryUnavailable from None
    if not argument.startswith("/"):
        raise ImportDirectoryUnavailable
    components = argument.split("/")[1:]
    if any(component in {".", "..", "~"} for component in components):
        raise ImportDirectoryUnavailable
    if Path(argument) == Path("/"):
        raise ImportDirectoryUnavailable


def _resolve_and_open(argument: str) -> tuple[int, RootSnapshot]:
    try:
        canonical = Path(argument).resolve(strict=True)
    except OSError, RuntimeError:
        raise ImportDirectoryUnavailable from None
    return _open_canonical(canonical)


def _open_canonical(canonical: Path) -> tuple[int, RootSnapshot]:
    descriptor = os.open(b"/", _DIRECTORY_FLAGS)
    identities = [_identity(os.fstat(descriptor))]
    try:
        for component in canonical.parts[1:]:
            raw_component = component.encode("utf-8")
            next_fd = os.open(raw_component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
            identities.append(_identity(os.fstat(descriptor)))
        snapshot = RootSnapshot(
            canonical_path=canonical,
            identity=identities[-1],
            ancestor_identities=tuple(identities),
        )
        return descriptor, snapshot
    except BaseException:
        os.close(descriptor)
        raise


def _directory_entries(
    descriptor: int,
    *,
    state: _ScanState,
    limits: ScanLimits,
    allow_large_vault: bool,
    interrupted: Callable[[], bool],
) -> tuple[bytes, ...]:
    try:
        raw_names: list[bytes] = []
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                if interrupted():
                    raise ImportInterrupted
                raw_name = os.fsencode(entry.name)
                state.discovered_entries += 1
                if (
                    not allow_large_vault
                    and state.discovered_entries > limits.visited_entries
                ):
                    raise ScanLimitExceeded(
                        ("visited_entries",),
                        state.discovered_entries,
                        state.selected_files,
                        state.aggregate_bytes,
                    )
                raw_names.append(raw_name)
        return tuple(sorted(raw_names))
    except OSError:
        raise ImportScanIncomplete from None


def _normalized_component(raw: bytes) -> str | None:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized in {".", ".."}
        or any(
            character in {"\x00", "/", "\\"}
            or ord(character) <= 0x1F
            or 0x7F <= ord(character) <= 0x9F
            or unicodedata.category(character) == "Cf"
            for character in normalized
        )
    ):
        return None
    return normalized


def _identity(value: os.stat_result) -> RootIdentity:
    return (value.st_dev, value.st_ino)


def _check_aggregate_limits(
    limits: ScanLimits,
    *,
    allow_large_vault: bool,
    visited: int,
    selected: int,
    aggregate: int,
) -> None:
    if allow_large_vault:
        return
    exceeded = tuple(
        name
        for name, observed, maximum in (
            ("aggregate_bytes", aggregate, limits.aggregate_bytes),
            ("selected_markdown_files", selected, limits.selected_files),
            ("visited_entries", visited, limits.visited_entries),
        )
        if observed > maximum
    )
    if exceeded:
        raise ScanLimitExceeded(exceeded, visited, selected, aggregate)


def _classify_candidates(
    candidates: list[MarkdownCandidate],
    *,
    maximum_bytes: int,
) -> tuple[list[MarkdownCandidate], list[tuple[str | None, str, bytes, bool]]]:
    grouped: dict[str, list[MarkdownCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.relative_path].append(candidate)
    retained: list[MarkdownCandidate] = []
    outcomes: list[tuple[str | None, str, bytes, bool]] = []
    for path, values in grouped.items():
        if len(values) > 1:
            outcomes.extend(
                (path, "path_collision", b"/".join(value.raw_parts), True) for value in values
            )
            continue
        candidate = values[0]
        raw_key = b"/".join(candidate.raw_parts)
        if len(quote(path, safe="/")) > MAX_ENCODED_RELATIVE_PATH:
            outcomes.append((path, "invalid_path", raw_key, True))
        elif candidate.observed.size > maximum_bytes:
            outcomes.append((path, "file_too_large", raw_key, True))
        else:
            retained.append(candidate)
    return retained, outcomes


def _render_outcomes(
    outcomes: list[tuple[str | None, str, bytes, bool]],
) -> tuple[ScanOutcome, ...]:
    valid = sorted(
        (item for item in outcomes if item[0] is not None),
        key=lambda item: (str(item[0]), item[2], item[1]),
    )
    invalid = sorted((item for item in outcomes if item[0] is None), key=lambda item: item[2])
    rendered = [
        ScanOutcome(path=str(path), reason=reason, raw_sort_key=raw, failed=failed)
        for path, reason, raw, failed in valid
    ]
    rendered.extend(
        ScanOutcome(
            path=f"<invalid-path-{index:06d}>",
            reason=reason,
            raw_sort_key=raw,
            failed=failed,
        )
        for index, (_path, reason, raw, failed) in enumerate(invalid, start=1)
    )
    return tuple(rendered)


__all__ = [
    "enumerate_markdown",
    "ImportDirectoryUnavailable",
    "ImportInterrupted",
    "ImportScanIncomplete",
    "MarkdownCandidate",
    "MAX_AGGREGATE_BYTES",
    "MAX_FILE_BYTES",
    "MAX_ENCODED_RELATIVE_PATH",
    "MAX_SELECTED_FILES",
    "MAX_VISITED_ENTRIES",
    "PinnedImportRoot",
    "pin_import_root",
    "read_markdown_candidate",
    "revalidate_import_root",
    "roots_overlap",
    "RootSnapshot",
    "ScanInventory",
    "ScanLimits",
    "ScanLimitExceeded",
    "snapshot_directory",
]
