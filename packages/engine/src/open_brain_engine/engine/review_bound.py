"""Frozen multi-source review bindings used by the local review engine."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.review_binding import (
    review_binding_digest,
    validate_review_binding,
)
from open_brain_engine.storage.filesystem import StorageError, read_confined
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from .contracts import CaptureFault, ProposalDraft, project_public_result_text
from .normalization import (
    _delivery_id,
    _new_id,
    _portable_id,
    _privacy,
    _receipt,
    _role_claim,
    _timestamp,
    _trust,
)

MAX_REVIEW_SOURCES = 32
MAX_REVIEW_MARKDOWN_BYTES = 64 * 1024
MAX_REVIEW_TITLE_CHARS = 200


def load_bound_context(
    connection: sqlite3.Connection, proposal_id: str
) -> tuple[dict[str, object], dict[str, object]] | None:
    row = connection.execute(
        "SELECT proposal_json, binding_json FROM review_contexts WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return None
    proposal = json.loads(cast(bytes, row["proposal_json"]))
    binding = validate_review_binding(json.loads(cast(bytes, row["binding_json"])))
    if not isinstance(proposal, dict):
        raise ValueError("invalid frozen review proposal")
    if review_binding_digest(proposal, binding) != binding["review_digest"]:
        raise ValueError("invalid frozen review binding")
    return cast(dict[str, object], proposal), binding


def propose_bound(
    engine: Any,
    capture_ids: Sequence[str],
    drafts: Sequence[ProposalDraft],
    *,
    delivery_id: str,
    target_page_id: str | None,
) -> tuple[object, ...]:
    selected = _selected_ids(capture_ids)
    _delivery_id(delivery_id)
    _validate_drafts(drafts)
    if target_page_id is not None:
        _portable_id(target_page_id, "page")
    request_value = {
        "capture_ids": list(selected),
        "drafts": [
            {
                "markdown": draft.markdown,
                "proposed_kind": draft.proposed_kind,
                "supplied_reason": draft.supplied_reason,
                "title": draft.title,
            }
            for draft in drafts
        ],
        "target_page_id": target_page_id,
    }
    request_sha = sha256(portable_canonical_json_bytes(request_value)).hexdigest()
    conflict: tuple[str, str] | None = None
    created = False
    with engine._store.transaction() as connection:
        existing = connection.execute(
            "SELECT * FROM proposal_sets WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if existing is not None:
            if cast(str, existing["request_sha256"]) != request_sha:
                conflict = (cast(str, existing["request_sha256"]), request_sha)
        else:
            captures = _capture_rows(connection, selected)
            target = _target_state(engine, connection, target_page_id)
            space_id = _one_space(captures)
            if target is not None and target["space_id"] != space_id:
                raise ValueError("review sources and target must share a space")
            inherited = () if target is None else cast(tuple[str, ...], target["provenance"])
            provenance = _ordered_unique((*inherited, *selected))
            if len(provenance) > MAX_REVIEW_SOURCES:
                raise ValueError("review source limit exceeded")
            all_captures = _capture_rows(connection, provenance)
            if _one_space(all_captures) != space_id:
                raise ValueError("review sources and target must share a space")
            now = _timestamp(engine._clock())
            proposal_ids = tuple(sorted(_new_id("proposal") for _ in drafts))
            page_ids = tuple(
                cast(str, target["page_id"]) if target is not None else _new_id("page")
                for _ in drafts
            )
            connection.execute(
                "INSERT INTO proposal_sets (delivery_id, request_sha256, capture_id, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                (delivery_id, request_sha, selected[0], now),
            )
            for proposal_id, page_id, draft in zip(proposal_ids, page_ids, drafts, strict=True):
                proposed_bytes = render_bound_page(
                    engine,
                    page_id=page_id,
                    space_id=space_id,
                    provenance=provenance,
                    title=draft.title,
                    body=draft.markdown,
                    modified_at=now,
                )
                canonical_path = (
                    cast(str, target["canonical_path"])
                    if target is not None
                    else engine._canonical_path(space_id, page_id)
                )
                receipt_id = _new_id("receipt")
                connection.execute(
                    """
                    INSERT INTO proposals (
                        proposal_id, set_delivery_id, capture_id, proposed_kind,
                        title, body, proposed_bytes, supplied_reason, space_id,
                        receipt_id, page_id, canonical_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        delivery_id,
                        provenance[0],
                        draft.proposed_kind,
                        draft.title,
                        draft.markdown,
                        proposed_bytes,
                        draft.supplied_reason,
                        space_id,
                        receipt_id,
                        page_id,
                        canonical_path,
                    ),
                )
                proposal = proposal_snapshot(
                    engine,
                    captures=all_captures,
                    proposal_id=proposal_id,
                    receipt_id=receipt_id,
                    proposed_bytes=proposed_bytes,
                    sibling_ids=proposal_ids,
                    supplied_reason=draft.supplied_reason,
                    recorded_at=now,
                )
                binding = binding_snapshot(
                    engine,
                    connection=connection,
                    captures=all_captures,
                    proposal=proposal,
                    proposal_id=proposal_id,
                    page_id=page_id,
                    selected=selected,
                    provenance=provenance,
                    recorded_at=now,
                    target=target,
                )
                proposal_json = portable_canonical_json_bytes(proposal)
                binding_json = portable_canonical_json_bytes(binding)
                connection.execute(
                    "INSERT INTO review_contexts (proposal_id, binding_json, proposal_json) "
                    "VALUES (?, ?, ?)",
                    (proposal_id, binding_json, proposal_json),
                )
                connection.executemany(
                    "INSERT INTO review_sources (proposal_id, capture_id, ordinal) "
                    "VALUES (?, ?, ?)",
                    (
                        (proposal_id, capture_id, ordinal)
                        for ordinal, capture_id in enumerate(provenance)
                    ),
                )
            created = True
    if conflict is not None:
        engine._quarantine(delivery_id, expected=conflict[0], actual=conflict[1])
        raise ValueError("conflicting delivery")
    if created:
        engine._fault(CaptureFault.AFTER_PROPOSAL_RESERVATION)
    engine._process_proposal_set(engine._proposal_set_row(delivery_id))
    return cast(
        tuple[object, ...],
        engine._list_proposals(
            capture_id=None,
            status=None,
            space_id=None,
            limit=None,
            offset=0,
            set_delivery_id=delivery_id,
        ),
    )


