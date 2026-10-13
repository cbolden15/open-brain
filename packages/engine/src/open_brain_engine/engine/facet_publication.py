"""Pure facet review bindings; transactional publication awaits facet storage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

from open_brain_engine.portable.review_binding import (
    review_binding_digest,
    validate_review_binding,
)

from .facet_contracts import ProposeFromFacetQueryRequest, ProposeFromFacetQueryResponse
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine

MAX_FACET_PUBLICATION_SOURCES = 32


def selected_capture_ids(rows: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """Accept the complete source result, including an optional 33rd overflow row."""
    if not rows:
        raise ValueError("facet query selected no sources")
    if len(rows) > MAX_FACET_PUBLICATION_SOURCES:
        raise ValueError("facet query source limit exceeded")
    ids = tuple(cast(str, row["capture_id"]) for row in rows)
    sources = tuple(cast(str, row["source_id"]) for row in rows)
    if len(set(ids)) != len(ids) or len(set(sources)) != len(sources):
        raise ValueError("facet query selected duplicate sources")
    return ids


def ordered_provenance(
    inherited_capture_ids: Sequence[str], selected_capture_ids: Sequence[str]
) -> tuple[str, ...]:
    """Retain prior order and append newly selected captures in query order."""
    provenance = tuple(dict.fromkeys((*inherited_capture_ids, *selected_capture_ids)))
    if not 1 <= len(provenance) <= MAX_FACET_PUBLICATION_SOURCES:
        raise ValueError("facet publication provenance limit exceeded")
    return provenance


def build_facet_binding(
    proposal: Mapping[str, object],
    base: Mapping[str, object],
    *,
    destination_space_id: str,
    query_id: str,
    query_version: int,
    query_selection_version: int,
    selector: Mapping[str, object],
    attachment_version: int,
    facet_source_states: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Extend frozen full-page evidence with v5 selection and exact proposal digest."""
    binding = dict(base)
    binding.update(
        schema_version=5,
        selection_kind="facet",
        destination_space_id=destination_space_id,
        query_id=query_id,
        query_version=query_version,
        query_selection_version=query_selection_version,
        selector=dict(selector),
        attachment_version=attachment_version,
        facet_source_states=[dict(state) for state in facet_source_states],
    )
    binding["review_digest"] = review_binding_digest(proposal, binding)
    return validate_review_binding(binding)


def verify_facet_snapshot(
    binding: Mapping[str, object],
    *,
    query: Mapping[str, object],
    attachment: Mapping[str, object] | None,
    selected_heads: Mapping[str, Mapping[str, object]],
    provenance_states: Mapping[str, Mapping[str, object]],
    authorized_capture_ids: frozenset[str],
    authority: EffectiveAuthority,
    target: Mapping[str, object] | None,
) -> None:
    """Compare a v5 binding to one caller-provided, authorized, consistent snapshot.

    The caller must resolve source heads, present selected memberships, capture
    bytes/routes, target and authority within its transactional publication seam.
    This function performs no I/O.
    """
    if binding.get("schema_version") != 5:
        raise ValueError("facet binding version conflict")
    destination = cast(str, binding["destination_space_id"])
    if not authority.permits_space(destination):
        raise ValueError("facet destination authority conflict")
    if (
        query.get("query_id") != binding["query_id"]
        or query.get("archived") is not False
        or query.get("selection_version") != binding["query_selection_version"]
        or query.get("selector") != binding["selector"]
    ):
        raise ValueError("facet query selection conflict")
    if binding["operation"] == "create":
        if attachment is not None or target is not None:
            raise ValueError("facet publication target conflict")
    elif (
        attachment is None
        or attachment.get("page_id") != binding["page_id"]
        or attachment.get("query_id") != binding["query_id"]
        or attachment.get("version") != binding["attachment_version"]
        or target is None
        or target.get("space_id") != destination
        or target.get("publication_id") != binding["expected_publication_id"]
        or target.get("sha256") != binding["expected_page_sha256"]
    ):
        raise ValueError("facet publication target conflict")
    for frozen in cast(Sequence[Mapping[str, object]], binding["source_states"]):
        capture_id = cast(str, frozen["capture_id"])
        current = provenance_states.get(capture_id)
        if (
            capture_id not in authorized_capture_ids
            or current is None
            or any(current.get(key) != frozen[key] for key in ("sha256", "route_id", "space_id"))
            or not authority.permits_space(cast(str | None, current["space_id"]))
        ):
            raise ValueError("facet provenance conflict")
    for frozen in cast(Sequence[Mapping[str, object]], binding["facet_source_states"]):
        source_id = cast(str, frozen["source_id"])
        current = selected_heads.get(source_id)
        if (
            current is None
            or current.get("capture_id") != frozen["capture_id"]
            or current.get("head_version") != frozen["head_version"]
        ):
            raise ValueError("facet source head conflict")
        memberships = cast(Mapping[str, object], current.get("memberships", {}))
        for pair in cast(Sequence[Mapping[str, object]], frozen["memberships"]):
            if memberships.get(cast(str, pair["value_id"])) != pair["membership_version"]:
                raise ValueError("facet membership conflict")


class FacetPublicationTasks:
    """C0 protocol shape; live transaction wiring is an E integration step."""

    def __init__(self, engine: BrainEngine) -> None:
        self.engine = engine

    def propose_from_facet_query(
        self, request: ProposeFromFacetQueryRequest, *, authority: EffectiveAuthority
    ) -> ProposeFromFacetQueryResponse:
        for capability in ("facet-read", "search", "review-propose"):
            authority.require(capability)
        raise NotImplementedError("facet publication requires verified E transactional seams")
