"""Versioned lexical keyset paging and complete projected record reads."""

from __future__ import annotations

import math
import sqlite3
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import StorageError

from .cursors import CursorStore, binding_digest
from .records import RecordProjector
from .t03_contracts import (
    EffectiveAuthority,
    RecordReadRequest,
    RecordReadResponse,
    SearchPageRequest,
    SearchPageResponse,
    T03Error,
)

if TYPE_CHECKING:
    from .local import BrainEngine

RANKING_VERSION = "fts5-bm25-0-10-1-binary-v1"
# Leave transport framing and duplicate JSON string escaping room below 1 MiB.
MAX_RESPONSE_BYTES = 240_000


def authority_binding(authority: EffectiveAuthority) -> dict[str, Any]:
    return {
        "principal": authority.principal_id,
        "session": authority.session_id,
        "capabilities": sorted(authority.capabilities),
        "owner": authority.owner,
        "spaces": None if authority.space_ids is None else sorted(authority.space_ids),
        "epoch": authority.authorization_epoch,
    }


@contextmanager
def read_snapshot(engine: BrainEngine) -> Iterator[sqlite3.Connection]:
    engine._assert_root()
    with engine._writer_lease.acquire_shared_writer():
        CursorStore(engine.profile).bind_root(engine)
        connection = engine._store.connect()
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 7:
                raise T03Error("incompatible_runtime")
            yield connection
        except sqlite3.Error, StorageError:
            raise T03Error("operation_pending") from None
        finally:
            connection.rollback()
            connection.close()


