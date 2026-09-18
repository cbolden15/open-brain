from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from open_brain.services.local_entrypoints import run_cli
from open_brain_collector.lifecycle import CollectorController, CollectorStateStore
from open_brain_connectors.runtime.source_intake import SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


def test_enabled_collector_imports_with_desktop_closed_and_fresh_cli_retrieves(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brain_root = tmp_path / "brain"
    collector_state = tmp_path / "collector" / "state.json"
    lease = tmp_path / "collector" / "collector.lock"
    fixture_runtime = tmp_path / "collector" / "fixture-runtime.json"
    fixture_runtime.parent.mkdir(parents=True)
    _write_fixture(
        fixture_runtime,
        revision="updated:2026-09-15T01:12:00Z",
        token="closed-desktop-token",
    )
    selection = _selection()
    clock = _Clock(100)
    controller = CollectorController(CollectorStateStore(collector_state), clock=clock)

    assert run_cli(("init", "--data-dir", str(brain_root), "--json")) == 0
    capsys.readouterr()
    controller.enable(
        source_id="github.fixture.closed",
        selection=selection,
        interval_seconds=60,
    )

    process = _collector_process(
        state=collector_state,
        brain_root=brain_root,
        lease=lease,
        fixture_runtime=fixture_runtime,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)

    assert result["outcome"] == "completed"
    assert result["results"][0]["outcome"] == "completed"
    assert result["results"][0]["captured_count"] == 1
    assert controller.status("github.fixture.closed").status == "enabled"
    assert controller.status("github.fixture.closed").next_run_epoch == result["results"][0][
        "next_run_epoch"
    ]

    assert run_cli(("search", "closed-desktop-token", "--data-dir", str(brain_root), "--json")) == 0
    search = cast(dict[str, object], json.loads(capsys.readouterr().out))
    hits = cast(list[dict[str, object]], search["results"])
    assert len(hits) == 1
    assert hits[0]["source_origin"] == "third_party"

    export = tmp_path / "export"
    export_args = ("export", str(export), "--verify", "--data-dir", str(brain_root), "--json")
    assert run_cli(export_args) == 0
    assert json.loads(capsys.readouterr().out)["verification"] == "verified"
    exported_captures = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (export / "sources/captures").rglob("capture_*.json")
    ]
    assert any("closed-desktop-token" in json.dumps(capture) for capture in exported_captures)


