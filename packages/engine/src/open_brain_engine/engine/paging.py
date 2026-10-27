"""Versioned lexical or hybrid keyset paging and complete projected record reads."""

from __future__ import annotations

import heapq
import math
import sqlite3
import struct
import unicodedata
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.portable.v4 import canonical_revision_id
from open_brain_engine.storage.filesystem import StorageError

from .consent_contracts import EgressMode
from .contracts import LocalEngineContext
from .cursors import CursorStore, binding_digest
from .embedding_backend import EmbeddingFailure
from .historical_read_snapshot import historical_read_snapshot
from .local_schema import PHASE1_STATE_SCHEMA_VERSION
from .mcp_eligibility import implicit, install
from .records import ProjectedRecord, RecordProjector
from .sharing import (
    EligibilityMode,
    external_candidate_clause,
    external_canonical_clause,
    sharing_eligible,
)
from .sharing_contracts import SharingError
from .t03_contracts import (
    EffectiveAuthority,
    RecordReadRequest,
    RecordReadResponse,
    SearchPageRequest,
    SearchPageResponse,
    T03Error,
)

if TYPE_CHECKING:
    from .embedding_cache import ActiveGeneration, ChunkRef
    from .local import BrainEngine

RANKING_VERSION = "fts5-match-tier-binary-v2"
# EMBEDDING-RESOURCE section 8 (2026-10-17 amendment) and decision D5: equal-weight
# reciprocal-rank fusion of max-cosine semantic ranks and lexical tiers 1-4 only.
HYBRID_RANKING_VERSION = "ob.hybrid-rrf.v2"
_RRF_K = 60
# Decision D13: each fusion input list is cut at 50, so a hybrid result set is at most 100.
_LIST_DEPTH = 50
# Candidates authorized per query, so the per-row clauses never run over the whole Brain.
_AUTHORIZE_BATCH = 128
# Leave transport framing and duplicate JSON string escaping room below 1 MiB.
MAX_RESPONSE_BYTES = 240_000


def authority_binding(authority: EffectiveAuthority) -> dict[str, Any]:
    return {
        "principal_id": authority.principal_id,
        "session_id": authority.session_id,
        "capabilities": sorted(authority.capabilities),
        "owner": authority.owner,
        "session_origin": authority.session_origin.value,
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
            if (
                connection.execute("PRAGMA user_version").fetchone()[0]
                != PHASE1_STATE_SCHEMA_VERSION
            ):
                raise T03Error("incompatible_runtime")
            from .capture_lifecycle import lifecycle_read_snapshot

            with (
                historical_read_snapshot(connection, engine.profile),
                lifecycle_read_snapshot(connection),
            ):
                yield connection
        except sqlite3.Error, StorageError, SharingError:
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
    install(connection, authority)
    readable_tiers = tuple(tier.value for tier in PrivacyTier if authority.permits_read_tier(tier))
    clauses: list[str] = ["mcp_record_visible(d.record_type,d.result_id)=1"]
    parameters: list[Any] = []
    if readable_tiers:
        clauses.append("d.effective_tier IN (" + ",".join("?" for _ in readable_tiers) + ")")
        parameters.extend(readable_tiers)
    else:
        clauses.append("0")
    if not authority.owner and authority.egress_mode is EgressMode.OWNER_LOCAL:
        clauses.append("scoped_projection_visible(d.record_type,d.result_id)=1")
    if authority.egress_mode is EgressMode.EXTERNAL_PROVIDER:
        clauses.append("d.effective_external_egress=1")
        clauses.append("sharing_visible(d.capture_id)=1")
        eligible, bindings = external_candidate_clause("d", authority)
        clauses.append(eligible)
        parameters.extend(bindings)
        members, member_bindings = external_canonical_clause("d", authority)
        clauses.append(members)
        parameters.extend(member_bindings)
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
    profile: LocalEngineContext,
    authority: EffectiveAuthority | None = None,
    visible_state: object | None = None,
) -> dict[str, Any]:
    install(connection, authority)
    row = connection.execute("SELECT * FROM engine_generations WHERE singleton=1").fetchone()
    if row is None:
        raise T03Error("operation_pending")
    if authority is None or (
        authority.owner
        and authority.egress_mode is EgressMode.OWNER_LOCAL
        and not implicit(authority)
    ):
        return dict(row)
    if authority.egress_mode is EgressMode.OWNER_LOCAL:
        from .historical_visibility import historical_capture_visibility

        local_projector = RecordProjector(profile, connection, authority)

        def historical_visible(capture_id: str) -> bool:
            return (
                historical_capture_visibility(
                    connection,
                    profile,
                    capture_id,
                    local_history=True,
                    provider_id=None,
                    brain_id=None,
                    issuer_epoch=None,
                )
                is not False
            )

        def scoped_projection_visible(record_type: object, record_id: object) -> int:
            if not isinstance(record_id, str):
                return 0
            try:
                if record_type == "source":
                    return int(historical_visible(record_id))
                elif record_type == "canonical":
                    publication = local_projector.current_publication(record_id)
                    members = connection.execute(
                        "SELECT capture_id FROM canonical_revision_members "
                        "WHERE page_id=? AND publication_id=? ORDER BY ordinal",
                        (record_id, publication),
                    ).fetchall()
                    return int(
                        bool(members) and all(historical_visible(item[0]) for item in members)
                    )
                else:
                    return 0
            except T03Error, StorageError, ValueError, TypeError, KeyError:
                return 0

        connection.create_function("scoped_projection_visible", 2, scoped_projection_visible)
    if authority.egress_mode is EgressMode.EXTERNAL_PROVIDER:
        projector = RecordProjector(profile, connection, authority)

        def current_publication(page_id: object) -> str | None:
            if not isinstance(page_id, str):
                return None
            try:
                publication_id = projector.current_publication(page_id)
                # Resolve and validate the exact same current projection before
                # ranking/digest construction. A malformed member set withheld
                # by the final projector must not affect visible cursor state.
                projector.canonical(page_id, expected=canonical_revision_id(publication_id))
                return publication_id
            except T03Error, StorageError, ValueError, TypeError, KeyError:
                # Damaged, ambiguous or inaccessible custody cannot become a candidate.
                return None

        connection.create_function("sharing_current_publication", 1, current_publication)
        connection.create_function(
            "sharing_visible",
            1,
            lambda capture_id: int(
                isinstance(capture_id, str)
                and sharing_eligible(
                    connection,
                    capture_id,
                    mode=EligibilityMode.EXTERNAL_READ,
                    provider_id=authority.provider_id,
                    brain_id=authority.brain_id,
                    issuer_epoch=authority.issuer_epoch,
                    profile=profile,
                )
            ),
        )
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
    stale_bindings: Collection[str] = (),
) -> dict[str, Any] | None:
    if cursor is None:
        return None
    record = store.resolve(cursor, now=now)
    if record.get("purpose") == purpose and record.get("binding") in stale_bindings:
        # The same request under the other ranking (hybrid vs. lexical fallback).
        raise T03Error("cursor_stale")
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


