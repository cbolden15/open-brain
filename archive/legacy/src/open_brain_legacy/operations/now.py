from __future__ import annotations

import os
import re
import secrets
import stat
import unicodedata
from contextlib import AbstractContextManager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import LockScope

from .models import DeploymentTarget, HostRole


class NowError(RuntimeError):
    """A NOW projection operation failed without exposing source content."""


class NowOwnershipError(NowError):
    """A non-canonical target attempted to generate the shared projection."""


class NowRootError(NowError):
    """An injected NOW output root is unsafe or shared by multiple targets."""


class NowLease(Protocol):
    def acquire(self, scope: LockScope) -> AbstractContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class NowRoots:
    canonical_output_root: Path
    edge_output_root: Path
    ingress_output_root: Path
    output_name: str = "NOW.md"

    def __post_init__(self) -> None:
        roots = (
            self.canonical_output_root,
            self.edge_output_root,
            self.ingress_output_root,
        )
        resolved = tuple(_validate_root(root) for root in roots)
        if len(set(resolved)) != len(resolved):
            raise NowRootError("NOW output roots must be distinct")
        if (
            not isinstance(self.output_name, str)
            or not self.output_name
            or self.output_name in {".", ".."}
            or "/" in self.output_name
            or "\\" in self.output_name
            or "\x00" in self.output_name
        ):
            raise NowRootError("invalid NOW output name")


@dataclass(frozen=True, slots=True)
class NowItem:
    title: str
    source_ref: str
    priority: int
    privacy_tier: PrivacyTier

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _validate_line(self.title, field="title"))
        object.__setattr__(
            self,
            "source_ref",
            _validate_line(self.source_ref, field="source reference", forbid_backtick=True),
        )
        if (
            not isinstance(self.priority, int)
            or isinstance(self.priority, bool)
            or not 1 <= self.priority <= 999
        ):
            raise NowError("invalid NOW item priority")
        if not isinstance(self.privacy_tier, PrivacyTier):
            raise NowError("invalid NOW item privacy tier")


@dataclass(frozen=True, slots=True)
class NowProjectionInput:
    focus: tuple[NowItem, ...]
    queue: tuple[NowItem, ...]
    life_os: tuple[NowItem, ...] | None
    messages: tuple[NowItem, ...] | None

    def __post_init__(self) -> None:
        sections = (self.focus, self.queue, self.life_os, self.messages)
        if any(
            section is not None
            and (
                not isinstance(section, tuple)
                or any(not isinstance(item, NowItem) for item in section)
            )
            for section in sections
        ):
            raise NowError("invalid NOW projection input")


@dataclass(frozen=True, slots=True)
class NowBuildResult:
    generation_id: str
    output_path: Path
    work_item_count: int
    filtered_item_count: int


@dataclass(frozen=True, slots=True)
class NowCheckResult:
    available: bool
    generation_id: str | None
    marker_valid: bool
    output_path: Path


_MARKER_PATTERN = re.compile(
    r"\A# NOW\n\n<!-- open-brain-now-generation:(now_[0-9a-f]{64}) -->\n\n"
)


def build_now(
    *,
    target: DeploymentTarget,
    host_role: HostRole,
    roots: NowRoots,
    lease: NowLease,
    projection: NowProjectionInput,
) -> NowBuildResult:
    if target is not DeploymentTarget.CANONICAL_WRITER or host_role is not HostRole.WRITER:
        raise NowOwnershipError("NOW builds require the canonical writer target and role")
    if not isinstance(roots, NowRoots):
        raise NowRootError("invalid NOW roots")
    if not isinstance(projection, NowProjectionInput):
        raise NowError("invalid NOW projection input")

    with lease.acquire(LockScope.SHARED_WRITER):
        body, work_item_count, filtered_item_count = _render_body(projection)
        generation_id = "now_" + sha256(body.encode("utf-8")).hexdigest()
        payload = body.replace(
            "# NOW\n\n",
            f"# NOW\n\n<!-- open-brain-now-generation:{generation_id} -->\n\n",
            1,
        ).encode("utf-8")
        output_path = roots.canonical_output_root / roots.output_name
        _ensure_safe_existing_output(output_path)
        temp_path = roots.canonical_output_root / (
            f".{roots.output_name}.{secrets.token_hex(16)}.tmp"
        )
        try:
            _write_temporary(temp_path, payload)
            _replace_file(temp_path, output_path)
            _fsync_directory(roots.canonical_output_root)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise NowError("temporary NOW cleanup failed") from None

    return NowBuildResult(
        generation_id=generation_id,
        output_path=output_path,
        work_item_count=work_item_count,
        filtered_item_count=filtered_item_count,
    )