def proposal_snapshot(
    engine: Any,
    *,
    captures: tuple[sqlite3.Row, ...],
    proposal_id: str,
    receipt_id: str,
    proposed_bytes: bytes,
    sibling_ids: tuple[str, ...],
    supplied_reason: str | None,
    recorded_at: str,
) -> dict[str, object]:
    evidence: list[dict[str, str]] = []
    references = tuple(cast(str, capture["source_reference"]) for capture in captures)
    for capture in captures:
        candidate = cast(str, capture["search_text"]).strip() or cast(
            str, capture["payload_family"]
        )
        excerpt = project_public_result_text(candidate, protected_literals=references)[:512]
        evidence.append(
            {
                "capture_id": cast(str, capture["capture_id"]),
                "excerpt": excerpt,
                "sha256": sha256(excerpt.encode("utf-8")).hexdigest(),
            }
        )
    receipt_payload = {
        "proposal_id": proposal_id,
        "proposed_content_sha256": sha256(proposed_bytes).hexdigest(),
    }
    return {
        "actor_id": engine.profile.owner_actor_id,
        "capture_ids": [capture["capture_id"] for capture in captures],
        "evidence": evidence,
        "expected_receipt": _receipt(
            "proposal_created", receipt_id, proposal_id, recorded_at, receipt_payload
        ),
        "privacy": _privacy(),
        "proposal_id": proposal_id,
        "proposed_content": {
            "bytes_base64": base64.b64encode(proposed_bytes).decode("ascii"),
            "media_type": "text/markdown",
            "sha256": sha256(proposed_bytes).hexdigest(),
        },
        "proposed_kind": "page_update",
        "recorded_at": recorded_at,
        "role_claim": _role_claim(engine.profile),
        "schema_version": 1,
        "sibling_context": {"proposal_ids": list(sibling_ids)},
        "space_id": captures[0]["space_id"],
        "status": "pending",
        "supplied_reason": supplied_reason,
        "tenant_id": engine.profile.tenant_id,
        "trust": _trust(
            engine.profile,
            recorded_at,
            (
                "third_party"
                if any(capture["source_origin"] == "third_party" for capture in captures)
                else "owner"
            ),
            "proposal retains capture trust",
        ),
    }


