"""Parse the fixed browser envelope and call grant-scoped Brain operations."""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from open_brain_engine.engine import CaptureSubmission, LocalEngineContext, PublicJobCaptureContext
from open_brain_engine.engine.browser_annotations import (
    Annotation,
    BrowserAnnotations,
    MaskedAnnotation,
    uuid4_id,
)
from open_brain_engine.engine.browser_filing import BrowserFiling
from open_brain_engine.engine.browser_pages import (
    BrowserBinding,
    BrowserPages,
    BrowserResult,
    _operation,
)
from open_brain_engine.engine.browser_revisits import BrowserRevisits, validate_calendar
from open_brain_engine.engine.local_schema import open_local_database_read_only

from open_brain.profile import open_existing_single_user_local
from open_brain_connectors.runtime.saved_item import (
    MAX_RECORD_TITLE_CHARS,
    MAX_WHY_CHARS,
    SavedItemSourceAdapter,
    identity_url,
    item_key,
    parse_record,
)
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

from . import bookmark_import
from .policy import Policy, Refused, encoded

VERSION = "browser.v1"
OPERATIONS = {
    **{operation: operation for operation in bookmark_import.OPERATIONS},
    "binding.describe": "describe",
    "page.lookup": "lookup",
    "page.save": "save",
    "operation.status": "status",
    "operation.list": "operation.list",
    "annotation.add": "annotation.add",
    "annotation.list": "annotation.list",
    "space.list": "space.list",
    "page.file": "page.file",
    **{
        name: name
        for name in (
            "revisit.set",
            "revisit.dismiss",
            "revisit.clear",
            "revisit.due",
            "revisit.get",
        )
    },
}
_RECEIPT_OPERATIONS = {
    "save",
    "status",
    "annotation.add",
    "page.file",
    "revisit.set",
    "revisit.dismiss",
    "revisit.clear",
}


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise Refused("invalid_request")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise Refused("invalid_request") from None
    return value


@dataclass(frozen=True, slots=True)
class Request:
    operation: str
    operation_id: str
    args: dict[str, Any]
    binding_id: str | None = None

    @classmethod
    def parse(cls, value: object) -> Request:
        if not isinstance(value, dict) or "version" not in value:
            raise Refused("invalid_request")
        if value["version"] != VERSION:
            raise Refused("unsupported_version")
        name = value.get("operation")
        if not isinstance(name, str) or name not in OPERATIONS:
            raise Refused("operation_denied")
        operation = OPERATIONS[name]
        fields = {"version", "operation", "args"}
        if operation in _RECEIPT_OPERATIONS:
            fields.add("operation_id")
        binding_id: str | None = None
        if "binding_id" in value and operation != "describe":
            supplied_id = value["binding_id"]
            if (
                not isinstance(supplied_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", supplied_id) is None
            ):
                raise Refused("invalid_request")
            binding_id = supplied_id
            fields.add("binding_id")
        args = value.get("args")
        if not isinstance(args, dict) or set(value) != fields:
            raise Refused("invalid_request")
        operation_id = value.get("operation_id", "")
        if operation in _RECEIPT_OPERATIONS and (
            not isinstance(operation_id, str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", operation_id
            )
            is None
        ):
            raise Refused("invalid_request")
        if operation in bookmark_import.OPERATIONS:
            if binding_id is None:
                raise Refused("invalid_request")
            return cls(operation, "", bookmark_import.parse_arguments(operation, args), binding_id)
        if operation.startswith("revisit."):
            if operation == "revisit.due":
                if set(args) != {"limit", "offset"}:
                    raise Refused("invalid_request")
                limit, offset = args["limit"], args["offset"]
                if (
                    type(limit) is not int
                    or not 1 <= limit <= 20
                    or type(offset) is not int
                    or not 0 <= offset < 2**31
                ):
                    raise Refused("invalid_request")
                return cls(operation, "", dict(limit=limit, offset=offset), binding_id)
            fields = {"url", "capture_id"}
            if operation == "revisit.set":
                fields.update(("local_date", "zone"))
            if set(args) != fields:
                raise Refused("invalid_request")
            revisit_args = {
                "url": _text(args["url"], 8192),
                "capture_id": _text(args["capture_id"], 128),
            }
            if operation == "revisit.set":
                day, zone = _text(args["local_date"], 10), _text(args["zone"], 255)
                try:
                    validate_calendar(day, zone, require_installed=False)
                except ValueError:
                    raise Refused("invalid_request") from None
                revisit_args.update(local_date=day, zone=zone)
            return cls(operation, operation_id, revisit_args, binding_id)
        if operation in {"describe", "status"} and not args:
            return cls(operation, operation_id, {}, binding_id)
        if operation == "operation.list":
            if (
                set(args) != {"after", "limit"}
                or type(args["limit"]) is not int
                or not 1 <= args["limit"] <= 20
            ):
                raise Refused("invalid_request")
            if args["after"] is not None:
                try:
                    uuid4_id(_text(args["after"], 36))
                except ValueError:
                    raise Refused("invalid_request") from None
            return cls(operation, "", args, binding_id)
        if operation == "lookup" and set(args) == {"url"}:
            return cls(operation, "", {"url": _text(args["url"], 8192)}, binding_id)
        if operation in {"annotation.add", "annotation.list", "space.list", "page.file"}:
            common = {"url", "capture_id"}
            if not common <= args.keys():
                raise Refused("invalid_request")
            clean: dict[str, Any] = {
                "url": _text(args["url"], 8192),
                "capture_id": _text(args["capture_id"], 128),
            }
            if operation == "page.file":
                if set(args) != common | {"space_id"}:
                    raise Refused("invalid_request")
                clean["space_id"] = _text(args["space_id"], 128)
            elif operation in {"annotation.list", "space.list"}:
                if set(args) != common | {"limit", "offset"}:
                    raise Refused("invalid_request")
                limit, offset = args["limit"], args["offset"]
                if (
                    type(limit) is not int
                    or not 1 <= limit <= (100 if operation == "space.list" else 10)
                    or type(offset) is not int
                    or not 0 <= offset < 2**31
                ):
                    raise Refused("invalid_request")
                clean.update(limit=limit, offset=offset)
            else:
                required_child = common | {"annotation_id", "quote", "prefix", "suffix"}
                if not required_child <= args.keys() <= required_child | {"note"}:
                    raise Refused("invalid_request")
                try:
                    clean["annotation_id"] = uuid4_id(_text(args["annotation_id"], 36))
                except ValueError:
                    raise Refused("invalid_request") from None
                clean["quote"] = _text(args["quote"], 4096)
                for key in ("prefix", "suffix"):
                    raw = args[key]
                    if not isinstance(raw, str) or len(raw) > 128 or "\x00" in raw:
                        raise Refused("invalid_request")
                    try:
                        raw.encode("utf-8")
                    except UnicodeError:
                        raise Refused("invalid_request") from None
                    clean[key] = raw
                if "note" in args:
                    clean["note"] = _text(args["note"], 1024)
            return cls(operation, operation_id, clean, binding_id)
        required = {"url", "reason"}
        limits = {
            "url": 8192,
            "reason": MAX_WHY_CHARS,
            "title": MAX_RECORD_TITLE_CHARS,
            "passage": 4096,
        }
        if operation == "save" and required <= set(args) <= limits.keys():
            return cls(
                operation,
                operation_id,
                {key: _text(args[key], limits[key]) for key in args},
                binding_id,
            )
        raise Refused("invalid_request")


def _capture_context(profile: LocalEngineContext, principal: str) -> PublicJobCaptureContext:
    actor = f"actor_{principal}"
    return PublicJobCaptureContext.create(
        profile=profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "role_claim_id": f"role_claim_{principal}",
            "role_id": f"role_{principal}",
            "tenant_id": profile.tenant_id,
            "capabilities": ["capture.accept"],
        },
    )


