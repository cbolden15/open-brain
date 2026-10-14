"""Immutable, revision-bound development evidence owned by the engine."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .records import RecordProjector
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine

DEVELOPMENT_EVIDENCE_PATH = "history/developments/evidence-v1.json"
DEVELOPMENT_APPEND_PATH = "history/developments/append-v1.json"


def _ids(values: Sequence[str], *, required: bool = False) -> list[str]:
    from .normalization import _portable_id

    if isinstance(values, str) or len(values) > 32 or (required and not values):
        raise T03Error("invalid_arguments")
    result = sorted(_portable_id(value, "capture") for value in values)
    if len(set(result)) != len(result):
        raise T03Error("invalid_arguments")
    return result


def source_snapshot(connection: sqlite3.Connection, capture_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT s.*,r.source_sha256 FROM source_revisions r "
        "JOIN logical_sources s USING(source_id) WHERE r.capture_id=?",
        (capture_id,),
    ).fetchone()
    if (
        row is None
        or row["head_capture_id"] != capture_id
        or (
            row["historical_only"]
            or row["lifecycle"] != "active"
            or row["availability"] != "available"
        )
    ):
        raise ValueError("development evidence revision conflict")
    return {
        "capture_id": capture_id,
        "source_id": row["source_id"],
        "head_version": row["head_version"],
        "sha256": row["source_sha256"],
        "space_id": row["space_id"],
        "route_version": row["route_version"],
    }


def validate_development_dependencies(
    connection: sqlite3.Connection,
    capture_ids: Sequence[str],
    *,
    engine: BrainEngine | None = None,
    authority: EffectiveAuthority | None = None,
) -> None:
    """Validate transitive supports in the approval reservation transaction."""
    if not connection.in_transaction:
        raise ValueError("development evidence requires transactional validation")
    seen: set[str] = set()
    pending = list(capture_ids)
    while pending:
        identity = pending.pop()
        if identity in seen:
            continue
        seen.add(identity)
        if len(seen) > 1024:
            raise ValueError("development evidence dependency limit")
        row = connection.execute(
            "SELECT receipt_json FROM development_evidence WHERE derived_capture_id=?",
            (identity,),
        ).fetchone()
        if row is None:
            continue
        source_snapshot(connection, identity)
        if engine is not None and authority is not None:
            RecordProjector(engine.profile, connection, authority).source(
                identity, expected=identity
            )
        receipt = json.loads(row[0])
        for frozen in receipt["source_states"]:
            if source_snapshot(connection, frozen["capture_id"]) != frozen:
                raise ValueError("development evidence revision conflict")
            if engine is not None and authority is not None:
                RecordProjector(engine.profile, connection, authority).source(
                    frozen["capture_id"],
                    expected=frozen["capture_id"],
                )
        pending.extend(receipt["supports_development_ids"])


class DevelopmentTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def get(self, derived_capture_id: str, *, authority: EffectiveAuthority) -> dict[str, Any]:
        authority.require("review-read")
        connection = self._engine._store.connect()
        try:
            projector = RecordProjector(self._engine.profile, connection, authority)
            projector.source(derived_capture_id, expected=derived_capture_id, history=True)
            row = connection.execute(
                "SELECT receipt_json FROM development_evidence WHERE derived_capture_id=?",
                (derived_capture_id,),
            ).fetchone()
            if row is None:
                raise T03Error("not_found")
            receipt = cast(dict[str, Any], json.loads(row[0]))
            for identity in receipt["supporting_capture_ids"]:
                projector.source(identity, expected=identity, history=True)
            return receipt
        finally:
            connection.close()

    def bind(
        self,
        derived_capture_id: str,
        supporting_capture_ids: Sequence[str],
        *,
        operation_id: str,
        authority: EffectiveAuthority,
        supports_development_ids: Sequence[str] = (),
        supersedes_development_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        from .normalization import _portable_id

        if not isinstance(authority, EffectiveAuthority):
            raise T03Error("unsupported_capability")
        authority.require("development-bind")
        _portable_id(derived_capture_id, "capture")
        if type(operation_id) is not str or not 1 <= len(operation_id) <= 128:
            raise T03Error("invalid_arguments")
        args: dict[str, Any] = {
            "schema_version": 1,
            "derived_capture_id": derived_capture_id,
            "supporting_capture_ids": _ids(supporting_capture_ids, required=True),
            "supports_development_ids": _ids(supports_development_ids),
            "supersedes_development_ids": _ids(supersedes_development_ids),
            "operation_id": operation_id,
        }
        if any(
            derived_capture_id in args[key]
            for key in (
                "supporting_capture_ids",
                "supports_development_ids",
                "supersedes_development_ids",
            )
        ):
            raise T03Error("invalid_arguments")
        request = canonical(args)
        digest = sha256(request).hexdigest()
        with (
            self._engine._writer_lease.acquire_shared_writer(),
            self._engine._store.transaction() as c,
        ):
            projector = RecordProjector(self._engine.profile, c, authority)
            projector.source(derived_capture_id, expected=derived_capture_id)
            previous = c.execute(
                "SELECT * FROM development_evidence WHERE operation_id=? OR derived_capture_id=?",
                (operation_id, derived_capture_id),
            ).fetchone()
            if previous is not None:
                if (
                    previous["operation_id"] != operation_id
                    or previous["request_sha256"] != digest
                    or previous["principal_id"] != authority.principal_id
                ):
                    raise ValueError("conflicting development evidence operation")
                for identity in args["supporting_capture_ids"]:
                    projector.source(identity, expected=identity, history=True)
                return cast(dict[str, Any], json.loads(previous["receipt_json"]))
            for identity in args["supporting_capture_ids"]:
                projector.source(identity, expected=identity)
            # Evidence cannot be attached after a proposal has already frozen this capture.
            if c.execute(
                "SELECT 1 FROM proposals WHERE capture_id=?", (derived_capture_id,)
            ).fetchone():
                raise ValueError("development evidence must precede proposal")
            for context in c.execute("SELECT binding_json FROM review_contexts"):
                if derived_capture_id in json.loads(context[0])["provenance"]:
                    raise ValueError("development evidence must precede proposal")
            for key in ("supports_development_ids", "supersedes_development_ids"):
                for identity in args[key]:
                    projector.source(
                        identity, expected=identity, history=key == "supersedes_development_ids"
                    )
                    if (
                        c.execute(
                            "SELECT 1 FROM development_evidence WHERE derived_capture_id=?",
                            (identity,),
                        ).fetchone()
                        is None
                    ):
                        raise ValueError("unknown earlier development")
            validate_development_dependencies(
                c, args["supports_development_ids"], engine=self._engine, authority=authority
            )
            from open_brain_engine.core.access_contracts import _TIER_PRECEDENCE

            from .privacy_projection import parse_effective_privacy_json

            def privacy(identity: str) -> Any:
                row = c.execute(
                    "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id=?",
                    (identity,),
                ).fetchone()
                if row is None:
                    raise T03Error("not_found")
                return parse_effective_privacy_json(row[0])

            derived_privacy = privacy(derived_capture_id)
            for identity in args["supporting_capture_ids"] + args["supports_development_ids"]:
                support = privacy(identity)
                if (
                    _TIER_PRECEDENCE[derived_privacy.tier] < _TIER_PRECEDENCE[support.tier]
                    or derived_privacy.authority.cloud
                    and not support.authority.cloud
                    or derived_privacy.authority.external_egress
                    and not support.authority.external_egress
                ):
                    raise ValueError("development evidence privacy conflict")
            receipt = {
                **args,
                "source_states": [
                    source_snapshot(c, identity) for identity in args["supporting_capture_ids"]
                ],
            }
            c.execute(
                "INSERT INTO development_evidence VALUES(?,?,?,?,?,?)",
                (
                    derived_capture_id,
                    operation_id,
                    authority.principal_id,
                    digest,
                    request.decode(),
                    canonical(receipt).decode(),
                ),
            )
            return receipt


def evidence_sidecar(connection: sqlite3.Connection) -> bytes:
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM development_evidence ORDER BY derived_capture_id",
        )
    ]
    return canonical({"schema_version": 1, "operations": rows})


def append_sidecar(connection: sqlite3.Connection) -> bytes:
    rows = []
    for row in connection.execute(
        "SELECT * FROM development_append_operations ORDER BY delivery_id",
    ):
        operation = dict(row)
        if operation["proposal_id"] is None:
            delivery = "append-" + sha256(operation["delivery_id"].encode()).hexdigest()
            proposal = connection.execute(
                "SELECT proposal_id FROM proposals WHERE set_delivery_id=?",
                (delivery,),
            ).fetchone()
            if proposal is not None:
                operation["proposal_id"] = proposal[0]
        rows.append(operation)
    return canonical({"schema_version": 1, "operations": rows})
