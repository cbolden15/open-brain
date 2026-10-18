"""Pure metrics of the G4 CLI acceptance driver on a tiny synthetic fixture."""

import os
from pathlib import Path

import pytest

from open_brain.services import embedding_helper
from tools.g4_embedding_probe.acceptance import (
    STAGE_PARTS,
    AcceptanceError,
    Cli,
    copy_brain_without_stage,
    criteria,
    evaluate,
    hit_at_k,
    latency_summary,
    measure_whole_commands,
    note_filename,
    percentile,
    regressions,
    whole_command_criteria,
    whole_command_summary,
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


def test_stage_parts_match_the_product() -> None:
    assert STAGE_PARTS == embedding_helper.STAGE_PARTS


def test_whole_command_summary_and_criteria() -> None:
    summary = whole_command_summary([float(v) for v in range(1, 21)])
    assert summary == {"runs": 20, "p50_ms": 10.0, "p95_ms": 19.0, "max_ms": 20.0}
    warm = {"version": {"p95_ms": 500.0}, "hybrid_required": {"p95_ms": 500.1}}
    cold = {"lexical": {"p95_ms": 3000.0}, "hybrid_required": {"p95_ms": 3000.1}}
    assert whole_command_criteria(warm, cold) == {
        "warm_version_p95_le_500_ms": True,
        "warm_hybrid_required_p95_le_500_ms": False,
        "cold_lexical_p95_le_3000_ms": True,
        "cold_hybrid_required_p95_le_3000_ms": False,
    }


def _prepared_brain(root: Path) -> Path:
    stage = root.joinpath(*STAGE_PARTS)
    stage.mkdir(parents=True)
    (stage / "helper").write_bytes(b"x")
    (root / "brain.sqlite").write_bytes(b"db")
    (root / "notes").symlink_to("brain.sqlite")
    return root


def test_copy_brain_leaves_out_only_the_stage(tmp_path: Path) -> None:
    source = _prepared_brain(tmp_path / "source")
    os.chmod(source / "brain.sqlite", 0o600)
    copy_brain_without_stage(source, tmp_path / "copy")
    copy = tmp_path / "copy"
    assert not copy.joinpath(*STAGE_PARTS).exists()
    assert copy.joinpath(*STAGE_PARTS[:-1]).is_dir()
    assert (copy / "brain.sqlite").read_bytes() == b"db"
    assert (copy / "brain.sqlite").stat().st_mode & 0o777 == 0o600
    assert os.readlink(copy / "notes") == "brain.sqlite"
    assert source.joinpath(*STAGE_PARTS, "helper").exists()


def test_copy_brain_refuses_a_brain_without_a_stage(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    with pytest.raises(AcceptanceError):
        copy_brain_without_stage(tmp_path / "source", tmp_path / "copy")


FAKE_CLI = """#!/bin/sh
[ "$1" = "--version" ] && {{ echo "open-brain 0.1.0"; exit 0; }}
[ -d "$8/{stage}" ] && echo staged >> "{log}" || echo cold >> "{log}"
echo '{{"status":"ok","mode_used":"'"$4"'","results":[]}}'
"""


def test_measure_whole_commands_runs_cold_commands_without_a_stage(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    executable = tmp_path / "open-brain"
    executable.write_text(FAKE_CLI.format(stage="/".join(STAGE_PARTS), log=log))
    executable.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cli = Cli(executable, scratch)
    _prepared_brain(cli.data_dir)
    result = measure_whole_commands(cli, CORPUS, scratch, runs=3)
    assert set(result["warm"]) == set(result["cold"]) == {"version", "lexical", "hybrid_required"}
    assert all(
        summary["runs"] == 3 for summary in (*result["warm"].values(), *result["cold"].values())
    )
    assert set(result["criteria"]) == {
        f"{kind}_{name}_p95_le_{limit}_ms"
        for kind, limit in (("warm", 500), ("cold", 3000))
        for name in ("version", "lexical", "hybrid_required")
    }
    # Six warm searches see the prepared stage; six cold searches see none.
    assert log.read_text().split() == ["staged"] * 6 + ["cold"] * 6
    assert list((scratch / "cold").iterdir()) == []
