"""Credential detection that decides whether an agent session is quarantined.

The engine redaction policy also matches personal details and every long identifier, so it
cannot decide quarantine: commit hashes, UUIDs and path slugs would refuse most sessions.
This gate matches credential shapes only and leaves redaction policy unchanged.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
_CREDENTIAL_URL = re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|secret|token)"
    r"\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&]+)"
)
# A bearer value needs a digit, so prose such as "bearer authentication" is not a credential.
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+(?=[A-Za-z._~+/-]*\d)[A-Za-z0-9._~+/-]{8,}")
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"
    r")"
)
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")
_SEPARATOR = re.compile(r"[-_]")
_MIN_SEGMENT = 16
_MIN_ENTROPY_BITS = 3.5


def contains_session_secret(text: str) -> bool:
    """Return whether text holds a credential shape that must quarantine its session."""
    if any(
        pattern.search(text)
        for pattern in (
            _PRIVATE_KEY,
            _CREDENTIAL_URL,
            _CREDENTIAL_ASSIGNMENT,
            _BEARER_TOKEN,
            _KNOWN_TOKEN,
        )
    ):
        return True
    return any(_is_random_token(match.group()) for match in _CANDIDATE.finditer(text))


def _is_random_token(value: str) -> bool:
    # Slugs and file names join short words; a credential has a long random segment.
    return any(
        len(segment) >= _MIN_SEGMENT and _is_random_segment(segment)
        for segment in _SEPARATOR.split(value)
    )


def _is_random_segment(value: str) -> bool:
    # Hashes and UUIDs are lowercase hex; words and identifiers lack mixed case with digits.
    if not (
        any(char.isupper() for char in value)
        and any(char.islower() for char in value)
        and any(char.isdigit() for char in value)
    ):
        return False
    counts = Counter(value)
    entropy = -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())
    return entropy >= _MIN_ENTROPY_BITS
