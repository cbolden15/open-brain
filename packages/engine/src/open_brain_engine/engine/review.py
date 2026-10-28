"""Proposal, decision, publication, and review task operations."""

from __future__ import annotations

import base64
import difflib
import json
import sqlite3
from collections.abc import Sequence
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.review_binding import review_binding_base_version
from open_brain_engine.portable.v4 import canonical_revision_id
from open_brain_engine.storage.filesystem import StorageError, read_confined
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from .contracts import (
    MAX_PATCH_REPLACEMENT_BYTES,
    CaptureFault,
    DecisionOutcome,
    DecisionRecord,
    PatchDraft,
    PatchOperation,
    ProposalDraft,
    ProposalRecord,
    ReviewBindingConflict,
    ReviewEvidence,
    ReviewProposal,
    _LocalEngineOperations,
    project_public_result_text,
    projection_markers_added,
)
from .local_schema import PHASE1_STATE_SCHEMA_VERSION
from .mcp_eligibility import install
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
from .records import RecordProjector
from .review_bound import (
    MAX_REVIEW_MARKDOWN_BYTES,
    _read_page,
    _target_state,
    bound_edited_bytes,
    duplicate_page_id,
    load_bound_context,
    patch_draft_from_binding,
    propose_bound,
    target_head_advanced,
    validate_current_binding,
)
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine


def _bounded_page_diff(current: str, proposed: str) -> str | None:
    """Return a unified diff of projected bodies, or None past the inspection limit."""
    diff = "".join(
        difflib.unified_diff(
            (current + "\n").splitlines(keepends=True),
            (proposed + "\n").splitlines(keepends=True),
            fromfile="current",
            tofile="proposed",
        )
    )
    return diff if len(diff.encode("utf-8")) <= MAX_REVIEW_MARKDOWN_BYTES else None