def binding_snapshot(
    engine: Any,
    *,
    connection: sqlite3.Connection,
    captures: tuple[sqlite3.Row, ...],
    proposal: Mapping[str, object],
    proposal_id: str,
    page_id: str,
    selected: tuple[str, ...],
    provenance: tuple[str, ...],
    recorded_at: str,
    target: dict[str, object] | None,
) -> dict[str, object]:
    states = [
        {
            "capture_id": capture["capture_id"],
            "route_id": _latest_route_id(connection, cast(str, capture["capture_id"])),
            "sha256": sha256(_source_record_bytes(engine, capture)).hexdigest(),
            "space_id": capture["space_id"],
        }
        for capture in captures
    ]
    binding: dict[str, object] = {
        "actor_id": engine.profile.owner_actor_id,
        "expected_page_sha256": None if target is None else target["published_sha256"],
        "expected_publication_id": None if target is None else target["publication_id"],
        "operation": "create" if target is None else "update",
        "page_id": page_id,
        "proposal_id": proposal_id,
        "provenance": list(provenance),
        "recorded_at": recorded_at,
        "review_digest": "0" * 64,
        "role_claim": _role_claim(engine.profile),
        "schema_version": 3,
        "selected_capture_ids": list(selected),
        "source_states": states,
        "tenant_id": engine.profile.tenant_id,
    }
    binding["review_digest"] = review_binding_digest(proposal, binding)
    return validate_review_binding(binding)


def render_bound_page(
    engine: Any,
    *,
    page_id: str,
    space_id: str,
    provenance: Sequence[str],
    title: str,
    body: str,
    modified_at: str,
) -> bytes:
    return render_markdown(
        fields={
            "actor_id": engine.profile.owner_actor_id,
            "modified_at": modified_at,
            "page_id": page_id,
            "privacy": _privacy(),
            "provenance": list(provenance),
            "role_claim": _role_claim(engine.profile),
            "schema_version": 1,
            "space_id": space_id,
            "status": "active",
            "tenant_id": engine.profile.tenant_id,
            "title": title,
            "trust": "reviewed",
        },
        body=body if body.endswith("\n") else body + "\n",
    ).encode("utf-8")


