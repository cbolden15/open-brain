from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from open_brain.integrations.ui import UiBindConfig
from open_brain.services.appliance_auth import derive_appliance_credential
from open_brain.services.appliance_daemon import (
    ApplianceDaemon,
    CliControlRequest,
    request_cli_dispatch,
    request_status,
)
from open_brain.services.appliance_init import APPLIANCE_OWNER_CREDENTIAL, initialize_appliance
from open_brain.services.appliance_scheduler import APPLIANCE_SCHEDULER_DIRECTORY
from open_brain.services.runtime import (
    ApplianceHttpConfiguration,
    ServiceConfigurationError,
    appliance_http_configuration_from_environment,
)

_ORIGINAL_SOCKET_BIND = socket.socket.bind
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SOCKET_LISTEN = socket.socket.listen
_PRIVATE_BIND_HOST = ".".join(("192", "168", "1", "10"))


@pytest.fixture(autouse=True)
def allow_loopback_only_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def bind(
        self: socket.socket, address: str | bytes | tuple[object, ...]
    ) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_BIND(self, address)
        if (
            self.family == socket.AF_INET
            and isinstance(address, tuple)
            and address[0] == "127.0.0.1"
        ):
            return _ORIGINAL_SOCKET_BIND(self, address)
        raise AssertionError("non-loopback network access is forbidden in P3-W3 tests")

    def connect(
        self: socket.socket, address: str | bytes | tuple[object, ...]
    ) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT(self, address)
        if (
            self.family == socket.AF_INET
            and isinstance(address, tuple)
            and address[0] == "127.0.0.1"
        ):
            return _ORIGINAL_SOCKET_CONNECT(self, address)
        raise AssertionError("non-loopback network access is forbidden in P3-W3 tests")

    def connect_ex(
        self: socket.socket, address: str | bytes | tuple[object, ...]
    ) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT_EX(self, address)
        if (
            self.family == socket.AF_INET
            and isinstance(address, tuple)
            and address[0] == "127.0.0.1"
        ):
            return _ORIGINAL_SOCKET_CONNECT_EX(self, address)
        raise AssertionError("non-loopback network access is forbidden in P3-W3 tests")

    def listen(self: socket.socket, backlog: int = 0) -> object:
        if self.family in {socket.AF_UNIX, socket.AF_INET}:
            return _ORIGINAL_SOCKET_LISTEN(self, backlog)
        raise AssertionError("unsupported listener family")

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "listen", listen)
    monkeypatch.setattr(socket, "getfqdn", lambda name="": "localhost")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, family=0, type=0, proto=0, flags=0: [
            (socket.AF_INET, socket.SOCK_STREAM, proto or 6, "", ("127.0.0.1", port))
        ],
    )


