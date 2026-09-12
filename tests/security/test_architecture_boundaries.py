from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
APP_SOURCE_ROOT = REPOSITORY_ROOT / "packages/app/src/open_brain"


def test_core_has_no_concrete_adapter_imports() -> None:
    core = (
        Path(__file__).parents[2]
        / "packages"
        / "engine"
        / "src"
        / "open_brain_engine"
        / "core"
    )
    prohibited = ("filesystem", "socket", "urllib.request", "open_brain.cli", "open_brain.config")
    assert all(token not in path.read_text() for path in core.glob("*.py") for token in prohibited)


def test_core_ports_expose_no_task_capability_or_raw_redaction() -> None:
    source = (
        Path(__file__).parents[2]
        / "packages"
        / "engine"
        / "src"
        / "open_brain_engine"
        / "core"
        / "ports.py"
    ).read_text()
    prohibited = ("create_task", "upsert_task", "enqueue_task", "TaskStore", "task_id")
    assert all(token not in source for token in prohibited)
    raw_store = source.split("class RawStore", maxsplit=1)[1].split(
        "class RedactionFindingCategory", maxsplit=1
    )[0]
    assert "RedactionReceipt" not in raw_store


def test_application_package_contains_only_foreground_local_surfaces() -> None:
    actual = {
        path.relative_to(APP_SOURCE_ROOT).as_posix()
        for path in APP_SOURCE_ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }

    assert actual == {
        "__main__.py",
        "local_data.py",
        "profile.py",
        "services/__init__.py",
        "services/local_bootstrap.py",
        "services/local_entrypoints.py",
        "services/local_mcp.py",
        "services/local_native_entrypoint.py",
        "services/local_operations.py",
        "services/mcp_protocol.py",
    }


def test_secure_node_and_legacy_sources_are_quarantined_outside_packages() -> None:
    secure_archive = REPOSITORY_ROOT / "archive/open-brain-secure-node"
    legacy_archive = REPOSITORY_ROOT / "archive/legacy"

    assert (secure_archive / "README.md").is_file()
    assert (legacy_archive / "README.md").is_file()
    assert not (REPOSITORY_ROOT / "packages/legacy").exists()
    assert (secure_archive / "app/src/open_brain/services/appliance_daemon.py").is_file()
    assert (secure_archive / "engine/src/open_brain_engine/protocol").is_dir()
    assert (secure_archive / "engine/src/open_brain_engine/ledger").is_dir()
