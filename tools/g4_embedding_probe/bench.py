"""Measure the embedding helper against the G4 product budgets.

Default staging follows decision D7: one persistent private stage per component,
created from verified source bytes, with every staged byte re-hashed before each helper
launch. ``--per-invocation`` reproduces the original section 4 model (verify sources and
copy a fresh stage for every query). Each query then starts one helper process, sends
one ``embed`` request and validates the response. A rebuild runs bounded document
exchanges, re-hashing before each, until the reference chunk count is reached. Child
peak RSS comes from ``os.wait4``. Inputs are synthetic corpus text only.

"Cold" is stage creation plus the first query (first use after install). The operating
system page cache is not purged (that needs administrator rights), so cold numbers are
a lower bound.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from tools.g4_embedding_probe.corpus import CORPUS
from tools.g4_embedding_probe.stage import MODEL_FILES, stage

PROTOCOL = "open-brain.embedding-helper.v1"
DOCUMENT_TARGET_BYTES = 26_000
DOCUMENTS_PER_EXCHANGE = 4
REFERENCE_CHUNKS = 10_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _request(component_id: str, purpose: str, texts: list[str], serial: int) -> bytes:
    body = {
        "protocol": PROTOCOL,
        "request_id": f"{serial:032x}",
        "operation": "embed",
        "component_id": component_id,
        "purpose": purpose,
        "items": [{"slot": i, "text": t} for i, t in enumerate(texts)],
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def _exchange(staged: Path, payload: bytes) -> tuple[dict[str, Any], int]:
    """Run one helper exchange; return the response and child peak RSS in bytes."""
    child = subprocess.Popen(
        [str(staged / "open-brain-embedding"), "--stdio-v1"],
        cwd=staged,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    assert child.stdin is not None and child.stdout is not None
    child.stdin.write(payload)
    child.stdin.close()
    output = child.stdout.read(1_048_577)
    child.stdout.close()
    _, status, usage = os.wait4(child.pid, 0)
    child.returncode = os.waitstatus_to_exitcode(status)
    rss = usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024
    response: dict[str, Any] = json.loads(output)
    if child.returncode != 0 or response.get("status") != "ok":
        raise RuntimeError(f"helper failed: exit {child.returncode} {response.get('error')}")
    return response, rss


def _verify_sources(helper: Path, assets: Path) -> None:
    for path in (helper, *(assets / rel for rel in MODEL_FILES.values())):
        _sha256(path)


def _stage(root: Path, serial: int, helper: Path, assets: Path) -> tuple[Path, str]:
    _verify_sources(helper, assets)
    staged = stage(root / f"stage-{serial}", helper=helper, assets=assets)
    return staged, _sha256(staged / "component.json")


def _staged_hashes(staged: Path) -> dict[str, str]:
    """Re-hash every staged byte, as the product does before each launch (decision D7)."""
    return {
        str(path.relative_to(staged)): _sha256(path)
        for path in sorted(staged.rglob("*"))
        if path.is_file()
    }


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def run(
    *,
    helper: Path,
    assets: Path,
    scratch: Path,
    query_runs: int,
    rebuild: bool = True,
    per_invocation: bool = False,
) -> dict[str, Any]:
    document = json.loads(CORPUS.read_text(encoding="utf-8"))
    queries = [q["text"] for q in document["queries"]]
    bodies = [r["body"] for r in document["records"]]
    root = Path(tempfile.mkdtemp(prefix="g4-bench-", dir=scratch))
    serial = 0
    try:
        cold: list[float] = []
        warm: list[float] = []
        stage_ms: list[float] = []
        exchange_ms: list[float] = []
        peak = 0
        began = time.perf_counter()
        persistent, component_id = _stage(root, serial, helper, assets)
        pinned = _staged_hashes(persistent)
        stage_create_ms = (time.perf_counter() - began) * 1000
        installed = sum(p.stat().st_size for p in persistent.rglob("*") if p.is_file())
        for index in range(query_runs):
            if per_invocation:
                began = time.perf_counter()
                staged, component_id = _stage(root, serial + 1, helper, assets)
            else:
                if index:
                    began = time.perf_counter()
                staged = persistent
                if _staged_hashes(staged) != pinned:
                    raise RuntimeError("staged bytes changed")
            staged_at = time.perf_counter()
            response, rss = _exchange(
                staged, _request(component_id, "query", [queries[index % len(queries)]], serial)
            )
            finished = time.perf_counter()
            elapsed = (finished - began) * 1000
            stage_ms.append((staged_at - began) * 1000)
            exchange_ms.append((finished - staged_at) * 1000)
            if per_invocation:
                shutil.rmtree(staged)
            serial += 1
            assert len(response["items"][0]["chunks"][0]["vector"]) == 384
            (cold if index == 0 else warm).append(elapsed)
            peak = max(peak, rss)

        summary: dict[str, Any] = {
            "schema": "ob.g4-bench.v2",
            "platform": sys.platform,
            "staging": "per-invocation" if per_invocation else "persistent-verified",
            "stage_create_ms": round(stage_create_ms, 1),
        }
        if not rebuild:
            return summary | {
                "query_runs": query_runs,
                "cold_ms": round(cold[0], 1),
                "warm_ms_p50": round(percentile(warm, 0.5), 1),
                "warm_ms_p95": round(percentile(warm, 0.95), 1),
                "stage_ms_p50": round(percentile(stage_ms, 0.5), 1),
                "exchange_ms_p50": round(percentile(exchange_ms, 0.5), 1),
                "child_peak_rss_mib": round(peak / 1024 / 1024, 1),
                "staged_mib": round(installed / 1024 / 1024, 1),
            }
        began = time.perf_counter()
        staged = persistent
        long_texts: list[str] = []
        cursor = 0
        while len(long_texts) < 64:
            text = ""
            while len(text.encode()) < DOCUMENT_TARGET_BYTES:
                text += bodies[cursor % len(bodies)] + "\n\n"
                cursor += 1
            long_texts.append(text)
        chunks = 0
        exchanges = 0
        while chunks < REFERENCE_CHUNKS:
            batch = [
                long_texts[(exchanges * DOCUMENTS_PER_EXCHANGE + i) % len(long_texts)]
                for i in range(DOCUMENTS_PER_EXCHANGE)
            ]
            if _staged_hashes(staged) != pinned:
                raise RuntimeError("staged bytes changed")
            response, rss = _exchange(staged, _request(component_id, "document", batch, serial))
            serial += 1
            exchanges += 1
            chunks += sum(len(item["chunks"]) for item in response["items"])
            peak = max(peak, rss)
        rebuild_seconds = time.perf_counter() - began
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return summary | {
        "query_runs": query_runs,
        "cold_ms": round(cold[0], 1),
        "warm_ms_p50": round(percentile(warm, 0.5), 1),
        "warm_ms_p95": round(percentile(warm, 0.95), 1),
        "stage_ms_p50": round(percentile(stage_ms, 0.5), 1),
        "exchange_ms_p50": round(percentile(exchange_ms, 0.5), 1),
        "rebuild_chunks": chunks,
        "rebuild_exchanges": exchanges,
        "rebuild_seconds": round(rebuild_seconds, 1),
        "child_peak_rss_mib": round(peak / 1024 / 1024, 1),
        "staged_bytes": installed,
        "staged_mib": round(installed / 1024 / 1024, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--query-runs", type=int, default=41)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-rebuild", action="store_true")
    parser.add_argument("--per-invocation", action="store_true", help="section 4 original model")
    args = parser.parse_args(argv)
    if args.output.exists():
        print(f"refusing existing output {args.output}", file=sys.stderr)
        return 2
    result = run(
        helper=args.helper.resolve(),
        assets=args.assets.resolve(),
        scratch=args.scratch.resolve(),
        query_runs=args.query_runs,
        rebuild=not args.skip_rebuild,
        per_invocation=args.per_invocation,
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
