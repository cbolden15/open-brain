"""Opt-in pytest timing reporter for local test-performance profiling.

Load it with ``-p tools.open_brain_dev.pytest_timing`` and set
``OPEN_BRAIN_PYTEST_TIMING_DIR`` to an absolute directory outside the checkout.
Each pytest process (the xdist controller and every worker, or the single
process of a serial run) appends JSON Lines records to its own file there.
Without the variable the plugin records nothing and changes no test behavior.

Workers record per-phase wall time, process CPU, reaped child-process CPU,
voluntary context switches, and audited sleep, spawn, and SQLite-connect
counts; exclusive fixture setup time is aggregated per fixture definition.
The controller records session wall time and when each worker finished
collection. Records hold node IDs and timings only, never test output.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import IO, Any

import pytest

TIMING_DIRECTORY_ENVIRONMENT = "OPEN_BRAIN_PYTEST_TIMING_DIR"
_AUDITED_EVENTS = frozenset(
    {
        "os.fork",
        "os.posix_spawn",
        "os.system",
        "sqlite3.connect",
        "subprocess.Popen",
        "time.sleep",
    }
)
_PHASES = ("setup", "call", "teardown")


class _AuditCounters:
    """Process-wide audited event counters; one test runs at a time per process."""

    def __init__(self) -> None:
        self.spawns = 0
        self.sqlite_connects = 0
        self.sleep_calls = 0
        self.sleep_seconds = 0.0

    def snapshot(self) -> tuple[int, int, int, float]:
        return (self.spawns, self.sqlite_connects, self.sleep_calls, self.sleep_seconds)

    def hook(self, event: str, arguments: tuple[Any, ...]) -> None:
        if event not in _AUDITED_EVENTS:
            return
        if event == "time.sleep":
            self.sleep_calls += 1
            if arguments and isinstance(arguments[0], int | float):
                self.sleep_seconds += float(arguments[0])
        elif event == "sqlite3.connect":
            self.sqlite_connects += 1
        else:
            self.spawns += 1


def _usage() -> tuple[float, float, int, int, int]:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (
        time.process_time(),
        children.ru_utime + children.ru_stime,
        own.ru_nvcsw,
        own.ru_inblock,
        own.ru_oublock,
    )


class TimingRecorder:
    """Writes one JSON Lines file per pytest process."""

    def __init__(self, config: pytest.Config, directory: Path) -> None:
        workerinput = getattr(config, "workerinput", None)
        self.worker = str(workerinput["workerid"]) if workerinput else "main"
        self.controller = workerinput is None and config.getoption("dist", "no") != "no"
        role = "controller" if self.controller else "worker"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{role}-{self.worker}-{os.getpid()}.jsonl"
        self._stream: IO[str] = path.open("a", encoding="utf-8", buffering=1)
        self._session_start = time.monotonic()
        self._audit = _AuditCounters()
        self._phase_start: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._phase_delta: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._fixtures: dict[tuple[str, str, str], list[float]] = {}
        if not self.controller:
            sys.addaudithook(self._audit.hook)
        self._write(
            {
                "kind": "session",
                "role": role,
                "worker": self.worker,
                "pid": os.getpid(),
                "start_epoch": time.time(),
                "python": sys.version.split()[0],
            }
        )

    def _write(self, record: dict[str, Any]) -> None:
        self._stream.write(json.dumps(record, sort_keys=True) + "\n")

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self._write(
            {
                "kind": "collection",
                "worker": self.worker,
                "items": len(session.items),
                "seconds": round(time.monotonic() - self._session_start, 6),
            }
        )

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: Any, ids: list[str]) -> None:
        self._write(
            {
                "kind": "worker_collected",
                "worker": str(node.gateway.id),
                "items": len(ids),
                "seconds": round(time.monotonic() - self._session_start, 6),
            }
        )

    def _begin(self, item: pytest.Item, when: str) -> None:
        self._phase_start[(item.nodeid, when)] = (*_usage(), *self._audit.snapshot())

    def _end(self, item: pytest.Item, when: str) -> None:
        start = self._phase_start.pop((item.nodeid, when), None)
        if start is None:
            return
        end = (*_usage(), *self._audit.snapshot())
        delta = tuple(after - before for after, before in zip(end, start, strict=True))
        self._phase_delta[(item.nodeid, when)] = delta

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Generator[None, object, object]:
        self._begin(item, "setup")
        try:
            return (yield)
        finally:
            self._end(item, "setup")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        self._begin(item, "call")
        try:
            return (yield)
        finally:
            self._end(item, "call")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> Generator[None, object, object]:
        self._begin(item, "teardown")
        try:
            return (yield)
        finally:
            self._end(item, "teardown")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if self.controller or report.when not in _PHASES:
            return
        delta = self._phase_delta.pop((report.nodeid, report.when), None)
        record: dict[str, Any] = {
            "kind": "phase",
            "worker": self.worker,
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": round(report.duration, 6),
            "start": round(report.start, 6),
            "stop": round(report.stop, 6),
        }
        if delta is not None:
            cpu, child_cpu, nvcsw, inblock, oublock, spawns, connects, sleeps, slept = delta
            record.update(
                cpu=round(cpu, 6),
                child_cpu=round(child_cpu, 6),
                voluntary_switches=nvcsw,
                block_in=inblock,
                block_out=oublock,
                spawns=spawns,
                sqlite_connects=connects,
                sleep_calls=sleeps,
                sleep_seconds=round(slept, 6),
            )
        self._write(record)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(
        self, fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
    ) -> Generator[None, object, object]:
        wall = time.perf_counter()
        cpu = time.process_time()
        try:
            return (yield)
        finally:
            key = (fixturedef.argname, fixturedef.scope, fixturedef.baseid)
            totals = self._fixtures.setdefault(key, [0, 0.0, 0.0, 0.0])
            elapsed = time.perf_counter() - wall
            totals[0] += 1
            totals[1] += elapsed
            totals[2] += time.process_time() - cpu
            totals[3] = max(totals[3], elapsed)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        for (argname, scope, baseid), (count, wall, cpu, longest) in sorted(self._fixtures.items()):
            self._write(
                {
                    "kind": "fixture",
                    "worker": self.worker,
                    "argname": argname,
                    "scope": scope,
                    "baseid": baseid,
                    "count": int(count),
                    "seconds": round(wall, 6),
                    "cpu": round(cpu, 6),
                    "max_seconds": round(longest, 6),
                }
            )
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        self._write(
            {
                "kind": "session_end",
                "worker": self.worker,
                "wall": round(time.monotonic() - self._session_start, 6),
                "cpu": round(time.process_time(), 6),
                "child_cpu": round(children.ru_utime + children.ru_stime, 6),
                "max_rss": own.ru_maxrss,
                "exitstatus": int(session.exitstatus),
            }
        )
        self._stream.close()


def pytest_configure(config: pytest.Config) -> None:
    directory = os.environ.get(TIMING_DIRECTORY_ENVIRONMENT)
    if not directory:
        return
    path = Path(directory)
    if not path.is_absolute():
        raise pytest.UsageError(f"{TIMING_DIRECTORY_ENVIRONMENT} must be an absolute path")
    config.pluginmanager.register(TimingRecorder(config, path), "open-brain-timing-recorder")
