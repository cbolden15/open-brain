"""Versioned lexical keyset paging and complete projected record reads."""

from __future__ import annotations

import math
import sqlite3
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier
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

RANKING_VERSION = "fts5-match-tier-binary-v2"
# Leave transport framing and duplicate JSON string escaping room below 1 MiB.
MAX_RESPONSE_BYTES = 240_000


def authority_binding(authority: EffectiveAuthority) -> dict[str, Any]:
    return {
        "principal_id": authority.principal_id,
        "session_id": authority.session_id,
        "capabilities": sorted(authority.capabilities),
        "owner": authority.owner,
        "space_ids": None if authority.space_ids is None else sorted(authority.space_ids),
        "allowed_read_tiers": sorted(tier.value for tier in authority.allowed_read_tiers),
        "allowed_capture_tiers": sorted(tier.value for tier in authority.allowed_capture_tiers),
        "egress_mode": authority.egress_mode.value,
        "provider_id": authority.provider_id,
        "consent_id": authority.consent_id,
        "authorization_generation": authority.authorization_generation,
        "brain_id": authority.brain_id,
        "issuer_epoch": authority.issuer_epoch,
    }


@contextmanager
def read_snapshot(engine: BrainEngine) -> Iterator[sqlite3.Connection]:
    engine._assert_root()
    with engine._reader_lease.acquire_shared_reader():
        CursorStore(engine.profile).verify_custody()
        connection = engine._store.connect()
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 9:
                raise T03Error("incompatible_runtime")
            yield connection
        except sqlite3.Error, StorageError:
            raise T03Error("operation_pending") from None
        finally:
            connection.rollback()
            connection.close()


def _authorized_retrieval_digest(
    connection: sqlite3.Connection,
    authority: EffectiveAuthority,
    *,
    visible_state: object | None,
) -> str:
    readable_tiers = tuple(
        tier.value for tier in PrivacyTier if authority.permits_read_tier(tier)
    )
    clauses: list[str] = []
    parameters: list[Any] = []
    if readable_tiers:
        clauses.append(
            "d.effective_tier IN (" + ",".join("?" for _ in readable_tiers) + ")"
        )
        parameters.extend(readable_tiers)
    else:
        clauses.append("0")
    if authority.space_ids is not None:
        spaces = sorted(authority.space_ids)
        if spaces:
            clauses.append("d.space_id IN (" + ",".join("?" for _ in spaces) + ")")
            parameters.extend(spaces)
        else:
            clauses.append("0")
    rows = connection.execute(
        f"""
        WITH authorized AS MATERIALIZED (
            SELECT d.* FROM search_documents d WHERE {" AND ".join(clauses)}
        )
        SELECT a.*,
            s.source_id AS logical_source_id,
            s.head_capture_id AS logical_head_capture_id,
            s.historical_only AS logical_historical_only,
            s.space_id AS logical_space_id,
            s.route_version AS logical_route_version,
            s.head_version AS logical_head_version,
            s.lifecycle AS logical_lifecycle,
            s.availability AS logical_availability
        FROM authorized a
        LEFT JOIN logical_sources s
          ON a.record_type='source' AND s.head_capture_id=a.result_id
        ORDER BY a.result_id COLLATE BINARY
        """,
        parameters,
    )
    digest = sha256(b"open-brain-authorization-visible-generation-v1\0")

    def update(value: object) -> None:
        encoded = portable_canonical_json_bytes(value)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    for row in rows:
        update({"kind": "search_document", "value": dict(row)})
    if visible_state is not None:
        update({"kind": "operation_state", "value": visible_state})
    return digest.hexdigest()


