from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_brain.profile import compile_single_user_local
from open_brain.services.appliance_daemon import (
    ApplianceControlUnavailableError,
    CliControlReceipt,
    StatusControlReceipt,
)
from open_brain.services.appliance_entrypoints import run_cli, run_mcp
from open_brain.services.appliance_init import initialize_appliance
from open_brain.services.appliance_lifecycle import (
    ApplianceUninstallReceipt,
    ApplianceUpgradeReceipt,
    ArtifactCandidate,
    LifecycleMigrationReceipt,
    OwnerLifecycleRequest,
)
from open_brain.services.appliance_supervisors import SupervisorCommandError


def test_appliance_cli_help_and_version_are_root_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(("--help",), environment={}) == 0
    help_output = capsys.readouterr().out
    assert "daemon" in help_output
    assert "init" in help_output
    assert run_cli(("--version",), environment={}) == 0
    assert capsys.readouterr().out == "open-brain 0.1.0\n"


def test_appliance_daemon_command_launches_foreground_with_the_selected_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    environment = {
        "OPEN_BRAIN_PROVIDER": "none",
        "OPEN_BRAIN_ROOT": str(root),
    }
    observed: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run_daemon(
        argv: tuple[str, ...],
        *,
        environment: dict[str, str],
    ) -> int:
        observed.append((argv, environment))
        return 0

    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.run_appliance_daemon",
        run_daemon,
    )

    assert run_cli(("daemon",), environment=environment) == 0
    assert observed == [(("--root", str(root)), environment)]


def test_appliance_status_reports_bounded_maintenance_without_leaking_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Personal",))

    exit_code = run_cli(("status", "--json"), environment={"OPEN_BRAIN_ROOT": str(root)})
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["maintenance"]["schema"]["state"] == "current"
    assert str(root) not in json.dumps(payload, sort_keys=True)


def test_appliance_mcp_rejects_absent_schema_without_mutating_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    compile_single_user_local(root)
    environment = {
        "OPEN_BRAIN_ROOT": str(root),
        "OPEN_BRAIN_MCP_ALLOWED_SPACE_IDS": "[]",
    }

    monkeypatch.setattr("open_brain.services.appliance_entrypoints.os.environ", environment)

    assert run_mcp() == 78
    assert not (root / ".open-brain" / "state" / "phase1.sqlite3").exists()
    assert not (root / ".open-brain" / ".open-brain-locks").exists()


@pytest.mark.parametrize(
    ("arguments", "command", "envelope"),
    (
        (
            ("capture", "quick", "text", "Synthetic text", "--delivery=delivery.capture", "--json"),
            "capture",
            {
                "canonical": False,
                "capture_id": "capture_123e4567-e89b-42d3-a456-426614174101",
                "command": "capture",
                "duplicate": False,
                "enrichment_state": "pending_enrichment",
                "payload_family": "text",
                "space_id": None,
                "state": "inbox",
                "status": "accepted",
            },
        ),
        (
            ("inbox", "list", "--json"),
            "inbox",
            {"captures": [], "command": "inbox", "status": "listed"},
        ),
        (
            ("proposals", "list", "--json"),
            "proposals",
            {"command": "proposals", "proposals": [], "status": "listed"},
        ),
        (
            (
                "review",
                "approve",
                "proposal_123e4567-e89b-42d3-a456-426614174102",
                "--delivery=delivery.review",
                "--json",
            ),
            "review",
            {
                "action": "approve",
                "command": "review",
                "decision_id": "decision_123e4567-e89b-42d3-a456-426614174103",
                "duplicate": False,
                "page_id": "page_123e4567-e89b-42d3-a456-426614174104",
                "proposal_id": "proposal_123e4567-e89b-42d3-a456-426614174102",
                "publication_id": "publication_123e4567-e89b-42d3-a456-426614174105",
                "state": "approved",
                "status": "decided",
            },
        ),
        (
            ("spaces", "list", "--json"),
            "spaces",
            {"command": "spaces", "spaces": [], "status": "listed"},
        ),
    ),
)
def test_appliance_cli_routes_retained_phase1_families_only_through_control(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    command: str,
    envelope: dict[str, object],
) -> None:
    root = tmp_path / "brain"
    observed: list[object] = []

    def dispatch(path: Path, *, command: str, argv: tuple[str, ...]) -> CliControlReceipt:
        observed.extend((path, command, argv))
        return CliControlReceipt(command=command, envelope=envelope, exit_code=0)

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("local mutation or offline fallback is forbidden")

    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.dispatch_phase1_command",
        dispatch,
    )
    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.ApplianceApplication.open_read_only",
        forbidden,
    )
    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints._control_active",
        lambda _root: True,
    )

    exit_code = run_cli(arguments, environment={"OPEN_BRAIN_ROOT": str(root)})
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload == envelope
    assert observed == [
        root,
        command,
        tuple(argument for argument in arguments[1:] if argument != "--json"),
    ]


