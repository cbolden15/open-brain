"""Logical-source route compare-and-swap, separate from immutable capture bytes."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .normalization import _timestamp
from .source_intake import (
    SourceRevisionReceipt,
    SourceRevisionSubmission,
    quarantine_stale_intakes,
)
from .t03_contracts import EffectiveAuthority, SourceRouteRequest, SourceRouteResponse, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine


class SourceTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def fence_intake(self, *, expected_epoch: int, authority: EffectiveAuthority) -> int:
        """Trusted owner coordination hook; collectors must bind their admission separately."""
        if not authority.owner:
            raise T03Error("unsupported_capability")
        if type(expected_epoch) is not int or expected_epoch < 0:
            raise T03Error("invalid_arguments")
        with self._engine._writer_lease.acquire_shared_writer():
            with self._engine._store.transaction() as connection:
                epoch = connection.execute(
                    "SELECT control_epoch FROM engine_generations"
                ).fetchone()[0]
                if epoch != expected_epoch:
                    raise T03Error("revision_changed")
                connection.execute("UPDATE engine_generations SET control_epoch=control_epoch+1")
            quarantine_stale_intakes(self._engine)
            return cast(int, epoch + 1)

    def submit_revision(self, submission: SourceRevisionSubmission) -> SourceRevisionReceipt:
        if not isinstance(submission, SourceRevisionSubmission):
            raise T03Error("invalid_arguments")
        submission.capture.validate_profile(self._engine.profile)
        with self._engine._writer_lease.acquire_shared_writer():
            return self._submit_revision_locked(submission)

    def _submit_revision_locked(
        self, submission: SourceRevisionSubmission
    ) -> SourceRevisionReceipt:
        quarantine_stale_intakes(self._engine)
        namespace_json = submission.namespace_bytes().decode("utf-8")
        namespace_sha = sha256(namespace_json.encode("utf-8")).hexdigest()
        conflict = False
        with self._engine._store.transaction() as connection:
            epoch = connection.execute("SELECT control_epoch FROM engine_generations").fetchone()[0]
            if submission.expected_control_epoch != epoch:
                raise T03Error("revision_changed")
            existing = connection.execute(
                "SELECT * FROM source_intakes WHERE namespace_sha256=? AND revision_key=?",
                (namespace_sha, submission.revision_key),
            ).fetchone()
            source = connection.execute(
                "SELECT s.* FROM source_namespaces n JOIN logical_sources s USING(source_id) "
                "WHERE n.namespace_sha256=?",
                (namespace_sha,),
            ).fetchone()
            if source is None and submission.expected_head is not None and existing is None:
                # Adoption requires the retained alias AND exact canonical submission bytes.
                # This explicitly records the provider key for the proven current baseline.
                legacy = connection.execute(
                    "SELECT s.*,r.request_sha256,r.revision_key FROM source_aliases a "
                    "JOIN logical_sources s USING(source_id) JOIN source_revisions r "
                    "ON r.capture_id=s.head_capture_id WHERE a.delivery_id=? "
                    "AND NOT EXISTS(SELECT 1 FROM source_namespaces n "
                    "WHERE n.source_id=s.source_id)",
                    (submission.capture.delivery_id,),
                ).fetchone()
                if (
                    legacy is not None
                    and legacy["head_capture_id"] == submission.expected_head
                    and legacy["request_sha256"] == submission.canonical_sha256
                    and legacy["revision_key"] is None
                    and submission.ordering["kind"] != "predecessor"
                    and not legacy["historical_only"]
                ):
                    connection.execute(
                        "INSERT INTO source_namespaces VALUES(?,?,?)",
                        (namespace_sha, namespace_json, legacy["source_id"]),
                    )
                    connection.execute(
                        "UPDATE source_revisions SET revision_key=?,ordering_json=? "
                        "WHERE capture_id=?",
                        (
                            submission.revision_key,
                            json.dumps(dict(submission.ordering), sort_keys=True),
                            submission.expected_head,
                        ),
                    )
                    receipt = dict(
                        source_id=legacy["source_id"],
                        capture_id=submission.expected_head,
                        outcome="captured",
                        control_epoch=epoch,
                    )
                    connection.execute(
                        "INSERT INTO source_intakes VALUES(?,?,?,?,?,?,?,?)",
                        (
                            namespace_sha,
                            submission.revision_key,
                            legacy["source_id"],
                            submission.canonical_sha256,
                            "adoption." + str(uuid4()),
                            "{}",
                            submission.custody_bytes(),
                            json.dumps(receipt, sort_keys=True),
                        ),
                    )
                    return SourceRevisionReceipt(**receipt)
            if existing is not None and existing["request_sha256"] == submission.canonical_sha256:
                if existing["receipt_json"] is not None:
                    return SourceRevisionReceipt(**json.loads(existing["receipt_json"]))
                delivery_id = existing["delivery_id"]
            else:
                if existing is not None:
                    conflict = True
                if source is None:
                    if submission.expected_head is not None:
                        raise T03Error("revision_changed")
                    if submission.ordering["kind"] == "predecessor":
                        conflict = True
                    source_id = "source_" + str(uuid4())
                    promote, predecessor = True, None
                else:
                    source_id = source["source_id"]
                    if source["head_capture_id"] != submission.expected_head:
                        raise T03Error("revision_changed")
                    head = connection.execute(
                        "SELECT * FROM source_revisions WHERE capture_id=?",
                        (source["head_capture_id"],),
                    ).fetchone()
                    previous_order = json.loads(head["ordering_json"] or "null")
                    order = submission.ordering
                    predecessor = None
                    promote = False
                    if order["kind"] == "predecessor":
                        predecessor = head["capture_id"]
                        promote = order["revision_key"] == head["revision_key"]
                        conflict |= not promote
                    elif order["kind"] == "monotonic" and previous_order is not None:
                        comparable = all(
                            order[key] == previous_order.get(key)
                            for key in ("kind", "provider_namespace", "epoch")
                        )
                        conflict |= not comparable or order["sequence"] == previous_order.get(
                            "sequence"
                        )
                        promote = comparable and order["sequence"] > previous_order["sequence"]
                    else:
                        conflict = True
                if conflict:
                    connection.execute(
                        "INSERT INTO source_quarantine VALUES(?,?,?,?,?,?)",
                        (
                            "custody_" + str(uuid4()),
                            source["source_id"] if source is not None else None,
                            submission.revision_key,
                            submission.canonical_sha256,
                            submission.custody_bytes(),
                            _timestamp(self._engine._clock()),
                        ),
                    )
                    delivery_id = ""
                else:
                    pending = connection.execute(
                        "SELECT 1 FROM source_intakes WHERE (source_id=? OR namespace_sha256=?) "
                        "AND receipt_json IS NULL",
                        (source_id, namespace_sha),
                    ).fetchone()
                    if pending is not None:
                        raise T03Error("operation_pending")
                    delivery_id = "revision." + str(uuid4())
                    plan = {
                        "namespace_json": namespace_json,
                        "ordering": dict(submission.ordering),
                        "expected_head": submission.expected_head,
                        "predecessor_capture_id": predecessor,
                        "promote": promote,
                        "control_epoch": epoch,
                        "legacy_delivery_id": submission.capture.delivery_id,
                    }
                    connection.execute(
                        "INSERT INTO source_intakes VALUES(?,?,?,?,?,?,?,NULL)",
                        (
                            namespace_sha,
                            submission.revision_key,
                            source_id,
                            submission.canonical_sha256,
                            delivery_id,
                            json.dumps(plan, sort_keys=True),
                            submission.custody_bytes(),
                        ),
                    )
        if conflict:
            raise T03Error("source_revision_conflict")
        self._engine._submit_capture(replace(submission.capture, delivery_id=delivery_id))
        connection = self._engine._store.connect()
        try:
            receipt = connection.execute(
                "SELECT receipt_json FROM source_intakes WHERE delivery_id=?", (delivery_id,)
            ).fetchone()[0]
            if receipt is None:
                raise T03Error("operation_pending")
            return SourceRevisionReceipt(**json.loads(receipt))
        finally:
            connection.close()

    def route(
        self, request: SourceRouteRequest, *, authority: EffectiveAuthority
    ) -> SourceRouteResponse:
        authority.require("organize")
        with self._engine._writer_lease.acquire_shared_writer():
            return self._route_locked(request, authority=authority)

    def _route_locked(
        self, request: SourceRouteRequest, *, authority: EffectiveAuthority
    ) -> SourceRouteResponse:
        digest = sha256(portable_canonical_json_bytes(request.to_wire())).hexdigest()
        with self._engine._store.transaction() as connection:
            source = connection.execute(
                "SELECT * FROM logical_sources WHERE source_id=?", (request.source_id,)
            ).fetchone()
            if source is None or not authority.permits_space(source["space_id"]):
                raise T03Error("not_found")
            if (
                not authority.permits_space(request.space_id)
                or connection.execute(
                    "SELECT 1 FROM spaces WHERE space_id=?", (request.space_id,)
                ).fetchone()
                is None
            ):
                raise T03Error("not_found")
            existing = connection.execute(
                "SELECT * FROM source_operations WHERE operation_id=?", (request.operation_id,)
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != digest:
                    raise T03Error("invalid_arguments")
                stored = json.loads(existing["receipt_json"])
                if stored["status"] == "ok":
                    return cast(SourceRouteResponse, SourceRouteResponse.from_wire(stored))
            else:
                if (
                    source["head_capture_id"] != request.expected_head
                    or source["route_version"] != request.expected_route_version
                ):
                    raise T03Error("revision_changed")
                if source["historical_only"] or source["lifecycle"] != "active":
                    raise T03Error("not_found")
                connection.execute(
                    "INSERT INTO source_operations VALUES(?,?,?,?)",
                    (
                        request.operation_id,
                        digest,
                        request.source_id,
                        json.dumps(
                            {"status": "pending", "request": request.to_wire()}, sort_keys=True
                        ),
                    ),
                )
        self._engine._route_capture(
            request.expected_head,
            request.space_id,
            "source.route." + request.operation_id,
            allow_published=True,
        )
        receipt: dict[str, Any] = {
            "status": "ok",
            "dto_version": 1,
            "source_id": request.source_id,
            "head": request.expected_head,
            "route_version": request.expected_route_version + 1,
        }
        response = cast(SourceRouteResponse, SourceRouteResponse.from_wire(receipt))
        with self._engine._store.transaction() as connection:
            connection.execute(
                "UPDATE logical_sources SET route_version=? WHERE source_id=?",
                (receipt["route_version"], request.source_id),
            )
            connection.execute(
                "UPDATE source_operations SET receipt_json=? WHERE operation_id=?",
                (json.dumps(receipt, sort_keys=True), request.operation_id),
            )
        return response

    def _recover_locked(self) -> None:
        connection = self._engine._store.connect()
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] < 7:
                return
            pending = [
                json.loads(row[0])["request"]
                for row in connection.execute(
                    "SELECT receipt_json FROM source_operations "
                    "WHERE json_extract(receipt_json,'$.status')='pending' ORDER BY operation_id"
                )
            ]
        finally:
            connection.close()
        authority = EffectiveAuthority(
            self._engine.profile.owner_actor_id, "source-recovery", frozenset(), None, owner=True
        )
        for request in pending:
            self._route_locked(SourceRouteRequest(**request), authority=authority)