def _binding_id(binding: BrowserBinding) -> str:
    # Installation grants and issuer epochs rotate. Explicit rebinding to the
    # original workflow on a restored Brain must still reconcile its receipts.
    identity = {
        "version": "browser-binding.v1",
        "topology": "local",
        "brain_id": binding.brain_id,
        "source": binding.source.values(),
        "workflow_principal": binding.workflow_principal,
    }
    return sha256(encoded(identity)).hexdigest()


def _save_page(
    policy: Policy,
    pages: BrowserPages,
    binding: BrowserBinding,
    args: dict[str, Any],
    operation_id: str,
) -> BrowserResult:
    value: dict[str, object] = {
        "schema_version": 1,
        "url": args["url"],
        "why": args["reason"],
        "received_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "fetch_status": "not_attempted",
    }
    if "title" in args:
        value["title"] = args["title"]
    if "passage" in args:
        value["shared_text"] = args["passage"]
    record = parse_record(encoded(value))
    intake = SavedItemSourceAdapter(
        SourceResourceSelection(*binding.source.values(), "folder")
    ).intake(record)
    submission = CaptureSubmission.for_public_job(
        context=_capture_context(pages.profile, binding.workflow_principal),
        payload=intake.payload(),
        delivery_id=intake.key.delivery_id(),
        source_origin="third_party",
        source_reference=intake.source_reference,
        provenance=intake.provenance(),
        privacy=intake.privacy,
        intent="reference",
        title=intake.title,
    )
    digest = sha256(encoded({**args, "url": record.identity_url})).hexdigest()
    return pages.save(
        policy.token, operation_id, submission, record.item_key, request_digest=digest
    )