def test_appliance_status_prefers_control_when_the_daemon_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    observed: list[Path] = []

    def status(path: Path) -> StatusControlReceipt:
        observed.append(path)
        return StatusControlReceipt(
            envelope={
                "maintenance": {"schema": {"state": "current", "version": 1}},
                "owner_actor_id": "actor_123e4567-e89b-42d3-a456-426614174106",
                "provider_mode": "none",
                "status": "ok",
                "tenant_id": "tenant_123e4567-e89b-42d3-a456-426614174107",
            }
        )

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("offline status fallback is forbidden while control is available")

    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.read_status_via_control",
        status,
    )
    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.read_appliance_status",
        forbidden,
    )
    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints._control_active",
        lambda _root: True,
    )

    exit_code = run_cli(("status", "--json"), environment={"OPEN_BRAIN_ROOT": str(root)})
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert observed == [root]


def test_appliance_supervisor_discovery_is_root_local_and_does_not_use_control(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    observed: list[tuple[Path, str]] = []

    def discover(path: Path, *, action: str) -> dict[str, object]:
        observed.append((path, action))
        return {
            "action": action,
            "command": "supervisor",
            "status": "ok",
            "supervisor": "launchd",
            "unit_name": "org.open-brain.appliance-daemon",
        }

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError(
            "phase1 control dispatch is forbidden for root-local supervisor actions"
        )

    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.run_supervisor_action",
        discover,
    )
    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.dispatch_phase1_command",
        forbidden,
    )

    exit_code = run_cli(
        ("supervisor", "discover", "--json"),
        environment={"OPEN_BRAIN_ROOT": str(root)},
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["command"] == "supervisor"
    assert payload["action"] == "discover"
    assert observed == [(root, "discover")]


def test_appliance_query_falls_back_to_the_read_only_app_when_control_is_unavailable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Personal",))
    observed: list[object] = []
    open_read_only = run_mcp.__globals__["ApplianceApplication"].open_read_only

    def unavailable(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ApplianceControlUnavailableError("unavailable")

    def record_read_only(
        path: Path,
        *,
        allowed_space_ids: frozenset[str] = frozenset(),
    ) -> object:
        observed.append(path)
        return open_read_only(path, allowed_space_ids=allowed_space_ids)

    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.dispatch_phase1_command",
        unavailable,
    )
    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.ApplianceApplication.open_read_only",
        record_read_only,
    )
    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.ApplianceApplication.open_mutating",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("local mutation is forbidden")
        ),
    )

    exit_code = run_cli(
        ("query", "synthetic", "--json"),
        environment={"OPEN_BRAIN_ROOT": str(root)},
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["command"] == "query"
    assert payload["status"] == "ok"
    assert observed == [root]


def test_appliance_cli_configuration_and_supervisor_failures_are_bounded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(("status", "--json"), environment={}) == 78
    configuration = json.loads(capsys.readouterr().out)
    assert configuration["status"] == "failed"
    assert configuration["error"]["redacted"] is True

    root = tmp_path / "private-root-canary"

    def failed_supervisor(path: Path, *, action: str) -> dict[str, object]:
        assert path == root
        assert action == "install"
        raise SupervisorCommandError(f"credential at {root}/token")

    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.run_supervisor_action",
        failed_supervisor,
    )
    assert run_cli(
        ("supervisor", "install", "--json"),
        environment={"OPEN_BRAIN_ROOT": str(root)},
    ) == 1
    failure = json.loads(capsys.readouterr().out)
    rendered = json.dumps(failure, sort_keys=True)
    assert failure["command"] == "supervisor"
    assert failure["status"] == "failed"
    assert "credential" not in rendered
    assert str(root) not in rendered


