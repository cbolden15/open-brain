"""Closed, canonical development evidence with retained revision references."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any, cast

from open_brain_engine.core.access_contracts import _TIER_PRECEDENCE
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import PrivacyTier

from .development_binding import development_closure
from .v1 import PortableValidationError
from .v4 import SOURCE_METADATA_PATH
from .v5 import EFFECTIVE_PRIVACY_PATH

DEVELOPMENT_EVIDENCE_PATH = "history/developments/evidence-v1.json"
DEVELOPMENT_APPEND_PATH = "history/developments/append-v1.json"
DEVELOPMENT_SIDECAR_PATHS = frozenset({DEVELOPMENT_EVIDENCE_PATH, DEVELOPMENT_APPEND_PATH})
_REQUEST_KEYS = {
    "schema_version",
    "derived_capture_id",
    "supporting_capture_ids",
    "supports_development_ids",
    "supersedes_development_ids",
    "operation_id",
}
_ROW_KEYS = {
    "derived_capture_id",
    "operation_id",
    "principal_id",
    "request_sha256",
    "request_json",
    "receipt_json",
}


def validate_development_evidence(files: Mapping[str, bytes]) -> list[dict[str, Any]]:
    try:
        raw = files[DEVELOPMENT_EVIDENCE_PATH]
        value = json.loads(raw)
        if (
            canonical(value) != raw
            or set(value) != {"schema_version", "operations"}
            or (
                type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or type(value["operations"]) is not list
            )
        ):
            raise ValueError
        metadata = json.loads(files[SOURCE_METADATA_PATH])
        revisions = {row["capture_id"]: row for row in metadata["revisions"]}
        sources = {row["source_id"]: row for row in metadata["sources"]}
        privacy = {
            row["target_id"]: row["effective_privacy"]
            for row in json.loads(files[EFFECTIVE_PRIVACY_PATH])["resolved_revisions"]
            if row["target_kind"] == "source_revision"
        }
        rows = value["operations"]
        identities: set[str] = set()
        operation_ids: set[str] = set()
        graph: dict[str, list[str]] = {}
        for row in rows:
            if type(row) is not dict or set(row) != _ROW_KEYS:
                raise ValueError
            request, receipt = json.loads(row["request_json"]), json.loads(row["receipt_json"])
            if (
                canonical(request).decode() != row["request_json"]
                or canonical(receipt).decode() != row["receipt_json"]
                or sha256(canonical(request)).hexdigest() != row["request_sha256"]
                or set(request) != _REQUEST_KEYS
                or set(receipt) != _REQUEST_KEYS | {"source_states"}
                or any(receipt[key] != request[key] for key in _REQUEST_KEYS)
                or type(request["schema_version"]) is not int
                or request["schema_version"] != 1
                or request["derived_capture_id"] != row["derived_capture_id"]
                or request["operation_id"] != row["operation_id"]
                or type(row["principal_id"]) is not str
                or not row["principal_id"]
                or type(row["operation_id"]) is not str
                or not 1 <= len(row["operation_id"]) <= 128
            ):
                raise ValueError
            identity = row["derived_capture_id"]
            if (
                identity in identities
                or row["operation_id"] in operation_ids
                or identity not in revisions
            ):
                raise ValueError
            identities.add(identity)
            operation_ids.add(row["operation_id"])
            for key in (
                "supporting_capture_ids",
                "supports_development_ids",
                "supersedes_development_ids",
            ):
                ids = request[key]
                if (
                    type(ids) is not list
                    or len(ids) > 32
                    or ids != sorted(set(ids))
                    or identity in ids
                ):
                    raise ValueError
                if any(item not in revisions for item in ids):
                    raise ValueError
            if not request["supporting_capture_ids"]:
                raise ValueError
            derived_privacy = privacy[identity]
            for support_id in (
                request["supporting_capture_ids"] + request["supports_development_ids"]
            ):
                support = privacy[support_id]
                if (
                    _TIER_PRECEDENCE[PrivacyTier(derived_privacy["tier"])]
                    < _TIER_PRECEDENCE[PrivacyTier(support["tier"])]
                    or derived_privacy["authority"]["cloud"]
                    and not support["authority"]["cloud"]
                    or derived_privacy["authority"]["external_egress"]
                    and not support["authority"]["external_egress"]
                ):
                    raise ValueError
            states = receipt["source_states"]
            if (
                type(states) is not list
                or [state["capture_id"] for state in states] != request["supporting_capture_ids"]
            ):
                raise ValueError
            for state in states:
                if set(state) != {
                    "capture_id",
                    "source_id",
                    "head_version",
                    "sha256",
                    "space_id",
                    "route_version",
                }:
                    raise ValueError
                revision = revisions[state["capture_id"]]
                if (
                    revision["source_id"] != state["source_id"]
                    or revision["source_sha256"] != state["sha256"]
                    or type(state["head_version"]) is not int
                    or state["head_version"] < 1
                    or state["head_version"] > sources[state["source_id"]]["head_version"]
                    or type(state["route_version"]) is not int
                    or state["route_version"] < 0
                ):
                    raise ValueError
            graph[identity] = (
                request["supports_development_ids"] + request["supersedes_development_ids"]
            )
        if [row["derived_capture_id"] for row in rows] != sorted(identities):
            raise ValueError
        if any(dependency not in identities for deps in graph.values() for dependency in deps):
            raise ValueError
        by_id = {row["derived_capture_id"]: row for row in rows}
        for identity in identities:
            development_closure((identity,), by_id.get)
        return rows
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise PortableValidationError("Portable11 development evidence invalid") from error


def validate_development_review_bindings(
    files: Mapping[str, bytes], rows: list[dict[str, Any]],
) -> None:
    """Require the exact closure committed by each owner review confirmation."""
    from .review_binding import validate_review_binding

    by_id = {row["derived_capture_id"]: row for row in rows}
    bound: set[str] = set()
    try:
        for path, payload in files.items():
            if not path.startswith("history/review-bindings/"):
                continue
            binding = validate_review_binding(json.loads(payload))
            actual = development_closure(cast(Sequence[str], binding["provenance"]), by_id.get)
            if binding["schema_version"] == 6:
                if actual != binding["development_evidence"]:
                    raise ValueError
            elif any(item["evidence_sha256"] is not None for item in actual):
                raise ValueError
            bound.add(cast(str, binding["proposal_id"]))
        for path, payload in files.items():
            if path.startswith("history/proposals/"):
                proposal = json.loads(payload)
                if proposal["proposal_id"] not in bound and any(
                    identity in by_id for identity in proposal["capture_ids"]
                ):
                    raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable11 development review commitment invalid") from error


def validate_append_operations(files: Mapping[str, bytes]) -> list[dict[str, Any]]:
    from open_brain_engine.engine.contracts import PatchDraft, PatchOperation

    try:
        raw = files[DEVELOPMENT_APPEND_PATH]
        value = json.loads(raw)
        if (
            canonical(value) != raw
            or set(value) != {"schema_version", "operations"}
            or (
                type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or type(value["operations"]) is not list
            )
        ):
            raise ValueError
        revisions = {
            row["capture_id"] for row in json.loads(files[SOURCE_METADATA_PATH])["revisions"]
        }
        previous = ""
        for row in value["operations"]:
            if set(row) != {
                "delivery_id",
                "request_sha256",
                "request_json",
                "draft_json",
                "proposal_id",
            }:
                raise ValueError
            if type(row["delivery_id"]) is not str or row["delivery_id"] <= previous:
                raise ValueError
            previous = row["delivery_id"]
            if row["proposal_id"] is not None and not any(
                path.startswith("history/proposals/")
                and json.loads(payload).get("proposal_id") == row["proposal_id"]
                for path, payload in files.items()
            ):
                raise ValueError
            request, frozen = json.loads(row["request_json"]), json.loads(row["draft_json"])
            if (
                set(request) != {"capture_ids", "target_page_id", "append_markdown"}
                or canonical(request).decode() != row["request_json"]
                or canonical(frozen).decode() != row["draft_json"]
                or sha256(canonical(request)).hexdigest() != row["request_sha256"]
                or set(frozen) != {"target_page_id", "expected_page_sha256", "operations"}
                or request["target_page_id"] != frozen["target_page_id"]
                or type(request["capture_ids"]) is not list
                or not request["capture_ids"]
                or any(identity not in revisions for identity in request["capture_ids"])
            ):
                raise ValueError
            if row["proposal_id"] is not None:
                bindings = [
                    json.loads(payload)
                    for path, payload in files.items()
                    if path.startswith("history/review-bindings/")
                    and json.loads(payload).get("proposal_id") == row["proposal_id"]
                ]
                if len(bindings) != 1:
                    raise ValueError
                binding = bindings[0]
                if (
                    binding["selected_capture_ids"] != request["capture_ids"]
                    or binding["page_id"] != request["target_page_id"]
                    or binding["patch"]["target_page_sha256"] != frozen["expected_page_sha256"]
                    or binding["patch"]["operations"] != frozen["operations"]
                ):
                    raise ValueError
            draft = PatchDraft(
                target_page_id=frozen["target_page_id"],
                expected_page_sha256=frozen["expected_page_sha256"],
                operations=tuple(PatchOperation(**operation) for operation in frozen["operations"]),
            )
            if len(draft.operations) != 1 or (
                draft.operations[0].replacement
                != "\n\n" + request["append_markdown"].strip() + "\n"
                or draft.operations[0].start_byte != draft.operations[0].end_byte
            ):
                raise ValueError
        return value["operations"]
    except (KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable11 append operations invalid") from error
