"""Synthetic exact-count, grouping, cursor and byte-bound compact search checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, DecisionOutcome, ProposalDraft, TextPayload
from open_brain_engine.engine.compact_search_contracts import (
    CompactSearchPageRequest,
    CompactSearchPageResponse,
)
from open_brain_engine.engine.cursors import CursorStore
from open_brain_engine.engine.embedding_backend import (
    EmbeddedItem,
    EmbeddingChunk,
    EmbeddingFailure,
    Purpose,
)
from open_brain_engine.engine.records import RecordProjector
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    SearchPageRequest,
    SourceRouteRequest,
    T03Error,
)

from open_brain.profile import compile_single_user_local


@pytest.fixture
def engine(tmp_path: Path) -> BrainEngine:
    tmp_path.chmod(0o700)
    return BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))


def _owner() -> EffectiveAuthority:
    return EffectiveAuthority("synthetic", "compact-tests", frozenset({"search"}), None, owner=True)


def _capture(engine: BrainEngine, index: int, title: str = "needle group") -> str:
    return engine.capture.accept(
        TextPayload(title + "\nneedle synthetic body " + str(index)),
        delivery_id=f"compact.synthetic.{index}",
        privacy_tier=PrivacyTier.WORK,
    ).capture_id


def _page(engine: BrainEngine, **kwargs: Any) -> dict[str, Any]:
    return engine.retrieval.compact_search_page(
        CompactSearchPageRequest(query="needle", **kwargs), authority=_owner()
    ).to_wire()


def _ids(page: Mapping[str, Any]) -> list[str]:
    return [member["record_id"] for row in page["results"] for member in row["members"]]


@pytest.mark.parametrize(
    "changes",
    [
        {"dto_version": True},
        {"dto_version": 1.0},
        {"dto_version": 2},
        {"limit": True},
        {"limit": 2.0},
        {"limit": 0},
        {"limit": 101},
        {"target_bytes": 2047},
        {"target_bytes": 8193},
        {"target_bytes": True},
        {"count_only": 1},
        {"count_only": True, "cursor": "opaque"},
        {"group_by": "TITLE"},
        {"owner": True},
        {"query": "\ud800"},
        {"query": "\0needle"},
        {"query": "?"},
        {"filters": {}},
    ],
)
def test_request_is_closed(changes: dict[str, object]) -> None:
    with pytest.raises(T03Error, match="invalid_arguments"):
        CompactSearchPageRequest.from_wire({"dto_version": 1, "query": "needle", **changes})


def test_defaults_freeze_inputs_and_require_wire_version() -> None:
    with pytest.raises(T03Error, match="invalid_arguments"):
        CompactSearchPageRequest.from_wire({"query": "needle"})
    arguments: dict[str, Any] = {
        "dto_version": 1,
        "query": "needle",
        "filters": {"space_ids": [], "payload_families": ["text"], "record_types": []},
    }
    request = CompactSearchPageRequest.from_wire(arguments)
    arguments["filters"]["payload_families"].clear()
    assert request.limit == 20 and request.target_bytes == 4096
    assert request.group_by == "title" and request.mode == "lexical"
    assert request.to_wire()["filters"]["payload_families"] == ["text"]


def test_group_fragments_are_contiguous_and_replayable(engine: BrainEngine) -> None:
    captures = {_capture(engine, i) for i in range(7)}
    _capture(engine, 10, "needle separate")
    first = _page(engine, limit=2)
    page = first
    seen: list[str] = []
    fragments: dict[str, list[dict[str, Any]]] = {}
    for _ in range(10):
        assert page["total_matches"] == 8 and page["total_groups"] == 2
        assert len(_ids(page)) <= 2
        seen.extend(_ids(page))
        for row in page["results"]:
            fragments.setdefault(row["group_id"], []).append(row)
        if page["complete"]:
            break
        cursor = page["next_cursor"]
        page = _page(engine, limit=2, cursor=cursor)
        replay = _page(engine, limit=2, cursor=cursor)
        assert page["results"] == replay["results"]
        assert page["total_matches"] == replay["total_matches"]
    assert len(seen) == len(set(seen)) == 8
    assert captures <= set(seen)
    dominant = next(rows for rows in fragments.values() if rows[0]["match_count"] == 7)
    offset = 0
    for row in dominant:
        assert row["member_offset"] == offset
        offset += len(row["members"])
        assert row["members_complete"] == (offset == 7)
    assert offset == 7
    assert all(
        set(member) == {"record_id", "revision_id"} for row in dominant for member in row["members"]
    )


def test_byte_budget_fragments_long_titles_and_transport_reflow(engine: BrainEngine) -> None:
    for index in range(12):
        _capture(engine, index, "needle " + "🧠" * 150)
    page = _page(engine, limit=100, target_bytes=2048)
    assert not page["complete"] and 0 < len(_ids(page)) < 12
    assert len(portable_canonical_json_bytes(page)) <= 2048
    assert len(page["results"][0]["title"].encode()) <= 256
    assert page["results"][0]["title"].endswith("…")
    assert page["results"][0]["title_truncated"] is True

    def measured(value: Mapping[str, object]) -> int:
        return 200 + 2 * len(portable_canonical_json_bytes(value))

    request = CompactSearchPageRequest(query="needle", limit=100, target_bytes=8192)
    small = engine.retrieval.compact_search_page(
        request,
        authority=_owner(),
        maximum_response_bytes=2600,
        encoded_size=measured,
    ).to_wire()
    assert measured(small) <= 2600 and 0 < len(_ids(small)) < len(_ids(page))
    following = engine.retrieval.compact_search_page(
        replace(request, cursor=small["next_cursor"]),
        authority=_owner(),
        maximum_response_bytes=2600,
        encoded_size=measured,
    ).to_wire()
    assert not set(_ids(small)) & set(_ids(following))
    assert following["results"][0]["member_offset"] == len(_ids(small))
    with pytest.raises(T03Error, match="response_too_large"):
        engine.retrieval.compact_search_page(
            request,
            authority=_owner(),
            maximum_response_bytes=1,
            encoded_size=measured,
        )


def test_exact_fit_empty_count_only_and_no_state_allocation(
    engine: BrainEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = _page(engine)
    assert empty["total_matches"] == empty["total_groups"] == 0
    assert empty["complete"] and empty["next_cursor"] is None
    for index in range(2):
        _capture(engine, index)

    def forbidden(*args: object, **kwargs: object) -> str:
        pytest.fail("terminal and count-only calls must not allocate cursors")

    monkeypatch.setattr(CursorStore, "allocate", forbidden)
    exact = _page(engine, limit=2)
    assert exact["complete"] and exact["next_cursor"] is None
    count = _page(engine, count_only=True, limit=1)
    assert count["total_matches"] == 2 and count["total_groups"] == 1
    assert count["results"] == [] and count["complete"] and count["next_cursor"] is None


def test_none_preserves_full_flat_order(engine: BrainEngine) -> None:
    for index in range(4):
        _capture(engine, index, "needle same" if index % 2 else "other")
    compact = _page(engine, group_by="none", limit=100, target_bytes=8192)
    full = engine.retrieval.search_page(
        SearchPageRequest(query="needle", limit=100), authority=_owner()
    ).to_wire()
    assert _ids(compact) == [
        row["record_id"] for row in cast(list[dict[str, Any]], full["results"])
    ]
    assert compact["total_groups"] == compact["total_matches"] == 4
    assert all(
        row["match_count"] == 1 and row["member_offset"] == 0 and row["members_complete"]
        for row in compact["results"]
    )


def test_nfc_case_full_title_and_partition_grouping(engine: BrainEngine) -> None:
    titles = [
        "needle café",
        "needle cafe\u0301",
        "needle CAFÉ",
        "needle " + "🧠" * 100 + "a",
        "needle " + "🧠" * 100 + "b",
    ]
    for index, title in enumerate(titles):
        _capture(engine, index, title)
    same = _page(engine, limit=100, target_bytes=8192)
    assert same["total_matches"] == 5 and same["total_groups"] == 4
    assert sorted(row["match_count"] for row in same["results"]) == [1, 1, 1, 2]
    shortened = [row for row in same["results"] if row["title_truncated"]]
    assert len(shortened) == 2 and shortened[0]["title"] == shortened[1]["title"]
    assert shortened[0]["group_id"] != shortened[1]["group_id"]

    space = engine.inbox.create_space("Synthetic", delivery_id="compact.space").space_id
    engine.capture.accept(
        TextPayload("needle café\nneedle"),
        delivery_id="compact.spaced",
        space_id=space,
        privacy_tier=PrivacyTier.WORK,
    )
    capture = engine.capture.accept(
        TextPayload("needle café\nneedle"),
        delivery_id="compact.canonical",
        space_id=space,
        privacy_tier=PrivacyTier.WORK,
    )
    proposal = engine.review.propose(
        (capture.capture_id,),
        (ProposalDraft("needle café", "needle synthetic page"),),
        delivery_id="compact.proposal",
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="compact.approve",
        expected_review_digest=proposal.review_digest,
    )
    partitioned = _page(engine, count_only=True)
    assert partitioned["total_groups"] == 6


@pytest.mark.parametrize(
    "changes",
    [
        {"query": "different"},
        {"group_by": "none"},
        {"limit": 2},
        {"target_bytes": 2048},
        {"mode": "hybrid_preferred"},
        {"filters": {"space_ids": [], "record_types": ["source"], "payload_families": []}},
    ],
)
def test_cursor_binds_arguments(engine: BrainEngine, changes: dict[str, Any]) -> None:
    for index in range(2):
        _capture(engine, index)
    request = CompactSearchPageRequest(query="needle", limit=1)
    first = engine.retrieval.compact_search_page(request, authority=_owner()).to_wire()
    with pytest.raises(T03Error, match="cursor_invalid"):
        engine.retrieval.compact_search_page(
            replace(request, cursor=first["next_cursor"], **changes), authority=_owner()
        )


def test_cursor_authority_visible_generation_rotation_and_purpose(engine: BrainEngine) -> None:
    for index in range(3):
        _capture(engine, index)
    first = _page(engine, limit=1)
    request = CompactSearchPageRequest(query="needle", limit=1, cursor=first["next_cursor"])
    with pytest.raises(T03Error, match="cursor_invalid"):
        engine.retrieval.compact_search_page(
            request, authority=replace(_owner(), session_id="other-session")
        )
    with pytest.raises(T03Error, match="cursor_invalid"):
        engine.retrieval.search_page(
            SearchPageRequest(query="needle", limit=1, cursor=request.cursor), authority=_owner()
        )
    _capture(engine, 4)
    with pytest.raises(T03Error, match="cursor_stale"):
        engine.retrieval.compact_search_page(request, authority=_owner())
    latest = _page(engine, limit=1)
    engine.retrieval.rotate_cursors(authority=_owner())
    with pytest.raises(T03Error, match="cursor_stale"):
        _page(engine, limit=1, cursor=latest["next_cursor"])


@pytest.mark.parametrize("lose_all", [False, True])
@pytest.mark.parametrize("tamper", [False, True])
def test_cursor_stales_when_final_projection_disappears_without_database_mutation(
    engine: BrainEngine,
    lose_all: bool,
    tamper: bool,
) -> None:
    from open_brain_engine.engine.paging import generations, read_snapshot

    for index in range(3):
        _capture(engine, index)
    first = _page(engine, limit=1)
    anchor = _ids(first)[0]
    with read_snapshot(engine) as connection:
        before = generations(connection, profile=engine.profile, authority=_owner())
        paths = [
            row["source_path"]
            for row in connection.execute(
                "SELECT source_path FROM source_revisions WHERE capture_id!=? OR ?",
                (anchor, lose_all),
            )
        ]
    # Remove only fixture custody; source DB/index/generation bytes stay unchanged.
    for relative in paths if lose_all else paths[:1]:
        path = engine.profile.root / relative
        if tamper:
            path.write_bytes(b"synthetic damaged source")
        else:
            path.unlink()
    with read_snapshot(engine) as connection:
        assert generations(connection, profile=engine.profile, authority=_owner()) == before
    with pytest.raises(T03Error, match="cursor_stale"):
        _page(engine, limit=1, cursor=first["next_cursor"])
    fresh = _page(engine, count_only=True)
    assert fresh["total_matches"] == (0 if lose_all else 2)


def test_hidden_changes_do_not_affect_scoped_counts_order_or_cursor(engine: BrainEngine) -> None:
    for index in range(3):
        _capture(engine, index)
    reader = replace(_owner(), owner=False, allowed_read_tiers=frozenset({PrivacyTier.WORK}))
    request = CompactSearchPageRequest(query="needle", limit=1)
    first = engine.retrieval.compact_search_page(request, authority=reader).to_wire()
    engine.capture.accept(
        TextPayload("needle group\nneedle hidden"),
        delivery_id="compact.hidden",
        privacy_tier=PrivacyTier.PERSONAL,
    )
    next_page = engine.retrieval.compact_search_page(
        replace(request, cursor=first["next_cursor"]), authority=reader
    ).to_wire()
    assert next_page["total_matches"] == 3 and next_page["total_groups"] == 1
    assert next_page["results"][0]["group_id"] == first["results"][0]["group_id"]
    assert not set(_ids(first)) & set(_ids(next_page))


@pytest.mark.parametrize("tamper", [False, True])
@pytest.mark.parametrize("mutation", ["route", "index_title"])
def test_excluded_projection_mutation_preserves_scoped_cursor_and_replay(
    engine: BrainEngine,
    tamper: bool,
    mutation: str,
) -> None:
    from open_brain_engine.engine.paging import read_snapshot

    ids = [_capture(engine, index) for index in range(3)]
    space = engine.inbox.create_space("Synthetic target", delivery_id="compact.target").space_id
    inaccessible = ids[-1]
    with read_snapshot(engine) as connection:
        source = connection.execute(
            "SELECT r.source_path,s.source_id,s.route_version FROM source_revisions r "
            "JOIN logical_sources s USING(source_id) WHERE r.capture_id=?",
            (inaccessible,),
        ).fetchone()
    path = engine.profile.root / source["source_path"]
    if tamper:
        path.write_bytes(b"synthetic damaged source")
    else:
        path.unlink()
    reader = replace(_owner(), owner=False, allowed_read_tiers=frozenset({PrivacyTier.WORK}))
    request = CompactSearchPageRequest(query="needle", limit=1)
    first = engine.retrieval.compact_search_page(request, authority=reader).to_wire()
    continued = replace(request, cursor=first["next_cursor"])
    before = engine.retrieval.compact_search_page(continued, authority=reader).to_wire()
    assert first["total_matches"] == before["total_matches"] == 2
    if mutation == "route":
        engine.sources.route(
            SourceRouteRequest(
                source_id=source["source_id"],
                space_id=space,
                expected_head=inaccessible,
                expected_route_version=source["route_version"],
                operation_id="operation_" + str(uuid4()),
            ),
            authority=_owner(),
        )
    else:
        with engine._store.transaction() as connection:
            connection.execute(
                "UPDATE search_documents SET title='needle hidden changed' WHERE result_id=?",
                (inaccessible,),
            )
    fresh = engine.retrieval.compact_search_page(
        replace(request, count_only=True),
        authority=reader,
    ).to_wire()
    assert fresh["total_matches"] == 2 and fresh["total_groups"] == 1
    replay = engine.retrieval.compact_search_page(continued, authority=reader).to_wire()
    assert replay == before
    assert first["results"][0]["group_id"] == replay["results"][0]["group_id"]
    assert not set(_ids(first)) & set(_ids(replay))
    assert set(_ids(first)) | set(_ids(replay)) == set(ids[:-1])
    # A visible partition change still invalidates the same final-set binding.
    with read_snapshot(engine) as connection:
        visible = connection.execute(
            "SELECT source_id,route_version FROM logical_sources WHERE head_capture_id=?",
            (ids[0],),
        ).fetchone()
    engine.sources.route(
        SourceRouteRequest(
            source_id=visible["source_id"],
            space_id=space,
            expected_head=ids[0],
            expected_route_version=visible["route_version"],
            operation_id="operation_" + str(uuid4()),
        ),
        authority=_owner(),
    )
    with pytest.raises(T03Error, match="cursor_stale"):
        engine.retrieval.compact_search_page(continued, authority=reader)


@pytest.mark.parametrize(
    "epoch",
    [
        "authorization_epoch",
        "projection_policy_version",
        "fencing_epoch",
    ],
)
def test_explicit_policy_and_custody_epochs_still_stale_compact_cursor(
    engine: BrainEngine,
    epoch: str,
) -> None:
    for index in range(2):
        _capture(engine, index)
    reader = replace(_owner(), owner=False, allowed_read_tiers=frozenset({PrivacyTier.WORK}))
    request = CompactSearchPageRequest(query="needle", limit=1)
    first = engine.retrieval.compact_search_page(request, authority=reader).to_wire()
    with engine._store.transaction() as connection:
        connection.execute(f"UPDATE engine_generations SET {epoch}={epoch}+1 WHERE singleton=1")
    with pytest.raises(T03Error, match="cursor_stale"):
        engine.retrieval.compact_search_page(
            replace(request, cursor=first["next_cursor"]),
            authority=reader,
        )


def test_all_candidates_project_before_counts_and_not_found_is_excluded(
    engine: BrainEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = sorted(_capture(engine, index) for index in range(3))
    original = RecordProjector.read

    def inaccessible(self: RecordProjector, record_id: str, **kwargs: Any) -> Any:
        if record_id == identities[-1]:
            raise T03Error("not_found")
        return original(self, record_id, **kwargs)

    monkeypatch.setattr(RecordProjector, "read", inaccessible)
    page = _page(engine, limit=1)
    assert page["total_matches"] == 2 and page["results"][0]["match_count"] == 2
    monkeypatch.setattr(RecordProjector, "read", original)
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE search_documents SET body=body || ' stale' WHERE result_id=?",
            (identities[-1],),
        )
    for count_only in (True, False):
        with pytest.raises(T03Error, match="projection_stale"):
            _page(engine, limit=1, count_only=count_only)


def test_lexical_never_embeds_and_hybrid_unavailable_is_explicit(
    engine: BrainEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(engine, 0)

    def unavailable(*args: object) -> None:
        pytest.fail("lexical search must never invoke an embedding helper")

    monkeypatch.setattr("open_brain_engine.engine.compact_search._semantic_query", unavailable)
    assert _page(engine, count_only=True)["mode_used"] == "lexical"
    monkeypatch.setattr("open_brain_engine.engine.compact_search._semantic_query", lambda *a: None)
    fallback = _page(engine, mode="hybrid_preferred", count_only=True)
    assert fallback["mode_used"] == "lexical" and fallback["count_scope"] == "lexical_matches"
    assert fallback["warnings"] == ["model_unavailable"]
    with pytest.raises(T03Error, match="model_unavailable"):
        _page(engine, mode="hybrid_required")
    with pytest.raises(T03Error, match="unsupported_capability"):
        engine.retrieval.compact_search_page(
            CompactSearchPageRequest(query="needle", count_only=True),
            authority=replace(_owner(), owner=False, capabilities=frozenset()),
        )


def test_response_grammar_rejects_extra_fields(engine: BrainEngine) -> None:
    page = _page(engine)
    with pytest.raises(T03Error, match="invalid_arguments"):
        CompactSearchPageResponse.from_wire(page | {"excerpt": "forbidden"})


class _SyntheticBackend:
    component_id = "ab" * 32

    def __init__(self) -> None:
        self.available = True

    def embed(self, purpose: Purpose, texts: Sequence[str]) -> list[EmbeddedItem]:
        from hashlib import sha256

        if not self.available:
            raise EmbeddingFailure("resource_missing")
        return [
            EmbeddedItem(
                slot,
                sha256(text.encode()).hexdigest(),
                len(text.split()),
                (
                    EmbeddingChunk(
                        0, 0, len(text.split()), 0, len(text.encode()), (1.0,) + (0.0,) * 383
                    ),
                ),
            )
            for slot, text in enumerate(texts)
        ]


def test_hybrid_totals_match_fusion_and_resource_switch_stales(
    engine: BrainEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_engine.engine import embedding_cache

    backend = _SyntheticBackend()
    monkeypatch.setattr(engine, "_embedding_backend", lambda: backend)
    for index in range(8):
        engine.capture.accept(
            TextPayload("needle body" if index < 3 else "unrelated synthetic body"),
            delivery_id=f"compact.hybrid.{index}",
            title=f"Synthetic {index}",
            privacy_tier=PrivacyTier.WORK,
        )
    engine.embedding_policy.enable(backend.component_id, authority=_owner())
    embedding_cache.rebuild(engine, authority=_owner(), backend=backend)
    full = engine.retrieval.search_page(
        SearchPageRequest(query="needle", mode="hybrid_required", limit=100),
        authority=_owner(),
    ).to_wire()
    compact = _page(engine, mode="hybrid_required", group_by="none", limit=100, target_bytes=8192)
    assert compact["mode_used"] == "hybrid" and compact["count_scope"] == "hybrid_candidates"
    assert compact["total_matches"] == 8
    assert _ids(compact) == [
        row["record_id"] for row in cast(list[dict[str, Any]], full["results"])
    ]
    assert _page(engine, count_only=True)["total_matches"] == 3
    for mode in ("hybrid_required", "hybrid_preferred"):
        first = _page(engine, mode=mode, limit=1)
        backend.available = False
        with pytest.raises(T03Error, match="cursor_stale"):
            _page(engine, mode=mode, limit=1, cursor=first["next_cursor"])
        backend.available = True