def dispatch(policy: Policy, request: Request) -> dict[str, object]:
    profile = open_existing_single_user_local(policy.brain_root)
    pages = BrowserPages(profile)
    binding = pages.binding(policy.token)
    policy.require(binding)
    if request.operation == "describe":
        return {"binding_id": _binding_id(binding), "destination": "This computer"}
    if request.binding_id is not None and request.binding_id != _binding_id(binding):
        raise Refused("binding_changed")
    if request.operation in bookmark_import.OPERATIONS:
        assert request.binding_id is not None

        def authorize_import() -> None:
            current = pages.binding(policy.token)
            policy.require(current)
            if _binding_id(current) != request.binding_id:
                raise Refused("binding_changed")
            if "save" not in current.operations or not current.complete_domain:
                raise Refused("operation_denied")

        def save_bookmark(url: str, reason: str, operation_id: str) -> str:
            try:
                return _save_page(
                    policy, pages, binding, {"url": url, "reason": reason}, operation_id
                ).state
            except PermissionError:
                authorize_import()
                return "refused"

        def reconcile_bookmark(operation_id: str) -> str | None:
            with closing(open_local_database_read_only(profile)) as db:
                if not db.in_transaction:
                    db.execute("BEGIN")
                grant = pages._grant(db, policy.token, "save")
                if not grant["complete_domain"]:
                    raise Refused("operation_denied")
                outcome = _operation(db, grant, operation_id)
                return None if outcome is None else str(outcome["state"])

        return bookmark_import.execute(
            request.operation,
            request.args,
            binding_id=request.binding_id,
            principal=binding.workflow_principal,
            authorize=authorize_import,
            save_row=save_bookmark,
            reconcile_row=reconcile_bookmark,
        )
    # The token came from the same policy snapshot as the pin. Every engine
    # operation rechecks that immutable grant inside its transaction, so a
    # concurrent setup cannot substitute different authority or redirect it.
    required_operation = {
        "operation.list": "status",
        "annotation.add": "save",
        "annotation.list": "lookup",
        "space.list": "lookup",
        "page.file": "save",
        "revisit.set": "save",
        "revisit.clear": "save",
        "revisit.dismiss": "save",
        "revisit.due": "lookup",
        "revisit.get": "lookup",
    }.get(request.operation, request.operation)
    if required_operation not in binding.operations:
        raise Refused("operation_denied")
    if request.operation == "operation.list":
        from open_brain_engine.engine.browser_recovery import list_intents

        return list_intents(pages, policy.token, request.args["after"], request.args["limit"])
    if request.operation == "status":
        result = pages.status(policy.token, request.operation_id)
    elif request.operation.startswith("revisit."):
        revisits = BrowserRevisits(profile)
        args = request.args
        if request.operation == "revisit.due":
            return revisits.due(
                policy.token, now=datetime.now(UTC), limit=args["limit"], offset=args["offset"]
            )
        identity, _ = identity_url(args["url"])
        key = item_key(identity)
        if request.operation == "revisit.get":
            return revisits.get(policy.token, key, args["capture_id"])
        result = revisits.mutate(
            policy.token,
            request.operation_id,
            key,
            args["capture_id"],
            request.operation.removeprefix("revisit."),
            local_date=args.get("local_date"),
            zone=args.get("zone"),
        )
    elif request.operation in {"space.list", "page.file"}:
        filing = BrowserFiling(profile)
        args = request.args
        identity, _ = identity_url(args["url"])
        key = item_key(identity)
        if request.operation == "space.list":
            return filing.spaces(
                policy.token, key, args["capture_id"], limit=args["limit"], offset=args["offset"]
            )
        result = filing.file(
            policy.token, request.operation_id, key, args["capture_id"], args["space_id"]
        )
    elif request.operation in {"annotation.add", "annotation.list"}:
        annotations = BrowserAnnotations(profile)
        identity, _ = identity_url(request.args["url"])
        args = request.args
        if request.operation == "annotation.list":
            items, continuation = annotations.list(
                policy.token,
                item_key(identity),
                args["capture_id"],
                limit=args["limit"],
                offset=args["offset"],
            )
            return {"annotations": items, "next_offset": continuation}
        try:
            result = annotations.add(
                policy.token,
                request.operation_id,
                item_key(identity),
                args["capture_id"],
                Annotation(
                    args["annotation_id"],
                    args["quote"],
                    args["prefix"],
                    args["suffix"],
                    args.get("note"),
                ),
            )
        except MaskedAnnotation:
            raise Refused("annotation_masked") from None
    elif request.operation == "lookup":
        identity, _ = identity_url(request.args["url"])
        page = pages.lookup(policy.token, item_key(identity))
        if page is None:
            return {"state": "absent"}
        return {"state": "saved", "capture_id": page.capture_id, "space_id": page.space_id}
    else:
        result = _save_page(policy, pages, binding, request.args, request.operation_id)
    response: dict[str, object] = {"state": result.state}
    if result.capture_id is not None:
        response["capture_id"] = result.capture_id
    if result.revisit_action is not None:
        response["revisit_action"] = result.revisit_action
    if result.space_id is not None:
        response["space_id"] = result.space_id
    if result.annotation_id is not None:
        response["annotation_id"] = result.annotation_id
    return response
