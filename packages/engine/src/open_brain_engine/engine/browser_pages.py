"""Owner-local source binding and grant-scoped lookup of saved page captures."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from open_brain_engine.core.models import PrivacyTier

from .contracts import CaptureSubmission, LocalEngineContext, ReferencePayload
from .local_schema import open_local_database_read_only
from .local_store import _LocalStore
from .records import RecordProjector
from .saved_item_authority import require_saved_item_owner
from .saved_item_identity import SavedItemSource
from .t03_contracts import EffectiveAuthority, T03Error

_MAX_PENDING_PER_BINDING = 64
_MAX_PENDING_INSTALLATION = 4096


@dataclass(frozen=True, slots=True)
class SavedPage:
    capture_id: str
    space_id: str | None


def _accepted(connection: sqlite3.Connection, delivery: str) -> str | None:
    row = connection.execute(
        "SELECT capture_id FROM captures WHERE delivery_id=? AND stage=3 "
        "AND submission_path='public_job' AND source_origin='third_party'",
        (delivery,),
    ).fetchone()
    return str(row[0]) if row else None


@dataclass(frozen=True, slots=True)
class BrowserResult:
    state: str
    capture_id: str | None = None
    annotation_id: str | None = None
    space_id: str | None = None
    revisit_action: str | None = None


@dataclass(frozen=True, slots=True)
class BrowserBinding:
    brain_id: str
    issuer_epoch: int
    generation: str
    source: SavedItemSource
    workflow_principal: str
    operations: frozenset[str]
    tiers: frozenset[str]
    spaces: frozenset[str | None]
    complete_domain: bool


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("invalid browser identifier")
    return value


class BrowserOwnerSetup:
    def __init__(self, profile: LocalEngineContext, authority: EffectiveAuthority) -> None:
        self.profile = profile
        self._authority = authority

    def sources(self, *, limit: int = 100, offset: int = 0) -> tuple[SavedItemSource, ...]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid source page")
        with closing(open_local_database_read_only(self.profile)) as db:
            require_saved_item_owner(self.profile, db, self._authority)
            return tuple(
                SavedItemSource(*row)
                for row in db.execute(
                    "SELECT connector_name,connection_id,resource_id FROM saved_item_sources "
                    "ORDER BY connector_name,connection_id,resource_id LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            )

    def register_legacy(self, source: SavedItemSource, item_key: str) -> None:
        with _LocalStore(self.profile, initialize=False).transaction() as db:
            require_saved_item_owner(self.profile, db, self._authority)
            if _accepted(db, source.delivery_id(item_key)) is None:
                raise ValueError("legacy_rebinding_requires_original_source")
            source.register(db)
            source.witness(db, item_key)

    def create_source(self) -> SavedItemSource:
        source = SavedItemSource("saved_item", "folder:" + uuid4().hex, "items:" + uuid4().hex)
        with _LocalStore(self.profile, initialize=False).transaction() as db:
            require_saved_item_owner(self.profile, db, self._authority)
            source.register(db)
        return source

    def revoke(self, token: str) -> None:
        with _LocalStore(self.profile, initialize=False).transaction() as db:
            require_saved_item_owner(self.profile, db, self._authority)
            db.execute(
                "DELETE FROM browser_grants WHERE token_hash=?",
                (sha256(token.encode()).hexdigest(),),
            )

    def bind(
        self,
        source: SavedItemSource,
        *,
        tiers: Sequence[str],
        spaces: Sequence[str | None],
        operations: Sequence[str] = ("lookup",),
        workflow_principal: str | None = None,
        complete_domain: bool = False,
    ) -> str:
        if not tiers or not set(tiers) <= {"public", "work", "personal"}:
            raise ValueError("invalid browser tiers")
        if (
            not spaces
            or len(spaces) > 256
            or any(
                value is not None
                and (
                    not isinstance(value, str)
                    or re.fullmatch(r"space_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value)
                    is None
                )
                for value in spaces
            )
        ):
            raise ValueError("invalid browser spaces")
        principal = _uuid(workflow_principal) if workflow_principal is not None else str(uuid4())
        if not operations or not set(operations) <= {"lookup", "save", "status"}:
            raise ValueError("invalid browser operations")
        if complete_domain and (set(tiers) != {"public", "work", "personal"} or None not in spaces):
            raise ValueError("incomplete browser domain")
        token = secrets.token_hex(32)
        with _LocalStore(self.profile, initialize=False).transaction() as db:
            require_saved_item_owner(self.profile, db, self._authority)
            if not db.execute(
                "SELECT 1 FROM saved_item_sources WHERE connector_name=? AND "
                "connection_id=? AND resource_id=?",
                source.values(),
            ).fetchone():
                raise ValueError("legacy_rebinding_requires_original_source")
            db.execute(
                "INSERT INTO browser_grants SELECT ?,brain_id,generation,?,?,?,?,?,? "
                "FROM brain_identity CROSS JOIN browser_installation",
                (
                    sha256(token.encode()).hexdigest(),
                    *source.values(),
                    json.dumps(list(operations)),
                    json.dumps(list(tiers)),
                    json.dumps(list(spaces)),
                ),
            )
            db.execute(
                "INSERT INTO browser_grant_bindings SELECT ?,?,issuer_epoch,? FROM brain_identity",
                (sha256(token.encode()).hexdigest(), principal, int(complete_domain)),
            )
        return token


class BrowserPages:
    def __init__(self, profile: LocalEngineContext) -> None:
        self.profile = profile

    @staticmethod
    def _grant(db: sqlite3.Connection, token: str, operation: str | None) -> sqlite3.Row:
        if not isinstance(token, str) or re.fullmatch("[0-9a-f]{64}", token) is None:
            raise PermissionError("browser_unbound")
        row = db.execute(
            "SELECT g.*,x.workflow_principal,x.issuer_epoch,x.complete_domain "
            "FROM browser_grants g "
            "JOIN browser_grant_bindings x USING(token_hash) JOIN browser_installation i "
            "ON i.generation=g.generation JOIN brain_identity b ON b.brain_id=g.brain_id "
            "WHERE g.token_hash=? AND b.issuer_epoch=x.issuer_epoch",
            (sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if row is None:
            raise PermissionError("browser_unbound")
        if operation is not None and operation not in json.loads(row["operations_json"]):
            raise PermissionError("browser_operation_denied")
        return cast(sqlite3.Row, row)

    def require(self, token: str, operation: str) -> None:
        with closing(open_local_database_read_only(self.profile)) as db:
            self._grant(db, token, operation)

    def lookup(self, token: str, item_key: str) -> SavedPage | None:
        with closing(open_local_database_read_only(self.profile)) as db:
            grant = self._grant(db, token, "lookup")
            source = SavedItemSource(
                grant["connector_name"], grant["connection_id"], grant["resource_id"]
            )
            return self._visible(db, grant, source.delivery_id(item_key))

    def _visible(
        self, db: sqlite3.Connection, grant: sqlite3.Row, delivery: str
    ) -> SavedPage | None:
        capture = _accepted(db, delivery)
        if capture is None:
            return None
        spaces = json.loads(grant["spaces_json"])
        row = db.execute(
            "SELECT s.space_id FROM logical_sources s JOIN source_revisions r USING(source_id) "
            "WHERE r.capture_id=? AND s.head_capture_id=?",
            (capture, capture),
        ).fetchone()
        if row is None or (not grant["complete_domain"] and row[0] not in spaces):
            return None
        authority = EffectiveAuthority(
            principal_id="browser",
            session_id="browser-lookup",
            capabilities=frozenset(),
            space_ids=None if row[0] is None else frozenset({row[0]}),
            allowed_read_tiers=frozenset(PrivacyTier(t) for t in json.loads(grant["tiers_json"])),
        )
        try:
            RecordProjector(self.profile, db, authority).source(capture, expected=capture)
        except T03Error as error:
            if error.code == "not_found":
                return None
            raise
        return SavedPage(capture, row[0])

    def binding(self, token: str) -> BrowserBinding:
        with closing(open_local_database_read_only(self.profile)) as db:
            row = self._grant(db, token, None)
            return BrowserBinding(
                row["brain_id"],
                row["issuer_epoch"],
                row["generation"],
                SavedItemSource(row["connector_name"], row["connection_id"], row["resource_id"]),
                row["workflow_principal"],
                frozenset(json.loads(row["operations_json"])),
                frozenset(json.loads(row["tiers_json"])),
                frozenset(json.loads(row["spaces_json"])),
                bool(row["complete_domain"]),
            )

    def save(
        self,
        token: str,
        operation_id: str,
        submission: CaptureSubmission,
        item_key: str,
        *,
        request_digest: str | None = None,
    ) -> BrowserResult:
        from .local import BrainEngine

        binding = self.binding(token)
        digest = request_digest if request_digest is not None else submission.request_sha256()
        if re.fullmatch("[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid browser digest")
        admission = BrowserSaveAdmission(
            token, _uuid(operation_id), digest, binding.source, item_key
        )
        if (
            submission.submission_path.value != "public_job"
            or submission.source_origin.value != "third_party"
            or submission.actor_id != f"actor_{binding.workflow_principal}"
            or submission.requested_tier != PrivacyTier.PERSONAL
            or submission.space_id is not None
            or submission.action.value != "quick"
            or not isinstance(submission.payload, ReferencePayload)
            or submission.source_reference != submission.payload.url
            or submission.delivery_id != binding.source.delivery_id(item_key)
        ):
            raise PermissionError("browser_operation_denied")
        if "save" not in binding.operations:
            raise PermissionError("browser_operation_denied")
        if not binding.complete_domain:
            # A narrowed grant may acknowledge its own still-visible retained result.
            # Everything else receives the same refusal before opening a write path.
            with closing(open_local_database_read_only(self.profile)) as db:
                grant = self._grant(db, token, "save")
                prior = _operation(db, grant, operation_id)
                if (
                    prior is not None
                    and prior["state"] == "accepted"
                    and prior["request_sha256"] == digest
                    and prior["delivery_id"] == binding.source.delivery_id(item_key)
                ):
                    visible = self._visible(db, grant, prior["delivery_id"])
                    if visible:
                        return BrowserResult("accepted", visible.capture_id)
            raise PermissionError("browser_operation_denied")
        engine = BrainEngine.open(self.profile, recover_on_open=False)
        with suppress(BrowserDeliveryRefused):
            engine.capture._submit(submission, selected=admission)
        return self._result(token, operation_id, "save")

    def status(self, token: str, operation_id: str) -> BrowserResult:
        return self._result(token, _uuid(operation_id), "status")

    def _result(self, token: str, operation_id: str, operation: str) -> BrowserResult:
        with closing(open_local_database_read_only(self.profile)) as db:
            if not db.in_transaction:
                db.execute("BEGIN")
            grant = self._grant(db, token, operation)
            if operation == "status" and db.execute("PRAGMA user_version").fetchone()[0] >= 26:
                from .browser_annotations import operation_row
                from .browser_filing import filing_status
                from .browser_revisits import result_for, revisit_row

                revisit = revisit_row(db, grant, operation_id)
                if revisit is not None:
                    return result_for(self, db, grant, revisit)

                route = filing_status(self, db, grant, operation_id)
                if route is not None:
                    return route
                child = operation_row(db, grant, operation_id)
                if child is not None:
                    page = self._visible(db, grant, child["delivery_id"])
                    if (
                        child["state"] == "accepted" and page
                        and page.capture_id == child["capture_id"]
                    ):
                        return BrowserResult("accepted", page.capture_id, child["annotation_id"])
                    return BrowserResult("refused")
            row = _operation(db, grant, operation_id)
            if row is None:
                return BrowserResult("refused")
            visible = (
                self._visible(db, grant, row["delivery_id"]) if row["state"] == "accepted" else None
            )
            if visible:
                return BrowserResult("accepted", visible.capture_id)
            if row["state"] == "pending" and grant["complete_domain"]:
                source = SavedItemSource(
                    grant["connector_name"], grant["connection_id"], grant["resource_id"]
                )
                with suppress(PermissionError):
                    _require_complete_write_domain(db, source)
                    return BrowserResult("pending")
            return BrowserResult("refused")


def _operation(db: sqlite3.Connection, grant: sqlite3.Row, operation_id: str) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        db.execute(
            "SELECT state,request_sha256,delivery_id FROM browser_operations WHERE brain_id=? AND "
            "workflow_principal=? AND connector_name=? AND connection_id=? AND resource_id=? "
            "AND operation_id=?",
            _operation_key(grant, operation_id),
        ).fetchone(),
    )


def _operation_key(grant: sqlite3.Row, operation_id: str) -> tuple[str, ...]:
    return (
        grant["brain_id"],
        grant["workflow_principal"],
        grant["connector_name"],
        grant["connection_id"],
        grant["resource_id"],
        operation_id,
    )


class BrowserDeliveryRefused(ValueError):
    """Admission durably recorded that the journal cannot accept this operation."""


@dataclass(frozen=True, slots=True)
class BrowserSaveAdmission:
    token: str
    operation_id: str
    digest: str
    source: SavedItemSource
    item_key: str

    def admit(self, profile: LocalEngineContext, db: sqlite3.Connection, delivery_id: str) -> None:
        grant = BrowserPages._grant(db, self.token, "save")
        if (
            not grant["complete_domain"]
            or self.source.delivery_id(self.item_key) != delivery_id
            or self.source.values()
            != (grant["connector_name"], grant["connection_id"], grant["resource_id"])
        ):
            raise PermissionError("browser_operation_denied")
        key = _operation_key(grant, self.operation_id)
        if db.execute("PRAGMA user_version").fetchone()[0] >= 26:
            from .browser_annotations import operation_row
            from .browser_filing import filing_row
            from .browser_revisits import revisit_row

            if (
                filing_row(db, grant, self.operation_id)
                or operation_row(db, grant, self.operation_id)
                or revisit_row(db, grant, self.operation_id)
            ):
                raise ValueError("browser_operation_conflict")
        prior = _operation(db, grant, self.operation_id)
        if prior:
            if prior["request_sha256"] != self.digest or prior["delivery_id"] != delivery_id:
                raise ValueError("browser_operation_conflict")
            return
        _require_complete_write_domain(db, self.source)
        counts = db.execute(
            "SELECT count(*),count(*) FILTER(WHERE brain_id=? AND workflow_principal=? AND "
            "connector_name=? AND connection_id=? AND resource_id=?) FROM browser_operations "
            "WHERE state='pending'",
            key[:-1],
        ).fetchone()
        if counts[0] >= _MAX_PENDING_INSTALLATION or counts[1] >= _MAX_PENDING_PER_BINDING:
            raise ValueError("browser_pending_limit")
        terminal = db.execute(
            "SELECT event_kind FROM capture_ingestion_events WHERE delivery_id=? "
            "ORDER BY event_sequence DESC LIMIT 1",
            (delivery_id,),
        ).fetchone()
        discarded = db.execute(
            "SELECT 1 FROM capture_ingestion_tombstones WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if discarded or terminal and terminal[0] in {"quarantined", "discarded"}:
            state = "refused"
        elif terminal:
            state = "accepted" if terminal[0] in {"accepted", "duplicate"} else "pending"
        else:
            state = "accepted" if _accepted(db, delivery_id) else "pending"
        db.execute(
            "INSERT INTO browser_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (*key, "page.save", self.digest, state, delivery_id, self.item_key),
        )


def _require_complete_write_domain(db: sqlite3.Connection, source: SavedItemSource) -> None:
    # Check the entire source before probing the requested delivery. Historical folder
    # identities are one-way digests. Until the owner proves their mapping, a protected
    # legacy delivery may belong to this source and must block new saves uniformly.
    protected = db.execute(
        """WITH protected(delivery_id) AS (
        SELECT c.delivery_id FROM captures c LEFT JOIN source_revision_privacy p USING(capture_id)
        WHERE c.delivery_id GLOB 'connector.saved_item.*' AND c.stage=3 AND
          coalesce(json_extract(p.effective_privacy_json,'$.tier'),'unknown')
          NOT IN ('public','work','personal')
        UNION ALL
        SELECT i.delivery_id FROM capture_ingestion_pending i
        JOIN capture_ingestion_events e ON e.event_sequence=(
          SELECT min(q.event_sequence) FROM capture_ingestion_events q
          WHERE q.delivery_id=i.delivery_id AND q.event_kind='queued'
        )
        WHERE i.delivery_id GLOB 'connector.saved_item.*' AND
          coalesce(json_extract(e.receipt_json,'$.receipt.final_admitted_tier'),'unknown')
          NOT IN ('public','work','personal')
        )
        SELECT 1 FROM protected p WHERE NOT EXISTS (
          SELECT 1 FROM saved_item_local_witnesses w WHERE w.delivery_id=p.delivery_id
          AND (w.connector_name,w.connection_id,w.resource_id) != (?,?,?)
        ) AND NOT EXISTS (
          SELECT 1 FROM browser_operations o WHERE o.delivery_id=p.delivery_id
          AND (o.connector_name,o.connection_id,o.resource_id) != (?,?,?)
        ) LIMIT 1""",
        (*source.values(), *source.values()),
    ).fetchone()
    if protected:
        raise PermissionError("browser_operation_denied")
