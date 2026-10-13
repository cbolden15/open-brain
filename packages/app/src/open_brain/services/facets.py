"""Grant-filtered facet transport boundary and CLI construction helpers.

Engine contracts own parsing and serialization. Callables and authority are supplied by
trusted session setup; no test implementation is installed by this module.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, cast

from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.facet_validation import CONTRACT
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.services.t03_adapters import T03SessionBudget

MAX_SESSION_CALLS = 500
MAX_SESSION_BYTES = 16 * 1024 * 1024
MAX_REVIEW_PROPOSALS = 100

_READ = frozenset({"facet-read"})
_CATALOG = frozenset({"facet-catalog-write"})
_ASSIGN = frozenset({"facet-assign"})
_QUERY_WRITE = frozenset({"facet-query-write"})
_SEARCH = frozenset({"facet-read", "search"})
_HISTORY = frozenset({"facet-read", "history-read"})
_REVIEW = frozenset({"facet-read", "search", "review-propose"})

# Operation -> (injected component, required capabilities, quota bucket, response cap).
_OPERATIONS: dict[str, tuple[str, frozenset[str], str, int]] = {
    "facet.dimension.list": ("facets", _READ, "metadata", fc.MAX_LIST_RESPONSE_BYTES),
    "facet.dimension.create": ("facets", _CATALOG, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.dimension.rename": ("facets", _CATALOG, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.dimension.archive": ("facets", _CATALOG, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.value.list": ("facets", _READ, "metadata", fc.MAX_LIST_RESPONSE_BYTES),
    "facet.value.create": ("facets", _CATALOG, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.value.rename": ("facets", _CATALOG, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.value.archive": ("facets", _CATALOG, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.assignment.get": ("facets", _READ, "metadata", fc.MAX_READ_RESPONSE_BYTES),
    "facet.assignment.patch": ("facets", _ASSIGN, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.query.list": ("facets", _READ, "metadata", fc.MAX_LIST_RESPONSE_BYTES),
    "facet.query.get": ("facets", _READ, "metadata", fc.MAX_READ_RESPONSE_BYTES),
    "facet.query.create": ("facets", _QUERY_WRITE, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.query.update": ("facets", _QUERY_WRITE, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.query.archive": ("facets", _QUERY_WRITE, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.query.attach": ("facets", _QUERY_WRITE, "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
    "facet.query.attachment.get": ("facets", _READ, "metadata", fc.MAX_READ_RESPONSE_BYTES),
    "facet.query.page": ("facets", _SEARCH, "search", fc.MAX_LIST_RESPONSE_BYTES),
    "facet.history.list": ("facets", _HISTORY, "history", fc.MAX_LIST_RESPONSE_BYTES),
    "facet.history.state": ("facets", _HISTORY, "history", fc.MAX_READ_RESPONSE_BYTES),
    "review.propose_from_facet_query": (
        "publication",
        _REVIEW,
        "review",
        fc.MAX_RECEIPT_RESPONSE_BYTES,
    ),
    "facet.vault.preview": ("vault", frozenset(), "metadata", fc.MAX_READ_RESPONSE_BYTES),
    "facet.vault.apply": ("vault", frozenset(), "write", fc.MAX_RECEIPT_RESPONSE_BYTES),
}

_MCP_NAMES: dict[str, str] = {
    operation: "brain_" + operation.replace(".", "_")
    for operation in _OPERATIONS
    if not operation.startswith("facet.vault.")
}
_MCP_OPERATIONS = {name: operation for operation, name in _MCP_NAMES.items()}


@dataclass(frozen=True, slots=True)
class FacetReviewQuota:
    """Callbacks over the same counters used by ordinary review operations.

    ``begin`` admits and counts one proposal attempt, including retries and failures.
    ``reserve`` checks available response bytes without charging them; ``commit``
    charges actual wrapped response bytes. All callbacks run synchronously on the
    session dispatch thread and raise ValueError when the shared quota is exhausted.
    Reservation is a capacity check, so failed handlers leave no bytes to release.
    """

    begin: Callable[[], None]
    reserve: Callable[[int], None]
    commit: Callable[[int], None]


@dataclass(slots=True)
class FacetSessionBudget:
    """Facet quotas with optional shared search/history and review accounting."""

    calls: dict[str, int] = field(default_factory=dict)
    response_bytes: dict[str, int] = field(default_factory=dict)
    negotiated: T03SessionBudget | None = None
    review: FacetReviewQuota | None = None

    def begin(self, bucket: str) -> None:
        limit = MAX_REVIEW_PROPOSALS if bucket == "review" else MAX_SESSION_CALLS
        if self.calls.get(bucket, 0) >= limit:
            raise fc.FacetError("limit_exceeded")
        if self.negotiated is not None and bucket in {"search", "history"}:
            try:
                self.negotiated.begin("search.page" if bucket == "search" else "history.list")
            except ValueError:
                raise fc.FacetError("limit_exceeded") from None
        if self.review is not None and bucket == "review":
            try:
                self.review.begin()
            except ValueError:
                raise fc.FacetError("limit_exceeded") from None
        self.calls[bucket] = self.calls.get(bucket, 0) + 1

    def reserve(self, bucket: str, bytes_: int) -> None:
        if self.response_bytes.get(bucket, 0) + bytes_ > MAX_SESSION_BYTES:
            raise fc.FacetError("limit_exceeded")
        if self.review is not None and bucket == "review":
            try:
                self.review.reserve(bytes_)
            except ValueError:
                raise fc.FacetError("limit_exceeded") from None

    def commit(self, bucket: str, bytes_: int) -> None:
        self.reserve(bucket, bytes_)
        if self.negotiated is not None and bucket in {"search", "history"}:
            try:
                self.negotiated.commit(
                    "search.page" if bucket == "search" else "history.list", bytes_
                )
            except ValueError:
                raise fc.FacetError("limit_exceeded") from None
        if self.review is not None and bucket == "review":
            try:
                self.review.commit(bytes_)
            except ValueError:
                raise fc.FacetError("limit_exceeded") from None
        self.response_bytes[bucket] = self.response_bytes.get(bucket, 0) + bytes_


@dataclass(slots=True)
class FacetAppAdapter:
    facets: fc.FacetTasks | None
    authority: EffectiveAuthority
    publication: fc.FacetPublicationTasks | None = None
    vault: fc.FacetVaultTasks | None = None
    budget: FacetSessionBudget = field(default_factory=FacetSessionBudget)
    # An additional MCP restriction, including when legacy setup supplies owner authority.
    # None preserves existing construction; CLI dispatch uses trusted authority directly.
    granted_capabilities: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.authority, EffectiveAuthority):
            raise ValueError("invalid facet authority")
        if self.granted_capabilities is not None and (
            type(self.granted_capabilities) is not frozenset
            or any(type(grant) is not str for grant in self.granted_capabilities)
        ):
            raise ValueError("invalid facet grants")

    def _method(self, operation: str) -> Callable[..., object] | None:
        component, _grants, _bucket, _cap = _OPERATIONS[operation]
        target = getattr(self, component)
        candidate = getattr(target, fc.OPERATION_METHODS[operation], None)
        return candidate if callable(candidate) else None

    def available_operations(self, *, transport: str = "mcp") -> tuple[str, ...]:
        if transport not in {"mcp", "cli"}:
            raise fc.FacetError("invalid_arguments")
        return tuple(
            operation
            for operation, (_component, grants, _bucket, _cap) in _OPERATIONS.items()
            if (transport == "cli" or operation in _MCP_NAMES)
            and (self.authority.owner or grants <= self.authority.capabilities)
            and (
                transport == "cli"
                or self.granted_capabilities is None
                or grants <= self.granted_capabilities
            )
            and (not operation.startswith("facet.vault.") or self.authority.owner)
            and self._method(operation) is not None
        )

    def tool_definitions(self) -> tuple[dict[str, object], ...]:
        """Return only MCP tools with both grants and an implementation callable."""
        return tuple(
            {
                "name": _MCP_NAMES[operation],
                "description": f"Call {operation} with the closed facet DTO.",
                "inputSchema": _mcp_schema(CONTRACT["definitions"][operation + ".request"]),
                "required_grants": sorted(_OPERATIONS[operation][1]),
            }
            for operation in self.available_operations()
        )

    def invoke_mcp(
        self, tool_name: str, arguments: Mapping[str, object] | str | bytes, **kwargs: Any
    ) -> dict[str, object]:
        operation = _MCP_OPERATIONS.get(tool_name)
        if operation is None:
            raise fc.FacetError("unsupported_capability")
        return self.invoke(operation, arguments, transport="mcp", **kwargs)

    def invoke(
        self,
        operation: str,
        arguments: Mapping[str, object] | str | bytes,
        *,
        transport: str = "mcp",
        maximum_response_bytes: int = fc.MAX_READ_RESPONSE_BYTES,
        encoded_size: Callable[[Mapping[str, object]], int] | None = None,
        encoded_request_size: int | None = None,
    ) -> dict[str, object]:
        # Authorization and implementation checks precede parsing and every handler effect.
        if operation not in _OPERATIONS or operation not in self.available_operations(
            transport=transport
        ):
            raise fc.FacetError("unsupported_capability")
        if type(maximum_response_bytes) is not int or maximum_response_bytes < 1:
            raise fc.FacetError("response_too_large")
        _component, _grants, bucket, cap = _OPERATIONS[operation]
        maximum_response_bytes = min(maximum_response_bytes, cap)
        try:
            raw = (
                arguments
                if isinstance(arguments, (str, bytes))
                else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            )
        except TypeError, ValueError, UnicodeError:
            raise fc.FacetError("invalid_arguments") from None
        request_size = len(raw.encode("utf-8") if isinstance(raw, str) else raw)
        if encoded_request_size is not None:
            if type(encoded_request_size) is not int or encoded_request_size < request_size:
                raise fc.FacetError("invalid_arguments")
            request_size = encoded_request_size
        request_cap = (
            fc.MAX_PUBLICATION_REQUEST_BYTES
            if operation == fc.PUBLICATION_OPERATION
            else fc.MAX_REQUEST_BYTES
        )
        if request_size > request_cap:
            raise fc.FacetError("invalid_arguments")
        parsed = fc.parse_wire(raw, operation=operation)
        request = fc.request_from_wire(operation, parsed)
        self.budget.begin(bucket)
        if self.budget.response_bytes.get(bucket, 0) >= MAX_SESSION_BYTES:
            raise fc.FacetError("limit_exceeded")
        size = encoded_size or _json_size
        if bucket in {"write", "review"}:
            # Reserve the complete bounded receipt, including the caller's wrapper.
            reservation = max(
                fc.MAX_RECEIPT_RESPONSE_BYTES,
                size(_receipt_bound(operation, parsed["operation_id"])),
            )
            if reservation > maximum_response_bytes:
                raise fc.FacetError("response_too_large")
            self.budget.reserve(bucket, reservation)
        method = self._method(operation)
        if operation.endswith(".list") or operation == "facet.query.page":
            admit = getattr(self.facets, "with_page_admission", None)
            if callable(admit):
                task = admit(maximum_response_bytes, size)
                method = getattr(task, fc.OPERATION_METHODS[operation])
        assert method is not None
        try:
            response = method(request, authority=self.authority)
            result = fc.response_to_wire(operation, response)
        except fc.FacetError:
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            if (
                isinstance(code, str)
                and code in CONTRACT["definitions"]["error"]["properties"]["code"]["enum"]
            ):
                raise fc.FacetError(code) from None
            raise fc.FacetError("operation_pending") from None
        bare_size = _json_size(result)
        wrapped_size = size(result)
        if bare_size > cap or wrapped_size > maximum_response_bytes:
            raise fc.FacetError("response_too_large")
        self.budget.commit(bucket, wrapped_size)
        return result


def _receipt_bound(operation: str, operation_id: str) -> dict[str, object]:
    """Largest receipt fields permitted by the closed receipt shapes and ID grammar."""
    suffix = "00000000-0000-4000-8000-000000000000"
    if operation == fc.PUBLICATION_OPERATION:
        return {
            "status": "proposed", "dto_version": 1,
            "effective_idempotency_key": operation_id,
            "proposal_id": "proposal_" + suffix, "page_id": "page_" + suffix,
            "space_id": "space_" + suffix, "target_page_id": "page_" + suffix,
            "operation": "create", "proposal_status": "rejected",
        }
    receipt: dict[str, object] = {
        "dto_version": 1, "status": "unchanged", "operation_id": operation_id,
        "subject_id": "facetquery_" + suffix, "version": 9_007_199_254_740_991,
        "event_id": "facetevent_" + suffix,
    }
    if operation == "facet.vault.apply":
        return {"status": "ok", "dto_version": 1, "receipt": receipt,
                "projection_status": "refresh_required"}
    return receipt


def _json_size(result: Mapping[str, object]) -> int:
    return len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _mcp_schema(rule: Mapping[str, Any]) -> dict[str, object]:
    """Translate the C0 closed grammar into MCP discovery JSON Schema."""
    if "ref" in rule:
        return _mcp_schema(CONTRACT["definitions"][rule["ref"]])
    if "anyOf" in rule:
        return {"anyOf": [_mcp_schema(item) for item in rule["anyOf"]]}
    if "enum" in rule:
        return {"enum": rule["enum"]}
    kind = rule["type"]
    schema: dict[str, object] = {"type": kind}
    if kind == "object":
        properties = rule["properties"]
        schema["properties"] = {name: _mcp_schema(child) for name, child in properties.items()}
        schema["required"] = sorted(set(properties) - set(rule["optional"]))
        schema["additionalProperties"] = False
    elif kind == "array":
        schema["items"] = _mcp_schema(rule["items"])
        schema["minItems"] = rule["min"]
        schema["maxItems"] = rule["max"]
        schema["uniqueItems"] = rule["unique"]
    elif kind == "string":
        schema["minLength"] = rule.get("min", 0)
        schema["maxLength"] = rule.get("max", 1048576)
    elif kind == "integer":
        schema["minimum"] = rule["min"]
        schema["maximum"] = rule["max"]
    return schema


def facet_error_result(error: fc.FacetError) -> dict[str, object]:
    return error.to_wire()


def facet_cli_exit_code(error: fc.FacetError, *, grammar: bool = False) -> int:
    if grammar:
        return 2
    return 75 if error.code == "operation_pending" else 1


def facet_cli_request(
    operation: str,
    arguments: Mapping[str, object],
    *,
    selector_text: str | bytes | None = None,
) -> dict[str, object]:
    """Build one engine request from CLI values; file reading remains caller-owned."""
    value = dict(arguments)
    value.setdefault("dto_version", 1)
    if selector_text is not None:
        if "selector" in value:
            raise fc.FacetError("invalid_arguments")
        value["selector"] = fc.parse_wire(selector_text)
    if operation in _OPERATIONS and _OPERATIONS[operation][2] in {"write", "review"}:
        value.setdefault("operation_id", str(uuid.uuid4()))
    return fc.request_from_wire(operation, value).to_wire()


def read_facet_selector_file(path: str, *, stdin: BinaryIO | None = None) -> bytes:
    """Read a bounded owner CLI selector, including ``-`` for standard input."""
    try:
        if path == "-":
            data = (stdin or sys.stdin.buffer).read(fc.MAX_REQUEST_BYTES + 1)
        else:
            with Path(path).open("rb") as stream:
                data = stream.read(fc.MAX_REQUEST_BYTES + 1)
    except OSError:
        raise fc.FacetError("invalid_arguments") from None
    if len(data) > fc.MAX_REQUEST_BYTES:
        raise fc.FacetError("invalid_arguments")
    fc.parse_wire(data)
    return data


def facet_mcp_operation(tool_name: str) -> str | None:
    return _MCP_OPERATIONS.get(tool_name)


def add_facet_grant_flags(parser: argparse.ArgumentParser) -> None:
    """Install four independent default-off agent setup flags."""
    for grant in ("facet-read", "facet-catalog-write", "facet-assign", "facet-query-write"):
        parser.add_argument("--allow-" + grant, action="store_true", default=False)


def facet_grants_from_namespace(parsed: argparse.Namespace) -> frozenset[str]:
    return frozenset(
        grant
        for grant in ("facet-read", "facet-catalog-write", "facet-assign", "facet-query-write")
        if getattr(parsed, "allow_" + grant.replace("-", "_"), False) is True
    )


def add_review_facet_arguments(parser: argparse.ArgumentParser) -> None:
    """Extend the coordinator-owned review propose parser with facet query inputs."""
    parser.add_argument("--facet-query-id")
    parser.add_argument("--expected-query-version", type=int)


def facet_cli_from_namespace(
    parsed: argparse.Namespace, *, selector_text: str | bytes | None = None
) -> tuple[str, dict[str, object]]:
    """Map the facet parser namespace to one closed C0 request."""
    group = getattr(parsed, "facet_group", None)
    action = getattr(parsed, "facet_action", None)
    aliases = {"show": "get", "run": "page", "state": "state", "attachment": "attachment.get"}
    if group == "assignment":
        method = "get" if action == "show" else action
    elif group in {"history", "vault"}:
        method = action
    elif group in {"dimension", "value", "query"}:
        method = aliases.get(cast(str, action), action)
    else:
        raise fc.FacetError("invalid_arguments")
    operation = f"facet.{group}.{method}"
    if operation not in _OPERATIONS:
        raise fc.FacetError("invalid_arguments")
    values = vars(parsed)
    fields = set(CONTRACT["definitions"][operation + ".request"]["properties"])
    arguments = {key: value for key, value in values.items() if key in fields and value is not None}
    if group == "history":
        arguments["subject"] = {"kind": values["kind"], "id": values["subject_id"]}
    if operation == "facet.query.page" and "query_id" not in arguments:
        arguments.pop("expected_query_version", None)
    if operation == "facet.query.page" and "query_id" in arguments and selector_text is not None:
        raise fc.FacetError("invalid_arguments")
    if operation in {"facet.dimension.create", "facet.value.create", "facet.query.create"}:
        arguments.setdefault("expected_version", 0)
    return operation, facet_cli_request(operation, arguments, selector_text=selector_text)


def add_facet_parsers(subparsers: Any) -> argparse.ArgumentParser:
    """Install the owner CLI grammar; parsed values go through facet_cli_request."""
    root = cast(
        argparse.ArgumentParser,
        subparsers.add_parser("facet", help="Manage owner-defined facets."),
    )
    groups = root.add_subparsers(dest="facet_group", required=True)
    for group, actions in {
        "dimension": ("list", "create", "rename", "archive"),
        "value": ("list", "create", "rename", "archive"),
        "assignment": ("show", "patch"),
        "query": ("list", "show", "create", "update", "archive", "attach", "attachment", "run"),
        "history": ("list", "state"),
        "vault": ("preview", "apply"),
    }.items():
        parent = groups.add_parser(group)
        children = parent.add_subparsers(dest="facet_action", required=True)
        for action in actions:
            parser = children.add_parser(action)
            parser.add_argument("--json", action="store_true")
            if action in {"list", "run"}:
                parser.add_argument("--limit", type=int, default=50)
                parser.add_argument("--cursor")
            if group in {"dimension", "value", "query"} and action == "list":
                parser.add_argument("--include-archived", action="store_true")
            if group == "dimension":
                if action in {"rename", "archive"}:
                    parser.add_argument("dimension_id")
                if action in {"create", "rename"}:
                    parser.add_argument("name")
            elif group == "value":
                if action in {"list", "create"}:
                    parser.add_argument("--dimension", dest="dimension_id", required=True)
                if action in {"rename", "archive"}:
                    parser.add_argument("value_id")
                if action in {"create", "rename"}:
                    parser.add_argument("name")
                if action == "create":
                    parser.add_argument("--expected-dimension-version", type=int, required=True)
            elif group == "assignment":
                parser.add_argument("record_id")
                if action == "patch":
                    parser.add_argument("--expected-revision-id", required=True)
                    parser.add_argument("--add", dest="add_value_ids", action="append", default=[])
                    parser.add_argument(
                        "--remove", dest="remove_value_ids", action="append", default=[]
                    )
            elif group == "query":
                if action in {"show", "update", "archive", "attach", "run"}:
                    parser.add_argument("query_id", nargs="?" if action == "run" else None)
                if action == "create":
                    parser.add_argument("name")
                if action == "update":
                    parser.add_argument("--name", required=True)
                if action in {"create", "update", "run"}:
                    parser.add_argument("--selector-file", required=action != "run")
                if action == "attach":
                    parser.add_argument("--page-id", required=True)
                    parser.add_argument("--expected-revision-id", required=True)
                if action == "attachment":
                    parser.add_argument("page_id")
                if action in {"run", "attach"}:
                    parser.add_argument("--expected-query-version", type=int)
            elif group == "history":
                parser.add_argument("--kind", required=True)
                parser.add_argument("--id", dest="subject_id", required=True)
                if action == "list":
                    parser.add_argument("--as-of-sequence", type=int)
                else:
                    parser.add_argument("--as-of-sequence", type=int, required=True)
            elif group == "vault":
                if action == "preview":
                    parser.add_argument("--record-id", required=True)
                    parser.add_argument("--workspace-id", required=True)
                else:
                    parser.add_argument("--preview-id", required=True)
            if (group, action) in {
                ("dimension", "create"),
                ("dimension", "rename"),
                ("dimension", "archive"),
                ("value", "create"),
                ("value", "rename"),
                ("value", "archive"),
                ("assignment", "patch"),
                ("query", "create"),
                ("query", "update"),
                ("query", "archive"),
                ("query", "attach"),
                ("vault", "apply"),
            }:
                parser.add_argument("--expected-version", type=int, required=group != "vault")
                parser.add_argument("--operation-id")
    return root
