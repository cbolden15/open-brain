"""Shared normalization and durable-record serialization helpers."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes

if TYPE_CHECKING:
    from .contracts import DecisionOutcome, LocalEngineContext

_MAX_TEXT = 65_536
_MAX_FILE_BYTES = 1_048_576
_MAX_NAME = 120
_MAX_REASON = 1_000
_DELIVERY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
_UNIT = re.compile(r"^[a-zA-Z][a-zA-Z0-9_./-]{0,63}$")
_DECIMAL = re.compile(
    r"^(?=.{1,130}$)(?=(?:[^0-9]*[0-9]){1,128}$)"
    r"(?:0|-?(?:(?:[1-9][0-9]*)(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$"
)
_TERM = re.compile(r"[A-Za-z0-9]+")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip() or "\x00" in normalized or len(normalized) > maximum:
        raise ValueError(f"invalid {field}")
    return normalized


def _optional_text(value: object, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, maximum=maximum)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid occurrence timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid occurrence timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("invalid occurrence timestamp")
    return _timestamp(parsed)


def _attributes(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid attributes")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        name = _text(key, field="attribute name", maximum=120)
        text = _text(item, field="attribute value", maximum=1_000)
        if name in normalized:
            raise ValueError("duplicate attribute")
        normalized[name] = text
    return MappingProxyType(dict(sorted(normalized.items())))


def _attribute_list(value: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"name": name, "value": item} for name, item in value.items()]


def _pairs(value: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(f"{name} {item}" for name, item in value.items())


def _delivery_id(value: str) -> str:
    if not isinstance(value, str) or _DELIVERY.fullmatch(value) is None:
        raise ValueError("invalid delivery identity")
    return value


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


def _portable_id(value: str, prefix: str) -> str:
    marker = prefix + "_"
    if not isinstance(value, str) or not value.startswith(marker):
        raise ValueError("invalid portable identifier")
    try:
        identifier = uuid.UUID(value.removeprefix(marker))
    except ValueError as error:
        raise ValueError("invalid portable identifier") from error
    if identifier.version != 4 or value != f"{prefix}_{identifier}":
        raise ValueError("invalid portable identifier")
    return value


def _role_claim(profile: LocalEngineContext) -> dict[str, object]:
    result = dict(profile.owner_role_claim)
    capabilities = result.get("capabilities")
    if isinstance(capabilities, tuple | list):
        result["capabilities"] = list(capabilities)
    return result


def _privacy() -> dict[str, object]:
    return {
        "authority": {"cloud": False, "external_egress": False},
        "confirmation_ref": None,
        "policy_version": "privacy-v1",
        "reason": "personal_local_only",
        "tier": "personal",
    }


def _trust(
    profile: LocalEngineContext,
    assessed_at: str,
    label: str,
    reason: str,
) -> dict[str, object]:
    return {
        "assessed_at": assessed_at,
        "assessor_actor_id": profile.owner_actor_id,
        "label": label,
        "reason": reason,
    }


def _receipt(
    kind: str,
    receipt_id: str,
    subject_id: str,
    recorded_at: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    thawed = dict(payload)
    return {
        "kind": kind,
        "payload": thawed,
        "receipt_id": receipt_id,
        "recorded_at": recorded_at,
        "sha256": sha256(portable_canonical_json_bytes(thawed)).hexdigest(),
        "subject_id": subject_id,
    }


def _dated_path(root: str, timestamp: str, identifier: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    return f"{root}/{parsed:%Y/%m}/{identifier}.json"


def _payload_dict(row: sqlite3.Row) -> dict[str, object]:
    raw = cast(bytes, row["payload_json"])
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid stored payload")
    return cast(dict[str, object], value)


def _space_row(connection: sqlite3.Connection, space_id: str) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        connection.execute("SELECT * FROM spaces WHERE space_id = ?", (space_id,)).fetchone(),
    )


def _slug(name: str, space_id: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "space"
    suffix = space_id.removeprefix("space_").replace("-", "")[:8]
    return f"{base[:80].strip('-') or 'space'}-{suffix}"


def _decision_record(
    *,
    profile: LocalEngineContext,
    proposal: Mapping[str, object],
    decision_id: str,
    outcome: DecisionOutcome,
    edited_bytes: bytes | None,
    recorded_at: str,
    expected_state_digest: str | None = None,
) -> dict[str, object]:
    proposal_id = cast(str, proposal["proposal_id"])
    edited = (
        {
            "bytes_base64": base64.b64encode(edited_bytes).decode("ascii"),
            "sha256": sha256(edited_bytes).hexdigest(),
        }
        if edited_bytes is not None
        else None
    )
    if expected_state_digest is None:
        expected_state_digest = sha256(portable_canonical_json_bytes(dict(proposal))).hexdigest()
    elif not isinstance(expected_state_digest, str) or len(expected_state_digest) != 64:
        raise ValueError("invalid expected state digest")
    terminal_payload = {
        "decision_id": decision_id,
        "edited_content_sha256": (
            sha256(edited_bytes).hexdigest() if edited_bytes is not None else None
        ),
        "expected_state_digest": expected_state_digest,
        "outcome": outcome.value,
        "proposal_id": proposal_id,
    }
    return {
        "actor_id": profile.owner_actor_id,
        "decision_id": decision_id,
        "edited_content": edited,
        "expected_receipt": proposal["expected_receipt"],
        "expected_state_digest": expected_state_digest,
        "outcome": outcome.value,
        "proposal_id": proposal_id,
        "recorded_at": recorded_at,
        "role_claim": _role_claim(profile),
        "schema_version": 1,
        "tenant_id": profile.tenant_id,
        "terminal_digest": sha256(portable_canonical_json_bytes(terminal_payload)).hexdigest(),
    }


def _publication_record(
    *,
    profile: LocalEngineContext,
    decision_id: str,
    page_id: str,
    publication_id: str,
    published_path: str,
    published_bytes: bytes,
    recorded_at: str,
) -> dict[str, object]:
    return {
        "actor_id": profile.owner_actor_id,
        "decision_id": decision_id,
        "page_id": page_id,
        "publication_id": publication_id,
        "published_bytes_base64": base64.b64encode(published_bytes).decode("ascii"),
        "published_path": published_path,
        "published_sha256": sha256(published_bytes).hexdigest(),
        "recorded_at": recorded_at,
        "role_claim": _role_claim(profile),
        "schema_version": 1,
        "tenant_id": profile.tenant_id,
    }


def _excerpt(body: str, terms: tuple[str, ...]) -> str:
    collapsed = " ".join(body.split())
    if not collapsed:
        return "(empty)"
    if not terms:
        return collapsed[:512]
    lower = collapsed.casefold()
    offsets = [lower.find(term) for term in terms if lower.find(term) >= 0]
    start = max(0, min(offsets, default=0) - 80)
    return collapsed[start : start + 512]


def _done(table: str) -> int:
    return {
        "captures": 3,
        "decisions": 3,
        "proposal_sets": 1,
        "route_operations": 2,
        "space_operations": 1,
    }[table]
