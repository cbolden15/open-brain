"""Explicit revision-bound owner decisions; relationships never rewrite source truth."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import StorageError

from .cursors import binding_digest
from .history import bounded_entries, public_timestamp
from .normalization import _timestamp
from .paging import read_snapshot
from .records import RecordProjector
from .t03_contracts import (
    DecisionHistoryRequest,
    DecisionHistoryResponse,
    EffectiveAuthority,
    RelationshipDecideRequest,
    RelationshipDecideResponse,
    RelationshipListRequest,
    RelationshipListResponse,
    T03Error,
)

if TYPE_CHECKING:
    from .local import BrainEngine


def endpoint(record_id: str, revision_id: str) -> dict[str, str]:
    return {"record_id": record_id, "revision_id": revision_id}


def entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "relationship_id": row["relationship_id"],
        "left": endpoint(row["left_record_id"], row["left_revision_id"]),
        "right": endpoint(row["right_record_id"], row["right_revision_id"]),
        "kind": row["kind"],
        "status": {"accept": "accepted", "reject": "rejected", "remove": "removed"}[
            row["decision"]
        ],
        "version": row["version"],
    }


def reaches(
    connection: sqlite3.Connection, start: tuple[str, str], target: tuple[str, str]
) -> bool:
    queue = [start]
    seen = set()
    while queue:
        node = queue.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        queue.extend(
            (row[0], row[1])
            for row in connection.execute(
                "SELECT right_record_id,right_revision_id FROM revision_relationships "
                "WHERE kind='supersedes' AND decision='accept' AND left_record_id=? AND "
                "left_revision_id=?",
                node,
            )
        )
    return False


class RelationshipTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def decide(
        self, request: RelationshipDecideRequest, *, authority: EffectiveAuthority
    ) -> RelationshipDecideResponse:
        try:
            return self._decide(request, authority=authority)
        except T03Error:
            raise
        except OSError, StorageError, sqlite3.Error, ValueError:
            raise T03Error("operation_pending") from None

    def _decide(
        self, request: RelationshipDecideRequest, *, authority: EffectiveAuthority
    ) -> RelationshipDecideResponse:
        if not authority.owner:
            raise T03Error("unsupported_capability")
        self._engine._assert_root()
        digest = binding_digest(request.to_wire())
        with (
            self._engine._writer_lease.acquire_shared_writer(),
            self._engine._store.transaction() as connection,
        ):
            projector = RecordProjector(self._engine.profile, connection, authority)
            # Authenticate access to both retained endpoint identities before exposing
            # replay, head, relationship version, or graph detail.
            for value in (request.left, request.right):
                projector.read(value["record_id"], expected=value["revision_id"], history=True)
            replay = connection.execute(
                "SELECT request_sha256,receipt_json FROM relationship_decisions WHERE "
                "operation_id=?",
                (request.operation_id,),
            ).fetchone()
            if replay is not None:
                if replay["request_sha256"] != digest:
                    raise T03Error("invalid_arguments")
                return cast(
                    RelationshipDecideResponse,
                    RelationshipDecideResponse.from_wire(json.loads(replay["receipt_json"])),
                )
            left_record = projector.read(
                request.left["record_id"], expected=request.left["revision_id"]
            )
            right_record = projector.read(
                request.right["record_id"], expected=request.right["revision_id"]
            )
            left_id = left_record.summary["source_id"] or left_record.summary["record_id"]
            right_id = right_record.summary["source_id"] or right_record.summary["record_id"]
            if left_id == right_id:
                raise T03Error("invalid_arguments")
            left = (left_record.summary["record_id"], left_record.summary["revision_id"])
            right = (right_record.summary["record_id"], right_record.summary["revision_id"])
            if request.kind in {"duplicate_of", "contradicts"} and right < left:
                left, right = right, left
            row = connection.execute(
                "SELECT * FROM revision_relationships WHERE left_record_id=? AND "
                "left_revision_id=? "
                "AND right_record_id=? AND right_revision_id=? AND kind=?",
                (*left, *right, request.kind),
            ).fetchone()
            version = 0 if row is None else row["version"]
            if version != request.expected_relationship_version:
                raise T03Error("revision_changed")
            if version >= 9007199254740991:
                raise T03Error("operation_pending")
            if (
                request.kind == "supersedes"
                and request.decision == "accept"
                and reaches(connection, right, left)
            ):
                raise T03Error("invalid_arguments")
            relationship_id = (
                "relationship_" + str(uuid4()) if row is None else row["relationship_id"]
            )
            decision_id = "decision_" + str(uuid4())
            version += 1
            response = {
                "status": "ok",
                "dto_version": 1,
                "relationship_id": relationship_id,
                "decision_id": decision_id,
                "version": version,
            }
            if row is None:
                connection.execute(
                    "INSERT INTO revision_relationships VALUES(?,?,?,?,?,?,?,?)",
                    (relationship_id, *left, *right, request.kind, request.decision, version),
                )
            else:
                connection.execute(
                    "UPDATE revision_relationships SET decision=?,version=? WHERE "
                    "relationship_id=?",
                    (request.decision, version, relationship_id),
                )
            sequence = connection.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM relationship_decisions"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO relationship_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    relationship_id,
                    sequence,
                    request.operation_id,
                    digest,
                    request.decision,
                    version,
                    _timestamp(self._engine._clock()),
                    self._engine.profile.owner_actor_id,
                    portable_canonical_json_bytes(response).decode(),
                ),
            )
            connection.execute(
                "UPDATE engine_generations SET "
                "retrieval_generation=retrieval_generation+1 WHERE singleton=1"
            )
            return cast(RelationshipDecideResponse, RelationshipDecideResponse.from_wire(response))

    def _visible(
        self, connection: sqlite3.Connection, record_id: str, authority: EffectiveAuthority
    ) -> list[sqlite3.Row]:
        projector = RecordProjector(self._engine.profile, connection, authority)
        current = projector.read(record_id, history=True)
        anchors = {record_id}
        if current.summary["source_id"] is not None:
            anchors = {
                row[0]
                for row in connection.execute(
                    "SELECT capture_id FROM source_revisions WHERE source_id=?",
                    (current.summary["source_id"],),
                )
            }
        visible = []
        for row in connection.execute(
            "SELECT * FROM revision_relationships ORDER BY relationship_id COLLATE BINARY"
        ):
            if row["left_record_id"] not in anchors and row["right_record_id"] not in anchors:
                continue
            try:
                projector.read(
                    row["left_record_id"], expected=row["left_revision_id"], history=True
                )
                projector.read(
                    row["right_record_id"], expected=row["right_revision_id"], history=True
                )
            except T03Error as error:
                if error.code == "not_found":
                    continue
                raise
            visible.append(row)
        return visible

    def list_relationships(
        self, request: RelationshipListRequest, *, authority: EffectiveAuthority
    ) -> RelationshipListResponse:
        authority.require("history-read")
        with read_snapshot(self._engine) as connection:
            entries = [
                entry(row) for row in self._visible(connection, request.record_id, authority)
            ]
            response = bounded_entries(
                self._engine,
                connection,
                request,
                authority,
                purpose="relationship.list",
                entries=entries,
                identity_key="relationship_id",
            )
            return cast(RelationshipListResponse, RelationshipListResponse.from_wire(response))

    def list_decisions(
        self, request: DecisionHistoryRequest, *, authority: EffectiveAuthority
    ) -> DecisionHistoryResponse:
        authority.require("history-read")
        with read_snapshot(self._engine) as connection:
            visible = {
                row["relationship_id"]
                for row in self._visible(connection, request.record_id, authority)
            }
            entries = [
                {
                    "decision_id": row["decision_id"],
                    "relationship_id": row["relationship_id"],
                    "decision": row["decision"],
                    "version": row["relationship_version"],
                    "recorded_at": public_timestamp(row["recorded_at"]),
                }
                for row in connection.execute(
                    "SELECT * FROM relationship_decisions ORDER BY sequence "
                    "DESC,decision_id COLLATE BINARY DESC"
                )
                if row["relationship_id"] in visible
            ]
            response = bounded_entries(
                self._engine,
                connection,
                request,
                authority,
                purpose="decision.history",
                entries=entries,
                identity_key="decision_id",
            )
            return cast(DecisionHistoryResponse, DecisionHistoryResponse.from_wire(response))