def generations(
    connection: sqlite3.Connection,
    *,
    authority: EffectiveAuthority | None = None,
    visible_state: object | None = None,
) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM engine_generations WHERE singleton=1").fetchone()
    if row is None:
        raise T03Error("operation_pending")
    if authority is None or authority.owner:
        return dict(row)
    return {
        "incarnation": row["incarnation"],
        "authorization_epoch": row["authorization_epoch"],
        "projection_policy_version": row["projection_policy_version"],
        "fencing_epoch": row["fencing_epoch"],
        "visible_retrieval_digest": _authorized_retrieval_digest(
            connection, authority, visible_state=visible_state
        ),
    }


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
        generation = generations(connection, authority=authority)
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
        readable_tiers = tuple(
            tier.value for tier in PrivacyTier if authority.permits_read_tier(tier)
        )
        if readable_tiers:
            clauses.append(
                "d.effective_tier IN (" + ",".join("?" for _ in readable_tiers) + ")"
            )
            parameters.extend(readable_tiers)
        else:
            clauses.append("0")
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
        parameters.extend(
            (
                compiled.title_phrase,
                compiled.title_all,
                compiled.phrase_any,
                compiled.all_any,
                compiled.disjunction,
            )
        )
        seek = ""
        if last is not None:
            seek = "WHERE (score,record_type COLLATE BINARY,result_id COLLATE BINARY) > (?,?,?)"
            parameters.extend(last)
        sql = f"""
            WITH authorized AS MATERIALIZED (
                SELECT d.*,i.fts_rowid FROM search_documents d
                JOIN search_fts_identity i USING(result_id) WHERE {" AND ".join(clauses)}
            ), title_phrase AS MATERIALIZED (
                SELECT rowid AS fts_rowid FROM search_documents_fts
                WHERE search_documents_fts MATCH ?
            ), title_all AS MATERIALIZED (
                SELECT rowid AS fts_rowid FROM search_documents_fts
                WHERE search_documents_fts MATCH ?
            ), phrase_any AS MATERIALIZED (
                SELECT rowid AS fts_rowid FROM search_documents_fts
                WHERE search_documents_fts MATCH ?
            ), all_any AS MATERIALIZED (
                SELECT rowid AS fts_rowid FROM search_documents_fts
                WHERE search_documents_fts MATCH ?
            ), hits AS MATERIALIZED (
                SELECT a.*,CAST(CASE
                    WHEN tp.fts_rowid IS NOT NULL THEN 1
                    WHEN ta.fts_rowid IS NOT NULL THEN 2
                    WHEN pa.fts_rowid IS NOT NULL THEN 3
                    WHEN aa.fts_rowid IS NOT NULL THEN 4
                    ELSE 5
                END AS REAL) AS score
                FROM authorized a JOIN search_documents_fts ON
                  search_documents_fts.rowid=a.fts_rowid AND
                  search_documents_fts.result_id=a.result_id
                LEFT JOIN title_phrase tp USING(fts_rowid)
                LEFT JOIN title_all ta USING(fts_rowid)
                LEFT JOIN phrase_any pa USING(fts_rowid)
                LEFT JOIN all_any aa USING(fts_rowid)
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
            indexed_text = (
                projected.text if projected.indexed_text is None else projected.indexed_text
            )
            if row["body"] != indexed_text or row["title"] != summary["title"]:
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
    history: bool = False,
) -> RecordReadResponse:
    authority.require("history-read" if history else "content-read")
    purpose = "history.show" if history else "record.read"
    with read_snapshot(engine) as connection:
        projected = RecordProjector(engine.profile, connection, authority).read(
            request.record_id, expected=request.expected_revision_id, history=history
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
        generation = generations(
            connection,
            authority=authority,
            visible_state={
                "record": projected.summary,
                "content_sha256": sha256(body).hexdigest(),
            },
        )
        now = engine._clock().timestamp()
        store = CursorStore(engine.profile)
        prior = continuation(
            store,
            request.cursor,
            purpose=purpose,
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
                    "purpose": purpose,
                    "binding": binding,
                    "generation": generation,
                    "offset": end,
                },
                now=now,
            )
        return cast(RecordReadResponse, RecordReadResponse.from_wire(response))
