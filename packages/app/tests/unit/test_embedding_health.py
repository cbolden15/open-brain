"""``ob.embedding-health.v1`` state mapping, surfaces and the no-helper-launch guarantee."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine import BrainEngine, TextPayload

from open_brain.profile import compile_single_user_local
from open_brain.services import embedding_helper as eh
from open_brain.services.embedding_health import embedding_health
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS
from open_brain.services.t03_adapters import owner_authority
from packages.app.tests import embedding_fake as fake
from packages.app.tests.unit.engine.test_embedding_cache import FakeBackend

KEYS = {"schema", "resource_state", "cache_state", "consent_state", "component_id", "last_error"}


def _installed(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "install"
    root.mkdir()
    base = fake.install(root, fake.resource_files(fake.fake_helper()))
    component = eh.verify_embedding_metadata(eh.discover_embedding_resource(base)).component_id
    return base, component


def _brain(tmp_path: Path) -> BrainEngine:
    tmp_path.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    engine.capture.accept(TextPayload("Synthetic garnet ledger for health"), delivery_id="h.1")
    return engine


def _owner(engine: BrainEngine) -> Any:
    return owner_authority(engine.tasks, session_id="owner-health-test")


def _health(engine: BrainEngine, base: Path | None) -> dict[str, object]:
    return embedding_health(engine.tasks, base_executable=base)


@pytest.fixture(autouse=True)
def no_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("health must not start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)


def test_fresh_brain_without_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr("sys.frozen", raising=False)
    health = _health(_brain(tmp_path), None)
    assert set(health) == KEYS
    assert health == {
        "schema": "ob.embedding-health.v1",
        "resource_state": "missing",
        "cache_state": "absent",
        "consent_state": "required",
        "component_id": None,
        "last_error": "resource_missing",
    }


def test_ready_resource_before_consent(tmp_path: Path) -> None:
    base, component = _installed(tmp_path)
    assert _health(_brain(tmp_path), base) == {
        "schema": "ob.embedding-health.v1",
        "resource_state": "ready",
        "cache_state": "absent",
        "consent_state": "required",
        "component_id": component,
        "last_error": None,
    }


def test_enabled_without_cache_is_incomplete_then_ready_then_revoked(tmp_path: Path) -> None:
    base, component = _installed(tmp_path)
    engine = _brain(tmp_path)
    engine.embedding_policy.enable(component, authority=_owner(engine))
    health = _health(engine, base)
    assert (health["consent_state"], health["cache_state"]) == ("enabled", "incomplete")
    assert health["last_error"] == "cache_incomplete"
    from open_brain_engine.engine import embedding_cache as cache

    cache.rebuild(
        engine,
        authority=_owner(engine),
        backend=FakeBackend(component),
        cancelled=lambda: False,
    )
    health = _health(engine, base)
    assert (health["consent_state"], health["cache_state"], health["last_error"]) == (
        "enabled",
        "ready",
        None,
    )
    engine.embedding_policy.disable(authority=_owner(engine))
    health = _health(engine, base)
    assert (health["consent_state"], health["cache_state"]) == ("revoked", "absent")


def test_active_generation_for_another_component_is_stale(tmp_path: Path) -> None:
    base, component = _installed(tmp_path)
    engine = _brain(tmp_path)
    other = "cd" * 32
    engine.embedding_policy.enable(other, authority=_owner(engine))
    from open_brain_engine.engine import embedding_cache as cache

    cache.rebuild(
        engine, authority=_owner(engine), backend=FakeBackend(other), cancelled=lambda: False
    )
    health = _health(engine, base)
    assert (health["cache_state"], health["last_error"]) == ("stale", "component_mismatch")
    assert health["component_id"] == component


def test_invalid_cache_pointer_is_stale(tmp_path: Path) -> None:
    base, component = _installed(tmp_path)
    engine = _brain(tmp_path)
    engine.embedding_policy.enable(component, authority=_owner(engine))
    from open_brain_engine.engine import embedding_cache as cache

    cache.rebuild(
        engine, authority=_owner(engine), backend=FakeBackend(component), cancelled=lambda: False
    )
    root = engine.profile.root / ".open-brain" / "cache" / "embedding-v1"
    for path in (root / "generations").iterdir():
        for member in path.iterdir():
            member.write_bytes(b"corrupt")
    health = _health(engine, base)
    assert (health["cache_state"], health["last_error"]) == ("stale", "cache_invalid")


def test_untrusted_and_unsupported_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _component = _installed(tmp_path)
    engine = _brain(tmp_path)
    monkeypatch.setattr(eh, "current_platform", lambda: eh._fail("unsupported_platform"))
    health = _health(engine, base)
    assert (health["resource_state"], health["last_error"]) == (
        "unsupported",
        "unsupported_platform",
    )
    assert health["component_id"] is None
    monkeypatch.undo()
    (base.parent.parent / eh.INVENTORY_PATH).write_bytes(b"{}")
    health = _health(engine, base)
    assert (health["resource_state"], health["last_error"]) == ("untrusted", "resource_untrusted")


def _cli(root: Path, *args: str) -> int:
    return run_cli(
        (*args, "--data-dir", str(root), "--json"),
        platform_name="darwin",
        filesystem_type_probe=lambda _path, _platform: "apfs",
    )


def test_status_doctor_and_embedding_status_include_health(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr("sys.frozen", raising=False)
    root = tmp_path / "brain"
    BrainEngine.open(compile_single_user_local(root))
    for args in (("status",), ("doctor", "--check", "foreground-runtime"), ("embedding", "status")):
        assert _cli(root, *args) in (0, 1)
        payload = json.loads(capsys.readouterr().out)
        health = payload["embedding_health"]
        assert set(health) == KEYS
        assert health["schema"] == "ob.embedding-health.v1"
    assert payload["schema"] == "ob.embedding-policy-status.v1"


def test_health_is_not_an_mcp_surface() -> None:
    assert not any("embedding" in name or "health" in name for name in MCP_REGISTERED_TOOLS)


def test_health_is_not_a_plugin_or_harness_surface() -> None:
    services = Path(eh.__file__).parent
    for name in ("plugin_bridge.py", "local_mcp.py", "harness_native_entrypoint.py"):
        assert "embedding_health" not in (services / name).read_text(encoding="utf-8")
