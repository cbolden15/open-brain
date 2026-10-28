"""Saved item records: one producer-written JSON file per URL the owner kept."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit

from open_brain_engine.capture.redaction import has_redaction_finding, redact_text
from open_brain_engine.core.ids import canonicalize_source_url
from open_brain_engine.core.models import ContentKind, SourceType
from open_brain_engine.engine import ReferencePayload

from open_brain_connectors.runtime.live_common import (
    LiveSourceError,
    bounded_json,
    local_source_privacy,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import (
    SAVED_ITEM_SOURCE,
    SourceResourceSelection,
)

__all__ = [
    "FETCH_STATUSES",
    "MAX_ENCODED_BYTES",
    "MAX_INTAKE_TITLE_CHARS",
    "MAX_RECORD_BYTES",
    "MAX_TEXT_CHARS",
    "RECORD_REFUSAL_CODES",
    "TRACKING_RULES",
    "TRACKING_RULE_VERSION",
    "TRUNCATED_MARKER",
    "SavedItemRecord",
    "SavedItemRefusal",
    "SavedItemSourceAdapter",
    "content_digest",
    "encoded_intake_size",
    "identity_url",
    "item_key",
    "parse_record",
]

TRACKING_RULE_VERSION = 1
# (hosts matched with their subdomains, () for any host; exact names; name prefixes).
TRACKING_RULES: tuple[tuple[tuple[str, ...], frozenset[str], tuple[str, ...]], ...] = (
    ((), frozenset({"fbclid", "gclid"}), ("utm_",)),
    (("instagram.com", "threads.net", "threads.com"), frozenset({"igsh", "igshid"}), ()),
    (("youtube.com", "youtu.be", "spotify.com"), frozenset({"si"}), ()),
)
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


class SavedItemRefusal(Exception):
    """A record that stays local, named by a stable refusal code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _host_matches(host: str, hosts: tuple[str, ...]) -> bool:
    return not hosts or any(host == item or host.endswith("." + item) for item in hosts)


def _is_tracking(host: str, name: str) -> bool:
    lowered = name.lower()
    return any(
        _host_matches(host, hosts) and (lowered in names or lowered.startswith(prefixes))
        for hosts, names, prefixes in TRACKING_RULES
    )


def identity_url(raw: object) -> tuple[str, str]:
    """Return ``(identity URL, stored URL)``; the stored URL never carries a fragment."""
    if type(raw) is not str or any(ord(character) < 32 for character in raw):
        raise SavedItemRefusal("invalid_url")
    try:
        raw.encode("utf-8")
        parts = urlsplit(raw)
        https = parts.scheme.lower() == "https"
        base = canonicalize_source_url(urlunsplit(parts._replace(fragment=""))) if https else ""
    except ValueError:
        raise SavedItemRefusal("invalid_url") from None
    if not https:
        raise SavedItemRefusal("invalid_url")
    split = urlsplit(base)
    host = split.hostname or ""
    query = "&".join(
        pair
        for pair in split.query.split("&")
        if pair and not _is_tracking(host, pair.partition("=")[0])
    )
    fragment = _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), parts.fragment)
    # Removed tracking values are never stored or hashed, so only what survives is checked.
    if has_redaction_finding(query) or has_redaction_finding(fragment):
        raise SavedItemRefusal("url_secret")
    stored = urlunsplit(split._replace(query=query))
    return stored + ("#" + fragment if fragment else ""), stored


def item_key(identity: str) -> str:
    return sha256(identity.encode("utf-8")).hexdigest()[:32]


MAX_RECORD_BYTES = 131_072
MAX_WHY_CHARS = 280
MAX_RECORD_TITLE_CHARS = 300
MAX_AUTHOR_CHARS = 200
FETCH_STATUSES = frozenset({"complete", "failed", "not_attempted", "partial"})
RECORD_REFUSAL_CODES = frozenset(
    {
        "file_too_large",
        "invalid_field",
        "invalid_json",
        "invalid_url",
        "invalid_why",
        "missing_field",
        "payload_too_large",
        "unknown_field",
        "url_secret",
    }
)
_REQUIRED = frozenset({"schema_version", "url", "why", "received_at", "fetch_status"})
_OPTIONAL = frozenset(
    {
        "source_type",
        "content_kind",
        "platform",
        "title",
        "author",
        "body",
        "media_text",
        "shared_text",
        "summary",
        "summary_origin",
    }
)
_RECEIVED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
_PLATFORM = re.compile(r"[a-z0-9][a-z0-9 ._+-]{0,39}")
_SUMMARY_ORIGIN = re.compile(r"producer:[a-z0-9][a-z0-9._-]{0,63}")
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
_SOCIAL_HOSTS = (
    "bsky.app",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "reddit.com",
    "threads.com",
    "threads.net",
    "tiktok.com",
    "twitter.com",
    "x.com",
)


