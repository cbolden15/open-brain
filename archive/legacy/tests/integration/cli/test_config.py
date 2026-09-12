from __future__ import annotations

from pathlib import Path

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy._compat.open_brain.config import AppConfig, RetainedRoots
from open_brain_legacy.cli.config import show_config


def test_config_serializes_deterministic_secret_free_metadata_without_roots() -> None:
    config = AppConfig(
        roots=RetainedRoots(
            work=Path("/synthetic/work"),
            personal=Path("/synthetic/personal"),
            capture=Path("/synthetic/capture"),
            saved_content=Path("/synthetic/saved-content"),
            state=Path("/synthetic/state"),
        ),
        backup=Path("/synthetic/backup"),
        provider="local",
        cloud_enabled=False,
        egress_enabled=False,
    )

    first = show_config(config=config)
    second = show_config(config=config)

    assert first.exit_code is ExitCode.SUCCESS
    assert first.to_json() == second.to_json()
    assert first.envelope == {
        "cloud_enabled": False,
        "command": "config",
        "egress_enabled": False,
        "ledger_route_count": 0,
        "provider": "local",
        "status": "ok",
    }
    assert "/synthetic" not in first.to_json()
    assert "state_root" not in first.to_json()
    assert "saved_content_root" not in first.to_json()
