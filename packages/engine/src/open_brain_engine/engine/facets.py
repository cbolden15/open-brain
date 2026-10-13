"""Foreground owner-defined facets over the admitted private engine store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import _timestamp
from open_brain_engine.storage.filesystem import StorageError
from open_brain_engine.storage.locks import LockBusyError

from . import facet_contracts as fc
from .cursors import CursorStore, binding_digest
from .facet_queries import (
    AuthorizedSubject,
    authorize_subject,
    membership_rows,
    require_query_scope,
    require_values,
    resolve_query,
    resolve_subject,
    select_records,
    subject_version,
)
from .facet_schema_migration import facet_schema_available
from .facet_store import (
    change,
    commit_mutation,
    get_row,
    name_key,
    next_sequence,
    replay_receipt,
    semantic_row,
)
from .paging import authority_binding, continuation, generations, read_snapshot
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine

MAX_LIST_BYTES = 250_000
ORDERING_VERSION = "facet-stable-subject-acceptance-v1"


class FacetTasks:
    def __init__(
        self, engine: BrainEngine, *, checkpoint: Callable[[str], None] = lambda _stage: None
    ) -> None:
        self._engine = engine
        self._checkpoint = checkpoint

    def _admit(
        self, request: fc.FacetRequest, operation: str, authority: EffectiveAuthority
    ) -> dict[str, Any]:
        value = fc.request_from_wire(operation, request).to_wire()
        if not isinstance(authority, EffectiveAuthority):
            raise fc.FacetError("unsupported_capability")
        try:
            for capability in fc.REQUIRED_CAPABILITIES[operation]:
                authority.require(capability)
        except T03Error:
            raise fc.FacetError("unsupported_capability") from None
        self._engine._assert_root()
        return value

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        try:
            with read_snapshot(self._engine) as connection:
                if not facet_schema_available(connection):
                    raise fc.FacetError("unsupported_capability")
                if not connection.in_transaction:
                    connection.execute("BEGIN")
                yield connection
        except T03Error as error:
            raise self._public_error(error) from None

    @staticmethod
    def _public_error(error: T03Error) -> fc.FacetError:
        try:
            return fc.FacetError(error.code)
        except ValueError:
            return fc.FacetError("operation_pending")

    def _check_selector(
        self,
        connection: sqlite3.Connection,
        selector: dict[str, Any],
        authority: EffectiveAuthority,
    ) -> None:
        require_query_scope(selector, authority)
        require_values(connection, selector["all_value_ids"])
        for space_id in selector["space_ids"]:
            if (
                connection.execute("SELECT 1 FROM spaces WHERE space_id=?", (space_id,)).fetchone()
                is None
            ):
                raise fc.FacetError("not_found")

    @staticmethod
    def _expect(expected: int, row: dict[str, Any] | None) -> None:
        if expected != (0 if row is None else row["version"]):
            raise fc.FacetError("version_conflict")

    @staticmethod
    def _entity(connection: sqlite3.Connection, kind: str, identity: str) -> dict[str, Any]:
        row = get_row(connection, kind, identity)
        if row is None:
            raise fc.FacetError("not_found")
        return row

    @staticmethod
    def _unique_name(
        connection: sqlite3.Connection,
        kind: str,
        name: str,
        *,
        dimension_id: str | None = None,
        own_id: str | None = None,
    ) -> None:
        tables = {
            "dimension": ("facet_dimensions", "dimension_id"),
            "value": ("facet_values", "value_id"),
            "query": ("facet_queries", "query_id"),
        }
        table, key = tables[kind]
        sql = f"SELECT {key} FROM {table} WHERE name_key=?"
        parameters: tuple[object, ...] = (name_key(name),)
        if kind == "value":
            sql += " AND dimension_id=?"
            parameters += (dimension_id,)
        row = connection.execute(sql, parameters).fetchone()
        if row is not None and row[0] != own_id:
            raise fc.FacetError("name_conflict")

    def _scope_write(
        self,
        connection: sqlite3.Connection,
        method: str,
        args: dict[str, Any],
        authority: EffectiveAuthority,
    ) -> tuple[dict[str, Any] | None, AuthorizedSubject | None]:
        subject = None
        row = None
        if method.startswith("facet.dimension.") and method != "facet.dimension.create":
            row = self._entity(connection, "dimension", args["dimension_id"])
        elif method == "facet.value.create":
            self._entity(connection, "dimension", args["dimension_id"])
        elif method.startswith("facet.value."):
            row = self._entity(connection, "value", args["value_id"])
        elif method == "facet.assignment.patch":
            subject = resolve_subject(
                connection, self._engine.profile, args["record_id"], authority
            )
            row = get_row(connection, "subject", subject.to_wire())
            require_values(connection, args["add_value_ids"] + args["remove_value_ids"])
        elif method == "facet.query.attach":
            subject = resolve_subject(connection, self._engine.profile, args["page_id"], authority)
            row = get_row(connection, "attachment", args["page_id"])
            resolve_query(connection, args["query_id"], authority)
        elif method.startswith("facet.query.") and method != "facet.query.create":
            row = resolve_query(connection, args["query_id"], authority)
        if "selector" in args:
            self._check_selector(connection, args["selector"], authority)
        return row, subject

    def run_mutation(
        self,
        connection: sqlite3.Connection,
        request: fc.FacetRequest,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, Any]:
        """Trusted P/V transaction seam; caller owns admission/fence/commit.

        This method is absent from public protocol and transport registries.
        It runs the identical request/scope/replay/version/effect logic as public writes.
        """
        method = request.operation
        args = self._admit(request, method, authority)
        if "expected_version" not in args or not connection.in_transaction:
            raise fc.FacetError("invalid_arguments")
        if not facet_schema_available(connection):
            raise fc.FacetError("unsupported_capability")
        before, subject = self._scope_write(connection, method, args, authority)
        receipt = replay_receipt(
            connection, method=method, arguments=args, principal_id=authority.principal_id
        )
        if receipt is not None:
            return receipt
        self._expect(args["expected_version"], before)
        recorded = _timestamp(self._engine._clock())
        changes, subject_id, version = self._changes(
            connection, method, args, before, subject, authority, recorded
        )
        brain = connection.execute(
            "SELECT brain_id FROM brain_identity WHERE singleton=1"
        ).fetchone()
        return commit_mutation(
            connection,
            brain_id=brain[0],
            method=method,
            arguments=args,
            authority=authority,
            actor_id=self._engine.profile.owner_actor_id,
            recorded_at=recorded,
            changes=changes,
            subject_id=subject_id,
            version=version,
        )

    def _mutate(
        self, request: fc.FacetRequest, method: str, authority: EffectiveAuthority
    ) -> fc.FacetResponse:
        self._admit(request, method, authority)
        try:
            with (
                self._engine._writer_lease.acquire_shared_writer(),
                self._engine._store.transaction() as connection,
            ):
                receipt = self.run_mutation(connection, request, authority=authority)
                self._checkpoint("before_semantic_commit")
            self._checkpoint("after_semantic_commit")
            return fc.response_from_wire(method, receipt)
        except T03Error as error:
            raise self._public_error(error) from None
        except sqlite3.Error, StorageError, LockBusyError:
            raise fc.FacetError("operation_pending") from None

    @staticmethod
    def _reserved_query_attachment(connection: sqlite3.Connection, query_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM decisions d JOIN review_contexts c USING(proposal_id) "
                "WHERE d.outcome IN ('approved','edited') AND d.stage<3 "
                "AND json_extract(c.binding_json,'$.schema_version')=5 "
                "AND json_extract(c.binding_json,'$.operation')='create' "
                "AND json_extract(c.binding_json,'$.query_id')=? LIMIT 1",
                (query_id,),
            ).fetchone()
            is not None
        )

    def _changes(
        self,
        connection: sqlite3.Connection,
        method: str,
        args: dict[str, Any],
        before: dict[str, Any] | None,
        subject: AuthorizedSubject | None,
        authority: EffectiveAuthority,
        recorded: str,
    ) -> tuple[list[dict[str, Any]], str, int]:
        after: dict[str, Any]
        kind, action = method.removeprefix("facet.").split(".")
        if kind == "assignment":
            assert subject is not None
            if args["expected_revision_id"] != subject.revision_id:
                raise fc.FacetError("revision_changed")
            rows = {
                item["value_id"]: item
                for item in membership_rows(
                    connection, subject.kind, subject.id, present_only=False
                )
            }
            present = {key for key, item in rows.items() if item["present"]}
            added = set(args["add_value_ids"]) - present
            removed = set(args["remove_value_ids"]) & present
            for value_id in added:
                value = self._entity(connection, "value", value_id)
                dimension = self._entity(connection, "dimension", value["dimension_id"])
                if value["archived"] or dimension["archived"]:
                    raise fc.FacetError("facet_archived")
            if len((present | added) - removed) > 128:
                raise fc.FacetError("limit_exceeded")
            version = 0 if before is None else before["version"]
            if not added and not removed:
                return [], subject.id, version
            after = {"subject_kind": subject.kind, "subject_id": subject.id, "version": version + 1}
            changes = [change("subject", before, after)]
            sequence = next_sequence(connection)
            for value_id in sorted(added | removed):
                old = rows.get(value_id)
                new = {
                    "subject_kind": subject.kind,
                    "subject_id": subject.id,
                    "value_id": value_id,
                    "present": value_id in added,
                    "membership_version": 1 if old is None else old["membership_version"] + 1,
                    "last_event_sequence": sequence,
                }
                changes.append(change("assignment", old, new))
            return changes, subject.id, version + 1
        if kind == "query" and action == "attach":
            assert subject is not None
            query = resolve_query(
                connection, args["query_id"], authority, args["expected_query_version"]
            )
            if subject.revision_id != args["expected_revision_id"]:
                raise fc.FacetError("revision_changed")
            version = 0 if before is None else before["version"]
            if before is not None and before["query_id"] == args["query_id"]:
                return [], args["page_id"], version
            if query["archived"]:
                raise fc.FacetError("query_archived")
            after = {
                "page_id": args["page_id"],
                "query_id": args["query_id"],
                "version": version + 1,
            }
            return [change("attachment", before, after)], args["page_id"], version + 1
        key = {"dimension": "dimension_id", "value": "value_id", "query": "query_id"}[kind]
        if action == "create":
            self._unique_name(connection, kind, args["name"], dimension_id=args.get("dimension_id"))
            identity = {"dimension": "facetdim_", "value": "facetval_", "query": "facetquery_"}[
                kind
            ] + str(uuid4())
            after = {
                key: identity,
                "name": args["name"],
                "archived": False,
                "version": 1,
                "created_at": recorded,
                "updated_at": recorded,
            }
            if kind == "value":
                parent = self._entity(connection, "dimension", args["dimension_id"])
                self._expect(args["expected_dimension_version"], parent)
                if parent["archived"]:
                    raise fc.FacetError("facet_archived")
                after["dimension_id"] = args["dimension_id"]
            if kind == "query":
                after.update(selector=args["selector"], selection_version=1)
            return [change(kind, None, after)], identity, 1
        assert before is not None
        identity = before[key]
        if kind == "query" and before["archived"] and action != "archive":
            raise fc.FacetError("query_archived")
        after = dict(before)
        if action in {"rename", "update"}:
            self._unique_name(
                connection,
                kind,
                args["name"],
                dimension_id=before.get("dimension_id"),
                own_id=identity,
            )
            after["name"] = args["name"]
        if action == "archive":
            after["archived"] = True
        if action == "update":
            after["selector"] = args["selector"]
            after["selection_version"] += int(before["selector"] != after["selector"])
        if (
            kind == "query"
            and (after["archived"] != before["archived"] or after["selector"] != before["selector"])
            and self._reserved_query_attachment(connection, identity)
        ):
            raise fc.FacetError("operation_pending")
        if after == before:
            return [], identity, before["version"]
        if before["version"] >= 2**53 - 1:
            raise fc.FacetError("limit_exceeded")
        after.update(version=before["version"] + 1, updated_at=recorded)
        return [change(kind, before, after)], identity, after["version"]

    def _page(
        self,
        connection: sqlite3.Connection,
        request: fc.FacetRequest,
        authority: EffectiveAuthority,
        items: Sequence[tuple[Any, dict[str, Any]]],
        *,
        visible_state: object | None = None,
    ) -> fc.FacetResponse:
        args = request.to_wire()
        cursor = args.pop("cursor", None)
        binding = binding_digest(
            {
                "method": request.operation,
                "arguments": args,
                "authority": authority_binding(authority),
                "ordering": ORDERING_VERSION,
            }
        )
        # Facet mutations conservatively invalidate all continuations, including denied changes.
        generation = generations(connection)
        store = CursorStore(self._engine.profile)
        now = self._engine._clock().timestamp()
        prior = continuation(
            store,
            cursor,
            purpose=request.operation,
            binding=binding,
            generation=generation,
            now=now,
        )
        last = None if prior is None else prior.get("last")
        if prior is not None and last is None:
            raise fc.FacetError("cursor_invalid")
        candidates = [(key, item) for key, item in items if last is None or key > last]
        selected: list[dict[str, Any]] = []
        selected_key = last
        for key, item in candidates:
            if len(selected) >= args["limit"]:
                break
            trial = {
                "status": "ok",
                "dto_version": 1,
                "items": [*selected, item],
                "next_cursor": "x" * 98,
                "complete": False,
            }
            if len(portable_canonical_json_bytes(trial)) > MAX_LIST_BYTES:
                if not selected:
                    raise fc.FacetError("response_too_large")
                break
            selected.append(item)
            selected_key = key
        complete = len(selected) == len(candidates)
        next_cursor = (
            None
            if complete
            else store.allocate(
                {
                    "purpose": request.operation,
                    "binding": binding,
                    "generation": generation,
                    "last": selected_key,
                },
                now=now,
            )
        )
        return fc.response_from_wire(
            request.operation,
            {
                "status": "ok",
                "dto_version": 1,
                "items": selected,
                "next_cursor": next_cursor,
                "complete": complete,
            },
        )

    @staticmethod
    def _public_entity(
        connection: sqlite3.Connection, kind: str, row: dict[str, Any]
    ) -> dict[str, Any]:
        value = {key: item for key, item in row.items() if key not in {"created_at", "updated_at"}}
        if kind == "value":
            parent = get_row(connection, "dimension", row["dimension_id"])
            value["effective_archived"] = (
                row["archived"] or parent is not None and parent["archived"]
            )
        return value

    def _list(
        self, request: fc.FacetRequest, method: str, kind: str, authority: EffectiveAuthority
    ) -> fc.FacetResponse:
        args = self._admit(request, method, authority)
        with self._read() as connection:
            table = {
                "dimension": "facet_dimensions",
                "value": "facet_values",
                "query": "facet_queries",
            }[kind]
            if kind == "value":
                self._entity(connection, "dimension", args["dimension_id"])
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE dimension_id=? ORDER BY value_id",
                    (args["dimension_id"],),
                )
            else:
                key = "dimension_id" if kind == "dimension" else "query_id"
                rows = connection.execute(f"SELECT * FROM {table} ORDER BY {key}")
            items = []
            for raw in rows:
                row = semantic_row(raw)
                if kind == "query":
                    try:
                        require_query_scope(row["selector"], authority)
                    except fc.FacetError:
                        continue
                value = self._public_entity(connection, kind, row)
                if not args["include_archived"] and value.get(
                    "effective_archived", value["archived"]
                ):
                    continue
                key = row[
                    {"dimension": "dimension_id", "value": "value_id", "query": "query_id"}[kind]
                ]
                items.append((key, value))
            return self._page(connection, request, authority, items)

    def dimensions_list(
        self, request: fc.DimensionsListRequest, *, authority: EffectiveAuthority
    ) -> fc.DimensionsListResponse:
        return cast(
            fc.DimensionsListResponse,
            self._list(request, "facet.dimension.list", "dimension", authority),
        )

    def values_list(
        self, request: fc.ValuesListRequest, *, authority: EffectiveAuthority
    ) -> fc.ValuesListResponse:
        return cast(
            fc.ValuesListResponse, self._list(request, "facet.value.list", "value", authority)
        )

    def queries_list(
        self, request: fc.QueriesListRequest, *, authority: EffectiveAuthority
    ) -> fc.QueriesListResponse:
        return cast(
            fc.QueriesListResponse, self._list(request, "facet.query.list", "query", authority)
        )

    def assignments_get(
        self, request: fc.AssignmentsGetRequest, *, authority: EffectiveAuthority
    ) -> fc.AssignmentsGetResponse:
        args = self._admit(request, "facet.assignment.get", authority)
        with self._read() as connection:
            subject = resolve_subject(
                connection, self._engine.profile, args["record_id"], authority
            )
            memberships = [
                {"value_id": row["value_id"], "membership_version": row["membership_version"]}
                for row in membership_rows(connection, subject.kind, subject.id)
            ]
            item = {
                "subject": subject.to_wire(),
                "record_id": subject.record_id,
                "revision_id": subject.revision_id,
                "version": subject_version(connection, subject),
                "memberships": memberships,
            }
            return cast(
                fc.AssignmentsGetResponse,
                fc.response_from_wire(
                    request.operation, {"status": "ok", "dto_version": 1, "item": item}
                ),
            )

    def query_get(
        self, request: fc.QueryGetRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryGetResponse:
        args = self._admit(request, "facet.query.get", authority)
        with self._read() as connection:
            row = resolve_query(connection, args["query_id"], authority)
            return cast(
                fc.QueryGetResponse,
                fc.response_from_wire(
                    request.operation,
                    {
                        "status": "ok",
                        "dto_version": 1,
                        "item": self._public_entity(connection, "query", row),
                    },
                ),
            )

    def query_attachment_get(
        self, request: fc.QueryAttachmentGetRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryAttachmentGetResponse:
        args = self._admit(request, "facet.query.attachment.get", authority)
        with self._read() as connection:
            resolve_subject(connection, self._engine.profile, args["page_id"], authority)
            item = get_row(connection, "attachment", args["page_id"])
            if item is not None:
                resolve_query(connection, item["query_id"], authority)
            return cast(
                fc.QueryAttachmentGetResponse,
                fc.response_from_wire(
                    request.operation, {"status": "ok", "dto_version": 1, "item": item}
                ),
            )

    def query_page(
        self, request: fc.QueryPageRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryPageResponse:
        args = self._admit(request, "facet.query.page", authority)
        with self._read() as connection:
            if "query_id" in args:
                query = resolve_query(
                    connection, args["query_id"], authority, args["expected_query_version"]
                )
                selector = query["selector"]
            else:
                selector = args["selector"]
            selected = select_records(connection, self._engine.profile, selector, authority)
            items = [
                ([item.accepted_at, item.subject.id, item.subject.kind], item.to_wire())
                for item in selected
            ]
            return cast(fc.QueryPageResponse, self._page(connection, request, authority, items))

    def _history_subject(
        self, connection: sqlite3.Connection, subject: dict[str, str], authority: EffectiveAuthority
    ) -> None:
        kind = subject["kind"]
        if kind in {"source", "canonical"}:
            authorize_subject(connection, self._engine.profile, subject, authority)
        elif kind == "query":
            resolve_query(connection, subject["id"], authority)
        else:
            self._entity(connection, kind, subject["id"])

    @staticmethod
    def _matches_change(item: dict[str, Any], subject: dict[str, str]) -> bool:
        entity = item["entity"]
        if subject["kind"] in {"source", "canonical"}:
            if entity["kind"] == "subject":
                return bool(entity["id"] == subject)
            if entity["kind"] == "assignment":
                return bool(
                    entity["id"]["subject_kind"] == subject["kind"]
                    and entity["id"]["subject_id"] == subject["id"]
                )
            return bool(
                subject["kind"] == "canonical"
                and entity["kind"] == "attachment"
                and entity["id"] == subject["id"]
            )
        return bool(entity["kind"] == subject["kind"] and entity["id"] == subject["id"])

    @staticmethod
    def _history_change_visible(
        connection: sqlite3.Connection, change_value: dict[str, Any], authority: EffectiveAuthority
    ) -> bool:
        try:
            kind = change_value["entity"]["kind"]
            for row in (change_value["before"], change_value["after"]):
                if row is None:
                    continue
                if kind == "query":
                    require_query_scope(row["selector"], authority)
                elif kind == "attachment":
                    resolve_query(connection, row["query_id"], authority)
        except fc.FacetError:
            return False
        return True

    def history_list(
        self, request: fc.HistoryListRequest, *, authority: EffectiveAuthority
    ) -> fc.HistoryListResponse:
        args = self._admit(request, "facet.history.list", authority)
        with self._read() as connection:
            self._history_subject(connection, args["subject"], authority)
            items = []
            for raw in connection.execute(
                "SELECT event_json FROM facet_events WHERE (? IS NULL OR sequence<=?) "
                "ORDER BY sequence",
                (args["as_of_sequence"], args["as_of_sequence"]),
            ):
                event = json.loads(raw[0])
                changes = [
                    item
                    for item in event["changes"]
                    if self._matches_change(item, args["subject"])
                    and self._history_change_visible(connection, item, authority)
                ]
                if not changes:
                    continue
                event = {
                    key: item
                    for key, item in event.items()
                    if key not in {"schema_version", "brain_id", "operation_id"}
                }
                event["changes"] = changes
                items.append((event["sequence"], event))
            return cast(fc.HistoryListResponse, self._page(connection, request, authority, items))

    def history_state(
        self, request: fc.HistoryStateRequest, *, authority: EffectiveAuthority
    ) -> fc.HistoryStateResponse:
        args = self._admit(request, "facet.history.state", authority)
        with self._read() as connection:
            self._history_subject(connection, args["subject"], authority)
            subject = args["subject"]
            rows: dict[tuple[str, str], dict[str, Any]] = {}
            for raw in connection.execute(
                "SELECT event_json FROM facet_events WHERE sequence<=? ORDER BY sequence",
                (args["as_of_sequence"],),
            ):
                for item in json.loads(raw[0])["changes"]:
                    entity = item["entity"]
                    rows[(entity["kind"], json.dumps(entity["id"], sort_keys=True))] = item["after"]
            if subject["kind"] in {"source", "canonical"}:
                state = rows.get(("subject", json.dumps(subject, sort_keys=True)))
                memberships = [
                    {"value_id": row["value_id"], "membership_version": row["membership_version"]}
                    for (kind, _), row in rows.items()
                    if kind == "assignment"
                    and row["subject_kind"] == subject["kind"]
                    and row["subject_id"] == subject["id"]
                    and row["present"]
                ]
                memberships.sort(key=lambda row: row["value_id"])
                attachment = (
                    rows.get(("attachment", json.dumps(subject["id"], sort_keys=True)))
                    if subject["kind"] == "canonical"
                    else None
                )
                if attachment is not None:
                    resolve_query(connection, attachment["query_id"], authority)
                item = {
                    "subject": subject,
                    "version": 0 if state is None else state["version"],
                    "memberships": memberships,
                    "attachment": attachment,
                }
            else:
                state = rows.get((subject["kind"], json.dumps(subject["id"], sort_keys=True)))
                item = (
                    None
                    if state is None
                    else {
                        key: value
                        for key, value in state.items()
                        if key not in {"created_at", "updated_at"}
                    }
                )
                if item is not None and subject["kind"] == "query":
                    require_query_scope(item["selector"], authority)
                if item is not None and subject["kind"] == "value":
                    assert state is not None
                    parent = rows.get(
                        ("dimension", json.dumps(state["dimension_id"], sort_keys=True))
                    )
                    item["effective_archived"] = (
                        item["archived"] or parent is not None and parent["archived"]
                    )
            return cast(
                fc.HistoryStateResponse,
                fc.response_from_wire(
                    request.operation,
                    {
                        "status": "ok",
                        "dto_version": 1,
                        "as_of_sequence": args["as_of_sequence"],
                        "item": item,
                    },
                ),
            )

    def dimension_create(
        self, request: fc.DimensionCreateRequest, *, authority: EffectiveAuthority
    ) -> fc.DimensionCreateResponse:
        return cast(
            fc.DimensionCreateResponse, self._mutate(request, "facet.dimension.create", authority)
        )

    def dimension_rename(
        self, request: fc.DimensionRenameRequest, *, authority: EffectiveAuthority
    ) -> fc.DimensionRenameResponse:
        return cast(
            fc.DimensionRenameResponse, self._mutate(request, "facet.dimension.rename", authority)
        )

    def dimension_archive(
        self, request: fc.DimensionArchiveRequest, *, authority: EffectiveAuthority
    ) -> fc.DimensionArchiveResponse:
        return cast(
            fc.DimensionArchiveResponse, self._mutate(request, "facet.dimension.archive", authority)
        )

    def value_create(
        self, request: fc.ValueCreateRequest, *, authority: EffectiveAuthority
    ) -> fc.ValueCreateResponse:
        return cast(fc.ValueCreateResponse, self._mutate(request, "facet.value.create", authority))

    def value_rename(
        self, request: fc.ValueRenameRequest, *, authority: EffectiveAuthority
    ) -> fc.ValueRenameResponse:
        return cast(fc.ValueRenameResponse, self._mutate(request, "facet.value.rename", authority))

    def value_archive(
        self, request: fc.ValueArchiveRequest, *, authority: EffectiveAuthority
    ) -> fc.ValueArchiveResponse:
        return cast(
            fc.ValueArchiveResponse, self._mutate(request, "facet.value.archive", authority)
        )

    def assignments_patch(
        self, request: fc.AssignmentsPatchRequest, *, authority: EffectiveAuthority
    ) -> fc.AssignmentsPatchResponse:
        return cast(
            fc.AssignmentsPatchResponse, self._mutate(request, "facet.assignment.patch", authority)
        )

    def query_create(
        self, request: fc.QueryCreateRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryCreateResponse:
        return cast(fc.QueryCreateResponse, self._mutate(request, "facet.query.create", authority))

    def query_update(
        self, request: fc.QueryUpdateRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryUpdateResponse:
        return cast(fc.QueryUpdateResponse, self._mutate(request, "facet.query.update", authority))

    def query_archive(
        self, request: fc.QueryArchiveRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryArchiveResponse:
        return cast(
            fc.QueryArchiveResponse, self._mutate(request, "facet.query.archive", authority)
        )

    def query_attach(
        self, request: fc.QueryAttachRequest, *, authority: EffectiveAuthority
    ) -> fc.QueryAttachResponse:
        return cast(fc.QueryAttachResponse, self._mutate(request, "facet.query.attach", authority))