@dataclass(frozen=True, slots=True)
class SavedItemRecord:
    """A validated version 1 record; build it only with ``parse_record``."""

    identity_url: str
    stored_url: str
    item_key: str
    why: str
    received_at: datetime
    fetch_status: str
    source_type: SourceType
    content_kind: ContentKind
    platform: str | None
    title: str | None
    author: str | None
    body: str
    media_text: str
    shared_text: str
    summary: str | None
    summary_origin: str | None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate key")
    return value


def _reject_constant(_name: str) -> object:
    raise ValueError("non-finite number")


def _string(value: object, maximum: int | None = None) -> str:
    if type(value) is not str or "\x00" in value or (maximum is not None and len(value) > maximum):
        raise SavedItemRefusal("invalid_field")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SavedItemRefusal("invalid_field") from None
    return value


def _optional_string(value: object, maximum: int | None = None) -> str | None:
    return None if value is None else _string(value, maximum)


def _why(value: object) -> str:
    if type(value) is not str:
        raise SavedItemRefusal("invalid_why")
    why = value.strip()
    if not 1 <= len(why) <= MAX_WHY_CHARS or any(
        unicodedata.category(character) in {"Cc", "Zl", "Zp"} for character in why
    ):
        raise SavedItemRefusal("invalid_why")
    try:
        why.encode("utf-8")
    except UnicodeEncodeError:
        raise SavedItemRefusal("invalid_why") from None
    return why


def _received_at(value: object) -> datetime:
    if type(value) is not str or _RECEIVED_AT.fullmatch(value) is None:
        raise SavedItemRefusal("invalid_field")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise SavedItemRefusal("invalid_field") from None


def _source_type(value: object, host: str) -> SourceType:
    if value is None:
        if _host_matches(host, _YOUTUBE_HOSTS):
            return SourceType.YOUTUBE
        return SourceType.SOCIAL if _host_matches(host, _SOCIAL_HOSTS) else SourceType.WEB
    if type(value) is not str or value not in {item.value for item in SourceType}:
        raise SavedItemRefusal("invalid_field")
    return SourceType(value)


def _content_kind(value: object) -> ContentKind:
    if value is None:
        return ContentKind.OTHER
    if type(value) is not str or value not in {item.value for item in ContentKind}:
        raise SavedItemRefusal("invalid_field")
    return ContentKind(value)


