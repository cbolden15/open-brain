"""Portable v10 facet sidecar codecs; catalog registration is coordinator-owned.

A decoded bundle contains the exact canonical bytes and a validated replay
assertion. Base Portable lineage/provenance validation must precede this codec.
"""

from __future__ import annotations

import base64
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.storage.markdown import parse_markdown

from .review_binding import review_binding_digest
from .v1 import PortableValidationError, _portable_record
from .v3 import _published_bytes, _recorded_before, _records
from .v4 import SOURCE_METADATA_PATH

FACET_EVENTS_PATH = "history/facets/events-v1.json"
FACET_OPERATIONS_PATH = "history/facets/operations-v1.json"
FACET_STATE_PATH = "content/facets/state-v1.json"
FACET_SIDECAR_PATHS = frozenset({FACET_EVENTS_PATH, FACET_OPERATIONS_PATH, FACET_STATE_PATH})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PortableValidationError(message)


@dataclass(frozen=True, slots=True)
class FacetPortableReferences:
    """Identity indexes obtained from validated lineage and retained base evidence.

    principal_actors records the producer's trusted principal/actor relationship;
    it is not inferred from transport session IDs or request arguments.
    """

    source_ids: frozenset[str]
    page_ids: frozenset[str]
    capture_sources: Mapping[str, str]
    revision_pages: Mapping[str, str]
    space_ids: frozenset[str]
    publication_ids: frozenset[str]
    principal_actors: Mapping[str, str]

    def __post_init__(self) -> None:
        for field in ("source_ids", "page_ids", "space_ids", "publication_ids"):
            _require(type(getattr(self, field)) is frozenset, "facet identity index is mutable")
        object.__setattr__(self, "capture_sources", MappingProxyType(dict(self.capture_sources)))
        object.__setattr__(self, "revision_pages", MappingProxyType(dict(self.revision_pages)))
        object.__setattr__(self, "principal_actors", MappingProxyType(dict(self.principal_actors)))