def validate_current_binding(engine: Any, binding: Mapping[str, object]) -> None:
    connection = engine._store.connect()
    try:
        captures = _capture_rows(connection, cast(Sequence[str], binding["provenance"]))
        state_by_id = {
            cast(str, state["capture_id"]): state
            for state in cast(list[dict[str, object]], binding["source_states"])
        }
        for capture in captures:
            capture_id = cast(str, capture["capture_id"])
            frozen = state_by_id[capture_id]
            if (
                capture["space_id"] != frozen["space_id"]
                or _latest_route_id(connection, capture_id) != frozen["route_id"]
                or sha256(_source_record_bytes(engine, capture)).hexdigest() != frozen["sha256"]
            ):
                raise ValueError("review source state conflict")
        if binding["operation"] == "update":
            head = connection.execute(
                "SELECT * FROM review_page_heads WHERE page_id = ?",
                (binding["page_id"],),
            ).fetchone()
            if head is None:
                target = _target_state(engine, connection, cast(str, binding["page_id"]))
                assert target is not None
                current_publication_id = target["publication_id"]
                current_sha256 = target["published_sha256"]
            else:
                current_publication_id = head["publication_id"]
                current_sha256 = head["published_sha256"]
                current_bytes = read_confined(
                    root=engine.profile.root,
                    relative=cast(str, head["canonical_path"]),
                    expected_root_identity=engine.profile.root_identity,
                    maximum_bytes=MAX_REVIEW_MARKDOWN_BYTES + 16_384,
                )
                if current_bytes is None or sha256(current_bytes).hexdigest() != current_sha256:
                    raise ValueError("canonical page revision conflict")
            if (
                current_publication_id != binding["expected_publication_id"]
                or current_sha256 != binding["expected_page_sha256"]
            ):
                raise ValueError("canonical page revision conflict")
        else:
            proposal = connection.execute(
                "SELECT canonical_path FROM proposals WHERE proposal_id = ?",
                (binding["proposal_id"],),
            ).fetchone()
            if proposal is None or proposal["canonical_path"] is None:
                raise ValueError("canonical page revision conflict")
            if (
                read_confined(
                    root=engine.profile.root,
                    relative=cast(str, proposal["canonical_path"]),
                    expected_root_identity=engine.profile.root_identity,
                    maximum_bytes=MAX_REVIEW_MARKDOWN_BYTES + 16_384,
                )
                is not None
            ):
                raise ValueError("canonical page revision conflict")
    except (StorageError, OSError) as error:
        raise ValueError("canonical page revision conflict") from error
    finally:
        connection.close()


def bound_edited_bytes(
    engine: Any,
    proposal: Mapping[str, object],
    binding: Mapping[str, object],
    body: str,
    modified_at: str,
) -> bytes:
    if len(body.encode("utf-8")) > MAX_REVIEW_MARKDOWN_BYTES:
        raise ValueError("review markdown limit exceeded")
    encoded = cast(str, cast(dict[str, object], proposal["proposed_content"])["bytes_base64"])
    proposed = base64.b64decode(encoded, validate=True)
    parsed = parse_markdown(proposed)
    return render_bound_page(
        engine,
        page_id=cast(str, binding["page_id"]),
        space_id=cast(str, proposal["space_id"]),
        provenance=cast(Sequence[str], binding["provenance"]),
        title=cast(str, parsed.fields["title"]),
        body=body,
        modified_at=modified_at,
    )


def _target_state(
    engine: Any, connection: sqlite3.Connection, page_id: str | None
) -> dict[str, object] | None:
    if page_id is None:
        return None
    head = connection.execute(
        "SELECT * FROM review_page_heads WHERE page_id = ?", (page_id,)
    ).fetchone()
    if head is not None:
        context = load_bound_context(connection, cast(str, head["proposal_id"]))
        if context is None:
            raise ValueError("canonical page revision conflict")
        provenance = tuple(cast(list[str], context[1]["provenance"]))
        current = _read_page(engine, cast(str, head["canonical_path"]))
        if sha256(current).hexdigest() != head["published_sha256"]:
            raise ValueError("canonical page revision conflict")
        return {
            "canonical_path": head["canonical_path"],
            "page_id": page_id,
            "provenance": provenance,
            "publication_id": head["publication_id"],
            "published_sha256": head["published_sha256"],
            "space_id": parse_markdown(current).fields["space_id"],
        }
    owner = connection.execute(
        """
        SELECT publication_id, canonical_path, publication_path
        FROM captures WHERE page_id = ? AND publication_id IS NOT NULL
        UNION ALL
        SELECT publication_id, canonical_path, publication_path
        FROM decisions WHERE page_id = ? AND publication_id IS NOT NULL AND stage >= 2
        LIMIT 1
        """,
        (page_id, page_id),
    ).fetchone()
    if owner is None or owner["canonical_path"] is None or owner["publication_path"] is None:
        raise ValueError("unknown target page")
    current = _read_page(engine, cast(str, owner["canonical_path"]))
    publication_value = json.loads(_read_page(engine, cast(str, owner["publication_path"])))
    published = base64.b64decode(publication_value["published_bytes_base64"], validate=True)
    if current != published:
        raise ValueError("canonical page revision conflict")
    fields = parse_markdown(current).fields
    provenance_value = fields.get("provenance")
    if not isinstance(provenance_value, list) or not provenance_value:
        raise ValueError("canonical page revision conflict")
    provenance = tuple(cast(str, value) for value in provenance_value)
    return {
        "canonical_path": owner["canonical_path"],
        "page_id": page_id,
        "provenance": provenance,
        "publication_id": owner["publication_id"],
        "published_sha256": sha256(current).hexdigest(),
        "space_id": fields["space_id"],
    }


