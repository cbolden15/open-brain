"""Executable closed T03 DTO grammar. Contract proof, not a task implementation."""

import json
import re
from pathlib import Path
from typing import Any, NoReturn

CONTRACT = json.loads(Path(__file__).with_name("strict-contract.schema.json").read_text())


def parse(raw: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("invalid_json")
            result[key] = value
        return result

    def reject(_: str) -> NoReturn:
        raise ValueError("invalid_json")

    decoded = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return json.loads(decoded, object_pairs_hook=pairs, parse_constant=reject)


def validate(name: str, value: Any) -> Any:
    def check(rule: dict[str, Any], value: Any) -> None:
        if "ref" in rule:
            return check(CONTRACT["definitions"][rule["ref"]], value)
        if "anyOf" in rule:
            for option in rule["anyOf"]:
                try:
                    check(option, value)
                    return
                except ValueError:
                    pass
            raise ValueError("invalid_dto")
        if "enum" in rule:
            if not any(type(value) is type(item) and value == item for item in rule["enum"]):
                raise ValueError("invalid_dto")
            return
        kind = rule["type"]
        types: dict[str, Any] = {
            "object": dict,
            "array": list,
            "string": str,
            "integer": int,
            "boolean": bool,
            "null": type(None),
        }
        if type(value) is not types[kind]:
            raise ValueError("invalid_dto")
        if kind == "object":
            props = rule["properties"]
            if set(value) - set(props) or set(props) - set(rule["optional"]) - set(value):
                raise ValueError("invalid_dto")
            for key, child in value.items():
                check(props[key], child)
            semantic(value)
        elif kind == "array":
            if not rule["min"] <= len(value) <= rule["max"]:
                raise ValueError("invalid_dto")
            if rule["unique"] and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
                raise ValueError("invalid_dto")
            for child in value:
                check(rule["items"], child)
        elif kind == "integer":
            if not rule["min"] <= value <= rule["max"]:
                raise ValueError("invalid_dto")
        elif kind == "string":
            if "\0" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                raise ValueError("invalid_dto")
            if not rule.get("min", 0) <= len(value) <= rule.get("max", 1048576):
                raise ValueError("invalid_dto")
            if "id_prefixes" in rule:
                pattern = (
                    "(?:"
                    + "|".join(rule["id_prefixes"])
                    + ")_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                )
                if re.fullmatch(pattern, value) is None:
                    raise ValueError("invalid_dto")
            if rule.get("format") == "query" and (
                not any(c.isalnum() for c in value) or len(value.encode()) > 4096
            ):
                raise ValueError("invalid_dto")
            if (
                rule.get("format") == "timestamp"
                and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value) is None
            ):
                raise ValueError("invalid_dto")

    check(CONTRACT["definitions"][name], value)
    return value


def semantic(value: dict[str, Any]) -> None:
    def require(condition: bool) -> None:
        if not condition:
            raise ValueError("invalid_dto")

    if "complete" in value:
        require(value["complete"] == (value["next_cursor"] is None))
    if "representative_capture_id" in value:
        require(value["representative_capture_id"] == value["capture_ids"][0])
    if "record_type" in value:
        if value["record_type"] == "source":
            require(
                value["record_id"].startswith("capture_")
                and value["record_id"] == value["revision_id"]
                and value["source_id"] is not None
            )
            require(value["provenance"]["capture_ids"] == [value["record_id"]])
        else:
            require(
                value["record_id"].startswith("page_")
                and value["revision_id"].startswith("revision_")
                and value["source_id"] is None
            )
    if "content" in value:
        length = len(value["content"]["text"].encode())
        require(value["end_byte"] - value["start_byte"] == length)
        require(value["complete"] or length > 0)
    if "required_grants" in value:
        require(value["required_grants"] == [CONTRACT["grants"][value["name"]]])
    if "operations" in value:
        require(len({item["name"] for item in value["operations"]}) == len(value["operations"]))