def test_persisted_pause_resume_and_disable_control_separate_collector_process(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brain_root = tmp_path / "brain"
    collector_state = tmp_path / "collector" / "state.json"
    lease = tmp_path / "collector" / "collector.lock"
    fixture_runtime = tmp_path / "collector" / "fixture-runtime.json"
    fixture_runtime.parent.mkdir(parents=True)
    _write_fixture(fixture_runtime, revision="updated:2026-09-15T01:12:00Z", token="first-token")
    controller = CollectorController(CollectorStateStore(collector_state), clock=_Clock(100))

    assert run_cli(("init", "--data-dir", str(brain_root), "--json")) == 0
    capsys.readouterr()
    controller.enable(
        source_id="github.fixture.closed",
        selection=_selection(),
        interval_seconds=1,
    )
    first = json.loads(
        _successful_collector_process(
            state=collector_state,
            brain_root=brain_root,
            lease=lease,
            fixture_runtime=fixture_runtime,
        ).stdout
    )
    assert first["results"][0]["captured_count"] == 1

    # Scheduling controls use a distinct record; unordered legacy revisions are refused.
    _write_fixture(
        fixture_runtime, revision="updated:2026-09-15T01:13:00Z", token="paused-token", issue=71,
    )
    assert _source_command(collector_state, "pause").returncode == 0
    paused = json.loads(
        _successful_collector_process(
            state=collector_state,
            brain_root=brain_root,
            lease=lease,
            fixture_runtime=fixture_runtime,
        ).stdout
    )
    assert paused["results"][0]["outcome"] == "deferred"
    assert paused["results"][0]["captured_count"] == 0

    assert _source_command(collector_state, "resume").returncode == 0
    resumed = json.loads(
        _successful_collector_process(
            state=collector_state,
            brain_root=brain_root,
            lease=lease,
            fixture_runtime=fixture_runtime,
        ).stdout
    )
    assert resumed["results"][0]["captured_count"] == 1

    assert _source_command(collector_state, "disable").returncode == 0
    _write_fixture(
        fixture_runtime, revision="updated:2026-09-15T01:14:00Z", token="disabled-token", issue=72,
    )
    disabled = json.loads(
        _successful_collector_process(
            state=collector_state,
            brain_root=brain_root,
            lease=lease,
            fixture_runtime=fixture_runtime,
        ).stdout
    )
    assert disabled["results"][0]["outcome"] == "skipped"
    assert disabled["results"][0]["captured_count"] == 0

    assert run_cli(("search", "disabled-token", "--data-dir", str(brain_root), "--json")) == 0
    disabled_search = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert disabled_search["results"] == []


def test_headless_source_schedule_updates_same_durable_state(tmp_path: Path) -> None:
    collector_state = tmp_path / "collector" / "state.json"
    collector_state.parent.mkdir(parents=True)
    controller = CollectorController(CollectorStateStore(collector_state), clock=_Clock(100))
    controller.enable(
        source_id="github.fixture.closed",
        selection=_selection(),
        interval_seconds=60,
    )

    scheduled = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_brain_collector.cli",
            "source",
            "--state",
            str(collector_state),
            "schedule",
            "--source-id",
            "github.fixture.closed",
            "--interval-seconds",
            "120",
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert scheduled.returncode == 0, scheduled.stderr
    result = json.loads(scheduled.stdout)
    persisted = json.loads(collector_state.read_text(encoding="utf-8"))
    assert result["status"] == "enabled"
    assert result["next_run_epoch"] >= 100
    assert persisted["sources"]["github.fixture.closed"]["interval_seconds"] == 120
    assert persisted["sources"]["github.fixture.closed"]["status"] == "enabled"


def test_sync_now_forces_only_selected_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brain_root = tmp_path / "brain"
    collector_state = tmp_path / "collector" / "state.json"
    lease = tmp_path / "collector" / "collector.lock"
    fixture_runtime = tmp_path / "collector" / "fixture-runtime.json"
    fixture_runtime.parent.mkdir(parents=True)
    _write_fixture(fixture_runtime, revision="updated:2026-09-15T01:15:00Z", token="selected-token")
    controller = CollectorController(CollectorStateStore(collector_state), clock=_Clock(100))

    assert run_cli(("init", "--data-dir", str(brain_root), "--json")) == 0
    capsys.readouterr()
    controller.enable(
        source_id="github.fixture.closed",
        selection=_selection(),
        interval_seconds=3600,
    )
    controller.enable(
        source_id="github.fixture.other",
        selection=SourceResourceSelection(
            connector_name="github",
            connection_id="account:cbolden15",
            resource_id="repo:cbolden15/other",
            resource_type="repository",
        ),
        interval_seconds=3600,
    )

    process = _collector_process(
        state=collector_state,
        brain_root=brain_root,
        lease=lease,
        fixture_runtime=fixture_runtime,
        source_id="github.fixture.closed",
        force=True,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)

    assert [item["source_id"] for item in result["results"]] == ["github.fixture.closed"]
    assert result["results"][0]["captured_count"] == 1
    state = json.loads(collector_state.read_text(encoding="utf-8"))
    assert state["sources"]["github.fixture.closed"]["last_run"] is not None
    assert state["sources"]["github.fixture.other"]["last_run"] is None


def test_run_once_uses_durable_source_runtime_without_fixture_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brain_root = tmp_path / "brain"
    collector_state = tmp_path / "collector" / "state.json"
    lease = tmp_path / "collector" / "collector.lock"
    runtime_dir = tmp_path / "collector" / "runtime"
    runtime_dir.mkdir(parents=True)
    selection = _selection()
    _write_runtime_page(
        runtime_dir,
        selection=selection,
        revision="updated:2026-09-15T01:16:00Z",
        token="durable-runtime-token",
    )
    controller = CollectorController(CollectorStateStore(collector_state), clock=_Clock(100))

    assert run_cli(("init", "--data-dir", str(brain_root), "--json")) == 0
    capsys.readouterr()
    controller.enable(
        source_id="github.fixture.closed",
        selection=selection,
        interval_seconds=60,
    )

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_brain_collector.cli",
            "run-once",
            "--state",
            str(collector_state),
            "--brain-root",
            str(brain_root),
            "--lease",
            str(lease),
            "--source-id",
            "github.fixture.closed",
            "--force",
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["results"][0]["captured_count"] == 1
    search_args = ("search", "durable-runtime-token", "--data-dir", str(brain_root), "--json")
    assert run_cli(search_args) == 0
    search = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert cast(list[dict[str, object]], search["results"])


def test_run_loop_stops_promptly_on_service_termination_signal(tmp_path: Path) -> None:
    collector_state = tmp_path / "collector" / "state.json"
    lease = tmp_path / "collector" / "collector.lock"
    fixture_runtime = tmp_path / "collector" / "fixture-runtime.json"
    brain_root = tmp_path / "brain"
    collector_state.parent.mkdir(parents=True)
    collector_state.write_text('{"schema_version":1,"sources":{}}\n', encoding="utf-8")
    fixture_runtime.write_text('{"records":[]}\n', encoding="utf-8")

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "open_brain_collector.cli",
            "run",
            "--state",
            str(collector_state),
            "--brain-root",
            str(brain_root),
            "--lease",
            str(lease),
            "--fixture-runtime-json",
            str(fixture_runtime),
            "--poll-seconds",
            "60",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not lease.exists():
            time.sleep(0.02)
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=3)

    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert result["iterations"] == 1
    assert result["stop_reason"] == "signal"