def check_now(*, target: DeploymentTarget, roots: NowRoots) -> NowCheckResult:
    if not isinstance(target, DeploymentTarget):
        raise NowOwnershipError("invalid NOW check target")
    if not isinstance(roots, NowRoots):
        raise NowRootError("invalid NOW roots")
    output_root = {
        DeploymentTarget.CANONICAL_WRITER: roots.canonical_output_root,
        DeploymentTarget.EDGE_OPERATOR: roots.edge_output_root,
        DeploymentTarget.INGRESS_NODE: roots.ingress_output_root,
    }[target]
    output_path = output_root / roots.output_name
    _ensure_safe_existing_output(output_path)
    if not output_path.exists():
        return NowCheckResult(False, None, False, output_path)
    try:
        payload = output_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise NowError("NOW projection check failed") from None
    marker = _MARKER_PATTERN.match(payload)
    if marker is None:
        return NowCheckResult(True, None, False, output_path)
    generation_id = marker.group(1)
    body = "# NOW\n\n" + payload[marker.end() :]
    marker_valid = generation_id == "now_" + sha256(body.encode("utf-8")).hexdigest()
    return NowCheckResult(True, generation_id, marker_valid, output_path)


def _validate_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise NowRootError("NOW output roots must be safe directories")
    try:
        metadata = os.lstat(root)
    except OSError:
        raise NowRootError("NOW output roots must be safe directories") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise NowRootError("NOW output roots must be safe directories")
    return root.resolve(strict=True)


def _validate_line(value: object, *, field: str, forbid_backtick: bool = False) -> str:
    if not isinstance(value, str):
        raise NowError(f"invalid NOW item {field}")
    normalized = unicodedata.normalize("NFC", value)
    forbidden = ("\x00", "\r", "\n", "\u0085", "\u2028", "\u2029")
    if (
        not normalized
        or normalized.isspace()
        or len(normalized) > 1000
        or any(marker in normalized for marker in forbidden)
        or (forbid_backtick and "`" in normalized)
    ):
        raise NowError(f"invalid NOW item {field}")
    return normalized


def _render_body(projection: NowProjectionInput) -> tuple[str, int, int]:
    sections = (projection.focus, projection.queue, projection.life_os, projection.messages)
    work_item_count = sum(
        1
        for section in sections
        if section is not None
        for item in section
        if item.privacy_tier is PrivacyTier.WORK
    )
    filtered_item_count = sum(
        1
        for section in sections
        if section is not None
        for item in section
        if item.privacy_tier is not PrivacyTier.WORK
    )
    queue_count = sum(
        1 for item in projection.queue if item.privacy_tier is PrivacyTier.WORK
    )
    queue_label = "work item" if queue_count == 1 else "work items"
    parts = (
        "# NOW",
        f"Queue pressure: {queue_count} {queue_label}.",
        _render_section("Focus", projection.focus),
        _render_section("Queue", projection.queue),
        _render_section("LifeOS", projection.life_os),
        _render_section("Messages", projection.messages),
    )
    return "\n\n".join(parts) + "\n", work_item_count, filtered_item_count


def _render_section(name: str, items: tuple[NowItem, ...] | None) -> str:
    if items is None:
        content = "_Unavailable: optional input not configured._"
    else:
        work_items = sorted(
            (item for item in items if item.privacy_tier is PrivacyTier.WORK),
            key=lambda item: (item.priority, item.title, item.source_ref),
        )
        content = (
            "\n".join(
                f"- [P{item.priority}] {item.title} (`{item.source_ref}`)"
                for item in work_items
            )
            if work_items
            else "_No work items._"
        )
    return f"## {name}\n\n{content}"


def _ensure_safe_existing_output(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise NowRootError("unsafe NOW output path") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise NowRootError("unsafe NOW output path")


def _write_temporary(path: Path, payload: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short NOW write")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError:
        raise NowError("durable NOW write failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _replace_file(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise NowError("NOW directory durability failed") from None
