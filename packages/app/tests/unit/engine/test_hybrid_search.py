"""Hybrid ``search.page``: ``ob.hybrid-rrf.v2`` fusion, caller-side vector matching and cursors."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine import embedding_cache as cache
from open_brain_engine.engine.embedding_backend import (
    EmbeddedItem,
    EmbeddingBackend,
    EmbeddingChunk,
    EmbeddingFailure,
)
from open_brain_engine.engine.records import RecordProjector
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    SearchPageRequest,
    T03Error,
)

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import T03AppAdapter, T03AppError, owner_authority
from packages.app.tests.unit.engine.test_embedding_cache import _retract, _Revisions

COMPONENT = "ab" * 32
OTHER = "cd" * 32
# Fake semantic space: words map to concept dimensions; the last dimension is a small
# shared base so every vector is non-zero and every vectorized record has a cosine.
CONCEPTS = {
    "vehicle": ("car", "automobile", "vehicle", "sedan", "brake", "garage"),
    "repair": ("repair", "fix", "mend", "maintenance", "service"),
    "garden": ("garden", "tomatoes", "compost", "soil"),
    "music": ("guitar", "piano", "music", "chords"),
    "travel": ("flight", "airport", "travel", "luggage"),
    "cooking": ("recipe", "bake", "oven", "bread"),
    "finance": ("budget", "invoice", "spending", "ledger"),
    "secret": ("clandestine", "covert", "hidden"),
}
_DIMENSION = {word: index for index, words in enumerate(CONCEPTS.values()) for word in words}
CORPUS = (
    ("Sedan service log", "Took the sedan in for brake maintenance and an oil change."),
    ("Compost routine", "Turned the compost and added soil to the tomatoes bed."),
    ("Piano practice", "Practiced chords on the piano for an hour of music."),
    ("Airport checklist", "Pack luggage early and confirm the flight before the airport."),
    ("Bread notes", "Bake bread with a hot oven; the recipe needs more water."),
    ("Monthly budget", "Reviewed spending against the ledger and paid one invoice."),
    ("Garage day", "Car keeps making noise; maybe fix the belt soon."),
    ("Weekend plans", "Notes about nothing in particular at all."),
)
QUERIES = ("automobile repair", "car notes", "budget ledger", "piano practice", "travel")


def _vector(text: str) -> tuple[float, ...]:
    values = [0.0] * 384
    values[383] = 0.05
    for word in re.findall(r"\w+", text.lower()):
        if word in _DIMENSION:
            values[_DIMENSION[word]] += 1.0
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _item(slot: int, text: str) -> EmbeddedItem:
    encoded = text.encode("utf-8")
    words = len(text.split())
    return EmbeddedItem(
        slot,
        hashlib.sha256(encoded).hexdigest(),
        words,
        (EmbeddingChunk(0, 0, words, 0, len(encoded), _vector(text)),),
    )


class FakeBackend:
    """Deterministic in-process backend recording every exchange."""

    def __init__(self, component_id: str = COMPONENT, *, fail: str | None = None) -> None:
        self.component_id = component_id
        self.fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    def embed(self, purpose: str, texts: Sequence[str]) -> list[EmbeddedItem]:
        self.calls.append((purpose, list(texts)))
        if purpose == "query" and self.fail is not None:
            raise EmbeddingFailure(self.fail)
        return [_item(slot, text) for slot, text in enumerate(texts)]

    @property
    def queries(self) -> list[str]:
        return [texts[0] for purpose, texts in self.calls if purpose == "query"]


class Factory:
    """Lazy app-side factory; counts constructions like the installed helper factory."""

    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.constructed = 0

    def __call__(self) -> FakeBackend:
        self.constructed += 1
        return self.backend


def wire(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value.to_wire())


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return owner_authority(engine.tasks, session_id="owner-hybrid-test")


def _scoped(*tiers: PrivacyTier) -> EffectiveAuthority:
    return EffectiveAuthority(
        "synthetic-principal",
        "scoped-hybrid-session",
        frozenset({"search", "content-read"}),
        None,
        allowed_read_tiers=frozenset(tiers),
    )


def _brain(
    tmp_path: Path, corpus: Sequence[tuple[str, str]] = CORPUS
) -> tuple[BrainEngine, Factory, dict[str, str]]:
    tmp_path.chmod(0o700)
    factory = Factory(FakeBackend())
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=()),
        embedding_backend=factory,
    )
    ids = {
        title: engine.capture.accept(
            TextPayload(body),
            delivery_id=f"hybrid.{index}",
            title=title,
            privacy_tier=PrivacyTier.PUBLIC,
        ).capture_id
        for index, (title, body) in enumerate(corpus)
    }
    engine.embedding_policy.enable(COMPONENT, authority=_owner(engine))
    return engine, factory, ids


def _rebuild(engine: BrainEngine, backend: EmbeddingBackend | None = None) -> cache.RebuildReceipt:
    return cache.rebuild(
        engine, authority=_owner(engine), backend=FakeBackend() if backend is None else backend
    )


def _search(
    engine: BrainEngine,
    query: str,
    *,
    mode: str = "hybrid_required",
    limit: int = 50,
    cursor: str | None = None,
    authority: EffectiveAuthority | None = None,
) -> dict[str, Any]:
    request = SearchPageRequest(query=query, mode=mode, limit=limit, cursor=cursor)
    return wire(
        engine.retrieval.search_page(
            request, authority=_owner(engine) if authority is None else authority
        )
    )


def _ids(response: dict[str, Any]) -> list[str]:
    return [row["record_id"] for row in response["results"]]


# ------------------------------------------------------------- probe reference

RRF_K = 60
LIST_DEPTH = 50


def _literal(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _reference(engine: BrainEngine, query: str) -> list[str]:
    """``tools/g4_embedding_probe`` relevance: tier 1-4 lexical emulation + max-cosine RRF."""
    fields: dict[str, dict[str, str]] = {}
    with engine._store.connect() as connection:
        connection.row_factory = sqlite3.Row
        projector = RecordProjector(engine.profile, connection, _owner(engine))
        for (record_id,) in connection.execute("SELECT result_id FROM search_documents"):
            fields[record_id] = cache.field_texts(projector.read(record_id))
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE VIRTUAL TABLE r USING fts5(rid UNINDEXED, title, body,"
        " tokenize='unicode61 remove_diacritics 2')"
    )
    db.executemany(
        "INSERT INTO r VALUES (?, ?, ?)",
        [(rid, texts["title"], texts["body"]) for rid, texts in fields.items()],
    )
    chunks = [c for c in query.split() if any(ch.isalnum() for ch in c)]
    literals = [_literal(c) for c in chunks]
    phrase = _literal(" ".join(chunks))
    conjunction = " AND ".join(literals)
    tiers = [f"title : {phrase}", f"title : ({conjunction})", phrase, conjunction]

    def matches(expression: str) -> set[str]:
        return {row[0] for row in db.execute("SELECT rid FROM r WHERE r MATCH ?", (expression,))}

    tier_sets = [matches(expression) for expression in tiers]
    ranked = sorted(
        (next((i + 1 for i, s in enumerate(tier_sets) if rid in s), 5), rid)
        for rid in matches(" OR ".join(literals))
    )
    lexical = [rid for tier, rid in ranked if tier <= 4][:LIST_DEPTH]
    query_vector = _vector(query)
    best = {
        rid: max(
            math.sumprod(array("f", _vector(text)), query_vector)
            for text in texts.values()
            if text.strip()
        )
        for rid, texts in fields.items()
    }
    semantic = sorted(best, key=lambda rid: (-best[rid], rid))[:LIST_DEPTH]
    scores: dict[str, float] = {}
    for ranking in (semantic, lexical):
        for rank, rid in enumerate(ranking, start=1):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda rid: (-scores[rid], rid))


def test_hybrid_ranking_equals_probe_reference(tmp_path: Path) -> None:
    engine, factory, _ = _brain(tmp_path)
    _rebuild(engine)
    for query in QUERIES:
        response = _search(engine, query)
        assert response["mode_used"] == "hybrid"
        assert response["warnings"] == []
        assert response["complete"] is True
        assert set(response) == {
            "status",
            "dto_version",
            "results",
            "complete",
            "next_cursor",
            "mode_used",
            "warnings",
        }
        assert _ids(response) == _reference(engine, query)
    assert factory.backend.queries == list(QUERIES)
    preferred = _search(engine, "automobile repair", mode="hybrid_preferred")
    assert preferred["mode_used"] == "hybrid" and preferred["warnings"] == []


def test_semantic_only_paraphrase_and_tier_five_records(tmp_path: Path) -> None:
    engine, _, ids = _brain(tmp_path)
    _rebuild(engine)
    assert _ids(_search(engine, "automobile repair", mode="lexical")) == []
    paraphrase = _ids(_search(engine, "automobile repair"))
    assert set(paraphrase[:2]) == {ids["Sedan service log"], ids["Garage day"]}
    # Coverage currency: records captured after the rebuild have no vector. A tier 1-4
    # lexical match still appears; a tier-5-only match has no hybrid contribution.
    late_tier5 = engine.capture.accept(
        TextPayload("Nothing here but notes."),
        delivery_id="hybrid.late.5",
        title="Late memo",
        privacy_tier=PrivacyTier.PUBLIC,
    ).capture_id
    late_tier1 = engine.capture.accept(
        TextPayload("Fresh entry."),
        delivery_id="hybrid.late.1",
        title="car notes",
        privacy_tier=PrivacyTier.PUBLIC,
    ).capture_id
    lexical = _ids(_search(engine, "car notes", mode="lexical"))
    assert {late_tier5, late_tier1, ids["Bread notes"]} <= set(lexical)
    hybrid = _ids(_search(engine, "car notes"))
    assert late_tier1 in hybrid and late_tier5 not in hybrid
    # The vectorized tier-5-only record enters through the semantic list alone.
    assert ids["Bread notes"] in hybrid


def test_restricted_caller_scores_only_authorized_identical_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, ids = _brain(tmp_path)
    personal = engine.capture.accept(
        TextPayload("Clandestine covert hidden diary."),
        delivery_id="hybrid.personal",
        title="Private diary",
        privacy_tier=PrivacyTier.PERSONAL,
    ).capture_id
    redacted = ids["Weekend plans"]
    original = cache.field_texts

    def owner_only_text(projected: Any) -> dict[str, str]:
        texts = original(projected)
        if projected.summary["record_id"] == redacted:
            texts = {name: text + " clandestine covert hidden" for name, text in texts.items()}
        return texts

    with monkeypatch.context() as patch:
        # The owner projection embedded for this record differs from what callers read.
        patch.setattr(cache, "field_texts", owner_only_text)
        _rebuild(engine)
    owner = _ids(_search(engine, "clandestine"))
    assert owner[0] == personal
    public = _search(engine, "clandestine", authority=_scoped(PrivacyTier.PUBLIC))
    assert public["mode_used"] == "hybrid"
    rows = _ids(public)
    assert personal not in rows
    # The redacted record's projection never matches its chunk hash, so it has no vector.
    assert redacted not in rows and redacted not in owner
    assert set(rows) == set(ids.values()) - {redacted}
    assert "clandestine" not in json.dumps(public["results"]).lower()


def test_retracted_or_edited_record_gets_no_vector_after_rebuild(tmp_path: Path) -> None:
    engine, _, ids = _brain(tmp_path)
    source = _Revisions(engine)
    old_head = source.submit("Synthetic jasper draft about sedan repair")
    _rebuild(engine)
    assert old_head in _ids(_search(engine, "automobile fix"))
    new_head = source.submit("Synthetic jasper revision about sourdough")
    _retract(engine, ids["Sedan service log"])
    rows = _ids(_search(engine, "automobile fix"))
    assert old_head not in rows and new_head not in rows
    assert ids["Sedan service log"] not in rows
    assert ids["Garage day"] == rows[0]
    assert new_head in _ids(_search(engine, "jasper sourdough"))


def test_consent_disabled_after_rebuild_refuses_or_falls_back(tmp_path: Path) -> None:
    engine, factory, _ = _brain(tmp_path)
    _rebuild(engine)
    engine.embedding_policy.disable(authority=_owner(engine))
    with pytest.raises(T03Error, match="^model_unavailable$"):
        _search(engine, "car notes")
    fallback = _search(engine, "car notes", mode="hybrid_preferred")
    assert fallback["mode_used"] == "lexical"
    assert fallback["warnings"] == ["model_unavailable"]
    lexical = _search(engine, "car notes", mode="lexical")
    assert fallback["results"] == lexical["results"]
    assert factory.constructed == 0 and factory.backend.calls == []


@pytest.mark.parametrize("failure", ["component", "helper", "missing", "no-cache"])
def test_unavailable_backend_refuses_required_and_falls_back_preferred(
    tmp_path: Path, failure: str
) -> None:
    engine, factory, _ = _brain(tmp_path)
    if failure != "no-cache":
        _rebuild(engine)
    if failure == "component":
        factory.backend = FakeBackend(OTHER)
    elif failure == "helper":
        factory.backend = FakeBackend(fail="worker_failed")
    elif failure == "missing":

        def missing() -> FakeBackend:
            raise EmbeddingFailure("resource_missing")

        engine._embedding_backend = missing
    with pytest.raises(T03Error, match="^model_unavailable$"):
        _search(engine, "car notes")
    fallback = _search(engine, "car notes", mode="hybrid_preferred")
    assert (fallback["mode_used"], fallback["warnings"]) == ("lexical", ["model_unavailable"])


def test_lexical_mode_never_touches_backend_or_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, factory, _ = _brain(tmp_path)
    _rebuild(engine)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("lexical search read the embedding cache")

    monkeypatch.setattr(cache, "active_status", forbidden)
    monkeypatch.setattr(cache, "load_active", forbidden)
    response = _search(engine, "car notes", mode="lexical", limit=1)
    assert response["mode_used"] == "lexical" and response["warnings"] == []
    _search(engine, "car notes", mode="lexical", limit=1, cursor=response["next_cursor"])
    assert factory.constructed == 0 and factory.backend.calls == []


def test_hybrid_cursor_continuity(tmp_path: Path) -> None:
    engine, _, _ = _brain(tmp_path)
    _rebuild(engine)
    full = _ids(_search(engine, "car notes"))
    pages: list[str] = []
    cursor = None
    while True:
        page = _search(engine, "car notes", limit=3, cursor=cursor)
        assert page["mode_used"] == "hybrid"
        pages.extend(_ids(page))
        if page["complete"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    assert pages == full and len(full) == len(CORPUS)


def _first(engine: BrainEngine, mode: str = "hybrid_preferred") -> dict[str, Any]:
    page = _search(engine, "car notes", mode=mode, limit=2)
    assert page["next_cursor"] is not None
    return page


@pytest.mark.parametrize("change", ["rebuild", "policy", "component"])
def test_hybrid_cursor_stales_on_generation_policy_or_component(
    tmp_path: Path, change: str
) -> None:
    engine, factory, _ = _brain(tmp_path)
    _rebuild(engine)
    cursor = _first(engine, "hybrid_required")["next_cursor"]
    if change == "rebuild":
        _rebuild(engine)
    elif change == "policy":
        engine.embedding_policy.disable(authority=_owner(engine))
        engine.embedding_policy.enable(COMPONENT, authority=_owner(engine))
        _rebuild(engine)
    else:
        engine.embedding_policy.disable(authority=_owner(engine))
        engine.embedding_policy.enable(OTHER, authority=_owner(engine))
        factory.backend = FakeBackend(OTHER)
        _rebuild(engine, FakeBackend(OTHER))
    with pytest.raises(T03Error, match="^cursor_stale$"):
        _search(engine, "car notes", mode="hybrid_required", limit=2, cursor=cursor)


def test_hybrid_and_lexical_cursors_never_cross(tmp_path: Path) -> None:
    engine, _, _ = _brain(tmp_path)
    _rebuild(engine)
    hybrid = _first(engine)
    assert hybrid["mode_used"] == "hybrid"
    engine.embedding_policy.disable(authority=_owner(engine))
    # A hybrid cursor cannot continue through the optional lexical fallback.
    with pytest.raises(T03Error, match="^cursor_stale$"):
        _search(engine, "car notes", mode="hybrid_preferred", limit=2, cursor=hybrid["next_cursor"])
    with pytest.raises(T03Error, match="^model_unavailable$"):
        _search(engine, "car notes", mode="hybrid_required", limit=2, cursor=hybrid["next_cursor"])
    fallback = _first(engine)
    assert fallback["mode_used"] == "lexical"
    continued = _search(
        engine, "car notes", mode="hybrid_preferred", limit=2, cursor=fallback["next_cursor"]
    )
    assert continued["mode_used"] == "lexical"
    engine.embedding_policy.enable(COMPONENT, authority=_owner(engine))
    _rebuild(engine)
    # A lexical fallback cursor cannot continue as hybrid.
    with pytest.raises(T03Error, match="^cursor_stale$"):
        _search(
            engine, "car notes", mode="hybrid_preferred", limit=2, cursor=fallback["next_cursor"]
        )


def test_lexical_cursor_record_is_unchanged(tmp_path: Path) -> None:
    from open_brain_engine.engine.cursors import CursorStore, binding_digest
    from open_brain_engine.engine.paging import RANKING_VERSION, authority_binding

    engine, _, _ = _brain(tmp_path)
    _rebuild(engine)
    authority = _owner(engine)
    page = _search(engine, "car notes", mode="lexical", limit=1, authority=authority)
    record = CursorStore(engine.profile).resolve(
        page["next_cursor"], now=engine._clock().timestamp()
    )
    assert {"purpose", "binding", "generation", "last"} <= set(record)
    assert record["purpose"] == "search.page"
    assert record["binding"] == binding_digest(
        {
            "query": "car notes",
            "filters": {"payload_families": [], "record_types": [], "space_ids": []},
            "mode": "lexical",
            "mode_used": "lexical",
            "limit": 1,
            "ranking": RANKING_VERSION,
            "authority": authority_binding(authority),
        }
    )
    assert "embedding" not in record["generation"]


def test_payload_bound_applies_after_fusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.core.ids import portable_canonical_json_bytes
    from open_brain_engine.engine import paging

    engine, _, _ = _brain(tmp_path)
    _rebuild(engine)
    full = _search(engine, "automobile repair")
    bound = len(portable_canonical_json_bytes(full["results"][:2])) + 1
    monkeypatch.setattr(paging, "MAX_RESPONSE_BYTES", bound)
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = _search(engine, "automobile repair", cursor=cursor)
        assert 1 <= len(page["results"]) <= 2
        rows.extend(page["results"])
        if page["complete"]:
            break
        cursor = page["next_cursor"]
    assert rows == full["results"]
    monkeypatch.setattr(paging, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(T03Error, match="^response_too_large$"):
        _search(engine, "automobile repair")


def test_mcp_adapter_error_shape_is_unchanged(tmp_path: Path) -> None:
    engine, factory, _ = _brain(tmp_path)
    adapter = T03AppAdapter(engine.tasks, _owner(engine))
    arguments = {"dto_version": 1, "query": "car notes", "mode": "hybrid_required"}
    with pytest.raises(T03AppError) as raised:
        adapter.invoke("search.page", arguments)
    assert raised.value.code == "model_unavailable"
    assert str(raised.value) == "model_unavailable"
    preferred = adapter.invoke("search.page", arguments | {"mode": "hybrid_preferred"})
    assert preferred["mode_used"] == "lexical" and preferred["warnings"] == ["model_unavailable"]
    _rebuild(engine)
    hybrid = adapter.invoke("search.page", arguments)
    assert hybrid["mode_used"] == "hybrid" and set(hybrid) == set(preferred)
    assert factory.constructed >= 1


def test_local_bootstrap_constructs_installed_helper_only_for_hybrid_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain.local_data import LocalRootSelection
    from open_brain.services import embedding_helper
    from open_brain.services.local_bootstrap import open_local_brain

    discovered: list[object] = []

    def missing(*_args: object) -> None:
        discovered.append(None)
        raise EmbeddingFailure("resource_missing")

    monkeypatch.setattr(embedding_helper, "discover_embedding_resource", missing)
    tmp_path.chmod(0o700)
    selection = LocalRootSelection(tmp_path, None, tmp_path / "brain", "darwin")
    with open_local_brain(selection) as session:
        session.tasks.capture.accept(TextPayload("car notes"), delivery_id="bootstrap.one")
        authority = owner_authority(session.tasks, session_id="bootstrap-hybrid")

        def search(mode: str) -> dict[str, Any]:
            request = SearchPageRequest(query="car notes", mode=mode)
            return wire(session.tasks.retrieval.search_page(request, authority=authority))

        assert search("lexical")["mode_used"] == "lexical"
        session.tasks.embedding_policy.enable(COMPONENT, authority=authority)
        session.tasks.embedding_policy.rebuild_cache(authority=authority, backend=FakeBackend())
        assert search("lexical")["mode_used"] == "lexical"
        assert discovered == []
        with pytest.raises(T03Error, match="^model_unavailable$"):
            search("hybrid_required")
        assert len(discovered) == 1
        fallback = search("hybrid_preferred")
        assert (fallback["mode_used"], fallback["warnings"]) == ("lexical", ["model_unavailable"])


@pytest.mark.integration
def test_real_helper_hybrid_on_frozen_relevance_corpus(tmp_path: Path) -> None:
    from open_brain.services import embedding_helper as eh
    from packages.app.tests import embedding_fake as fake
    from packages.app.tests.unit.test_embedding_helper import REPO, _real_inputs

    inputs = _real_inputs()
    if inputs is None:
        pytest.skip("contributor helper, runtime or MiniLM assets are not built")
    files = {path: source.read_bytes() for path, source in inputs.items()}
    files[f"{fake.LICENSES}/NOTICE.txt"] = b"synthetic notice"
    tmp_path.chmod(0o700)
    base = fake.install(
        tmp_path / "install",
        files,
        {"profile_id": "ob.minilm-wordpiece-mean.v1", "tokenizer_build": "tokenizers 0.22.1"},
    )
    brain = tmp_path / "brain"
    installed = eh.discover_embedding_resource(base)
    engine = BrainEngine.open(
        compile_single_user_local(brain, starter_spaces=()),
        embedding_backend=lambda: eh.HelperEmbeddingBackend(installed, brain),
    )
    corpus = json.loads(
        (REPO / "tools/g4_embedding_probe/corpus/relevance-v1.json").read_text(encoding="utf-8")
    )
    ids = {
        record["id"]: engine.capture.accept(
            TextPayload(record["body"]),
            delivery_id=f"relevance.{record['id']}",
            title=record["title"],
            privacy_tier=PrivacyTier.PUBLIC,
        ).capture_id
        for record in corpus["records"]
    }
    backend = eh.HelperEmbeddingBackend(installed, brain)
    engine.embedding_policy.enable(backend.component_id, authority=_owner(engine))
    _rebuild(engine, backend)
    hits: dict[str, list[bool]] = {"paraphrase": [], "exact_id": []}
    regressions: list[str] = []
    for query in corpus["queries"]:
        relevant = {ids[rid] for rid in query["relevant"]}
        hybrid = _search(engine, query["text"], limit=5)
        assert hybrid["mode_used"] == "hybrid"
        hit = bool(relevant & set(_ids(hybrid)))
        hits[query["kind"]].append(hit)
        lexical = _ids(_search(engine, query["text"], mode="lexical", limit=5))
        if query["kind"] == "exact_id" and relevant & set(lexical) and not hit:
            regressions.append(query["id"])
    paraphrase = sum(hits["paraphrase"]) / len(hits["paraphrase"])
    exact = sum(hits["exact_id"]) / len(hits["exact_id"])
    print(f"hybrid hit@5 paraphrase={paraphrase:.3f} exact_id={exact:.3f}")
    assert paraphrase >= 0.9
    assert regressions == []
