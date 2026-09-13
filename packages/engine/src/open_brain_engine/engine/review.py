"""Proposal, decision, publication, and review task operations."""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Sequence
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from .contracts import (
    CaptureFault,
    DecisionOutcome,
    DecisionRecord,
    ProposalDraft,
    ProposalRecord,
    _LocalEngineOperations,
)
from .normalization import (
    _MAX_TEXT,
    _dated_path,
    _decision_record,
    _delivery_id,
    _new_id,
    _optional_text,
    _portable_id,
    _privacy,
    _publication_record,
    _receipt,
    _role_claim,
    _timestamp,
    _trust,
)
from .portability_ports import portable_write_port

if TYPE_CHECKING:
    from .local import BrainEngine


class ReviewOperations(_LocalEngineOperations):
    def _propose(
        self, capture_id: str, drafts: Sequence[ProposalDraft], delivery_id: str
    ) -> tuple[ProposalRecord, ...]:
        _portable_id(capture_id, "capture")
        _delivery_id(delivery_id)
        if (
            isinstance(drafts, str)
            or not isinstance(drafts, Sequence)
            or not 1 <= len(drafts) <= 8
            or any(not isinstance(draft, ProposalDraft) for draft in drafts)
        ):
            raise ValueError("invalid proposal set")
        request_value = {
            "capture_id": capture_id,
            "drafts": [
                {
                    "markdown": draft.markdown,
                    "proposed_kind": draft.proposed_kind,
                    "supplied_reason": draft.supplied_reason,
                    "title": draft.title,
                }
                for draft in drafts
            ],
        }
        request_sha = sha256(portable_canonical_json_bytes(request_value)).hexdigest()
        conflict: tuple[str, str] | None = None
        created = False
        with self._store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM proposal_sets WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if existing is not None:
                if cast(str, existing["request_sha256"]) != request_sha:
                    conflict = (cast(str, existing["request_sha256"]), request_sha)
            else:
                capture = connection.execute(
                    "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
                ).fetchone()
                if capture is None:
                    raise ValueError("unknown capture")
                if (
                    any(draft.proposed_kind == "page_update" for draft in drafts)
                    and capture["space_id"] is None
                ):
                    raise ValueError("page proposal requires routed capture")
                now = _timestamp(self._clock())
                proposal_ids = tuple(_new_id("proposal") for _ in drafts)
                connection.execute(
                    """
                    INSERT INTO proposal_sets (
                        delivery_id, request_sha256, capture_id, recorded_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (delivery_id, request_sha, capture_id, now),
                )
                for proposal_id, draft in zip(proposal_ids, drafts, strict=True):
                    page_id = _new_id("page") if draft.proposed_kind == "page_update" else None
                    proposed_bytes = (
                        self._proposal_page_bytes(
                            capture,
                            page_id=page_id,
                            title=draft.title,
                            body=draft.markdown,
                            modified_at=now,
                        )
                        if page_id is not None
                        else portable_canonical_json_bytes(
                            {"kind": draft.proposed_kind, "text": draft.markdown}
                        )
                    )
                    canonical_path = (
                        self._canonical_path(cast(str, capture["space_id"]), page_id)
                        if page_id is not None
                        else None
                    )
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
                            capture_id,
                            draft.proposed_kind,
                            draft.title,
                            draft.markdown,
                            proposed_bytes,
                            draft.supplied_reason,
                            capture["space_id"],
                            _new_id("receipt"),
                            page_id,
                            canonical_path,
                        ),
                    )
                created = True
        if conflict is not None:
            self._quarantine(delivery_id, expected=conflict[0], actual=conflict[1])
            raise ValueError("conflicting delivery")
        if created:
            self._fault(CaptureFault.AFTER_PROPOSAL_RESERVATION)
        self._process_proposal_set(self._proposal_set_row(delivery_id))
        return self._list_proposals(capture_id=capture_id, status=None, set_delivery_id=delivery_id)

    def _proposal_page_bytes(
        self,
        capture: sqlite3.Row,
        *,
        page_id: str,
        title: str,
        body: str,
        modified_at: str,
    ) -> bytes:
        return render_markdown(
            fields={
                "actor_id": self.profile.owner_actor_id,
                "modified_at": modified_at,
                "page_id": page_id,
                "privacy": _privacy(),
                "provenance": [capture["capture_id"]],
                "role_claim": _role_claim(self.profile),
                "schema_version": 1,
                "space_id": capture["space_id"],
                "status": "active",
                "tenant_id": self.profile.tenant_id,
                "title": title,
                "trust": "reviewed",
            },
            body=body if body.endswith("\n") else body + "\n",
        ).encode("utf-8")

    def _proposal_set_row(self, delivery_id: str) -> sqlite3.Row:
        connection = self._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM proposal_sets WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("unknown proposal set")
        return cast(sqlite3.Row, row)

    def _proposal_rows(self, delivery_id: str) -> tuple[sqlite3.Row, ...]:
        connection = self._store.connect()
        try:
            return tuple(
                connection.execute(
                    "SELECT * FROM proposals WHERE set_delivery_id = ? ORDER BY proposal_id",
                    (delivery_id,),
                )
            )
        finally:
            connection.close()

    def _process_proposal_set(self, supplied_row: sqlite3.Row) -> None:
        row = self._proposal_set_row(cast(str, supplied_row["delivery_id"]))
        if cast(int, row["stage"]) >= 1:
            return
        proposals = self._proposal_rows(cast(str, row["delivery_id"]))
        siblings = tuple(cast(str, proposal["proposal_id"]) for proposal in proposals)
        capture = self._capture_row(cast(str, row["capture_id"]))
        for proposal in proposals:
            record = self._proposal_record(
                capture,
                proposal_id=cast(str, proposal["proposal_id"]),
                receipt_id=cast(str, proposal["receipt_id"]),
                proposed_bytes=cast(bytes, proposal["proposed_bytes"]),
                proposed_kind=cast(str, proposal["proposed_kind"]),
                sibling_ids=siblings,
                supplied_reason=cast(str | None, proposal["supplied_reason"]),
                recorded_at=cast(str, row["recorded_at"]),
            )
            portable_write_port(self).put_history(
                "proposal", portable_canonical_json_bytes(record)
            )
        self._fault(CaptureFault.AFTER_PROPOSAL_WRITE)
        with self._store.transaction() as connection:
            connection.execute(
                "UPDATE proposal_sets SET stage = 1 WHERE delivery_id = ?",
                (row["delivery_id"],),
            )

    def _proposal_record(
        self,
        capture: sqlite3.Row,
        *,
        proposal_id: str,
        receipt_id: str,
        proposed_bytes: bytes,
        proposed_kind: str,
        sibling_ids: tuple[str, ...],
        supplied_reason: str | None,
        recorded_at: str,
    ) -> dict[str, object]:
        excerpt = cast(str, capture["search_text"]).strip()[:512] or cast(
            str, capture["payload_family"]
        )
        receipt_payload = {
            "proposal_id": proposal_id,
            "proposed_content_sha256": sha256(proposed_bytes).hexdigest(),
        }
        return {
            "actor_id": self.profile.owner_actor_id,
            "capture_ids": [capture["capture_id"]],
            "evidence": [
                {
                    "capture_id": capture["capture_id"],
                    "excerpt": excerpt,
                    "sha256": sha256(excerpt.encode("utf-8")).hexdigest(),
                }
            ],
            "expected_receipt": _receipt(
                "proposal_created", receipt_id, proposal_id, recorded_at, receipt_payload
            ),
            "privacy": _privacy(),
            "proposal_id": proposal_id,
            "proposed_content": {
                "bytes_base64": base64.b64encode(proposed_bytes).decode("ascii"),
                "media_type": (
                    "text/markdown" if proposed_kind == "page_update" else "application/json"
                ),
                "sha256": sha256(proposed_bytes).hexdigest(),
            },
            "proposed_kind": proposed_kind,
            "recorded_at": recorded_at,
            "role_claim": _role_claim(self.profile),
            "schema_version": 1,
            "sibling_context": {"proposal_ids": list(sibling_ids)},
            "space_id": capture["space_id"],
            "status": "pending",
            "supplied_reason": supplied_reason,
            "tenant_id": self.profile.tenant_id,
            "trust": _trust(
                self.profile,
                recorded_at,
                "third_party" if capture["source_origin"] == "third_party" else "owner",
                "proposal retains capture trust",
            ),
        }

    def _list_proposals(
        self,
        *,
        capture_id: str | None,
        status: str | None,
        set_delivery_id: str | None = None,
    ) -> tuple[ProposalRecord, ...]:
        if capture_id is not None:
            _portable_id(capture_id, "capture")
        if status is not None and status not in {
            "pending",
            DecisionOutcome.APPROVED.value,
            DecisionOutcome.REJECTED.value,
            DecisionOutcome.EDITED.value,
        }:
            raise ValueError("invalid proposal status")
        clauses: list[str] = []
        parameters: list[object] = []
        if capture_id is not None:
            clauses.append("capture_id = ?")
            parameters.append(capture_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if set_delivery_id is not None:
            clauses.append("set_delivery_id = ?")
            parameters.append(set_delivery_id)
        sql = "SELECT * FROM proposals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY proposal_id"
        connection = self._store.connect()
        try:
            rows = tuple(connection.execute(sql, parameters))
            sibling_rows = tuple(
                connection.execute(
                    "SELECT set_delivery_id, proposal_id FROM proposals ORDER BY proposal_id"
                )
            )
        finally:
            connection.close()
        siblings: dict[str, list[str]] = {}
        for sibling in sibling_rows:
            siblings.setdefault(cast(str, sibling["set_delivery_id"]), []).append(
                cast(str, sibling["proposal_id"])
            )
        return tuple(
            ProposalRecord(
                proposal_id=cast(str, row["proposal_id"]),
                capture_id=cast(str, row["capture_id"]),
                proposed_kind=cast(str, row["proposed_kind"]),
                status=cast(str, row["status"]),
                space_id=cast(str | None, row["space_id"]),
                sibling_proposal_ids=tuple(siblings[cast(str, row["set_delivery_id"])]),
                terminal_decision_id=cast(str | None, row["terminal_decision_id"]),
            )
            for row in rows
        )

    def _proposal_row(self, proposal_id: str) -> sqlite3.Row:
        _portable_id(proposal_id, "proposal")
        connection = self._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("unknown proposal")
        return cast(sqlite3.Row, row)

    def _decide(
        self,
        proposal_id: str,
        outcome: DecisionOutcome,
        *,
        delivery_id: str,
        edited_markdown: str | None,
    ) -> DecisionRecord:
        _portable_id(proposal_id, "proposal")
        _delivery_id(delivery_id)
        outcome = DecisionOutcome(outcome)
        edited_markdown = _optional_text(
            edited_markdown, field="edited markdown", maximum=_MAX_TEXT
        )
        if (outcome is DecisionOutcome.EDITED) != (edited_markdown is not None):
            raise ValueError("edited outcome requires edited content")
        request_sha = sha256(
            portable_canonical_json_bytes(
                {
                    "edited_markdown": edited_markdown,
                    "outcome": outcome.value,
                    "proposal_id": proposal_id,
                }
            )
        ).hexdigest()
        conflict: tuple[str, str] | None = None
        created = False
        duplicate = False
        with self._store.transaction() as connection:
            delivery = connection.execute(
                "SELECT * FROM decisions WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise ValueError("unknown proposal")
            existing = connection.execute(
                "SELECT * FROM decisions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if delivery is not None and cast(str, delivery["request_sha256"]) != request_sha:
                conflict = (cast(str, delivery["request_sha256"]), request_sha)
            elif existing is not None:
                existing_outcome = DecisionOutcome(cast(str, existing["outcome"]))
                if existing_outcome is not outcome:
                    raise ValueError("proposal already has a terminal decision")
                if outcome is DecisionOutcome.EDITED:
                    current = cast(bytes, existing["effective_bytes"])
                    capture = connection.execute(
                        "SELECT * FROM captures WHERE capture_id = ?", (proposal["capture_id"],)
                    ).fetchone()
                    assert capture is not None
                    expected = self._proposal_page_bytes(
                        capture,
                        page_id=cast(str, proposal["page_id"]),
                        title=cast(str, proposal["title"]),
                        body=cast(str, edited_markdown),
                        modified_at=cast(str, existing["recorded_at"]),
                    )
                    if current != expected:
                        raise ValueError("proposal already has a terminal decision")
                duplicate = True
                decision_id = cast(str, existing["decision_id"])
            else:
                if (
                    cast(str, proposal["proposed_kind"]) == "page_update"
                    and proposal["space_id"] is None
                ):
                    raise ValueError("page proposal requires routed capture")
                now = _timestamp(self._clock())
                capture = connection.execute(
                    "SELECT * FROM captures WHERE capture_id = ?", (proposal["capture_id"],)
                ).fetchone()
                assert capture is not None
                if outcome is DecisionOutcome.REJECTED:
                    effective_bytes = None
                elif outcome is DecisionOutcome.EDITED:
                    effective_bytes = self._proposal_page_bytes(
                        capture,
                        page_id=cast(str, proposal["page_id"]),
                        title=cast(str, proposal["title"]),
                        body=cast(str, edited_markdown),
                        modified_at=now,
                    )
                else:
                    effective_bytes = cast(bytes, proposal["proposed_bytes"])
                decision_id = _new_id("decision")
                publishable = (
                    cast(str, proposal["proposed_kind"]) == "page_update"
                    and effective_bytes is not None
                )
                connection.execute(
                    """
                    INSERT INTO decisions (
                        delivery_id, request_sha256, decision_id, decision_receipt_id,
                        proposal_id, outcome, effective_bytes, recorded_at, page_id,
                        publication_id, canonical_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        delivery_id,
                        request_sha,
                        decision_id,
                        _new_id("receipt"),
                        proposal_id,
                        outcome.value,
                        effective_bytes,
                        now,
                        proposal["page_id"] if publishable else None,
                        _new_id("publication") if publishable else None,
                        proposal["canonical_path"] if publishable else None,
                    ),
                )
                connection.execute(
                    "UPDATE proposals SET status = ?, terminal_decision_id = ? "
                    "WHERE proposal_id = ?",
                    (outcome.value, decision_id, proposal_id),
                )
                created = True
        if conflict is not None:
            self._quarantine(delivery_id, expected=conflict[0], actual=conflict[1])
            raise ValueError("conflicting delivery")
        if created:
            self._fault(CaptureFault.AFTER_DECISION_RESERVATION)
        row = self._decision_row(decision_id)
        self._process_decision(row)
        return self._decision_public(self._decision_row(decision_id), duplicate=duplicate)

    def _decision_row(self, decision_id: str) -> sqlite3.Row:
        connection = self._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("unknown decision")
        return cast(sqlite3.Row, row)

    def _process_decision(self, supplied_row: sqlite3.Row) -> None:
        row = self._decision_row(cast(str, supplied_row["decision_id"]))
        proposal = self._proposal_row(cast(str, row["proposal_id"]))
        set_row = self._proposal_set_row(cast(str, proposal["set_delivery_id"]))
        siblings = tuple(
            cast(str, item["proposal_id"])
            for item in self._proposal_rows(cast(str, proposal["set_delivery_id"]))
        )
        capture = self._capture_row(cast(str, proposal["capture_id"]))
        proposal_record = self._proposal_record(
            capture,
            proposal_id=cast(str, proposal["proposal_id"]),
            receipt_id=cast(str, proposal["receipt_id"]),
            proposed_bytes=cast(bytes, proposal["proposed_bytes"]),
            proposed_kind=cast(str, proposal["proposed_kind"]),
            sibling_ids=siblings,
            supplied_reason=cast(str | None, proposal["supplied_reason"]),
            recorded_at=cast(str, set_row["recorded_at"]),
        )
        if cast(int, row["stage"]) < 1:
            edited_bytes = (
                cast(bytes, row["effective_bytes"])
                if cast(str, row["outcome"]) == DecisionOutcome.EDITED.value
                else None
            )
            record = _decision_record(
                profile=self.profile,
                proposal=proposal_record,
                decision_id=cast(str, row["decision_id"]),
                outcome=DecisionOutcome(cast(str, row["outcome"])),
                edited_bytes=edited_bytes,
                recorded_at=cast(str, row["recorded_at"]),
            )
            portable_write_port(self).put_history(
                "decision", portable_canonical_json_bytes(record)
            )
            self._fault(CaptureFault.AFTER_DECISION_WRITE)
            with self._store.transaction() as connection:
                connection.execute(
                    "UPDATE decisions SET stage = 1 WHERE decision_id = ?",
                    (row["decision_id"],),
                )
            row = self._decision_row(cast(str, row["decision_id"]))
        if cast(int, row["stage"]) < 2:
            effective = cast(bytes | None, row["effective_bytes"])
            canonical_path = cast(str | None, row["canonical_path"])
            publication_path: str | None = None
            if effective is not None and canonical_path is not None:
                portable_write_port(self).put_page(canonical_path, effective)
                self._fault(CaptureFault.AFTER_REVIEW_PAGE_WRITE)
                publication = _publication_record(
                    profile=self.profile,
                    decision_id=cast(str, row["decision_id"]),
                    page_id=cast(str, row["page_id"]),
                    publication_id=cast(str, row["publication_id"]),
                    published_path=canonical_path,
                    published_bytes=effective,
                    recorded_at=cast(str, row["recorded_at"]),
                )
                publication_path = _dated_path(
                    "history/publications",
                    cast(str, row["recorded_at"]),
                    cast(str, row["publication_id"]),
                )
                portable_write_port(self).put_history(
                    "publication", portable_canonical_json_bytes(publication)
                )
                self._fault(CaptureFault.AFTER_REVIEW_PUBLICATION_WRITE)
            with self._store.transaction() as connection:
                connection.execute(
                    "UPDATE decisions SET publication_path = ?, stage = 2 WHERE decision_id = ?",
                    (publication_path, row["decision_id"]),
                )
            row = self._decision_row(cast(str, row["decision_id"]))
        if cast(int, row["stage"]) < 3:
            effective = cast(bytes | None, row["effective_bytes"])
            canonical_path = cast(str | None, row["canonical_path"])
            with self._store.transaction() as connection:
                if effective is not None and canonical_path is not None:
                    parsed = parse_markdown(effective)
                    self._upsert_canonical_search(
                        connection,
                        result_id=cast(str, row["page_id"]),
                        capture_id=cast(str, proposal["capture_id"]),
                        payload_family=cast(str, capture["payload_family"]),
                        space_id=cast(str, proposal["space_id"]),
                        title=cast(str, parsed.fields["title"]),
                        body=parsed.body,
                        canonical_path=canonical_path,
                        updated_at=cast(str, row["recorded_at"]),
                    )
                connection.execute(
                    "UPDATE decisions SET stage = 3 WHERE decision_id = ?",
                    (row["decision_id"],),
                )

    def _decision_public(self, row: sqlite3.Row, *, duplicate: bool) -> DecisionRecord:
        return DecisionRecord(
            decision_id=cast(str, row["decision_id"]),
            proposal_id=cast(str, row["proposal_id"]),
            outcome=DecisionOutcome(cast(str, row["outcome"])),
            page_id=cast(str | None, row["page_id"]),
            publication_id=cast(str | None, row["publication_id"]),
            duplicate=duplicate,
        )


class ReviewTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def propose(
        self,
        capture_id: str,
        drafts: Sequence[ProposalDraft],
        *,
        delivery_id: str,
    ) -> tuple[ProposalRecord, ...]:
        with self._engine._writer_lease.acquire_shared_writer():
            return self._engine._propose(capture_id, drafts, delivery_id)

    def list(
        self, *, capture_id: str | None = None, status: str | None = None
    ) -> tuple[ProposalRecord, ...]:
        return self._engine._list_proposals(capture_id=capture_id, status=status)

    def decide(
        self,
        proposal_id: str,
        outcome: DecisionOutcome,
        *,
        delivery_id: str,
        edited_markdown: str | None = None,
    ) -> DecisionRecord:
        with self._engine._writer_lease.acquire_shared_writer():
            return self._engine._decide(
                proposal_id,
                outcome,
                delivery_id=delivery_id,
                edited_markdown=edited_markdown,
            )