def generations(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM engine_generations WHERE singleton=1").fetchone()
    if row is None:
        raise T03Error("operation_pending")
    return dict(row)


def continuation(
    store: CursorStore,
    cursor: str | None,
    *,
    purpose: str,
    binding: str,
    generation: dict[str, Any],
    now: float,
) -> dict[str, Any] | None:
    if cursor is None:
        return None
    record = store.resolve(cursor, now=now)
    if record.get("purpose") != purpose or record.get("binding") != binding:
        raise T03Error("cursor_invalid")
    if record.get("generation") != generation:
        raise T03Error("cursor_stale")
    return record


def _score_hex(score: float) -> str:
    if not math.isfinite(score):
        raise T03Error("projection_stale")
    return struct.pack(">d", 0.0 if score == 0 else score).hex()


def _last_key(record: dict[str, Any] | None) -> tuple[float, str, str] | None:
    if record is None:
        return None
    try:
        key = record["last"]
        if not isinstance(key, list) or len(key) != 4 or len(key[0]) != 16:
            raise ValueError
        score = struct.unpack(">d", bytes.fromhex(key[0]))[0]
        if not math.isfinite(score) or any(type(value) is not str for value in key[1:]):
            raise ValueError
        return score, key[1], key[2]
    except ValueError, TypeError, KeyError, struct.error:
        raise T03Error("cursor_invalid") from None


def search_page(
    engine: BrainEngine,
    request: SearchPageRequest,
    *,
    authority: EffectiveAuthority,
) -> SearchPageResponse:
    authority.require("search")
    if request.mode == "hybrid_required":
        raise T03Error("model_unavailable")
    from .retrieval import _compile_query

    try:
        compiled = _compile_query(request.query)
    except ValueError:
        raise T03Error("invalid_arguments") from None
    filters = {key: sorted(value) for key, value in request.filters.items()}
    binding = binding_digest(
        {
            "query": request.query.strip(),
            "filters": filters,
            "mode": request.mode,
            "mode_used": "lexical",
            "limit": request.limit,
            "ranking": RANKING_VERSION,
            "authority": authority_binding(authority),
        }
    )
    with read_snapshot(engine) as connection:
        generation = generations(connection)
        now = engine._clock().timestamp()
        store = CursorStore(engine.profile)
        prior = continuation(
            store,
            request.cursor,
            purpose="search.page",
            binding=binding,
            generation=generation,
            now=now,
        )
        last = _last_key(prior)
        parameters: list[Any] = []
        clauses = [
            "(d.record_type='canonical' OR EXISTS (SELECT 1 FROM logical_sources s "
            "WHERE s.head_capture_id=d.result_id AND s.historical_only=0 "
            "AND s.lifecycle='active' AND s.availability='available'))"
        ]
        if authority.space_ids is not None:
            values = sorted(authority.space_ids)
            clauses.append("d.space_id IN (" + ",".join("?" for _ in values) + ")")
            parameters.extend(values)
        for name, column in (
            ("space_ids", "space_id"),
            ("payload_families", "payload_family"),
            ("record_types", "record_type"),
        ):
            values = filters[name]
            if values:
                clauses.append(f"d.{column} IN (" + ",".join("?" for _ in values) + ")")
                parameters.extend(values)
        parameters.append(compiled.disjunction)
        seek = ""
        if last is not None:
            seek = "WHERE (score,record_type COLLATE BINARY,result_id COLLATE BINARY) > (?,?,?)"
            parameters.extend(last)
        sql = f"""
            WITH authorized AS MATERIALIZED (
                SELECT d.*,i.fts_rowid FROM search_documents d
                JOIN search_fts_identity i USING(result_id) WHERE {" AND ".join(clauses)}
            ), hits AS MATERIALIZED (
                SELECT a.*,bm25(search_documents_fts,0.0,10.0,1.0) AS score
                FROM authorized a JOIN search_documents_fts ON
                  search_documents_fts.rowid=a.fts_rowid AND
                  search_documents_fts.result_id=a.result_id
                WHERE search_documents_fts MATCH ?
            )
            SELECT * FROM hits {seek}
            ORDER BY score ASC,record_type COLLATE BINARY,result_id COLLATE BINARY
        """
        projector = RecordProjector(engine.profile, connection, authority)
        results: list[dict[str, Any]] = []
        next_key: list[Any] | None = None
        complete = True
        for row in connection.execute(sql, parameters):
            score = float(row["score"])
            exact = _score_hex(score)
            try:
                projected = projector.read(row["result_id"])
            except T03Error as error:
                if error.code == "not_found":
                    continue
                raise
            summary = projected.summary
            if row["body"] != projected.text or row["title"] != summary["title"]:
                raise T03Error("projection_stale")
            candidate = results + [summary]
            size = len(portable_canonical_json_bytes(candidate))
            if len(results) == request.limit or size > MAX_RESPONSE_BYTES:
                if not results:
                    raise T03Error("response_too_large")
                complete = False
                break
            results.append(summary)
            next_key = [exact, summary["record_type"], summary["record_id"], summary["revision_id"]]
        next_cursor = None
        if not complete:
            next_cursor = store.allocate(
                {
                    "purpose": "search.page",
                    "binding": binding,
                    "generation": generation,
                    "last": next_key,
                },
                now=now,
            )
        return cast(
            SearchPageResponse,
            SearchPageResponse.from_wire(
                {
                    "status": "ok",
                    "dto_version": 1,
                    "results": results,
                    "complete": complete,
                    "next_cursor": next_cursor,
                    "mode_used": "lexical",
                    "warnings": ["model_unavailable"] if request.mode == "hybrid_preferred" else [],
                }
            ),
        )


def read_record(
    engine: BrainEngine,
    request: RecordReadRequest,
    *,
    authority: EffectiveAuthority,
) -> RecordReadResponse:
    authority.require("content-read")
    with read_snapshot(engine) as connection:
        projected = RecordProjector(engine.profile, connection, authority).read(
            request.record_id, expected=request.expected_revision_id
        )
        body = projected.text.encode("utf-8")
        binding = binding_digest(
            {
                "record_id": request.record_id,
                "revision_id": request.expected_revision_id,
                "target_bytes": request.target_bytes,
                "projection": binding_digest(projected.text),
                "authority": authority_binding(authority),
            }
        )
        generation = generations(connection)
        now = engine._clock().timestamp()
        store = CursorStore(engine.profile)
        prior = continuation(
            store,
            request.cursor,
            purpose="record.read",
            binding=binding,
            generation=generation,
            now=now,
        )
        start = 0 if prior is None else prior.get("offset")
        if type(start) is not int or not 0 <= start <= len(body):
            raise T03Error("cursor_invalid")
        end = min(len(body), start + request.target_bytes)
        while end < len(body) and body[end] & 0xC0 == 0x80:
            end -= 1
        if end == start and start < len(body):
            end = start + 1
            while end < len(body) and body[end] & 0xC0 == 0x80:
                end += 1
        try:
            text = body[start:end].decode("utf-8")
        except UnicodeError:
            raise T03Error("cursor_invalid") from None
        complete = end == len(body)
        response = {
            "status": "ok",
            "dto_version": 1,
            "record": projected.summary,
            "content": {"kind": "untrusted_text", "text": text},
            "start_byte": start,
            "end_byte": end,
            "complete": complete,
            "next_cursor": None,
        }
        while len(portable_canonical_json_bytes(response)) > MAX_RESPONSE_BYTES:
            if end <= start:
                raise T03Error("response_too_large")
            proposed = start + (end - start) // 2
            while proposed > start and body[proposed] & 0xC0 == 0x80:
                proposed -= 1
            if proposed == start:
                raise T03Error("response_too_large")
            end = proposed
            text = body[start:end].decode("utf-8")
            complete = end == len(body)
            response["content"] = {"kind": "untrusted_text", "text": text}
            response["end_byte"] = end
            response["complete"] = complete
        if not complete:
            response["next_cursor"] = store.allocate(
                {
                    "purpose": "record.read",
                    "binding": binding,
                    "generation": generation,
                    "offset": end,
                },
                now=now,
            )
        return cast(RecordReadResponse, RecordReadResponse.from_wire(response))
