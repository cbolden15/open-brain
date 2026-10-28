"""Exact compact search over one authorized projection snapshot."""

from __future__ import annotations

import hmac
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import closing
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .compact_search_contracts import CompactSearchPageRequest, CompactSearchPageResponse
from .cursors import CursorStore, binding_digest
from .paging import (
    HYBRID_RANKING_VERSION,
    RANKING_VERSION,
    _semantic_current,
    _semantic_query,
    authority_binding,
    continuation,
    generations,
    ranked_candidates,
    read_snapshot,
)
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine


def _binding(
    request: CompactSearchPageRequest,
    filters: dict[str, list[str]],
    authority: EffectiveAuthority,
    *,
    hybrid: bool,
) -> str:
    return binding_digest(
        {
            "operation": "search.compact.page",
            "dto_version": 1,
            "query": request.query.strip(),
            "filters": filters,
            "mode": request.mode,
            "mode_used": "hybrid" if hybrid else "lexical",
            "group_by": request.group_by,
            "grouping_version": 1,
            "limit": request.limit,
            "target_bytes": request.target_bytes,
            "ranking": HYBRID_RANKING_VERSION if hybrid else RANKING_VERSION,
            "authority": authority_binding(authority),
        }
    )


def _title(title: str) -> tuple[str, bool]:
    encoded = title.encode("utf-8")
    if len(encoded) <= 256:
        return title, False
    return encoded[:253].decode("utf-8", errors="ignore") + "…", True


