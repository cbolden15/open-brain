from __future__ import annotations

import platform
import plistlib
from importlib.resources import files
from pathlib import Path

import pytest

import open_brain.services.appliance_lifecycle as appliance_lifecycle
from open_brain.services.appliance_lifecycle import run_supervisor_action
from open_brain.services.appliance_supervisors import (
    LaunchdSupervisor,
    SupervisorCommandError,
    SystemdSupervisor,
)


def test_launchd_manifest_is_deterministic_and_source_checkout_safe(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    checkout_root = Path(__file__).parents[3]
    launch_agents = tmp_path / "LaunchAgents"
    supervisor = LaunchdSupervisor(
        root=root,
        checkout_root=checkout_root,
        python_executable="/usr/bin/python3",
        unit_directory=launch_agents,
        user_id=501,
    )

    first = supervisor.render()
    second = supervisor.render()
    payload = plistlib.loads(first.encode("utf-8"))

    assert first == second
    assert payload["Label"] == "org.open-brain.appliance-daemon"
    assert payload["ProgramArguments"] == [
        "/usr/bin/python3",
        "-m",
        "open_brain.services.appliance_daemon",
        "--root",
        str(root),
    ]
    assert payload["EnvironmentVariables"] == {"PYTHONPATH": str(checkout_root / "src")}
    assert payload["WorkingDirectory"] == str(checkout_root)
    assert payload["RunAtLoad"] is False
    assert payload["KeepAlive"] is True
    assert payload["Umask"] == 0o077
    assert payload["StandardOutPath"] == str(root / ".open-brain" / "run" / "daemon.stdout.log")
    assert payload["StandardErrorPath"] == str(root / ".open-brain" / "run" / "daemon.stderr.log")
    assert "credential" not in first.casefold()


def test_systemd_manifest_is_deterministic_and_source_checkout_safe(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    checkout_root = Path(__file__).parents[3]
    units = tmp_path / "systemd"
    supervisor = SystemdSupervisor(
        root=root,
        checkout_root=checkout_root,
        python_executable="/usr/bin/python3",
        unit_directory=units,
    )

    first = supervisor.render()
    second = supervisor.render()

    assert first == second
    assert "Description=Open Brain appliance daemon" in first
    assert "Type=simple" in first
    assert "Restart=on-failure" in first
    assert (
        f"ExecStart=/usr/bin/python3 -m open_brain.services.appliance_daemon --root {root}"
        in first
    )
    assert f"Environment=PYTHONPATH={checkout_root / 'src'}" in first
    assert f"WorkingDirectory={checkout_root}" in first
    assert "open-brain-http" not in first
    assert "credential" not in first.casefold()


def test_systemd_manifest_escapes_arguments_without_shell_only_quoting(tmp_path: Path) -> None:
    root = tmp_path / "brain root%name"
    checkout_root = tmp_path / "checkout root%name"
    supervisor = SystemdSupervisor(
        root=root,
        checkout_root=checkout_root,
        python_executable="/usr/local/bin/python 3",
        unit_directory=tmp_path / "systemd",
    )

    rendered = supervisor.render()

    assert "ExecStart=" in rendered
    assert "'" not in rendered
    assert "\\x20" in rendered
    assert "%%" in rendered


def test_supervisor_templates_are_declared_package_resources() -> None:
    resources = files("open_brain").joinpath("resources/supervisors")

    assert resources.joinpath("launchd.json").is_file()
    assert resources.joinpath("systemd.service").is_file()


def test_installed_supervisor_manifests_do_not_require_source_checkout(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    launchd = LaunchdSupervisor(
        root=root,
        checkout_root=None,
        python_executable="/opt/open-brain/bin/python",
        unit_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    systemd = SystemdSupervisor(
        root=root,
        checkout_root=None,
        python_executable="/opt/open-brain/bin/python",
        unit_directory=tmp_path / "systemd",
    )

    launchd_payload = plistlib.loads(launchd.render().encode("utf-8"))
    systemd_payload = systemd.render()

    assert "EnvironmentVariables" not in launchd_payload
    assert "WorkingDirectory" not in launchd_payload
    assert "PYTHONPATH" not in systemd_payload
    assert "WorkingDirectory=" not in systemd_payload
    assert str(root) in systemd_payload


def test_lifecycle_factory_selects_installed_supervisor_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_module = (
        tmp_path
        / "venv/lib/python3.14/site-packages/open_brain/services/appliance_lifecycle.py"
    )
    monkeypatch.setattr(appliance_lifecycle, "__file__", str(installed_module))
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    rendered = appliance_lifecycle._supervisor((tmp_path / "brain").resolve()).render()

    assert "PYTHONPATH" not in rendered
    assert "WorkingDirectory=" not in rendered


def test_supervisor_actions_are_allowlisted_and_do_not_expose_dynamic_attributes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"

    with pytest.raises(ValueError, match="supervisor request"):
        run_supervisor_action(root, action="__getattribute__")

    with pytest.raises(ValueError, match="supervisor request"):
        run_supervisor_action(root, action="render")


def test_launchd_lifecycle_uses_only_injected_file_and_command_adapters(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    checkout_root = Path(__file__).parents[3]
    writes: list[tuple[Path, str]] = []
    removals: list[Path] = []
    commands: list[tuple[str, ...]] = []

    def run_command(argv: tuple[str, ...]) -> str:
        commands.append(tuple(argv))
        return "ok"

    supervisor = LaunchdSupervisor(
        root=root,
        checkout_root=checkout_root,
        python_executable="/usr/bin/python3",
        unit_directory=tmp_path / "LaunchAgents",
        user_id=501,
        write_file=lambda path, content: writes.append((path, content)),
        remove_file=lambda path: removals.append(path),
        run_command=run_command,
    )

    supervisor.install()
    supervisor.start()
    supervisor.stop()
    supervisor.quiesce()
    supervisor.resume()
    supervisor.restart()
    assert supervisor.status() == "ok"
    supervisor.remove()

    assert [path for path, _content in writes] == [supervisor.unit_path]
    assert commands == [
        ("launchctl", "bootstrap", "gui/501", str(supervisor.unit_path)),
        ("launchctl", "kickstart", "-k", "gui/501/org.open-brain.appliance-daemon"),
        ("launchctl", "kill", "TERM", "gui/501/org.open-brain.appliance-daemon"),
        ("launchctl", "bootout", "gui/501", str(supervisor.unit_path)),
        ("launchctl", "bootstrap", "gui/501", str(supervisor.unit_path)),
        ("launchctl", "kickstart", "-k", "gui/501/org.open-brain.appliance-daemon"),
        ("launchctl", "kill", "TERM", "gui/501/org.open-brain.appliance-daemon"),
        ("launchctl", "kickstart", "-k", "gui/501/org.open-brain.appliance-daemon"),
        ("launchctl", "print", "gui/501/org.open-brain.appliance-daemon"),
        ("launchctl", "bootout", "gui/501", str(supervisor.unit_path)),
    ]
    assert removals == [supervisor.unit_path]
    assert not (root / ".open-brain" / ".open-brain-locks").exists()


def test_systemd_install_failure_cleans_up_only_the_incomplete_unit(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    checkout_root = Path(__file__).parents[3]
    writes: list[tuple[Path, str]] = []
    removals: list[Path] = []
    commands: list[tuple[str, ...]] = []

    def run_command(argv: tuple[str, ...]) -> str:
        commands.append(tuple(argv))
        if argv[:3] == ("systemctl", "--user", "enable"):
            raise SupervisorCommandError("load failed")
        return "ok"

    supervisor = SystemdSupervisor(
        root=root,
        checkout_root=checkout_root,
        python_executable="/usr/bin/python3",
        unit_directory=tmp_path / "systemd",
        write_file=lambda path, content: writes.append((path, content)),
        remove_file=lambda path: removals.append(path),
        run_command=run_command,
    )

    with pytest.raises(SupervisorCommandError, match="action failed"):
        supervisor.install()

    assert [path for path, _content in writes] == [supervisor.unit_path]
    assert commands == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", supervisor.unit_name),
    ]
    assert removals == [supervisor.unit_path]
    assert not (root / ".open-brain" / ".open-brain-locks").exists()


@pytest.mark.parametrize(
    ("root", "checkout_root", "python_executable", "unit_directory"),
    (
        (Path("relative"), Path("/tmp/checkout"), "/usr/bin/python3", Path("/tmp/units")),
        (Path("/tmp/root"), Path("relative"), "/usr/bin/python3", Path("/tmp/units")),
        (Path("/tmp/root"), Path("/tmp/checkout"), "python\n3", Path("/tmp/units")),
        (Path("/tmp/root"), Path("/tmp/checkout"), "/usr/bin/python3", Path("relative")),
    ),
)
def test_supervisor_rejects_relative_newline_and_specifier_inputs(
    root: Path,
    checkout_root: Path,
    python_executable: str,
    unit_directory: Path,
) -> None:
    with pytest.raises(ValueError, match="supervisor configuration"):
        SystemdSupervisor(
            root=root,
            checkout_root=checkout_root,
            python_executable=python_executable,
            unit_directory=unit_directory,
        )


def test_launchd_install_failure_returns_bounded_generic_evidence(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    checkout_root = Path(__file__).parents[3]

    def run_command(argv: tuple[str, ...]) -> str:
        del argv
        raise RuntimeError(f"credential leaked at {root}/token.txt")

    supervisor = LaunchdSupervisor(
        root=root,
        checkout_root=checkout_root,
        python_executable="/usr/bin/python3",
        unit_directory=tmp_path / "LaunchAgents",
        user_id=501,
        run_command=run_command,
    )

    with pytest.raises(SupervisorCommandError) as error:
        supervisor.install()

    message = str(error.value)
    assert message
    assert "credential" not in message.casefold()
    assert str(root) not in message
