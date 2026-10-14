"""O0 pure plugin codecs. No live dispatch, advertisement, or authority selection.

B must bound its reader before calling these helpers and use response_size for
engine page admission before effects. A serialized reply is not a commit guard.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from open_brain_engine.engine.facet_validation import CONTRACT, parse, validate

RESOLVE_OPERATION = "workspace.record.resolve"
OPERATIONS = (*CONTRACT["operations"], RESOLVE_OPERATION)
MAX_REQUEST_BYTES = 1048576
PROTOCOL = {"protocol": "open-brain-client", "protocol_version": 1}
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


def limits(operation: str) -> tuple[int, int]:
    if operation not in OPERATIONS:
        raise ValueError("unsupported_capability")
    page = operation.endswith(".list") or operation == "facet.query.page"
    read = page or operation.rsplit(".", 1)[-1] in {"get", "state", "preview", "resolve"}
    return (
        1048576 if operation == "review.propose_from_facet_query" else 65536,
        250000 if page else 1048576 if read else 4096,
    )


def capabilities(available_operations: Iterable[str]) -> dict[str, object]:
    """Describe only explicitly supplied, registered and authorized operations."""
    available = set(available_operations)
    return {
        "version": 1,
        "operations": [
            {"operation": op, "dto_versions": [1],
             "request_bytes": limits(op)[0], "response_bytes": limits(op)[1]}
            for op in sorted(available.intersection(OPERATIONS))
        ],
    }


def _exact(value: Any, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("invalid_arguments")
    return value


def _id(value: Any, prefix: str, *, v4: bool = False) -> bool:
    return isinstance(value, str) and re.fullmatch(
        prefix + "_" + (_UUID4 if v4 else _UUID), value
    ) is not None


def managed_request(value: Any) -> dict[str, Any]:
    item = _exact(value, {"dto_version", "workspace_id", "relative_path"})
    path = item["relative_path"]
    if (
        type(item["dto_version"]) is not int or item["dto_version"] != 1
        or not _id(item["workspace_id"], "workspace")
        or not isinstance(path, str) or len(path.encode("utf-8")) > 4096
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in path)
        or "\\" in path or re.match(r"^[A-Za-z]:", path)
        or not path.lower().endswith(".md")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("invalid_arguments")
    return dict(item)


def managed_response(value: Any) -> dict[str, Any]:
    item = _exact(value, {
        "dto_version", "status", "workspace_id", "record_id", "revision_id", "subject",
    })
    subject = _exact(item["subject"], {"kind", "id"})
    source = subject["kind"] == "source"
    if (
        type(item["dto_version"]) is not int or item["dto_version"] != 1
        or item["status"] != "ok" or not _id(item["workspace_id"], "workspace")
        or subject["kind"] not in {"source", "canonical"}
        or not _id(subject["id"], "source" if source else "page")
        or not _id(item["record_id"], "capture" if source else "page")
        or not _id(item["revision_id"], "capture" if source else "revision")
        or (item["record_id"] != (item["revision_id"] if source else subject["id"]))
    ):
        raise ValueError("invalid_arguments")
    return {**item, "subject": dict(subject)}


def _frame(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8") + b"\n"


def _request_id(value: Any) -> None:
    if not _id(value, "plugin", v4=True):
        raise ValueError("invalid_request")


def decode_request(raw: bytes, *, available_operations: Iterable[str]) -> dict[str, Any]:
    """Admit one complete UTF-8 frame before any engine invocation."""
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n") or b"\n" in raw[:-1]:
        raise ValueError("invalid_request")
    value = _exact(parse(raw), {*PROTOCOL, "request_id", "operation", "arguments"})
    if any(type(value[key]) is not type(expected) or value[key] != expected
           for key, expected in PROTOCOL.items()):
        raise ValueError("incompatible_protocol")
    _request_id(value["request_id"])
    operation = value["operation"]
    if not isinstance(operation, str) or operation not in set(available_operations):
        raise ValueError("unsupported_capability")
    if len(raw) > limits(operation)[0]:
        raise ValueError("invalid_arguments")
    arguments = (managed_request(value["arguments"]) if operation == RESOLVE_OPERATION
                 else validate(operation + ".request", value["arguments"]))
    return {**value, "arguments": arguments}


def response_size(request_id: str, result: Mapping[str, Any]) -> int:
    """Exact bytes for a successful reply including its newline and request ID."""
    _request_id(request_id)
    return len(_frame({**PROTOCOL, "request_id": request_id, "ok": True, "result": result}))


def encode_response(operation: str, request_id: str, result: Any) -> bytes:
    _request_id(request_id)
    limit = limits(operation)[1]
    result = (managed_response(result) if operation == RESOLVE_OPERATION
              else validate(operation + ".response", result))
    frame = _frame({**PROTOCOL, "request_id": request_id, "ok": True, "result": result})
    if len(frame) > limit:
        raise ValueError("response_too_large")
    return frame