@dataclass(frozen=True, slots=True)
class _Semantic:
    active: ActiveGeneration
    query: tuple[float, ...]


def _semantic_query(engine: BrainEngine, query: str) -> _Semantic | None:
    """Embed the query outside every read snapshot and lease; ``None`` is unavailable.

    Detailed categories (consent, resource, cache, helper) stay owner-local; callers only
    see the existing ``model_unavailable``. The query text is never stored.
    """
    factory = engine._embedding_backend
    if factory is None:
        return None
    from .embedding_cache import ActiveGeneration, active_status

    status = active_status(engine)
    if not isinstance(status, ActiveGeneration):
        return None
    try:
        backend = factory()
        if backend.component_id != status.component_id:
            return None
        items = backend.embed("query", [unicodedata.normalize("NFC", query)])
    except EmbeddingFailure, ValueError, OSError:
        # Unencodable query text (``ValueError``) or an unusable resource/pipe (``OSError``)
        # is unavailability, never an internal error; anything else still propagates.
        return None
    if len(items) != 1 or len(items[0].chunks) != 1:
        return None
    return _Semantic(status, items[0].chunks[0].vector)


def _semantic_current(
    engine: BrainEngine, connection: sqlite3.Connection, active: ActiveGeneration
) -> bool:
    """Policy generation and active cache generation are unchanged inside the snapshot."""
    from .embedding_cache import active_pointer
    from .embedding_policy import read_policy

    policy = read_policy(connection)
    if (
        not policy.enabled
        or policy.component_id != active.component_id
        or policy.policy_generation != active.policy_generation
    ):
        return False
    return active_pointer(engine) == active.generation_id


def _search_binding(
    request: SearchPageRequest,
    filters: dict[str, list[str]],
    authority: EffectiveAuthority,
    *,
    hybrid: bool,
) -> str:
    return binding_digest(
        {
            "query": request.query.strip(),
            "filters": filters,
            "mode": request.mode,
            "mode_used": "hybrid" if hybrid else "lexical",
            "limit": request.limit,
            "ranking": HYBRID_RANKING_VERSION if hybrid else RANKING_VERSION,
            "authority": authority_binding(authority),
        }
    )