def test_run_loop_keeps_ownership_lease_between_scheduled_iterations(tmp_path: Path) -> None:
    collector_state = tmp_path / "collector" / "state.json"
    lease = tmp_path / "collector" / "collector.lock"
    fixture_runtime = tmp_path / "collector" / "fixture-runtime.json"
    brain_root = tmp_path / "brain"
    collector_state.parent.mkdir(parents=True)
    collector_state.write_text('{"schema_version":1,"sources":{}}\n', encoding="utf-8")
    fixture_runtime.write_text('{"records":[]}\n', encoding="utf-8")

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "open_brain_collector.cli",
            "run",
            "--state",
            str(collector_state),
            "--brain-root",
            str(brain_root),
            "--lease",
            str(lease),
            "--fixture-runtime-json",
            str(fixture_runtime),
            "--poll-seconds",
            "60",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not lease.exists():
            time.sleep(0.02)
        assert lease.exists()
        contender = _collector_process(
            state=collector_state,
            brain_root=brain_root,
            lease=lease,
            fixture_runtime=fixture_runtime,
        )
        assert contender.returncode == 0, contender.stderr
        assert json.loads(contender.stdout) == {
            "failure_code": "collector_already_owned",
            "outcome": "failed",
            "results": [],
        }
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=3)

    assert process.returncode == 0, stderr
    assert json.loads(stdout)["stop_reason"] == "signal"
    assert not lease.exists()


