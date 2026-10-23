"""Schema23 follow-up store: coordinates, tables, migration from schema22 and fencing."""

from __future__ import annotations

import re
from pathlib import Path

from open_brain.services import local_runtime_session

REPO = Path(__file__).resolve().parents[5]


def test_runtime_session_coordinate_stays_sixteen() -> None:
    """The follow-up store changes no session registry or handshake contract (Task 1)."""
    assert local_runtime_session.RUNTIME_SESSION_VERSION == 16
    collector = (
        REPO / "packages/collector/src/open_brain_collector/source_metadata.py"
    ).read_text()
    assert "_RUNTIME_COMPATIBILITY: Final = 16" in collector
    plugin = (REPO / "packages/obsidian-plugin/src/contracts.ts").read_text()
    assert "export const OPEN_BRAIN_RUNTIME_SESSION_VERSION = 16;" in plugin
    desktop = (REPO / "packages/desktop/src-tauri/src/runtime.rs").read_text()
    assert re.search(r"const SUPPORTED_RUNTIME_SESSION: u64 = 16;", desktop)
