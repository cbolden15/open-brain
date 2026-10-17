"""Owner-local ``embedding rebuild`` CLI: source execution and exact JSON receipt shape."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import BrainEngine, TextPayload

from open_brain.profile import compile_single_user_local
from open_brain.services.embedding_policy_cli import run_embedding_command
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.t03_adapters import owner_authority
from packages.app.tests.unit.engine.test_embedding_cache import COMPONENT, FakeBackend


def _cli(root: Path, *args: str) -> int:
    return run_cli(
        (*args, "--data-dir", str(root), "--json"),
        platform_name="darwin",
        filesystem_type_probe=lambda _path, _platform: "apfs",
    )


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return cast(dict[str, object], json.loads(captured.out))


def _engine(root: Path, *, enable: bool) -> BrainEngine:
    root.parent.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(root))
    engine.capture.accept(TextPayload("Synthetic rebuild ledger"), delivery_id="rebuild.one")
    if enable:
        engine.embedding_policy.enable(
            COMPONENT, authority=owner_authority(engine.tasks, session_id="owner-test")
        )
    return engine


def test_source_execution_reports_resource_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    _engine(root, enable=True)
    assert _cli(root, "embedding", "rebuild") == 78
    assert _output(capsys) == {"error": {"code": "resource_missing"}}
    assert not (root / ".open-brain" / "cache" / "embedding-v1" / "active.json").exists()


def test_rebuild_receipt_shape_with_injected_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _engine(tmp_path / "brain", enable=True)
    seen: list[Path] = []

    def factory(root: Path, cancelled: object) -> FakeBackend:
        assert callable(cancelled) and cancelled() is False
        seen.append(root)
        return FakeBackend()

    parsed = argparse.Namespace(embedding_action="rebuild")
    assert (
        run_embedding_command(parsed, engine.tasks, json_output=True, backend_factory=factory) == 0
    )
    output = _output(capsys)
    assert set(output) == {
        "schema",
        "generation_id",
        "records",
        "chunks",
        "dropped_changed",
        "uncovered",
        "excluded_oversize",
        "seconds",
    }
    assert output["schema"] == "ob.embedding-rebuild.v1"
    assert output["records"] == 1 and output["dropped_changed"] == 0
    assert output["uncovered"] == 0 and output["excluded_oversize"] == 0
    assert isinstance(output["chunks"], int) and output["chunks"] >= 2
    assert isinstance(output["seconds"], float)
    assert seen == [engine.profile.root]


def test_rebuild_without_consent_is_a_bounded_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _engine(tmp_path / "brain", enable=False)
    parsed = argparse.Namespace(embedding_action="rebuild")
    assert (
        run_embedding_command(
            parsed, engine.tasks, json_output=True, backend_factory=lambda *_: FakeBackend()
        )
        == 78
    )
    assert _output(capsys) == {"error": {"code": "consent_required"}}
