from __future__ import annotations

import inspect
import json
import socket
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from open_brain_engine.engine import (
    DaemonAuthorityError,
    DaemonAuthorityRootMismatchError,
    DaemonAuthorityStaleError,
    LocalEngineContext,
    ProposalDraft,
    TextPayload,
    acquire_daemon_authority,
    open_local_engine,
)
from open_brain_engine.engine.authority import DaemonAuthorityCapability
from open_brain_engine.storage.locks import LockBusyError

from open_brain.cli._common import ExitCode
from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.appliance_application import ApplianceApplication
from open_brain.services.appliance_daemon import (
    MAXIMUM_CONTROL_ENVELOPE_BYTES,
    ApplianceControlSocketError,
    ApplianceControlUnavailableError,
    ApplianceDaemon,
    ApplianceDaemonConflictError,
    CliControlReceipt,
    CliControlRequest,
    ControlRequest,
    acquire_control_socket_authority,
    cleanup_stale_control_socket,
    main,
    request_cli_dispatch,
)
from open_brain.services.appliance_init import initialize_appliance
from open_brain.services.appliance_lifecycle import submit_control_request
from open_brain.services.appliance_scheduler import (
    APPLIANCE_SCHEDULER_DIRECTORY,
    ApplianceScheduler,
)
from open_brain.services.http_server import HttpServerFactory, HttpServerProtocol
from open_brain.services.runtime import appliance_http_configuration_from_environment

_ORIGINAL_SOCKET_BIND = socket.socket.bind
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SOCKET_LISTEN = socket.socket.listen


def _existing_profile(root: Path) -> LocalEngineContext:
    compile_single_user_local(root)
    return open_existing_single_user_local(root)


def _capture_rows(root: Path, delivery_id: str) -> tuple[tuple[str, str], ...]:
    connection = sqlite3.connect(root / ".open-brain" / "state" / "phase1.sqlite3")
    try:
        return tuple(
            connection.execute(
                "SELECT capture_id, delivery_id FROM captures WHERE delivery_id = ?",
                (delivery_id,),
            )
        )
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def allow_unix_domain_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def bind(
        self: socket.socket, address: str | bytes | tuple[Any, ...]
    ) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_BIND(self, address)
        raise AssertionError("network access is forbidden in the Phase 2 test suite")

    def connect(
        self: socket.socket, address: str | bytes | tuple[Any, ...]
    ) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT(self, address)
        raise AssertionError("network access is forbidden in the Phase 2 test suite")

    def connect_ex(
        self: socket.socket, address: str | bytes | tuple[Any, ...]
    ) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT_EX(self, address)
        raise AssertionError("network access is forbidden in the Phase 2 test suite")

    def listen(self: socket.socket, backlog: int = 0) -> object:
        if self.family == socket.AF_UNIX:
            assert backlog >= 2
            return _ORIGINAL_SOCKET_LISTEN(self, backlog)
        raise AssertionError("network access is forbidden in the Phase 2 test suite")

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "listen", listen)


@pytest.fixture
def short_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="ob-", dir=Path("/tmp").resolve()) as directory:
        yield Path(directory) / "brain"


def test_daemon_authority_capability_is_issuer_created() -> None:
    with pytest.raises(TypeError, match="issuer-created"):
        DaemonAuthorityCapability()


def test_appliance_application_constructor_cannot_accept_mutating_tasks() -> None:
    assert "mutations" not in inspect.signature(ApplianceApplication).parameters


def test_appliance_mutating_composition_rejects_missing_stale_and_wrong_root_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    other_root = tmp_path / "other-brain"
    profile = _existing_profile(root)
    _existing_profile(other_root)

    with pytest.raises(DaemonAuthorityError, match="missing"):
        ApplianceApplication.open_mutating(root)

    with acquire_daemon_authority(profile) as authority, pytest.raises(
        DaemonAuthorityRootMismatchError,
        match="root mismatch",
    ):
        ApplianceApplication.open_mutating(other_root, authority=authority)

    with pytest.raises(DaemonAuthorityStaleError, match="stale"):
        ApplianceApplication.open_mutating(root, authority=authority)


