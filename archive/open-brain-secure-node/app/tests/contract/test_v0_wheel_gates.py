from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import ProposalDraft, TextPayload, open_local_engine

from open_brain.profile import open_existing_single_user_local
from open_brain.services.appliance_auth import derive_appliance_credential
from open_brain.services.appliance_daemon import ApplianceDaemon
from open_brain.services.appliance_entrypoints import run_cli
from open_brain.services.appliance_init import APPLIANCE_OWNER_CREDENTIAL, initialize_appliance
from open_brain.services.runtime import appliance_http_configuration_from_environment

_ORIGINAL_SOCKET_BIND = socket.socket.bind
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SOCKET_LISTEN = socket.socket.listen


@pytest.fixture(autouse=True)
def allow_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def bind(self: socket.socket, address: str | bytes | tuple[object, ...]) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_BIND(self, address)
        if (
            self.family == socket.AF_INET
            and isinstance(address, tuple)
            and address[0] == "127.0.0.1"
        ):
            return _ORIGINAL_SOCKET_BIND(self, address)
        raise AssertionError("non-loopback network access is forbidden in P4-W2 contract tests")

    def connect(self: socket.socket, address: str | bytes | tuple[object, ...]) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT(self, address)
        if (
            self.family == socket.AF_INET
            and isinstance(address, tuple)
            and address[0] == "127.0.0.1"
        ):
            return _ORIGINAL_SOCKET_CONNECT(self, address)
        raise AssertionError("non-loopback network access is forbidden in P4-W2 contract tests")

    def connect_ex(self: socket.socket, address: str | bytes | tuple[object, ...]) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT_EX(self, address)
        if (
            self.family == socket.AF_INET
            and isinstance(address, tuple)
            and address[0] == "127.0.0.1"
        ):
            return _ORIGINAL_SOCKET_CONNECT_EX(self, address)
        raise AssertionError("non-loopback network access is forbidden in P4-W2 contract tests")

    def listen(self: socket.socket, backlog: int = 0) -> object:
        if self.family in {socket.AF_UNIX, socket.AF_INET}:
            return _ORIGINAL_SOCKET_LISTEN(self, backlog)
        raise AssertionError("unsupported listener family")

    def getaddrinfo(
        host: str,
        port: int,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del family, type, flags
        if host not in {"127.0.0.1", "localhost"}:
            raise AssertionError(
                "non-loopback network access is forbidden in P4-W2 contract tests"
            )
        return [(socket.AF_INET, socket.SOCK_STREAM, proto or 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "listen", listen)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "getfqdn", lambda name="": "localhost")


@pytest.fixture
def short_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="ob-p4w2-", dir="/tmp") as directory:
        yield Path(directory).resolve() / "brain"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])


