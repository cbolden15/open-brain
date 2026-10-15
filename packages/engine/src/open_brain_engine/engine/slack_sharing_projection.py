"""Field-wise inert Slack evidence projection; only citation.permalink permits a URL."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

from open_brain_engine.core.ids import portable_canonical_json_bytes

ENVELOPE_VERSION = "slack-recall-evidence.v1"
PROJECTION_VERSION = "slack-recall-projection.v1"
MARKER_PREFIX = "urn:open-brain:sharing-copy:v2:"
ANY_MARKER_PREFIX = "urn:open-brain:sharing-copy:"
DELIVERY_PREFIX = "sharing.copy.v2.:"


def safe_permalink(value: str, *, channel: str | None = None, timestamp: str | None = None) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)?slack\.com", host)
            or parsed.netloc != host
        ):
            return False
        match = re.fullmatch(r"/archives/([CG][A-Z0-9]+)/p([0-9]{10,16})", parsed.path)
        return bool(
            match
            and (channel is None or channel == match[1])
            and (timestamp is None or timestamp.replace(".", "") == match[2])
        )
    except ValueError:
        return False


def project_envelope(value: dict[str, Any], *, source_reference: str) -> dict[str, Any]:
    from .search_projection import public_search_text

    if (
        set(value) != {"envelope_version", "message_text", "citation"}
        or value["envelope_version"] != ENVELOPE_VERSION
    ):
        raise ValueError("invalid Slack evidence")
    citation = value["citation"]
    required = {
        "original_record_id",
        "original_revision_id",
        "source_timestamp",
        "observed_at",
        "upstream_currency",
        "permalink",
        "permalink_unavailable_reason",
        "content_origin",
        "verification",
        "scope",
        "slack_coverage",
    }
    if (
        type(citation) is not dict
        or set(citation) != required
        or type(value["message_text"]) is not str
    ):
        raise ValueError("invalid Slack evidence")
    projected = {}
    for key, field in citation.items():
        if key == "permalink":
            projected[key] = field if type(field) is str and safe_permalink(field) else None
        elif field is None:
            projected[key] = None
        elif type(field) is str:
            projected[key] = public_search_text(field, protected_source_reference=source_reference)
        else:
            raise ValueError("invalid Slack citation")
    if projected["permalink"] is None and projected["permalink_unavailable_reason"] is None:
        projected["permalink_unavailable_reason"] = "unsafe_url"
    return {
        "envelope_version": ENVELOPE_VERSION,
        "message_text": public_search_text(
            value["message_text"], protected_source_reference=source_reference
        ),
        "citation": projected,
    }


def envelope_text(value: dict[str, Any], *, source_reference: str) -> str:
    return portable_canonical_json_bytes(
        project_envelope(value, source_reference=source_reference)
    ).decode("utf-8")


def project_copy_text(text: str, *, source_reference: str) -> str:
    return envelope_text(json.loads(text), source_reference=source_reference)


def projection_sha256(text: str) -> str:
    return sha256(b"open-brain-slack-recall-evidence.v1\0" + text.encode("utf-8")).hexdigest()


def build_envelope(evidence: dict[str, Any], *, capture_id: str) -> str:
    namespace = evidence["namespace"]
    channel = namespace["resource_id"].removeprefix("channel:")
    external = namespace["external_id"]
    # A reply identity includes its root timestamp; only the final timestamp is the message.
    timestamp = external.rsplit(":", 1)[-1]
    if not re.fullmatch(r"[0-9]{10}\.[0-9]{6}", timestamp):
        raise ValueError("invalid Slack message identity")
    seconds, micros = timestamp.split(".")
    source_timestamp = (
        datetime.fromtimestamp(int(seconds), UTC)
        .replace(microsecond=int(micros))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    reference = evidence["revision"]["source_reference"]
    permalink = (
        reference if safe_permalink(reference, channel=channel, timestamp=timestamp) else None
    )
    from .materializer import _payload_search_text

    value = {
        "envelope_version": ENVELOPE_VERSION,
        "message_text": _payload_search_text(evidence["record"]["payload"]),
        "citation": {
            "original_record_id": capture_id,
            "original_revision_id": capture_id,
            "source_timestamp": source_timestamp,
            "observed_at": evidence["record"]["accepted_at"],
            "upstream_currency": "unknown_since_observation",
            "permalink": permalink,
            "permalink_unavailable_reason": None if permalink else "unsafe_url",
            "content_origin": "third_party",
            "verification": "unverified",
            "scope": "selected_owner_approved_messages",
            "slack_coverage": "unknown",
        },
    }
    # The raw reference stays protected in message text. Only the validated field is exempt.
    return envelope_text(value, source_reference=reference)
