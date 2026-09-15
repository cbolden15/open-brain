from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from open_brain_collector.service import (
    CollectorLaunchdServiceManager,
    CollectorOwnedServiceRegistry,
    CollectorProcessCleanup,
    CollectorServiceError,
)
from open_brain_connectors.runtime.connectors import ConnectorContractError


def test_owned_service_removal_refuses_unowned_registration(tmp_path: Path) -> None:
    registry = CollectorOwnedServiceRegistry(tmp_path / "services.json")
    registry.install(
        label="open-brain.collector.fixture",
        owner_token="collector-owned:goal70",
        command=("open-brain-collector", "source", "status"),
    )
    state = json.loads((tmp_path / "services.json").read_text(encoding="utf-8"))
    state["open-brain.collector.other"] = {
        "command": ["other"],
        "owner_token": "someone-else",
        "schema_version": 1,
    }
    (tmp_path / "services.json").write_text(json.dumps(state), encoding="utf-8")

    assert registry.remove_owned(
        label="open-brain.collector.fixture",
        owner_token="collector-owned:goal70",
    )
    with pytest.raises(CollectorServiceError, match="collector_service_not_owned"):
        registry.remove_owned(
            label="open-brain.collector.other",
            owner_token="collector-owned:goal70",
        )
    remaining = json.loads((tmp_path / "services.json").read_text(encoding="utf-8"))
    assert "open-brain.collector.fixture" not in remaining
    assert "open-brain.collector.other" in remaining


def test_owned_process_cleanup_terminates_process_group() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cleanup = CollectorProcessCleanup.from_process(process)
    result = cleanup.terminate(timeout_seconds=1.0)
    process.wait(timeout=2)

    assert result["cleaned"] is True
    assert process.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)


def test_launchd_service_install_status_and_removal_are_owned(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        item = list(command)
        calls.append(item)
        if item[:2] == ["launchctl", "print"] and "open-brain.collector.fixture" in item[-1]:
            return subprocess.CompletedProcess(item, 0, "", "")
        return subprocess.CompletedProcess(item, 0, "", "")

    manager = CollectorLaunchdServiceManager(
        plist_dir=tmp_path / "LaunchAgents",
        runner=runner,
        uid=501,
    )
    installed = manager.install(
        label="open-brain.collector.fixture",
        owner_token="collector-owned:goal70",
        command=("open-brain-collector", "run", "--state", "/tmp/state.json"),
    )

    plist = tmp_path / "LaunchAgents" / "open-brain.collector.fixture.plist"
    assert installed["status"] == "loaded"
    assert plist.exists()
    assert calls[0] == ["launchctl", "bootstrap", "gui/501", str(plist)]
    assert calls[1] == ["launchctl", "print", "gui/501/open-brain.collector.fixture"]

    removed = manager.remove_owned(
        label="open-brain.collector.fixture",
        owner_token="collector-owned:goal70",
    )

    assert removed["status"] == "loaded"
    assert not plist.exists()
    assert calls[2] == ["launchctl", "bootout", "gui/501/open-brain.collector.fixture"]


def test_launchd_service_removal_refuses_unowned_plist(tmp_path: Path) -> None:
    manager = CollectorLaunchdServiceManager(
        plist_dir=tmp_path / "LaunchAgents",
        runner=lambda command: subprocess.CompletedProcess(list(command), 0, "", ""),
        uid=501,
    )
    manager.install(
        label="open-brain.collector.fixture",
        owner_token="collector-owned:goal70",
        command=("open-brain-collector", "run", "--state", "/tmp/state.json"),
    )
    plist = tmp_path / "LaunchAgents" / "open-brain.collector.fixture.plist"
    payload = plist.read_bytes().replace(b"collector-owned:goal70", b"collector-owned:otherx")
    plist.write_bytes(payload)

    with pytest.raises(CollectorServiceError, match="collector_service_not_owned"):
        manager.remove_owned(
            label="open-brain.collector.fixture",
            owner_token="collector-owned:goal70",
        )


def test_service_cli_reports_owned_failure_as_bounded_json(tmp_path: Path) -> None:
    label = "open-brain.collector.fixture"
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    (plist_dir / f"{label}.plist").write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "OPEN_BRAIN_COLLECTOR_OWNER": "collector-owned:otherx",
                },
                "Label": label,
                "ProgramArguments": ["open-brain-collector", "run"],
            },
            sort_keys=True,
        )
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_brain_collector.cli",
            "service",
            "--plist-dir",
            str(plist_dir),
            "--label",
            label,
            "--owner-token",
            "collector-owned:goal70",
            "remove",
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 78
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "failure_code": "collector_service_not_owned",
        "label": label,
        "outcome": "failed",
        "schema_version": 1,
    }
    assert (plist_dir / f"{label}.plist").exists()


