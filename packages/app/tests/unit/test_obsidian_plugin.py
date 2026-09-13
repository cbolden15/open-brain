from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_brain.services.obsidian_plugin import (
    PLUGIN_ASSETS,
    PLUGIN_DIRECTORY,
    ObsidianPluginFailure,
    discover_obsidian_plugin_assets,
    install_obsidian_plugin,
    load_obsidian_plugin_bundle,
    obsidian_plugin_status,
    remove_obsidian_plugin,
)


def _assets(directory: Path, *, javascript: str = "module.exports = {};\n") -> Path:
    directory.mkdir(parents=True)
    (directory / "main.js").write_text(javascript, encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": "open-brain",
                "isDesktopOnly": True,
                "minAppVersion": "1.12.7",
                "name": "Open Brain",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )
    (directory / "styles.css").write_text(".open-brain {}\n", encoding="utf-8")
    return directory


def test_asset_discovery_binds_to_the_resolved_product_prefix(tmp_path: Path) -> None:
    prefix = tmp_path / "Cellar/open-brain/0.1.0"
    executable = prefix / "bin/open-brain"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    assets = _assets(prefix / "share/open-brain/obsidian-plugin")
    link = tmp_path / "bin/open-brain"
    link.parent.mkdir()
    link.symlink_to(executable)

    assert discover_obsidian_plugin_assets(link) == assets
    assert load_obsidian_plugin_bundle(assets).version == "0.1.0"

    moved = tmp_path / "assets"
    assets.rename(moved)
    assets.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ObsidianPluginFailure, match="assets_unavailable"):
        discover_obsidian_plugin_assets(link)


def test_install_upgrade_and_remove_preserve_obsidian_user_state(tmp_path: Path) -> None:
    workspace = tmp_path / "Open Brain Vault"
    workspace.mkdir(mode=0o700)
    first = load_obsidian_plugin_bundle(_assets(tmp_path / "first"))
    second = load_obsidian_plugin_bundle(
        _assets(tmp_path / "second", javascript="module.exports = { version: 2 };\n")
    )

    assert install_obsidian_plugin(workspace, first)["status"] == "installed"
    plugin = workspace / PLUGIN_DIRECTORY
    data = plugin / "data.json"
    extra = plugin / "owner-note.txt"
    activation = workspace / ".obsidian/community-plugins.json"
    data.write_text('{"inferencePaused":true}\n', encoding="utf-8")
    extra.write_text("preserve me\n", encoding="utf-8")
    activation.write_text('["open-brain"]\n', encoding="utf-8")

    assert obsidian_plugin_status(workspace, second)["status"] == "update_available"
    assert install_obsidian_plugin(workspace, second)["status"] == "installed"
    assert obsidian_plugin_status(workspace, second)["status"] == "current"
    assert data.read_text(encoding="utf-8") == '{"inferencePaused":true}\n'
    assert activation.read_text(encoding="utf-8") == '["open-brain"]\n'

    assert remove_obsidian_plugin(workspace)["status"] == "removed"
    assert all(not (plugin / name).exists() for name in PLUGIN_ASSETS)
    assert data.read_text(encoding="utf-8") == '{"inferencePaused":true}\n'
    assert extra.read_text(encoding="utf-8") == "preserve me\n"
    assert activation.read_text(encoding="utf-8") == '["open-brain"]\n'


def test_foreign_or_modified_plugin_is_never_overwritten_or_removed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "Open Brain Vault"
    workspace.mkdir(mode=0o700)
    bundle = load_obsidian_plugin_bundle(_assets(tmp_path / "assets"))
    plugin = workspace / PLUGIN_DIRECTORY
    plugin.mkdir(parents=True)
    foreign = plugin / "main.js"
    foreign.write_text("owner bytes\n", encoding="utf-8")

    with pytest.raises(ObsidianPluginFailure, match="foreign_plugin"):
        install_obsidian_plugin(workspace, bundle)
    assert foreign.read_text(encoding="utf-8") == "owner bytes\n"

    foreign.unlink()
    plugin.rmdir()
    install_obsidian_plugin(workspace, bundle)
    installed = {name: (plugin / name).read_bytes() for name in PLUGIN_ASSETS}
    (plugin / "main.js").write_text("changed by owner\n", encoding="utf-8")

    with pytest.raises(ObsidianPluginFailure, match="modified_plugin"):
        remove_obsidian_plugin(workspace)
    assert (plugin / "main.js").read_text(encoding="utf-8") == "changed by owner\n"
    assert all(
        (plugin / name).read_bytes() == payload
        for name, payload in installed.items()
        if name != "main.js"
    )