def test_appliance_offline_query_fails_boundedly_for_oversized_results(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    provenance = SimpleNamespace(as_dict=lambda: {"source_type": "note"})
    result = SimpleNamespace(
        capture_id="capture_123e4567-e89b-42d3-a456-426614174180",
        excerpt="x" * 5_000,
        explanation="synthetic",
        payload_family="text",
        provenance=provenance,
        record_type="canonical",
        result_id="result_123e4567-e89b-42d3-a456-426614174181",
        space_id=None,
        title="Synthetic",
        trust="owner",
    )
    application = SimpleNamespace(
        retrieval=SimpleNamespace(search=lambda *args, **kwargs: (result,))
    )
    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints._control_active",
        lambda _root: False,
    )
    monkeypatch.setattr(
        "open_brain.services.appliance_entrypoints.ApplianceApplication.open_read_only",
        lambda _root: application,
    )

    assert run_cli(
        ("query", "synthetic", "--json"),
        environment={"OPEN_BRAIN_ROOT": str(root)},
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "query"
    assert payload["status"] == "failed"
    assert "x" * 100 not in json.dumps(payload)


def test_appliance_cli_exposes_injected_upgrade_and_uninstall_without_path_residue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "brain"
    lifecycle = _RecordingLifecycleCommands()
    environment = {"OPEN_BRAIN_ROOT": str(root)}

    upgrade_exit = run_cli(
        (
            "upgrade",
            "--request-id=upgrade_123e4567-e89b-42d3-a456-426614174430",
            "--requested-at=2026-09-01T14:00:00Z",
            "--candidate-id=candidate_source-checkout-v110",
            "--version=1.1.0",
            f"--backup-destination={tmp_path / 'backup'}",
            f"--disposable-root={tmp_path / 'preflight'}",
            "--confirm-owner",
            "--json",
        ),
        environment=environment,
        lifecycle=lifecycle,
    )
    upgrade_payload = json.loads(capsys.readouterr().out)
    uninstall_exit = run_cli(
        (
            "uninstall",
            "--request-id=uninstall_123e4567-e89b-42d3-a456-426614174431",
            "--requested-at=2026-09-01T14:01:00Z",
            "--confirm-owner",
            "--json",
        ),
        environment=environment,
        lifecycle=lifecycle,
    )
    uninstall_payload = json.loads(capsys.readouterr().out)

    assert upgrade_exit == 0
    assert uninstall_exit == 0
    assert upgrade_payload["status"] == "upgraded"
    assert uninstall_payload["status"] == "uninstalled"
    assert lifecycle.operations == ["upgrade", "uninstall"]
    rendered = json.dumps((upgrade_payload, uninstall_payload), sort_keys=True)
    assert str(root) not in rendered
    assert str(tmp_path / "backup") not in rendered
    assert str(tmp_path / "preflight") not in rendered


def test_appliance_lifecycle_cli_fails_closed_without_injected_artifact_port(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "brain"
    exit_code = run_cli(
        (
            "upgrade",
            "--request-id=upgrade_123e4567-e89b-42d3-a456-426614174433",
            "--requested-at=2026-09-01T14:02:00Z",
            "--candidate-id=candidate_source-checkout-v110",
            "--version=1.1.0",
            f"--backup-destination={tmp_path / 'backup'}",
            f"--disposable-root={tmp_path / 'preflight'}",
            "--confirm-owner",
            "--json",
        ),
        environment={"OPEN_BRAIN_ROOT": str(root)},
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["command"] == "upgrade"
    assert payload["status"] == "unavailable"
    assert str(root) not in json.dumps(payload, sort_keys=True)


def test_appliance_cli_routes_the_explicit_native_artifact_kind(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "brain"
    lifecycle = _RecordingLifecycleCommands()

    exit_code = run_cli(
        (
            "upgrade",
            "--request-id=upgrade_123e4567-e89b-42d3-a456-426614174434",
            "--requested-at=2026-09-01T14:03:00Z",
            "--candidate-id=candidate_native-v020",
            "--version=0.2.0",
            "--artifact-kind=native-onedir",
            f"--backup-destination={tmp_path / 'backup'}",
            f"--disposable-root={tmp_path / 'preflight'}",
            "--confirm-owner",
            "--json",
        ),
        environment={"OPEN_BRAIN_ROOT": str(root)},
        lifecycle=lifecycle,
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "upgraded"
    assert lifecycle.candidates == [
        ArtifactCandidate(
            candidate_id="candidate_native-v020",
            version="0.2.0",
            artifact_kind="native-onedir",
        )
    ]


class _RecordingLifecycleCommands:
    def __init__(self) -> None:
        self.operations: list[str] = []
        self.candidates: list[ArtifactCandidate] = []

    def upgrade(
        self,
        *,
        owner_request: OwnerLifecycleRequest | None,
        candidate: ArtifactCandidate,
        backup_destination: Path,
        disposable_root: Path,
    ) -> ApplianceUpgradeReceipt:
        del backup_destination, disposable_root
        assert owner_request is not None
        self.operations.append("upgrade")
        self.candidates.append(candidate)
        return ApplianceUpgradeReceipt(
            request_id=owner_request.request_id,
            status="upgraded",
            candidate_id=candidate.candidate_id,
            prior_candidate_id="candidate_current-v1",
            active_candidate_id=candidate.candidate_id,
            compatibility_state="compatible",
            backup_id="backup_123e4567-e89b-42d3-a456-426614174432",
            manifest_digest_sha256="a" * 64,
            preflight_state="ready",
            migrations=(
                LifecycleMigrationReceipt("engine", "1.0.0", "1.1.0", "applied"),
                LifecycleMigrationReceipt("app", "1.0.0", "1.1.0", "applied"),
            ),
            activation_state="activated",
            restart_state="restarted",
            doctor_state="healthy",
        )

    def uninstall(
        self,
        *,
        owner_request: OwnerLifecycleRequest | None,
    ) -> ApplianceUninstallReceipt:
        assert owner_request is not None
        self.operations.append("uninstall")
        return ApplianceUninstallReceipt(
            request_id=owner_request.request_id,
            status="uninstalled",
            prior_candidate_id="candidate_current-v1",
            daemon_stop_state="stopped",
            supervisor_remove_state="removed",
            artifact_remove_state="removed",
            brain_root_state="preserved",
        )