def references_from_portable_files(
    files: Mapping[str, bytes],
    *,
    principal_actors: Mapping[str, str] | None = None,
) -> FacetPortableReferences:
    """Index already validated retained source/page identities from pinned bytes."""
    from open_brain_engine.engine.facet_evidence import validate_evidence

    try:
        metadata = _portable_record(files[SOURCE_METADATA_PATH], "source metadata")
        sources = cast(list[dict[str, Any]], metadata["sources"])
        revisions = cast(list[dict[str, Any]], metadata["revisions"])
        members = cast(list[dict[str, Any]], metadata["canonical_members"])
        capture_sources = {row["capture_id"]: row["source_id"] for row in revisions}
        revision_pages = {row["revision_id"]: row["page_id"] for row in members}
        pages = {row["page_id"] for row in members}
        spaces: set[str] = set()
        for path, payload in files.items():
            if path.startswith("content/spaces/") and path.endswith("/_space.md"):
                spaces.add(cast(str, parse_markdown(payload).fields["space_id"]))
            elif path.startswith("content/spaces/") and "/notes/" in path and path.endswith(".md"):
                pages.add(cast(str, parse_markdown(payload).fields["page_id"]))
        publications = _records(files, "history/publications/", "publication")
        pages.update(cast(str, row["page_id"]) for row in publications.values())
        if principal_actors is None:
            operations = _canonical_record(files[FACET_OPERATIONS_PATH], "operations_sidecar")
            owner_actor_id = tomllib.loads(files["brain.toml"].decode("utf-8"))["owner_actor_id"]
            validate_evidence("actor_id", owner_actor_id)
            principal_actors = {
                row["principal_id"]: owner_actor_id for row in operations["operations"]
            }
        return FacetPortableReferences(
            frozenset(row["source_id"] for row in sources),
            frozenset(pages),
            capture_sources,
            revision_pages,
            frozenset(spaces),
            frozenset(publications),
            principal_actors,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable v10 retained identity index is invalid") from error


def _canonical_record(payload: bytes, definition: str) -> dict[str, Any]:
    from open_brain_engine.engine.facet_evidence import validate_evidence
    from open_brain_engine.engine.facet_validation import parse

    try:
        value = parse(payload)
        validate_evidence(definition, value)
        _require(canonical(value) == payload, "facet sidecar is not canonical")
        return value  # type: ignore[no-any-return]
    except (ValueError, TypeError, UnicodeError) as error:
        raise PortableValidationError(f"invalid facet {definition}: {error}") from error


def _subject_exists(kind: str, identity: str, references: FacetPortableReferences) -> bool:
    return identity in (references.source_ids if kind == "source" else references.page_ids)


def _references(
    events: dict[str, Any],
    operations: dict[str, Any],
    state: dict[str, Any],
    references: FacetPortableReferences,
) -> None:
    for event in events["events"]:
        for change in event["changes"]:
            for row in (change["before"], change["after"]):
                if row is None:
                    continue
                if "subject_kind" in row:
                    _require(
                        _subject_exists(row["subject_kind"], row["subject_id"], references),
                        "facet subject reference is missing",
                    )
                if "page_id" in row:
                    _require(
                        row["page_id"] in references.page_ids, "facet page reference is missing"
                    )
                if "selector" in row:
                    _require(
                        set(row["selector"]["space_ids"]) <= references.space_ids,
                        "facet selector space reference is missing",
                    )
    by_operation = {row["operation_id"]: row for row in operations["operations"]}
    for event in events["events"]:
        operation = by_operation[event["operation_id"]]
        _require(
            references.principal_actors.get(operation["principal_id"]) == event["actor_id"],
            "facet principal/actor binding is invalid",
        )
    dimensions = {row["dimension_id"] for row in state["dimensions"]}
    values = {row["value_id"] for row in state["values"]}
    queries = {row["query_id"] for row in state["queries"]}
    for operation in operations["operations"]:
        _require(
            operation["principal_id"] in references.principal_actors,
            "facet operation principal is unknown",
        )
        args = operation["request_json"]["arguments"]
        method = operation["request_json"]["method"]
        if not method.endswith(".create") and method != "facet.assignment.patch":
            target = (
                "page_id"
                if ".attach" in method
                else "dimension_id"
                if method.startswith("facet.dimension.")
                else "value_id"
                if method.startswith("facet.value.")
                else "query_id"
            )
            _require(
                operation["receipt_json"]["subject_id"] == args[target],
                "facet operation receipt target is invalid",
            )
        if "add_value_ids" in args:
            _require(
                set(args["add_value_ids"]) | set(args["remove_value_ids"]) <= values,
                "facet operation membership value is missing",
            )
        for field, identities in (
            ("dimension_id", dimensions),
            ("value_id", values),
            ("query_id", queries),
            ("page_id", references.page_ids),
            ("publication_id", references.publication_ids),
        ):
            if field in args:
                _require(args[field] in identities, "facet operation reference is missing")
        if "selector" in args:
            _require(
                set(args["selector"]["space_ids"]) <= references.space_ids,
                "facet operation selector space is missing",
            )
        if "record_id" in args:
            anchor = args["record_id"]
            expected = args["expected_revision_id"]
            if anchor.startswith("capture_"):
                source_id = references.capture_sources.get(anchor)
                _require(source_id is not None, "facet operation capture is missing")
                _require(
                    references.capture_sources.get(expected) == source_id,
                    "facet operation capture lineage is invalid",
                )
                affected = operation["receipt_json"]["subject_id"]
                _require(affected == source_id, "facet operation subject lineage is invalid")
            else:
                _require(
                    anchor in references.page_ids
                    and references.revision_pages.get(expected) == anchor,
                    "facet operation canonical revision is missing",
                )
                _require(
                    operation["receipt_json"]["subject_id"] == anchor,
                    "facet operation page receipt is invalid",
                )
        elif "expected_revision_id" in args:
            _require(
                references.revision_pages.get(args["expected_revision_id"]) == args["page_id"],
                "facet attachment revision is missing",
            )


@dataclass(frozen=True, slots=True)
class PortableFacetBundle:
    """Detached canonical facet evidence, suitable for a hidden restore stage."""

    brain_id: str
    sidecars: Mapping[str, bytes]
    state_sha256: str
    operations_sha256: str
    evidence_sha256: str
    next_sequence: int

    def document(self, path: str) -> dict[str, Any]:
        # Return detached values so an importer cannot mutate a validated bundle.
        from open_brain_engine.engine.facet_validation import parse

        value = parse(self.sidecars[path])
        return value  # type: ignore[no-any-return]


def decode_portable_facets(
    files: Mapping[str, bytes],
    *,
    brain_id: str,
    references: FacetPortableReferences,
) -> PortableFacetBundle:
    """Validate all required sidecars, their replay, identity and principal bindings."""
    from open_brain_engine.engine.facet_evidence import (
        FacetEvidenceError,
        validate_portable_facet_sidecars,
    )

    _require(files.keys() >= FACET_SIDECAR_PATHS, "Portable v10 facet sidecar is missing")
    _require(
        not any(
            path.startswith(("history/facets/", "content/facets/"))
            and path not in FACET_SIDECAR_PATHS
            for path in files
        ),
        "unknown Portable v10 facet evidence path",
    )
    definitions = {
        FACET_EVENTS_PATH: "events_sidecar",
        FACET_OPERATIONS_PATH: "operations_sidecar",
        FACET_STATE_PATH: "state_sidecar",
    }
    docs = {path: _canonical_record(files[path], name) for path, name in definitions.items()}
    events, operations, state = (
        docs[FACET_EVENTS_PATH],
        docs[FACET_OPERATIONS_PATH],
        docs[FACET_STATE_PATH],
    )
    _require(
        all(doc["brain_id"] == brain_id for doc in docs.values()),
        "Portable v10 facet Brain binding mismatch",
    )
    try:
        validate_portable_facet_sidecars(events, operations, state)
    except FacetEvidenceError as error:
        raise PortableValidationError(f"Portable v10 facet replay failed: {error.code}") from error
    _references(events, operations, state, references)
    return PortableFacetBundle(
        brain_id,
        MappingProxyType({path: bytes(files[path]) for path in definitions}),
        sha256(files[FACET_STATE_PATH]).hexdigest(),
        sha256(files[FACET_OPERATIONS_PATH]).hexdigest(),
        sha256(canonical({"events": events, "operations": operations, "state": state})).hexdigest(),
        max((event["sequence"] for event in events["events"]), default=0) + 1,
    )


def _facet_route_matches_frozen_state(
    routes: Mapping[str, dict[str, object]],
    *,
    captures: Mapping[str, dict[str, object]],
    revisions: Mapping[str, dict[str, Any]],
    references: FacetPortableReferences,
    state: Mapping[str, Any],
    proposed_at: object,
) -> bool:
    """Resolve frozen logical routing through proven revisions, independent of current state."""
    source_id = references.capture_sources.get(state["capture_id"])
    source_revisions = {
        capture_id: row
        for capture_id, row in revisions.items()
        if references.capture_sources.get(capture_id) == source_id
    }
    if source_id is None or not source_revisions:
        return False
    applicable = {
        route_id: route
        for route_id, route in routes.items()
        if route["capture_id"] in source_revisions
    }
    chosen_id = state["route_id"]
    if chosen_id is None:
        initial_id = min(
            source_revisions, key=lambda identity: source_revisions[identity]["sequence"]
        )
        return captures[initial_id].get("space_id") == state["space_id"] and not any(
            _recorded_before(route["recorded_at"], proposed_at) for route in applicable.values()
        )
    chosen = applicable.get(chosen_id)
    if (
        chosen is None
        or chosen["space_id"] != state["space_id"]
        or _recorded_before(proposed_at, chosen["recorded_at"])
    ):
        return False
    chosen_sequence = source_revisions[cast(str, chosen["capture_id"])]["sequence"]
    children = {
        route["supersedes"]: route_id
        for route_id, route in applicable.items()
        if route["supersedes"] is not None
    }
    successors: set[str] = set()
    successor = children.get(chosen_id)
    while successor is not None:
        if successor in successors:
            return False
        successors.add(successor)
        successor = children.get(successor)
    return not any(
        (
            route_id in successors
            or source_revisions[cast(str, route["capture_id"])]["sequence"] > chosen_sequence
        )
        and _recorded_before(route["recorded_at"], proposed_at)
        for route_id, route in applicable.items()
    )


def validate_facet_binding_record(
    binding: object,
    *,
    proposal: Mapping[str, Any],
    files: Mapping[str, bytes],
    references: FacetPortableReferences,
    tenant_id: str,
) -> dict[str, Any]:
    """Validate retained v5 draft bytes and historical routes across spaces.

    A pending proposal may already be stale. Import preserves that evidence;
    approval must separately revalidate current heads, memberships and authority.
    """
    from open_brain_engine.engine.facet_evidence import FacetEvidenceError, validate_evidence

    try:
        value = validate_evidence("review_binding_v5", binding)
    except FacetEvidenceError as error:
        raise PortableValidationError("invalid version-5 facet review binding") from error
    _require(
        value["tenant_id"] == tenant_id and value["proposal_id"] == proposal.get("proposal_id"),
        "facet review proposal identity mismatch",
    )
    for key in ("actor_id", "role_claim", "recorded_at", "tenant_id"):
        _require(value[key] == proposal.get(key), "facet review proposal metadata mismatch")
    _require(
        value["review_digest"] == review_binding_digest(proposal, value),
        "facet review digest mismatch",
    )
    provenance = value["provenance"]
    _require(
        proposal.get("capture_ids") == provenance
        and [item.get("capture_id") for item in proposal["evidence"]] == provenance,
        "facet review provenance mismatch",
    )
    _require(
        value["destination_space_id"] == proposal.get("space_id")
        and value["destination_space_id"] in references.space_ids,
        "facet review destination mismatch",
    )
    content = proposal["proposed_content"]
    _require(isinstance(content, Mapping), "facet review proposed content is invalid")
    proposed_bytes = base64.b64decode(content["bytes_base64"], validate=True)
    _require(
        sha256(proposed_bytes).hexdigest() == content["sha256"],
        "facet review proposed bytes digest mismatch",
    )
    page = parse_markdown(proposed_bytes)
    expected = {
        "page_id": value["page_id"],
        "provenance": provenance,
        "space_id": value["destination_space_id"],
        "tenant_id": tenant_id,
        "actor_id": value["actor_id"],
        "role_claim": value["role_claim"],
        "privacy": proposal.get("privacy"),
        "modified_at": value["recorded_at"],
        "status": "active",
        "trust": "reviewed",
    }
    _require(
        all(page.fields.get(key) == item for key, item in expected.items()),
        "facet review proposed page binding mismatch",
    )
    captures = _records(files, "sources/captures/", "capture")
    routes = _records(files, "history/routes/", "routing")
    metadata = _portable_record(files[SOURCE_METADATA_PATH], "source metadata")
    revisions = {
        row["capture_id"]: row for row in cast(list[dict[str, Any]], metadata["revisions"])
    }
    for capture_id, source_state in zip(provenance, value["source_states"], strict=True):
        paths = [
            path
            for path in files
            if path.startswith("sources/captures/") and path.endswith(f"/{capture_id}.json")
        ]
        _require(len(paths) == 1 and capture_id in captures, "facet review source is missing")
        _require(
            source_state["sha256"] == sha256(files[paths[0]]).hexdigest()
            and _facet_route_matches_frozen_state(
                routes,
                captures=captures,
                revisions=revisions,
                references=references,
                state=source_state,
                proposed_at=value["recorded_at"],
            ),
            "facet review retained source state mismatch",
        )
    for source_state in value["facet_source_states"]:
        _require(
            references.capture_sources.get(source_state["capture_id"]) == source_state["source_id"],
            "facet review selected source lineage mismatch",
        )
    if value["operation"] == "create":
        _require(
            provenance == value["selected_capture_ids"],
            "facet review create cumulative provenance is invalid",
        )
    else:
        publications = _records(files, "history/publications/", "publication")
        parent = publications.get(value["expected_publication_id"])
        _require(
            parent is not None
            and parent.get("page_id") == value["page_id"]
            and parent.get("published_sha256") == value["expected_page_sha256"],
            "facet review predecessor mismatch",
        )
        assert parent is not None
        inherited = list(
            cast(list[str], parse_markdown(_published_bytes(parent)).fields["provenance"])
        )
        inherited.extend(item for item in value["selected_capture_ids"] if item not in inherited)
        _require(inherited == provenance, "facet review cumulative provenance mismatch")
    return value  # type: ignore[no-any-return]


__all__ = [
    "FACET_EVENTS_PATH",
    "FACET_OPERATIONS_PATH",
    "FACET_STATE_PATH",
    "FACET_SIDECAR_PATHS",
    "FacetPortableReferences",
    "PortableFacetBundle",
    "decode_portable_facets",
    "references_from_portable_files",
    "validate_facet_binding_record",
]
