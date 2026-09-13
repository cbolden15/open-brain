from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes

from open_brain.services.appliance_auth import derive_appliance_credential
from open_brain.services.appliance_daemon import ApplianceDaemon
from open_brain.services.appliance_init import (
    APPLIANCE_OWNER_CREDENTIAL,
    initialize_appliance,
)
from open_brain.services.appliance_lifecycle import (
    ApplianceLifecycleError,
    ApplianceLifecycleFailureReceipt,
)
from open_brain.services.appliance_scheduler import (
    APPLIANCE_SCHEDULER_DIRECTORY,
    ApplianceRunReceipt,
)
from open_brain.services.runtime import appliance_http_configuration_from_environment

_ORIGINAL_SOCKET_BIND = socket.socket.bind
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SOCKET_LISTEN = socket.socket.listen


@pytest.fixture(autouse=True)
def allow_unix_sockets_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def bind(self: socket.socket, address: str | bytes | tuple[object, ...]) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_BIND(self, address)
        raise AssertionError("network access is forbidden in the Phase 2 test suite")

    def connect(self: socket.socket, address: str | bytes | tuple[object, ...]) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT(self, address)
        raise AssertionError("network access is forbidden in the Phase 2 test suite")

    def connect_ex(self: socket.socket, address: str | bytes | tuple[object, ...]) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT_EX(self, address)
        raise AssertionError("network access is forbidden in the Phase 2 test suite")

    def listen(self: socket.socket, backlog: int = 0) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_LISTEN(self, backlog)
        raise AssertionError("network access is forbidden in the Phase 2 test suite")

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "listen", listen)


def test_browser_run_history_route_stays_metadata_only_and_root_confined(tmp_path: Path) -> None:
    del tmp_path
    with tempfile.TemporaryDirectory(prefix="ob-", dir=Path("/tmp").resolve()) as directory:
        root = Path(directory) / "brain"
        initialize_appliance(root)
        browser = derive_appliance_credential(
            (root / APPLIANCE_OWNER_CREDENTIAL).read_text(encoding="utf-8").strip(),
            purpose="browser-bootstrap",
        )
        run_directory = root / APPLIANCE_SCHEDULER_DIRECTORY / "runs" / "engine-recover"
        outside = Path(directory) / "outside-run.json"
        outside.write_bytes(
            canonical_json_bytes(
                ApplianceRunReceipt(
                    job_name="engine-recover",
                    run_id="run_22222222-2222-4222-8222-222222222222",
                    attempt=1,
                    status="completed",
                    started_at="2026-09-01T09:00:00Z",
                    finished_at="2026-09-01T09:00:01Z",
                    next_due_at="2026-09-01T10:00:00Z",
                    reason="outside_canary",
                ).to_dict()
            )
        )
        with ApplianceDaemon(root) as daemon:
            control_thread = threading.Thread(target=daemon.serve_until_stopped)
            control_thread.start()
            _wait_for_run_receipt(run_directory)
            (run_directory / "run_invalid.json").write_bytes(
                canonical_json_bytes(
                    {
                        "argv": ["password=secret", "/private/runtime/canary"],
                        "body": "csrf%3Dencoded",
                        "cookie": "session%3Dencoded",
                        "url": "https://example.test/private?token=secret",
                    }
                )
            )
            (run_directory / "run_22222222-2222-4222-8222-222222222222.json").symlink_to(outside)
            configuration = appliance_http_configuration_from_environment(
                {
                    "OPEN_BRAIN_UI_BIND": "127.0.0.1",
                    "OPEN_BRAIN_UI_PORT": "8788",
                    "OPEN_BRAIN_UI_ALLOW_PRIVATE": "false",
                }
            )
            lifecycle = daemon._compose_http_service(configuration)
            login_payload = canonical_json_bytes({"credential": browser})
            login = lifecycle.dispatch(
                method="POST",
                path="/auth/login",
                headers=(
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(login_payload))),
                    ("Origin", configuration.allowed_origin),
                ),
                body_reader=lambda _maximum_bytes, _timeout_seconds: login_payload,
            )
            login_json = json.loads(login.body)
            cookie = dict(login.headers)["Set-Cookie"].split(";", 1)[0]
            runs = lifecycle.dispatch(
                method="GET",
                path="/api/runs?limit=5",
                headers=(("Cookie", cookie),),
                body_reader=lambda _maximum_bytes, _timeout_seconds: b"",
            )
            daemon.stop()
            control_thread.join(timeout=5)

    payload = json.loads(runs.body)
    rendered = json.dumps(payload, sort_keys=True)

    assert runs.status == 200
    assert payload["status"] == "ok"
    assert len(payload["runs"]) >= 1
    assert payload["runs"][0].keys() == {
        "attempt",
        "finished_at",
        "job_name",
        "next_due_at",
        "reason",
        "run_id",
        "started_at",
        "status",
    }
    assert "outside_canary" not in rendered
    assert "password=secret" not in rendered
    assert "/private/runtime/canary" not in rendered
    assert "csrf%3Dencoded" not in rendered
    assert "session%3Dencoded" not in rendered
    assert "https://example.test/private?token=secret" not in rendered
    assert cookie not in rendered
    assert login_json["csrf_token"] not in rendered


def test_appliance_lifecycle_failures_stay_bounded_for_log_envelopes() -> None:
    error = ApplianceLifecycleError(
        ApplianceLifecycleFailureReceipt(
            operation="upgrade",
            request_id="upgrade_123e4567-e89b-42d3-a456-4266141744aa",
            status="failed",
            failure_stage="doctor",
            candidate_id="candidate_source-checkout-v110",
            prior_candidate_id="candidate_current-v1",
            active_candidate_id="candidate_current-v1",
            rollback_state="rollback_failed",
        )
    )
    rendered = json.dumps(error.receipt.to_dict(), sort_keys=True)

    assert str(error) == "appliance lifecycle failed"
    assert "/private/brain-root" not in rendered
    assert "/private/backup-root" not in rendered
    assert "/private/candidate-checkout" not in rendered
    assert "password=secret" not in rendered
    assert "RuntimeError" not in rendered
    assert "/private/brain-root" not in str(error)


def _wait_for_run_receipt(path: Path, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if any(
                entry.name.startswith("run_")
                and entry.suffix == ".json"
                and not entry.is_symlink()
                and entry.is_file()
                for entry in path.iterdir()
            ):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for a run receipt in {path}")
