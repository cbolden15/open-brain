from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from open_brain.profile import compile_single_user_local
from open_brain_collector.live_manager import LiveSourceManager
from open_brain_connectors.runtime.agent_session_hooks import (
    AgentSessionHookManager,
    HookConfigError,
    HookConfigPreview,
)
from open_brain_connectors.runtime.live_common import LiveSourceError


def _preview(manager: LiveSourceManager, project: Path, *, transcript: bool = False) -> str:
    result = manager.dispatch(
        "sources.session_preview",
        {
            "client": "claude_code",
            "project_path": str(project),
            "capture_summary": not transcript,
            "capture_transcript": transcript,
            "action": "configure",
        },
    )
    return cast(str, result["preview_id"])


def _manager(tmp_path: Path) -> tuple[LiveSourceManager, Path]:
    compile_single_user_local(tmp_path / "brain")
    project = tmp_path / "project"
    project.mkdir()
    return LiveSourceManager(tmp_path / "live", tmp_path / "brain"), project


def test_failed_hook_change_preserves_enabled_source_and_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, project = _manager(tmp_path)
    result = manager.dispatch("sources.session_apply", {"preview_id": _preview(manager, project)})
    source_id = cast(str, result["source_id"])
    manager.capture.control(source_id, "enable")
    before = manager.capture._load(source_id)
    preview_id = _preview(manager, project, transcript=True)

    def race(_manager: AgentSessionHookManager, _preview: HookConfigPreview) -> Path:
        raise HookConfigError("concurrent synthetic edit")

    monkeypatch.setattr(AgentSessionHookManager, "apply", race)
    with pytest.raises(LiveSourceError, match="source_hook_apply_failed"):
        manager.dispatch("sources.session_apply", {"preview_id": preview_id})
    assert manager.capture._load(source_id) == before


@pytest.mark.parametrize("existing", [False, True])
def test_failed_source_save_restores_exact_original_hook_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    manager, project = _manager(tmp_path)
    config = project / ".claude/settings.local.json"
    original = b'{"unrelated": true}\n\n'
    if existing:
        config.parent.mkdir()
        config.write_bytes(original)
    preview_id = _preview(manager, project)

    def fail_save(_entry: dict[str, object]) -> None:
        raise LiveSourceError("source_write_failed")

    monkeypatch.setattr(manager.capture, "_save", fail_save)
    with pytest.raises(LiveSourceError, match="source_write_failed"):
        manager.dispatch("sources.session_apply", {"preview_id": preview_id})
    assert config.read_bytes() == original if existing else not config.exists()
    assert manager.capture.status()["sources"] == []


def test_owned_hook_updates_queue_destination_and_rejects_fifo(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = AgentSessionHookManager(tmp_path / "first")
    first.apply(first.preview_apply("claude_code", project))
    second = AgentSessionHookManager(tmp_path / "second")
    changed = second.preview_apply("claude_code", project)
    assert changed.changed
    second.apply(changed)
    text = changed.config_path.read_text()
    assert str(tmp_path / "second") in text and str(tmp_path / "first") not in text
    import os

    codex = project / ".codex"
    codex.mkdir()
    os.mkfifo(codex / "hooks.json")
    with pytest.raises(HookConfigError, match="unsafe hook configuration"):
        second.preview_apply("codex", project)
