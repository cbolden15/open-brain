from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

import open_brain.services.native_entrypoint as native_entrypoint
from open_brain.services.appliance_lifecycle import ApplianceLifecycleService
from open_brain.services.appliance_supervisors import LaunchdSupervisor, SystemdSupervisor
from open_brain.services.native_artifacts import (
    NATIVE_ARTIFACT_KIND,
    NativeArtifactLifecycleAdapter,
    NativeArtifactManifest,
    native_platform_tag,
)


def test_native_entrypoint_routes_cli_daemon_and_connector_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, object]] = []

    def cli(argv: tuple[str, ...], *, environment: dict[str, str]) -> int:
        observed.append(("cli", (argv, environment)))
        return 11

    def daemon(
        argv: tuple[str, ...],
        *,
        environment: dict[str, str],
    ) -> int:
        observed.append(("daemon", (argv, environment)))
        return 12

    def worker() -> int:
        observed.append(("worker", None))
        return 13

    monkeypatch.setattr(native_entrypoint, "run_cli", cli)
    monkeypatch.setattr(native_entrypoint, "run_appliance_daemon", daemon)
    monkeypatch.setattr(native_entrypoint, "run_connector_worker", worker)
    environment = {"OPEN_BRAIN_ROOT": "/tmp/brain"}

    assert native_entrypoint.main(("--version",), environment=environment) == 11
    assert (
        native_entrypoint.main(
            ("__appliance-daemon", "--root", "/tmp/brain"),
            environment=environment,
        )
        == 12
    )
    assert native_entrypoint.main(("__connector-worker",), environment=environment) == 13
    assert observed == [
        ("cli", (("--version",), environment)),
        ("daemon", (("--root", "/tmp/brain"), environment)),
        ("worker", None),
    ]


def test_native_entrypoint_ships_no_mutation_capable_smoke_commands() -> None:
    source = inspect.getsource(native_entrypoint)

    assert "__native-portable-self-check" not in source
    assert "__native-rollback-self-check" not in source


def test_native_self_check_exercises_packaged_resources_and_direct_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = Path("/opt/open-brain/current/open-brain")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(native_entrypoint, "native_platform_tag", lambda: "macos-arm64")

    assert native_entrypoint.main(("__native-self-check",), environment={}) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "artifact_kind": "native-onedir",
        "connector_child": "direct-frozen-executable",
        "daemon_launch": "direct-frozen-executable",
        "frozen": True,
        "package_resources": "available",
        "platform": "macos-arm64",
        "status": "ok",
    }
    assert str(executable) not in output


def test_frozen_lifecycle_commands_receive_real_native_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "brain").resolve()
    root.mkdir()
    install_root = (tmp_path / "install").resolve()
    candidate = install_root / "candidates/candidate_native-v1"
    candidate.mkdir(parents=True)
    executable = candidate / "open-brain"
    executable.write_bytes(b"native executable")
    executable.chmod(0o755)
    NativeArtifactManifest.create(
        candidate,
        candidate_id=candidate.name,
        version="0.1.0",
        platform_tag=native_platform_tag(),
    ).write(candidate)
    (install_root / "current").symlink_to(Path("candidates") / candidate.name)
    observed: list[object] = []

    def cli(
        argv: tuple[str, ...],
        *,
        environment: dict[str, str],
        lifecycle: object | None = None,
    ) -> int:
        del argv, environment
        observed.append(lifecycle)
        return 0

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(native_entrypoint, "run_cli", cli)

    assert (
        native_entrypoint.main(
            ("uninstall", "--confirm-owner"),
            environment={"OPEN_BRAIN_ROOT": str(root)},
        )
        == 0
    )

    assert len(observed) == 1
    lifecycle = observed[0]
    assert isinstance(lifecycle, ApplianceLifecycleService)
    assert isinstance(lifecycle._artifact_port, NativeArtifactLifecycleAdapter)
    supervisor = lifecycle._supervisor
    assert isinstance(supervisor, LaunchdSupervisor | SystemdSupervisor)
    assert supervisor.runtime_kind == NATIVE_ARTIFACT_KIND
    assert supervisor.checkout_root is None
    rendered = supervisor.render()
    assert " -m " not in rendered
