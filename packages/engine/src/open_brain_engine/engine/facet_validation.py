"""Closed facet grammar, default expansion, and strict JSON token admission.

The packaged schema is a draft C0 contract, separate from historical schemas.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

CONTRACT: dict[str, Any] = json.loads(Path(__file__).with_name("facet_schema.json").read_text())
MAX_INTEGER = 2**53 - 1


def parse(raw: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("invalid_json")
            result[key] = value
        return result

    def reject(_: str) -> NoReturn:
        raise ValueError("invalid_json")

    def integer(token: str) -> int:
        # Bound tokens before int(): arbitrarily long JSON integers must fail safely.
        if len(token.lstrip("-")) > 16:
            raise ValueError("invalid_json")
        value = int(token)
        if abs(value) > MAX_INTEGER:
            raise ValueError("invalid_json")
        return value

    decoded = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
    decoded.encode("utf-8", errors="strict")
    return json.loads(
        decoded,
        object_pairs_hook=pairs,
        parse_int=integer,
        parse_float=reject,
        parse_constant=reject,
    )


def validate(name: str, value: Any) -> Any:
    """Validate and return a detached normalized value; never modify caller data."""
    normalize = name.endswith(".request") or name == "selector.input"

    def check(rule: dict[str, Any], child: Any) -> Any:
        if "ref" in rule:
            return check(CONTRACT["definitions"][rule["ref"]], child)
        if "anyOf" in rule:
            for option in rule["anyOf"]:
                try:
                    return check(option, child)
                except ValueError:
                    pass
            raise ValueError("invalid_dto")
        if "enum" in rule:
            if not any(type(child) is type(item) and child == item for item in rule["enum"]):
                raise ValueError("invalid_dto")
            return child
        kind = rule["type"]
        types: dict[str, Any] = {
            "object": dict,
            "array": list,
            "string": str,
            "integer": int,
            "boolean": bool,
            "null": type(None),
        }
        if type(child) is not types[kind]:
            raise ValueError("invalid_dto")
        if kind == "object":
            props = rule["properties"]
            if set(child) - set(props) or set(props) - set(rule["optional"]) - set(child):
                raise ValueError("invalid_dto")
            expanded = {**rule.get("defaults", {}), **child} if normalize else child
            result = {key: check(props[key], item) for key, item in expanded.items()}
            semantic(result, rule.get("semantic"))
            return result
        if kind == "array":
            array_result = [check(rule["items"], item) for item in child]
            if normalize and rule.get("normalize_set"):
                array_result = sorted(set(array_result))
            if not rule["min"] <= len(array_result) <= rule["max"]:
                raise ValueError("invalid_dto")
            if rule["unique"] and len({json.dumps(x, sort_keys=True) for x in array_result}) != len(
                array_result
            ):
                raise ValueError("invalid_dto")
            if rule.get("sorted") and array_result != sorted(array_result):
                raise ValueError("invalid_dto")
            return array_result
        if kind == "integer" and not rule["min"] <= child <= rule["max"]:
            raise ValueError("invalid_dto")
        if kind == "string":
            if "\0" in child or any(0xD800 <= ord(c) <= 0xDFFF for c in child):
                raise ValueError("invalid_dto")
            format_ = rule.get("format")
            if format_ == "name":
                if any(unicodedata.category(c) == "Cc" for c in child):
                    raise ValueError("invalid_dto")
                normalized = unicodedata.normalize("NFC", child).strip()
                if normalize:
                    child = normalized
                elif child != normalized:
                    raise ValueError("invalid_dto")
            if format_ in {"name", "operation_id", "title"} and (
                not child.strip() or any(unicodedata.category(c) == "Cc" for c in child)
            ):
                raise ValueError("invalid_dto")
            if not rule.get("min", 0) <= len(child) <= rule.get("max", 1048576):
                raise ValueError("invalid_dto")
            if len(child.encode("utf-8")) > rule.get("max_bytes", 1048576):
                raise ValueError("invalid_dto")
            if "id_prefixes" in rule:
                prefixes = "|".join(rule["id_prefixes"])
                facet_only = child.startswith(
                    ("facetdim_", "facetval_", "facetquery_", "facetevent_")
                )
                uuid = (
                    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
                    if facet_only
                    else r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                )
                if re.fullmatch(f"(?:{prefixes})_{uuid}", child) is None:
                    raise ValueError("invalid_dto")
            if format_ == "timestamp":
                if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", child) is None:
                    raise ValueError("invalid_dto")
                datetime.strptime(child, "%Y-%m-%dT%H:%M:%S.%fZ")
        return child

    return check(CONTRACT["definitions"][name], value)


def semantic(value: dict[str, Any], rule: str | None) -> None:
    def require(condition: bool) -> None:
        if not condition:
            raise ValueError("invalid_dto")

    if rule == "assignment_patch":
        add, remove = value["add_value_ids"], value["remove_value_ids"]
        require(bool(add or remove) and not set(add) & set(remove))
        source = value["record_id"].startswith("capture_")
        require(value["expected_revision_id"].startswith("capture_" if source else "revision_"))
        if source:
            require(value["expected_revision_id"] == value["record_id"])
    if rule == "query_page":
        require(("selector" in value) != ("query_id" in value))
        require(("query_id" in value) == ("expected_query_version" in value))
    if rule == "publication":
        create = value["target_page_id"] is None
        require((value["expected_page_revision_id"] is None) == create)
        require((value["destination_space_id"] is not None) == create)
    if "complete" in value:
        require(value["complete"] == (value["next_cursor"] is None))
    if "status" in value and value["status"] in {"applied", "unchanged"}:
        require((value["event_id"] is None) == (value["status"] == "unchanged"))
    if "effective_archived" in value:
        require(not value["archived"] or value["effective_archived"])
    if "selection_version" in value:
        require(value["selection_version"] <= value["version"])
    if "subject_kind" in value:
        require(
            value["subject_id"].startswith(
                "source_" if value["subject_kind"] == "source" else "page_"
            )
        )
    if "attachment" in value and "subject" in value:
        if value["subject"]["kind"] == "source":
            require(value["attachment"] is None)
        elif value["attachment"] is not None:
            require(value["attachment"]["page_id"] == value["subject"]["id"])
    if "memberships" in value:
        ids = [item["value_id"] for item in value["memberships"]]
        require(len(ids) == len(set(ids)))
    if "subject" in value and "record_id" in value:
        source = value["subject"]["kind"] == "source"
        require(value["record_id"].startswith("capture_" if source else "page_"))
        require(value["revision_id"].startswith("capture_" if source else "revision_"))
        require(
            value["record_id"] == value["revision_id"]
            if source
            else value["record_id"] == value["subject"]["id"]
        )
    if "record" in value and "classification_version" in value:
        record, subject = value["record"], value["subject"]
        require(record["record_type"] == subject["kind"])
        require(
            subject["id"]
            == (record["source_id"] if subject["kind"] == "source" else record["record_id"])
        )
    if "representative_capture_id" in value:
        require(value["representative_capture_id"] == value["capture_ids"][0])
    if "record_type" in value:
        source = value["record_type"] == "source"
        require(value["record_id"].startswith("capture_" if source else "page_"))
        require(value["revision_id"].startswith("capture_" if source else "revision_"))
        require((value["source_id"] is not None) == source)
        if source:
            require(value["record_id"] == value["revision_id"])
            require(value["provenance"]["capture_ids"] == [value["record_id"]])

    if "entity" in value and "after" in value:
        entity = value["entity"]
        for row in (value["before"], value["after"]):
            if row is None:
                continue
            kind = entity["kind"]
            if kind == "subject":
                identity = {"kind": row["subject_kind"], "id": row["subject_id"]}
            elif kind == "assignment":
                identity = {key: row[key] for key in ("subject_kind", "subject_id", "value_id")}
            else:
                identity = row[
                    {
                        "dimension": "dimension_id",
                        "value": "value_id",
                        "query": "query_id",
                        "attachment": "page_id",
                    }[kind]
                ]
            require(identity == entity["id"])
    if "conflicts" in value:
        for conflict in value["conflicts"]:
            if conflict["code"] in {"unknown_name", "ambiguous_name"}:
                require(conflict["value_id"] is None)
    if "status" in value and value["status"] == "proposed":
        create = value["operation"] == "create"
        require((value["target_page_id"] is None) == create)
        if not create:
            require(value["page_id"] == value["target_page_id"])
