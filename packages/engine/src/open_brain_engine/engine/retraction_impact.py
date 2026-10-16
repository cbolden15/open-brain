"""Read-only owner retraction impact, not a preview token or approval authority.

Public preview custody and a versioned approval/receipt contract must precede
exposing commit. This snapshot deliberately cannot be passed to journal.retract.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.markdown import parse_markdown

from .capture_lifecycle_contracts import (
    LifecycleError,
    digest,
    validate_capture_id,
    validate_digest,
    validate_session_id,
)
from .capture_lifecycle_journal import LifecycleJournal, _State

_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True, order=True)
class RetractionImpactItem:
    kind: str
    id: str
    effect: str


@dataclass(frozen=True, slots=True)
class RetractionImpact:
    target_kind: str
    target_id: str
    capture_ids: tuple[str, ...]
    membership_version: int | None
    membership_sha256: str
    items: tuple[RetractionImpactItem, ...]
    counts: tuple[tuple[str, int], ...]
    preimage_sha256: str

    def page(self, item_page: int = 0) -> dict[str, object]:
        pages = max(1, (len(self.items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        if type(item_page) is not int or not 0 <= item_page < pages:
            raise LifecycleError("invalid_arguments")
        return {
            "dto_version": 1,
            "status": "impact",
            "target": {"kind": self.target_kind, "id": self.target_id},
            "counts": dict(self.counts),
            "items": [
                {"kind": item.kind, "id": item.id, "effect": item.effect}
                for item in self.items[item_page * _PAGE_SIZE : (item_page + 1) * _PAGE_SIZE]
            ],
            "item_page": item_page,
            "item_pages": pages,
            "next_item_page": item_page + 1 if item_page + 1 < pages else None,
            "history_retained": True,
        }


def _batches(values: Iterable[str]) -> Iterator[tuple[str, ...]]:
    ordered = sorted(set(values))
    for offset in range(0, len(ordered), 128):
        yield tuple(ordered[offset : offset + 128])


def _rows_for(
    connection: sqlite3.Connection, sql: str, identities: Iterable[str]
) -> Iterator[sqlite3.Row]:
    for batch in _batches(identities):
        yield from connection.execute(sql.format(ids=",".join("?" for _ in batch)), batch)


def _row_digest(row: sqlite3.Row) -> str:
    # Some operational evidence is BLOB, some TEXT. Hash bytes without decoding
    # bodies or paths into the retained impact object or its output.
    return digest(
        [
            {"bytes_sha256": sha256(value).hexdigest()} if isinstance(value, bytes) else value
            for value in row
        ]
    )


def _capture_closure(
    connection: sqlite3.Connection,
    seeds: set[str],
    *,
    forward: bool = False,
    state: _State | None = None,
) -> set[str]:
    seen = set(seeds)
    pending = set(seeds)
    while pending:
        found: set[str] = set()
        for table in ("historical_relations", "sharing_links"):
            selected, matched = (
                ("original_capture_id", "copy_capture_id")
                if forward
                else ("copy_capture_id", "original_capture_id")
            )
            found.update(
                row[0]
                for row in _rows_for(
                    connection,
                    f"SELECT {selected} FROM {table} WHERE {matched} IN ({{ids}})",
                    pending,
                )
            )
        for key in (
            "supporting_capture_ids",
            "supports_development_ids",
            "supersedes_development_ids",
        ):
            found.update(
                row[0]
                for row in _rows_for(
                    connection,
                    (
                        "SELECT DISTINCT member.value FROM development_evidence d, "
                        f"json_each(d.receipt_json,'$.{key}') member "
                        "WHERE d.derived_capture_id IN ({ids})"
                    )
                    if forward
                    else (
                        "SELECT DISTINCT d.derived_capture_id FROM development_evidence d, "
                        f"json_each(d.receipt_json,'$.{key}') member "
                        "WHERE member.value IN ({ids})"
                    ),
                    pending,
                )
            )
        if forward and state is not None:
            for capture in pending:
                binding = state.capture_bindings.get(capture)
                if binding is not None:
                    for revision in (binding[1], state.current.get(binding[0])):
                        if revision is None:
                            continue
                        manifest = state.revisions.get((binding[0], revision))
                        if manifest is not None:
                            found.update(member.capture_id for member in manifest.segments)
        pending = found - seen
        seen.update(pending)
    return seen


def _source_rows(connection: sqlite3.Connection, captures: set[str]) -> tuple[sqlite3.Row, ...]:
    return tuple(
        _rows_for(
            connection,
            """
        SELECT r.capture_id,r.source_sha256,r.request_sha256,r.sequence,
               s.source_id,s.head_capture_id,s.head_version,s.route_version,s.lifecycle,
               s.availability,s.historical_only,s.space_id,l.lifecycle_version,
               c.privacy_json,c.provenance_json,c.source_reference,c.stage,p.effective_privacy_json
        FROM source_revisions r JOIN logical_sources s USING(source_id)
        JOIN source_lifecycle_state l USING(source_id) JOIN captures c USING(capture_id)
        JOIN source_revision_privacy p USING(capture_id)
        WHERE r.capture_id IN ({ids}) ORDER BY r.capture_id
    """,
            captures,
        )
    )


def inspect_retraction(
    journal: LifecycleJournal,
    *,
    capture_id: str | None,
    session_id: str | None,
    expected_preimage_sha256: str | None,
) -> RetractionImpact:
    """Resolve all effects in the caller's admitted, consistent read snapshot."""
    if (capture_id is None) == (session_id is None):
        raise LifecycleError("invalid_arguments")
    if expected_preimage_sha256 is not None:
        validate_digest(expected_preimage_sha256)
    connection = journal.connection
    state = journal._state()
    if capture_id is not None:
        validate_capture_id(capture_id)
        target_kind, target_id = "capture", capture_id
        selected: tuple[str, ...] = (capture_id,)
        membership_version = None
    else:
        validate_session_id(session_id)
        assert session_id is not None
        target_kind, target_id = "session", session_id
        if session_id not in state.members:
            raise LifecycleError("operation_not_found")
        selected = tuple(state.members[session_id])
        membership_version = state.session_versions[session_id]

    items: set[RetractionImpactItem] = set()
    suppressed: set[str] = set()
    affected = set(selected)
    retracted = set(state.retracted_captures)
    for session in state.retracted_sessions:
        retracted.update(state.members[session])
    bindings: list[object] = []
    for capture in selected:
        if not state.admitted(capture):
            raise LifecycleError("operation_pending")
        effect = "already_retracted" if capture in retracted else "retract"
        binding = state.capture_bindings.get(capture)
        if binding is not None:
            group, revision = binding
            current = state.current.get(group)
            bindings.append([capture, group, revision, current, group in state.barriers])
            if current != revision:
                if effect != "already_retracted":
                    effect = "historical_only"
                if current is not None:
                    for member in state.revisions[group, current].segments:
                        items.add(RetractionImpactItem("newer_head", member.capture_id, "preserve"))
            elif state.permits(capture, history=False):
                suppressed.add(group)
                affected.update(member.capture_id for member in state.revisions[binding].segments)
        items.add(RetractionImpactItem("capture", capture, effect))
    items.update(
        RetractionImpactItem("current_group", group, "suppress_current") for group in suppressed
    )

    closure = _capture_closure(connection, affected)
    source_rows = _source_rows(connection, closure)
    by_capture = {row["capture_id"]: row for row in source_rows}
    if not set(selected) <= by_capture.keys():
        raise LifecycleError("operation_not_found")
    for capture in selected:
        row = by_capture[capture]
        if row["head_capture_id"] != capture:
            items.add(RetractionImpactItem("newer_head", row["head_capture_id"], "preserve"))
            if capture not in retracted:
                items.discard(RetractionImpactItem("capture", capture, "retract"))
                items.add(RetractionImpactItem("capture", capture, "historical_only"))

    pages = {
        row[0]
        for row in _rows_for(
            connection,
            "SELECT DISTINCT page_id FROM canonical_revision_members WHERE capture_id IN ({ids})",
            closure,
        )
    }
    pages.update(
        row[0]
        for row in _rows_for(
            connection,
            "SELECT page_id FROM captures WHERE capture_id IN ({ids}) AND page_id IS NOT NULL",
            closure,
        )
    )
    # Graph eligibility retains every managed revision and every link. Accepted
    # heads alone omit dependencies still used by retained graph quotations.
    managed_rows = list(
        connection.execute(
            "SELECT note_id,revision_id,body_sha256,body_bytes FROM managed_note_revisions "
            "ORDER BY note_id,revision_id"
        )
    )
    managed_provenance: dict[str, set[str]] = {}
    for row in managed_rows:
        provenance = parse_markdown(bytes(row["body_bytes"])).fields.get("provenance", [])
        if not isinstance(provenance, list) or any(type(value) is not str for value in provenance):
            raise LifecycleError("invalid_evidence")
        managed_provenance.setdefault(row["note_id"], set()).update(provenance)
        if closure.intersection(provenance):
            pages.add(row["note_id"])
    pending = set(pages)
    while pending:
        found = {
            row[0]
            for row in _rows_for(
                connection,
                "SELECT source_note_id FROM managed_links WHERE target_note_id IN ({ids})",
                pending,
            )
        }
        pending = found - pages
        pages.update(pending)
    items.update(
        RetractionImpactItem("dependent_page", page, "dependency_retracted") for page in pages
    )
    copies = {
        row[0]
        for row in _rows_for(
            connection,
            "SELECT copy_capture_id FROM sharing_links WHERE original_capture_id IN ({ids})",
            closure,
        )
    }
    copies.update(
        row[0]
        for row in _rows_for(
            connection,
            "SELECT d.copy_capture_id FROM sharing_decisions d "
            "JOIN sharing_previews p USING(preview_id) "
            "WHERE p.original_capture_id IN ({ids}) AND d.decision='approve' "
            "AND d.copy_capture_id IS NOT NULL",
            closure,
        )
    )
    items.update(
        RetractionImpactItem("approved_copy", copy, "dependency_retracted") for copy in copies
    )
    preserved = {item.id for item in items if item.kind == "newer_head"}
    # Freshness has a wider scope than the effects: a page/development's other
    # evidence may change while the selected capture and output IDs stay fixed.
    read_pages = set(pages)
    pending = set(pages)
    while pending:
        found = {
            row[0]
            for row in _rows_for(
                connection,
                "SELECT target_note_id FROM managed_links WHERE source_note_id IN ({ids})",
                pending,
            )
        }
        pending = found - read_pages
        read_pages.update(pending)
    read_captures = closure | preserved
    read_captures.update(
        row[0]
        for row in _rows_for(
            connection,
            "SELECT capture_id FROM canonical_revision_members WHERE page_id IN ({ids})",
            read_pages,
        )
    )
    read_captures.update(
        row[0]
        for row in _rows_for(
            connection, "SELECT capture_id FROM captures WHERE page_id IN ({ids})", read_pages
        )
    )
    for page in read_pages:
        read_captures.update(managed_provenance.get(page, ()))
    read_captures = _capture_closure(connection, read_captures, forward=True, state=state)
    lifecycle: list[object] = []
    for capture in sorted(read_captures):
        binding = state.capture_bindings.get(capture)
        lifecycle.append(
            [
                capture,
                binding,
                None if binding is None else state.current.get(binding[0]),
                False if binding is None else binding[0] in state.barriers,
                capture in retracted,
                state.permits(capture, history=False),
                sorted(
                    [session, state.session_versions[session], session in state.retracted_sessions]
                    for session, members in state.members.items()
                    if capture in members
                ),
            ]
        )

    # Recompute this whole read set on revalidation. No global retrieval counter:
    # unrelated captures must not stale an otherwise unchanged selected impact.
    preimages: dict[str, object] = {
        "contract": "ob.retraction-impact.v1",
        "brain_id": journal.brain_id,
        "issuer_epoch": journal.issuer_epoch,
        "target": [target_kind, target_id],
        "selected": list(selected),
        "membership_version": membership_version,
        "lifecycle": lifecycle,
        "bindings": bindings,
        "sources": sorted(_row_digest(row) for row in _source_rows(connection, read_captures)),
        "controls": [
            tuple(row)
            for row in connection.execute(
                "SELECT incarnation,authorization_epoch,projection_policy_version,control_epoch "
                "FROM engine_generations"
            )
        ],
        "managed_revisions": sorted(
            _row_digest(row) for row in managed_rows if row["note_id"] in read_pages
        ),
    }
    for label, sql, identities in (
        (
            "members",
            "SELECT * FROM canonical_revision_members WHERE page_id IN ({ids})",
            read_pages,
        ),
        ("heads", "SELECT * FROM review_page_heads WHERE page_id IN ({ids})", read_pages),
        (
            "page_privacy",
            "SELECT DISTINCT p.* FROM canonical_revision_privacy p "
            "JOIN canonical_revision_members m USING(revision_id) WHERE m.page_id IN ({ids})",
            read_pages,
        ),
        ("managed_heads", "SELECT * FROM managed_notes WHERE note_id IN ({ids})", read_pages),
        ("links", "SELECT * FROM managed_links WHERE source_note_id IN ({ids})", read_pages),
        (
            "historical",
            "SELECT * FROM historical_relations WHERE copy_capture_id IN ({ids})",
            read_captures,
        ),
        (
            "sharing",
            "SELECT * FROM sharing_links WHERE original_capture_id IN ({ids})",
            read_captures,
        ),
        (
            "approvals",
            "SELECT d.* FROM sharing_decisions d JOIN sharing_previews p USING(preview_id) "
            "WHERE p.original_capture_id IN ({ids})",
            read_captures,
        ),
        (
            "revocations",
            "SELECT r.* FROM sharing_revocations r JOIN sharing_decisions d USING(approval_id) "
            "JOIN sharing_previews p USING(preview_id) WHERE p.original_capture_id IN ({ids})",
            read_captures,
        ),
        (
            "developments",
            "SELECT * FROM development_evidence WHERE derived_capture_id IN ({ids})",
            read_captures,
        ),
    ):
        preimages[label] = sorted(
            _row_digest(row) for row in _rows_for(connection, sql, identities)
        )
    ordered = tuple(sorted(items))
    counts = (
        ("selected_captures", len(selected)),
        ("already_retracted", len(set(selected).intersection(retracted))),
        ("current_groups_suppressed", len(suppressed)),
        ("newer_heads_preserved", sum(item.kind == "newer_head" for item in ordered)),
        ("dependent_pages", len(pages)),
        ("approved_copies", len(copies)),
    )
    preimages["items"] = [[item.kind, item.id, item.effect] for item in ordered]
    fingerprint = sha256(portable_canonical_json_bytes(preimages)).hexdigest()
    if expected_preimage_sha256 is not None and expected_preimage_sha256 != fingerprint:
        raise LifecycleError("revision_changed")
    return RetractionImpact(
        target_kind,
        target_id,
        selected,
        membership_version,
        digest(list(selected)),
        ordered,
        counts,
        fingerprint,
    )
