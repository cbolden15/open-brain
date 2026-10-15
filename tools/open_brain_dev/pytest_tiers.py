"""Opt-in, measured Python test tiers for development.

Load with ``-p tools.open_brain_dev.pytest_tiers``. Generate the manifest with
``generate NODES_JSON --output PATH``; run ``audit [--baseline IDS_FILE]`` from
the pytest root directory. List hashes use sorted unique IDs, UTF-8 encoded,
with one newline per ID (the empty list hashes empty bytes).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

MANIFEST = Path("tests/tiers/integration.txt")
PLUGIN = "tools.open_brain_dev.pytest_tiers"


def function_id(nodeid: str) -> str:
    """Remove parametrization only from the final node ID segment."""
    prefix, separator, final = nodeid.rpartition("::")
    return prefix + separator + final.split("[", 1)[0]


def read_ids(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: measured process, sleep, or slow test")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    manifest = read_ids(config.rootpath / MANIFEST)
    for item in items:
        if function_id(item.nodeid) in manifest and item.get_closest_marker("integration") is None:
            item.add_marker(pytest.mark.integration)


def generate(nodes_json: Path, output: Path) -> None:
    source = nodes_json.read_bytes()
    nodes: dict[str, dict[str, float]] = json.loads(source)
    integration = sorted(
        {
            function_id(nodeid)
            for nodeid, totals in nodes.items()
            if totals["spawns"] > 0 or totals["sleep_calls"] > 0 or totals["total"] >= 0.5
        }
    )
    header = (
        "# Integration if ANY parametrization has spawns > 0, sleep_calls > 0, "
        "or total >= 0.5 s.\n"
        f"# Input sha256: {hashlib.sha256(source).hexdigest()}\n"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(header + "".join(f"{nodeid}\n" for nodeid in integration), encoding="utf-8")


def _summary(ids: Iterable[str], *, include_ids: bool = False) -> dict[str, Any]:
    ordered = sorted(set(ids))
    result: dict[str, Any] = {
        "count": len(ordered),
        "sha256": hashlib.sha256("".join(f"{nodeid}\n" for nodeid in ordered).encode()).hexdigest(),
    }
    if include_ids:
        result["ids"] = ordered
    return result


def _collect(rootdir: Path, expression: str | None) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        PLUGIN,
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    if expression is not None:
        command.extend(["-m", expression])
    return subprocess.run(command, cwd=rootdir, capture_output=True, text=True, timeout=120)


def audit(rootdir: Path, baseline: Path | None = None) -> dict[str, Any]:
    collections: dict[str, set[str]] = {}
    errors: dict[str, dict[str, Any]] = {}
    for name, expression in (
        ("all", None),
        ("integration", "integration"),
        ("fast", "not integration"),
    ):
        completed = _collect(rootdir, expression)
        # Quiet pytest collection emits one unindented node ID per line.
        collections[name] = {
            line for line in completed.stdout.splitlines() if "::" in line and not line[0].isspace()
        }
        if completed.returncode not in (0, 5):
            errors[name] = {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
    all_ids = collections["all"]
    integration = collections["integration"]
    fast = collections["fast"]
    union = integration | fast
    overlap = integration & fast
    missing = all_ids - union
    unexpected = union - all_ids
    missing_baseline = read_ids(baseline) - all_ids if baseline is not None else set()
    stale = read_ids(rootdir / MANIFEST) - {function_id(nodeid) for nodeid in all_ids}
    return {
        **{name: _summary(ids) for name, ids in collections.items()},
        "union": _summary(union),
        "disjoint": not overlap,
        "union_equals_all": union == all_ids,
        "overlap": _summary(overlap, include_ids=True),
        "missing_from_tiers": _summary(missing, include_ids=True),
        "unexpected_in_tiers": _summary(unexpected, include_ids=True),
        "missing_baseline": _summary(missing_baseline, include_ids=True),
        "stale_manifest": _summary(stale, include_ids=True),
        "collection_errors": errors,
        "ok": not (errors or overlap or missing or unexpected or missing_baseline or stale),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generator = commands.add_parser("generate", help="classify a measured node profile")
    generator.add_argument("nodes_json", type=Path)
    generator.add_argument("--output", type=Path, required=True)
    auditor = commands.add_parser(
        "audit", help="verify collection partition and manifest freshness"
    )
    auditor.add_argument("--baseline", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "generate":
            generate(arguments.nodes_json, arguments.output)
            return 0
        result = audit(Path.cwd(), arguments.baseline)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        json.dump({"ok": False, "error": str(error)}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