def _selected_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError("invalid review sources")
    selected = tuple(values)
    if not 1 <= len(selected) <= MAX_REVIEW_SOURCES:
        raise ValueError("invalid review sources")
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate review source")
    if any(not isinstance(value, str) or not value.startswith("capture_") for value in selected):
        raise ValueError("invalid review sources")
    return selected


def _validate_drafts(drafts: Sequence[ProposalDraft]) -> None:
    if (
        isinstance(drafts, str)
        or not isinstance(drafts, Sequence)
        or not 1 <= len(drafts) <= 8
        or any(not isinstance(draft, ProposalDraft) for draft in drafts)
    ):
        raise ValueError("invalid proposal set")
    for draft in drafts:
        if draft.proposed_kind != "page_update":
            raise ValueError("bound review requires page update")
        if len(draft.title) > MAX_REVIEW_TITLE_CHARS:
            raise ValueError("review title limit exceeded")
        if len(draft.markdown.encode("utf-8")) > MAX_REVIEW_MARKDOWN_BYTES:
            raise ValueError("review markdown limit exceeded")


def _capture_rows(
    connection: sqlite3.Connection, capture_ids: Sequence[str]
) -> tuple[sqlite3.Row, ...]:
    rows: list[sqlite3.Row] = []
    for capture_id in capture_ids:
        row = connection.execute(
            "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown capture")
        rows.append(row)
    return tuple(rows)


def _one_space(captures: Sequence[sqlite3.Row]) -> str:
    spaces = {cast(str | None, capture["space_id"]) for capture in captures}
    if None in spaces:
        raise ValueError("page proposal requires routed capture")
    if len(spaces) != 1:
        raise ValueError("review sources must share a space")
    return cast(str, next(iter(spaces)))


def _latest_route_id(connection: sqlite3.Connection, capture_id: str) -> str | None:
    rows = connection.execute(
        "SELECT current.route_id FROM route_operations current "
        "LEFT JOIN route_operations child ON child.supersedes_route_id = current.route_id "
        "WHERE current.capture_id = ? AND child.route_id IS NULL",
        (capture_id,),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("review source state conflict")
    return None if not rows else cast(str, rows[0]["route_id"])


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _read_page(engine: Any, relative: str) -> bytes:
    try:
        payload = read_confined(
            root=engine.profile.root,
            relative=relative,
            expected_root_identity=engine.profile.root_identity,
            maximum_bytes=2 * 1024 * 1024,
        )
        if payload is None:
            raise ValueError("canonical page revision conflict")
        return payload
    except (StorageError, OSError) as error:
        raise ValueError("canonical page revision conflict") from error


def _source_record_bytes(engine: Any, capture: sqlite3.Row) -> bytes:
    relative = cast(str | None, capture["source_path"])
    if relative is None or not relative.startswith("sources/captures/"):
        raise ValueError("review source is not durable")
    try:
        payload = read_confined(
            root=engine.profile.root,
            relative=relative,
            expected_root_identity=engine.profile.root_identity,
            maximum_bytes=2 * 1024 * 1024,
        )
        if payload is None:
            raise ValueError("review source is not durable")
        return payload
    except (StorageError, OSError) as error:
        raise ValueError("review source is not durable") from error


__all__ = [
    "MAX_REVIEW_MARKDOWN_BYTES",
    "bound_edited_bytes",
    "load_bound_context",
    "propose_bound",
    "render_bound_page",
    "validate_current_binding",
]