def _platform(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _PLATFORM.fullmatch(value) is None:
        raise SavedItemRefusal("invalid_field")
    return value


def _summary(summary: object, origin: object) -> tuple[str | None, str | None]:
    text = _optional_string(summary)
    if (text is None) != (origin is None):
        raise SavedItemRefusal("invalid_field")
    if origin is not None and (
        type(origin) is not str or _SUMMARY_ORIGIN.fullmatch(origin) is None
    ):
        raise SavedItemRefusal("invalid_field")
    return text, None if origin is None else str(origin)


def parse_record(raw: bytes) -> SavedItemRecord:
    """Validate one version 1 record file; any fault is a named local refusal."""
    if type(raw) is not bytes or len(raw) > MAX_RECORD_BYTES:
        raise SavedItemRefusal("file_too_large")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise SavedItemRefusal("invalid_json") from None
    if not isinstance(value, dict):
        raise SavedItemRefusal("invalid_json")
    if set(value) - (_REQUIRED | _OPTIONAL):
        raise SavedItemRefusal("unknown_field")
    if _REQUIRED - set(value):
        raise SavedItemRefusal("missing_field")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise SavedItemRefusal("invalid_field")
    identity, stored = identity_url(value["url"])
    why = _why(value["why"])
    received_at = _received_at(value["received_at"])
    if type(value["fetch_status"]) is not str or value["fetch_status"] not in FETCH_STATUSES:
        raise SavedItemRefusal("invalid_field")
    summary, summary_origin = _summary(value.get("summary"), value.get("summary_origin"))
    return SavedItemRecord(
        identity_url=identity,
        stored_url=stored,
        item_key=item_key(identity),
        why=why,
        received_at=received_at,
        fetch_status=str(value["fetch_status"]),
        source_type=_source_type(value.get("source_type"), urlsplit(stored).hostname or ""),
        content_kind=_content_kind(value.get("content_kind")),
        platform=_platform(value.get("platform")),
        title=_optional_string(value.get("title"), MAX_RECORD_TITLE_CHARS),
        author=_optional_string(value.get("author"), MAX_AUTHOR_CHARS),
        body=_string(value.get("body", "")),
        media_text=_string(value.get("media_text", "")),
        shared_text=_string(value.get("shared_text", "")),
        summary=summary,
        summary_origin=summary_origin,
    )


MAX_TEXT_CHARS = 60_000
MAX_ENCODED_BYTES = 65_536
MAX_INTAKE_TITLE_CHARS = 200
TRUNCATED_MARKER = "[truncated]"
_TRUNCATION_ORDER = ("Media text", "Body", "Shared text")


def _sections(record: SavedItemRecord) -> list[tuple[str, str]]:
    kind = [
        f"source_type: {record.source_type.value}",
        f"content_kind: {record.content_kind.value}",
    ]
    if record.platform is not None:
        kind.append(f"platform: {record.platform}")
    raw = [
        ("Stated reason", record.why),
        (f"Summary ({record.summary_origin})", record.summary or ""),
        ("Body", record.body),
        ("Media text", record.media_text),
        ("Shared text", record.shared_text),
        ("Kind", "; ".join(kind)),
        ("Fetch status", record.fetch_status),
    ]
    # Masking, never refusing: the engine policy flags ordinary emails and long ids too.
    return [(heading, redact_text(body).strip()) for heading, body in raw]


def _render(sections: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{heading}\n{body}" for heading, body in sections if body)


def _fits(url: str, text: str) -> bool:
    if len(text) > MAX_TEXT_CHARS:
        return False
    try:
        bounded_json(ReferencePayload(url=url, supplied_text=text).to_dict(), MAX_ENCODED_BYTES)
    except ValueError, LiveSourceError:
        return False
    return True


def _truncated(body: str, keep: int) -> str:
    head = body[:keep].rstrip()
    return f"{head}\n{TRUNCATED_MARKER}" if head else TRUNCATED_MARKER


def _bounded_text(url: str, sections: list[tuple[str, str]]) -> str:
    text = _render(sections)
    if _fits(url, text):
        return text
    for heading in _TRUNCATION_ORDER:
        index = next(i for i, (name, _body) in enumerate(sections) if name == heading)
        body = sections[index][1]
        if not body:
            continue
        best, low, high = TRUNCATED_MARKER, 1, len(body) - 1
        while low <= high:
            middle = (low + high) // 2
            sections[index] = (heading, _truncated(body, middle))
            if _fits(url, _render(sections)):
                best, low = sections[index][1], middle + 1
            else:
                high = middle - 1
        sections[index] = (heading, best)
        text = _render(sections)
        if _fits(url, text):
            return text
    raise SavedItemRefusal("payload_too_large")


def _title(record: SavedItemRecord) -> str:
    if record.title is not None and record.title.strip():
        base = record.title
    elif record.author is not None and record.author.strip() and record.platform is not None:
        base = f"{record.author.strip()} on {record.platform}"
    else:
        base = record.stored_url
    title = " ".join(redact_text(base).split())[:MAX_INTAKE_TITLE_CHARS].strip()
    return title or record.stored_url[:MAX_INTAKE_TITLE_CHARS]


def encoded_intake_size(intake: SourceRecordIntake) -> int:
    """Bytes one intake adds to a page, measured the way the collector encodes it."""
    value = {"payload": intake.payload().to_dict(), "title": intake.title}
    return len(bounded_json(value, 2 * MAX_ENCODED_BYTES))


def content_digest(intake: SourceRecordIntake) -> str:
    """Fixed-size digest of the admitted bytes, used by first-save-wins."""
    value = [intake.url, intake.title, intake.text]
    return sha256(bounded_json(value, 2 * MAX_ENCODED_BYTES)).hexdigest()[:32]


class SavedItemSourceAdapter:
    """Turn validated saved-item records into intakes for one selected folder."""

    def __init__(self, selection: SourceResourceSelection) -> None:
        if type(selection) is not SourceResourceSelection or (
            selection.connector_name != SAVED_ITEM_SOURCE
        ):
            raise LiveSourceError("source_selection_mismatch")
        self._selection = selection

    @staticmethod
    def record_from_bytes(raw: bytes) -> SavedItemRecord:
        return parse_record(raw)

    def intake(self, record: SavedItemRecord) -> SourceRecordIntake:
        text = _bounded_text(record.stored_url, _sections(record))
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=SAVED_ITEM_SOURCE,
                connection_id=self._selection.connection_id,
                resource_id=self._selection.resource_id,
                external_id=record.item_key,
                revision_id="1",
            ),
            url=record.stored_url,
            text=text,
            privacy=local_source_privacy(),
            title=_title(record),
        )
