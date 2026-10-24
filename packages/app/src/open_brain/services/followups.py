"""Shared bounded follow-up operations for the owner CLI and granted MCP tools."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Literal, cast

from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.followup_contracts import (
    MAX_EVIDENCE_PER_ITEM,
    MAX_OPERATION_ID_CHARS,
    MAX_SUMMARY_CHARS,
    AddEvidenceRequest,
    DecideRequest,
    DecisionAction,
    EvidenceRef,
    EvidenceRole,
    FollowupError,
    FollowupKind,
    FollowupState,
    ListFilter,
    OwnerKind,
    ProposeRequest,
)
from open_brain_engine.engine.followups import FollowupTasks
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

DEFAULT_FOLLOWUP_PAGE_LIMIT = 50
MAX_FOLLOWUP_PAGE_LIMIT = 100
MAX_FOLLOWUP_KEY_CHARACTERS = MAX_OPERATION_ID_CHARS
# Engine contract limits (followup_contracts): project and owner_name 128, ticket_key 200,
# ticket_url 2048.
MAX_FOLLOWUP_TEXT_CHARACTERS = 128
MAX_FOLLOWUP_TICKET_KEY_CHARACTERS = 200
MAX_FOLLOWUP_URL_CHARACTERS = 2048
MAX_FOLLOWUP_CURSOR_CHARACTERS = 256
MAX_FOLLOWUP_LIST_JSON_BYTES = 250_000
MAX_FOLLOWUP_MUTATION_RESPONSE_BYTES = 4096

FOLLOWUP_ERROR_CODES = frozenset(
    {
        "evidence_mismatch",
        "not_found",
        "tier_mismatch",
        "version_conflict",
        "operation_conflict",
        "duplicate_item",
        "invalid_request",
        "capacity_reached",
    }
)
FOLLOWUP_GRANTS = frozenset({"followup-read", "followup-propose", "followup-decide"})

FollowupOperation = Callable[[Mapping[str, object]], dict[str, object]]

_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KINDS = frozenset(kind.value for kind in FollowupKind)
_STATES = frozenset(state.value for state in FollowupState)
_OWNERS = frozenset(owner.value for owner in OwnerKind)
_ROLES = frozenset(role.value for role in EvidenceRole)
_ACTIONS = frozenset(action.value for action in DecisionAction)
_RESOLUTIONS = frozenset({"evidence", "owner_statement"})
_EVIDENCE_KEYS = frozenset(
    {"capture_id", "revision_id", "quote_start", "quote_end", "quote_sha256", "role"}
)
_CORRECTION_KEYS = frozenset(
    {"owner", "owner_name", "due", "due_evidence_id", "project", "summary"}
)
_REQUIRED: dict[str, set[str]] = {
    "list": set(),
    "show": {"item_id"},
    "propose": {"operation_id", "kind", "summary", "owner", "evidence"},
    "add_evidence": {"operation_id", "item_id", "evidence"},
    "decide": {"operation_id", "item_id", "expected_version", "action"},
}
_OPTIONAL: dict[str, set[str]] = {
    "list": {
        "kinds",
        "states",
        "owner",
        "project",
        "due_before",
        "include_closed",
        "limit",
        "cursor",
    },
    "show": set(),
    "propose": {"project", "owner_name", "due", "due_evidence_index"},
    "add_evidence": set(),
    "decide": {
        "snoozed_until",
        "corrections",
        "ticket_key",
        "ticket_url",
        "superseded_by",
        "resolution",
    },
}
_ACTION_FIELDS = {
    "snooze": {"snoozed_until"},
    "correct": {"corrections"},
    "link": {"ticket_key", "ticket_url"},
    "supersede": {"superseded_by"},
    "resolve": {"resolution"},
}


class FollowupServiceError(ValueError):
    """One bounded argument-shape failure without item or quote content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_followup_arguments(operation: str, arguments: Mapping[str, object]) -> None:
    required = _REQUIRED.get(operation)
    optional = _OPTIONAL.get(operation)
    if required is None or optional is None or not isinstance(arguments, Mapping):
        raise FollowupServiceError("invalid_arguments")
    keys = set(arguments)
    if required - keys or keys - (required | optional):
        raise FollowupServiceError("invalid_arguments")

    for field, value in arguments.items():
        if field in {"item_id", "superseded_by"}:
            _require(_identifier(value, "followup"))
        elif field == "operation_id":
            _require(_valid_text(value, maximum_characters=MAX_FOLLOWUP_KEY_CHARACTERS))
        elif field == "kind":
            _require(value in _KINDS)
        elif field == "owner":
            _require(value in _OWNERS)
        elif field == "summary":
            _require(_valid_text(value, maximum_characters=MAX_SUMMARY_CHARS))
        elif field in {"project", "owner_name"}:
            _require(_valid_text(value, maximum_characters=MAX_FOLLOWUP_TEXT_CHARACTERS))
        elif field == "ticket_key":
            _require(_valid_text(value, maximum_characters=MAX_FOLLOWUP_TICKET_KEY_CHARACTERS))
        elif field == "ticket_url":
            _require(_valid_text(value, maximum_characters=MAX_FOLLOWUP_URL_CHARACTERS))
        elif field in {"due", "due_before", "snoozed_until"}:
            _require(_valid_date(value))
        elif field == "due_evidence_index":
            _require(type(value) is int and value >= 0)
        elif field == "expected_version":
            _require(type(value) is int and value >= 1)
        elif field == "limit":
            _require(type(value) is int and 1 <= value <= MAX_FOLLOWUP_PAGE_LIMIT)
        elif field == "cursor":
            _require(_valid_text(value, maximum_characters=MAX_FOLLOWUP_CURSOR_CHARACTERS))
        elif field == "include_closed":
            _require(type(value) is bool)
        elif field == "kinds":
            _require(_closed_list(value, _KINDS))
        elif field == "states":
            _require(_closed_list(value, _STATES))
        elif field == "action":
            _require(value in _ACTIONS)
        elif field == "resolution":
            _require(value in _RESOLUTIONS)
        elif field == "evidence":
            if operation == "propose":
                _require(isinstance(value, list) and 1 <= len(value) <= MAX_EVIDENCE_PER_ITEM)
                for entry in cast(list[object], value):
                    _validate_evidence(entry)
            else:
                _validate_evidence(value)
        elif field == "corrections":
            _validate_corrections(value)

    if operation == "propose":
        if (arguments["owner"] == "other") != ("owner_name" in keys):
            raise FollowupServiceError("invalid_arguments")
        index = arguments.get("due_evidence_index")
        if index is not None and not (
            "due" in keys and cast(int, index) < len(cast(list[object], arguments["evidence"]))
        ):
            raise FollowupServiceError("invalid_arguments")
    if operation == "decide":
        action = cast(str, arguments["action"])
        allowed = _ACTION_FIELDS.get(action, set())
        present = keys & set().union(*_ACTION_FIELDS.values())
        if present - allowed:
            raise FollowupServiceError("invalid_arguments")
        if action in {"snooze", "correct", "supersede", "resolve"} and not present:
            raise FollowupServiceError("invalid_arguments")
        if action == "link" and "ticket_key" not in keys:
            raise FollowupServiceError("invalid_arguments")


