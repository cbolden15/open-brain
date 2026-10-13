"""Unregistered C0 facet evidence grammar and pure fixture assertion checks.

These checks do not admit a live Brain, import Portable data, or implement FacetTasks.
Source/page existence and current authority belong to those future consumers.
"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes

EVIDENCE_SCHEMA: dict[str, Any] = json.loads(
    Path(__file__).with_name("facet_evidence_schema.json").read_text(encoding="utf-8")
)
SIDECAR_PATHS: dict[str, str] = EVIDENCE_SCHEMA["x-sidecar-paths"]
_ROWS = {
    "dimension": ("dimensions", "dimension_id"),
    "value": ("values", "value_id"),
    "subject": ("subjects", "subject_id"),
    "assignment": ("assignments", "value_id"),
    "query": ("queries", "query_id"),
    "attachment": ("page_queries", "page_id"),
}


class FacetEvidenceError(ValueError):
    """A draft evidence validation failure, distinct from wire or import errors."""

    def __init__(self, code: str = "invalid_evidence") -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str = "invalid_evidence") -> None:
    if not condition:
        raise FacetEvidenceError(code)


def canonical_evidence_bytes(value: object) -> bytes:
    """Use the same RFC8785 canonical bytes as existing Portable evidence."""
    try:
        return portable_canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise FacetEvidenceError() from error


def request_sha256(method: str, arguments: dict[str, object]) -> str:
    """Hash normalized arguments, including expected versions and operation ID."""
    validate_evidence("semantic_request", {"method": method, "arguments": arguments})
    return sha256(canonical_evidence_bytes({"method": method, "arguments": arguments})).hexdigest()


def _check(rule: dict[str, Any], value: Any) -> None:
    if "$ref" in rule:
        name = rule["$ref"].removeprefix("#/$defs/")
        _check(EVIDENCE_SCHEMA["$defs"][name], value)
        _semantic(name, value)
        return
    if "oneOf" in rule or "anyOf" in rule:
        matches = 0
        for option in rule.get("oneOf", rule.get("anyOf", [])):
            try:
                _check(option, value)
                matches += 1
            except FacetEvidenceError:
                pass
        _require(matches == 1 if "oneOf" in rule else matches > 0)
        return
    if "const" in rule:
        _require(type(value) is type(rule["const"]) and value == rule["const"])
    if "enum" in rule:
        _require(any(type(value) is type(item) and value == item for item in rule["enum"]))
    kind = rule.get("type")
    if kind is None:
        return
    types = {"object": dict, "array": list, "string": str,
             "integer": int, "boolean": bool, "null": type(None)}
    _require(type(value) is types[kind])
    if kind == "object":
        _require(set(value) == set(rule["properties"]))
        for key, child in value.items():
            _check(rule["properties"][key], child)
    elif kind == "array":
        _require(rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", 2**53 - 1))
        if rule.get("uniqueItems"):
            _require(len({canonical_evidence_bytes(item) for item in value}) == len(value))
        for item in value:
            _check(rule["items"], item)
    elif kind == "integer":
        _require(rule["minimum"] <= value <= rule["maximum"])
    elif kind == "string":
        _require(not any(0xD800 <= ord(c) <= 0xDFFF for c in value))
        _require(rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 2**53 - 1))
        if "pattern" in rule:
            _require(re.fullmatch(rule["pattern"], value) is not None)
        if rule.get("format") == "date-time":
            try:
                datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
            except ValueError as error:
                raise FacetEvidenceError() from error


def _key(kind: str, row: dict[str, Any]) -> tuple[str, ...]:
    if kind in {"subject", "assignment"}:
        key = (row["subject_kind"], row["subject_id"])
        return (*key, row["value_id"]) if kind == "assignment" else key
    return (row[_ROWS[kind][1]],)


def _entity_id(kind: str, row: dict[str, Any]) -> object:
    if kind == "subject":
        return {"kind": row["subject_kind"], "id": row["subject_id"]}
    if kind == "assignment":
        return {key: row[key] for key in ("subject_kind", "subject_id", "value_id")}
    return row[_ROWS[kind][1]]


def _sorted_unique(values: list[Any]) -> None:
    _require(values == sorted(set(values)))


def _semantic(name: str, value: Any) -> None:
    if name == "name":
        _require(value == unicodedata.normalize("NFC", value).strip() and bool(value))
    elif name in {"operation_id", "principal_id"}:
        _require(bool(value.strip()))
    elif name == "selector":
        for items in value.values():
            _sorted_unique(items)
    elif name.endswith("_row"):
        if "subject_kind" in value:
            prefix = "source_" if value["subject_kind"] == "source" else "page_"
            _require(value["subject_id"].startswith(prefix))
        if "created_at" in value:
            _require(value["created_at"] <= value["updated_at"])
        if name == "query_row":
            _require(value["selection_version"] <= value["version"])
    elif name == "change":
        entity = value["entity"]
        kind = entity["kind"]
        _require(entity["id"] == _entity_id(kind, value["after"]))
        if value["before"] is not None:
            _require(entity["id"] == _entity_id(kind, value["before"]))
    elif name.endswith("_request") and name != "semantic_request":
        if name.endswith("create_request"):
            _require(value["expected_version"] == 0)
        if name == "facet_assignment_patch_request":
            add, remove = value["add_value_ids"], value["remove_value_ids"]
            _sorted_unique(add)
            _sorted_unique(remove)
            _require(bool(add or remove) and not set(add) & set(remove))
            _require(value["record_id"].startswith("page_") ==
                     value["expected_revision_id"].startswith("revision_"))
            if value["record_id"].startswith("capture_"):
                _require(value["record_id"] == value["expected_revision_id"])
    elif name == "receipt":
        _require((value["status"] == "unchanged") == (value["event_id"] is None))
    elif name == "operation":
        request = value["request_json"]
        receipt = value["receipt_json"]
        _require(value["operation_id"] == request["arguments"]["operation_id"] ==
                 receipt["operation_id"])
        _require(value["event_id"] == receipt["event_id"])
        if receipt["status"] == "unchanged":
            _require(not request["method"].endswith(".create"))
            _require(receipt["version"] == request["arguments"].get("expected_version"))
        _require(value["request_sha256"] == sha256(canonical_evidence_bytes(request)).hexdigest(),
                 "request_digest_mismatch")
    elif name == "event":
        _event_semantics(value)
    elif name in {"replay_state", "state_sidecar"}:
        _state_semantics(value)
    elif name == "events_sidecar":
        events = value["events"]
        _require(all(event["brain_id"] == value["brain_id"] for event in events))
        sequences = [event["sequence"] for event in events]
        _sorted_unique(sequences)
        for field in ("event_id", "operation_id"):
            _require(len({event[field] for event in events}) == len(events))
    elif name == "operations_sidecar":
        _sorted_unique([operation["operation_id"] for operation in value["operations"]])
    elif name == "review_binding_v5":
        _binding_semantics(value)


def _event_semantics(event: dict[str, Any]) -> None:
    changes = event["changes"]
    kind, action = event["kind"].split(".")
    if kind == "assignment":
        _require(changes[0]["entity"]["kind"] == "subject" and len(changes) >= 2)
        member_ids = []
        for change in changes[1:]:
            _require(change["entity"]["kind"] == "assignment")
            _require(_key("subject", change["after"]) == _key("subject", changes[0]["after"]))
            member_ids.append(change["after"]["value_id"])
        _sorted_unique(member_ids)
    else:
        expected = "attachment" if event["kind"] == "query.attach" else kind
        _require(len(changes) == 1 and changes[0]["entity"]["kind"] == expected)
    for change in changes:
        before, after = change["before"], change["after"]
        entity_kind = change["entity"]["kind"]
        version = "membership_version" if entity_kind == "assignment" else "version"
        _require(after[version] == (0 if before is None else before[version]) + 1)
        if action == "create":
            _require(before is None)
        if before is None:
            _require(action in {"create", "patch", "attach"})
            if "archived" in after:
                _require(after["archived"] is False)
                _require(after["created_at"] == event["recorded_at"])
            if entity_kind == "query":
                _require(after["selection_version"] == 1)
            if entity_kind == "assignment":
                _require(after["present"] is True)
        else:
            allowed = {"version", "updated_at"}
            if action == "rename":
                allowed.add("name")
                _require(after["name"] != before["name"])
            elif action == "archive":
                allowed.add("archived")
                _require(not before["archived"] and after["archived"])
            elif action == "update":
                allowed |= {"name", "selector", "selection_version"}
                _require(not before["archived"] and not after["archived"])
                selector_change = before["selector"] != after["selector"]
                _require(after["selection_version"] == before["selection_version"] +
                         int(selector_change))
                _require(selector_change or before["name"] != after["name"])
            elif action == "attach":
                allowed.add("query_id")
                _require(before["query_id"] != after["query_id"])
            elif entity_kind == "assignment":
                allowed = {"present", "membership_version", "last_event_sequence"}
                _require(before["present"] != after["present"])
            _require(all(after[key] == val for key, val in before.items() if key not in allowed))
        if "updated_at" in after:
            _require(after["updated_at"] == event["recorded_at"])
        if entity_kind == "assignment":
            _require(after["last_event_sequence"] == event["sequence"])


def _state_semantics(state: dict[str, Any]) -> None:
    for kind, (table, _field) in _ROWS.items():
        keys = [_key(kind, row) for row in state[table]]
        _require(keys == sorted(set(keys)))
    dimensions = {row["dimension_id"] for row in state["dimensions"]}
    values = {row["value_id"] for row in state["values"]}
    subjects = {_key("subject", row) for row in state["subjects"]}
    queries = {row["query_id"] for row in state["queries"]}
    _require(all(row["dimension_id"] in dimensions for row in state["values"]))
    _require(all(row["value_id"] in values and _key("subject", row) in subjects
                 for row in state["assignments"]))
    _require(all(set(row["selector"]["all_value_ids"]) <= values for row in state["queries"]))
    _require(all(row["query_id"] in queries for row in state["page_queries"]))
    for table in ("dimensions", "values", "queries"):
        names = [(row.get("dimension_id") if table == "values" else None,
                  unicodedata.normalize("NFC", row["name"].casefold())) for row in state[table]]
        _require(len(names) == len(set(names)))
    _require(all(sum(row["present"] for row in state["assignments"]
                     if _key("subject", row) == subject) <= 128 for subject in subjects))


def _binding_semantics(binding: dict[str, Any]) -> None:
    selected, provenance = binding["selected_capture_ids"], binding["provenance"]
    _require(set(selected) <= set(provenance))
    _require([row["capture_id"] for row in binding["source_states"]] == provenance)
    states = binding["facet_source_states"]
    _require([row["capture_id"] for row in states] == selected)
    _require(len({row["source_id"] for row in states}) == len(states))
    for state in states:
        _require([pair["value_id"] for pair in state["memberships"]] ==
                 binding["selector"]["all_value_ids"])
    _require(not binding["selector"]["record_types"] or
             "source" in binding["selector"]["record_types"])
    _require(binding["query_selection_version"] <= binding["query_version"])
    create = binding["operation"] == "create"
    _require(not create or selected == provenance)
    _require((binding["attachment_version"] == 0) == create)
    _require((binding["expected_publication_id"] is None) == create)
    _require((binding["expected_page_sha256"] is None) == create)
    role = binding["role_claim"]
    _require(role["actor_id"] == binding["actor_id"] and
             role["tenant_id"] == binding["tenant_id"])
    _sorted_unique(role["capabilities"])


def validate_evidence(name: str, value: object) -> Any:
    """Validate a closed draft definition and its local semantic invariants."""
    if name not in EVIDENCE_SCHEMA["$defs"]:
        raise FacetEvidenceError("unknown_evidence_schema")
    _check({"$ref": "#/$defs/" + name}, value)
    return value


def _operation_effect(
    event: dict[str, Any], arguments: dict[str, Any],
    rows: dict[str, dict[tuple[str, ...], Any]],
) -> None:
    first = event["changes"][0]
    after = first["after"]
    for field in ("name", "selector", "dimension_id", "value_id", "query_id", "page_id"):
        if field in arguments and field in after:
            _require(arguments[field] == after[field], "operation_event_mismatch")
    if event["kind"] == "value.create":
        parent = rows["dimension"].get((after["dimension_id"],))
        _require(parent is not None and not parent["archived"] and
                 parent["version"] == arguments["expected_dimension_version"],
                 "operation_event_mismatch")
    if event["kind"] == "query.attach":
        query = rows["query"].get((after["query_id"],))
        _require(query is not None and not query["archived"], "operation_event_mismatch")
        assert query is not None
        query_version_field = (
            "expected_query_version" if "expected_query_version" in arguments else None
        )
        if query_version_field is not None:
            _require(query["version"] == arguments[query_version_field], "operation_event_mismatch")
        else:
            _require(query["selection_version"] == arguments["query_selection_version"],
                     "operation_event_mismatch")
    if event["kind"] == "assignment.patch":
        subject = _key("subject", after)
        changes = {change["after"]["value_id"]: change for change in event["changes"][1:]}
        requested = set(arguments["add_value_ids"]) | set(arguments["remove_value_ids"])
        _require(set(changes) <= requested, "operation_event_mismatch")
        for value_id in requested:
            membership = rows["assignment"].get((*subject, value_id))
            present = value_id in arguments["add_value_ids"]
            if value_id in changes:
                _require(changes[value_id]["after"]["present"] == present,
                         "operation_event_mismatch")
                value = rows["value"].get((value_id,))
                _require(value is not None, "operation_event_mismatch")
                assert value is not None
                parent = rows["dimension"].get((value["dimension_id"],))
                _require(parent is not None, "operation_event_mismatch")
                assert parent is not None
                if present:
                    _require(not value["archived"] and not parent["archived"],
                             "operation_event_mismatch")
            else:
                _require(bool(membership and membership["present"]) == present,
                         "operation_event_mismatch")


def _unchanged_reachable(
    operation: dict[str, Any], rows: dict[str, dict[tuple[str, ...], Any]],
) -> bool:
    """Match a no-op at one replay boundary, including before later mutations.

    No-op receipts have no event sequence. Their expected version and complete
    requested effect must nevertheless describe a state the ledger reached.
    """
    method = operation["request_json"]["method"]
    args = operation["request_json"]["arguments"]
    receipt = operation["receipt_json"]
    kind, action = method.removeprefix("facet.").split(".", maxsplit=1)
    if kind == "assignment":
        subject_kind = "source" if args["record_id"].startswith("capture_") else "canonical"
        key = (subject_kind, receipt["subject_id"])
        subject = rows["subject"].get(key)
        if args["expected_version"] != (0 if subject is None else subject["version"]):
            return False
        for present, field in ((True, "add_value_ids"), (False, "remove_value_ids")):
            for value_id in args[field]:
                membership = rows["assignment"].get((*key, value_id))
                if (value_id,) not in rows["value"] or bool(
                    membership and membership["present"]
                ) != present:
                    return False
        return True
    if kind == "query" and action == "attach":
        row = rows["attachment"].get((args["page_id"],))
        query = rows["query"].get((args["query_id"],))
        return bool(
            row and query and receipt["subject_id"] == args["page_id"]
            and row["version"] == args["expected_version"]
            and row["query_id"] == args["query_id"]
            and query["version"] == args["expected_query_version"]
        )
    if kind not in {"dimension", "value", "query"} or action not in {
        "rename", "update", "archive",
    }:
        return False
    identity = args[f"{kind}_id"]
    row = rows[kind].get((identity,))
    if not row or receipt["subject_id"] != identity or row["version"] != args["expected_version"]:
        return False
    if action == "archive":
        return bool(row["archived"])
    if action == "update":
        return bool(not row["archived"] and row["name"] == args["name"]
                    and row["selector"] == args["selector"])
    return bool(row["name"] == args["name"])


def validate_portable_facet_sidecars(
    events: object, operations: object, state: object,
) -> dict[str, object]:
    """Assert synthetic sidecar agreement by replaying rows; never touch storage.

    Runtime authority, head identity, source/page existence, import fencing and
    publication state-machine admission must still be proven by E/P/X consumers.
    """
    event_doc = validate_evidence("events_sidecar", events)
    operation_doc = validate_evidence("operations_sidecar", operations)
    state_doc = validate_evidence("state_sidecar", state)
    _require(event_doc["brain_id"] == operation_doc["brain_id"] == state_doc["brain_id"])
    rows: dict[str, dict[tuple[str, ...], Any]] = {kind: {} for kind in _ROWS}
    by_operation = {row["operation_id"]: row for row in operation_doc["operations"]}
    unmatched = [operation for operation in operation_doc["operations"]
                 if operation["receipt_json"]["status"] == "unchanged"]
    unmatched = [operation for operation in unmatched if not _unchanged_reachable(operation, rows)]
    linked: set[str] = set()
    for event in event_doc["events"]:
        operation = by_operation.get(event["operation_id"])
        _require(operation is not None, "operation_event_mismatch")
        assert operation is not None
        _require(operation["event_id"] == event["event_id"], "operation_event_mismatch")
        method = operation["request_json"]["method"]
        expected_method = "facet." + event["kind"]
        _require(method == expected_method or
                 method == "facet.query.attach.publication" and event["kind"] == "query.attach",
                 "operation_event_mismatch")
        first = event["changes"][0]
        receipt = operation["receipt_json"]
        arguments = operation["request_json"]["arguments"]
        _require(receipt["status"] == "applied" and
                 receipt["subject_id"] == first["after"][_ROWS[first["entity"]["kind"]][1]] and
                 receipt["version"] == first["after"]["version"], "operation_event_mismatch")
        if "expected_version" in arguments:
            _require(arguments["expected_version"] ==
                     (0 if first["before"] is None else first["before"]["version"]),
                     "operation_event_mismatch")
        _operation_effect(event, arguments, rows)
        for change in event["changes"]:
            kind, after = change["entity"]["kind"], change["after"]
            key = _key(kind, after)
            _require(rows[kind].get(key) == change["before"], "replay_before_mismatch")
            rows[kind][key] = deepcopy(after)
        linked.add(event["operation_id"])
        # Validate vocabulary/reference constraints at every event boundary.
        intermediate = {"schema_version": 1, "brain_id": event_doc["brain_id"]}
        intermediate.update({table: [rows[kind][key] for key in sorted(rows[kind])]
                             for kind, (table, _field) in _ROWS.items()})
        validate_evidence("replay_state", intermediate)
        unmatched = [operation for operation in unmatched
                     if not _unchanged_reachable(operation, rows)]
    _require(not unmatched, "operation_event_mismatch")
    for operation in operation_doc["operations"]:
        _require((operation["event_id"] is not None) == (operation["operation_id"] in linked),
                 "operation_event_mismatch")
    projected: dict[str, object] = {"schema_version": 1, "brain_id": event_doc["brain_id"]}
    projected.update({table: [rows[kind][key] for key in sorted(rows[kind])]
                      for kind, (table, _field) in _ROWS.items()})
    _require(projected == state_doc, "replay_state_mismatch")
    return projected


__all__ = [
    "EVIDENCE_SCHEMA", "SIDECAR_PATHS", "FacetEvidenceError", "canonical_evidence_bytes",
    "request_sha256", "validate_evidence", "validate_portable_facet_sidecars",
]
