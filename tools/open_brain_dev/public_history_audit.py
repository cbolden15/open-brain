"""Metadata-only audit for private-only material in tracked Git history."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from tools.open_brain_dev.release_audit import (
    _load_denylist,
    _safe_location,
    content_rule_ids,
    path_rule_ids,
)

GIT_TIMEOUT_SECONDS = 60
MAX_HISTORY_COMMITS = 10_000
MAX_HISTORY_BLOBS = 100_000
MAX_HISTORY_BYTES = 512 * 1024 * 1024
HISTORY_ALLOWLIST_PATH = PurePosixPath("release/public-history-allowlist.json")
MAX_HISTORY_ALLOWLIST_BYTES = 128 * 1024
MAX_HISTORY_ALLOWLIST_ENTRIES = 256
MAX_REVIEWED_DENYLIST_OCCURRENCES = 256
ALLOWLISTABLE_HISTORY_RULES = frozenset({"absolute-home-path", "private-ip-address"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_REASON_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,95}")


@dataclass(frozen=True)
class HistoryFinding:
    """One Git-history finding with no matched bytes or matching term."""

    commit: str
    path: str
    rule: str


@dataclass(frozen=True)
class _HistoryAllowance:
    blob_sha256: str
    path: str
    rule: str
    commit: str | None = None


def _string_mapping(value: object, *, error: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(error)
    return {str(key): item for key, item in value.items()}


def _denylist_fingerprint(terms: Sequence[str]) -> str:
    """Version-2 policy fingerprint of the scanner's normalized semantic term set."""
    payload = json.dumps(sorted(set(terms)), ensure_ascii=True, separators=(",", ":"))
    return sha256(payload.encode("ascii")).hexdigest()