def test_daemon_owned_http_listener_proves_browser_auth_route_separation_and_shared_state(
    tmp_path: Path,
) -> None:
    del tmp_path
    with tempfile.TemporaryDirectory(prefix="ob-", dir=Path("/tmp").resolve()) as directory:
        root = Path(directory) / "brain"
        initialize_appliance(root, starter_spaces=("Projects",))
        seed = (root / APPLIANCE_OWNER_CREDENTIAL).read_text(encoding="utf-8").strip()
        browser = derive_appliance_credential(seed, purpose="browser-bootstrap")
        intake = derive_appliance_credential(seed, purpose="intake-bearer")
        port = _free_port()
        configuration = appliance_http_configuration_from_environment(
            {
                "OPEN_BRAIN_UI_BIND": "127.0.0.1",
                "OPEN_BRAIN_UI_PORT": str(port),
                "OPEN_BRAIN_UI_ALLOW_PRIVATE": "false",
            }
        )

        with ApplianceDaemon(root) as daemon:
            control_thread = threading.Thread(target=daemon.serve_until_stopped)
            control_thread.start()
            _wait_for(
                root / APPLIANCE_SCHEDULER_DIRECTORY / "runs" / "engine-recover",
                predicate=lambda path: path.exists(),
            )
            daemon.start_http_listener(configuration)
            try:
                unauthorized_status = _request("GET", port, "/api/status")
                login = _request(
                    "POST",
                    port,
                    "/auth/login",
                    body={"credential": browser},
                    headers={"Origin": configuration.allowed_origin},
                )
                login_headers = cast(dict[str, str], login["headers"])
                login_json = _mapping(login["json"])
                cookie = login_headers["Set-Cookie"].split(";", 1)[0]
                csrf = cast(str, login_json["csrf_token"])

                blocked_share = _request(
                    "POST",
                    port,
                    "/share",
                    body={"url": "https://example.test/shared", "why": "share should fail"},
                    headers={
                        "Cookie": cookie,
                        "Origin": configuration.allowed_origin,
                        "X-CSRF-Token": csrf,
                    },
                )
                blocked_browser = _request(
                    "POST",
                    port,
                    "/api/spaces",
                    body={"delivery_id": "ui.space.forbidden", "name": "Forbidden"},
                    headers={
                        "Authorization": "Bearer " + intake,
                        "Origin": configuration.allowed_origin,
                    },
                )
                created_space = _request(
                    "POST",
                    port,
                    "/api/spaces",
                    body={"delivery_id": "ui.space.create", "name": "Browser UI"},
                    headers={
                        "Cookie": cookie,
                        "Origin": configuration.allowed_origin,
                        "X-CSRF-Token": csrf,
                    },
                )
                quick = _request(
                    "POST",
                    port,
                    "/api/captures/quick",
                    body={
                        "delivery_id": "ui.capture.quick",
                        "text": "Browser inbox token",
                    },
                    headers={
                        "Cookie": cookie,
                        "Origin": configuration.allowed_origin,
                        "X-CSRF-Token": csrf,
                    },
                )
                canonical = _request(
                    "POST",
                    port,
                    "/api/captures/canonical",
                    body={
                        "delivery_id": "ui.capture.canonical",
                        "space_id": _mapping(created_space["json"])["space_id"],
                        "text": "Browser canonical page token",
                    },
                    headers={
                        "Cookie": cookie,
                        "Origin": configuration.allowed_origin,
                        "X-CSRF-Token": csrf,
                    },
                )
                inbox = _request("GET", port, "/api/inbox", headers={"Cookie": cookie})
                spaces = _request("GET", port, "/api/spaces", headers={"Cookie": cookie})
                proposals = _request("GET", port, "/api/proposals", headers={"Cookie": cookie})
                search = _request(
                    "GET",
                    port,
                    "/api/search?q=Browser%20canonical%20page%20token",
                    headers={"Cookie": cookie},
                )
                search_json = _mapping(search["json"])
                search_results = _records(search_json["results"])
                page_id = cast(str, search_results[0]["result_id"])
                page = _request("GET", port, f"/pages/{page_id}", headers={"Cookie": cookie})
                status = _request("GET", port, "/api/status", headers={"Cookie": cookie})
                doctor = _request("GET", port, "/api/doctor", headers={"Cookie": cookie})
                runs = _request("GET", port, "/api/runs?limit=5", headers={"Cookie": cookie})
                control_status = request_status(root).envelope
                control_spaces = request_cli_dispatch(
                    root,
                    CliControlRequest(command="spaces", argv=("list",)),
                ).envelope
                control_inbox = request_cli_dispatch(
                    root,
                    CliControlRequest(command="inbox", argv=("list",)),
                ).envelope
            finally:
                daemon.stop()
                control_thread.join(timeout=5)

    created_space_json = _mapping(created_space["json"])
    quick_json = _mapping(quick["json"])
    canonical_json = _mapping(canonical["json"])
    inbox_json = _mapping(inbox["json"])
    inbox_captures = _records(inbox_json["captures"])
    spaces_json = _mapping(spaces["json"])
    space_rows = _records(spaces_json["spaces"])
    proposals_json = _mapping(proposals["json"])
    status_json = _mapping(status["json"])
    doctor_json = _mapping(doctor["json"])
    runs_json = _mapping(runs["json"])
    run_rows = _records(runs_json["runs"])
    configuration_json = _mapping(status_json["configuration"])
    http_json = _mapping(configuration_json["http"])
    ownership_json = _mapping(status_json["ownership"])
    maintenance_json = _mapping(status_json["maintenance"])
    schema_json = _mapping(maintenance_json["schema"])
    control_space_rows = _records(control_spaces["spaces"])
    control_inbox_rows = _records(control_inbox["captures"])

    assert unauthorized_status["status"] == 401
    assert login["status"] == 200
    assert blocked_share["status"] == 401
    assert blocked_browser["status"] == 401
    assert created_space_json["status"] == "created"
    assert quick_json["state"] == "inbox"
    assert canonical_json["canonical"] is True
    assert inbox_captures[0]["capture_id"] == quick_json["capture_id"]
    assert {item["space_id"] for item in space_rows} == {
        item["space_id"] for item in control_space_rows
    }
    assert [item["capture_id"] for item in inbox_captures] == [
        item["capture_id"] for item in control_inbox_rows
    ]
    assert proposals_json["proposals"] == []
    assert search_results[0]["capture_id"] == canonical_json["capture_id"]
    assert "Browser canonical page token" in cast(str, page["body"])
    assert status_json == doctor_json == control_status
    assert http_json["access"] == "loopback"
    assert ownership_json["daemon_authority"] == "held"
    assert schema_json["state"] == "current"
    assert status_json["last_successful_run"] is not None
    assert runs_json["status"] == "ok"
    assert len(run_rows) <= 5
    assert str(root) not in json.dumps(
        {"status": status_json, "runs": runs_json},
        sort_keys=True,
    )


