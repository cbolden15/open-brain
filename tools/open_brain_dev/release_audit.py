"""Fail-closed checks for public Open Brain source trees and release archives."""

from __future__ import annotations

import argparse
import re
import stat
import sys
import tarfile
import unicodedata
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REQUIRED_FILES = ("LICENSE", "NOTICE")
FORBIDDEN_SUFFIXES = {
    ".db",
    ".env",
    ".key",
    ".log",
    ".pem",
    ".sqlite",
    ".sqlite3",
    ".token",
}
FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "captures",
    "content",
    "media",
    "private",
    "runtime",
    "state",
    "transcripts",
    "vault",
}
TREE_SKIP_PARTS = {
    ".git",
    ".codegraph",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
ABSOLUTE_HOME_RE = re.compile(rb"/(?:Users|home)/[A-Za-z0-9._-]+/")
PRIVATE_IPV4_RE = re.compile(
    rb"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    rb"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    rb"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret)"
    rb"\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{8,}"
)
TEXT_SCAN_LIMIT = 2 * 1024 * 1024
NO_ADDITIONAL_TERMS_MARKER = "# no additional project terms"
PORTABLE_FIXTURE_PATHS = (
    ("tests", "fixtures", "portable-brain", "v1", "brain-root"),
    ("open_brain", "portable", "conformance", "v1", "brain-root"),
    (
        "packages",
        "engine",
        "src",
        "open_brain_engine",
        "portable",
        "conformance",
        "v1",
        "brain-root",
    ),
    ("open_brain_engine", "portable", "conformance", "v1", "brain-root"),
    ("src", "open_brain_engine", "portable", "conformance", "v1", "brain-root"),
)
LEGACY_SYNTHETIC_VAULT_PREFIX = ("examples", "synthetic-vault")


@dataclass(frozen=True)
class Finding:
    """One redacted release-audit finding."""

    location: str
    rule: str


def _safe_location(value: str) -> str:
    return value.replace("\n", "?").replace("\r", "?")


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _is_portable_fixture(path: PurePosixPath) -> bool:
    lowered = tuple(part.lower() for part in path.parts)
    return any(
        lowered[index : index + len(fixture_path)] == fixture_path
        for fixture_path in PORTABLE_FIXTURE_PATHS
        for index in range(len(lowered) - len(fixture_path) + 1)
    )


def _is_legacy_synthetic_vault(path: PurePosixPath) -> bool:
    return path.parts[: len(LEGACY_SYNTHETIC_VAULT_PREFIX)] == LEGACY_SYNTHETIC_VAULT_PREFIX


def _path_rules(name: str) -> list[Finding]:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    findings: list[Finding] = []
    if path.is_absolute() or ".." in path.parts:
        findings.append(Finding(_safe_location(normalized), "unsafe-archive-path"))
    forbidden_parts = {part.lower() for part in path.parts} & FORBIDDEN_PARTS
    if _is_portable_fixture(path):
        forbidden_parts -= {"captures", "content"}
    if _is_legacy_synthetic_vault(path):
        forbidden_parts -= {"vault"}
    if forbidden_parts:
        findings.append(Finding(_safe_location(normalized), "forbidden-path-family"))
    lowered_name = path.name.lower()
    if any(lowered_name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        findings.append(Finding(_safe_location(normalized), "forbidden-file-type"))
    if normalized.startswith(("/Users/", "/home/")):
        findings.append(Finding(_safe_location(normalized), "absolute-home-path"))
    return findings


def _content_rules(location: str, data: bytes, deny_terms: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    if ABSOLUTE_HOME_RE.search(data):
        findings.append(Finding(location, "absolute-home-path"))
    if PRIVATE_IPV4_RE.search(data):
        findings.append(Finding(location, "private-ip-address"))
    if CREDENTIAL_ASSIGNMENT_RE.search(data):
        findings.append(Finding(location, "credential-assignment"))
    if len(data) > TEXT_SCAN_LIMIT:
        findings.append(Finding(location, "content-scan-limit-exceeded"))
        return findings
    normalized = _normalize_text(data.decode("utf-8", errors="surrogateescape"))
    if any(term in normalized for term in deny_terms):
        findings.append(Finding(location, "private-denylist-term"))
    return findings


def entry_rule_ids(name: str, data: bytes, deny_terms: Sequence[str]) -> tuple[str, ...]:
    """Return applicable rule IDs without retaining scanned content."""
    return tuple(
        finding.rule
        for finding in (*_path_rules(name), *_content_rules(_safe_location(name), data, deny_terms))
    )


def path_rule_ids(name: str) -> tuple[str, ...]:
    """Return path-only rule IDs for history scans."""
    return tuple(finding.rule for finding in _path_rules(name))


def content_rule_ids(data: bytes, deny_terms: Sequence[str]) -> tuple[str, ...]:
    """Return content-only rule IDs without retaining scanned bytes."""
    return tuple(finding.rule for finding in _content_rules("<redacted>", data, deny_terms))


def _load_denylist(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        raise ValueError("private denylist is required and must be a readable file")
    lines = tuple(
        _normalize_text(line.strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    terms = tuple(
        line for line in lines if not line.startswith("#")
    )
    if terms:
        return terms
    if lines == (NO_ADDITIONAL_TERMS_MARKER,):
        return ()
    raise ValueError(
        "private denylist must contain at least one non-comment term or the exact "
        f"{NO_ADDITIONAL_TERMS_MARKER!r} marker"
    )


def _iter_tree(root: Path) -> Iterable[tuple[str, bytes]]:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink() or not path.is_file() or set(relative.parts) & TREE_SKIP_PARTS:
            continue
        yield relative.as_posix(), path.read_bytes()


def _iter_archive(path: Path) -> Iterable[tuple[str, bytes | None]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    mode = (info.external_attr >> 16) & 0o170000
                    yield info.filename, None if mode == stat.S_IFLNK else archive.read(info)
        return
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    yield member.name, None
                if member.isfile():
                    handle = archive.extractfile(member)
                    if handle is not None:
                        yield member.name, handle.read()
        return
    raise ValueError(f"unsupported release artifact: {path.name}")


def audit(root: Path, denylist: Path, artifacts: Sequence[Path] = ()) -> list[Finding]:
    """Audit one source tree and any built artifacts."""
    if not root.is_dir():
        raise ValueError("root must be an existing directory")
    terms = _load_denylist(denylist)
    findings: list[Finding] = []

    for required in REQUIRED_FILES:
        if not (root / required).is_file():
            findings.append(Finding(required, "missing-required-file"))

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink() and not set(relative.parts) & TREE_SKIP_PARTS:
            findings.append(Finding(_safe_location(relative.as_posix()), "symlink-not-allowed"))

    for name, data in _iter_tree(root):
        findings.extend(_path_rules(name))
        findings.extend(_content_rules(name, data, terms))

    for artifact in artifacts:
        if not artifact.is_file():
            findings.append(Finding(artifact.name, "missing-release-artifact"))
            continue
        for name, archive_data in _iter_archive(artifact):
            location = f"{artifact.name}:{_safe_location(name)}"
            findings.extend(Finding(location, finding.rule) for finding in _path_rules(name))
            if archive_data is None:
                findings.append(Finding(location, "symlink-not-allowed"))
                continue
            findings.extend(_content_rules(location, archive_data, terms))

    return sorted(set(findings), key=lambda item: (item.location, item.rule))


def build_parser() -> argparse.ArgumentParser:
    """Build the release-audit CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--private-denylist", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, nargs="*", default=())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the release audit without exposing matching content."""
    args = build_parser().parse_args(argv)
    try:
        findings = audit(args.root.resolve(), args.private_denylist, args.artifacts)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"release audit error: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"{finding.location}: {finding.rule}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
