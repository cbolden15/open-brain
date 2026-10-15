"""Synthetic profiles and isolated projects for the opt-in pytest tiers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.open_brain_dev import pytest_tiers

REPOSITORY = Path(__file__).resolve().parents[2]
PREFIX = "nested/test_sample.py::"
PLAIN = PREFIX + "test_plain"
EXPLICIT = PREFIX + "test_explicit"
GROUPED = PREFIX + "test_grouped"
METHOD = PREFIX + "TestGroup::test_method"
ALL_IDS = {PLAIN, EXPLICIT, GROUPED + "[a[b]]", GROUPED + "[plain]", METHOD}


@pytest.mark.parametrize(
    ("nodeid", "expected"),
    [
        ("test_a.py::test_a", "test_a.py::test_a"),
        ("test_a.py::test_a[x[y][z]]", "test_a.py::test_a"),
        ("test_a.py::TestA::test_a[x[y]]", "test_a.py::TestA::test_a"),
        ("dir[one]/test_a.py::TestA::test_a", "dir[one]/test_a.py::TestA::test_a"),
        ("test_a[x]", "test_a"),
    ],
)
def test_function_id(nodeid: str, expected: str) -> None:
    assert pytest_tiers.function_id(nodeid) == expected


def _totals(total: float, *, spawns: int = 0, sleeps: int = 0) -> dict[str, float]:
    return {
        "setup": 0.0,
        "call": total,
        "teardown": 0.0,
        "total": total,
        "spawns": spawns,
        "sleep_calls": sleeps,
    }


def test_generate_rules_grouping_and_determinism(tmp_path: Path) -> None:
    nodes = {
        "test_z.py::test_spawn": _totals(0.0, spawns=1),
        "test_z.py::test_sleep": _totals(0.0, sleeps=1),
        "test_z.py::test_threshold": _totals(0.5),
        "test_z.py::test_below": _totals(0.499999),
        "test_z.py::TestGroup::test_grouped[fast[a]]": _totals(0.01),
        "test_z.py::TestGroup::test_grouped[spawn]": _totals(0.01, spawns=1),
        "test_z.py::TestGroup::test_grouped[slow]": _totals(1.0),
        "test_z.py::test_sum[a]": _totals(0.3),
        "test_z.py::test_sum[b]": _totals(0.3),
    }
    source = tmp_path / "nodes.json"
    source.write_text(json.dumps(nodes), encoding="utf-8")
    output = tmp_path / "tiers" / "integration.txt"
    command = ["generate", str(source), "--output", str(output)]
    assert pytest_tiers.main(command) == 0
    first = output.read_bytes()
    assert pytest_tiers.main(command) == 0
    assert output.read_bytes() == first
    lines = first.decode().splitlines()
    assert lines[0].startswith("# Integration if ANY parametrization")
    assert "spawns > 0" in lines[0] and "sleep_calls > 0" in lines[0]
    assert "total >= 0.5 s" in lines[0]
    assert lines[1] == f"# Input sha256: {hashlib.sha256(source.read_bytes()).hexdigest()}"
    expected = [
        "test_z.py::TestGroup::test_grouped",
        "test_z.py::test_sleep",
        "test_z.py::test_spawn",
        "test_z.py::test_threshold",
    ]
    assert lines[2:] == expected
    source.write_text(json.dumps(dict(reversed(list(nodes.items())))), encoding="utf-8")
    assert pytest_tiers.main(command) == 0
    assert output.read_text(encoding="utf-8").splitlines()[2:] == expected


def test_generate_empty_manifest(tmp_path: Path) -> None:
    source = tmp_path / "empty.json"
    source.write_text("{}", encoding="utf-8")
    output = tmp_path / "integration.txt"
    pytest_tiers.generate(source, output)
    assert pytest_tiers.read_ids(output) == set()
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "nested").mkdir(parents=True)
    (root / "pytest.ini").write_text(
        "[pytest]\naddopts = --import-mode=importlib\n", encoding="utf-8"
    )
    (root / "nested/test_sample.py").write_text(
        "import pytest\n"
        "def test_plain():\n    pass\n"
        "@pytest.mark.parametrize('value', ['a[b]', 'plain'])\n"
        "def test_grouped(value):\n    assert value\n"
        "@pytest.mark.integration\n"
        "def test_explicit(request):\n"
        "    assert len(list(request.node.iter_markers('integration'))) == 1\n"
        "class TestGroup:\n    def test_method(self):\n        pass\n",
        encoding="utf-8",
    )
    manifest = root / pytest_tiers.MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"# synthetic manifest\n\n{GROUPED}\n{EXPLICIT}\n{METHOD}\n", encoding="utf-8"
    )
    return root


def _run(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY), environment.get("PYTHONPATH", "")]
    )
    environment["PYTEST_ADDOPTS"] = ""
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _collected(project: Path, *arguments: str) -> list[str]:
    completed = _run(
        project, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *arguments
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return [line for line in completed.stdout.splitlines() if line.startswith(PREFIX)]


@pytest.mark.integration
def test_plugin_adds_markers_without_changing_order_or_unloaded_behavior(project: Path) -> None:
    unloaded = _collected(project)
    loaded = _collected(project, "-p", pytest_tiers.PLUGIN, "--strict-markers")
    assert loaded == unloaded
    assert set(loaded) == ALL_IDS
    assert set(_collected(project, "-p", pytest_tiers.PLUGIN, "-m", "integration")) == (
        ALL_IDS - {PLAIN}
    )
    assert _collected(project, "-p", pytest_tiers.PLUGIN, "-m", "not integration") == [PLAIN]
    assert _collected(project, "-m", "integration") == [EXPLICIT]
    # Start below rootpath and execute to verify root-relative lookup and no duplicate marks.
    completed = _run(
        project / "nested",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        pytest_tiers.PLUGIN,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "5 passed" in completed.stdout


@pytest.mark.integration
def test_audit_partition_baseline_and_hashes(project: Path) -> None:
    baseline = project / "baseline.txt"
    baseline.write_text("\n".join(sorted(ALL_IDS)) + "\n", encoding="utf-8")
    completed = _run(project, "-m", pytest_tiers.PLUGIN, "audit", "--baseline", str(baseline))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["ok"] and result["disjoint"] and result["union_equals_all"]
    assert result["all"]["count"] == result["union"]["count"] == 5
    assert result["integration"]["count"] == 4
    assert result["fast"]["count"] == 1
    assert result["all"]["sha256"] == hashlib.sha256(baseline.read_bytes()).hexdigest()
    assert result["union"]["sha256"] == result["all"]["sha256"]
    assert result["stale_manifest"]["ids"] == result["missing_baseline"]["ids"] == []


@pytest.mark.integration
@pytest.mark.parametrize("expression", ["integration", "not integration"])
def test_audit_accepts_an_empty_tier(project: Path, expression: str) -> None:
    if expression == "integration":
        (project / pytest_tiers.MANIFEST).write_text("", encoding="utf-8")
        source = project / "nested/test_sample.py"
        source.write_text(
            source.read_text(encoding="utf-8").replace("@pytest.mark.integration", "")
        )
    else:
        (project / pytest_tiers.MANIFEST).write_text(
            "\n".join([PLAIN, EXPLICIT, GROUPED, METHOD]) + "\n", encoding="utf-8"
        )
    completed = _run(project, "-m", pytest_tiers.PLUGIN, "audit")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    empty = "integration" if expression == "integration" else "fast"
    assert result[empty]["count"] == 0
    assert result[empty]["sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("violation", "field", "expected"),
    [
        ("overlap", "overlap", EXPLICIT),
        ("missing", "missing_from_tiers", PLAIN),
        ("unexpected", "unexpected_in_tiers", PLAIN),
        ("baseline", "missing_baseline", PREFIX + "test_removed[case]"),
        ("stale", "stale_manifest", PREFIX + "test_stale"),
        ("collection", "collection_errors", "integration"),
    ],
)
def test_audit_rejects_violations(project: Path, violation: str, field: str, expected: str) -> None:
    arguments = ["-m", pytest_tiers.PLUGIN, "audit"]
    if violation == "baseline":
        baseline = project / "baseline.txt"
        baseline.write_text(expected + "\n", encoding="utf-8")
        arguments.extend(["--baseline", str(baseline)])
    elif violation == "stale":
        with (project / pytest_tiers.MANIFEST).open("a", encoding="utf-8") as stream:
            stream.write(expected + "\n")
    else:
        (project / "conftest.py").write_text(
            "import pytest\n"
            "@pytest.hookimpl(trylast=True)\n"
            "def pytest_collection_modifyitems(config, items):\n"
            f"    violation = {violation!r}\n"
            "    expression = config.option.markexpr\n"
            "    if violation == 'overlap' and expression == 'not integration':\n"
            f"        items[0]._nodeid = {EXPLICIT!r}\n"
            "    if (violation == 'missing' and expression == 'not integration') or (\n"
            "        violation == 'unexpected' and not expression\n"
            "    ):\n"
            "        items[:] = [item for item in items if item.name != 'test_plain']\n"
            "    if violation == 'collection' and expression == 'integration':\n"
            "        raise pytest.UsageError('synthetic collection failure')\n",
            encoding="utf-8",
        )
    completed = _run(project, *arguments)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert not result["ok"]
    assert expected in (result[field] if field == "collection_errors" else result[field]["ids"])
