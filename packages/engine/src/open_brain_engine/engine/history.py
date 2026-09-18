"""Bounded immutable history, authorized against the current logical record."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.v4 import canonical_revision_id
from open_brain_engine.storage.filesystem import read_confined

from .cursors import CursorStore, binding_digest
from .normalization import _dated_path
from .paging import (
    MAX_RESPONSE_BYTES,
    authority_binding,
    continuation,
    generations,
    read_record,
    read_snapshot,
)
from .records import RecordProjector
from .review_bound import load_bound_context
from .search_projection import public_search_text
from .t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    HistoryListResponse,
    RecordReadRequest,
    RecordReadResponse,
    T03Error,
)

if TYPE_CHECKING:
    from .local import BrainEngine


def public_timestamp(value: str) -> str:
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def canonical_versions(projector: RecordProjector, page_id: str) -> list[dict[str, Any]]:
    """Order causal publication chains; independent roots have no invented predecessor."""
    connection = projector.connection
    rows = connection.execute(
        "SELECT revision_id,publication_id,min(rowid) AS insertion_sequence "
        "FROM canonical_revision_members WHERE page_id=? GROUP BY revision_id,publication_id",
        (page_id,),
    ).fetchall()
    versions: dict[str, dict[str, Any]] = {}
    for row in rows:
        publication, _ = projector._publication(row["publication_id"])
        predecessor = None
        bound = connection.execute(
            "SELECT proposal_id FROM decisions WHERE publication_id=?", (row["publication_id"],)
        ).fetchone()
        reason = "Independent retained publication; causal order unavailable"
        if bound is not None:
            context = load_bound_context(connection, bound["proposal_id"])
            if context is not None:
                _proposal, binding = context
                path = _dated_path(
                    "history/review-bindings",
                    cast(str, binding["recorded_at"]),
                    cast(str, binding["proposal_id"]),
                )
                durable = read_confined(
                    root=projector.profile.root,
                    relative=path,
                    expected_root_identity=projector.profile.root_identity,
                )
                if (
                    durable != portable_canonical_json_bytes(binding)
                    or binding["page_id"] != page_id
                ):
                    raise T03Error("operation_pending")
                previous = binding["expected_publication_id"]
                predecessor = None if previous is None else canonical_revision_id(str(previous))
                reason = "Published revision" if predecessor is None else "Updated publication"
        versions[row["revision_id"]] = dict(
            row, predecessor=predecessor, recorded_at=publication["recorded_at"], reason=reason
        )
    depths: dict[str, int] = {}

    def depth(revision: str, visiting: frozenset[str] = frozenset()) -> int:
        if revision in visiting or revision not in versions:
            raise T03Error("operation_pending")
        if revision not in depths:
            previous = versions[revision]["predecessor"]
            depths[revision] = 0 if previous is None else 1 + depth(previous, visiting | {revision})
        return depths[revision]

    for revision in versions:
        depth(revision)
    # Causal depth supplies the engine sequence within a proven chain. The binary
    # tie-break is presentation only; roots explicitly retain an unordered diagnostic.
    return sorted(
        versions.values(), key=lambda v: (depths[v["revision_id"]], v["revision_id"]), reverse=True
    )


def history_entries(projector: RecordProjector, record_id: str) -> list[dict[str, Any]]:
    current = projector.read(record_id, history=True)
    entries = []
    if record_id.startswith("capture_"):
        source_id = current.summary["source_id"]
        source = projector.connection.execute(
            "SELECT * FROM logical_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        rows = projector.connection.execute(
            "SELECT * FROM source_revisions WHERE source_id=? "
            "ORDER BY sequence DESC,capture_id COLLATE BINARY DESC",
            (source_id,),
        )
        for row in rows:
            projected = projector.read(record_id, expected=row["capture_id"], history=True)
            _, capture = projector._capture(row["capture_id"])
            reason = (
                "Ungrouped legacy history; causal order unavailable"
                if row["diagnostic"]
                else str(capture["capture_why"] or "Retained source revision")
            )
            entries.append(
                {
                    "revision_id": row["capture_id"],
                    "predecessor_revision_id": row["predecessor_capture_id"],
                    "recorded_at": public_timestamp(row["recorded_at"]),
                    "reason": public_search_text(
                        reason, protected_source_reference=capture["source"]["reference"]
                    ),
                    "lifecycle": source["lifecycle"],
                    "availability": source["availability"],
                    "is_current": not source["historical_only"]
                    and row["capture_id"] == source["head_capture_id"],
                    "provenance": projected.summary["provenance"],
                }
            )
    else:
        for version in canonical_versions(projector, record_id):
            projected = projector.read(record_id, expected=version["revision_id"], history=True)
            entries.append(
                {
                    "revision_id": version["revision_id"],
                    "predecessor_revision_id": version["predecessor"],
                    "recorded_at": public_timestamp(version["recorded_at"]),
                    "reason": version["reason"],
                    "lifecycle": "active",
                    "availability": "available",
                    "is_current": version["revision_id"] == current.summary["revision_id"],
                    "provenance": projected.summary["provenance"],
                }
            )
    return entries


def bounded_entries(
    engine: BrainEngine,
    connection: sqlite3.Connection,
    request: HistoryListRequest,
    authority: EffectiveAuthority,
    *,
    purpose: str,
    entries: list[dict[str, Any]],
    identity_key: str,
) -> dict[str, Any]:
    generation = generations(connection)
    now = engine._clock().timestamp()
    store = CursorStore(engine.profile)
    binding = binding_digest(
        {
            "record_id": request.record_id,
            "limit": request.limit,
            "authority": authority_binding(authority),
        }
    )
    prior = continuation(
        store, request.cursor, purpose=purpose, binding=binding, generation=generation, now=now
    )
    start = 0
    if prior is not None:
        last = prior.get("last_id")
        positions = [index for index, entry in enumerate(entries) if entry[identity_key] == last]
        if len(positions) != 1:
            raise T03Error("cursor_invalid")
        start = positions[0] + 1
    page: list[dict[str, Any]] = []
    for entry in entries[start:]:
        if (
            len(page) == request.limit
            or len(portable_canonical_json_bytes(page + [entry])) > MAX_RESPONSE_BYTES
        ):
            if not page:
                raise T03Error("response_too_large")
            break
        page.append(entry)
    complete = start + len(page) == len(entries)
    cursor = (
        None
        if complete
        else store.allocate(
            {
                "purpose": purpose,
                "binding": binding,
                "generation": generation,
                "last_id": page[-1][identity_key],
            },
            now=now,
        )
    )
    return {
        "status": "ok",
        "dto_version": 1,
        "entries": page,
        "complete": complete,
        "next_cursor": cursor,
    }


class HistoryTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def list_history(
        self, request: HistoryListRequest, *, authority: EffectiveAuthority
    ) -> HistoryListResponse:
        authority.require("history-read")
        with read_snapshot(self._engine) as connection:
            projector = RecordProjector(self._engine.profile, connection, authority)
            entries = history_entries(projector, request.record_id)
            response = bounded_entries(
                self._engine,
                connection,
                request,
                authority,
                purpose="history.list",
                entries=entries,
                identity_key="revision_id",
            )
            return cast(HistoryListResponse, HistoryListResponse.from_wire(response))

    def read_history(
        self, request: RecordReadRequest, *, authority: EffectiveAuthority
    ) -> RecordReadResponse:
        return read_record(self._engine, request, authority=authority, history=True)
