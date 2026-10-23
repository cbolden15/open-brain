"""Synthetic projects for the opt-in pytest shard selector."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.open_brain_dev import pytest_shard

REPOSITORY = Path(__file__).resolve().parents[2]
PREFIX = "test_sample.py::"


@pytest.mark.parametrize("value", ["1/4", "4/4", "1/1"])
def test_parse_shard_accepts_one_based_index(value: str) -> None:
    index, count = pytest_shard.parse_shard(value)
    assert f"{index}/{count}" == value


@pytest.mark.parametrize("value", ["0/4", "5/4", "1/0", "4", "a/b", "1/4/2", "-1/4", ""])
def test_parse_shard_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValueError):
        pytest_shard.parse_shard(value)


def test_every_node_belongs_to_exactly_one_shard() -> None:
    nodeids = [f"tests/test_{number}.py::test_case[{number}]" for number in range(400)]
    for count in (1, 2, 4, 7):
        shards = [pytest_shard.shard_of(nodeid, count) for nodeid in nodeids]
        assert set(shards) == set(range(1, count + 1))
        assert shards == [pytest_shard.shard_of(nodeid, count) for nodeid in nodeids]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pytest.ini").write_text(
        "[pytest]\naddopts = --import-mode=importlib\n", encoding="utf-8"
    )
    (root / "test_sample.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', range(40))\n"
        "def test_case(value):\n    pass\n",
        encoding="utf-8",
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
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _collected(project: Path, *arguments: str) -> list[str]:
    completed = _run(project, "--collect-only", "-q", *arguments)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return [line for line in completed.stdout.splitlines() if line.startswith(PREFIX)]


@pytest.mark.integration
def test_shards_partition_the_collection(project: Path) -> None:
    everything = _collected(project)
    assert _collected(project, "-p", pytest_shard.PLUGIN) == everything
    shards = [_collected(project, "-p", pytest_shard.PLUGIN, f"--shard={i}/3") for i in (1, 2, 3)]
    assert all(shards)
    assert sorted(nodeid for shard in shards for nodeid in shard) == sorted(everything)
    for shard in shards:
        assert shard == [nodeid for nodeid in everything if nodeid in shard]


@pytest.mark.integration
def test_malformed_shard_is_a_usage_error(project: Path) -> None:
    completed = _run(project, "-p", pytest_shard.PLUGIN, "--shard=4/3")
    assert completed.returncode == 4
    assert "INDEX/COUNT" in completed.stderr
