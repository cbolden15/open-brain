"""Isolated pytest projects and synthetic records for opt-in timing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.open_brain_dev import pytest_timing, pytest_timing_report

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "test_sample.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', range(4))\n"
        "def test_sample(value):\n    assert value >= 0\n",
        encoding="utf-8",
    )
    return root


def _run(
    project: Path, directory: Path | None, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop(pytest_timing.TIMING_DIRECTORY_ENVIRONMENT, None)
    environment.pop("PYTEST_PLUGINS", None)
    if directory is not None:
        environment[pytest_timing.TIMING_DIRECTORY_ENVIRONMENT] = str(directory)
    environment["PYTHONPATH"] = str(REPOSITORY)
    environment["PYTEST_ADDOPTS"] = ""
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tools.open_brain_dev.pytest_timing",
            *arguments,
        ],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _records(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted(directory.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


@pytest.mark.integration
def test_inactive_without_environment_variable(project: Path, tmp_path: Path) -> None:
    completed = _run(project, None)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "4 passed" in completed.stdout
    assert not list(tmp_path.rglob("*.jsonl"))


@pytest.mark.integration
@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "xdist"])
def test_one_run_and_no_duplicate_tests(project: Path, tmp_path: Path, parallel: bool) -> None:
    directory = tmp_path / "timing"
    directory.mkdir()
    (directory / "unrelated.txt").write_text("keep", encoding="utf-8")
    arguments = ("-p", "xdist.plugin", "-n", "2") if parallel else ()
    completed = _run(project, directory, *arguments)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "4 passed" in completed.stdout
    records = _records(directory)
    runs = {record["run"] for record in records}
    assert len(runs) == 1
    assert all(isinstance(run, str) and run for run in runs)
    sessions = [record for record in records if record["kind"] == "session"]
    assert {record["worker"] for record in sessions} == (
        {"main", "gw0", "gw1"} if parallel else {"main"}
    )
    phases = [record for record in records if record["kind"] == "phase"]
    assert len(phases) == 12
    assert len({(record["nodeid"], record["when"]) for record in phases}) == 12
    summary, nodes = pytest_timing_report.summarize(directory, top=10)
    assert summary["tests"] == len(nodes) == 4
    assert summary["outcomes"] == {"passed": 4}
    assert summary["run"] in runs


@pytest.mark.integration
@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "xdist"])
def test_refuse_existing_jsonl(project: Path, tmp_path: Path, parallel: bool) -> None:
    directory = tmp_path / "timing"
    directory.mkdir()
    existing = directory / "previous.jsonl"
    existing.touch()
    arguments = ("-p", "xdist.plugin", "-n", "2") if parallel else ()
    completed = _run(project, directory, *arguments)
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert "fresh directory" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert list(directory.iterdir()) == [existing]
    assert existing.read_bytes() == b""


def test_summary_rejects_mixed_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for run in ("run-a", "run-b"):
        _write_records(
            tmp_path / f"{run}.jsonl",
            [{"kind": "session_end", "worker": "main", "wall": 1.0, "run": run}],
        )
    with pytest.raises(ValueError, match="multiple run identifiers"):
        pytest_timing_report.summarize(tmp_path, top=10)
    nodes_out = tmp_path / "nodes.json"
    assert pytest_timing_report.main([str(tmp_path), "--nodes-out", str(nodes_out)]) == 1
    captured = capsys.readouterr()
    assert "multiple run identifiers" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert not nodes_out.exists()


@pytest.mark.parametrize("invalid", ["empty", "missing", "null", "blank", "partly-missing"])
def test_summary_requires_run_identifiers(tmp_path: Path, invalid: str) -> None:
    records: list[dict[str, Any]] = []
    if invalid != "empty":
        record: dict[str, Any] = {"kind": "session", "worker": "main"}
        if invalid in {"null", "blank"}:
            record["run"] = None if invalid == "null" else ""
        if invalid == "partly-missing":
            records.append({**record, "run": "run-a"})
        records.append(record)
    _write_records(tmp_path / "records.jsonl", records)
    with pytest.raises(ValueError, match="run identifier"):
        pytest_timing_report.summarize(tmp_path, top=10)


def test_summary_arithmetic(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    for worker, nodeid, start, durations in (
        ("gw0", "test_sample.py::test_a", 10.0, (0.5, 2.0, 0.5)),
        ("gw1", "test_sample.py::test_b", 10.0, (1.0, 4.0, 1.0)),
    ):
        for when, duration in zip(pytest_timing_report.PHASES, durations, strict=True):
            records.append(
                {
                    "run": "synthetic",
                    "kind": "phase",
                    "worker": worker,
                    "nodeid": nodeid,
                    "when": when,
                    "outcome": "passed",
                    "duration": duration,
                    "start": start,
                    "stop": start + duration,
                }
            )
            start += duration + 0.25
    records.append({"run": "synthetic", "kind": "session_end", "worker": "main", "wall": 8.0})
    _write_records(tmp_path / "synthetic.jsonl", records)
    summary, nodes = pytest_timing_report.summarize(tmp_path, top=10)
    assert summary["tests"] == len(nodes) == 2
    assert summary["cumulative_phase_seconds"] == {"setup": 1.5, "call": 6.0, "teardown": 1.5}
    assert summary["cumulative_seconds"] == 9.0
    assert summary["session_wall_seconds"] == 8.0
    assert nodes["test_sample.py::test_a"]["total"] == 3.0
    assert nodes["test_sample.py::test_b"]["total"] == 6.0
    for worker, busy in (("gw0", 3.0), ("gw1", 6.0)):
        assert summary["workers"][worker]["busy_seconds"] == busy
        assert summary["workers"][worker]["span_seconds"] == busy + 0.5
        assert summary["workers"][worker]["idle_in_span_seconds"] == 0.5


@pytest.mark.integration
def test_fixture_hook_elapsed_semantics(project: Path, tmp_path: Path) -> None:
    # Advance only the reporter's fixture clock for exact nested-interval assertions.
    (project / "conftest.py").write_text(
        "import time\n"
        "from types import SimpleNamespace\n"
        "from tools.open_brain_dev import pytest_timing\n"
        "clock = [0.0]\n"
        "def pytest_sessionstart(session):\n"
        "    pytest_timing.time = SimpleNamespace(\n"
        "        perf_counter=lambda: clock[0], process_time=time.process_time,\n"
        "        monotonic=time.monotonic, time=time.time)\n",
        encoding="utf-8",
    )
    (project / "test_sample.py").write_text(
        "import pytest\n"
        "from conftest import clock\n"
        "@pytest.fixture\n"
        "def declared_dep():\n    clock[0] += 2.0\n"
        "@pytest.fixture\n"
        "def declared_parent(declared_dep):\n    clock[0] += 3.0\n"
        "@pytest.fixture\n"
        "def dynamic_dep():\n    clock[0] += 2.0\n"
        "@pytest.fixture\n"
        "def dynamic_parent(request):\n"
        "    request.getfixturevalue('dynamic_dep')\n"
        "    clock[0] += 3.0\n"
        "def test_declared(declared_parent):\n    pass\n"
        "def test_dynamic(dynamic_parent):\n    pass\n",
        encoding="utf-8",
    )
    directory = tmp_path / "timing"
    completed = _run(project, directory)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "2 passed" in completed.stdout
    fixtures = {
        record["argname"]: record for record in _records(directory) if record["kind"] == "fixture"
    }
    for name, seconds in (
        ("declared_dep", 2.0),
        ("declared_parent", 3.0),
        ("dynamic_dep", 2.0),
        ("dynamic_parent", 5.0),
    ):
        assert fixtures[name]["count"] == 1
        assert fixtures[name]["seconds"] == fixtures[name]["max_seconds"] == seconds
