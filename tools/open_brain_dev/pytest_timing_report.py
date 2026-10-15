"""Summarize records written by ``tools.open_brain_dev.pytest_timing``.

Usage: ``python -m tools.open_brain_dev.pytest_timing_report DIRECTORY [--top N]
[--nodes-out PATH]``. Prints one JSON summary that separates collection, cumulative
setup/call/teardown time, per-worker busy and idle time, and session wall time.
``--nodes-out`` writes per-node phase totals for comparisons and tier audits.
Every record must identify the same run; missing or mixed run identifiers are
rejected. Fixture totals are setup hook elapsed time and can overlap when a
fixture requests a dependency dynamically with ``request.getfixturevalue()``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

PHASES = ("setup", "call", "teardown")
MEASURES = (
    "cpu",
    "child_cpu",
    "spawns",
    "sqlite_connects",
    "sleep_calls",
    "sleep_seconds",
    "voluntary_switches",
)


def _records(directory: Path) -> Iterable[dict[str, Any]]:
    for path in sorted(directory.glob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def _round(value: float) -> float:
    return round(value, 3)


def _node_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def summarize(directory: Path, top: int) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    run: str | None = None
    nodes: dict[str, dict[str, Any]] = {}
    fixtures: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    collection: dict[str, float] = {}
    worker_collected: dict[str, float] = {}
    sessions: dict[str, dict[str, Any]] = {}
    worker_busy: dict[str, float] = defaultdict(float)
    worker_last_stop: dict[str, float] = {}
    worker_first_start: dict[str, float] = {}
    for record in _records(directory):
        record_run = record.get("run")
        if not isinstance(record_run, str) or not record_run:
            raise ValueError("Timing records require a non-empty run identifier on every record")
        if run is not None and record_run != run:
            raise ValueError(
                "Timing directory contains multiple run identifiers; use one fresh run"
            )
        run = record_run
        kind = record["kind"]
        worker = str(record.get("worker", "main"))
        if kind == "phase":
            node = nodes.setdefault(
                record["nodeid"],
                {"worker": worker, "outcome": "passed", **{p: 0.0 for p in PHASES}},
            )
            node[record["when"]] = node[record["when"]] + record["duration"]
            for measure in MEASURES:
                node[measure] = node.get(measure, 0) + record.get(measure, 0)
            if record["outcome"] != "passed":
                node["outcome"] = record["outcome"]
            worker_busy[worker] += record["duration"]
            worker_first_start[worker] = min(
                worker_first_start.get(worker, record["start"]), record["start"]
            )
            worker_last_stop[worker] = max(worker_last_stop.get(worker, 0.0), record["stop"])
        elif kind == "fixture":
            totals = fixtures[(record["argname"], record["scope"], record["baseid"])]
            totals[0] += record["count"]
            totals[1] += record["seconds"]
            totals[2] += record["cpu"]
            totals[3] = max(totals[3], record["max_seconds"])
        elif kind == "collection":
            collection[worker] = record["seconds"]
        elif kind == "worker_collected":
            worker_collected[worker] = record["seconds"]
        elif kind == "session_end":
            sessions[worker] = record
    if run is None:
        raise ValueError("No timing run identifier found; directory contains no timing records")
    for node in nodes.values():
        node["total"] = node["setup"] + node["call"] + node["teardown"]
    phase_totals = {p: _round(sum(n[p] for n in nodes.values())) for p in PHASES}
    cumulative = sum(n["total"] for n in nodes.values())
    cpu = sum(n.get("cpu", 0.0) for n in nodes.values())
    child_cpu = sum(n.get("child_cpu", 0.0) for n in nodes.values())
    by_file: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for nodeid, node in nodes.items():
        entry = by_file[_node_file(nodeid)]
        entry["tests"] += 1
        for measure in ("total", "setup", "call", "teardown", *MEASURES):
            entry[measure] += node.get(measure, 0.0)
    ordered = sorted(nodes.items(), key=lambda item: item[1]["total"], reverse=True)
    shares = {}
    for fraction in (0.01, 0.05, 0.10):
        count = max(1, int(len(ordered) * fraction))
        shares[f"top_{int(fraction * 100)}pct_tests_share"] = _round(
            sum(n["total"] for _, n in ordered[:count]) / cumulative if cumulative else 0.0
        )
    buckets = {"lt_0.1s": 0, "0.1_1s": 0, "1_5s": 0, "5_20s": 0, "ge_20s": 0}
    for _, node in ordered:
        total = node["total"]
        if total < 0.1:
            buckets["lt_0.1s"] += 1
        elif total < 1:
            buckets["0.1_1s"] += 1
        elif total < 5:
            buckets["1_5s"] += 1
        elif total < 20:
            buckets["5_20s"] += 1
        else:
            buckets["ge_20s"] += 1
    outcomes: dict[str, int] = defaultdict(int)
    for node in nodes.values():
        outcomes[node["outcome"]] += 1
    workers = sorted(worker_busy)
    controller = sessions.get("main")
    summary: dict[str, Any] = {
        "run": run,
        "tests": len(nodes),
        "outcomes": dict(sorted(outcomes.items())),
        "session_wall_seconds": _round(controller["wall"]) if controller else None,
        "collection_seconds": {w: _round(s) for w, s in sorted(collection.items())},
        "worker_collected_at_seconds": {w: _round(s) for w, s in sorted(worker_collected.items())},
        "cumulative_phase_seconds": phase_totals,
        "cumulative_seconds": _round(cumulative),
        "cpu_seconds": {"worker_processes": _round(cpu), "reaped_children": _round(child_cpu)},
        "spawns": int(sum(n.get("spawns", 0) for n in nodes.values())),
        "sleep_seconds": _round(sum(n.get("sleep_seconds", 0.0) for n in nodes.values())),
        "workers": {
            w: {
                "busy_seconds": _round(worker_busy[w]),
                "span_seconds": _round(worker_last_stop[w] - worker_first_start[w]),
                "idle_in_span_seconds": _round(
                    worker_last_stop[w] - worker_first_start[w] - worker_busy[w]
                ),
                "session_wall_seconds": _round(sessions[w]["wall"]) if w in sessions else None,
            }
            for w in workers
        },
        "duration_buckets": buckets,
        **shares,
        "top_nodes": [
            {
                "nodeid": nodeid,
                **{
                    k: _round(v) if isinstance(v, float) else v
                    for k, v in node.items()
                    if k != "worker"
                },
            }
            for nodeid, node in ordered[:top]
        ],
        "top_files": [
            {"file": name, **{k: _round(v) for k, v in values.items()}}
            for name, values in sorted(by_file.items(), key=lambda i: i[1]["total"], reverse=True)[
                :top
            ]
        ],
        "top_fixtures": [
            {
                "argname": argname,
                "scope": scope,
                "baseid": baseid,
                "count": int(count),
                "seconds": _round(seconds),
                "cpu": _round(fixture_cpu),
                "max_seconds": _round(longest),
            }
            for (argname, scope, baseid), (count, seconds, fixture_cpu, longest) in sorted(
                fixtures.items(), key=lambda item: item[1][1], reverse=True
            )[:top]
        ],
        "top_sleep_files": [
            {
                "file": name,
                "sleep_seconds": _round(values["sleep_seconds"]),
                "total": _round(values["total"]),
            }
            for name, values in sorted(
                by_file.items(), key=lambda i: i[1]["sleep_seconds"], reverse=True
            )[:top]
        ],
    }
    return summary, nodes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--nodes-out", type=Path)
    arguments = parser.parse_args(argv)
    try:
        summary, nodes = summarize(arguments.directory, arguments.top)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if arguments.nodes_out is not None:
        arguments.nodes_out.write_text(json.dumps(nodes, indent=1, sort_keys=True) + "\n")
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