def _request(
    method: str,
    port: int,
    path: str,
    *,
    body: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = None if body is None else json.dumps(dict(body), separators=(",", ":"))
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    response_body = response.read()
    result = {
        "body": response_body.decode("utf-8"),
        "headers": dict(response.getheaders()),
        "status": response.status,
    }
    connection.close()
    if response_body and response.getheader("Content-Type", "").startswith("application/json"):
        result["json"] = json.loads(response_body)
    return result


@contextmanager
def _running_surfaces(root: Path) -> Iterator[tuple[int, dict[str, str]]]:
    seed = (root / APPLIANCE_OWNER_CREDENTIAL).read_text(encoding="utf-8").strip()
    browser_credential = derive_appliance_credential(seed, purpose="browser-bootstrap")
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
        daemon.start_http_listener(configuration)
        login = _request(
            "POST",
            port,
            "/auth/login",
            body={"credential": browser_credential},
            headers={"Origin": configuration.allowed_origin},
        )
        assert login["status"] == 200
        login_headers = cast(dict[str, str], login["headers"])
        login_payload = cast(dict[str, object], login["json"])
        mutation_headers = {
            "Cookie": login_headers["Set-Cookie"].split(";", 1)[0],
            "Origin": configuration.allowed_origin,
            "X-CSRF-Token": cast(str, login_payload["csrf_token"]),
        }
        try:
            yield port, mutation_headers
        finally:
            daemon.stop()
            control_thread.join(timeout=5)
            assert not control_thread.is_alive()


def _cli_json(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> dict[str, object]:
    exit_code = run_cli(
        (*arguments, "--json"),
        environment={"OPEN_BRAIN_ROOT": str(root)},
    )
    output = capsys.readouterr().out
    assert exit_code == 0, output
    value = json.loads(output)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _json(response: Mapping[str, object]) -> dict[str, object]:
    assert response["status"] == 200
    value = response["json"]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_v0_gate_07_wheel_cli_and_ui_decide_sibling_proposals_independently(
    short_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = short_root
    initialize_appliance(root, starter_spaces=("Reviews",))
    setup = open_local_engine(open_existing_single_user_local(root))
    space_id = setup.inbox.spaces()[0].space_id
    capture = setup.capture.accept(
        TextPayload("V0 gate 07 sibling source"),
        delivery_id="wheel.gate07.capture",
        space_id=space_id,
    )
    proposals = setup.review.propose(
        capture.capture_id,
        tuple(
            ProposalDraft(f"Sibling {index}", f"Original sibling meaning {index}")
            for index in range(6)
        ),
        delivery_id="wheel.gate07.proposals",
    )

    with _running_surfaces(root) as (port, ui_headers):
        cli_results = (
            _cli_json(
                root,
                capsys,
                "review",
                "approve",
                proposals[0].proposal_id,
                "--delivery=wheel.gate07.cli.approve",
            ),
            _cli_json(
                root,
                capsys,
                "review",
                "reject",
                proposals[1].proposal_id,
                "--delivery=wheel.gate07.cli.reject",
            ),
            _cli_json(
                root,
                capsys,
                "review",
                "edit",
                proposals[2].proposal_id,
                "CLI safely edited meaning",
                "--delivery=wheel.gate07.cli.edit",
            ),
        )
        ui_results = tuple(
            _json(
                _request(
                    "POST",
                    port,
                    f"/api/proposals/{proposal.proposal_id}/decision",
                    body={
                        "delivery_id": f"wheel.gate07.ui.{action}",
                        "outcome": outcome,
                        **(
                            {"edited_markdown": "UI safely edited meaning"}
                            if action == "edit"
                            else {}
                        ),
                    },
                    headers=ui_headers,
                )
            )
            for proposal, action, outcome in (
                (proposals[3], "approve", "approved"),
                (proposals[4], "reject", "rejected"),
                (proposals[5], "edit", "edited"),
            )
        )

    assert [result["state"] for result in cli_results] == ["approved", "rejected", "edited"]
    assert [result["state"] for result in ui_results] == ["approved", "rejected", "edited"]
    reopened = open_local_engine(open_existing_single_user_local(root))
    states = {proposal.proposal_id: proposal.status for proposal in reopened.review.list()}
    assert states == {
        proposals[0].proposal_id: "approved",
        proposals[1].proposal_id: "rejected",
        proposals[2].proposal_id: "edited",
        proposals[3].proposal_id: "approved",
        proposals[4].proposal_id: "rejected",
        proposals[5].proposal_id: "edited",
    }
    assert len(tuple((root / "history/decisions").rglob("*.json"))) == 6
    assert len(tuple((root / "history/publications").rglob("*.json"))) == 4
    assert reopened.retrieval.search("CLI safely edited meaning", record_type="canonical")
    assert reopened.retrieval.search("UI safely edited meaning", record_type="canonical")
    source = next((root / "sources/captures").rglob(f"{capture.capture_id}.json")).read_text(
        encoding="utf-8"
    )
    assert "V0 gate 07 sibling source" in source
    assert "safely edited meaning" not in source


def test_v0_gate_13_wheel_create_rename_later_route_and_retrieve_across_spaces(
    short_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = short_root
    initialize_appliance(root)

    with _running_surfaces(root) as (port, ui_headers):
        first_space = _cli_json(
            root,
            capsys,
            "spaces",
            "create",
            "First space",
            "--delivery=wheel.gate13.space.first",
        )
        first_space_id = cast(str, first_space["space_id"])
        second_space = _json(
            _request(
                "POST",
                port,
                "/api/spaces",
                body={"delivery_id": "wheel.gate13.space.second", "name": "Second space"},
                headers=ui_headers,
            )
        )
        second_space_id = cast(str, second_space["space_id"])
        renamed = _json(
            _request(
                "POST",
                port,
                f"/api/spaces/{first_space_id}/rename",
                body={"delivery_id": "wheel.gate13.space.rename", "name": "Renamed space"},
                headers=ui_headers,
            )
        )
        quick = _json(
            _request(
                "POST",
                port,
                "/api/captures/quick",
                body={
                    "delivery_id": "wheel.gate13.capture.unassigned",
                    "text": "wheelgate13 first routed token",
                },
                headers=ui_headers,
            )
        )
        quick_capture_id = cast(str, quick["capture_id"])
        routed = _cli_json(
            root,
            capsys,
            "spaces",
            "route",
            quick_capture_id,
            first_space_id,
            "--delivery=wheel.gate13.route.later",
        )
        canonical = _json(
            _request(
                "POST",
                port,
                "/api/captures/canonical",
                body={
                    "delivery_id": "wheel.gate13.capture.second",
                    "space_id": second_space_id,
                    "text": "wheelgate13 second canonical token",
                },
                headers=ui_headers,
            )
        )
        cli_scoped = _cli_json(
            root,
            capsys,
            "query",
            "wheelgate13",
            f"--space={first_space_id}",
        )
        cli_all = _cli_json(root, capsys, "query", "wheelgate13")
        ui_scoped = _json(
            _request(
                "GET",
                port,
                f"/api/search?q=wheelgate13&space={first_space_id}",
                headers={"Cookie": ui_headers["Cookie"]},
            )
        )
        ui_all = _json(
            _request(
                "GET",
                port,
                "/api/search?q=wheelgate13",
                headers={"Cookie": ui_headers["Cookie"]},
            )
        )
        spaces = _json(
            _request("GET", port, "/api/spaces", headers={"Cookie": ui_headers["Cookie"]})
        )

    canonical_capture_id = cast(str, canonical["capture_id"])
    assert renamed["space_id"] == first_space_id
    assert routed == {
        "capture_id": quick_capture_id,
        "command": "spaces",
        "space_id": first_space_id,
        "status": "routed",
    }
    expected_capture_ids = {quick_capture_id, canonical_capture_id}
    cli_scoped_results = cast(list[dict[str, object]], cli_scoped["results"])
    cli_all_results = cast(list[dict[str, object]], cli_all["results"])
    ui_scoped_results = cast(list[dict[str, object]], ui_scoped["results"])
    ui_all_results = cast(list[dict[str, object]], ui_all["results"])
    assert {result["capture_id"] for result in cli_scoped_results} == {quick_capture_id}
    assert {result["space_id"] for result in cli_scoped_results} == {first_space_id}
    assert {result["capture_id"] for result in ui_scoped_results} == {quick_capture_id}
    assert {result["space_id"] for result in ui_scoped_results} == {first_space_id}
    assert {result["capture_id"] for result in cli_all_results} == expected_capture_ids
    assert {result["capture_id"] for result in ui_all_results} == expected_capture_ids
    space_rows = cast(list[dict[str, object]], spaces["spaces"])
    assert {cast(str, row["space_id"]): row["name"] for row in space_rows} == {
        first_space_id: "Renamed space",
        second_space_id: "Second space",
    }
    source = json.loads(
        next((root / "sources/captures").rglob(f"{quick_capture_id}.json")).read_bytes()
    )
    assert source["capture_id"] == quick_capture_id
    assert source["space_id"] is None