def _fused(
    tiered: list[tuple[int, str, str]],
    authorize: Callable[[list[tuple[str, str]]], dict[tuple[str, str], sqlite3.Row]],
    projector: RecordProjector,
    semantic: _Semantic,
) -> list[tuple[float, dict[str, Any], bool]]:
    """``ob.hybrid-rrf.v2`` over the top 50 of each input list (decision D13), best first.

    Semantic cosines come from the active generation alone. Candidates are then taken in
    descending score order, authorized for the caller in batches by the unchanged search
    clauses (``authorize``), and confirmed: a vector is scored only when the caller's own
    projected field bytes hash to exactly the owner-built chunk's projection hash (decision
    D10), until 50 records are confirmed or candidates run out. The lexical list is the
    first 50 authorized tier 1-4 rows (``tiered``, in lexical order). An unauthorized
    record never takes a rank. Authorization and projection therefore cover about 100
    records plus one batch per list, however large the Brain is.

    Coverage currency: a record created or edited after the last rebuild has no matching
    chunk, so it is simply absent from the semantic list and can still appear through
    lexical tiers 1-4; hybrid never claims semantic coverage for it.

    A stale candidate (its search row disagrees with its projection) takes no rank in
    either list. It is returned flagged at the position its ranks would have given it, so
    it fails only a page that reaches it, as lexical search does.
    """
    from .embedding_cache import field_texts, projection_sha256

    candidates: dict[tuple[str, str], sqlite3.Row] = {}
    checked: set[tuple[str, str]] = set()

    def admitted(order: list[tuple[str, str]], position: int) -> bool:
        """Authorize the next batch of ``order`` from ``position`` when first reached."""
        identity = order[position]
        if identity not in checked:
            batch = [
                item
                for item in order[position : position + _AUTHORIZE_BATCH]
                if item not in checked
            ]
            checked.update(batch)
            candidates.update(authorize(batch))
        return identity in candidates

    projections: dict[tuple[str, str], ProjectedRecord | None] = {}

    def project(identity: tuple[str, str]) -> ProjectedRecord | None:
        if identity not in projections:
            try:
                projections[identity] = projector.read(identity[1])
            except T03Error as error:
                if error.code != "not_found":
                    raise
                projections[identity] = None
        return projections[identity]

    summaries: dict[tuple[str, str], dict[str, Any]] = {}
    stale: dict[tuple[str, str], list[int]] = {}

    def confirmed(identity: tuple[str, str], projected: ProjectedRecord, rank: int) -> bool:
        row = candidates[identity]
        summary = projected.summary
        summaries[identity] = summary
        indexed_text = projected.text if projected.indexed_text is None else projected.indexed_text
        if row["body"] != indexed_text or row["title"] != summary["title"]:
            stale.setdefault(identity, []).append(rank)
            return False
        return True

    # An upper bound per record from every one of its chunks, before any projection.
    scored: dict[tuple[str, str], list[tuple[ChunkRef, float]]] = {}
    for ref, vectors in semantic.active.chunks.items():
        identity = (ref.record_type, ref.result_id)
        if vectors:
            scored.setdefault(identity, []).append(
                (ref, max(math.sumprod(vector, semantic.query) for vector in vectors))
            )
    pending = [
        (-max(score for _ref, score in refs), identity, False) for identity, refs in scored.items()
    ]
    heapq.heapify(pending)
    # Unchecked records leave the heap in exactly this bound order.
    order = [identity for _bound, identity, _exact in sorted(pending)]
    positions = {identity: position for position, identity in enumerate(order)}
    semantic_list: list[tuple[str, str]] = []
    while pending and len(semantic_list) < _LIST_DEPTH:
        bound, identity, exact = heapq.heappop(pending)
        if not admitted(order, positions[identity]):
            continue
        projected = project(identity)
        if projected is None:
            continue
        if not exact:
            revision = projected.summary["revision_id"]
            hashes = {
                name: projection_sha256(text) for name, text in field_texts(projected).items()
            }
            matched = [
                score
                for ref, score in scored[identity]
                if ref.revision_id == revision and ref.projection_sha256 == hashes[ref.field]
            ]
            if not matched:
                continue
            if max(matched) < -bound:
                # Reorder by the caller-visible score before confirming its rank.
                heapq.heappush(pending, (-max(matched), identity, True))
                continue
        if confirmed(identity, projected, len(semantic_list) + 1):
            semantic_list.append(identity)
    lexical_order = [(record_type, result_id) for _tier, record_type, result_id in tiered]
    lexical_list: list[tuple[str, str]] = []
    for position, identity in enumerate(lexical_order):
        if len(lexical_list) == _LIST_DEPTH:
            break
        if not admitted(lexical_order, position):
            continue
        projected = project(identity)
        if projected is not None and confirmed(identity, projected, len(lexical_list) + 1):
            lexical_list.append(identity)
    fused: dict[tuple[str, str], float] = {}
    for ranking in (semantic_list, lexical_list):
        for rank, identity in enumerate(ranking, start=1):
            fused[identity] = fused.get(identity, 0.0) + 1.0 / (_RRF_K + rank)
    entries = [(score, summaries[identity], False) for identity, score in fused.items()]
    entries.extend(
        (sum(1.0 / (_RRF_K + rank) for rank in ranks), summaries[identity], True)
        for identity, ranks in stale.items()
    )
    return sorted(
        entries, key=lambda item: (-item[0], item[1]["record_type"], item[1]["record_id"])
    )


