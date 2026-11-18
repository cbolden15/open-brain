"""Parse the fixed browser envelope and call grant-scoped Brain operations."""

from __future__ import annotations

import re
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
from open_brain_engine.engine.browser_pages import BrowserBinding, BrowserPages

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

from .policy import Policy, Refused, encoded

VERSION = "browser.v1"
OPERATIONS = {
    "binding.describe": "describe",
    "page.lookup": "lookup",
    "page.save": "save",
    "operation.status": "status",
    "annotation.add": "annotation.add",
    "annotation.list": "annotation.list",
}
_RECEIPT_OPERATIONS = {"save", "status", "annotation.add"}


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
        if operation in {"describe", "status"} and not args:
            return cls(operation, operation_id, {}, binding_id)
        if operation == "lookup" and set(args) == {"url"}:
            return cls(operation, "", {"url": _text(args["url"], 8192)}, binding_id)
        if operation in {"annotation.add", "annotation.list"}:
            common = {"url", "capture_id"}
            if not common <= args.keys():
                raise Refused("invalid_request")
            clean: dict[str, Any] = {
                "url": _text(args["url"], 8192),
                "capture_id": _text(args["capture_id"], 128),
            }
            if operation == "annotation.list":
                if set(args) != common | {"limit", "offset"}:
                    raise Refused("invalid_request")
                limit, offset = args["limit"], args["offset"]
                if (
                    type(limit) is not int
                    or not 1 <= limit <= 10
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


def dispatch(policy: Policy, request: Request) -> dict[str, object]:
    profile = open_existing_single_user_local(policy.brain_root)
    pages = BrowserPages(profile)
    binding = pages.binding(policy.token)
    policy.require(binding)
    if request.operation == "describe":
        return {"binding_id": _binding_id(binding), "destination": "This computer"}
    if request.binding_id is not None and request.binding_id != _binding_id(binding):
        raise Refused("binding_changed")
    # The token came from the same policy snapshot as the pin. Every engine
    # operation rechecks that immutable grant inside its transaction, so a
    # concurrent setup cannot substitute different authority or redirect it.
    required_operation = {"annotation.add": "save", "annotation.list": "lookup"}.get(
        request.operation, request.operation
    )
    if required_operation not in binding.operations:
        raise Refused("operation_denied")
    if request.operation == "status":
        result = pages.status(policy.token, request.operation_id)
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
        value: dict[str, object] = {
            "schema_version": 1,
            "url": request.args["url"],
            "why": request.args["reason"],
            "received_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "fetch_status": "not_attempted",
        }
        if "title" in request.args:
            value["title"] = request.args["title"]
        if "passage" in request.args:
            value["shared_text"] = request.args["passage"]
        record = parse_record(encoded(value))
        intake = SavedItemSourceAdapter(
            SourceResourceSelection(*binding.source.values(), "folder")
        ).intake(record)
        submission = CaptureSubmission.for_public_job(
            context=_capture_context(profile, binding.workflow_principal),
            payload=intake.payload(),
            delivery_id=intake.key.delivery_id(),
            source_origin="third_party",
            source_reference=intake.source_reference,
            provenance=intake.provenance(),
            privacy=intake.privacy,
            intent="reference",
            title=intake.title,
        )
        digest = sha256(encoded({**request.args, "url": record.identity_url})).hexdigest()
        result = pages.save(
            policy.token, request.operation_id, submission, record.item_key, request_digest=digest
        )
    response: dict[str, object] = {"state": result.state}
    if result.capture_id is not None:
        response["capture_id"] = result.capture_id
    if result.annotation_id is not None:
        response["annotation_id"] = result.annotation_id
    return response
