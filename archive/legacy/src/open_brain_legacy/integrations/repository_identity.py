"""Stable, path-free identities for work repositories."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlsplit

_SLUG_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SCP_ORIGIN = re.compile(r"^(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>[^?#]+)$")
_MAX_SLUG_LENGTH = 256
_GIT_TIMEOUT_SECONDS = 5.0


class RepositoryIdentityError(ValueError):
    """A repository cannot be represented by the work identity boundary."""


class RepositoryExcludedError(RepositoryIdentityError):
    """A repository matches an explicit work-project exclusion."""


class RepositoryIdentitySource(StrEnum):
    """The normalized source used to derive a stable repository slug."""

    ORIGIN = "origin"
    PROJECT_RELATIVE = "project_relative"
    BASENAME = "basename"


@dataclass(frozen=True, slots=True)
class StableRepoIdentity:
    """Opaque identity plus bounded work metadata; filesystem paths are omitted."""

    repository_id: str
    slug: str
    source: RepositoryIdentitySource

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"repo_[0-9a-f]{32}", self.repository_id) is None
            or not _is_safe_slug(self.slug)
            or not isinstance(self.source, RepositoryIdentitySource)
        ):
            raise ValueError("invalid repository identity")

    def to_dict(self) -> dict[str, str]:
        return {
            "repository_id": self.repository_id,
            "slug": self.slug,
            "source": self.source.value,
        }


class RepositoryIdentityResolver:
    """Resolve Git repositories without exposing host paths in results or errors."""

    def __init__(
        self,
        *,
        projects_root: Path,
        exclusions: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(projects_root, Path) or not isinstance(exclusions, tuple):
            raise ValueError("invalid repository identity configuration")
        if any(not isinstance(pattern, str) for pattern in exclusions):
            raise ValueError("invalid repository identity configuration")
        self._projects_root = projects_root.absolute()
        self._exclusions = tuple(
            pattern.strip()
            for pattern in exclusions
            if pattern.strip() and not pattern.lstrip().startswith("#")
        )

    def identify(self, path: Path) -> StableRepoIdentity:
        if not isinstance(path, Path):
            raise RepositoryIdentityError("repository unavailable")
        try:
            candidate = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise RepositoryIdentityError("repository unavailable") from None
        if not candidate.is_dir():
            raise RepositoryIdentityError("repository unavailable")

        worktree_root_value = _git(candidate, "rev-parse", "--show-toplevel")
        common_dir_value = _git(
            candidate,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
        if worktree_root_value is None or common_dir_value is None:
            raise RepositoryIdentityError("repository unavailable")

        worktree_root = Path(worktree_root_value).resolve()
        common_dir = Path(common_dir_value).resolve()
        main_root = common_dir.parent if common_dir.name == ".git" else worktree_root
        origin = _git(candidate, "config", "--get", "remote.origin.url")
        origin_parts = _normalized_origin(origin)

        if origin_parts is not None:
            host, slug = origin_parts
            source = RepositoryIdentitySource.ORIGIN
            identity_material = f"origin:{host}/{slug}"
        else:
            relative = _relative_slug(main_root, self._projects_root)
            if relative is not None:
                slug = relative
                source = RepositoryIdentitySource.PROJECT_RELATIVE
                identity_material = f"project:{relative}"
            else:
                slug = main_root.name
                source = RepositoryIdentitySource.BASENAME
                identity_material = f"fallback:{main_root}"

        if not _is_safe_slug(slug):
            raise RepositoryIdentityError("repository unavailable")
        if self._is_excluded(slug=slug, main_root=main_root, origin_slug=origin_parts):
            raise RepositoryExcludedError("repository excluded")

        repository_id = "repo_" + sha256(identity_material.encode("utf-8")).hexdigest()[:32]
        return StableRepoIdentity(
            repository_id=repository_id,
            slug=slug,
            source=source,
        )

    def _is_excluded(
        self,
        *,
        slug: str,
        main_root: Path,
        origin_slug: tuple[str, str] | None,
    ) -> bool:
        relative = _relative_slug(main_root, self._projects_root)
        candidates = {slug}
        if relative is not None:
            candidates.add(relative)
        for pattern in self._exclusions:
            if pattern.startswith("origin:"):
                expected = pattern.removeprefix("origin:")
                if origin_slug is not None and fnmatch.fnmatchcase(origin_slug[1], expected):
                    return True
            elif any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
                return True
        return False


def normalize_origin(origin: str | None) -> str | None:
    """Return a bounded repository slug for supported network Git origins."""
    normalized = _normalized_origin(origin)
    return normalized[1] if normalized is not None else None


def _normalized_origin(origin: str | None) -> tuple[str, str] | None:
    if not isinstance(origin, str) or not origin.strip():
        return None
    value = origin.strip()
    host: str | None
    raw_path: str
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname
        raw_path = parsed.path
    else:
        match = _SCP_ORIGIN.fullmatch(value)
        if match is None:
            return None
        host = match.group("host")
        raw_path = match.group("path")
    if host is None:
        return None

    path = unquote(raw_path).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segments = tuple(part for part in path.split("/") if part)
    slug = "/".join(segments)
    if len(segments) < 2 or not _is_safe_slug(slug):
        return None
    return host.lower(), slug


def _relative_slug(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return None
    value = relative.as_posix()
    return value if value != "." and _is_safe_slug(value) else None


def _is_safe_slug(value: str) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_SLUG_LENGTH
        and not value.startswith("/")
        and all(
            part not in {"", ".", ".."} and _SLUG_SEGMENT.fullmatch(part) is not None
            for part in value.split("/")
        )
    )


def _git(cwd: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-c", "core.askPass=", *args),
            cwd=cwd,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None