def test_daemon_authority_is_process_exclusive_and_enables_shared_writer_mutations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    profile = _existing_profile(root)

    with acquire_daemon_authority(profile) as authority:
        with pytest.raises(
            LockBusyError,
            match="lease already held by this process",
        ), acquire_daemon_authority(profile):
            pass

        application = ApplianceApplication.open_mutating(root, authority=authority)
        assert application.mutations is not None

        receipt = application.mutations.capture.accept(
            TextPayload("Synthetic daemon-authorized capture"),
            delivery_id="delivery.appliance.daemon-authority",
        )

        assert receipt.state == "inbox"
        result = application.retrieval.search("daemon-authorized capture")[0]
        assert result.capture_id == receipt.capture_id

    with pytest.raises(DaemonAuthorityStaleError, match="stale"):
        assert application.mutations is not None
        application.mutations.capture.accept(
            TextPayload("Synthetic stale authority capture"),
            delivery_id="delivery.appliance.daemon-authority.stale",
        )


def test_daemon_holds_authority_creates_owner_only_socket_and_rejects_second_daemon(
    short_root: Path,
) -> None:
    root = short_root
    profile = _existing_profile(root)

    with ApplianceDaemon(root) as daemon:
        run_directory = root / ".open-brain" / "run"
        socket_path = run_directory / "control.sock"

        assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700
        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
        assert stat.S_IMODE(socket_path.lstat().st_mode) == 0o600

        with pytest.raises(LockBusyError, match="lease already held"), acquire_daemon_authority(
            profile
        ):
            pass

        with pytest.raises(
            ApplianceDaemonConflictError, match="already active"
        ), ApplianceDaemon(root):
            pass

        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        daemon.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()

    with acquire_daemon_authority(profile):
        pass


def test_daemon_owns_exactly_one_http_listener_and_closes_it_before_releasing_authority(
    short_root: Path,
) -> None:
    root = short_root
    initialize_appliance(root)
    profile = open_existing_single_user_local(root)
    configuration = appliance_http_configuration_from_environment(
        {
            "OPEN_BRAIN_UI_BIND": "127.0.0.1",
            "OPEN_BRAIN_UI_PORT": "8788",
            "OPEN_BRAIN_UI_ALLOW_PRIVATE": "false",
        }
    )

    class FakeHttpServer:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.started = threading.Event()
            self.released = threading.Event()

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            assert poll_interval == 0.5
            self.events.append("serve")
            self.started.set()
            self.released.wait(timeout=5)

        def shutdown(self) -> None:
            self.events.append("shutdown")
            self.released.set()

        def server_close(self) -> None:
            self.events.append("close")

    fake_server = FakeHttpServer()
    daemon = ApplianceDaemon(root)
    assert not hasattr(daemon, "http_lifecycle")

    with pytest.raises(RuntimeError, match="not running"):
        daemon.start_http_listener(
            configuration,
            server_factory=lambda _address, _handler: fake_server,
        )

    daemon.start()
    try:
        daemon.start_http_listener(
            configuration,
            server_factory=lambda _address, _handler: fake_server,
        )
        assert fake_server.started.wait(timeout=5)
        with pytest.raises(RuntimeError, match="already started"):
            daemon.start_http_listener(
                configuration,
                server_factory=lambda _address, _handler: fake_server,
            )
        stop_thread = threading.Thread(target=daemon.stop)
        stop_thread.start()
        stop_thread.join(timeout=5)
        assert not stop_thread.is_alive()
        assert fake_server.events == ["serve", "shutdown", "close"]
        with acquire_daemon_authority(profile):
            pass
    finally:
        fake_server.released.set()
        daemon.stop()


