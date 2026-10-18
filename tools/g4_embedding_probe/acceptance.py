"""End-to-end G4 acceptance through the real installed CLI.

Given an installed-layout prefix (``<prefix>/bin/open-brain`` with the v2 component
manifest, inventories and ``libexec/open-brain-embedding``) and a scratch directory, the
driver creates a disposable Brain with an explicit ``--data-dir``, imports the 200 frozen
corpus records as Markdown, enables and rebuilds local embedding, then runs every corpus
query through ``search-page`` in ``hybrid_required`` and ``lexical`` modes. It prints one
JSON receipt holding metrics only: no corpus text and no path outside the scratch
directory. Latency is the wall-clock time of one complete CLI invocation, so it includes
process start; ``process_start_ms`` records ``--version`` on the same host for context.

The ``whole_command`` section applies the D17/D18/D20 budgets to ``--version``, lexical and
``hybrid_required`` ``search-page``: warm runs reuse the prepared Brain, while each cold run
is the first command of a fresh process against a fresh copy of that Brain with no helper
stage. ``first_invocation_ms`` is the driver's first command; it is the first launch after
install only when the prefix was installed immediately before.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.g4_embedding_probe.corpus import CORPUS, Corpus, Query, load_corpus

RECEIPT_SCHEMA = "ob.g4-acceptance-receipt.v1"
TOP_K = 5
PARAPHRASE_MIN_HIT = 0.90
# D17 whole-command warm p95 budgets; D20 relaxed only hybrid search to 900 ms.
WARM_P95_MAX_MS = {"version": 500.0, "lexical": 500.0, "hybrid_required": 900.0}
COLD_MAX_MS = 3000.0
PROCESS_START_SAMPLES = 5
COMMAND_TIMEOUT = 900
WHOLE_COMMAND_RUNS = 20
WHOLE_COMMANDS = ("version", "lexical", "hybrid_required")
# Mirrors open_brain.services.embedding_helper.STAGE_PARTS (pinned by a test).
STAGE_PARTS = (".open-brain", "cache", "embedding-v1", "stage")


class AcceptanceError(RuntimeError):
    """The CLI refused or answered outside its documented shape."""


# Pure metrics -------------------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty sample."""
    if not values or not 0 < fraction <= 1:
        raise ValueError("percentile needs values and a fraction in (0, 1]")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def hit_at_k(ranked: Sequence[str], relevant: Sequence[str], k: int = TOP_K) -> bool:
    return any(item in relevant for item in ranked[:k])


def hit_rate(rankings: Mapping[str, Sequence[str]], queries: Sequence[Query]) -> float:
    if not queries:
        raise ValueError("hit rate needs queries")
    return sum(hit_at_k(rankings[q.id], q.relevant) for q in queries) / len(queries)


def regressions(
    hybrid: Mapping[str, Sequence[str]],
    lexical: Mapping[str, Sequence[str]],
    queries: Sequence[Query],
) -> list[str]:
    """Query IDs that lexical answers within the top K but hybrid does not."""
    return [
        q.id
        for q in queries
        if hit_at_k(lexical[q.id], q.relevant) and not hit_at_k(hybrid[q.id], q.relevant)
    ]


def latency_summary(latencies_ms: Sequence[float]) -> dict[str, float | int]:
    """The first query is cold; warm statistics use the rest."""
    if len(latencies_ms) < 2:
        raise ValueError("latency summary needs a cold and at least one warm query")
    warm = latencies_ms[1:]
    return {
        "cold_ms": round(latencies_ms[0], 1),
        "warm_count": len(warm),
        "warm_p50_ms": round(percentile(warm, 0.50), 1),
        "warm_p95_ms": round(percentile(warm, 0.95), 1),
        "warm_max_ms": round(max(warm), 1),
    }