def followup_list(
    tasks: FollowupTasks, arguments: Mapping[str, object], *, authority: EffectiveAuthority
) -> dict[str, object]:
    validate_followup_arguments("list", arguments)
    filter_ = ListFilter(
        kinds=frozenset(FollowupKind(item) for item in _strings(arguments.get("kinds"))),
        states=frozenset(FollowupState(item) for item in _strings(arguments.get("states"))),
        owner=None if "owner" not in arguments else OwnerKind(cast(str, arguments["owner"])),
        project=cast(str | None, arguments.get("project")),
        due_before=_date(arguments.get("due_before")),
        include_closed=cast(bool, arguments.get("include_closed", False)),
        limit=cast(int, arguments.get("limit", DEFAULT_FOLLOWUP_PAGE_LIMIT)),
        cursor=cast(str | None, arguments.get("cursor")),
    )
    try:
        items, next_cursor = tasks.list(filter_, authority=authority)
    except (FollowupError, T03Error) as error:
        return _refusal(error)
    return _ok(_bounded_items([_json_safe(item) for item in items], next_cursor))


def followup_show(
    tasks: FollowupTasks, arguments: Mapping[str, object], *, authority: EffectiveAuthority
) -> dict[str, object]:
    validate_followup_arguments("show", arguments)
    try:
        item, evidence, decisions = tasks.show(cast(str, arguments["item_id"]), authority=authority)
    except (FollowupError, T03Error) as error:
        return _refusal(error)
    return _ok(
        {
            "item": _json_safe(item),
            "evidence": [_json_safe(row) for row in evidence],
            "decisions": [_json_safe(row) for row in decisions],
        }
    )