class ReviewOperations(_LocalEngineOperations):
    def _propose_append(
        self,
        capture_id: str | Sequence[str],
        *,
        target_page_id: str,
        append_markdown: str,
        delivery_id: str,
    ) -> tuple[ProposalRecord, ...]:
        """Create a revision-bound append proposal without exposing page storage."""
        selected = (capture_id,) if isinstance(capture_id, str) else tuple(capture_id)
        if (
            not selected
            or any(not isinstance(value, str) for value in selected)
            or not isinstance(append_markdown, str)
            or not append_markdown.strip()
            or len(append_markdown.encode("utf-8")) > MAX_PATCH_REPLACEMENT_BYTES
        ):
            raise ValueError("invalid append proposal")
        _portable_id(target_page_id, "page")
        _delivery_id(delivery_id)
        append_request = portable_canonical_json_bytes(
            {
                "capture_ids": list(selected),
                "target_page_id": target_page_id,
                "append_markdown": append_markdown,
            }
        )
        append_digest = sha256(append_request).hexdigest()
        proposal_delivery = "append-" + sha256(delivery_id.encode()).hexdigest()
        with self._store.transaction() as connection:
            reserved = (
                connection.execute(
                    "SELECT * FROM development_append_operations WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if connection.execute("PRAGMA user_version").fetchone()[0] >= 16
                else None
            )
            if reserved is not None:
                if reserved["request_sha256"] != append_digest:
                    raise ValueError("conflicting append delivery")
                if reserved["proposal_id"] is not None:
                    existing_proposal = self._proposal_row(reserved["proposal_id"])
                    return self._list_proposals(
                        capture_id=None,
                        status=None,
                        set_delivery_id=existing_proposal["set_delivery_id"],
                    )
                frozen = json.loads(reserved["draft_json"])
                draft = PatchDraft(
                    target_page_id=frozen["target_page_id"],
                    expected_page_sha256=frozen["expected_page_sha256"],
                    operations=tuple(PatchOperation(**item) for item in frozen["operations"]),
                )
            else:
                draft = None
        if draft is not None:
            records = self._propose(
                selected, (draft,), proposal_delivery, target_page_id=target_page_id
            )
            with self._store.transaction() as connection:
                connection.execute(
                    "UPDATE development_append_operations SET proposal_id=? WHERE delivery_id=?",
                    (records[0].proposal_id, delivery_id),
                )
            return records
        with self._store.transaction() as connection:
            target = _target_state(self, connection, target_page_id)
            if target is None:
                raise ValueError("unknown target page")
            target_space_id = cast(str, target["space_id"])
        for selected_capture_id in selected:
            route_delivery = (
                "review-route-"
                + sha256((delivery_id + "\x1f" + selected_capture_id).encode("utf-8")).hexdigest()
            )
            self._route_capture(selected_capture_id, target_space_id, route_delivery)
        with self._store.transaction() as connection:
            target = _target_state(self, connection, target_page_id)
            if target is None:
                raise ValueError("unknown target page")
            current = _read_page(self, cast(str, target["canonical_path"]))
        body = parse_markdown(current).body
        draft = PatchDraft(
            target_page_id=target_page_id,
            expected_page_sha256=cast(str, target["published_sha256"]),
            operations=(
                PatchOperation(
                    start_byte=len(body.encode("utf-8")),
                    end_byte=len(body.encode("utf-8")),
                    replacement="\n\n" + append_markdown.strip() + "\n",
                ),
            ),
        )
        with self._store.transaction() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] >= 16:
                connection.execute(
                    "INSERT OR IGNORE INTO development_append_operations VALUES(?,?,?,?,NULL)",
                    (
                        delivery_id,
                        append_digest,
                        append_request.decode(),
                        portable_canonical_json_bytes(draft.to_dict()).decode(),
                    ),
                )
                reserved = connection.execute(
                    "SELECT * FROM development_append_operations WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                assert reserved is not None
                if reserved["request_sha256"] != append_digest:
                    raise ValueError("conflicting append delivery")
                frozen = json.loads(reserved["draft_json"])
                draft = PatchDraft(
                    target_page_id=frozen["target_page_id"],
                    expected_page_sha256=frozen["expected_page_sha256"],
                    operations=tuple(PatchOperation(**item) for item in frozen["operations"]),
                )
            else:
                proposal_delivery = (
                    "append-"
                    + sha256(
                        (delivery_id + "\x1f" + cast(str, target["published_sha256"])).encode()
                    ).hexdigest()
                )
        records = self._propose(
            selected,
            (draft,),
            proposal_delivery,
            target_page_id=target_page_id,
        )
        with self._store.transaction() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] >= 16:
                connection.execute(
                    "UPDATE development_append_operations SET proposal_id=? WHERE delivery_id=?",
                    (records[0].proposal_id, delivery_id),
                )
        return records

    def _propose(
        self,
        capture_id: str | Sequence[str],
        drafts: Sequence[ProposalDraft | PatchDraft],
        delivery_id: str,
        *,
        target_page_id: str | None = None,
    ) -> tuple[ProposalRecord, ...]:
        development = False
        if isinstance(capture_id, str):
            with self._store.transaction() as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] >= 16:
                    development = connection.execute(
                        "SELECT 1 FROM development_evidence WHERE derived_capture_id=?",
                        (capture_id,),
                    ).fetchone() is not None
        if (
            development
            or not isinstance(capture_id, str)
            or target_page_id is not None
            or any(isinstance(draft, PatchDraft) for draft in drafts)
        ):
            return cast(
                tuple[ProposalRecord, ...],
                propose_bound(
                    self,
                    (capture_id,) if isinstance(capture_id, str) else capture_id,
                    drafts,
                    delivery_id=delivery_id,
                    target_page_id=target_page_id,
                ),
            )
        return self._propose_legacy(capture_id, cast(Sequence[ProposalDraft], drafts), delivery_id)

    def _propose_legacy(
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
        return self._list_proposals(
            capture_id=capture_id,
            status=None,
            space_id=None,
            limit=None,
            offset=0,
            set_delivery_id=delivery_id,
        )

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
            connection = self._store.connect()
            try:
                context = load_bound_context(connection, cast(str, proposal["proposal_id"]))
            finally:
                connection.close()
            if context is None:
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
            else:
                record, binding = context
            portable_write_port(self).put_history("proposal", portable_canonical_json_bytes(record))
            if context is not None:
                portable_write_port(self).put_history(
                    "review_binding", portable_canonical_json_bytes(binding)
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

    def _legacy_proposal_snapshot(
        self, proposal: sqlite3.Row, *, sibling_ids: tuple[str, ...]
    ) -> dict[str, object]:
        set_row = self._proposal_set_row(cast(str, proposal["set_delivery_id"]))
        relative = _dated_path(
            "history/proposals",
            cast(str, set_row["recorded_at"]),
            cast(str, proposal["proposal_id"]),
        )
        try:
            payload = read_confined(
                root=self.profile.root,
                relative=relative,
                expected_root_identity=self.profile.root_identity,
                maximum_bytes=2 * 1024 * 1024,
            )
        except (StorageError, OSError) as error:
            raise ValueError("invalid frozen review proposal") from error
        if payload is None:
            value = self._proposal_record(
                self._capture_row(cast(str, proposal["capture_id"])),
                proposal_id=cast(str, proposal["proposal_id"]),
                receipt_id=cast(str, proposal["receipt_id"]),
                proposed_bytes=cast(bytes, proposal["proposed_bytes"]),
                proposed_kind=cast(str, proposal["proposed_kind"]),
                sibling_ids=sibling_ids,
                supplied_reason=cast(str | None, proposal["supplied_reason"]),
                recorded_at=cast(str, set_row["recorded_at"]),
            )
        else:
            value = json.loads(payload)
        if not isinstance(value, dict) or value.get("proposal_id") != proposal["proposal_id"]:
            raise ValueError("invalid frozen review proposal")
        return value

    def _list_proposals(
        self,
        *,
        capture_id: str | None,
        status: str | None,
        space_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        set_delivery_id: str | None = None,
        authority: EffectiveAuthority | None = None,
        facet_query_id: str | None = None,
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
        if space_id is not None:
            _portable_id(space_id, "space")
        if facet_query_id is not None:
            from .facet_validation import validate

            validate("facet.query.get.request", {"dto_version": 1, "query_id": facet_query_id})
        if (
            (limit is not None and (type(limit) is not int or not 1 <= limit <= 100))
            or type(offset) is not int
            or not 0 <= offset <= 1_000_000
        ):
            raise ValueError("invalid page bounds")
        clauses: list[str] = [
            "mcp_capture_visible(proposals.capture_id)=1 AND NOT EXISTS ("
            "SELECT 1 FROM review_sources protected "
            "WHERE protected.proposal_id=proposals.proposal_id "
            "AND mcp_capture_visible(protected.capture_id)=0)"
        ]
        parameters: list[object] = []
        if capture_id is not None:
            clauses.append(
                "(capture_id = ? OR EXISTS (SELECT 1 FROM review_sources rs "
                "WHERE rs.proposal_id = proposals.proposal_id AND rs.capture_id = ?))"
            )
            parameters.extend((capture_id, capture_id))
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if set_delivery_id is not None:
            clauses.append("set_delivery_id = ?")
            parameters.append(set_delivery_id)
        if space_id is not None:
            clauses.append("space_id = ?")
            parameters.append(space_id)
        if facet_query_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM review_contexts rc "
                "WHERE rc.proposal_id=proposals.proposal_id "
                "AND COALESCE(json_extract(rc.binding_json,'$.base_schema_version'), "
                "json_extract(rc.binding_json,'$.schema_version'))=5 "
                "AND json_extract(CAST(rc.binding_json AS TEXT),'$.query_id')=?)"
            )
            parameters.append(facet_query_id)
        sql = "SELECT * FROM proposals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid"
        if limit is not None and authority is None:
            sql += " LIMIT ? OFFSET ?"
            parameters.extend((limit, offset))
        elif offset and authority is None:
            sql += " LIMIT -1 OFFSET ?"
            parameters.append(offset)
        connection = self._store.connect()
        try:
            install(connection, authority)
            if not connection.in_transaction:
                connection.execute("BEGIN")
            rows = tuple(connection.execute(sql, parameters))
            sibling_rows = tuple(
                connection.execute(
                    "SELECT set_delivery_id, proposal_id FROM proposals ORDER BY proposal_id"
                )
            )
            contexts = {
                cast(str, row["proposal_id"]): load_bound_context(
                    connection, cast(str, row["proposal_id"])
                )
                for row in rows
            }
            if authority is not None:
                from .facet_contracts import FacetError
                from .facet_publication import authorize_facet_evidence
                from .t03_contracts import T03Error

                visible = []
                for row in rows:
                    context = contexts[cast(str, row["proposal_id"])]
                    if context is not None and review_binding_base_version(context[1]) == 5:
                        try:
                            authorize_facet_evidence(self, connection, context[1], authority)
                        except (FacetError, T03Error) as error:
                            if error.code == "not_found":
                                continue
                            raise
                    visible.append(row)
                rows = tuple(
                    visible[offset:] if limit is None else visible[offset : offset + limit]
                )
            references_by_proposal = {
                cast(str, row["proposal_id"]): tuple(
                    cast(str, capture["source_reference"])
                    for capture in connection.execute(
                        "SELECT source_reference FROM captures WHERE capture_id = ? "
                        "OR capture_id IN (SELECT capture_id FROM review_sources "
                        "WHERE proposal_id = ?)",
                        (row["capture_id"], row["proposal_id"]),
                    )
                )
                for row in rows
            }
            stale_ids: set[str] = set()
            for row in rows:
                bound = contexts[cast(str, row["proposal_id"])]
                if (
                    bound is not None
                    and cast(str, row["status"]) == "pending"
                    and target_head_advanced(connection, bound[1])
                ):
                    stale_ids.add(cast(str, row["proposal_id"]))
        finally:
            connection.close()
        siblings: dict[str, list[str]] = {}
        for sibling in sibling_rows:
            siblings.setdefault(cast(str, sibling["set_delivery_id"]), []).append(
                cast(str, sibling["proposal_id"])
            )
        result: list[ProposalRecord] = []
        for row in rows:
            proposal_id = cast(str, row["proposal_id"])
            context = contexts[proposal_id]
            capture_ids: tuple[str, ...]
            selected_capture_ids: tuple[str, ...]
            if context is None:
                proposal_value = self._legacy_proposal_snapshot(
                    row,
                    sibling_ids=tuple(siblings[cast(str, row["set_delivery_id"])]),
                )
                capture_ids = (cast(str, row["capture_id"]),)
                selected_capture_ids = capture_ids
                operation = "create"
                target_page_id = None
                draft_type = "full_page"
                review_digest = sha256(portable_canonical_json_bytes(proposal_value)).hexdigest()
            else:
                _, binding = context
                capture_ids = tuple(cast(list[str], binding["provenance"]))
                selected_capture_ids = tuple(cast(list[str], binding["selected_capture_ids"]))
                operation = cast(str, binding["operation"])
                target_page_id = cast(str, binding["page_id"]) if operation == "update" else None
                review_digest = cast(str, binding["review_digest"])
                draft_type = "patch" if binding.get("patch") is not None else "full_page"
            result.append(
                ProposalRecord(
                    proposal_id=cast(str, row["proposal_id"]),
                    capture_id=cast(str, row["capture_id"]),
                    proposed_kind=cast(str, row["proposed_kind"]),
                    status=cast(str, row["status"]),
                    space_id=cast(str | None, row["space_id"]),
                    sibling_proposal_ids=tuple(siblings[cast(str, row["set_delivery_id"])]),
                    terminal_decision_id=cast(str | None, row["terminal_decision_id"]),
                    title=project_public_result_text(
                        cast(str, row["title"]),
                        protected_literals=references_by_proposal[proposal_id],
                    ),
                    capture_ids=capture_ids,
                    selected_capture_ids=selected_capture_ids,
                    page_id=cast(str | None, row["page_id"]),
                    target_page_id=target_page_id,
                    operation=operation,
                    review_digest=review_digest,
                    draft_type=draft_type,
                    stale=proposal_id in stale_ids,
                )
            )
        return tuple(result)

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

    def _show_proposal(
        self, proposal_id: str, *, authority: EffectiveAuthority | None = None
    ) -> ReviewProposal:
        _portable_id(proposal_id, "proposal")
        capture_ids: tuple[str, ...]
        selected_capture_ids: tuple[str, ...]
        connection = self._store.connect()
        try:
            install(connection, authority)
            if not connection.in_transaction:
                connection.execute("BEGIN")
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=? "
                "AND mcp_capture_visible(capture_id)=1 AND NOT EXISTS ("
                "SELECT 1 FROM review_sources protected "
                "WHERE protected.proposal_id=proposals.proposal_id "
                "AND mcp_capture_visible(protected.capture_id)=0)", (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise ValueError("unknown proposal")
            context = load_bound_context(connection, proposal_id)
            if context is None:
                siblings = tuple(
                    cast(str, item["proposal_id"])
                    for item in connection.execute(
                        "SELECT proposal_id FROM proposals WHERE set_delivery_id = ? "
                        "ORDER BY proposal_id",
                        (proposal["set_delivery_id"],),
                    )
                )
                proposal_value = self._legacy_proposal_snapshot(proposal, sibling_ids=siblings)
                binding: dict[str, object] | None = None
                capture_ids = (cast(str, proposal["capture_id"]),)
                selected_capture_ids = capture_ids
                review_digest = sha256(portable_canonical_json_bytes(proposal_value)).hexdigest()
            else:
                proposal_value, binding = context
                if review_binding_base_version(binding) == 5:
                    from .facet_publication import authorize_facet_evidence, owner_review_authority

                    authorize_facet_evidence(
                        self,
                        connection,
                        binding,
                        owner_review_authority(self) if authority is None else authority,
                    )
                capture_ids = tuple(cast(list[str], binding["provenance"]))
                selected_capture_ids = tuple(cast(list[str], binding["selected_capture_ids"]))
                review_digest = cast(str, binding["review_digest"])
            sources = {
                capture_id: item
                for capture_id in capture_ids
                for item in connection.execute(
                    "SELECT source_reference, search_text, payload_family FROM captures "
                    "WHERE capture_id = ?",
                    (capture_id,),
                )
            }
            references = tuple(cast(str, item["source_reference"]) for item in sources.values())
            stale = (
                binding is not None
                and cast(str, proposal["status"]) == "pending"
                and target_head_advanced(connection, binding)
            )
            live_body: str | None = None
            if (
                binding is not None
                and binding["operation"] == "update"
                and binding.get("patch") is None
            ):
                try:
                    from .facet_publication import owner_review_authority

                    # Ordinary current reads admit only the current schema.
                    if (
                        connection.execute("PRAGMA user_version").fetchone()[0]
                        != PHASE1_STATE_SCHEMA_VERSION
                    ):
                        raise T03Error("incompatible_runtime")
                    target = _target_state(self, connection, cast(str, binding["page_id"]))
                    assert target is not None
                    # A newer target may depend on sources outside the frozen proposal.
                    RecordProjector(
                        self.profile, connection,
                        owner_review_authority(self) if authority is None else authority,
                    ).canonical(
                        cast(str, binding["page_id"]),
                        expected=canonical_revision_id(cast(str, target["publication_id"])),
                    )
                    live_body = parse_markdown(
                        _read_page(self, cast(str, target["canonical_path"]))
                    ).body.removesuffix("\n")
                    live_references = tuple(
                        cast(str, item["source_reference"])
                        for capture_id in cast(tuple[str, ...], target["provenance"])
                        for item in connection.execute(
                            "SELECT source_reference FROM captures WHERE capture_id = ?",
                            (capture_id,),
                        )
                    )
                    references = tuple(dict.fromkeys((*references, *live_references)))
                except ValueError:
                    live_body = None
        finally:
            connection.close()
        proposed_bytes = cast(bytes, proposal["proposed_bytes"])
        if len(proposed_bytes) > MAX_REVIEW_MARKDOWN_BYTES + 16_384:
            raise ValueError("review proposal exceeds inspection limit")
        if cast(str, proposal["proposed_kind"]) == "page_update":
            parsed = parse_markdown(proposed_bytes)
            proposed_body = parsed.body
            raw_markdown = parsed.body.removesuffix("\n")
            raw_title = cast(str, parsed.fields["title"])
        else:
            proposed_body = cast(str, proposal["body"])
            raw_markdown = cast(str, proposal["body"])
            raw_title = cast(str, proposal["title"])
        markdown = project_public_result_text(raw_markdown, protected_literals=references)
        title = project_public_result_text(raw_title, protected_literals=references)
        if len(markdown.encode("utf-8")) > MAX_REVIEW_MARKDOWN_BYTES:
            raise ValueError("review proposal exceeds inspection limit")
        evidence_items: list[ReviewEvidence] = []
        evidence_projected = False
        for item in cast(list[dict[str, object]], proposal_value["evidence"]):
            raw_excerpt = cast(str, item["excerpt"])
            source = sources[cast(str, item["capture_id"])]
            candidate = cast(str, source["search_text"]).strip() or cast(
                str, source["payload_family"]
            )
            projected_candidate = project_public_result_text(
                candidate, protected_literals=references
            )[:512]
            # Engine snapshots use this exact candidate. Recognize both legacy raw
            # excerpts and new projected excerpts without rewriting immutable history.
            if raw_excerpt in {candidate[:512], projected_candidate}:
                excerpt = projected_candidate
                projected = excerpt != candidate[:512]
            else:
                excerpt = project_public_result_text(raw_excerpt, protected_literals=references)
                projected = excerpt != raw_excerpt
            evidence_projected = evidence_projected or projected
            evidence_items.append(
                ReviewEvidence(
                    capture_id=cast(str, item["capture_id"]),
                    excerpt=excerpt,
                    sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
                    projection_applied=projected,
                )
            )
        operation = "create" if binding is None else cast(str, binding["operation"])
        patch = None if binding is None else patch_draft_from_binding(binding)
        patch_diff = None
        if patch is not None:
            assert binding is not None
            patch_value = cast(dict[str, object], binding["patch"])
            patch_diff = "".join(
                difflib.unified_diff(
                    cast(str, patch_value["base_body"]).splitlines(keepends=True),
                    proposed_body.splitlines(keepends=True),
                    fromfile="current",
                    tofile="proposed",
                )
            )
        replacement: str | None = None
        page_diff: str | None = None
        live_projected = False
        if binding is not None and operation == "update" and patch is None:
            replacement = "full_page"
            if live_body is not None:
                projected_live = project_public_result_text(
                    live_body, protected_literals=references
                )
                live_projected = projected_live != live_body
                page_diff = _bounded_page_diff(projected_live, markdown)
        return ReviewProposal(
            proposal_id=proposal_id,
            status=cast(str, proposal["status"]),
            title=title,
            markdown=markdown,
            space_id=cast(str | None, proposal["space_id"]),
            page_id=cast(str | None, proposal["page_id"]),
            target_page_id=(
                cast(str, binding["page_id"])
                if binding is not None and operation == "update"
                else None
            ),
            operation=operation,
            capture_ids=capture_ids,
            selected_capture_ids=selected_capture_ids,
            evidence=tuple(evidence_items),
            review_digest=review_digest,
            expected_page_sha256=(
                None if binding is None else cast(str | None, binding["expected_page_sha256"])
            ),
            expected_publication_id=(
                None if binding is None else cast(str | None, binding["expected_publication_id"])
            ),
            projection_applied=(
                markdown != raw_markdown
                or title != raw_title
                or evidence_projected
                or live_projected
            ),
            patch=patch,
            patch_diff=patch_diff,
            facet_binding_json=(
                portable_canonical_json_bytes(binding).decode("utf-8")
                if binding is not None and review_binding_base_version(binding) == 5
                else None
            ),
            stale=stale,
            replacement=replacement,
            page_diff=page_diff,
        )

    def _edit_baseline_body(
        self,
        proposal_id: str,
        context: tuple[dict[str, object], dict[str, object]] | None,
    ) -> str:
        """Return the stored body an edit replaces: the live target, else the raw draft."""
        connection = self._store.connect()
        try:
            if context is not None and context[1]["operation"] == "update":
                target = _target_state(self, connection, cast(str, context[1]["page_id"]))
                assert target is not None
                return parse_markdown(_read_page(self, cast(str, target["canonical_path"]))).body
            proposal = connection.execute(
                "SELECT proposed_kind, proposed_bytes, body FROM proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        finally:
            connection.close()
        if proposal is None:
            raise ValueError("unknown proposal")
        if cast(str, proposal["proposed_kind"]) == "page_update":
            return parse_markdown(cast(bytes, proposal["proposed_bytes"])).body
        return cast(str | None, proposal["body"]) or ""

    def _decide(
        self,
        proposal_id: str,
        outcome: DecisionOutcome,
        *,
        delivery_id: str,
        edited_markdown: str | None,
        expected_review_digest: str | None = None,
        authority: EffectiveAuthority | None = None,
        allow_duplicate: bool = False,
    ) -> DecisionRecord:
        _portable_id(proposal_id, "proposal")
        _delivery_id(delivery_id)
        outcome = DecisionOutcome(outcome)
        edited_markdown = _optional_text(
            edited_markdown, field="edited markdown", maximum=_MAX_TEXT
        )
        if (outcome is DecisionOutcome.EDITED) != (edited_markdown is not None):
            raise ValueError("edited outcome requires edited content")
        inspection = self._show_proposal(proposal_id, authority=authority)
        connection = self._store.connect()
        try:
            context = load_bound_context(connection, proposal_id)
            reserved = connection.execute(
                "SELECT 1 FROM decisions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            prior_delivery = connection.execute(
                "SELECT request_sha256 FROM decisions WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        finally:
            connection.close()
        request_value: dict[str, object] = {
            "edited_markdown": edited_markdown,
            "outcome": outcome.value,
            "proposal_id": proposal_id,
        }
        if context is not None:
            request_value["expected_review_digest"] = expected_review_digest
        request_sha = sha256(portable_canonical_json_bytes(request_value)).hexdigest()
        if prior_delivery is not None and prior_delivery["request_sha256"] != request_sha:
            self._quarantine(
                delivery_id,
                expected=cast(str, prior_delivery["request_sha256"]),
                actual=request_sha,
            )
            raise ValueError("conflicting delivery")
        if context is not None and expected_review_digest is None:
            raise ValueError("review digest is required")
        if (
            expected_review_digest is not None
            and expected_review_digest != inspection.review_digest
        ):
            raise ReviewBindingConflict("review digest conflict", binding="draft")
        if (
            outcome is DecisionOutcome.EDITED
            and reserved is None
            and projection_markers_added(
                self._edit_baseline_body(proposal_id, context), cast(str, edited_markdown)
            )
        ):
            raise ValueError("replacement contains projection placeholder")
        if (
            context is not None and reserved is None
            and outcome is not DecisionOutcome.REJECTED
            and review_binding_base_version(context[1]) != 5
        ):
            # Rejection writes no page, so a source, route or target that moved
            # after drafting must not strand the proposal. Facet (v5) bindings
            # keep their transactional check because it also enforces authority.
            validate_current_binding(self, context[1])
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
                if context is not None and delivery is None:
                    raise ValueError("proposal already has a terminal decision")
                existing_outcome = DecisionOutcome(cast(str, existing["outcome"]))
                if existing_outcome is not outcome:
                    raise ValueError("proposal already has a terminal decision")
                if outcome is DecisionOutcome.EDITED:
                    current = cast(bytes, existing["effective_bytes"])
                    if context is None:
                        capture = connection.execute(
                            "SELECT * FROM captures WHERE capture_id = ?",
                            (proposal["capture_id"],),
                        ).fetchone()
                        assert capture is not None
                        expected = self._proposal_page_bytes(
                            capture,
                            page_id=cast(str, proposal["page_id"]),
                            title=cast(str, proposal["title"]),
                            body=cast(str, edited_markdown),
                            modified_at=cast(str, existing["recorded_at"]),
                        )
                    else:
                        expected = bound_edited_bytes(
                            self,
                            context[0],
                            context[1],
                            cast(str, edited_markdown),
                            cast(str, existing["recorded_at"]),
                        )
                    if current != expected:
                        raise ValueError("proposal already has a terminal decision")
                duplicate = True
                decision_id = cast(str, existing["decision_id"])
            else:
                if context is not None and review_binding_base_version(context[1]) == 5:
                    validate_current_binding(
                        self, context[1], connection=connection, authority=authority
                    )
                if outcome in (DecisionOutcome.APPROVED, DecisionOutcome.EDITED):
                    from .developments import source_snapshot, validate_development_dependencies
                    from .facet_publication import owner_review_authority

                    if connection.execute("PRAGMA user_version").fetchone()[0] >= 16:
                        from .developments import validate_development_commitments

                        if context is not None:
                            validate_development_commitments(connection, context[1])
                        elif connection.execute(
                            "SELECT 1 FROM development_evidence WHERE derived_capture_id=?",
                            (proposal["capture_id"],),
                        ).fetchone():
                            raise ValueError(
                                "development evidence requires a committed review binding"
                            )
                        current_selected = (
                            context[1]["selected_capture_ids"]
                            if context is not None
                            else [proposal["capture_id"]]
                        )
                        for identity in cast(Sequence[str], current_selected):
                            source_snapshot(connection, identity)
                        selected = (
                            context[1]["provenance"]
                            if context is not None
                            else [proposal["capture_id"]]
                        )
                        validate_development_dependencies(
                            connection,
                            cast(Sequence[str], selected),
                            engine=cast("BrainEngine", self),
                            authority=authority
                            if authority is not None
                            else owner_review_authority(self),
                        )
                if (
                    cast(str, proposal["proposed_kind"]) == "page_update"
                    and proposal["space_id"] is None
                ):
                    raise ValueError("page proposal requires routed capture")
                if (
                    outcome in (DecisionOutcome.APPROVED, DecisionOutcome.EDITED)
                    and not allow_duplicate
                    and cast(str, proposal["proposed_kind"]) == "page_update"
                ):
                    sibling_page_ids = frozenset(
                        cast(str, item["page_id"])
                        for item in connection.execute(
                            "SELECT page_id FROM proposals WHERE set_delivery_id = ? "
                            "AND proposal_id != ? AND page_id IS NOT NULL",
                            (proposal["set_delivery_id"], proposal_id),
                        )
                    )
                    if (
                        duplicate_page_id(
                            connection,
                            space_id=cast(str, proposal["space_id"]),
                            page_id=cast(str, proposal["page_id"]),
                            title=cast(str, proposal["title"]),
                            capture_ids=(
                                cast(Sequence[str], context[1]["provenance"])
                                if context is not None
                                else (cast(str, proposal["capture_id"]),)
                            ),
                            sibling_page_ids=sibling_page_ids,
                        )
                        is not None
                    ):
                        raise ValueError("duplicate canonical page")
                now = _timestamp(self._clock())
                capture = connection.execute(
                    "SELECT * FROM captures WHERE capture_id = ?", (proposal["capture_id"],)
                ).fetchone()
                assert capture is not None
                if outcome is DecisionOutcome.REJECTED:
                    effective_bytes = None
                elif outcome is DecisionOutcome.EDITED:
                    if context is None:
                        effective_bytes = self._proposal_page_bytes(
                            capture,
                            page_id=cast(str, proposal["page_id"]),
                            title=cast(str, proposal["title"]),
                            body=cast(str, edited_markdown),
                            modified_at=now,
                        )
                    else:
                        effective_bytes = bound_edited_bytes(
                            self,
                            context[0],
                            context[1],
                            cast(str, edited_markdown),
                            now,
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
        siblings = tuple(
            cast(str, item["proposal_id"])
            for item in self._proposal_rows(cast(str, proposal["set_delivery_id"]))
        )
        capture = self._capture_row(cast(str, proposal["capture_id"]))
        connection = self._store.connect()
        try:
            context = load_bound_context(connection, cast(str, proposal["proposal_id"]))
        finally:
            connection.close()
        if context is None:
            proposal_record = self._legacy_proposal_snapshot(proposal, sibling_ids=siblings)
            binding = None
        else:
            proposal_record, binding = context
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
                expected_state_digest=(
                    None if binding is None else cast(str, binding["review_digest"])
                ),
            )
            portable_write_port(self).put_history("decision", portable_canonical_json_bytes(record))
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
                if binding is not None and binding["operation"] == "update":
                    portable_write_port(self).replace_page(
                        canonical_path,
                        effective,
                        expected_sha256=cast(str, binding["expected_page_sha256"]),
                    )
                else:
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
                    if binding is not None:
                        self._update_bound_page_head(
                            connection,
                            row=row,
                            proposal=proposal,
                            binding=binding,
                            canonical_path=canonical_path,
                            published_sha256=sha256(effective).hexdigest(),
                        )
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

    def _update_bound_page_head(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        proposal: sqlite3.Row,
        binding: dict[str, object],
        canonical_path: str,
        published_sha256: str,
    ) -> None:
        values = (
            row["page_id"],
            row["publication_id"],
            proposal["proposal_id"],
            proposal["capture_id"],
            canonical_path,
            published_sha256,
        )
        if binding["operation"] == "create":
            connection.execute(
                """
                INSERT INTO review_page_heads (
                    page_id, publication_id, proposal_id, capture_id,
                    canonical_path, published_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            if review_binding_base_version(binding) == 5:
                from .facet_publication import commit_publication_attachment

                commit_publication_attachment(
                    self,
                    connection,
                    binding=binding,
                    publication_id=cast(str, row["publication_id"]),
                    recorded_at=cast(str, row["recorded_at"]),
                )
            return
        current_head = connection.execute(
            "SELECT publication_id FROM review_page_heads WHERE page_id = ?",
            (row["page_id"],),
        ).fetchone()
        if current_head is None:
            connection.execute(
                """
                INSERT INTO review_page_heads (
                    page_id, publication_id, proposal_id, capture_id,
                    canonical_path, published_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            return
        updated = connection.execute(
            """
            UPDATE review_page_heads
            SET publication_id = ?, proposal_id = ?, capture_id = ?,
                canonical_path = ?, published_sha256 = ?
            WHERE page_id = ? AND publication_id = ?
            """,
            (
                row["publication_id"],
                proposal["proposal_id"],
                proposal["capture_id"],
                canonical_path,
                published_sha256,
                row["page_id"],
                binding["expected_publication_id"],
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("canonical page revision conflict")

    def _decision_public(self, row: sqlite3.Row, *, duplicate: bool) -> DecisionRecord:
        return DecisionRecord(
            decision_id=cast(str, row["decision_id"]),
            proposal_id=cast(str, row["proposal_id"]),
            outcome=DecisionOutcome(cast(str, row["outcome"])),
            page_id=cast(str | None, row["page_id"]),
            publication_id=cast(str | None, row["publication_id"]),
            duplicate=duplicate,
        )

    def _drain_review_decisions(self) -> None:
        connection = self._store.connect()
        try:
            decision_ids = tuple(
                cast(str, row["decision_id"])
                for row in connection.execute(
                    "SELECT decision_id FROM decisions WHERE stage < 3 ORDER BY rowid"
                )
            )
        finally:
            connection.close()
        for decision_id in decision_ids:
            self._process_decision(self._decision_row(decision_id))


class ReviewTasks:
    def __init__(self, engine: BrainEngine, *, authority: EffectiveAuthority | None = None) -> None:
        self._engine = engine
        self._authority = authority

    def with_authority(self, authority: EffectiveAuthority) -> ReviewTasks:
        """Bind review reads and decisions to the current trusted session authority."""
        return ReviewTasks(self._engine, authority=authority)

    def _proposal_scope(self, capture_id: str | Sequence[str], target_page_id: str | None) -> None:
        if self._authority is None:
            from .mcp_eligibility import lifecycle_capture_visible

            connection = self._engine._store.connect()
            try:
                for identity in (capture_id,) if isinstance(capture_id, str) else capture_id:
                    if not lifecycle_capture_visible(connection, identity):
                        raise T03Error("not_found")
            finally:
                connection.close()
            return
        from .records import RecordProjector

        self._authority.require("review-propose")
        connection = self._engine._store.connect()
        try:
            projector = RecordProjector(self._engine.profile, connection, self._authority)
            for identity in (capture_id,) if isinstance(capture_id, str) else capture_id:
                projector.source(identity, expected=identity)
            if target_page_id is not None:
                projector.canonical(target_page_id)
        finally:
            connection.close()

    def propose(
        self,
        capture_id: str | Sequence[str],
        drafts: Sequence[ProposalDraft | PatchDraft],
        *,
        delivery_id: str,
        target_page_id: str | None = None,
    ) -> tuple[ProposalRecord, ...]:
        self._proposal_scope(capture_id, target_page_id)
        with self._engine._writer_lease.acquire_shared_writer():
            self._proposal_scope(capture_id, target_page_id)
            if self._authority is None and (
                not isinstance(capture_id, str) or target_page_id is not None
            ):
                self._engine._drain_review_decisions()
            return self._engine._propose(
                capture_id, drafts, delivery_id, target_page_id=target_page_id
            )

    def propose_append(
        self,
        capture_id: str | Sequence[str],
        *,
        target_page_id: str,
        append_markdown: str,
        delivery_id: str,
    ) -> tuple[ProposalRecord, ...]:
        self._proposal_scope(capture_id, target_page_id)
        with self._engine._writer_lease.acquire_shared_writer():
            self._proposal_scope(capture_id, target_page_id)
            return self._engine._propose_append(
                capture_id,
                target_page_id=target_page_id,
                append_markdown=append_markdown,
                delivery_id=delivery_id,
            )

    def list(
        self,
        *,
        capture_id: str | None = None,
        status: str | None = None,
        space_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        facet_query_id: str | None = None,
    ) -> tuple[ProposalRecord, ...]:
        if self._authority is not None:
            self._authority.require("review-read")
        return self._engine._list_proposals(
            capture_id=capture_id,
            status=status,
            space_id=space_id,
            limit=limit,
            offset=offset,
            authority=self._authority,
            facet_query_id=facet_query_id,
        )

    def show(self, proposal_id: str) -> ReviewProposal:
        if self._authority is not None:
            self._authority.require("review-read")
        return self._engine._show_proposal(proposal_id, authority=self._authority)

    def decide(
        self,
        proposal_id: str,
        outcome: DecisionOutcome,
        *,
        delivery_id: str,
        edited_markdown: str | None = None,
        expected_review_digest: str | None = None,
        allow_duplicate: bool = False,
    ) -> DecisionRecord:
        if self._authority is not None:
            self._authority.require("review-decide")
        with self._engine._writer_lease.acquire_shared_writer():
            self._engine._drain_review_decisions()
            return self._engine._decide(
                proposal_id,
                outcome,
                delivery_id=delivery_id,
                edited_markdown=edited_markdown,
                expected_review_digest=expected_review_digest,
                authority=self._authority,
                allow_duplicate=allow_duplicate,
            )