def evaluate(
    corpus: Corpus,
    hybrid: Mapping[str, Sequence[str]],
    lexical: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    paraphrase = [q for q in corpus.queries if q.kind == "paraphrase"]
    exact = [q for q in corpus.queries if q.kind == "exact_id"]
    return {
        "k": TOP_K,
        "paraphrase_queries": len(paraphrase),
        "exact_id_queries": len(exact),
        "paraphrase_hit_at_5": round(hit_rate(hybrid, paraphrase), 4),
        "paraphrase_hit_at_5_lexical": round(hit_rate(lexical, paraphrase), 4),
        "exact_id_hit_at_5": round(hit_rate(hybrid, exact), 4),
        "exact_id_hit_at_5_lexical": round(hit_rate(lexical, exact), 4),
        "exact_id_regressions": regressions(hybrid, lexical, exact),
        "paraphrase_misses": [q.id for q in paraphrase if not hit_at_k(hybrid[q.id], q.relevant)],
    }


def criteria(
    metrics: Mapping[str, Any],
    latency: Mapping[str, Any],
    rebuild: Mapping[str, Any],
    health: Mapping[str, Any],
    record_count: int,
) -> dict[str, bool]:
    return {
        "paraphrase_hit_at_5_ge_0_90": metrics["paraphrase_hit_at_5"] >= PARAPHRASE_MIN_HIT,
        "exact_id_regressions_zero": not metrics["exact_id_regressions"],
        "warm_p95_le_900_ms": latency["warm_p95_ms"] <= WARM_P95_MAX_MS["hybrid_required"],
        "cold_le_3000_ms": latency["cold_ms"] <= COLD_MAX_MS,
        "rebuild_complete": rebuild.get("records") == record_count
        and rebuild.get("uncovered") == 0
        and rebuild.get("excluded_oversize") == 0,
        "health_ready_ready_enabled": (
            health.get("resource_state"),
            health.get("cache_state"),
            health.get("consent_state"),
        )
        == ("ready", "ready", "enabled"),
    }


def whole_command_summary(latencies_ms: Sequence[float]) -> dict[str, float | int]:
    return {
        "runs": len(latencies_ms),
        "p50_ms": round(percentile(latencies_ms, 0.50), 1),
        "p95_ms": round(percentile(latencies_ms, 0.95), 1),
        "max_ms": round(max(latencies_ms), 1),
    }


def whole_command_criteria(
    warm: Mapping[str, Mapping[str, Any]], cold: Mapping[str, Mapping[str, Any]]
) -> dict[str, bool]:
    result = {
        f"warm_{name}_p95_le_{WARM_P95_MAX_MS[name]:.0f}_ms": warm[name]["p95_ms"]
        <= WARM_P95_MAX_MS[name]
        for name in warm
    }
    result.update(
        {f"cold_{name}_p95_le_3000_ms": cold[name]["p95_ms"] <= COLD_MAX_MS for name in cold}
    )
    return result


def copy_brain_without_stage(source: Path, target: Path) -> None:
    """Copy a prepared Brain, leaving out the helper stage so the copy starts cold."""
    stage_parent = source.joinpath(*STAGE_PARTS[:-1])

    def ignore(directory: str, names: list[str]) -> list[str]:
        if Path(directory) == stage_parent and STAGE_PARTS[-1] in names:
            return [STAGE_PARTS[-1]]
        return []

    if not stage_parent.joinpath(STAGE_PARTS[-1]).is_dir():
        raise AcceptanceError("prepared Brain has no helper stage to leave out")
    shutil.copytree(source, target, symlinks=True, ignore=ignore)


def note_filename(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        raise AcceptanceError("record title has no filename characters")
    return f"{slug}.md"


# CLI driving --------------------------------------------------------------------------


class Cli:
    def __init__(self, executable: Path, scratch: Path) -> None:
        self.executable = executable
        self.data_dir = scratch / "brain"
        home = scratch / "home"
        home.mkdir(mode=0o700)
        self.environment = {
            "HOME": os.fspath(home),
            "PATH": os.defpath,
            "XDG_DATA_HOME": os.fspath(home / ".local/share"),
            "XDG_CONFIG_HOME": os.fspath(home / ".config"),
            "XDG_CACHE_HOME": os.fspath(home / ".cache"),
            "XDG_STATE_HOME": os.fspath(home / ".local/state"),
            "LANG": "C.UTF-8",
        }
        if "TMPDIR" in os.environ:
            self.environment["TMPDIR"] = os.environ["TMPDIR"]

    def run(self, *arguments: str, local: bool = True) -> tuple[Any, float]:
        command = [os.fspath(self.executable), *arguments]
        if local:
            command += ["--data-dir", os.fspath(self.data_dir), "--json"]
        started = time.perf_counter()
        completed = subprocess.run(  # noqa: S603 - fixed installed executable
            command,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        if completed.returncode != 0:
            code = "unknown"
            with contextlib.suppress(ValueError, KeyError, TypeError):
                code = json.loads(completed.stdout)["error"]["code"]
            raise AcceptanceError(f"{arguments[0]} exited {completed.returncode}: {code}")
        if not local:
            return completed.stdout, elapsed_ms
        return json.loads(completed.stdout), elapsed_ms


def _ranked_ids(page: Mapping[str, Any], ids_by_title: Mapping[str, str]) -> list[str]:
    if page.get("status") != "ok" or not isinstance(page.get("results"), list):
        raise AcceptanceError("search-page answered outside its shape")
    return [ids_by_title.get(item.get("title"), "unknown") for item in page["results"]]


def _whole_command(cli: Cli, name: str, query: str) -> float:
    if name == "version":
        return cli.run("--version", local=False)[1]
    page, elapsed = cli.run("search-page", query, "--mode", name, "--limit", str(TOP_K))
    if page.get("status") != "ok" or page.get("mode_used") is None:
        raise AcceptanceError("search-page answered outside its shape")
    return elapsed


def measure_whole_commands(
    cli: Cli, corpus: Corpus, scratch: Path, runs: int = WHOLE_COMMAND_RUNS
) -> dict[str, Any]:
    """Warm runs reuse the prepared Brain; each cold run gets a fresh HOME and Brain copy."""
    queries = [corpus.queries[index % len(corpus.queries)].text for index in range(runs)]
    warm = {
        name: whole_command_summary([_whole_command(cli, name, query) for query in queries])
        for name in WHOLE_COMMANDS
    }
    cold_root = scratch / "cold"
    cold_root.mkdir(mode=0o700)
    cold_samples: dict[str, list[float]] = {name: [] for name in WHOLE_COMMANDS}
    for index, query in enumerate(queries):
        for name in WHOLE_COMMANDS:
            run_root = cold_root / f"{name}-{index}"
            run_root.mkdir(mode=0o700)
            fresh = Cli(cli.executable, run_root)
            if name != "version":
                copy_brain_without_stage(cli.data_dir, fresh.data_dir)
            cold_samples[name].append(_whole_command(fresh, name, query))
            shutil.rmtree(run_root)
    cold = {name: whole_command_summary(samples) for name, samples in cold_samples.items()}
    return {
        "measure": "wall_clock_per_cli_invocation",
        "warm": warm,
        "cold": cold,
        "criteria": whole_command_criteria(warm, cold),
    }


def run_acceptance(
    prefix: Path,
    scratch: Path,
    corpus_path: Path = CORPUS,
    runs: int = WHOLE_COMMAND_RUNS,
) -> dict[str, Any]:
    corpus = load_corpus(corpus_path)
    executable = prefix / "bin" / "open-brain"
    cli = Cli(executable, scratch)
    started = time.perf_counter()

    version, first_invocation_ms = cli.run("--version", local=False)
    process_start = [cli.run("--version", local=False)[1] for _ in range(PROCESS_START_SAMPLES)]

    notes = scratch / "notes"
    notes.mkdir(mode=0o700)
    ids_by_title: dict[str, str] = {}
    for record_id, (title, body) in corpus.records.items():
        target = notes / note_filename(title)
        if target.exists():
            raise AcceptanceError("record titles collide as filenames")
        target.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        ids_by_title[title] = record_id
    imported, import_ms = cli.run("import", os.fspath(notes), "--yes")
    if imported.get("status") != "completed" or imported.get("imported") != len(corpus.records):
        raise AcceptanceError("corpus import did not complete")

    before, _ = cli.run("embedding", "status")
    component_id = before["embedding_health"]["component_id"]
    if before["embedding_health"]["resource_state"] != "ready" or not component_id:
        raise AcceptanceError("embedding resource is not ready before enable")
    enabled, _ = cli.run("embedding", "enable", "--component-id", component_id)
    if enabled.get("enabled") is not True:
        raise AcceptanceError("embedding enable did not take effect")
    rebuild, rebuild_ms = cli.run("embedding", "rebuild")

    rankings: dict[str, dict[str, list[str]]] = {"hybrid_required": {}, "lexical": {}}
    latencies: dict[str, list[float]] = {"hybrid_required": [], "lexical": []}
    modes_used: dict[str, set[str]] = {"hybrid_required": set(), "lexical": set()}
    for mode in ("hybrid_required", "lexical"):
        for query in corpus.queries:
            page, elapsed = cli.run(
                "search-page", query.text, "--mode", mode, "--limit", str(TOP_K)
            )
            rankings[mode][query.id] = _ranked_ids(page, ids_by_title)
            latencies[mode].append(elapsed)
            modes_used[mode].add(str(page.get("mode_used")))

    whole_command = measure_whole_commands(cli, corpus, scratch, runs)
    after, _ = cli.run("embedding", "status")
    health = after["embedding_health"]
    metrics = evaluate(corpus, rankings["hybrid_required"], rankings["lexical"])
    latency: dict[str, Any] = {
        "measure": "wall_clock_per_cli_invocation",
        "hybrid_required": latency_summary(latencies["hybrid_required"]),
        "lexical": latency_summary(latencies["lexical"]),
        "process_start_ms": {
            "samples": PROCESS_START_SAMPLES,
            "p50": round(percentile(process_start, 0.50), 1),
            "max": round(max(process_start), 1),
        },
    }
    rebuild_summary = {
        key: rebuild.get(key)
        for key in ("records", "chunks", "dropped_changed", "uncovered", "excluded_oversize")
    }
    rebuild_summary["helper_seconds"] = rebuild.get("seconds")
    rebuild_summary["wall_ms"] = round(rebuild_ms, 1)
    return {
        "schema": RECEIPT_SCHEMA,
        "platform": f"{platform.system().lower()}-{platform.machine()}",
        "version": version.strip(),
        "first_invocation_ms": round(first_invocation_ms, 1),
        "whole_command": whole_command,
        "corpus": {
            "schema": "ob.g4-relevance-corpus.v1",
            "sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
            "records": len(corpus.records),
            "queries": len(corpus.queries),
        },
        "import": {"imported": imported.get("imported"), "wall_ms": round(import_ms, 1)},
        "component_id": component_id,
        "rebuild": rebuild_summary,
        "modes_used": {mode: sorted(used) for mode, used in modes_used.items()},
        "metrics": metrics,
        "latency": latency,
        "embedding_health": {
            key: health.get(key)
            for key in ("resource_state", "cache_state", "consent_state", "last_error")
        },
        "health_component_matches": health.get("component_id") == component_id,
        "criteria": criteria(
            metrics, latency["hybrid_required"], rebuild, health, len(corpus.records)
        ),
        "total_seconds": round(time.perf_counter() - started, 1),
    }


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.g4_embedding_probe.acceptance")
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True, help="Empty, owner-only directory.")
    parser.add_argument("--runs", type=int, default=WHOLE_COMMAND_RUNS)
    namespace = parser.parse_args(argv)
    if namespace.runs < 1:
        parser.error("--runs must be positive")
    prefix = namespace.prefix.resolve(strict=True)
    scratch = namespace.scratch.resolve(strict=True)
    if any(scratch.iterdir()):
        print("acceptance: scratch directory must be empty", file=sys.stderr)
        return 2
    try:
        receipt = run_acceptance(prefix, scratch, runs=namespace.runs)
    except (AcceptanceError, OSError, subprocess.TimeoutExpired, ValueError) as error:
        print(f"acceptance: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
