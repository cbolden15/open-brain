from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    DecisionOutcome,
    ProposalDraft,
    TextPayload,
)

from open_brain.profile import compile_single_user_local


def _engine(root: Path, *, starter_spaces: tuple[str, ...] = ()) -> BrainEngine:
    return BrainEngine.open(
        compile_single_user_local(root, starter_spaces=starter_spaces),
        faults=set(),
    )


def _canonical(
    engine: BrainEngine,
    *,
    delivery_id: str,
    space_id: str,
    title: str,
    body: str,
) -> str:
    receipt = engine.capture.accept(
        TextPayload(body),
        delivery_id=delivery_id,
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space_id,
        title=title,
    )
    return receipt.capture_id


def _database(root: Path) -> Path:
    return root / ".open-brain/state/phase1.sqlite3"


def _drop_live_search(connection: sqlite3.Connection) -> None:
    for trigger in (
        "search_documents_result_id_immutable",
        "search_documents_fts_insert",
        "search_documents_fts_update",
        "search_documents_fts_delete",
    ):
        connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute("DROP TABLE search_documents_fts")
    connection.execute("DROP TABLE search_fts_identity")


def test_live_search_schema_uses_fts5_and_synchronizes_mutations(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first = engine.capture.accept(
        TextPayload("Original searchable nebula"),
        delivery_id="search.schema.first",
    )
    second = engine.capture.accept(
        TextPayload("Rollback anchor quasar"),
        delivery_id="search.schema.second",
    )

    with sqlite3.connect(_database(root), isolation_level=None) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'search_documents_fts'"
        ).fetchone()
        assert schema is not None
        normalized_schema = " ".join(str(schema[0]).split()).casefold()
        assert "using fts5" in normalized_schema
        assert "unicode61 remove_diacritics 2" in normalized_schema
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'search_documents'"
            )
        }
        assert triggers >= {
            "search_documents_fts_insert",
            "search_documents_fts_update",
            "search_documents_fts_delete",
            "search_documents_result_id_immutable",
        }

        joined = connection.execute(
            """
            SELECT d.result_id, i.fts_rowid, f.rowid, f.result_id, f.title, f.body
            FROM search_documents AS d
            JOIN search_fts_identity AS i USING (result_id)
            JOIN search_documents_fts AS f ON f.rowid = i.fts_rowid
            ORDER BY d.result_id
            """
        ).fetchall()
        assert len(joined) == 2
        assert all(row[0] == row[3] and row[1] == row[2] for row in joined)

        probe = connection.execute(
            """
            SELECT bm25(search_documents_fts, 0.0, 10.0, 1.0),
                   snippet(search_documents_fts, -1, '[', ']', '…', 24)
            FROM search_documents_fts
            WHERE search_documents_fts MATCH ?
            """,
            ('"nebula"',),
        ).fetchone()
        assert probe is not None
        assert isinstance(probe[0], float)
        assert "[nebula]" in str(probe[1]).casefold()

        first_rowid = connection.execute(
            "SELECT fts_rowid FROM search_fts_identity WHERE result_id = ?",
            (first.capture_id,),
        ).fetchone()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE search_documents SET title = ?, body = ? WHERE result_id = ?",
            ("Replacement title", "Replacement searchable pulsar", first.capture_id),
        )
        assert connection.execute(
            "SELECT count(*) FROM search_documents_fts "
            "WHERE search_documents_fts MATCH ?",
            ('"pulsar"',),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM search_documents_fts "
            "WHERE search_documents_fts MATCH ?",
            ('"nebula"',),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT fts_rowid FROM search_fts_identity WHERE result_id = ?",
            (first.capture_id,),
        ).fetchone() == first_rowid
        connection.execute("COMMIT")
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute(
                "UPDATE search_documents SET result_id = ? WHERE result_id = ?",
                ("capture_00000000-0000-4000-8000-000000000099", first.capture_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute(
                "UPDATE search_documents SET result_id = NULL WHERE result_id = ?",
                (first.capture_id,),
            )
        assert connection.execute(
            "SELECT result_id FROM search_documents WHERE result_id = ?",
            (first.capture_id,),
        ).fetchone() == (first.capture_id,)

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE search_documents SET body = ? WHERE result_id = ?",
            ("Transient rollback token", second.capture_id),
        )
        connection.execute("ROLLBACK")
        assert connection.execute(
            "SELECT count(*) FROM search_documents_fts "
            "WHERE search_documents_fts MATCH ?",
            ('"rollback"',),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM search_documents_fts "
            "WHERE search_documents_fts MATCH ?",
            ('"transient"',),
        ).fetchone() == (0,)

        connection.execute("DELETE FROM search_documents WHERE result_id = ?", (first.capture_id,))
        assert connection.execute(
            "SELECT count(*) FROM search_fts_identity WHERE result_id = ?",
            (first.capture_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM search_documents_fts WHERE result_id = ?",
            (first.capture_id,),
        ).fetchone() == (0,)


def test_search_uses_documented_ranking_unicode_and_safe_literal_queries(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain", starter_spaces=("Notes",))
    space = engine.inbox.spaces()[0]
    title_hit = _canonical(
        engine,
        delivery_id="search.rank.title",
        space_id=space.space_id,
        title="Aurora field atlas",
        body="Quiet reference material",
    )
    body_phrase = _canonical(
        engine,
        delivery_id="search.rank.body",
        space_id=space.space_id,
        title="Body phrase candidate",
        body="An aurora atlas for observers",
    )
    phrase_hit = _canonical(
        engine,
        delivery_id="search.rank.phrase",
        space_id=space.space_id,
        title="Comet phrase",
        body="The blue comet crossed tonight",
    )
    split_hit = _canonical(
        engine,
        delivery_id="search.rank.split",
        space_id=space.space_id,
        title="Comet split",
        body="Blue objects may have a distant comet tail",
    )
    unicode_hit = _canonical(
        engine,
        delivery_id="search.rank.unicode",
        space_id=space.space_id,
        title="Unicode reference",
        body="Cafe\u0301 東京案内 api_client_v2",
    )
    long_body_hit = _canonical(
        engine,
        delivery_id="search.rank.long-snippet",
        space_id=space.space_id,
        title="Long excerpt candidate",
        body=("leading context " * 200) + "deepmarker" + (" trailing context" * 200),
    )

    ranked = engine.retrieval.search("aurora atlas", record_type="canonical")
    assert [item.capture_id for item in ranked[:2]] == [title_hit, body_phrase]
    assert ranked[0].explanation == "title match"
    phrase_results = engine.retrieval.search("blue comet", record_type="canonical")
    assert [item.capture_id for item in phrase_results[:2]] == [phrase_hit, split_hit]
    assert phrase_results[0].explanation == "exact phrase match"
    assert engine.retrieval.search("cafe", record_type="canonical")[0].capture_id == unicode_hit
    assert engine.retrieval.search("CAFÉ", record_type="canonical")[0].capture_id == unicode_hit
    assert engine.retrieval.search("東京案内", record_type="canonical")[0].capture_id == unicode_hit
    assert engine.retrieval.search("api_client_v2", record_type="canonical")[0].capture_id == (
        unicode_hit
    )
    long_result = engine.retrieval.search("deepmarker", record_type="canonical")[0]
    assert long_result.capture_id == long_body_hit
    assert "[deepmarker]" in long_result.excerpt.casefold()
    assert len(long_result.excerpt) <= 500

    for hostile in (
        "AND OR NOT NEAR",
        "title:",
        'alpha"',
        "(alpha",
        "alpha)",
        "alpha*",
        "title:alpha",
        "NEAR(alpha beta)",
    ):
        assert isinstance(engine.retrieval.search(hostile), tuple)
    for invalid in ("", "!!!", '"', "(", "*", "\x00", "x" * 501):
        with pytest.raises(ValueError, match="^invalid query$"):
            engine.retrieval.search(invalid)
    assert isinstance(engine.retrieval.search("x" * 500), tuple)


def test_search_applies_authorization_filters_ranking_and_limit_in_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path / "brain", starter_spaces=("Allowed", "Denied"))
    allowed, denied = engine.inbox.spaces()
    allowed_capture = engine.capture.accept(
        TextPayload("priority signal"),
        delivery_id="search.scope.allowed",
        space_id=allowed.space_id,
    )
    engine.capture.accept(
        TextPayload("priority signal priority signal priority signal"),
        delivery_id="search.scope.denied",
        space_id=denied.space_id,
    )
    statements: list[str] = []
    original_connect = engine._store.connect

    def traced_connect() -> sqlite3.Connection:
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(engine._store, "connect", traced_connect)
    scoped = engine.retrieval.scoped(allowed_space_ids=frozenset({allowed.space_id}))

    results = scoped.search("priority signal", payload_family="text", limit=1)

    assert [result.capture_id for result in results] == [allowed_capture.capture_id]
    ranked_statements = [
        statement
        for statement in statements
        if "MATCH" in statement and "ORDER BY" in statement and "LIMIT" in statement
    ]
    assert len(ranked_statements) == 1
    assert "space_id" in ranked_statements[0]
    assert "payload_family" in ranked_statements[0]
    assert "SELECT * FROM search_documents" not in ranked_statements[0]
    assert scoped.search("priority signal", limit=1) == results
    assert engine.retrieval.scoped(allowed_space_ids=frozenset()).search("priority") == ()
    assert engine.retrieval.scoped(
        allowed_space_ids=frozenset({"space_00000000-0000-4000-8000-000000000000"})
    ).search("priority") == ()
    with pytest.raises(ValueError, match="allowed space"):
        engine.retrieval.scoped(
            allowed_space_ids=frozenset(f"space_{index}" for index in range(257))
        ).search("priority")


def test_protected_values_are_removed_before_matching(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    protected_prefix = "/" + "/".join(("Users", "synthetic"))
    protected_path = f"{protected_prefix}/private-note.md"
    engine.capture.accept(
        TextPayload(
            "Public astronomy note token=synthetic-secret-value "
            f"stored at {protected_path}"
        ),
        delivery_id="search.projection.protected",
    )

    public = engine.retrieval.search("astronomy")[0]

    assert "astronomy" in public.excerpt.casefold()
    assert engine.retrieval.search("synthetic-secret-value") == ()
    assert engine.retrieval.search("private-note") == ()
    serialized = " ".join((public.title, public.excerpt, public.explanation))
    assert "synthetic-secret-value" not in serialized
    assert protected_prefix not in serialized


def test_defensive_projection_precedes_fetch_clamping_and_search_snippets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    fetch_capture = engine.capture.accept(
        TextPayload("Initial fetch content"),
        delivery_id="search.projection.fetch-boundary",
    )
    fetch_reference = "urn:synthetic:fetch:" + "protected-segment-" * 20
    search_capture = engine.capture.accept(
        TextPayload("Initial search content"),
        delivery_id="search.projection.snippet-boundary",
    )
    search_reference = " ".join(f"protectedsegment{index}" for index in range(40))

    with sqlite3.connect(_database(root)) as connection:
        connection.execute(
            "UPDATE captures SET source_reference = ? WHERE capture_id = ?",
            (fetch_reference, fetch_capture.capture_id),
        )
        connection.execute(
            "UPDATE search_documents SET body = ? WHERE result_id = ?",
            ("x" * 490 + fetch_reference, fetch_capture.capture_id),
        )
        connection.execute(
            "UPDATE captures SET source_reference = ? WHERE capture_id = ?",
            (search_reference, search_capture.capture_id),
        )
        connection.execute(
            "UPDATE search_documents SET body = ? WHERE result_id = ?",
            ("visible lead " + search_reference + " visible tail", search_capture.capture_id),
        )

    fetched = engine.retrieval.fetch(fetch_capture.capture_id)

    assert fetched is not None
    assert len(fetched.excerpt) <= 500
    assert fetch_reference[:30] not in fetched.excerpt
    assert fetched.provenance.source_origin == "unknown"
    assert fetched.trust == "unverified"
    assert engine.retrieval.search("protectedsegment20") == ()


def test_relevance_ties_and_source_canonical_duplicates_are_deterministic(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "brain", starter_spaces=("Notes",))
    space = engine.inbox.spaces()[0]
    duplicate_capture = _canonical(
        engine,
        delivery_id="search.tie.duplicate",
        space_id=space.space_id,
        title="Stable tie token",
        body="Stable tie token",
    )
    first = engine.capture.accept(
        TextPayload("Equal tie token"),
        delivery_id="search.tie.first",
    )
    second = engine.capture.accept(
        TextPayload("Equal tie token"),
        delivery_id="search.tie.second",
    )
    able = engine.capture.accept(
        TextPayload("Able order token"),
        delivery_id="search.tie.able",
    )
    zulu = engine.capture.accept(
        TextPayload("Zulu order token"),
        delivery_id="search.tie.zulu",
    )

    duplicates = [
        result
        for result in engine.retrieval.search("Stable tie token")
        if result.capture_id == duplicate_capture
    ]
    equal_ties = engine.retrieval.search("Equal tie token", record_type="source")
    title_ties = engine.retrieval.search("order token", record_type="source")

    assert [result.record_type for result in duplicates] == ["canonical", "source"]
    assert [result.result_id for result in equal_ties[:2]] == sorted(
        (first.capture_id, second.capture_id)
    )
    assert duplicate_capture not in {result.capture_id for result in equal_ties[:2]}
    assert [result.capture_id for result in title_ties[:2]] == [able.capture_id, zulu.capture_id]
    assert engine.retrieval.search("named-no-result-xylophonic") == ()


def test_reconciliation_update_and_projection_delete_change_ordered_results(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, starter_spaces=("Notes",))
    space = engine.inbox.spaces()[0]
    mutable = _canonical(
        engine,
        delivery_id="search.lifecycle.mutable",
        space_id=space.space_id,
        title="Mutable meteor title",
        body="Quiet mutable meteor body",
    )
    remaining = _canonical(
        engine,
        delivery_id="search.lifecycle.remaining",
        space_id=space.space_id,
        title="Fallback note",
        body="Mutable meteor remains searchable",
    )

    before = engine.retrieval.search("mutable meteor", record_type="canonical")
    assert [result.capture_id for result in before[:2]] == [mutable, remaining]

    page = next(
        path
        for path in (root / "content/spaces").rglob("page_*.md")
        if mutable in path.read_text(encoding="utf-8")
    )
    page.write_text(
        page.read_text(encoding="utf-8")
        .replace("Mutable meteor title", "Renewed stellar title")
        .replace("Quiet mutable meteor body", "Renewed stellar body signal"),
        encoding="utf-8",
    )

    receipt = engine.reconciliation.reconcile()
    old_query = engine.retrieval.search("mutable meteor", record_type="canonical")
    new_query = engine.retrieval.search("renewed stellar", record_type="canonical")

    assert receipt.page_updates >= 1
    assert [result.capture_id for result in old_query] == [remaining]
    assert new_query[0].capture_id == mutable
    assert "[renewed]" in new_query[0].excerpt.casefold()
    assert len(new_query[0].excerpt) <= 500

    with engine._store.transaction() as connection:
        connection.execute(
            "DELETE FROM search_documents WHERE capture_id = ? AND record_type = 'canonical'",
            (remaining,),
        )

    assert engine.retrieval.search("mutable meteor", record_type="canonical") == ()


def test_version_one_adoption_is_public_safe_atomic_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "adopted"
    engine = _engine(root)
    captured = engine.capture.accept(
        TextPayload("Initial projection"),
        delivery_id="search.adoption.capture",
    )
    with sqlite3.connect(_database(root)) as connection:
        _drop_live_search(connection)
        provenance = json.loads(
            connection.execute(
                "SELECT provenance_json FROM captures WHERE capture_id = ?",
                (captured.capture_id,),
            ).fetchone()[0]
        )
        provenance["source_ref"] = "synthetic-protected-source"
        connection.execute(
            "UPDATE captures SET source_reference = ?, provenance_json = ? WHERE capture_id = ?",
            (
                "synthetic-protected-source",
                json.dumps(provenance, sort_keys=True, separators=(",", ":")),
                captured.capture_id,
            ),
        )
        connection.execute(
            "UPDATE search_documents SET body = ? WHERE result_id = ?",
            (
                "Public adoption text synthetic-protected-source token=adoption-secret",
                captured.capture_id,
            ),
        )

    reopened = _engine(root)
    result = reopened.retrieval.search("public adoption")[0]
    assert result.capture_id == captured.capture_id
    assert reopened.retrieval.search("synthetic-protected-source") == ()
    assert reopened.retrieval.search("adoption-secret") == ()
    with sqlite3.connect(_database(root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        first_identity = connection.execute(
            "SELECT fts_rowid, result_id FROM search_fts_identity"
        ).fetchall()
        first_counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM search_documents),
                (SELECT count(*) FROM search_fts_identity),
                (SELECT count(*) FROM search_documents_fts)
            """
        ).fetchone()

    _engine(root)
    with sqlite3.connect(_database(root)) as connection:
        assert connection.execute(
            "SELECT fts_rowid, result_id FROM search_fts_identity"
        ).fetchall() == first_identity
        assert connection.execute(
            """
            SELECT
                (SELECT count(*) FROM search_documents),
                (SELECT count(*) FROM search_fts_identity),
                (SELECT count(*) FROM search_documents_fts)
            """
        ).fetchone() == first_counts == (1, 1, 1)

    orphan_root = tmp_path / "orphan"
    _engine(orphan_root)
    with sqlite3.connect(_database(orphan_root)) as connection:
        _drop_live_search(connection)
        connection.execute(
            """
            INSERT INTO search_documents (
                result_id, capture_id, record_type, payload_family, space_id,
                title, body, trust, provenance_json, canonical_path, updated_at
            ) VALUES (?, ?, 'source', 'text', NULL, ?, ?, 'unverified', '{}', NULL, ?)
            """,
            (
                "capture_00000000-0000-4000-8000-000000000001",
                "capture_00000000-0000-4000-8000-000000000001",
                "Orphan title",
                "Orphan body",
                "2026-09-08T00:00:00Z",
            ),
        )

    with pytest.raises(ValueError, match="capture is unavailable"):
        _engine(orphan_root)
    with sqlite3.connect(_database(orphan_root)) as connection:
        assert connection.execute(
            "SELECT body FROM search_documents"
        ).fetchone() == ("Orphan body",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'search_documents_fts'"
        ).fetchone() is None


@pytest.mark.parametrize(
    "provenance_json",
    (
        "not-json",
        '{"content_origin":"owner_authored","owner_context":"automation_absent",'
        '"source_ref":"urn:synthetic:wrong-link"}',
    ),
)
def test_version_one_adoption_rejects_malformed_or_unlinked_provenance(
    tmp_path: Path,
    provenance_json: str,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    captured = engine.capture.accept(
        TextPayload("Preserved adoption projection"),
        delivery_id="search.adoption.invalid-provenance",
    )
    with sqlite3.connect(_database(root)) as connection:
        _drop_live_search(connection)
        connection.execute(
            "UPDATE captures SET provenance_json = ? WHERE capture_id = ?",
            (provenance_json, captured.capture_id),
        )

    with pytest.raises(ValueError, match="search projection provenance is invalid"):
        _engine(root)

    with sqlite3.connect(_database(root)) as connection:
        assert connection.execute(
            "SELECT body FROM search_documents WHERE result_id = ?",
            (captured.capture_id,),
        ).fetchone() == ("Preserved adoption projection",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'search_documents_fts'"
        ).fetchone() is None


def test_live_rebuild_preserves_order_and_leaves_portable_snapshot_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    for index, body in enumerate(
        (
            "Orion search phrase",
            "Orion search phrase with another occurrence of Orion",
            "Unrelated distractor",
        )
    ):
        engine.capture.accept(TextPayload(body), delivery_id=f"search.rebuild.{index}")
    engine.portability.rebuild_index()
    snapshot = root / ".open-brain/indexes/search.sqlite3"
    snapshot_bytes = snapshot.read_bytes()
    before = tuple(
        (result.result_id, result.title, result.excerpt, result.explanation)
        for result in engine.retrieval.search("Orion search")
    )
    with sqlite3.connect(_database(root)) as connection:
        identities = connection.execute(
            "SELECT fts_rowid, result_id FROM search_fts_identity ORDER BY fts_rowid"
        ).fetchall()
        connection.execute("DELETE FROM search_documents_fts")
    assert engine.retrieval.search("Orion search") == ()

    engine._rebuild_live_search_index()

    after = tuple(
        (result.result_id, result.title, result.excerpt, result.explanation)
        for result in engine.retrieval.search("Orion search")
    )
    assert after == before
    assert snapshot.read_bytes() == snapshot_bytes
    with sqlite3.connect(_database(root)) as connection:
        assert connection.execute(
            "SELECT fts_rowid, result_id FROM search_fts_identity ORDER BY fts_rowid"
        ).fetchall() == identities
    engine._rebuild_live_search_index()
    assert tuple(
        (result.result_id, result.title, result.excerpt, result.explanation)
        for result in engine.retrieval.search("Orion search")
    ) == before


def test_full_projection_rederivation_replaces_rows_from_durable_sources(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, starter_spaces=("Notes",))
    space = engine.inbox.spaces()[0]
    canonical_capture = _canonical(
        engine,
        delivery_id="search.rederive.canonical",
        space_id=space.space_id,
        title="Orion durable atlas",
        body="Orion durable body",
    )
    source_capture = engine.capture.accept(
        TextPayload("Orion durable field log"),
        delivery_id="search.rederive.source",
    )
    engine.portability.rebuild_index()
    portable_snapshot = root / ".open-brain/indexes/search.sqlite3"
    portable_bytes = portable_snapshot.read_bytes()
    before = tuple(
        (result.result_id, result.title, result.excerpt, result.trust)
        for result in engine.retrieval.search("Orion durable")
    )
    canonical_result_id = next(
        result.result_id
        for result in engine.retrieval.search("Orion durable", record_type="canonical")
        if result.capture_id == canonical_capture
    )
    orphan_id = "capture_00000000-0000-4000-8000-000000000099"

    with sqlite3.connect(_database(root)) as connection:
        connection.execute(
            "UPDATE search_documents SET title = 'Corrupt title', body = 'Corrupt body', "
            "trust = 'unverified' WHERE result_id = ?",
            (canonical_result_id,),
        )
        connection.execute(
            "DELETE FROM search_documents WHERE result_id = ?",
            (source_capture.capture_id,),
        )
        connection.execute(
            """
            INSERT INTO search_documents (
                result_id, capture_id, record_type, payload_family, space_id,
                title, body, trust, provenance_json, canonical_path, updated_at
            ) VALUES (?, ?, 'source', 'text', NULL, 'Ghost title', 'Orion durable ghost',
                      'unverified', ?, NULL, '2026-09-08T00:00:00Z')
            """,
            (orphan_id, canonical_capture, json.dumps({"capture_id": canonical_capture})),
        )

    assert engine.retrieval.search("ghost")[0].result_id == orphan_id

    engine._rederive_live_search_projection()

    after = tuple(
        (result.result_id, result.title, result.excerpt, result.trust)
        for result in engine.retrieval.search("Orion durable")
    )
    assert after == before
    assert engine.retrieval.search("ghost") == ()
    assert portable_snapshot.read_bytes() == portable_bytes
    with sqlite3.connect(_database(root)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM search_documents WHERE result_id = ?",
            (orphan_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT
                (SELECT count(*) FROM search_documents),
                (SELECT count(*) FROM search_fts_identity),
                (SELECT count(*) FROM search_documents_fts)
            """
        ).fetchone() == (3, 3, 3)


def test_full_projection_rederivation_preserves_previous_state_on_invalid_inputs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, starter_spaces=("Notes",))
    space = engine.inbox.spaces()[0]
    capture_id = _canonical(
        engine,
        delivery_id="search.rederive.invalid",
        space_id=space.space_id,
        title="Preserved projection title",
        body="Preserved projection body",
    )

    def projection_state() -> tuple[tuple[tuple[object, ...], ...], ...]:
        with sqlite3.connect(_database(root)) as connection:
            return (
                tuple(
                    connection.execute(
                        "SELECT result_id, capture_id, title, body, trust "
                        "FROM search_documents ORDER BY result_id"
                    )
                ),
                tuple(
                    connection.execute(
                        "SELECT fts_rowid, result_id FROM search_fts_identity ORDER BY fts_rowid"
                    )
                ),
                tuple(
                    connection.execute(
                        "SELECT rowid, result_id, title, body "
                        "FROM search_documents_fts ORDER BY rowid"
                    )
                ),
            )

    before = projection_state()
    with sqlite3.connect(_database(root)) as connection:
        original_provenance = connection.execute(
            "SELECT provenance_json FROM captures WHERE capture_id = ?",
            (capture_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE captures SET provenance_json = 'not-json' WHERE capture_id = ?",
            (capture_id,),
        )

    with pytest.raises(ValueError, match="search projection provenance is invalid"):
        engine._rederive_live_search_projection()
    assert projection_state() == before

    with sqlite3.connect(_database(root)) as connection:
        connection.execute(
            "UPDATE captures SET provenance_json = ? WHERE capture_id = ?",
            (original_provenance, capture_id),
        )
    next((root / "content/spaces").rglob("page_*.md")).unlink()

    with pytest.raises(ValueError, match="canonical page Markdown is missing"):
        engine._rederive_live_search_projection()
    assert projection_state() == before


def test_full_projection_rederivation_restores_reviewed_publications(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, starter_spaces=("Notes",))
    space = engine.inbox.spaces()[0]
    capture = engine.capture.accept(
        TextPayload("Reviewed source material"),
        delivery_id="search.rederive.reviewed.capture",
        space_id=space.space_id,
    )
    proposal = engine.review.propose(
        capture.capture_id,
        (ProposalDraft("Reviewed constellation", "Reviewed nebula publication"),),
        delivery_id="search.rederive.reviewed.proposal",
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="search.rederive.reviewed.decision",
    )
    before = engine.retrieval.search("Reviewed nebula", record_type="canonical")
    assert len(before) == 1
    assert before[0].trust == "reviewed"

    with engine._store.transaction() as connection:
        connection.execute(
            "DELETE FROM search_documents WHERE result_id = ?",
            (before[0].result_id,),
        )
    assert engine.retrieval.search("Reviewed nebula", record_type="canonical") == ()

    engine._rederive_live_search_projection()

    assert engine.retrieval.search("Reviewed nebula", record_type="canonical") == before
