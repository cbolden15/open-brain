"""Facet-query full-page proposals and frozen publication evidence."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from open_brain_engine.core.access_contracts import (
    aggregate_privacy_decisions,
    validate_stored_privacy_decision,
)
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.portable.review_binding import (
    review_binding_base_version,
    review_binding_digest,
    validate_review_binding,
)
from open_brain_engine.storage.filesystem import StorageError, read_confined
from open_brain_engine.storage.markdown import parse_markdown

from .contracts import CaptureFault
from .facet_contracts import (
    FacetError,
    ProposeFromFacetQueryRequest,
    ProposeFromFacetQueryResponse,
    request_from_wire,
)
from .normalization import _new_id, _timestamp
from .records import RecordProjector
from .t03_contracts import EffectiveAuthority, T03Error

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
    if review_binding_base_version(binding) != 5:
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
    """Persist frozen proposals through the existing review stage machine."""

    def __init__(self, engine: BrainEngine) -> None:
        self.engine = engine

    def propose_from_facet_query(
        self, request: ProposeFromFacetQueryRequest, *, authority: EffectiveAuthority
    ) -> ProposeFromFacetQueryResponse:
        args = request_from_wire("review.propose_from_facet_query", request).to_wire()
        try:
            for capability in ("facet-read", "search", "review-propose"):
                authority.require(capability)
            if args["target_page_id"] is None:
                authority.require("facet-query-write")
        except (AttributeError, T03Error):
            raise FacetError("unsupported_capability") from None
        self.engine._assert_root()
        try:
            with self.engine._writer_lease.acquire_shared_writer():
                self.engine._drain_review_decisions()
                return self._propose(args, authority)
        except T03Error as error:
            code = (
                error.code if error.code in {"not_found", "revision_changed"}
                else "operation_pending"
            )
            raise FacetError(code) from None
        except (sqlite3.Error, StorageError):
            raise FacetError("operation_pending") from None

    def _propose(
        self, args: dict[str, Any], authority: EffectiveAuthority
    ) -> ProposeFromFacetQueryResponse:
        from .facet_queries import (
            resolve_query,
            resolve_subject,
            select_records,
            selected_source_state,
        )
        from .facet_store import get_row
        from .review_bound import (
            _capture_rows,
            _target_state,
            binding_snapshot,
            load_bound_context,
            proposal_snapshot,
            render_bound_page,
        )

        engine = self.engine
        delivery_id = "facet-review-" + sha256(args["operation_id"].encode()).hexdigest()
        request_digest = sha256(portable_canonical_json_bytes(
            {"method": "review.propose_from_facet_query", "arguments": args,
             "principal_id": authority.principal_id}
        )).hexdigest()
        created = False
        with engine._store.transaction() as connection:
            query = resolve_query(connection, args["query_id"], authority)
            existing = connection.execute(
                "SELECT request_sha256 FROM proposal_sets WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_digest:
                    raise FacetError("idempotency_conflict")
                row = connection.execute(
                    "SELECT proposal_id FROM proposals WHERE set_delivery_id=?", (delivery_id,)
                ).fetchone()
                if row is None:
                    raise FacetError("operation_pending")
                context = load_bound_context(connection, row["proposal_id"])
                if context is None:
                    raise FacetError("operation_pending")
                binding = context[1]
                authorize_facet_evidence(engine, connection, binding, authority)
            else:
                if query["version"] != args["expected_query_version"]:
                    raise FacetError("version_conflict")
                if query["archived"]:
                    raise FacetError("query_archived")
                selector = query["selector"]
                if selector["record_types"] and "source" not in selector["record_types"]:
                    raise FacetError("invalid_arguments")
                target = None
                attachment_version = 0
                if args["target_page_id"] is not None:
                    subject = resolve_subject(
                        connection, engine.profile, args["target_page_id"], authority
                    )
                    if subject.revision_id != args["expected_page_revision_id"]:
                        raise FacetError("revision_changed")
                    target = _target_state(engine, connection, args["target_page_id"])
                    assert target is not None
                    attachment = get_row(connection, "attachment", args["target_page_id"])
                    if attachment is None or attachment["query_id"] != args["query_id"]:
                        raise FacetError("review_conflict")
                    attachment_version = attachment["version"]
                destination = cast(
                    str, args["destination_space_id"] if target is None else target["space_id"]
                )
                require_destination(connection, destination, authority)
                selections = select_records(
                    connection, engine.profile, selector, authority, source_only=True, limit=33
                )
                if not selections:
                    raise FacetError("invalid_arguments")
                if len(selections) > MAX_FACET_PUBLICATION_SOURCES:
                    raise FacetError("limit_exceeded")
                states = [selected_source_state(item) for item in selections]
                selected = selected_capture_ids(states)
                inherited = () if target is None else cast(Sequence[str], target["provenance"])
                try:
                    provenance = ordered_provenance(inherited, selected)
                except ValueError:
                    raise FacetError("limit_exceeded") from None
                projector = RecordProjector(engine.profile, connection, authority)
                for capture_id in provenance:
                    projector.source(capture_id, expected=capture_id, history=True)
                captures = _capture_rows(connection, provenance)
                privacy, trust = publication_policy(engine, captures)
                if not authority.permits_read_tier(PrivacyTier(cast(str, privacy["tier"]))):
                    raise FacetError("not_found")
                now = _timestamp(engine._clock())
                proposal_id, receipt_id = _new_id("proposal"), _new_id("receipt")
                page_id = _new_id("page") if target is None else cast(str, target["page_id"])
                proposed_bytes = render_bound_page(
                    engine, page_id=page_id, space_id=destination, provenance=provenance,
                    title=args["title"], body=args["markdown"], modified_at=now, privacy=privacy,
                )
                canonical_path = (
                    engine._canonical_path(destination, page_id)
                    if target is None else target["canonical_path"]
                )
                proposal = proposal_snapshot(
                    engine, captures=captures, proposal_id=proposal_id, receipt_id=receipt_id,
                    proposed_bytes=proposed_bytes, sibling_ids=(proposal_id,), supplied_reason=None,
                    recorded_at=now, destination_space_id=destination, privacy=privacy,
                    trust_label=trust,
                )
                binding = binding_snapshot(
                    engine, connection=connection, captures=captures, proposal=proposal,
                    proposal_id=proposal_id, page_id=page_id, selected=selected,
                    provenance=provenance, recorded_at=now, target=target, patch=None,
                    facet={
                        "selection_kind": "facet", "destination_space_id": destination,
                        "query_id": query["query_id"], "query_version": query["version"],
                        "query_selection_version": query["selection_version"],
                        "selector": selector, "attachment_version": attachment_version,
                        "facet_source_states": states,
                    },
                )
                connection.execute(
                    "INSERT INTO proposal_sets (delivery_id,request_sha256,capture_id,recorded_at) "
                    "VALUES (?,?,?,?)", (delivery_id, request_digest, selected[0], now),
                )
                connection.execute(
                    "INSERT INTO proposals (proposal_id,set_delivery_id,capture_id,proposed_kind,"
                    "title,body,proposed_bytes,supplied_reason,space_id,receipt_id,"
                    "page_id,canonical_path) "
                    "VALUES (?,?,?,'page_update',?,?,?,NULL,?,?,?,?)",
                    (proposal_id, delivery_id, provenance[0], args["title"], args["markdown"],
                     proposed_bytes, destination, receipt_id, page_id, canonical_path),
                )
                connection.execute(
                    "INSERT INTO review_contexts (proposal_id,binding_json,proposal_json) "
                    "VALUES (?,?,?)",
                    (proposal_id, portable_canonical_json_bytes(binding),
                     portable_canonical_json_bytes(proposal)),
                )
                connection.executemany(
                    "INSERT INTO review_sources (proposal_id,capture_id,ordinal) VALUES (?,?,?)",
                    ((proposal_id, capture_id, ordinal)
                     for ordinal, capture_id in enumerate(provenance)),
                )
                created = True
        if created:
            engine._fault(CaptureFault.AFTER_PROPOSAL_RESERVATION)
        engine._process_proposal_set(engine._proposal_set_row(delivery_id))
        return ProposeFromFacetQueryResponse(portable_canonical_json_bytes({
            "dto_version": 1, "status": "proposed", "proposal_status": "pending",
            "effective_idempotency_key": args["operation_id"],
            "proposal_id": binding["proposal_id"], "page_id": binding["page_id"],
            "space_id": binding["destination_space_id"], "operation": binding["operation"],
            "target_page_id": args["target_page_id"],
        }).decode("utf-8"))


def owner_review_authority(engine: Any) -> EffectiveAuthority:
    """Authority for the existing explicitly local owner review task."""
    return EffectiveAuthority(
        principal_id=engine.profile.owner_actor_id, session_id="local-owner-review",
        capabilities=frozenset(), space_ids=None, owner=True,
    )


def require_destination(
    connection: sqlite3.Connection, space_id: str, authority: EffectiveAuthority
) -> None:
    if not authority.permits_space(space_id) or connection.execute(
        "SELECT 1 FROM spaces WHERE space_id=?", (space_id,)
    ).fetchone() is None:
        raise FacetError("not_found")


def authorize_facet_evidence(
    engine: Any, connection: sqlite3.Connection, binding: Mapping[str, object],
    authority: EffectiveAuthority,
) -> None:
    """Current read eligibility for inspection/replay, without proposal grants."""
    from .facet_queries import authorize_subject, require_query_scope

    require_query_scope(cast(dict[str, Any], binding["selector"]), authority)
    require_destination(connection, cast(str, binding["destination_space_id"]), authority)
    projector = RecordProjector(engine.profile, connection, authority)
    for capture_id in cast(Sequence[str], binding["provenance"]):
        projector.source(capture_id, expected=capture_id, history=True)
    if binding["operation"] == "update":
        authorize_subject(
            connection, engine.profile,
            {"kind": "canonical", "id": cast(str, binding["page_id"])}, authority,
        )


def publication_policy(
    engine: Any, captures: Sequence[sqlite3.Row],
) -> tuple[dict[str, object], str]:
    """Use the same complete provenance privacy join as canonical projection."""
    from .review_bound import _source_record_bytes

    records = [json.loads(_source_record_bytes(engine, capture)) for capture in captures]
    privacy_values = [record["privacy"] for record in records]
    try:
        aggregate = aggregate_privacy_decisions(privacy_values)
    except ValueError:
        raise FacetError("review_conflict") from None
    reasons = {
        "public": "policy_public", "work": "policy_work", "personal": "personal_local_only",
        "secret": "secret_detected", "unknown": "classification_missing",
    }
    confirmation_ref = None
    if aggregate.tier is PrivacyTier.PERSONAL and (
        aggregate.authority.cloud or aggregate.authority.external_egress
    ):
        if len(aggregate.confirmation_refs) != 1:
            raise FacetError("review_conflict")
        confirmation_ref = aggregate.confirmation_refs[0]
        reasons["personal"] = "personal_confirmed"
    privacy: dict[str, object] = {
        "tier": aggregate.tier.value, "reason": reasons[aggregate.tier.value],
        "policy_version": "privacy-v1", "confirmation_ref": confirmation_ref,
        "authority": {
            "cloud": aggregate.authority.cloud,
            "external_egress": aggregate.authority.external_egress,
        },
    }
    try:
        validate_stored_privacy_decision(privacy)
    except ValueError:
        raise FacetError("review_conflict") from None
    labels = {record["trust"]["label"] for record in records}
    if not labels <= {"owner", "third_party", "unverified"}:
        raise FacetError("review_conflict")
    origins = {RecordProjector._origin(record) for record in records}
    trust = (
        "unverified" if "unverified" in labels or origins & {"mixed", "unknown"} else
        "third_party" if "third_party" in labels or "third_party" in origins else "owner"
    )
    return privacy, trust


def validate_facet_current_binding(
    engine: Any, connection: sqlite3.Connection, binding: Mapping[str, object], *,
    authority: EffectiveAuthority | None,
) -> None:
    """Validate selected evidence inside the transaction that reserves approval."""
    from .facet_queries import authorize_subject, membership_rows, resolve_query
    from .facet_store import get_row
    from .review_bound import (
        _capture_rows,
        _facet_source_state,
        _target_state,
        load_bound_context,
    )

    authority = owner_review_authority(engine) if authority is None else authority
    if connection.execute(
        "SELECT 1 FROM decisions WHERE page_id=? AND stage<3 "
        "AND outcome IN ('approved','edited')", (binding["page_id"],)
    ).fetchone() is not None:
        raise FacetError("review_conflict")
    authorize_facet_evidence(engine, connection, binding, authority)
    query = resolve_query(connection, cast(str, binding["query_id"]), authority)
    heads: dict[str, Mapping[str, object]] = {}
    for state in cast(Sequence[Mapping[str, Any]], binding["facet_source_states"]):
        subject = authorize_subject(
            connection, engine.profile, {"kind": "source", "id": state["source_id"]}, authority
        )
        heads[subject.id] = {
            "capture_id": subject.record_id, "head_version": subject.head_version,
            "memberships": {item["value_id"]: item["membership_version"]
                            for item in membership_rows(connection, "source", subject.id)},
        }
    captures = _capture_rows(connection, cast(Sequence[str], binding["provenance"]))
    provenance = {
        capture["capture_id"]: _facet_source_state(engine, connection, capture)
        for capture in captures
    }
    target = (
        _target_state(engine, connection, cast(str, binding["page_id"]))
        if binding["operation"] == "update" else None
    )
    verify_facet_snapshot(
        binding, query=query, attachment=get_row(connection, "attachment", binding["page_id"]),
        selected_heads=heads, provenance_states=provenance,
        authorized_capture_ids=frozenset(provenance), authority=authority,
        target=None if target is None else {**target, "sha256": target["published_sha256"]},
    )
    row = connection.execute(
        "SELECT * FROM proposals WHERE proposal_id=?", (binding["proposal_id"],)
    ).fetchone()
    context = load_bound_context(connection, cast(str, binding["proposal_id"]))
    if row is None or context is None or context[1] != binding:
        raise FacetError("review_conflict")
    draft = cast(bytes, row["proposed_bytes"])
    proposal = context[0]
    content = cast(Mapping[str, object], proposal["proposed_content"])
    privacy, trust = publication_policy(engine, captures)
    if not authority.permits_read_tier(PrivacyTier(cast(str, privacy["tier"]))):
        raise FacetError("not_found")
    parsed = parse_markdown(draft)
    expected_path = (
        engine._canonical_path(binding["destination_space_id"], binding["page_id"])
        if target is None else target["canonical_path"]
    )
    if (
        draft != base64.b64decode(cast(str, content["bytes_base64"]), validate=True)
        or sha256(draft).hexdigest() != content["sha256"]
        or parsed.fields["space_id"] != binding["destination_space_id"]
        or parsed.fields["page_id"] != binding["page_id"]
        or parsed.fields["provenance"] != binding["provenance"]
        or parsed.fields["privacy"] != privacy
        or proposal["privacy"] != privacy
        or cast(Mapping[str, object], proposal["trust"])["label"] != trust
        or row["space_id"] != binding["destination_space_id"]
        or row["page_id"] != binding["page_id"]
        or row["canonical_path"] != expected_path
        or proposal["space_id"] != binding["destination_space_id"]
        or proposal["capture_ids"] != binding["provenance"]
    ):
        raise FacetError("review_conflict")
    if binding["operation"] == "create" and read_confined(
        root=engine.profile.root, relative=row["canonical_path"],
        expected_root_identity=engine.profile.root_identity,
    ) is not None:
        raise FacetError("review_conflict")


def commit_publication_attachment(
    engine: Any, connection: sqlite3.Connection, *, binding: Mapping[str, object],
    publication_id: str, recorded_at: str,
) -> None:
    """Commit the create attachment with the page head in publication stage three."""
    if binding["operation"] != "create":
        return
    from .facet_store import attach_for_publication

    brain = connection.execute("SELECT brain_id FROM brain_identity WHERE singleton=1").fetchone()
    if brain is None:
        raise FacetError("operation_pending")
    attach_for_publication(
        connection, brain_id=brain[0], authority=owner_review_authority(engine),
        actor_id=engine.profile.owner_actor_id,
        recorded_at=recorded_at, publication_id=publication_id,
        page_id=cast(str, binding["page_id"]), query_id=cast(str, binding["query_id"]),
        query_selection_version=cast(int, binding["query_selection_version"]),
    )