def test_sync_now_wakes_running_loop_from_durable_state_change(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brain_root = tmp_path / "brain"
    collector_state = tmp_path / "collector" / "state.json"
    lease = tmp_path / "collector" / "collector.lock"
    fixture_runtime = tmp_path / "collector" / "fixture-runtime.json"
    fixture_runtime.parent.mkdir(parents=True)
    _write_fixture(
        fixture_runtime,
        revision="updated:2026-09-15T01:17:00Z",
        token="first-loop-token",
    )
    controller = CollectorController(CollectorStateStore(collector_state), clock=_Clock(100))

    assert run_cli(("init", "--data-dir", str(brain_root), "--json")) == 0
    capsys.readouterr()
    controller.enable(
        source_id="github.fixture.closed",
        selection=_selection(),
        interval_seconds=3600,
    )

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "open_brain_collector.cli",
            "run",
            "--state",
            str(collector_state),
            "--brain-root",
            str(brain_root),
            "--lease",
            str(lease),
            "--fixture-runtime-json",
            str(fixture_runtime),
            "--poll-seconds",
            "60",
            "--max-iterations",
            "2",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for_last_run(collector_state, "github.fixture.closed")
        _write_fixture(
            fixture_runtime,
            revision="updated:2026-09-15T01:18:00Z",
            token="sync-now-loop-token",
            issue=71,
        )
        sync_now = subprocess.run(
            [
                sys.executable,
                "-m",
                "open_brain_collector.cli",
                "source",
                "--state",
                str(collector_state),
                "sync-now",
                "--source-id",
                "github.fixture.closed",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        assert sync_now.returncode == 0, sync_now.stderr
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=3)

    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert result["iterations"] == 2
    assert result["stop_reason"] == "bounded"
    assert result["last"]["results"][0]["captured_count"] == 1
    assert run_cli(("search", "sync-now-loop-token", "--data-dir", str(brain_root), "--json")) == 0
    search = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert cast(list[dict[str, object]], search["results"])


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def _selection() -> SourceResourceSelection:
    return SourceResourceSelection(
        connector_name="github",
        connection_id="account:cbolden15",
        resource_id="repo:cbolden15/open-brain-fixture",
        resource_type="repository",
    )


def _write_fixture(path: Path, *, revision: str, token: str, issue: int = 70) -> None:
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "external_id": f"issue:{issue}",
                        "revision_id": revision,
                        "text": f"Synthetic unattended import body with {token}.",
                        "title": "D3 closed desktop fixture",
                        "url": f"https://github.com/cbolden15/open-brain-fixture/issues/{issue}",
                    }
                ]
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _write_runtime_page(
    root: Path,
    *,
    selection: SourceResourceSelection,
    revision: str,
    token: str,
) -> None:
    name = SourceRecordKey(
        connector_name=selection.connector_name,
        connection_id=selection.connection_id,
        resource_id=selection.resource_id,
        external_id="collector-runtime",
        revision_id="v1",
    ).revision_identity()
    (root / f"{name}.json").write_text(
        json.dumps(
            {
                "connection_id": selection.connection_id,
                "connector_name": selection.connector_name,
                "cursor": None,
                "records": [
                    {
                        "external_id": "issue:closed",
                        "revision_id": revision,
                        "text": f"Synthetic unattended import body with {token}.",
                        "title": "D3 durable runtime fixture",
                        "url": "https://github.com/cbolden15/open-brain-fixture/issues/70",
                    }
                ],
                "resource_id": selection.resource_id,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _collector_process(
    *,
    state: Path,
    brain_root: Path,
    lease: Path,
    fixture_runtime: Path,
    source_id: str | None = None,
    force: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "open_brain_collector.cli",
        "run-once",
        "--state",
        str(state),
        "--brain-root",
        str(brain_root),
        "--lease",
        str(lease),
        "--fixture-runtime-json",
        str(fixture_runtime),
    ]
    if source_id is not None:
        command.extend(["--source-id", source_id])
    if force:
        command.append("--force")
    return subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
    )


def _successful_collector_process(
    *,
    state: Path,
    brain_root: Path,
    lease: Path,
    fixture_runtime: Path,
) -> subprocess.CompletedProcess[str]:
    process = _collector_process(
        state=state,
        brain_root=brain_root,
        lease=lease,
        fixture_runtime=fixture_runtime,
    )
    assert process.returncode == 0, process.stderr
    return process


def _source_command(
    state: Path,
    command: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "open_brain_collector.cli",
            "source",
            "--state",
            str(state),
            command,
            "--source-id",
            "github.fixture.closed",
        ],
        check=True,
        text=True,
        capture_output=True,
    )


def _wait_for_last_run(state: Path, source_id: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            source = json.loads(state.read_text(encoding="utf-8"))["sources"][source_id]
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            time.sleep(0.02)
            continue
        last_run = source.get("last_run")
        if (
            source.get("committed_revisions")
            and isinstance(last_run, dict)
            and last_run.get("captured_count") == 1
        ):
            return
        time.sleep(0.02)
    raise AssertionError("collector loop did not complete first run")