def _load_history_allowlist(
    repository: Path, deny_terms: Sequence[str]
) -> frozenset[_HistoryAllowance]:
    path = repository.joinpath(*HISTORY_ALLOWLIST_PATH.parts)
    if not path.exists():
        return frozenset()
    if path.is_symlink() or not path.is_file():
        raise ValueError("history audit allowlist must be a regular file")
    payload = path.read_bytes()
    if len(payload) > MAX_HISTORY_ALLOWLIST_BYTES:
        raise ValueError("history audit allowlist size limit exceeded")
    try:
        raw_policy: object = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("history audit allowlist is invalid") from error
    policy = _string_mapping(raw_policy, error="history audit allowlist is invalid")
    policy_version = policy.get("policy_version")
    if (
        set(policy) != {"entries", "policy_version"}
        or type(policy_version) is not int
        or policy_version not in (1, 2)
    ):
        raise ValueError("history audit allowlist policy is invalid")
    raw_entries = policy["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_HISTORY_ALLOWLIST_ENTRIES:
        raise ValueError("history audit allowlist entries are invalid")

    entries: set[_HistoryAllowance] = set()
    reviewed_count = 0
    for raw_entry in raw_entries:
        entry = _string_mapping(raw_entry, error="history audit allowlist entry is invalid")
        private_rule = policy_version == 2 and entry.get("rule") == "private-denylist-term"
        required_keys = {"blob_sha256", "path", "reason", "rule"}
        if private_rule:
            required_keys |= {"reviewed_commits", "normalized_denylist_sha256"}
        if set(entry) != required_keys:
            raise ValueError("history audit allowlist entry is invalid")
        blob_digest = entry["blob_sha256"]
        raw_path = entry["path"]
        reason = entry["reason"]
        rule = entry["rule"]
        if not isinstance(blob_digest, str) or _SHA256_RE.fullmatch(blob_digest) is None:
            raise ValueError("history audit allowlist digest is invalid")
        if not isinstance(raw_path, str):
            raise ValueError("history audit allowlist path is invalid")
        normalized_path = raw_path.replace("\\", "/")
        parsed_path = PurePosixPath(normalized_path)
        if (
            raw_path != normalized_path
            or not parsed_path.parts
            or parsed_path.is_absolute()
            or not raw_path.isprintable()
            or ".." in parsed_path.parts
            or parsed_path.as_posix() != raw_path
        ):
            raise ValueError("history audit allowlist path is invalid")
        if not isinstance(reason, str) or _REASON_RE.fullmatch(reason) is None:
            raise ValueError("history audit allowlist reason is invalid")
        if not isinstance(rule, str) or (
            rule not in ALLOWLISTABLE_HISTORY_RULES and not private_rule
        ):
            raise ValueError("history audit allowlist rule is invalid")
        commits: list[str | None] = [None]
        if private_rule:
            fingerprint = entry["normalized_denylist_sha256"]
            if (
                not isinstance(fingerprint, str)
                or _SHA256_RE.fullmatch(fingerprint) is None
                or fingerprint != _denylist_fingerprint(deny_terms)
            ):
                raise ValueError("history audit denylist approval fingerprint is invalid")
            reviewed = entry["reviewed_commits"]
            if not isinstance(reviewed, list) or not reviewed:
                raise ValueError("history audit reviewed commits are invalid")
            reviewed_count += len(reviewed)
            if reviewed_count > MAX_REVIEWED_DENYLIST_OCCURRENCES:
                raise ValueError("history audit reviewed occurrence limit exceeded")
            commits = []
            for commit in reviewed:
                if not isinstance(commit, str) or _COMMIT_RE.fullmatch(commit) is None:
                    raise ValueError("history audit reviewed commits are invalid")
                commits.append(commit)
        for commit in commits:
            allowance = _HistoryAllowance(blob_digest, raw_path, rule, commit)
            if allowance in entries:
                raise ValueError("history audit allowlist contains a duplicate entry")
            entries.add(allowance)
    return frozenset(entries)


def _git(repository: Path, *args: str, input_data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=False,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("Git history audit command timed out") from error
    if result.returncode != 0:
        raise ValueError("Git history audit could not read the repository")
    return result.stdout


def _history_commits(repository: Path, *, maximum: int) -> tuple[str, ...]:
    commits = tuple(
        commit.decode("ascii") for commit in _git(repository, "rev-list", "--all").splitlines()
    )
    if len(commits) > maximum:
        raise ValueError("Git history audit commit limit exceeded")
    return commits


def _tree_entries(repository: Path, commit: str) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for record in _git(repository, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3 or parts[1] != b"blob":
            continue
        path = _safe_location(raw_path.decode("utf-8", "surrogateescape"))
        entries.append((parts[2].decode("ascii"), path))
    return tuple(entries)


def _history_occurrences(
    repository: Path,
    *,
    maximum_commits: int,
    maximum_blobs: int,
) -> dict[str, set[tuple[str, str]]]:
    occurrences: dict[str, set[tuple[str, str]]] = {}
    for commit in _history_commits(repository, maximum=maximum_commits):
        for object_id, path in _tree_entries(repository, commit):
            occurrences.setdefault(object_id, set()).add((commit, path))
            if len(occurrences) > maximum_blobs:
                raise ValueError("Git history audit blob limit exceeded")
    return occurrences


def _batch_blob_sizes(repository: Path, object_ids: tuple[str, ...]) -> dict[str, int]:
    request = "".join(f"{object_id}\n" for object_id in object_ids).encode("ascii")
    output = _git(
        repository,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_data=request,
    )
    sizes: dict[str, int] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != b"blob":
            raise ValueError("Git history audit encountered a non-blob object")
        sizes[parts[0].decode("ascii")] = int(parts[2])
    if set(sizes) != set(object_ids):
        raise ValueError("Git history audit blob inventory mismatch")
    return sizes


def _batch_blobs(repository: Path, object_ids: tuple[str, ...]) -> dict[str, bytes]:
    request = "".join(f"{object_id}\n" for object_id in object_ids).encode("ascii")
    output = _git(repository, "cat-file", "--batch", input_data=request)
    blobs: dict[str, bytes] = {}
    offset = 0
    for expected_id in object_ids:
        header_end = output.find(b"\n", offset)
        if header_end < 0:
            raise ValueError("Git history audit batch output is truncated")
        header = output[offset:header_end].split()
        if len(header) != 3 or header[0].decode("ascii") != expected_id or header[1] != b"blob":
            raise ValueError("Git history audit batch header mismatch")
        size = int(header[2])
        start = header_end + 1
        end = start + size
        if end >= len(output) or output[end : end + 1] != b"\n":
            raise ValueError("Git history audit batch body is truncated")
        blobs[expected_id] = output[start:end]
        offset = end + 1
    if offset != len(output):
        raise ValueError("Git history audit batch output has trailing data")
    return blobs


def _reported_path(path: str, deny_terms: Sequence[str]) -> str:
    normalized = unicodedata.normalize("NFKC", path).casefold()
    if any(term in normalized for term in deny_terms):
        digest = sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:16]
        return f"<redacted-path:{digest}>"
    return _safe_location(path)


def audit_history(
    repository: Path,
    denylist: Path,
    *,
    maximum_commits: int = MAX_HISTORY_COMMITS,
    maximum_blobs: int = MAX_HISTORY_BLOBS,
    maximum_bytes: int = MAX_HISTORY_BYTES,
) -> list[HistoryFinding]:
    """Scan reachable blobs once and return redacted commit/path/rule findings."""
    if not repository.is_dir():
        raise ValueError("repository must be an existing directory")
    terms = _load_denylist(denylist)
    allowances = _load_history_allowlist(repository, terms)
    occurrences = _history_occurrences(
        repository,
        maximum_commits=maximum_commits,
        maximum_blobs=maximum_blobs,
    )
    object_ids = tuple(sorted(occurrences))
    sizes = _batch_blob_sizes(repository, object_ids)
    if sum(sizes.values()) > maximum_bytes:
        raise ValueError("Git history audit byte limit exceeded")
    blobs = _batch_blobs(repository, object_ids)
    findings: set[HistoryFinding] = set()
    for object_id, blob_occurrences in occurrences.items():
        content_rules = set(content_rule_ids(blobs[object_id], terms))
        blob_digest = sha256(blobs[object_id]).hexdigest()
        for commit, path in blob_occurrences:
            rules = content_rules | set(path_rule_ids(path))
            reported_path = _reported_path(path, terms)
            findings.update(
                HistoryFinding(commit=commit, path=reported_path, rule=rule)
                for rule in rules
                if _HistoryAllowance(blob_digest, path, rule) not in allowances
                and _HistoryAllowance(blob_digest, path, rule, commit) not in allowances
            )
    return sorted(findings, key=lambda finding: (finding.commit, finding.path, finding.rule))


def build_parser() -> argparse.ArgumentParser:
    """Build the metadata-only Git-history audit parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--private-denylist", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the history audit without printing matched bytes or denylist terms."""
    args = build_parser().parse_args(argv)
    try:
        findings = audit_history(args.repository, args.private_denylist)
    except (OSError, ValueError) as exc:
        print(f"history audit error: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"{finding.commit}:{finding.path}: {finding.rule}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