def compact_search_page(
    engine: BrainEngine,
    request: CompactSearchPageRequest,
    *,
    authority: EffectiveAuthority,
    maximum_response_bytes: int | None = None,
    encoded_size: Callable[[Mapping[str, object]], int] | None = None,
) -> CompactSearchPageResponse:
    authority.require("search")
    from .retrieval import _compile_query

    try:
        compiled = _compile_query(request.query)
    except ValueError:
        raise T03Error("invalid_arguments") from None
    filters = {key: sorted(value) for key, value in request.filters.items()}
    semantic = None if request.mode == "lexical" else _semantic_query(engine, request.query.strip())
    # The source connection is strictly read-only. This anonymous SQLite database
    # holds compact metadata, spills beyond a bounded cache, and dies with this call.
    with read_snapshot(engine) as connection, closing(sqlite3.connect("")) as eligible:
        eligible.row_factory = sqlite3.Row
        eligible.execute("PRAGMA cache_size=-2048")
        eligible.executescript("""
            CREATE TABLE members (
                ordinal INTEGER PRIMARY KEY, group_id TEXT NOT NULL,
                title TEXT NOT NULL, record_type TEXT NOT NULL, record_id TEXT NOT NULL,
                revision_id TEXT NOT NULL, payload_family TEXT NOT NULL, space_id TEXT,
                member_offset INTEGER NOT NULL,
                UNIQUE(record_type, record_id)
            );
            CREATE TABLE groups (
                group_id TEXT PRIMARY KEY, first_ordinal INTEGER NOT NULL,
                match_count INTEGER NOT NULL
            );
            CREATE INDEX member_groups ON members(group_id, ordinal);
        """)
        if semantic is not None and not _semantic_current(engine, connection, semantic.active):
            semantic = None
        hybrid = semantic is not None
        binding = _binding(request, filters, authority, hybrid=hybrid)
        stale = (
            (_binding(request, filters, authority, hybrid=not hybrid),)
            if request.mode in ("hybrid_preferred", "hybrid_required")
            else ()
        )
        # Install the existing authority-aware visibility functions, but bind only
        # explicit policy/root/custody epochs. Raw index generations and the
        # pre-projection digest include candidates the final projector withholds;
        # their mutations must not invalidate an unchanged visible compact set.
        current_generations = generations(connection, profile=engine.profile, authority=authority)
        generation = {
            name: current_generations[name]
            for name in (
                "incarnation",
                "authorization_epoch",
                "projection_policy_version",
                "fencing_epoch",
            )
        }
        if semantic is not None:
            generation = generation | {
                "embedding": {
                    "component_id": semantic.active.component_id,
                    "policy_generation": semantic.active.policy_generation,
                    "cache_generation_id": semantic.active.generation_id,
                }
            }
        store = CursorStore(engine.profile)
        now = engine._clock().timestamp()
        prior = continuation(
            store,
            request.cursor,
            purpose="search.compact.page",
            binding=binding,
            generation=generation,
            now=now,
            stale_bindings=stale,
        )
        if semantic is None and request.mode == "hybrid_required":
            raise T03Error("model_unavailable")
        seed = secrets.token_hex(32) if prior is None else prior.get("group_key")
        if not isinstance(seed, str) or re.fullmatch(r"[0-9a-f]{64}", seed) is None:
            raise T03Error("cursor_invalid")
        private_key = bytes.fromhex(seed)
        scope = {"binding": binding, "generation": generation}
        projected_set = sha256(b"open-brain-compact-eligible-v1\0")
        total = 0
        # Project every eligible candidate, even beyond the page boundary. Only
        # brief metadata enters the relation; bodies are released as iteration advances.
        for ordinal, (_score, summary) in enumerate(
            ranked_candidates(engine, connection, compiled, filters, authority, semantic)
        ):
            full_title = unicodedata.normalize("NFC", summary["title"])
            key = [
                full_title,
                summary["space_id"],
                summary["record_type"],
                summary["payload_family"],
            ]
            if request.group_by == "none" or not full_title:
                key.extend([summary["record_type"], summary["record_id"]])
            group_id = hmac.digest(
                private_key, portable_canonical_json_bytes({"scope": scope, "key": key}), "sha256"
            ).hex()
            inserted = eligible.execute(
                "INSERT OR IGNORE INTO members VALUES (?,?,?,?,?,?,?,?,0)",
                (
                    ordinal,
                    group_id,
                    summary["title"],
                    summary["record_type"],
                    summary["record_id"],
                    summary["revision_id"],
                    summary["payload_family"],
                    summary["space_id"],
                ),
            )
            if not inserted.rowcount:
                continue
            metadata = portable_canonical_json_bytes(
                [
                    group_id,
                    summary["title"],
                    summary["record_type"],
                    summary["record_id"],
                    summary["revision_id"],
                    summary["payload_family"],
                    summary["space_id"],
                ]
            )
            projected_set.update(len(metadata).to_bytes(8, "big"))
            projected_set.update(metadata)
            group = eligible.execute(
                "INSERT INTO groups VALUES (?,?,1) ON CONFLICT(group_id) DO UPDATE "
                "SET match_count=match_count+1 RETURNING match_count",
                (group_id, ordinal),
            ).fetchone()
            eligible.execute(
                "UPDATE members SET member_offset=? WHERE ordinal=?", (group[0] - 1, ordinal)
            )
            total += 1
        eligible_digest = projected_set.hexdigest()
        # Filesystem custody can withhold a projection without changing source
        # database generations. Bind the complete final eligible relation as well,
        # before an empty terminal response or any rows/counts can escape.
        if prior is not None and prior.get("eligible_digest") != eligible_digest:
            raise T03Error("cursor_stale")
        groups = eligible.execute("SELECT count(*) FROM groups").fetchone()[0]
        response: dict[str, Any] = {
            "status": "ok",
            "dto_version": 1,
            "view": "compact",
            "group_by": request.group_by,
            "count_only": request.count_only,
            "mode_used": "hybrid" if hybrid else "lexical",
            "count_scope": "hybrid_candidates" if hybrid else "lexical_matches",
            "total_matches": total,
            "total_groups": groups,
            "results": [],
            "complete": True,
            "next_cursor": None,
            "warnings": ["model_unavailable"]
            if request.mode == "hybrid_preferred" and not hybrid
            else [],
        }

        def fits(value: dict[str, Any]) -> bool:
            return len(portable_canonical_json_bytes(value)) <= request.target_bytes and (
                maximum_response_bytes is None
                or (
                    encoded_size(value)
                    if encoded_size is not None
                    else len(portable_canonical_json_bytes(value))
                )
                <= maximum_response_bytes
            )

        if request.count_only or not total:
            if not fits(response):
                raise T03Error("response_too_large")
            return CompactSearchPageResponse.from_wire(response)
        after = (-1, -1)
        if prior is not None:
            anchor = prior.get("last")
            if not isinstance(anchor, list) or len(anchor) != 4:
                raise T03Error("cursor_invalid")
            found = eligible.execute(
                "SELECT g.first_ordinal,m.ordinal FROM members m JOIN groups g USING(group_id) "
                "WHERE m.group_id=? AND m.record_type=? AND m.record_id=? AND m.revision_id=?",
                anchor,
            ).fetchone()
            if found is None:
                raise T03Error("cursor_stale")
            after = (found[0], found[1])
        remaining = eligible.execute(
            "SELECT m.*,g.match_count,best.title AS group_title "
            "FROM members m JOIN groups g USING(group_id) "
            "JOIN members best ON best.ordinal=g.first_ordinal "
            "WHERE (g.first_ordinal,m.ordinal) > (?,?) "
            "ORDER BY g.first_ordinal,m.ordinal",
            after,
        )
        current = remaining.fetchone()
        emitted = 0
        last: list[str] | None = None
        while current is not None and emitted < request.limit:
            following = remaining.fetchone()
            rows = [dict(row, members=list(row["members"])) for row in response["results"]]
            member = {"record_id": current["record_id"], "revision_id": current["revision_id"]}
            if rows and rows[-1]["group_id"] == current["group_id"]:
                rows[-1]["members"].append(member)
                rows[-1]["members_complete"] = (
                    current["member_offset"] + 1 == current["match_count"]
                )
            else:
                title, truncated = _title(current["group_title"])
                rows.append(
                    {
                        "group_id": current["group_id"],
                        "title": title,
                        "title_truncated": truncated,
                        "record_type": current["record_type"],
                        "payload_family": current["payload_family"],
                        "space_id": current["space_id"],
                        "match_count": current["match_count"],
                        "member_offset": current["member_offset"],
                        "members": [member],
                        "members_complete": current["member_offset"] + 1 == current["match_count"],
                    }
                )
            candidate = response | {
                "results": rows,
                "complete": following is None,
                # Cursor handles use exactly 98 unescaped ASCII bytes. Reserve
                # their real encoded cost before allocating any private state.
                "next_cursor": None if following is None else "A" * 98,
            }
            if not fits(candidate):
                break
            response = candidate
            emitted += 1
            last = [
                current["group_id"],
                current["record_type"],
                current["record_id"],
                current["revision_id"],
            ]
            current = following
        if not emitted:
            raise T03Error("response_too_large")
        if not response["complete"]:
            response["next_cursor"] = store.allocate(
                {
                    "purpose": "search.compact.page",
                    "binding": binding,
                    "generation": generation,
                    "eligible_digest": eligible_digest,
                    "group_key": seed,
                    "last": last,
                },
                now=now,
            )
        # An adapter's measurement is required to be deterministic for identical
        # UTF-8 framing. Recheck its actual handle before returning selected rows.
        if not fits(response):
            raise T03Error("response_too_large")
        return CompactSearchPageResponse.from_wire(response)