def followup_propose(
    tasks: FollowupTasks, arguments: Mapping[str, object], *, authority: EffectiveAuthority
) -> dict[str, object]:
    validate_followup_arguments("propose", arguments)
    try:
        request = ProposeRequest(
            operation_id=cast(str, arguments["operation_id"]),
            kind=FollowupKind(cast(str, arguments["kind"])),
            summary=unicodedata.normalize("NFC", cast(str, arguments["summary"])),
            project=cast(str | None, arguments.get("project")),
            owner=OwnerKind(cast(str, arguments["owner"])),
            owner_name=cast(str | None, arguments.get("owner_name")),
            due=_date(arguments.get("due")),
            due_evidence_index=cast(int | None, arguments.get("due_evidence_index")),
            evidence=tuple(
                _evidence_ref(entry)
                for entry in cast(list[Mapping[str, object]], arguments["evidence"])
            ),
        )
        item = tasks.propose(request, authority=authority)
    except (FollowupError, T03Error) as error:
        return _refusal(error)
    return _ok({"item": _json_safe(item)})


def followup_add_evidence(
    tasks: FollowupTasks, arguments: Mapping[str, object], *, authority: EffectiveAuthority
) -> dict[str, object]:
    validate_followup_arguments("add_evidence", arguments)
    try:
        request = AddEvidenceRequest(
            operation_id=cast(str, arguments["operation_id"]),
            item_id=cast(str, arguments["item_id"]),
            evidence=_evidence_ref(cast(Mapping[str, object], arguments["evidence"])),
        )
        item = tasks.add_evidence(request, authority=authority)
    except (FollowupError, T03Error) as error:
        return _refusal(error)
    return _ok({"item": _json_safe(item)})


def followup_decide(
    tasks: FollowupTasks, arguments: Mapping[str, object], *, authority: EffectiveAuthority
) -> dict[str, object]:
    validate_followup_arguments("decide", arguments)
    corrections = cast(Mapping[str, object] | None, arguments.get("corrections"))
    try:
        request = DecideRequest(
            operation_id=cast(str, arguments["operation_id"]),
            item_id=cast(str, arguments["item_id"]),
            expected_version=cast(int, arguments["expected_version"]),
            action=DecisionAction(cast(str, arguments["action"])),
            snoozed_until=_date(arguments.get("snoozed_until")),
            corrections=None if corrections is None else _typed_corrections(corrections),
            ticket_key=cast(str | None, arguments.get("ticket_key")),
            ticket_url=cast(str | None, arguments.get("ticket_url")),
            superseded_by=cast(str | None, arguments.get("superseded_by")),
            resolution=cast(
                Literal["evidence", "owner_statement"] | None, arguments.get("resolution")
            ),
        )
        item = tasks.decide(request, authority=authority)
    except (FollowupError, T03Error) as error:
        return _refusal(error)
    return _ok({"item": _json_safe(item)})


def bind_followup_operations(
    tasks: FollowupTasks, *, authority: EffectiveAuthority
) -> dict[str, FollowupOperation]:
    """Close the five operations over one task set and one fixed authority."""
    handlers = {
        "list": followup_list,
        "show": followup_show,
        "propose": followup_propose,
        "add_evidence": followup_add_evidence,
        "decide": followup_decide,
    }

    def bound(
        handler: Callable[..., dict[str, object]],
    ) -> FollowupOperation:
        return lambda arguments: handler(tasks, arguments, authority=authority)

    return {name: bound(handler) for name, handler in handlers.items()}


def followup_session_authority(
    authority: EffectiveAuthority, *, grants: frozenset[str], launcher_policy: bool
) -> EffectiveAuthority:
    """Follow-up operations in an MCP session are Work-only unless a launcher policy says more.

    With a launcher policy the authority already carries that policy's explicit
    ``allowed_read_tiers`` and is returned unchanged. Without one (an implicit
    ``open-brain mcp`` session, owner authority included) the follow-up callbacks run as a
    non-owner Work-only session holding exactly the selected follow-up grants, so Personal
    and Public items are neither listed, shown, cited nor decided. The owner CLI never
    passes through here.
    """
    if launcher_policy:
        return authority
    return replace(
        authority,
        owner=False,
        capabilities=authority.capabilities | (grants & FOLLOWUP_GRANTS),
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
    )


def _require(condition: bool) -> None:
    if not condition:
        raise FollowupServiceError("invalid_arguments")


def _identifier(value: object, prefix: str) -> bool:
    return isinstance(value, str) and re.fullmatch(prefix + "_" + _UUID4, value) is not None


