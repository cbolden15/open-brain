"""Follow-up items: client-proposed commitments and open questions with owner decisions.

Items are updated only by compare-and-swap on ``version``. Every write appends one
``followup_decisions`` row keyed by ``operation_id``; a repeated operation with the
same request digest returns the original receipt and never writes again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from open_brain_engine.core.models import PrivacyTier

from .cursors import binding_digest
from .followup_contracts import (
    CLOSED_STATES,
    MAX_EVIDENCE_PER_ITEM,
    MAX_ITEMS,
    OPEN_STATES,
    AddEvidenceRequest,
    DecideRequest,
    DecisionAction,
    EvidenceRef,
    EvidenceRole,
    FollowupDecisionRow,
    FollowupError,
    FollowupEvidenceView,
    FollowupItem,
    FollowupKind,
    FollowupState,
    ListFilter,
    OwnerKind,
    ProposeRequest,
    request_wire,
)
from .followup_visibility import evidence_ref, rebuild_quote, visible_to
from .normalization import _timestamp
from .paging import read_snapshot
from .privacy_projection import parse_effective_privacy_json
from .records import RecordProjector
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine

_ITEM_COLUMNS = (
    "item_id,kind,project,summary,owner,owner_name,owner_confirmed,due,due_evidence_id,"
    "state,snoozed_until,snooze_return_state,ticket_key,ticket_url,superseded_by,resolution,"
    "privacy_tier,version,created_at,updated_at,evidence_retracted,new_evidence_since_decision"
)


def _date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def item_from_row(row: sqlite3.Row) -> FollowupItem:
    return FollowupItem(
        item_id=row["item_id"],
        kind=FollowupKind(row["kind"]),
        project=row["project"],
        summary=row["summary"],
        owner=OwnerKind(row["owner"]),
        owner_name=row["owner_name"],
        owner_confirmed=bool(row["owner_confirmed"]),
        due=_date(row["due"]),
        due_evidence_id=row["due_evidence_id"],
        state=FollowupState(row["state"]),
        snoozed_until=_date(row["snoozed_until"]),
        snooze_return_state=(
            None
            if row["snooze_return_state"] is None
            else FollowupState(row["snooze_return_state"])
        ),
        ticket_key=row["ticket_key"],
        ticket_url=row["ticket_url"],
        superseded_by=row["superseded_by"],
        resolution=row["resolution"],
        privacy_tier=row["privacy_tier"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        evidence_retracted=bool(row["evidence_retracted"]),
        new_evidence_since_decision=bool(row["new_evidence_since_decision"]),
    )


def effective_item(item: FollowupItem, today: date) -> FollowupItem:
    """Project snooze expiry at read time; the stored row and version are untouched."""
    if (
        item.state is FollowupState.SNOOZED
        and item.snoozed_until is not None
        and item.snoozed_until <= today
    ):
        return replace(
            item,
            state=item.snooze_return_state or FollowupState.SUGGESTED,
            snoozed_until=None,
            snooze_return_state=None,
        )
    return item


def _item_wire(item: FollowupItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "kind": item.kind.value,
        "project": item.project,
        "summary": item.summary,
        "owner": item.owner.value,
        "owner_name": item.owner_name,
        "owner_confirmed": item.owner_confirmed,
        "due": None if item.due is None else item.due.isoformat(),
        "due_evidence_id": item.due_evidence_id,
        "state": item.state.value,
        "snoozed_until": None if item.snoozed_until is None else item.snoozed_until.isoformat(),
        "snooze_return_state": (
            None if item.snooze_return_state is None else item.snooze_return_state.value
        ),
        "ticket_key": item.ticket_key,
        "ticket_url": item.ticket_url,
        "superseded_by": item.superseded_by,
        "resolution": item.resolution,
        "privacy_tier": item.privacy_tier,
        "version": item.version,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "evidence_retracted": item.evidence_retracted,
        "new_evidence_since_decision": item.new_evidence_since_decision,
    }


def _item_from_wire(value: dict[str, Any]) -> FollowupItem:
    return FollowupItem(
        item_id=value["item_id"],
        kind=FollowupKind(value["kind"]),
        project=value["project"],
        summary=value["summary"],
        owner=OwnerKind(value["owner"]),
        owner_name=value["owner_name"],
        owner_confirmed=bool(value["owner_confirmed"]),
        due=_date(value["due"]),
        due_evidence_id=value["due_evidence_id"],
        state=FollowupState(value["state"]),
        snoozed_until=_date(value["snoozed_until"]),
        snooze_return_state=(
            None
            if value["snooze_return_state"] is None
            else FollowupState(value["snooze_return_state"])
        ),
        ticket_key=value["ticket_key"],
        ticket_url=value["ticket_url"],
        superseded_by=value["superseded_by"],
        resolution=value["resolution"],
        privacy_tier=value["privacy_tier"],
        version=int(value["version"]),
        created_at=value["created_at"],
        updated_at=value["updated_at"],
        evidence_retracted=bool(value["evidence_retracted"]),
        new_evidence_since_decision=bool(value["new_evidence_since_decision"]),
    )


def decision_from_row(row: sqlite3.Row) -> FollowupDecisionRow:
    return FollowupDecisionRow(
        decision_id=row["decision_id"],
        item_id=row["item_id"],
        sequence=row["sequence"],
        operation_id=row["operation_id"],
        principal_id=row["principal_id"],
        action=row["action"],
        prior_state=None if row["prior_state"] is None else FollowupState(row["prior_state"]),
        new_state=FollowupState(row["new_state"]),
        prior_version=row["prior_version"],
        new_version=row["new_version"],
        evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
        recorded_at=row["recorded_at"],
    )


class FollowupTasks:
    """Follow-up operations for the owner CLI and granted MCP sessions."""

    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    # -- shared plumbing -------------------------------------------------------

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._engine._assert_root()
        with (
            self._engine._writer_lease.acquire_shared_writer(),
            self._engine._store.transaction() as connection,
        ):
            yield connection

    def _today(self) -> date:
        return self._engine._clock().date()

    def _now(self) -> str:
        return _timestamp(self._engine._clock())

    def _replay(
        self, connection: sqlite3.Connection, operation_id: str, digest: str
    ) -> FollowupItem | None:
        row = connection.execute(
            "SELECT request_sha256,receipt_json,item_id FROM followup_decisions "
            "WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != digest:
            raise FollowupError("operation_conflict")
        if row["receipt_json"] is None:
            # Rows restored from Portable 18 carry no receipt; replay returns the current item.
            current = self._row(connection, row["item_id"])
            assert current is not None
            return item_from_row(current)
        return _item_from_wire(json.loads(row["receipt_json"]))

    def _log(
        self,
        connection: sqlite3.Connection,
        *,
        item: FollowupItem,
        operation_id: str,
        digest: str,
        principal_id: str,
        action: str,
        prior_state: FollowupState | None,
        prior_version: int,
        evidence_ids: tuple[str, ...],
    ) -> None:
        sequence = connection.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM followup_decisions WHERE item_id=?",
            (item.item_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO followup_decisions(decision_id,item_id,sequence,operation_id,"
            "request_sha256,principal_id,action,prior_state,new_state,prior_version,"
            "new_version,evidence_ids_json,receipt_json,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "decision_" + str(uuid4()),
                item.item_id,
                sequence,
                operation_id,
                digest,
                principal_id,
                action,
                None if prior_state is None else prior_state.value,
                item.state.value,
                prior_version,
                item.version,
                json.dumps(sorted(evidence_ids), separators=(",", ":")),
                json.dumps(_item_wire(item), sort_keys=True, separators=(",", ":")),
                item.updated_at,
            ),
        )

    @staticmethod
    def _require_readable(projector: RecordProjector, ref: EvidenceRef) -> None:
        """The session can read the cited source now; ``not_found`` otherwise."""
        try:
            projector.read(ref.capture_id)
        except T03Error as error:
            if error.code == "not_found":
                raise FollowupError("not_found") from None
            raise

    def _verify_evidence(
        self,
        connection: sqlite3.Connection,
        projector: RecordProjector,
        ref: EvidenceRef,
    ) -> PrivacyTier:
        """Position, hash, currency and readability of one quote; returns its tier."""
        try:
            record = projector.read(ref.capture_id, expected=ref.revision_id)
        except T03Error as error:
            if error.code == "not_found":
                raise FollowupError("not_found") from None
            if error.code == "revision_changed":
                raise FollowupError("evidence_mismatch") from None
            raise
        if rebuild_quote(record.text, ref) is None:
            raise FollowupError("evidence_mismatch")
        row = connection.execute(
            "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id=?",
            (ref.capture_id,),
        ).fetchone()
        if row is None:
            raise FollowupError("not_found")
        try:
            return parse_effective_privacy_json(row["effective_privacy_json"]).tier
        except ValueError:
            raise FollowupError("not_found") from None

    def _insert_evidence(
        self,
        connection: sqlite3.Connection,
        item_id: str,
        ref: EvidenceRef,
        recorded_at: str,
        evidence_id: str,
    ) -> str:
        ordinal = connection.execute(
            "SELECT count(*) FROM followup_evidence WHERE item_id=?", (item_id,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO followup_evidence(evidence_id,item_id,ordinal,role,capture_id,"
            "revision_id,quote_start,quote_end,quote_sha256,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                evidence_id,
                item_id,
                ordinal,
                ref.role.value,
                ref.capture_id,
                ref.revision_id,
                ref.quote_start,
                ref.quote_end,
                ref.quote_sha256,
                recorded_at,
            ),
        )
        return evidence_id

    def _row(self, connection: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = connection.execute(
            f"SELECT {_ITEM_COLUMNS} FROM followup_items WHERE item_id=?", (item_id,)
        ).fetchone()
        return row

    def _evidence_rows(self, connection: sqlite3.Connection, item_id: str) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM followup_evidence WHERE item_id=? ORDER BY ordinal",
            (item_id,),
        ).fetchall()

    def _visible_item(
        self,
        connection: sqlite3.Connection,
        item_id: str,
        *,
        authority: EffectiveAuthority,
    ) -> tuple[FollowupItem, list[sqlite3.Row]]:
        """The stored item when the session may see it; ``not_found`` otherwise."""
        row = self._row(connection, item_id)
        if row is None:
            raise FollowupError("not_found")
        item = item_from_row(row)
        evidence = self._evidence_rows(connection, item_id)
        projector = RecordProjector(self._engine.profile, connection, authority)
        refs = [evidence_ref(row) for row in evidence]
        if not visible_to(item, refs, authority=authority, projector=projector):
            raise FollowupError("not_found")
        return item, evidence

    # -- propose ---------------------------------------------------------------

    def propose(self, request: ProposeRequest, *, authority: EffectiveAuthority) -> FollowupItem:
        authority.require("followup-propose")
        digest = binding_digest(request_wire(request))
        with self._write() as connection:
            projector = RecordProjector(self._engine.profile, connection, authority)
            # Check readability first so an unreadable citation never reveals replay
            # state, duplicates, or capacity. Revision, position and hash are checked only
            # after replay misses, so a retry after the source moved returns its receipt.
            for ref in request.evidence:
                self._require_readable(projector, ref)
            replayed = self._replay(connection, request.operation_id, digest)
            if replayed is not None:
                return replayed
            tiers = [self._verify_evidence(connection, projector, ref) for ref in request.evidence]
            if any(tier is not tiers[0] for tier in tiers):
                raise FollowupError("tier_mismatch")
            origin = request.evidence[0]
            existing = connection.execute(
                "SELECT item_id FROM followup_evidence WHERE role='origin' AND revision_id=? "
                "AND quote_start=? AND quote_end=?",
                origin.position(),
            ).fetchone()
            if existing is not None:
                # A duplicate the session cannot see is reported as unreadable, not by id.
                self._visible_item(connection, existing["item_id"], authority=authority)
                raise FollowupError("duplicate_item", existing_item_id=existing["item_id"])
            count = connection.execute("SELECT count(*) FROM followup_items").fetchone()[0]
            if count >= MAX_ITEMS:
                raise FollowupError("capacity_reached")
            now = self._now()
            item_id = "followup_" + str(uuid4())
            # Evidence rows reference the item, so the item row is written first.
            evidence_ids = tuple("evidence_" + str(uuid4()) for _ref in request.evidence)
            due_evidence_id = (
                None
                if request.due_evidence_index is None
                else evidence_ids[request.due_evidence_index]
            )
            item = FollowupItem(
                item_id=item_id,
                kind=request.kind,
                project=request.project,
                summary=request.summary,
                owner=request.owner,
                owner_name=request.owner_name,
                owner_confirmed=False,
                due=request.due,
                due_evidence_id=due_evidence_id,
                state=FollowupState.SUGGESTED,
                snoozed_until=None,
                snooze_return_state=None,
                ticket_key=None,
                ticket_url=None,
                superseded_by=None,
                resolution=None,
                privacy_tier=tiers[0].value,
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._store_item(connection, item, insert=True)
            for ref, evidence_id in zip(request.evidence, evidence_ids, strict=True):
                self._insert_evidence(connection, item_id, ref, now, evidence_id)
            self._log(
                connection,
                item=item,
                operation_id=request.operation_id,
                digest=digest,
                principal_id=authority.principal_id,
                action="propose",
                prior_state=None,
                prior_version=0,
                evidence_ids=evidence_ids,
            )
            return item

    # -- add evidence ----------------------------------------------------------

    def add_evidence(
        self, request: AddEvidenceRequest, *, authority: EffectiveAuthority
    ) -> FollowupItem:
        authority.require("followup-propose")
        digest = binding_digest(request_wire(request))
        with self._write() as connection:
            stored, evidence = self._visible_item(connection, request.item_id, authority=authority)
            projector = RecordProjector(self._engine.profile, connection, authority)
            self._require_readable(projector, request.evidence)
            replayed = self._replay(connection, request.operation_id, digest)
            if replayed is not None:
                return replayed
            tier = self._verify_evidence(connection, projector, request.evidence)
            if tier.value != stored.privacy_tier:
                raise FollowupError("tier_mismatch")
            current = effective_item(stored, self._today())
            if current.state is FollowupState.SUPERSEDED:
                raise FollowupError("invalid_request")
            if len(evidence) >= MAX_EVIDENCE_PER_ITEM:
                raise FollowupError("capacity_reached")
            now = self._now()
            evidence_id = self._insert_evidence(
                connection, stored.item_id, request.evidence, now, "evidence_" + str(uuid4())
            )
            # Written from the effective item, so a lapsed snooze is settled by this write
            # and the log records the state the session saw, as decide does.
            item = replace(
                current,
                version=stored.version + 1,
                updated_at=now,
                new_evidence_since_decision=(
                    current.new_evidence_since_decision
                    or current.state in {FollowupState.DISMISSED, FollowupState.RESOLVED}
                ),
            )
            self._store_item(connection, item, insert=False)
            self._log(
                connection,
                item=item,
                operation_id=request.operation_id,
                digest=digest,
                principal_id=authority.principal_id,
                action="add_evidence",
                prior_state=current.state,
                prior_version=stored.version,
                evidence_ids=(evidence_id,),
            )
            return item

    # -- decide ----------------------------------------------------------------

    def decide(self, request: DecideRequest, *, authority: EffectiveAuthority) -> FollowupItem:
        authority.require("followup-decide")
        digest = binding_digest(request_wire(request))
        with self._write() as connection:
            stored, evidence = self._visible_item(connection, request.item_id, authority=authority)
            replayed = self._replay(connection, request.operation_id, digest)
            if replayed is not None:
                return replayed
            if request.expected_version != stored.version:
                raise FollowupError("version_conflict", current_version=stored.version)
            today = self._today()
            current = effective_item(stored, today)
            now = self._now()
            affected: tuple[str, ...] = ()
            item = self._transition(connection, request, current, evidence, today, authority)
            if request.action is DecisionAction.RESOLVE and request.resolution == "evidence":
                affected = tuple(
                    row["evidence_id"] for row in evidence if row["role"] == "resolution"
                )
            elif request.action is DecisionAction.CORRECT and item.due_evidence_id is not None:
                affected = (item.due_evidence_id,)
            item = replace(item, version=stored.version + 1, updated_at=now)
            self._store_item(connection, item, insert=False)
            self._log(
                connection,
                item=item,
                operation_id=request.operation_id,
                digest=digest,
                principal_id=authority.principal_id,
                action=request.action.value,
                prior_state=current.state,
                prior_version=stored.version,
                evidence_ids=affected,
            )
            return item

    def _transition(
        self,
        connection: sqlite3.Connection,
        request: DecideRequest,
        current: FollowupItem,
        evidence: list[sqlite3.Row],
        today: date,
        authority: EffectiveAuthority,
    ) -> FollowupItem:
        """The next item for one decision over the effective state; refusals write nothing."""
        action = request.action
        state = current.state
        if state is FollowupState.SUPERSEDED:
            raise FollowupError("invalid_request")
        cleared = replace(current, snoozed_until=None, snooze_return_state=None)
        if action is DecisionAction.CONFIRM:
            if state not in {FollowupState.SUGGESTED, FollowupState.SNOOZED}:
                raise FollowupError("invalid_request")
            return replace(cleared, state=FollowupState.CONFIRMED, owner_confirmed=True)
        if action is DecisionAction.DISMISS:
            if state not in OPEN_STATES:
                raise FollowupError("invalid_request")
            return replace(
                cleared, state=FollowupState.DISMISSED, new_evidence_since_decision=False
            )
        if action is DecisionAction.SNOOZE:
            assert request.snoozed_until is not None
            if state not in OPEN_STATES or request.snoozed_until <= today:
                raise FollowupError("invalid_request")
            return_state = current.snooze_return_state if state is FollowupState.SNOOZED else state
            return replace(
                current,
                state=FollowupState.SNOOZED,
                snoozed_until=request.snoozed_until,
                snooze_return_state=return_state,
            )
        if action is DecisionAction.CORRECT:
            if state not in OPEN_STATES:
                raise FollowupError("invalid_request")
            return self._apply_corrections(current, request, evidence)
        if action is DecisionAction.LINK:
            return replace(current, ticket_key=request.ticket_key, ticket_url=request.ticket_url)
        if action is DecisionAction.RESOLVE:
            if state not in OPEN_STATES:
                raise FollowupError("invalid_request")
            if request.resolution == "evidence" and not any(
                row["role"] == "resolution" for row in evidence
            ):
                raise FollowupError("invalid_request")
            return replace(
                cleared,
                state=FollowupState.RESOLVED,
                resolution=request.resolution,
                new_evidence_since_decision=False,
            )
        if action is DecisionAction.REOPEN:
            if state not in {FollowupState.DISMISSED, FollowupState.RESOLVED}:
                raise FollowupError("invalid_request")
            return replace(
                cleared,
                state=(
                    FollowupState.CONFIRMED if current.owner_confirmed else FollowupState.SUGGESTED
                ),
                resolution=None,
                new_evidence_since_decision=False,
            )
        if action is DecisionAction.SUPERSEDE:
            assert request.superseded_by is not None
            if state not in OPEN_STATES:
                raise FollowupError("invalid_request")
            target, _target_evidence = self._visible_item(
                connection, request.superseded_by, authority=authority
            )
            if (
                effective_item(target, today).state is FollowupState.SUPERSEDED
                or target.privacy_tier != current.privacy_tier
            ):
                raise FollowupError("invalid_request")
            return replace(cleared, state=FollowupState.SUPERSEDED, superseded_by=target.item_id)
        raise FollowupError("invalid_request")

    @staticmethod
    def _apply_corrections(
        current: FollowupItem, request: DecideRequest, evidence: list[sqlite3.Row]
    ) -> FollowupItem:
        corrections = request.corrections
        assert corrections is not None
        item = current
        if "summary" in corrections:
            item = replace(item, summary=str(corrections["summary"]))
        if "project" in corrections:
            project = corrections["project"]
            item = replace(item, project=None if project is None else str(project))
        if "owner" in corrections:
            owner = OwnerKind(str(corrections["owner"]))
            name = corrections.get("owner_name")
            item = replace(item, owner=owner, owner_name=None if name is None else str(name))
        if "due" in corrections:
            due = corrections["due"]
            if due is None:
                item = replace(item, due=None, due_evidence_id=None)
            else:
                stating = str(corrections["due_evidence_id"])
                if not any(row["evidence_id"] == stating for row in evidence):
                    raise FollowupError("invalid_request")
                item = replace(item, due=date.fromisoformat(str(due)), due_evidence_id=stating)
        return item

    def _store_item(
        self, connection: sqlite3.Connection, item: FollowupItem, *, insert: bool
    ) -> None:
        values = (
            item.item_id,
            item.kind.value,
            item.project,
            item.summary,
            item.owner.value,
            item.owner_name,
            int(item.owner_confirmed),
            None if item.due is None else item.due.isoformat(),
            item.due_evidence_id,
            item.state.value,
            None if item.snoozed_until is None else item.snoozed_until.isoformat(),
            None if item.snooze_return_state is None else item.snooze_return_state.value,
            item.ticket_key,
            item.ticket_url,
            item.superseded_by,
            item.resolution,
            item.privacy_tier,
            item.version,
            item.created_at,
            item.updated_at,
            int(item.evidence_retracted),
            int(item.new_evidence_since_decision),
        )
        if insert:
            connection.execute(
                f"INSERT INTO followup_items({_ITEM_COLUMNS}) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            return
        # Compare-and-swap: the caller already verified expected_version against the
        # row it read inside this transaction; the WHERE clause is the last guard.
        updated = connection.execute(
            "UPDATE followup_items SET kind=?,project=?,summary=?,owner=?,owner_name=?,"
            "owner_confirmed=?,due=?,due_evidence_id=?,state=?,snoozed_until=?,"
            "snooze_return_state=?,ticket_key=?,ticket_url=?,superseded_by=?,resolution=?,"
            "privacy_tier=?,version=?,created_at=?,updated_at=?,evidence_retracted=?,"
            "new_evidence_since_decision=? "
            "WHERE item_id=? AND version=?",
            (*values[1:], item.item_id, item.version - 1),
        ).rowcount
        if updated != 1:
            raise FollowupError("version_conflict", current_version=item.version - 1)

    # -- read ------------------------------------------------------------------

    def _owner_projection(
        self,
        connection: sqlite3.Connection,
        item: FollowupItem,
        evidence: list[sqlite3.Row],
        authority: EffectiveAuthority,
    ) -> tuple[FollowupItem, tuple[FollowupEvidenceView, ...]]:
        """Rebuild quotes; an unreadable revision marks the item retracted for the owner."""
        projector = RecordProjector(self._engine.profile, connection, authority)
        views = []
        retracted = False
        for row in evidence:
            ref = evidence_ref(row)
            quote: str | None
            try:
                record = projector.read(ref.capture_id, expected=ref.revision_id, history=True)
                quote = rebuild_quote(record.text, ref)
            except T03Error as error:
                if error.code not in {"not_found", "revision_changed"}:
                    raise
                quote = None
            retracted = retracted or quote is None
            views.append(
                FollowupEvidenceView(
                    evidence_id=row["evidence_id"],
                    role=EvidenceRole(row["role"]),
                    capture_id=row["capture_id"],
                    revision_id=row["revision_id"],
                    quote=quote,
                )
            )
        if retracted:
            item = replace(item, evidence_retracted=True, summary=None)
        return item, tuple(views)

    @staticmethod
    def _matches(item: FollowupItem, filter: ListFilter) -> bool:
        if filter.states:
            if item.state not in filter.states:
                return False
        elif not filter.include_closed and (
            item.state is FollowupState.SNOOZED
            or (item.state in CLOSED_STATES and not item.new_evidence_since_decision)
        ):
            # The default list holds open items: a snoozed item leaves it until its date and
            # a closed item returns only when new evidence arrived after the decision.
            return False
        if filter.kinds and item.kind not in filter.kinds:
            return False
        if filter.owner is not None and item.owner is not filter.owner:
            return False
        if filter.project is not None and item.project != filter.project:
            return False
        return filter.due_before is None or (item.due is not None and item.due < filter.due_before)

    def show(
        self, item_id: str, *, authority: EffectiveAuthority
    ) -> tuple[FollowupItem, tuple[FollowupEvidenceView, ...], tuple[FollowupDecisionRow, ...]]:
        authority.require("followup-read")
        with read_snapshot(self._engine) as connection:
            stored, evidence = self._visible_item(connection, item_id, authority=authority)
            item = effective_item(stored, self._today())
            item, views = self._owner_projection(connection, item, evidence, authority)
            history = tuple(
                decision_from_row(row)
                for row in connection.execute(
                    "SELECT * FROM followup_decisions WHERE item_id=? ORDER BY sequence",
                    (item_id,),
                )
            )
            return item, views, history

    def list(
        self, filter: ListFilter, *, authority: EffectiveAuthority
    ) -> tuple[tuple[FollowupItem, ...], str | None]:
        authority.require("followup-read")
        with read_snapshot(self._engine) as connection:
            today = self._today()
            after: tuple[str, str] | None = None
            if filter.cursor is not None:
                # A hidden anchor is refused exactly like an unknown one.
                try:
                    anchor, _anchor_evidence = self._visible_item(
                        connection, filter.cursor, authority=authority
                    )
                except FollowupError:
                    raise FollowupError("invalid_request") from None
                after = (anchor.created_at, anchor.item_id)
            projector = RecordProjector(self._engine.profile, connection, authority)
            selected: list[FollowupItem] = []
            more = False
            rows = connection.execute(
                f"SELECT {_ITEM_COLUMNS} FROM followup_items "
                "ORDER BY created_at COLLATE BINARY, item_id COLLATE BINARY"
            )
            for row in rows:
                if after is not None and (row["created_at"], row["item_id"]) <= after:
                    continue
                stored = item_from_row(row)
                evidence = self._evidence_rows(connection, stored.item_id)
                refs = [evidence_ref(row) for row in evidence]
                if not visible_to(stored, refs, authority=authority, projector=projector):
                    continue
                item = effective_item(stored, today)
                if authority.owner:
                    item, _views = self._owner_projection(connection, item, evidence, authority)
                if not self._matches(item, filter):
                    continue
                if len(selected) == filter.limit:
                    more = True
                    break
                selected.append(item)
            cursor = selected[-1].item_id if more and selected else None
            return tuple(selected), cursor


__all__ = [
    "FollowupTasks",
    "decision_from_row",
    "effective_item",
    "item_from_row",
]
