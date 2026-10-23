"""Portable18 ``ob.followups.v1`` evidence: follow-up items, evidence positions and decisions.

Evidence rows carry positions and hashes of quotes, never quote text, so a retracted
revision leaves nothing behind. A retracted item travels with ``summary`` null and
``evidence_retracted`` true.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .v1 import PortableValidationError

FOLLOWUPS_CONTRACT = "ob.followups.v1"
FOLLOWUPS_PATH = "history/followups/followups-v1.json"
MAX_ITEMS = 10_000
MAX_EVIDENCE_PER_ITEM = 50
MAX_SUMMARY_CHARS = 280
_MAX_ID = 128
_MAX_OPERATION_ID = 128
_MAX_PRINCIPAL_ID = 256
_KINDS = frozenset({"commitment", "question"})
_STATES = frozenset({"suggested", "confirmed", "snoozed", "dismissed", "resolved", "superseded"})
_RETURN_STATES = frozenset({"suggested", "confirmed"})
_OWNERS = frozenset({"self", "other", "unknown"})
_ROLES = frozenset({"origin", "supporting", "correction", "resolution"})
_RESOLUTIONS = frozenset({"evidence", "owner_statement"})
_ACTIONS = frozenset(
    {
        "propose",
        "add_evidence",
        "confirm",
        "dismiss",
        "snooze",
        "correct",
        "link",
        "resolve",
        "reopen",
        "supersede",
    }
)
_TIERS = frozenset({"public", "work", "personal", "secret", "unknown"})
_ITEM_KEYS = frozenset(
    {
        "item_id",
        "kind",
        "project",
        "summary",
        "owner",
        "owner_name",
        "owner_confirmed",
        "due",
        "due_evidence_id",
        "state",
        "snoozed_until",
        "snooze_return_state",
        "ticket_key",
        "ticket_url",
        "privacy_tier",
        "superseded_by",
        "resolution",
        "version",
        "created_at",
        "updated_at",
        "new_evidence_since_decision",
        "evidence_retracted",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "evidence_id",
        "item_id",
        "ordinal",
        "role",
        "capture_id",
        "revision_id",
        "quote_start",
        "quote_end",
        "quote_sha256",
        "recorded_at",
    }
)
_DECISION_KEYS = frozenset(
    {
        "decision_id",
        "item_id",
        "sequence",
        "operation_id",
        "request_sha256",
        "principal_id",
        "action",
        "prior_state",
        "new_state",
        "prior_version",
        "new_version",
        "evidence_ids",
        "recorded_at",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _invalid(detail: str) -> PortableValidationError:
    return PortableValidationError(f"Portable18 follow-up evidence invalid: {detail}")


def _text(value: object, *, limit: int = _MAX_ID) -> str:
    if type(value) is not str or not 1 <= len(value) <= limit:
        raise ValueError("text")
    return value


def _optional_text(value: object, *, limit: int = _MAX_ID) -> str | None:
    return None if value is None else _text(value, limit=limit)


def _optional_date(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _DATE.fullmatch(value) is None:
        raise ValueError("date")
    return value


def _timestamp(value: object) -> str:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("timestamp")
    return value


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("flag")
    return value


def _count(value: object, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("count")
    return value


def _choice(value: object, choices: frozenset[str]) -> str:
    if type(value) is not str or value not in choices:
        raise ValueError("choice")
    return value


def _optional_choice(value: object, choices: frozenset[str]) -> str | None:
    return None if value is None else _choice(value, choices)


def _item(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _ITEM_KEYS:
        raise ValueError("item keys")
    retracted = _flag(value["evidence_retracted"])
    summary = value["summary"]
    if retracted:
        if summary is not None:
            raise ValueError("retracted summary")
    else:
        _text(summary, limit=MAX_SUMMARY_CHARS)
    state = _choice(value["state"], _STATES)
    owner = _choice(value["owner"], _OWNERS)
    owner_name = _optional_text(value["owner_name"], limit=128)
    if (owner == "other") != (owner_name is not None):
        raise ValueError("owner name")
    due = _optional_date(value["due"])
    due_evidence_id = _optional_text(value["due_evidence_id"])
    if (due is None) != (due_evidence_id is None):
        raise ValueError("due evidence")
    snoozed_until = _optional_date(value["snoozed_until"])
    snooze_return_state = _optional_choice(value["snooze_return_state"], _RETURN_STATES)
    if (state == "snoozed") != (snoozed_until is not None) or (state == "snoozed") != (
        snooze_return_state is not None
    ):
        raise ValueError("snooze")
    ticket_key = _optional_text(value["ticket_key"], limit=200)
    ticket_url = _optional_text(value["ticket_url"], limit=2048)
    if ticket_url is not None and ticket_key is None:
        raise ValueError("ticket")
    superseded_by = _optional_text(value["superseded_by"])
    if (state == "superseded") != (superseded_by is not None):
        raise ValueError("superseded")
    resolution = _optional_choice(value["resolution"], _RESOLUTIONS)
    if (state == "resolved") != (resolution is not None):
        raise ValueError("resolution")
    return {
        "item_id": _text(value["item_id"]),
        "kind": _choice(value["kind"], _KINDS),
        "project": _optional_text(value["project"], limit=128),
        "summary": summary,
        "owner": owner,
        "owner_name": owner_name,
        "owner_confirmed": _flag(value["owner_confirmed"]),
        "due": due,
        "due_evidence_id": due_evidence_id,
        "state": state,
        "snoozed_until": snoozed_until,
        "snooze_return_state": snooze_return_state,
        "ticket_key": ticket_key,
        "ticket_url": ticket_url,
        "privacy_tier": _choice(value["privacy_tier"], _TIERS),
        "superseded_by": superseded_by,
        "resolution": resolution,
        "version": _count(value["version"], minimum=1),
        "created_at": _timestamp(value["created_at"]),
        "updated_at": _timestamp(value["updated_at"]),
        "new_evidence_since_decision": _flag(value["new_evidence_since_decision"]),
        "evidence_retracted": retracted,
    }


def _evidence(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _EVIDENCE_KEYS:
        raise ValueError("evidence keys")
    start = _count(value["quote_start"], minimum=0)
    end = _count(value["quote_end"], minimum=start + 1)
    digest = value["quote_sha256"]
    if type(digest) is not str or _SHA256.fullmatch(digest) is None:
        raise ValueError("quote hash")
    ordinal = _count(value["ordinal"], minimum=0)
    if ordinal >= MAX_EVIDENCE_PER_ITEM:
        raise ValueError("ordinal")
    # v1 cites source captures only: the revision identity is the capture identity.
    capture_id = _text(value["capture_id"])
    if not capture_id.startswith("capture_") or value["revision_id"] != capture_id:
        raise ValueError("evidence revision")
    return {
        "evidence_id": _text(value["evidence_id"]),
        "item_id": _text(value["item_id"]),
        "ordinal": ordinal,
        "capture_id": capture_id,
        "revision_id": capture_id,
        "quote_start": start,
        "quote_end": end,
        "quote_sha256": digest,
        "role": _choice(value["role"], _ROLES),
        "recorded_at": _timestamp(value["recorded_at"]),
    }


def _decision(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _DECISION_KEYS:
        raise ValueError("decision keys")
    request = value["request_sha256"]
    if type(request) is not str or _SHA256.fullmatch(request) is None:
        raise ValueError("request hash")
    evidence_ids = value["evidence_ids"]
    if type(evidence_ids) is not list or evidence_ids != sorted(set(evidence_ids)):
        raise ValueError("evidence ids")
    prior_version = _count(value["prior_version"], minimum=0)
    new_version = _count(value["new_version"], minimum=1)
    if new_version != prior_version + 1:
        raise ValueError("decision version")
    return {
        "decision_id": _text(value["decision_id"]),
        "sequence": _count(value["sequence"], minimum=1),
        "item_id": _text(value["item_id"]),
        "operation_id": _text(value["operation_id"], limit=_MAX_OPERATION_ID),
        "request_sha256": request,
        "principal_id": _text(value["principal_id"], limit=_MAX_PRINCIPAL_ID),
        "action": _choice(value["action"], _ACTIONS),
        "prior_state": _optional_choice(value["prior_state"], _STATES),
        "new_state": _choice(value["new_state"], _STATES),
        "prior_version": prior_version,
        "new_version": new_version,
        "evidence_ids": [_text(identity) for identity in evidence_ids],
        "recorded_at": _timestamp(value["recorded_at"]),
    }


def _decision_key(row: Mapping[str, object]) -> tuple[str, int]:
    sequence = row["sequence"]
    return str(row["item_id"]), sequence if type(sequence) is int else 0


def validate_followups_payload(value: object) -> dict[str, list[dict[str, object]]]:
    """Exact typed payload: sorted unique identities, bounded sizes, consistent references."""
    if type(value) is not dict or set(value) != {"contract", "items", "evidence", "decisions"}:
        raise _invalid("object keys")
    if value["contract"] != FOLLOWUPS_CONTRACT:
        raise _invalid("contract")
    if any(type(value[key]) is not list for key in ("items", "evidence", "decisions")):
        raise _invalid("lists")
    try:
        items = [_item(row) for row in value["items"]]
        evidence = [_evidence(row) for row in value["evidence"]]
        decisions = [_decision(row) for row in value["decisions"]]
    except ValueError as error:
        raise _invalid(str(error)) from error
    item_ids = [str(row["item_id"]) for row in items]
    if len(items) > MAX_ITEMS or item_ids != sorted(set(item_ids)):
        raise _invalid("items")
    known_items = set(item_ids)
    for row in items:
        if row["superseded_by"] is not None and row["superseded_by"] not in known_items:
            raise _invalid("superseded reference")
    evidence_ids = [str(row["evidence_id"]) for row in evidence]
    if evidence_ids != sorted(set(evidence_ids)):
        raise _invalid("evidence identities")
    per_item: dict[str, list[str]] = {identity: [] for identity in item_ids}
    ordinals: set[tuple[str, int]] = set()
    evidence_item: dict[str, str] = {}
    for row in evidence:
        owner = str(row["item_id"])
        if owner not in known_items:
            raise _invalid("evidence item")
        ordinal = (owner, int(str(row["ordinal"])))
        if ordinal in ordinals:
            raise _invalid("evidence ordinal")
        ordinals.add(ordinal)
        per_item[owner].append(str(row["role"]))
        evidence_item[str(row["evidence_id"])] = owner
    for roles in per_item.values():
        if not 1 <= len(roles) <= MAX_EVIDENCE_PER_ITEM or roles.count("origin") != 1:
            raise _invalid("evidence per item")
    for row in items:
        due_evidence = row["due_evidence_id"]
        if due_evidence is not None and evidence_item.get(str(due_evidence)) != row["item_id"]:
            raise _invalid("due evidence reference")
    decision_ids = [row["decision_id"] for row in decisions]
    operation_ids = [row["operation_id"] for row in decisions]
    if len(decision_ids) != len(set(decision_ids)) or len(operation_ids) != len(set(operation_ids)):
        raise _invalid("decision identities")
    sequences: dict[str, list[int]] = {identity: [] for identity in item_ids}
    for row in decisions:
        owner = str(row["item_id"])
        if owner not in sequences:
            raise _invalid("decision item")
        sequences[owner].append(int(str(row["sequence"])))
        if not set(cast(list[str], row["evidence_ids"])) <= set(evidence_ids):
            raise _invalid("decision references")
    for numbers in sequences.values():
        if numbers != list(range(1, len(numbers) + 1)):
            raise _invalid("decision sequence")
    if decisions != sorted(decisions, key=_decision_key):
        raise _invalid("decision order")
    return {"items": items, "evidence": evidence, "decisions": decisions}


def followups_sidecar(
    items: list[dict[str, object]],
    evidence: list[dict[str, object]],
    decisions: list[dict[str, object]],
) -> bytes:
    value: dict[str, object] = {
        "contract": FOLLOWUPS_CONTRACT,
        "items": sorted(items, key=lambda row: str(row["item_id"])),
        "evidence": sorted(evidence, key=lambda row: str(row["evidence_id"])),
        "decisions": sorted(decisions, key=_decision_key),
    }
    payload = validate_followups_payload(value)
    return canonical({"contract": FOLLOWUPS_CONTRACT, **payload})


def validate_followups_evidence(files: Mapping[str, bytes]) -> dict[str, list[dict[str, object]]]:
    """Decode the required family; the file is one canonical JSON object."""
    try:
        raw = files[FOLLOWUPS_PATH]
    except KeyError:
        raise PortableValidationError("Portable18 follow-up evidence missing") from None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise _invalid("json") from error
    if canonical(value) != raw:
        raise PortableValidationError("Portable18 follow-up evidence is not canonical")
    return validate_followups_payload(value)


__all__ = [
    "FOLLOWUPS_CONTRACT",
    "FOLLOWUPS_PATH",
    "MAX_EVIDENCE_PER_ITEM",
    "MAX_ITEMS",
    "MAX_SUMMARY_CHARS",
    "followups_sidecar",
    "validate_followups_evidence",
    "validate_followups_payload",
]