def _prefixed_identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z]{1,32}_" + _UUID4, value) is not None


def _valid_text(value: object, *, maximum_characters: int) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum_characters
        or not value.strip()
        or "\x00" in value
        or any(
            unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}
            for character in value
        )
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _valid_date(value: object) -> bool:
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _closed_list(value: object, allowed: frozenset[str]) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= len(allowed)
        and all(item in allowed for item in value)
        and len(set(cast(list[str], value))) == len(value)
    )


def _validate_evidence(value: object) -> None:
    _require(isinstance(value, Mapping) and set(value) == _EVIDENCE_KEYS)
    entry = cast(Mapping[str, object], value)
    _require(_identifier(entry["capture_id"], "capture"))
    _require(_prefixed_identifier(entry["revision_id"]))
    start, end = entry["quote_start"], entry["quote_end"]
    _require(type(start) is int and type(end) is int and 0 <= start < end)
    digest = entry["quote_sha256"]
    _require(isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None)
    _require(entry["role"] in _ROLES)


def _validate_corrections(value: object) -> None:
    _require(isinstance(value, Mapping) and bool(value) and set(value) <= _CORRECTION_KEYS)
    corrections = cast(Mapping[str, object], value)
    for field, item in corrections.items():
        if field == "owner":
            _require(item in _OWNERS)
        elif field == "summary":
            _require(_valid_text(item, maximum_characters=MAX_SUMMARY_CHARS))
        elif field in {"owner_name", "project"}:
            _require(
                item is None or _valid_text(item, maximum_characters=MAX_FOLLOWUP_TEXT_CHARACTERS)
            )
        elif field == "due":
            _require(item is None or _valid_date(item))
        elif field == "due_evidence_id":
            _require(
                item is None or _valid_text(item, maximum_characters=MAX_FOLLOWUP_KEY_CHARACTERS)
            )
    # A stated date names the evidence row that states it, and only then (engine rule).
    _require(
        (corrections.get("due") is not None) == (corrections.get("due_evidence_id") is not None)
    )


def _typed_corrections(value: Mapping[str, object]) -> dict[str, object]:
    typed: dict[str, object] = dict(value)
    if "owner" in typed:
        typed["owner"] = OwnerKind(cast(str, typed["owner"]))
    if "due" in typed:
        typed["due"] = _date(typed["due"])
    if "summary" in typed:
        typed["summary"] = unicodedata.normalize("NFC", cast(str, typed["summary"]))
    return typed


def _evidence_ref(entry: Mapping[str, object]) -> EvidenceRef:
    return EvidenceRef(
        capture_id=cast(str, entry["capture_id"]),
        revision_id=cast(str, entry["revision_id"]),
        quote_start=cast(int, entry["quote_start"]),
        quote_end=cast(int, entry["quote_end"]),
        quote_sha256=cast(str, entry["quote_sha256"]),
        role=EvidenceRole(cast(str, entry["role"])),
    )


def _strings(value: object) -> list[str]:
    return [] if value is None else cast(list[str], value)


def _date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(cast(str, value))


def _json_safe(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_safe(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | frozenset | set):
        return [_json_safe(item) for item in value]
    return value


def _bounded_items(rows: list[object], next_cursor: str | None) -> dict[str, object]:
    selected: list[object] = []
    size = 1024
    for row in rows:
        row_size = len(json.dumps(row, ensure_ascii=True, separators=(",", ":"))) + 1
        if size + row_size > MAX_FOLLOWUP_LIST_JSON_BYTES:
            break
        selected.append(row)
        size += row_size
    return {
        "items": selected,
        "next_cursor": next_cursor,
        "truncated": len(selected) < len(rows),
    }


def _ok(data: dict[str, object]) -> dict[str, object]:
    return {"outcome": "ok", "data": data, "code": None}


def _refusal(error: Exception) -> dict[str, object]:
    code = cast(str, getattr(error, "code", "invalid_request"))
    if isinstance(error, FollowupError) and code not in FOLLOWUP_ERROR_CODES:
        code = "invalid_request"
    detail: dict[str, object] | None = None
    current_version = getattr(error, "current_version", None)
    existing_item_id = getattr(error, "existing_item_id", None)
    if code == "version_conflict" and current_version is not None:
        detail = {"current_version": current_version}
    elif code == "duplicate_item" and existing_item_id is not None:
        detail = {"existing_item_id": existing_item_id}
    return {"outcome": "refused", "data": detail, "code": code}