def test_daemon_main_composes_default_http_configuration_with_injected_server(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = short_root
    initialize_appliance(root)
    observed: list[tuple[str, int]] = []

    class FakeHttpServer:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.started = threading.Event()
            self.released = threading.Event()

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            assert poll_interval == 0.5
            self.events.append("serve")
            self.started.set()
            self.released.wait(timeout=5)

        def shutdown(self) -> None:
            self.events.append("shutdown")
            self.released.set()

        def server_close(self) -> None:
            self.events.append("close")

    fake_server = FakeHttpServer()

    def factory(
        address: tuple[str, int], _handler: type[BaseHTTPRequestHandler]
    ) -> HttpServerProtocol:
        observed.append(address)
        return fake_server

    def interrupt_after_http(_self: ApplianceDaemon) -> None:
        assert fake_server.started.wait(timeout=5)
        raise KeyboardInterrupt

    monkeypatch.setattr(ApplianceDaemon, "serve_until_stopped", interrupt_after_http)

    exit_code = main(
        ["--root", str(root)],
        environment={
            "OPEN_BRAIN_UI_BIND": "127.0.0.1",
            "OPEN_BRAIN_UI_PORT": "8788",
            "OPEN_BRAIN_UI_ALLOW_PRIVATE": "false",
        },
        http_server_factory=cast(HttpServerFactory, factory),
    )

    assert exit_code == 0
    assert observed == [("127.0.0.1", 8788)]
    assert fake_server.events == ["serve", "shutdown", "close"]


def test_daemon_main_can_disable_http_listener_for_unit_callers(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = short_root
    initialize_appliance(root)
    called = {"count": 0}

    def factory(
        address: tuple[str, int], _handler: type[BaseHTTPRequestHandler]
    ) -> HttpServerProtocol:
        called["count"] += 1
        raise AssertionError(address)

    monkeypatch.setattr(
        ApplianceDaemon,
        "serve_until_stopped",
        lambda _self: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    exit_code = main(
        ["--root", str(root)],
        environment={
            "OPEN_BRAIN_UI_BIND": "127.0.0.1",
            "OPEN_BRAIN_UI_PORT": "8788",
            "OPEN_BRAIN_UI_ALLOW_PRIVATE": "false",
        },
        http_server_factory=cast(HttpServerFactory, factory),
        enable_http_listener=False,
    )

    assert exit_code == 0
    assert called["count"] == 0


def test_daemon_main_closes_authority_without_traceback_when_http_start_fails(
    short_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = short_root
    initialize_appliance(root)
    profile = open_existing_single_user_local(root)

    def fail_listener(
        _address: tuple[str, int],
        _handler: type[BaseHTTPRequestHandler],
    ) -> HttpServerProtocol:
        raise OSError("synthetic bind failure /private/runtime/canary")

    assert (
        main(
            ["--root", str(root)],
            environment={
                "OPEN_BRAIN_UI_BIND": "127.0.0.1",
                "OPEN_BRAIN_UI_PORT": "8788",
                "OPEN_BRAIN_UI_ALLOW_PRIVATE": "false",
            },
            http_server_factory=cast(HttpServerFactory, fail_listener),
        )
        == ExitCode.CONFIGURATION
    )
    assert capsys.readouterr() == ("", "")
    with acquire_daemon_authority(profile):
        pass


def test_daemon_refuses_symlink_and_non_socket_replacements(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    _existing_profile(root)
    run_directory = root / ".open-brain" / "run"
    run_directory.mkdir(exist_ok=True)
    socket_path = run_directory / "control.sock"
    target = tmp_path / "target.sock"
    target.write_text("target", encoding="utf-8")
    socket_path.symlink_to(target)

    with pytest.raises(ApplianceControlSocketError, match="symlink"), ApplianceDaemon(root):
        pass

    socket_path.unlink()
    socket_path.write_text("not a socket", encoding="utf-8")

    with pytest.raises(
        ApplianceControlSocketError, match="non-socket"
    ), ApplianceDaemon(root):
        pass


def test_stale_socket_cleanup_requires_authority_and_detects_replacement_race(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = short_root
    profile = _existing_profile(root)
    run_directory = root / ".open-brain" / "run"
    run_directory.mkdir(exist_ok=True)
    socket_path = run_directory / "control.sock"

    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()

    with pytest.raises(DaemonAuthorityError, match="missing"):
        cleanup_stale_control_socket(profile, None)
    assert socket_path.exists()

    with acquire_control_socket_authority(profile) as authority:
        cleanup_stale_control_socket(profile, authority)
    assert not socket_path.exists()

    replaced = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replaced.bind(str(socket_path))
    replaced.close()

    original = socket_path.lstat()
    replacement = root / ".open-brain" / "run" / "replacement.sock"
    replacement_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement_socket.bind(str(replacement))
    replacement_socket.close()
    replacement_stat = replacement.lstat()
    calls = {"count": 0}

    def swapped(path: Path) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            return original
        return replacement_stat

    monkeypatch.setattr("open_brain.services.appliance_daemon._lstat_path", swapped)

    with acquire_control_socket_authority(profile) as authority, pytest.raises(
        ApplianceControlSocketError,
        match="replaced during cleanup",
    ):
        cleanup_stale_control_socket(profile, authority)


def test_stale_socket_cleanup_authority_is_root_bound(tmp_path: Path) -> None:
    first = _existing_profile(tmp_path / "first-brain")
    second = _existing_profile(tmp_path / "second-brain")

    with acquire_control_socket_authority(first) as authority, pytest.raises(
        DaemonAuthorityRootMismatchError,
        match="root mismatch",
    ):
        cleanup_stale_control_socket(second, authority)


def test_control_request_routes_only_through_authority_backed_application(
    short_root: Path,
) -> None:
    root = short_root
    _existing_profile(root)
    seen: list[object] = []

    def application_factory(path: Path, authority: object) -> ApplianceApplication:
        seen.append(authority)
        return ApplianceApplication.open_mutating(path, authority=authority)

    with ApplianceDaemon(root, application_factory=application_factory) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        receipt = submit_control_request(
            root,
            ControlRequest(
                delivery_id="delivery.appliance.control.capture",
                text="Synthetic daemon control capture",
            ),
        )
        daemon.stop()
        thread.join(timeout=5)

    assert seen
    assert receipt.state == "inbox"
    assert receipt.capture_id.startswith("capture_")
    assert _capture_rows(root, "delivery.appliance.control.capture") == (
        (receipt.capture_id, "delivery.appliance.control.capture"),
    )


def test_control_client_fails_closed_without_direct_mutation_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    _existing_profile(root)
    called = {"count": 0}

    def forbidden(*args: object, **kwargs: object) -> object:
        called["count"] += 1
        raise AssertionError("direct mutation fallback is forbidden")

    monkeypatch.setattr(
        "open_brain.services.appliance_application.ApplianceApplication.open_mutating",
        forbidden,
    )

    with pytest.raises(ApplianceControlUnavailableError, match="unavailable"):
        submit_control_request(
            root,
            ControlRequest(
                delivery_id="delivery.appliance.control.unavailable",
                text="Synthetic unavailable control capture",
            ),
        )

    assert called["count"] == 0


def test_restart_preserves_accepted_capture_identity_without_duplicate_state(
    short_root: Path,
) -> None:
    root = short_root
    _existing_profile(root)
    request = ControlRequest(
        delivery_id="delivery.appliance.control.restart",
        text="Synthetic restart-safe control capture",
    )

    with ApplianceDaemon(root) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        first = submit_control_request(root, request)
        daemon.stop()
        thread.join(timeout=5)

    with ApplianceDaemon(root) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        second = submit_control_request(root, request)
        daemon.stop()
        thread.join(timeout=5)

    assert first.capture_id == second.capture_id
    assert first.state == second.state == "inbox"

    assert _capture_rows(root, "delivery.appliance.control.restart") == (
        (first.capture_id, "delivery.appliance.control.restart"),
    )


def test_daemon_owns_the_internal_scheduler_inventory_and_persists_run_evidence(
    short_root: Path,
) -> None:
    root = short_root
    _existing_profile(root)
    state_path = root / APPLIANCE_SCHEDULER_DIRECTORY / "state.json"

    with ApplianceDaemon(root) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not state_path.exists():
            time.sleep(0.05)
        daemon.stop()
        thread.join(timeout=5)

    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert sorted(state["jobs"]) == [
        "backup-create",
        "engine-recover",
        "markdown-reconcile",
        "portable-export",
        "portable-import",
    ]
    recover_runs = root / APPLIANCE_SCHEDULER_DIRECTORY / "runs" / "engine-recover"
    reconcile_runs = root / APPLIANCE_SCHEDULER_DIRECTORY / "runs" / "markdown-reconcile"
    assert len(tuple(recover_runs.glob("*.json"))) == 1
    assert len(tuple(reconcile_runs.glob("*.json"))) == 1


def test_stalled_client_does_not_hold_the_daemon_control_loop(short_root: Path) -> None:
    root = short_root
    _existing_profile(root)

    with ApplianceDaemon(root, connection_timeout=0.05) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stalled.connect(str(daemon.socket_path))
        stalled.sendall(b"{")

        receipt = submit_control_request(
            root,
            ControlRequest(
                delivery_id="delivery.appliance.control.after-stall",
                text="Synthetic capture after a stalled client",
            ),
        )
        stalled.close()
        daemon.stop()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert receipt.state == "inbox"


def test_restart_replays_commit_when_receipt_was_not_delivered(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = short_root
    _existing_profile(root)
    request = ControlRequest(
        delivery_id="delivery.appliance.control.lost-receipt",
        text="Synthetic accepted capture with a lost receipt",
    )

    with ApplianceDaemon(root) as daemon:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(daemon.socket_path))
        client.sendall(request.to_bytes())
        client.shutdown(socket.SHUT_WR)

        with monkeypatch.context() as patch:
            def lose_receipt(connection: socket.socket, payload: bytes) -> None:
                del connection, payload
                raise BrokenPipeError

            patch.setattr(
                "open_brain.services.appliance_daemon._send_control_bytes",
                lose_receipt,
            )
            with pytest.raises(BrokenPipeError):
                daemon.serve_once()
        client.close()

    accepted_rows = _capture_rows(root, request.delivery_id)
    assert len(accepted_rows) == 1

    with ApplianceDaemon(root) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        replay = submit_control_request(root, request)
        daemon.stop()
        thread.join(timeout=5)

    assert replay.capture_id == accepted_rows[0][0]
    assert _capture_rows(root, request.delivery_id) == accepted_rows


def test_daemon_stop_waits_for_an_inflight_cli_mutation_before_releasing_authority(
    short_root: Path,
) -> None:
    root = short_root
    profile = _existing_profile(root)
    started = threading.Event()
    release = threading.Event()
    receipt = {
        "command": "spaces",
        "spaces": [],
        "status": "listed",
    }

    class BlockingAdapter:
        def dispatch(self, argv: tuple[str, ...]) -> object:
            assert argv == ("list",)
            started.set()
            assert release.wait(timeout=5)
            return SimpleNamespace(envelope=receipt, exit_code=0)

    class BlockingApplication:
        mutations = None

        def cli_adapter(self, command: str) -> BlockingAdapter | None:
            return BlockingAdapter() if command == "spaces" else None

    class NoOpScheduler:
        def run_due(self, *, now: datetime) -> tuple[object, ...]:
            del now
            return ()

    daemon = ApplianceDaemon(
        root,
        application_factory=lambda _root, _authority: cast(
            ApplianceApplication,
            BlockingApplication(),
        ),
        scheduler_factory=lambda _root, _profile, _authority: cast(
            ApplianceScheduler,
            NoOpScheduler(),
        ),
    )
    daemon.start()
    serve_thread = threading.Thread(target=daemon.serve_once, kwargs={"timeout": 5})
    serve_thread.start()

    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            result["receipt"] = request_cli_dispatch(
                root,
                CliControlRequest(command="spaces", argv=("list",)),
            )
        except BaseException as error:  # pragma: no cover - test cleanup path
            errors.append(error)

    client_thread = threading.Thread(target=invoke)
    client_thread.start()
    stop_thread = threading.Thread(target=daemon.stop)
    try:
        assert started.wait(timeout=5)

        stop_thread.start()
        time.sleep(0.1)
        assert stop_thread.is_alive()
        with pytest.raises(LockBusyError, match="lease already held"), acquire_daemon_authority(
            profile
        ):
            pass
    finally:
        release.set()
        stop_thread.join(timeout=5)
        serve_thread.join(timeout=5)
        client_thread.join(timeout=5)
        daemon.stop()

    assert not errors
    assert not stop_thread.is_alive()
    assert not serve_thread.is_alive()
    assert not client_thread.is_alive()
    receipt_value = result["receipt"]
    assert isinstance(receipt_value, CliControlReceipt)
    assert receipt_value.envelope == receipt

    with acquire_daemon_authority(profile):
        pass


def test_daemon_closes_operation_admission_before_releasing_authority(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = short_root
    profile = _existing_profile(root)
    daemon = ApplianceDaemon(root)
    daemon.start()
    waiting = threading.Event()
    release = threading.Event()
    original_wait = daemon._wait_for_operations

    def blocked_wait() -> None:
        waiting.set()
        assert release.wait(timeout=5)
        original_wait()

    monkeypatch.setattr(daemon, "_wait_for_operations", blocked_wait)
    stop_thread = threading.Thread(target=daemon.stop)
    stop_thread.start()
    try:
        assert waiting.wait(timeout=5)
        with pytest.raises(RuntimeError, match="stopping"), daemon._operation():
            pass
        with pytest.raises(LockBusyError, match="lease already held"), acquire_daemon_authority(
            profile
        ):
            pass
    finally:
        release.set()
        stop_thread.join(timeout=5)

    assert not stop_thread.is_alive()
    with acquire_daemon_authority(profile):
        pass


def test_oversized_cli_result_returns_bounded_failure_without_stopping_daemon(
    short_root: Path,
) -> None:
    root = short_root
    _existing_profile(root)

    class OversizedAdapter:
        def dispatch(self, argv: tuple[str, ...]) -> object:
            assert argv == ("synthetic",)
            return SimpleNamespace(
                envelope={
                    "command": "query",
                    "results": [{"excerpt": "x" * 5_000}],
                    "status": "ok",
                },
                exit_code=0,
            )

    class QueryApplication:
        mutations = None

        def cli_adapter(self, command: str) -> OversizedAdapter | None:
            return OversizedAdapter() if command == "query" else None

    class NoOpScheduler:
        def run_due(self, *, now: datetime) -> tuple[object, ...]:
            del now
            return ()

    with ApplianceDaemon(
        root,
        application_factory=lambda _root, _authority: cast(
            ApplianceApplication,
            QueryApplication(),
        ),
        scheduler_factory=lambda _root, _profile, _authority: cast(
            ApplianceScheduler,
            NoOpScheduler(),
        ),
    ) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        receipt = request_cli_dispatch(
            root,
            CliControlRequest(command="query", argv=("synthetic",)),
        )
        daemon.stop()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert receipt.exit_code == 1
    assert receipt.envelope["command"] == "query"
    assert receipt.envelope["status"] == "failed"
    assert len(receipt.to_bytes()) <= MAXIMUM_CONTROL_ENVELOPE_BYTES


def test_restart_replays_review_publication_when_cli_receipt_was_lost(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = short_root
    profile = _existing_profile(root)
    engine = open_local_engine(profile)
    space = engine.inbox.create_space("Review", delivery_id="delivery.appliance.review.space")
    capture = engine.capture.accept(
        TextPayload("Synthetic daemon review publication"),
        delivery_id="delivery.appliance.review.capture",
        space_id=space.space_id,
    )
    proposal = engine.review.propose(
        capture.capture_id,
        (ProposalDraft("Review", "Synthetic daemon publication"),),
        delivery_id="delivery.appliance.review.proposal",
    )[0]
    request = CliControlRequest(
        command="review",
        argv=(
            "approve",
            proposal.proposal_id,
            "--delivery=delivery.appliance.review.receipt",
        ),
    )

    with ApplianceDaemon(root) as daemon:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(daemon.socket_path))
        client.sendall(request.to_bytes())
        client.shutdown(socket.SHUT_WR)

        with monkeypatch.context() as patch:
            def lose_receipt(connection: socket.socket, payload: bytes) -> None:
                del connection, payload
                raise BrokenPipeError

            patch.setattr(
                "open_brain.services.appliance_daemon._send_control_bytes",
                lose_receipt,
            )
            with pytest.raises(BrokenPipeError):
                daemon.serve_once()
        client.close()

    assert len(tuple((root / "history" / "decisions").rglob("*.json"))) == 1
    assert len(tuple((root / "history" / "publications").rglob("*.json"))) == 1
    assert len(tuple((root / "content" / "spaces").rglob("page_*.md"))) == 1

    with ApplianceDaemon(root) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        first = request_cli_dispatch(root, request)
        daemon.stop()
        thread.join(timeout=5)

    with ApplianceDaemon(root) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        second = request_cli_dispatch(root, request)
        daemon.stop()
        thread.join(timeout=5)

    for envelope in (first.envelope, second.envelope):
        assert envelope["command"] == "review"
        assert envelope["status"] == "decided"
        assert envelope["state"] == "approved"
    assert first.envelope["decision_id"] == second.envelope["decision_id"]
    assert first.envelope["publication_id"] == second.envelope["publication_id"]
    assert first.envelope["page_id"] == second.envelope["page_id"]
    assert len(tuple((root / "history" / "decisions").rglob("*.json"))) == 1
    assert len(tuple((root / "history" / "publications").rglob("*.json"))) == 1
    assert len(tuple((root / "content" / "spaces").rglob("page_*.md"))) == 1
