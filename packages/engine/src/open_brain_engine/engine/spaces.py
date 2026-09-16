"""Inbox, space, routing, and their durable replay operations."""

from __future__ import annotations

import sqlite3
import uuid
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import atomic_write_new
from open_brain_engine.storage.markdown import render_markdown

from .contracts import (
    CaptureAction,
    CaptureFault,
    InboxItem,
    RoutedCapture,
    SpaceRecord,
    _LocalEngineOperations,
    project_public_result_text,
    project_public_space,
)
from .normalization import (
    _MAX_NAME,
    _delivery_id,
    _new_id,
    _portable_id,
    _role_claim,
    _slug,
    _space_row,
    _text,
    _timestamp,
)
from .portability_ports import portable_write_port

if TYPE_CHECKING:
    from .local import BrainEngine


class SpaceOperations(_LocalEngineOperations):
    def _list_inbox(
        self, *, unassigned_only: bool, limit: int | None = None, offset: int = 0
    ) -> tuple[InboxItem, ...]:
        _validate_page(limit, offset)
        sql = (
            "SELECT capture_id, payload_family, space_id, intent, capture_why, "
            "title, search_text AS preview, source_reference "
            "FROM captures WHERE action = ?"
        )
        parameters: list[object] = [CaptureAction.QUICK.value]
        if unassigned_only:
            sql += " AND space_id IS NULL"
        sql += " ORDER BY accepted_at, capture_id LIMIT ? OFFSET ?"
        parameters.extend((-1 if limit is None else limit, offset))
        connection = self._store.connect()
        try:
            rows = tuple(connection.execute(sql, parameters))
        finally:
            connection.close()
        return tuple(
            InboxItem(
                capture_id=cast(str, row["capture_id"]),
                payload_family=cast(str, row["payload_family"]),
                state="inbox",
                space_id=cast(str | None, row["space_id"]),
                intent=_project_optional_text(row, "intent"),
                capture_why=_project_optional_text(row, "capture_why"),
                title=_project_optional_text(row, "title", maximum=120),
                preview=project_public_result_text(
                    cast(str, row["preview"]),
                    protected_literals=(cast(str, row["source_reference"]),),
                )[:320],
            )
            for row in rows
        )

    def _space_operation(
        self, operation: str, space_id: str | None, name: str, delivery_id: str
    ) -> SpaceRecord:
        _delivery_id(delivery_id)
        name = _text(name, field="space name", maximum=_MAX_NAME)
        if operation not in {"create", "rename"}:
            raise ValueError("invalid space operation")
        if space_id is not None:
            _portable_id(space_id, "space")
        request_sha = sha256(
            portable_canonical_json_bytes(
                {"name": name, "operation": operation, "space_id": space_id}
            )
        ).hexdigest()
        conflict: tuple[str, str] | None = None
        created = False
        with self._store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM space_operations WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if existing is not None:
                if cast(str, existing["request_sha256"]) != request_sha:
                    conflict = (cast(str, existing["request_sha256"]), request_sha)
                target_id = cast(str, existing["space_id"])
            else:
                now = _timestamp(self._clock())
                if operation == "create":
                    target_id = _new_id("space")
                    slug = _slug(name, target_id)
                    connection.execute(
                        "INSERT INTO spaces (space_id, name, slug, updated_at) VALUES (?, ?, ?, ?)",
                        (target_id, name, slug, now),
                    )
                else:
                    if space_id is None or _space_row(connection, space_id) is None:
                        raise ValueError("unknown space")
                    target_id = space_id
                    connection.execute(
                        "UPDATE spaces SET name = ?, updated_at = ? WHERE space_id = ?",
                        (name, now, target_id),
                    )
                connection.execute(
                    """
                    INSERT INTO space_operations (
                        delivery_id, request_sha256, operation, space_id, name,
                        receipt_id, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (delivery_id, request_sha, operation, target_id, name, _new_id("receipt"), now),
                )
                created = True
        if conflict is not None:
            self._quarantine(delivery_id, expected=conflict[0], actual=conflict[1])
            raise ValueError("conflicting delivery")
        if created:
            self._fault(CaptureFault.AFTER_SPACE_RESERVATION)
        self._process_space_operation(self._space_operation_row(delivery_id))
        return self._space(target_id)

    def _space_operation_row(self, delivery_id: str) -> sqlite3.Row:
        connection = self._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM space_operations WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("unknown space operation")
        return cast(sqlite3.Row, row)

    def _process_space_operation(self, supplied_row: sqlite3.Row) -> None:
        row = self._space_operation_row(cast(str, supplied_row["delivery_id"]))
        if cast(int, row["stage"]) >= 1:
            return
        space = self._space(cast(str, row["space_id"]))
        payload = render_markdown(
            fields={
                "actor_id": self.profile.owner_actor_id,
                "name": space.name,
                "role_claim": _role_claim(self.profile),
                "schema_version": 1,
                "slug": space.slug,
                "space_id": space.space_id,
                "tenant_id": self.profile.tenant_id,
            },
            body="",
        ).encode("utf-8")
        portable_write_port(self).put_space(
            payload,
            replace=cast(str, row["operation"]) == "rename",
        )
        self._fault(CaptureFault.AFTER_SPACE_WRITE)
        with self._store.transaction() as connection:
            connection.execute(
                "UPDATE space_operations SET stage = 1 WHERE delivery_id = ?",
                (row["delivery_id"],),
            )

    def _space(self, space_id: str) -> SpaceRecord:
        connection = self._store.connect()
        try:
            row = _space_row(connection, space_id)
        finally:
            connection.close()
        if row is None:
            raise ValueError("unknown space")
        return SpaceRecord(
            space_id=cast(str, row["space_id"]),
            name=cast(str, row["name"]),
            slug=cast(str, row["slug"]),
        )

    def _list_spaces(self, *, limit: int | None = None, offset: int = 0) -> tuple[SpaceRecord, ...]:
        _validate_page(limit, offset)
        connection = self._store.connect()
        try:
            rows = tuple(
                connection.execute(
                    "SELECT * FROM spaces ORDER BY name, space_id LIMIT ? OFFSET ?",
                    (-1 if limit is None else limit, offset),
                )
            )
        finally:
            connection.close()
        return tuple(
            SpaceRecord(
                space_id=cast(str, row["space_id"]),
                name=cast(str, row["name"]),
                slug=cast(str, row["slug"]),
            )
            for row in rows
        )

    def _canonical_path(self, space_id: str, page_id: str) -> str:
        space = self._space(space_id)
        return f"content/spaces/{space.slug}/notes/{page_id}.md"

    def _route_capture(self, capture_id: str, space_id: str, delivery_id: str) -> RoutedCapture:
        _portable_id(capture_id, "capture")
        _portable_id(space_id, "space")
        _delivery_id(delivery_id)
        request_sha = sha256(
            portable_canonical_json_bytes({"capture_id": capture_id, "space_id": space_id})
        ).hexdigest()
        conflict: tuple[str, str] | None = None
        created = False
        with self._store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM route_operations WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if existing is not None:
                if cast(str, existing["request_sha256"]) != request_sha:
                    conflict = (cast(str, existing["request_sha256"]), request_sha)
            else:
                capture = connection.execute(
                    "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
                ).fetchone()
                if capture is None or _space_row(connection, space_id) is None:
                    raise ValueError("unknown route target")
                if cast(str, capture["action"]) == CaptureAction.CANONICAL_NOTE.value:
                    raise ValueError("published capture cannot be rerouted")
                if (
                    connection.execute(
                        "SELECT 1 FROM review_sources s JOIN decisions d USING (proposal_id) "
                        "WHERE s.capture_id = ? AND d.outcome IN ('approved', 'edited') LIMIT 1",
                        (capture_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("published capture cannot be rerouted")
                now = _timestamp(self._clock())
                previous = connection.execute(
                    """
                    SELECT current.route_id
                    FROM route_operations AS current
                    LEFT JOIN route_operations AS child
                      ON child.supersedes_route_id = current.route_id
                    WHERE current.capture_id = ? AND child.route_id IS NULL
                    """,
                    (capture_id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO route_operations (
                        delivery_id, request_sha256, route_id, supersedes_route_id,
                        capture_id, space_id, receipt_id, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        delivery_id,
                        request_sha,
                        _new_id("route"),
                        None if previous is None else previous["route_id"],
                        capture_id,
                        space_id,
                        _new_id("receipt"),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE captures SET space_id = ? WHERE capture_id = ?", (space_id, capture_id)
                )
                created = True
        if conflict is not None:
            self._quarantine(delivery_id, expected=conflict[0], actual=conflict[1])
            raise ValueError("conflicting delivery")
        if created:
            self._fault(CaptureFault.AFTER_ROUTE_RESERVATION)
        self._process_route(self._route_row(delivery_id))
        return RoutedCapture(capture_id=capture_id, space_id=space_id)

    def _route_row(self, delivery_id: str) -> sqlite3.Row:
        connection = self._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM route_operations WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("unknown route")
        return cast(sqlite3.Row, row)

    def _process_route(self, supplied_row: sqlite3.Row) -> None:
        row = self._route_row(cast(str, supplied_row["delivery_id"]))
        stage = cast(int, row["stage"])
        if stage < 1:
            capture = self._capture_row(cast(str, row["capture_id"]))
            if cast(str | None, capture["source_path"]) is None:
                self._process_capture(capture)
            portable_write_port(self).put_history(
                "routing",
                portable_canonical_json_bytes(self._routing_record(row)),
            )
            self._fault(CaptureFault.AFTER_ROUTE_SOURCE_WRITE)
            with self._store.transaction() as connection:
                connection.execute(
                    "UPDATE route_operations SET stage = 1 WHERE delivery_id = ?",
                    (row["delivery_id"],),
                )
            stage = 1
        if stage < 2:
            with self._store.transaction() as connection:
                connection.execute(
                    "UPDATE search_documents SET space_id = ?, updated_at = ? WHERE capture_id = ?",
                    (row["space_id"], row["recorded_at"], row["capture_id"]),
                )
                connection.execute(
                    "UPDATE route_operations SET stage = 2 WHERE delivery_id = ?",
                    (row["delivery_id"],),
                )

    def _routing_record(self, row: sqlite3.Row) -> dict[str, object]:
        capture_id = cast(str, row["capture_id"])
        space_id = cast(str, row["space_id"])
        recorded_at = cast(str, row["recorded_at"])
        receipt_payload = {"capture_id": capture_id, "space_id": space_id}
        return {
            "actor_id": self.profile.owner_actor_id,
            "capture_id": capture_id,
            "receipt": {
                "kind": "routing",
                "payload": receipt_payload,
                "receipt_id": row["receipt_id"],
                "recorded_at": recorded_at,
                "sha256": sha256(portable_canonical_json_bytes(receipt_payload)).hexdigest(),
                "subject_id": capture_id,
            },
            "recorded_at": recorded_at,
            "role_claim": _role_claim(self.profile),
            "route_id": row["route_id"],
            "schema_version": 1,
            "space_id": space_id,
            "supersedes": row["supersedes_route_id"],
            "tenant_id": self.profile.tenant_id,
        }

    def _quarantine(self, delivery_id: str, *, expected: str, actual: str) -> None:
        marker = sha256(delivery_id.encode("utf-8")).hexdigest()
        payload = {
            "actual_sha256": actual,
            "delivery_id_sha256": marker,
            "expected_sha256": expected,
            "recorded_at": _timestamp(self._clock()),
            "schema_version": 1,
        }
        atomic_write_new(
            root=self.profile.root,
            relative=f".open-brain/quarantine/{marker}-{uuid.uuid4()}.json",
            data=portable_canonical_json_bytes(payload),
            expected_root_identity=self.profile.root_identity,
        )


class InboxSpaceTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def list(
        self, *, unassigned_only: bool = False, limit: int | None = None, offset: int = 0
    ) -> tuple[InboxItem, ...]:
        return self._engine._list_inbox(unassigned_only=unassigned_only, limit=limit, offset=offset)

    def spaces(self, *, limit: int | None = None, offset: int = 0) -> tuple[SpaceRecord, ...]:
        return tuple(
            _project_space(space) for space in self._engine._list_spaces(limit=limit, offset=offset)
        )

    def create_space(self, name: str, *, delivery_id: str) -> SpaceRecord:
        with self._engine._writer_lease.acquire_shared_writer():
            return _project_space(self._engine._space_operation("create", None, name, delivery_id))

    def rename_space(self, space_id: str, name: str, *, delivery_id: str) -> SpaceRecord:
        with self._engine._writer_lease.acquire_shared_writer():
            return _project_space(
                self._engine._space_operation("rename", space_id, name, delivery_id)
            )

    def route(self, capture_id: str, space_id: str, *, delivery_id: str) -> RoutedCapture:
        with self._engine._writer_lease.acquire_shared_writer():
            return self._engine._route_capture(capture_id, space_id, delivery_id)


def _project_space(space: SpaceRecord) -> SpaceRecord:
    return project_public_space(space)


def _validate_page(limit: int | None, offset: int) -> None:
    if (
        (limit is not None and (type(limit) is not int or not 1 <= limit <= 1000))
        or type(offset) is not int
        or not 0 <= offset <= 1_000_000
    ):
        raise ValueError("invalid page bounds")


def _project_optional_text(
    row: sqlite3.Row, field: str, *, maximum: int | None = None
) -> str | None:
    value = cast(str | None, row[field])
    if value is None:
        return None
    return project_public_result_text(
        value, protected_literals=(cast(str, row["source_reference"]),)
    )[:maximum]