def test_private_bind_requires_explicit_external_encryption_termination() -> None:
    with pytest.raises(ServiceConfigurationError, match="invalid HTTP service configuration"):
        appliance_http_configuration_from_environment(
            {
                "OPEN_BRAIN_UI_BIND": _PRIVATE_BIND_HOST,
                "OPEN_BRAIN_UI_PORT": "8788",
                "OPEN_BRAIN_UI_ALLOW_PRIVATE": "true",
            }
        )
    with pytest.raises(ServiceConfigurationError, match="invalid HTTP service configuration"):
        appliance_http_configuration_from_environment(
            {
                "OPEN_BRAIN_UI_BIND": _PRIVATE_BIND_HOST,
                "OPEN_BRAIN_UI_PORT": "8788",
                "OPEN_BRAIN_UI_ALLOW_PRIVATE": "true",
                "OPEN_BRAIN_UI_EXTERNAL_TLS_TERMINATION": "true",
            }
        )

    configuration = appliance_http_configuration_from_environment(
        {
            "OPEN_BRAIN_UI_BIND": _PRIVATE_BIND_HOST,
            "OPEN_BRAIN_UI_PORT": "8788",
            "OPEN_BRAIN_UI_ALLOW_PRIVATE": "true",
            "OPEN_BRAIN_UI_EXTERNAL_TLS_TERMINATION": "true",
            "OPEN_BRAIN_UI_EXTERNAL_ORIGIN": "https://brain.example.test",
        }
    )
    loopback = appliance_http_configuration_from_environment(
        {
            "OPEN_BRAIN_UI_BIND": "127.0.0.1",
            "OPEN_BRAIN_UI_PORT": "8788",
            "OPEN_BRAIN_UI_ALLOW_PRIVATE": "false",
        }
    )

    assert configuration.bind.allow_private_network is True
    assert configuration.external_encryption_terminated is True

    with pytest.raises(ServiceConfigurationError, match="invalid HTTP service configuration"):
        appliance_http_configuration_from_environment(
            {
                "OPEN_BRAIN_UI_BIND": "127.0.0.1",
                "OPEN_BRAIN_UI_PORT": "8788",
                "OPEN_BRAIN_UI_ALLOW_PRIVATE": "false",
                "OPEN_BRAIN_UI_EXTERNAL_TLS_TERMINATION": "true",
            }
        )
    with pytest.raises(ValueError, match="invalid HTTP service configuration"):
        ApplianceHttpConfiguration(
            bind=UiBindConfig(
                host=_PRIVATE_BIND_HOST,
                port=8788,
                allow_private_network=True,
            ),
            allowed_origin=f"http://{_PRIVATE_BIND_HOST}:8788",
            external_encryption_terminated=True,
        )
    with pytest.raises(ServiceConfigurationError, match="invalid HTTP service configuration"):
        appliance_http_configuration_from_environment(
            {
                "OPEN_BRAIN_UI_BIND": _PRIVATE_BIND_HOST,
                "OPEN_BRAIN_UI_PORT": "8788",
                "OPEN_BRAIN_UI_ALLOW_PRIVATE": "true",
                "OPEN_BRAIN_UI_EXTERNAL_TLS_TERMINATION": "true",
                "OPEN_BRAIN_UI_EXTERNAL_ORIGIN": "https://brain.example.test:not-a-port",
            }
        )
    assert configuration.allowed_origin == "https://brain.example.test"
    assert loopback.allowed_origin == "http://127.0.0.1:8788"


def _free_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _request(
    method: str,
    port: int,
    path: str,
    *,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = (
        b""
        if body is None
        else json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    )
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Length"] = str(len(payload))
        request_headers["Content-Type"] = "application/json"
    connection.request(
        method,
        path,
        body=payload if body is not None else None,
        headers=request_headers,
    )
    response = connection.getresponse()
    try:
        raw = response.read()
    finally:
        connection.close()
    content_type = response.getheader("Content-Type", "")
    return {
        "body": raw.decode("utf-8"),
        "headers": dict(response.getheaders()),
        "json": json.loads(raw) if content_type.startswith("application/json") else None,
        "status": response.status,
    }


def _wait_for(path: Path, *, predicate: Callable[[Path], bool]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate(path):
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _records(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [_mapping(item) for item in value]