def _reached(
    entries: list[tuple[float, dict[str, Any], bool]],
) -> Iterator[tuple[float, dict[str, Any]]]:
    """Fused entries in page order; a stale entry fails the page that reaches it."""
    for score, summary, stale in entries:
        if stale:
            raise T03Error("projection_stale")
        yield score, summary


def search_page(
    engine: BrainEngine,
    request: SearchPageRequest,
    *,
    authority: EffectiveAuthority,
) -> SearchPageResponse:
    authority.require("search")
    from .retrieval import _compile_query

    try:
        compiled = _compile_query(request.query)
    except ValueError:
        raise T03Error("invalid_arguments") from None
    filters = {key: sorted(value) for key, value in request.filters.items()}
    # ``lexical`` never reaches the backend factory, the cache or the resource.
    semantic = None if request.mode == "lexical" else _semantic_query(engine, request.query.strip())
    if semantic is None and request.mode == "hybrid_required":
        raise T03Error("model_unavailable")
    with read_snapshot(engine) as connection:
        if semantic is not None and not _semantic_current(engine, connection, semantic.active):
            # currentness_changed between the query exchange and this snapshot.
            if request.mode == "hybrid_required":
                raise T03Error("model_unavailable")
            semantic = None
        hybrid = semantic is not None
        binding = _search_binding(request, filters, authority, hybrid=hybrid)
        stale = (
            (_search_binding(request, filters, authority, hybrid=not hybrid),)
            if request.mode == "hybrid_preferred"
            else ()
        )
        generation = generations(connection, profile=engine.profile, authority=authority)
        if semantic is not None:
            generation = generation | {
                "embedding": {
                    "component_id": semantic.active.component_id,
                    "policy_generation": semantic.active.policy_generation,
                    "cache_generation_id": semantic.active.generation_id,
                }
            }
        now = engine._clock().timestamp()
        store = CursorStore(engine.profile)
        prior = continuation(
            store,
            request.cursor,
            purpose="search.page",
            binding=binding,
            generation=generation,
            now=now,
            stale_bindings=stale,
        )
        last = _last_key(prior)
        parameters: list[Any] = []
        clauses = [
            "mcp_record_visible(d.record_type,d.result_id)=1",
            "(d.record_type='canonical' OR EXISTS (SELECT 1 FROM logical_sources s "
            "WHERE s.head_capture_id=d.result_id AND s.historical_only=0 "
            "AND s.lifecycle='active' AND s.availability='available'))",
        ]
        readable_tiers = tuple(
            tier.value for tier in PrivacyTier if authority.permits_read_tier(tier)
        )
        if readable_tiers:
            clauses.append("d.effective_tier IN (" + ",".join("?" for _ in readable_tiers) + ")")
            parameters.extend(readable_tiers)
        else:
            clauses.append("0")
        if not authority.owner and authority.egress_mode is EgressMode.OWNER_LOCAL:
            clauses.append("scoped_projection_visible(d.record_type,d.result_id)=1")
        if authority.egress_mode is EgressMode.EXTERNAL_PROVIDER:
            clauses.append("d.effective_external_egress=1")
            clauses.append("sharing_visible(d.capture_id)=1")
            eligible, bindings = external_candidate_clause("d", authority)
            clauses.append(eligible)
            parameters.extend(bindings)
            members, member_bindings = external_canonical_clause("d", authority)
            clauses.append(members)
            parameters.extend(member_bindings)
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
        authorization = list(parameters)
        parameters.extend(
            (
                compiled.title_phrase,
                compiled.title_all,
                compiled.phrase_any,
                compiled.all_any,
            )
        )
        tiers = f"""
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
            )"""
        projector = RecordProjector(engine.profile, connection, authority)
        if semantic is None:
            parameters.append(compiled.disjunction)
            seek = ""
            if last is not None:
                seek = "WHERE (score,record_type COLLATE BINARY,result_id COLLATE BINARY) > (?,?,?)"
                parameters.extend(last)
            sql = f"""{tiers}, hits AS MATERIALIZED (
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
            ranked = _lexical_ranked(connection.execute(sql, parameters), projector)
        else:
            # Tier 1-4 candidates only (D5), unauthorized here and bounded by the FTS hits.
            tier_sql = """
                WITH title_phrase AS MATERIALIZED (
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
                )
                SELECT * FROM (
                    SELECT CASE
                        WHEN tp.fts_rowid IS NOT NULL THEN 1
                        WHEN ta.fts_rowid IS NOT NULL THEN 2
                        WHEN pa.fts_rowid IS NOT NULL THEN 3
                        WHEN aa.fts_rowid IS NOT NULL THEN 4
                    END AS tier,d.record_type,d.result_id
                    FROM search_documents d JOIN search_fts_identity i USING(result_id)
                    JOIN search_documents_fts ON
                      search_documents_fts.rowid=i.fts_rowid AND
                      search_documents_fts.result_id=d.result_id
                    LEFT JOIN title_phrase tp ON tp.fts_rowid=i.fts_rowid
                    LEFT JOIN title_all ta ON ta.fts_rowid=i.fts_rowid
                    LEFT JOIN phrase_any pa ON pa.fts_rowid=i.fts_rowid
                    LEFT JOIN all_any aa ON aa.fts_rowid=i.fts_rowid
                ) WHERE tier IS NOT NULL
                ORDER BY tier,record_type COLLATE BINARY,result_id COLLATE BINARY
            """
            tiered = [
                (int(row["tier"]), row["record_type"], row["result_id"])
                for row in connection.execute(tier_sql, parameters[len(authorization) :])
            ]

            def authorize(
                identities: list[tuple[str, str]],
            ) -> dict[tuple[str, str], sqlite3.Row]:
                """The unchanged authorized clauses, evaluated only over ``identities``."""
                if not identities:
                    return {}
                wanted = set(identities)
                rows = connection.execute(
                    f"""SELECT d.* FROM search_documents d
                    JOIN search_fts_identity i USING(result_id)
                    JOIN search_documents_fts ON
                      search_documents_fts.rowid=i.fts_rowid AND
                      search_documents_fts.result_id=d.result_id
                    WHERE d.result_id IN ({",".join("?" for _ in identities)})
                    AND {" AND ".join(clauses)}""",
                    [result_id for _record_type, result_id in identities] + authorization,
                )
                return {
                    (row["record_type"], row["result_id"]): row
                    for row in rows
                    if (row["record_type"], row["result_id"]) in wanted
                }

            fused = _fused(tiered, authorize, projector, semantic)
            if last is not None:
                after = (-last[0], last[1], last[2])
                fused = [
                    item
                    for item in fused
                    if (-item[0], item[1]["record_type"], item[1]["record_id"]) > after
                ]
            ranked = _reached(fused)
        results: list[dict[str, Any]] = []
        next_key: list[Any] | None = None
        complete = True
        # Result and byte limits apply only after fusion and record collapse.
        for score, summary in ranked:
            exact = _score_hex(score)
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
                    "mode_used": "hybrid" if hybrid else "lexical",
                    "warnings": ["model_unavailable"]
                    if request.mode == "hybrid_preferred" and not hybrid
                    else [],
                }
            ),
        )


def _lexical_ranked(
    rows: Iterator[sqlite3.Row], projector: RecordProjector
) -> Iterator[tuple[float, dict[str, Any]]]:
    """Lexical rows projected lazily, so paging stops projecting at the page boundary."""
    for row in rows:
        score = float(row["score"])
        try:
            projected = projector.read(row["result_id"])
        except T03Error as error:
            if error.code == "not_found":
                continue
            raise
        summary = projected.summary
        indexed_text = projected.text if projected.indexed_text is None else projected.indexed_text
        if row["body"] != indexed_text or row["title"] != summary["title"]:
            raise T03Error("projection_stale")
        yield score, summary


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
            profile=engine.profile,
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
            "projection_applied": projected.projection_applied,
        }
        from .slack_sharing import read_selection

        if projected.summary["record_type"] == "source":
            selection = read_selection(
                connection,
                engine.profile,
                projected.summary["record_id"],
                authority,
                history=history,
            )
            if selection is not None:
                response["selection"] = selection
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
