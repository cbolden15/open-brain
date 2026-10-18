"""Pure metrics of the G4 CLI acceptance driver on a tiny synthetic fixture."""

import pytest

from tools.g4_embedding_probe.acceptance import (
    AcceptanceError,
    criteria,
    evaluate,
    hit_at_k,
    latency_summary,
    note_filename,
    percentile,
    regressions,
)
from tools.g4_embedding_probe.corpus import Corpus, Query

QUERIES = (
    Query("p01", "paraphrase", "synthetic one", ("r001",)),
    Query("p02", "paraphrase", "synthetic two", ("r002", "r003")),
    Query("e01", "exact_id", "ID-0001", ("r004",)),
    Query("e02", "exact_id", "ID-0002", ("r005",)),
)
CORPUS = Corpus(records={f"r00{i}": (f"t{i}", "b") for i in range(1, 7)}, queries=QUERIES)


def test_percentile_is_nearest_rank() -> None:
    values = [float(v) for v in range(1, 21)]
    assert percentile(values, 0.50) == 10.0
    assert percentile(values, 0.95) == 19.0
    assert percentile([7.0], 0.95) == 7.0
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_hit_at_k_only_counts_the_top_k() -> None:
    assert hit_at_k(["a", "b", "r001"], ("r001",))
    assert not hit_at_k(["a", "b", "c", "d", "e", "r001"], ("r001",))
    assert not hit_at_k([], ("r001",))


def test_evaluate_and_regressions() -> None:
    hybrid = {
        "p01": ["r001"],
        "p02": ["r006", "r003"],
        "e01": ["r006"],
        "e02": ["r005"],
    }
    lexical = {"p01": ["r006"], "p02": ["r006"], "e01": ["r004"], "e02": []}
    metrics = evaluate(CORPUS, hybrid, lexical)
    assert metrics["paraphrase_hit_at_5"] == 1.0
    assert metrics["paraphrase_hit_at_5_lexical"] == 0.0
    assert metrics["exact_id_hit_at_5"] == 0.5
    assert metrics["exact_id_hit_at_5_lexical"] == 0.5
    assert metrics["exact_id_regressions"] == ["e01"]
    assert metrics["paraphrase_misses"] == []
    exact = [q for q in QUERIES if q.kind == "exact_id"]
    assert regressions(lexical, lexical, exact) == []


def test_latency_summary_excludes_the_cold_query() -> None:
    summary = latency_summary([2500.0, 100.0, 200.0, 300.0])
    assert summary["cold_ms"] == 2500.0
    assert summary["warm_count"] == 3
    assert summary["warm_p50_ms"] == 200.0
    assert summary["warm_p95_ms"] == 300.0
    with pytest.raises(ValueError):
        latency_summary([1.0])


def test_criteria_report_each_gate() -> None:
    metrics = {"paraphrase_hit_at_5": 0.9, "exact_id_regressions": []}
    latency = {"warm_p95_ms": 500.0, "cold_ms": 3000.1}
    rebuild = {"records": 6, "uncovered": 0, "excluded_oversize": 0}
    health = {"resource_state": "ready", "cache_state": "ready", "consent_state": "enabled"}
    assert criteria(metrics, latency, rebuild, health, 6) == {
        "paraphrase_hit_at_5_ge_0_90": True,
        "exact_id_regressions_zero": True,
        "warm_p95_le_500_ms": True,
        "cold_le_3000_ms": False,
        "rebuild_complete": True,
        "health_ready_ready_enabled": True,
    }
    stale = {**health, "cache_state": "stale"}
    assert not criteria(metrics, latency, {**rebuild, "records": 5}, stale, 6)["rebuild_complete"]
    assert not criteria(metrics, latency, rebuild, stale, 6)["health_ready_ready_enabled"]


def test_note_filename_is_a_title_slug() -> None:
    assert note_filename("Portuguese verbs: finally stuck!") == "portuguese-verbs-finally-stuck.md"
    with pytest.raises(AcceptanceError):
        note_filename("!!!")