def test_launchd_service_install_refuses_to_overwrite_unowned_plist(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        item = list(command)
        calls.append(item)
        return subprocess.CompletedProcess(item, 0, "", "")

    manager = CollectorLaunchdServiceManager(
        plist_dir=tmp_path / "LaunchAgents",
        runner=runner,
        uid=501,
    )
    manager.install(
        label="open-brain.collector.fixture",
        owner_token="collector-owned:goal70",
        command=("open-brain-collector", "run", "--state", "/tmp/state.json"),
    )
    plist = tmp_path / "LaunchAgents" / "open-brain.collector.fixture.plist"
    payload = plist.read_bytes().replace(b"collector-owned:goal70", b"collector-owned:otherx")
    plist.write_bytes(payload)

    with pytest.raises(CollectorServiceError, match="collector_service_not_owned"):
        manager.install(
            label="open-brain.collector.fixture",
            owner_token="collector-owned:goal70",
            command=("open-brain-collector", "run", "--state", "/tmp/next.json"),
        )

    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "print", "gui/501/open-brain.collector.fixture"],
    ]


def test_launchd_service_reinstall_boots_out_owned_loaded_service_before_replacing(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        item = list(command)
        calls.append(item)
        if item[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(item, 0, "", "")
        return subprocess.CompletedProcess(item, 0, "", "")

    manager = CollectorLaunchdServiceManager(
        plist_dir=tmp_path / "LaunchAgents",
        runner=runner,
        uid=501,
    )
    label = "open-brain.collector.fixture"
    manager.install(
        label=label,
        owner_token="collector-owned:goal70",
        command=("open-brain-collector", "run", "--state", "/tmp/state.json"),
    )
    updated = manager.install(
        label=label,
        owner_token="collector-owned:goal70",
        command=("open-brain-collector", "run", "--state", "/tmp/updated.json"),
    )

    plist = tmp_path / "LaunchAgents" / f"{label}.plist"
    payload = plistlib.loads(plist.read_bytes())
    assert payload["ProgramArguments"] == [
        "open-brain-collector",
        "run",
        "--state",
        "/tmp/updated.json",
    ]
    assert updated["status"] == "loaded"
    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "print", f"gui/501/{label}"],
        ["launchctl", "print", f"gui/501/{label}"],
        ["launchctl", "bootout", f"gui/501/{label}"],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "print", f"gui/501/{label}"],
    ]


def test_launchd_service_install_removes_owned_plist_when_bootstrap_fails(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        item = list(command)
        calls.append(item)
        return subprocess.CompletedProcess(item, 5, "", "bootstrap denied")

    manager = CollectorLaunchdServiceManager(
        plist_dir=tmp_path / "LaunchAgents",
        runner=runner,
        uid=501,
    )

    with pytest.raises(CollectorServiceError, match="collector_service_install_failed"):
        manager.install(
            label="open-brain.collector.fixture",
            owner_token="collector-owned:goal70",
            command=("open-brain-collector", "run", "--state", "/tmp/state.json"),
        )

    assert not (tmp_path / "LaunchAgents" / "open-brain.collector.fixture.plist").exists()
    assert calls == [
        [
            "launchctl",
            "bootstrap",
            "gui/501",
            str(tmp_path / "LaunchAgents" / "open-brain.collector.fixture.plist"),
        ]
    ]


def test_launchd_service_absent_remove_does_not_bootout_same_label(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        item = list(command)
        calls.append(item)
        return subprocess.CompletedProcess(item, 0, "", "")

    manager = CollectorLaunchdServiceManager(
        plist_dir=tmp_path / "LaunchAgents",
        runner=runner,
        uid=501,
    )

    removed = manager.remove_owned(
        label="open-brain.collector.fixture",
        owner_token="collector-owned:goal70",
    )

    assert removed["outcome"] == "absent"
    assert removed["status"] == "not_loaded"
    assert calls == []


def test_launchd_service_ownership_identifiers_are_bounded(tmp_path: Path) -> None:
    manager = CollectorLaunchdServiceManager(
        plist_dir=tmp_path / "LaunchAgents",
        runner=lambda command: subprocess.CompletedProcess(list(command), 0, "", ""),
        uid=501,
    )

    invalid_cases = [
        ("open-brain.collector../bad", "collector-owned:goal70"),
        ("open-brain.collector." + ("a" * 120), "collector-owned:goal70"),
        ("open-brain.collector.fixture", "collector-owned:"),
        ("open-brain.collector.fixture", "collector-owned:bad/token"),
        ("open-brain.collector.fixture", "collector-owned:" + ("a" * 120)),
    ]

    for label, owner_token in invalid_cases:
        with pytest.raises(ConnectorContractError, match="invalid collector service ownership"):
            manager.install(
                label=label,
                owner_token=owner_token,
                command=("open-brain-collector", "run", "--state", "/tmp/state.json"),
            )
