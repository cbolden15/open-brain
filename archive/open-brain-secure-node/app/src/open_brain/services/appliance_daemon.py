from __future__ import annotations

import argparse
import errno
import json
import os
import re
import socket
import stat
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from open_brain_engine.engine import (
    DaemonAuthorityCapability,
    LocalEngineContext,
    TextPayload,
    acquire_daemon_authority,
    canonical_json_bytes,
    recover_authoritative_local_engine,
    require_daemon_authority,
)

from open_brain.capture.http import ShareHttpHandler
from open_brain.cli._common import (
    ExitCode,
    adapter_failed_envelope,
    validate_adapter_envelope,
)
from open_brain.integrations.phase1_ui import BrowserSessionStore
from open_brain.profile import open_existing_single_user_local

from .appliance_application import ApplianceApplication
from .appliance_auth import ApplianceBrowserSessionStore, derive_appliance_credential
from .appliance_history import read_appliance_run_history
from .appliance_init import APPLIANCE_OWNER_CREDENTIAL
from .appliance_recovery import ApplianceRecoveryService
from .appliance_scheduler import (
    ApplianceJobResult,
    ApplianceScheduler,
    ApplianceSchedulerInterruptedError,
)
from .appliance_status import read_appliance_status
from .http_server import (
    HttpRouteMode,
    HttpServerFactory,
    HttpService,
    HttpServiceConfig,
    ManagedHttpServer,
    create_http_server,
)
from .runtime import (
    ApplianceHttpConfiguration,
    ServiceConfigurationError,
    appliance_http_configuration_from_environment,
    read_private_service_secret,
)

CONTROL_SCHEMA_VERSION: Final[int] = 1
CONTROL_ACTION_CAPTURE_TEXT: Final[str] = "capture.accept.text"
CONTROL_ACTION_CLI_DISPATCH: Final[str] = "cli.dispatch"
CONTROL_ACTION_RECOVERY_REQUEST: Final[str] = "recovery.request"
CONTROL_ACTION_STATUS_READ: Final[str] = "status.read"
CONTROL_STATUS_ACCEPTED: Final[str] = "accepted"
CONTROL_STATUS_COMPLETED: Final[str] = "completed"
MAXIMUM_CONTROL_ENVELOPE_BYTES: Final[int] = 4_096
RUN_DIRECTORY_MODE: Final[int] = 0o700
SOCKET_MODE: Final[int] = 0o600
CONTROL_CONNECTION_TIMEOUT_SECONDS: Final[float] = 1.0
CONTROL_SOCKET_BACKLOG: Final[int] = 16
_DELIVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_CONTROLLED_COMMANDS = frozenset({"capture", "inbox", "proposals", "query", "review", "spaces"})


class ApplianceControlProtocolError(RuntimeError):
    """The appliance control envelope is invalid or out of bounds."""


class ApplianceControlSocketError(RuntimeError):
    """The appliance control socket path is unsafe or unusable."""


class ApplianceControlUnavailableError(RuntimeError):
    """The appliance control socket is unavailable."""


class ApplianceDaemonConflictError(RuntimeError):
    """Another appliance daemon already owns the root authority."""


@dataclass(frozen=True, slots=True)
class ControlRequest:
    delivery_id: str
    text: str
    action: str = CONTROL_ACTION_CAPTURE_TEXT
    schema_version: int = CONTROL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_capture_action(self.action)
        if self.schema_version != CONTROL_SCHEMA_VERSION:
            raise ApplianceControlProtocolError("unsupported request envelope")
        _validate_delivery_id(self.delivery_id, envelope="request")
        if not isinstance(self.text, str) or not self.text:
            raise ApplianceControlProtocolError("invalid request envelope")
        _validate_control_size(self.to_dict(), envelope="request")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "delivery_id": self.delivery_id,
            "schema_version": self.schema_version,
            "text": self.text,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> ControlRequest:
        value = _parse_control_bytes(payload, envelope="request")
        expected = {"action", "delivery_id", "schema_version", "text"}
        if set(value) != expected:
            raise ApplianceControlProtocolError("invalid request envelope")
        return cls(
            action=_required_str(value, "action", envelope="request"),
            delivery_id=_required_str(value, "delivery_id", envelope="request"),
            schema_version=_required_int(value, "schema_version", envelope="request"),
            text=_required_str(value, "text", envelope="request"),
        )


@dataclass(frozen=True, slots=True)
class ControlReceipt:
    delivery_id: str
    capture_id: str
    state: str
    action: str = CONTROL_ACTION_CAPTURE_TEXT
    status: str = CONTROL_STATUS_ACCEPTED
    schema_version: int = CONTROL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_capture_action(self.action)
        if self.status != CONTROL_STATUS_ACCEPTED or self.schema_version != CONTROL_SCHEMA_VERSION:
            raise ApplianceControlProtocolError("unsupported receipt envelope")
        _validate_delivery_id(self.delivery_id, envelope="receipt")
        _validate_portable_identifier(self.capture_id, prefix="capture")
        if self.state != "inbox":
            raise ApplianceControlProtocolError("invalid receipt envelope")
        _validate_control_size(self.to_dict(), envelope="receipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "capture_id": self.capture_id,
            "delivery_id": self.delivery_id,
            "schema_version": self.schema_version,
            "state": self.state,
            "status": self.status,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> ControlReceipt:
        value = _parse_control_bytes(payload, envelope="receipt")
        expected = {
            "action",
            "capture_id",
            "delivery_id",
            "schema_version",
            "state",
            "status",
        }
        if set(value) != expected:
            raise ApplianceControlProtocolError("invalid receipt envelope")
        return cls(
            action=_required_str(value, "action", envelope="receipt"),
            capture_id=_required_str(value, "capture_id", envelope="receipt"),
            delivery_id=_required_str(value, "delivery_id", envelope="receipt"),
            schema_version=_required_int(value, "schema_version", envelope="receipt"),
            state=_required_str(value, "state", envelope="receipt"),
            status=_required_str(value, "status", envelope="receipt"),
        )


@dataclass(frozen=True, slots=True)
class CliControlRequest:
    command: str
    argv: tuple[str, ...]
    action: str = CONTROL_ACTION_CLI_DISPATCH
    schema_version: int = CONTROL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.action != CONTROL_ACTION_CLI_DISPATCH
            or self.schema_version != CONTROL_SCHEMA_VERSION
        ):
            raise ApplianceControlProtocolError("unsupported request envelope")
        _validate_control_command(self.command)
        _validate_argv(self.argv)
        _validate_control_size(self.to_dict(), envelope="request")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "argv": list(self.argv),
            "command": self.command,
            "schema_version": self.schema_version,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> CliControlRequest:
        value = _parse_control_bytes(payload, envelope="request")
        if set(value) != {"action", "argv", "command", "schema_version"}:
            raise ApplianceControlProtocolError("invalid request envelope")
        return cls(
            action=_required_str(value, "action", envelope="request"),
            argv=_required_argv(value, envelope="request"),
            command=_required_str(value, "command", envelope="request"),
            schema_version=_required_int(value, "schema_version", envelope="request"),
        )


@dataclass(frozen=True, slots=True)
class CliControlReceipt:
    command: str
    exit_code: int
    envelope: dict[str, object]
    action: str = CONTROL_ACTION_CLI_DISPATCH
    status: str = CONTROL_STATUS_COMPLETED
    schema_version: int = CONTROL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.action != CONTROL_ACTION_CLI_DISPATCH or self.status != CONTROL_STATUS_COMPLETED:
            raise ApplianceControlProtocolError("unsupported receipt envelope")
        if self.schema_version != CONTROL_SCHEMA_VERSION:
            raise ApplianceControlProtocolError("unsupported receipt envelope")
        _validate_control_command(self.command)
        if (
            not isinstance(self.exit_code, int)
            or isinstance(self.exit_code, bool)
            or self.exit_code not in {int(code) for code in ExitCode}
        ):
            raise ApplianceControlProtocolError("invalid receipt envelope")
        if type(self.envelope) is not dict:
            raise ApplianceControlProtocolError("invalid receipt envelope")
        _validate_control_size(self.to_dict(), envelope="receipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "command": self.command,
            "envelope": self.envelope,
            "exit_code": self.exit_code,
            "schema_version": self.schema_version,
            "status": self.status,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> CliControlReceipt:
        value = _parse_control_bytes(payload, envelope="receipt")
        if set(value) != {"action", "command", "envelope", "exit_code", "schema_version", "status"}:
            raise ApplianceControlProtocolError("invalid receipt envelope")
        envelope = value.get("envelope")
        if type(envelope) is not dict:
            raise ApplianceControlProtocolError("invalid receipt envelope")
        return cls(
            action=_required_str(value, "action", envelope="receipt"),
            command=_required_str(value, "command", envelope="receipt"),
            envelope=envelope,
            exit_code=_required_int(value, "exit_code", envelope="receipt"),
            schema_version=_required_int(value, "schema_version", envelope="receipt"),
            status=_required_str(value, "status", envelope="receipt"),
        )


@dataclass(frozen=True, slots=True)
class StatusControlRequest:
    action: str = CONTROL_ACTION_STATUS_READ
    schema_version: int = CONTROL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.action != CONTROL_ACTION_STATUS_READ
            or self.schema_version != CONTROL_SCHEMA_VERSION
        ):
            raise ApplianceControlProtocolError("unsupported request envelope")
        _validate_control_size(self.to_dict(), envelope="request")

    def to_dict(self) -> dict[str, object]:
        return {"action": self.action, "schema_version": self.schema_version}

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> StatusControlRequest:
        value = _parse_control_bytes(payload, envelope="request")
        if set(value) != {"action", "schema_version"}:
            raise ApplianceControlProtocolError("invalid request envelope")
        return cls(
            action=_required_str(value, "action", envelope="request"),
            schema_version=_required_int(value, "schema_version", envelope="request"),
        )


@dataclass(frozen=True, slots=True)
class StatusControlReceipt:
    envelope: dict[str, object]
    action: str = CONTROL_ACTION_STATUS_READ
    status: str = CONTROL_STATUS_COMPLETED
    schema_version: int = CONTROL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.action != CONTROL_ACTION_STATUS_READ or self.status != CONTROL_STATUS_COMPLETED:
            raise ApplianceControlProtocolError("unsupported receipt envelope")
        if self.schema_version != CONTROL_SCHEMA_VERSION or type(self.envelope) is not dict:
            raise ApplianceControlProtocolError("invalid receipt envelope")
        _validate_control_size(self.to_dict(), envelope="receipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "envelope": self.envelope,
            "schema_version": self.schema_version,
            "status": self.status,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> StatusControlReceipt:
        value = _parse_control_bytes(payload, envelope="receipt")
        if set(value) != {"action", "envelope", "schema_version", "status"}:
            raise ApplianceControlProtocolError("invalid receipt envelope")
        envelope = value.get("envelope")
        if type(envelope) is not dict:
            raise ApplianceControlProtocolError("invalid receipt envelope")
        return cls(
            action=_required_str(value, "action", envelope="receipt"),
            envelope=envelope,
            schema_version=_required_int(value, "schema_version", envelope="receipt"),
            status=_required_str(value, "status", envelope="receipt"),
        )


@dataclass(frozen=True, slots=True)
class RecoveryControlRequest:
    operation: str
    request_id: str
    destination: str
    source: str | None = None
    action: str = CONTROL_ACTION_RECOVERY_REQUEST
    schema_version: int = CONTROL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.action != CONTROL_ACTION_RECOVERY_REQUEST
            or self.schema_version != CONTROL_SCHEMA_VERSION
        ):
            raise ApplianceControlProtocolError("unsupported request envelope")
        _validate_recovery_operation(self.operation)
        _validate_recovery_request_id(self.operation, self.request_id)
        _validate_control_path(self.destination, envelope="request")
        if self.operation == "portable-import":
            if self.source is None:
                raise ApplianceControlProtocolError("invalid request envelope")
            _validate_control_path(self.source, envelope="request")
        elif self.source is not None:
            raise ApplianceControlProtocolError("invalid request envelope")
        _validate_control_size(self.to_dict(), envelope="request")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "destination": self.destination,
            "operation": self.operation,
            "request_id": self.request_id,
            "schema_version": self.schema_version,
            "source": self.source,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> RecoveryControlRequest:
        value = _parse_control_bytes(payload, envelope="request")
        if set(value) != {
            "action",
            "destination",
            "operation",
            "request_id",
            "schema_version",
            "source",
        }:
            raise ApplianceControlProtocolError("invalid request envelope")
        source = value.get("source")
        if source is not None and not isinstance(source, str):
            raise ApplianceControlProtocolError("invalid request envelope")
        return cls(
            action=_required_str(value, "action", envelope="request"),
            destination=_required_str(value, "destination", envelope="request"),
            operation=_required_str(value, "operation", envelope="request"),
            request_id=_required_str(value, "request_id", envelope="request"),
            schema_version=_required_int(value, "schema_version", envelope="request"),
            source=source,
        )


@dataclass(frozen=True, slots=True)
class RecoveryControlReceipt:
    operation: str
    request_id: str
    status: str
    action: str = CONTROL_ACTION_RECOVERY_REQUEST
    schema_version: int = CONTROL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.action != CONTROL_ACTION_RECOVERY_REQUEST
            or self.schema_version != CONTROL_SCHEMA_VERSION
        ):
            raise ApplianceControlProtocolError("unsupported receipt envelope")
        _validate_recovery_operation(self.operation)
        _validate_recovery_request_id(self.operation, self.request_id)
        if self.status not in {"scheduled", "completed"}:
            raise ApplianceControlProtocolError("invalid receipt envelope")
        _validate_control_size(self.to_dict(), envelope="receipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "operation": self.operation,
            "request_id": self.request_id,
            "schema_version": self.schema_version,
            "status": self.status,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> RecoveryControlReceipt:
        value = _parse_control_bytes(payload, envelope="receipt")
        if set(value) != {"action", "operation", "request_id", "schema_version", "status"}:
            raise ApplianceControlProtocolError("invalid receipt envelope")
        return cls(
            action=_required_str(value, "action", envelope="receipt"),
            operation=_required_str(value, "operation", envelope="receipt"),
            request_id=_required_str(value, "request_id", envelope="receipt"),
            schema_version=_required_int(value, "schema_version", envelope="receipt"),
            status=_required_str(value, "status", envelope="receipt"),
        )


class ApplianceDaemon:
    """Own one root authority, one confined control socket, and one mutation path."""

    def __init__(
        self,
        root: Path,
        *,
        application_factory: (
            Callable[[Path, DaemonAuthorityCapability], ApplianceApplication] | None
        ) = None,
        scheduler_factory: (
            Callable[
                [Path, LocalEngineContext, DaemonAuthorityCapability],
                ApplianceScheduler,
            ]
            | None
        ) = None,
        connection_timeout: float = CONTROL_CONNECTION_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(root, Path):
            raise ValueError("invalid appliance root")
        _validate_timeout(connection_timeout)
        self._root = root
        self._application_factory = application_factory or _open_mutating_application
        self._scheduler_factory = scheduler_factory
        self._connection_timeout = connection_timeout
        self._profile: LocalEngineContext | None = None
        self._authority: DaemonAuthorityCapability | None = None
        self._application: ApplianceApplication | None = None
        self._scheduler: ApplianceScheduler | None = None
        self._http_configuration: ApplianceHttpConfiguration | None = None
        self._http_server: ManagedHttpServer | None = None
        self._http_thread: threading.Thread | None = None
        self._listener: socket.socket | None = None
        self._exit_stack: ExitStack | None = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._operation_condition = threading.Condition()
        self._active_operations = 0
        self._stopping = True

    def __enter__(self) -> ApplianceDaemon:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()

    @property
    def socket_path(self) -> Path:
        return _control_socket_path(self._root)

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start()

    def _start(self) -> None:
        if self._exit_stack is not None:
            raise RuntimeError("appliance daemon already started")
        profile = open_existing_single_user_local(self._root)
        stack = ExitStack()
        authority: DaemonAuthorityCapability
        try:
            try:
                authority = stack.enter_context(acquire_control_socket_authority(profile))
            except Exception as error:
                if type(error).__name__ != "LockBusyError":
                    raise
                raise ApplianceDaemonConflictError("appliance daemon already active") from error
            application = self._application_factory(self._root, authority)
            scheduler = (
                _open_scheduler(self._root, profile, authority, application)
                if self._scheduler_factory is None
                else self._scheduler_factory(self._root, profile, authority)
            )
            _ensure_run_directory(_run_directory(self._root))
            cleanup_stale_control_socket(profile, authority)
            listener = _bind_listener(self.socket_path)
        except Exception:
            stack.close()
            raise
        self._profile = profile
        self._authority = authority
        self._application = application
        self._scheduler = scheduler
        self._listener = listener
        self._exit_stack = stack
        self._stop_event.clear()
        with self._operation_condition:
            self._stopping = False

    def serve_once(self, *, timeout: float = 0.1) -> bool:
        listener = self._listener
        if listener is None or self._application is None:
            raise RuntimeError("appliance daemon is not running")
        listener.settimeout(timeout)
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            return False
        except OSError:
            if self._stop_event.is_set():
                return False
            raise
        with connection, self._operation():
            connection.settimeout(self._connection_timeout)
            request = _parse_request(_read_bounded_bytes(connection))
            _send_control_bytes(connection, _dispatch_request(self, request).to_bytes())
        return True

    def serve_until_stopped(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._scheduler is not None:
                    with self._operation():
                        self._scheduler.run_due(now=datetime.now(UTC))
                if self._stop_event.is_set():
                    break
                self.serve_once()
            except ApplianceSchedulerInterruptedError:
                continue
            except (ApplianceControlProtocolError, ConnectionError, TimeoutError):
                continue
            except RuntimeError:
                if self._stop_event.is_set():
                    break
                raise

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop()

    def _stop(self) -> None:
        listener = self._listener
        http_server = self._http_server
        http_thread = self._http_thread
        profile = self._profile
        authority = self._authority
        stack = self._exit_stack
        if (
            listener is None
            or profile is None
            or authority is None
            or stack is None
        ):
            return
        with self._operation_condition:
            self._stopping = True
        self._stop_event.set()
        if http_server is not None:
            try:
                http_server.shutdown()
            finally:
                self._http_server = None
        if (
            http_thread is not None
            and http_thread.is_alive()
            and http_thread is not threading.current_thread()
        ):
            http_thread.join()
        self._http_thread = None
        try:
            listener.close()
        finally:
            self._listener = None
        self._wait_for_operations()
        try:
            cleanup_stale_control_socket(profile, authority)
        finally:
            stack.close()
            self._exit_stack = None
            self._profile = None
            self._authority = None
            self._application = None
            self._scheduler = None
            self._http_configuration = None

    @contextmanager
    def _operation(self) -> Iterator[None]:
        with self._operation_condition:
            if self._stopping:
                raise RuntimeError("appliance daemon is stopping")
            self._active_operations += 1
        try:
            yield
        finally:
            with self._operation_condition:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._operation_condition.notify_all()

    def _wait_for_operations(self) -> None:
        with self._operation_condition:
            while self._active_operations > 0:
                self._operation_condition.wait()

    def _compose_http_service(
        self,
        configuration: ApplianceHttpConfiguration,
        *,
        clock: Callable[[], datetime] | None = None,
        route_mode: HttpRouteMode = HttpRouteMode.COMBINED,
    ) -> HttpService:
        application = self._application
        if (
            application is None
            or application.mutations is None
            or not isinstance(configuration, ApplianceHttpConfiguration)
            or not isinstance(route_mode, HttpRouteMode)
        ):
            raise RuntimeError("appliance daemon is not running")
        now = (lambda: datetime.now(UTC)) if clock is None else clock
        seed = read_private_service_secret(self._root / APPLIANCE_OWNER_CREDENTIAL)
        browser_credential = derive_appliance_credential(seed, purpose="browser-bootstrap")
        intake_credential = derive_appliance_credential(seed, purpose="intake-bearer")
        sessions = ApplianceBrowserSessionStore(
            expected_bootstrap_credential=browser_credential,
            now=now,
            secure_cookie=configuration.allowed_origin.startswith("https://"),
        )
        return HttpService(
            ui_handler=application.ui_handler(
                browser_sessions=cast(BrowserSessionStore, sessions),
                allowed_origin=configuration.allowed_origin,
                status_reader=lambda: read_appliance_status(
                    self._root,
                    bind=configuration.bind,
                    allowed_origin=configuration.allowed_origin,
                    external_encryption_terminated=(
                        configuration.external_encryption_terminated
                    ),
                    daemon_authority_held=True,
                    now=now,
                ).to_dict(),
                history_reader=lambda limit: read_appliance_run_history(
                    self._root,
                    limit=limit,
                ).to_dict(),
            ),
            share_handler_factory=lambda body_reader: ShareHttpHandler(
                expected_bearer_token=intake_credential,
                capture=application.mutations.capture,
                body_reader=body_reader,
                clock=now,
            ),
            config=HttpServiceConfig(
                bind=configuration.bind,
                route_mode=route_mode,
            ),
            operation_gate=self._operation,
        )

    def start_http_listener(
        self,
        configuration: ApplianceHttpConfiguration,
        *,
        clock: Callable[[], datetime] | None = None,
        route_mode: HttpRouteMode = HttpRouteMode.COMBINED,
        server_factory: HttpServerFactory | None = None,
    ) -> None:
        with self._lifecycle_lock:
            if self._exit_stack is None:
                raise RuntimeError("appliance daemon is not running")
            if self._http_server is not None or self._http_thread is not None:
                raise RuntimeError("appliance HTTP listener already started")
            server = create_http_server(
                self._compose_http_service(
                    configuration,
                    clock=clock,
                    route_mode=route_mode,
                ),
                server_factory=server_factory,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            self._http_configuration = configuration
            self._http_server = server
            self._http_thread = thread
            try:
                thread.start()
            except Exception:
                self._http_configuration = None
                self._http_server = None
                self._http_thread = None
                server.close()
                raise


def cleanup_stale_control_socket(
    profile: LocalEngineContext,
    authority: object | None,
) -> None:
    if not isinstance(profile, LocalEngineContext):
        raise ValueError("invalid local profile")
    require_daemon_authority(profile, authority)
    path = _control_socket_path(profile.root)
    try:
        metadata = _lstat_path(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise ApplianceControlSocketError("control socket symlink replacement refused")
    if not stat.S_ISSOCK(metadata.st_mode):
        raise ApplianceControlSocketError("control socket non-socket replacement refused")
    if not _same_identity(metadata, _lstat_path(path)):
        raise ApplianceControlSocketError("control socket replaced during cleanup")
    path.unlink()


@contextmanager
def acquire_control_socket_authority(
    profile: LocalEngineContext,
) -> Iterator[DaemonAuthorityCapability]:
    if not isinstance(profile, LocalEngineContext):
        raise ValueError("invalid local profile")
    with acquire_daemon_authority(profile) as authority:
        yield authority


def request_control(
    root: Path,
    request: ControlRequest,
    *,
    timeout: float = 1.0,
) -> ControlReceipt:
    if not isinstance(root, Path):
        raise ValueError("invalid appliance root")
    _validate_timeout(timeout)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        try:
            client.connect(str(_control_socket_path(root)))
        except OSError as error:
            raise ApplianceControlUnavailableError(
                "appliance control socket unavailable"
            ) from error
        client.sendall(request.to_bytes())
        client.shutdown(socket.SHUT_WR)
        return ControlReceipt.from_bytes(_read_bounded_bytes(client))
    finally:
        client.close()


def request_cli_dispatch(
    root: Path,
    request: CliControlRequest,
    *,
    timeout: float = 1.0,
) -> CliControlReceipt:
    return CliControlReceipt.from_bytes(_request_bytes(root, request.to_bytes(), timeout=timeout))


def request_status(
    root: Path,
    request: StatusControlRequest | None = None,
    *,
    timeout: float = 1.0,
) -> StatusControlReceipt:
    selected = StatusControlRequest() if request is None else request
    return StatusControlReceipt.from_bytes(
        _request_bytes(root, selected.to_bytes(), timeout=timeout)
    )


def request_recovery_job(
    root: Path,
    request: RecoveryControlRequest,
    *,
    timeout: float = 1.0,
) -> RecoveryControlReceipt:
    if not isinstance(request, RecoveryControlRequest):
        raise ValueError("invalid appliance recovery request")
    return RecoveryControlReceipt.from_bytes(
        _request_bytes(root, request.to_bytes(), timeout=timeout)
    )


def _open_mutating_application(
    root: Path,
    authority: DaemonAuthorityCapability,
) -> ApplianceApplication:
    return ApplianceApplication.open_mutating(root, authority=authority)


def _open_scheduler(
    root: Path,
    profile: LocalEngineContext,
    authority: DaemonAuthorityCapability,
    application: ApplianceApplication,
) -> ApplianceScheduler:
    recovery = ApplianceRecoveryService(root, application)
    return ApplianceScheduler(
        profile,
        handlers={
            "backup-create": lambda context: recovery.handle_job("backup-create", context),
            "markdown-reconcile": (
                lambda _context: ApplianceJobResult.completed()
                if application.mutations is not None
                and application.mutations.reconciliation.reconcile().status == "reconciled"
                else ApplianceJobResult.empty()
            ),
            "portable-export": lambda context: recovery.handle_job("portable-export", context),
            "portable-import": lambda context: recovery.handle_job("portable-import", context),
        },
        engine_recoverer=lambda: recover_authoritative_local_engine(profile, authority),
    )


def _run_directory(root: Path) -> Path:
    return root / ".open-brain" / "run"


def _control_socket_path(root: Path) -> Path:
    return _run_directory(root) / "control.sock"


def _request_bytes(root: Path, payload: bytes, *, timeout: float) -> bytes:
    if not isinstance(root, Path):
        raise ValueError("invalid appliance root")
    _validate_timeout(timeout)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        try:
            client.connect(str(_control_socket_path(root)))
        except OSError as error:
            raise ApplianceControlUnavailableError(
                "appliance control socket unavailable"
            ) from error
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        return _read_bounded_bytes(client)
    finally:
        client.close()


def _ensure_run_directory(path: Path) -> None:
    try:
        metadata = _lstat_path(path)
    except FileNotFoundError:
        path.mkdir(exist_ok=True)
        os.chmod(path, RUN_DIRECTORY_MODE)
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ApplianceControlSocketError("invalid appliance run directory")
    os.chmod(path, RUN_DIRECTORY_MODE)


def _bind_listener(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(CONTROL_SOCKET_BACKLOG)
        os.chmod(path, SOCKET_MODE)
        return listener
    except OSError as error:
        listener.close()
        if error.errno == errno.EADDRINUSE:
            raise ApplianceControlSocketError("control socket already bound") from error
        raise


def _read_bounded_bytes(connection: socket.socket) -> bytes:
    chunks = bytearray()
    limit = MAXIMUM_CONTROL_ENVELOPE_BYTES + 1
    while len(chunks) <= MAXIMUM_CONTROL_ENVELOPE_BYTES:
        chunk = connection.recv(limit - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    payload = bytes(chunks)
    if len(payload) > MAXIMUM_CONTROL_ENVELOPE_BYTES:
        raise ApplianceControlProtocolError("control envelope is too large")
    return payload


def _parse_control_bytes(payload: bytes, *, envelope: str) -> dict[str, object]:
    if not isinstance(payload, bytes):
        raise ApplianceControlProtocolError(f"invalid {envelope} envelope")
    if len(payload) > MAXIMUM_CONTROL_ENVELOPE_BYTES:
        raise ApplianceControlProtocolError("control envelope is too large")
    try:
        value = json.loads(payload.decode("utf-8"))
        canonical = canonical_json_bytes(value)
    except Exception as error:
        raise ApplianceControlProtocolError(f"invalid {envelope} envelope") from error
    if not isinstance(value, dict):
        raise ApplianceControlProtocolError(f"invalid {envelope} envelope")
    if canonical != payload:
        raise ApplianceControlProtocolError(f"{envelope} envelope must be canonical")
    return value


def _required_int(value: dict[str, object], key: str, *, envelope: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ApplianceControlProtocolError(f"invalid {envelope} envelope")
    return item


def _required_str(value: dict[str, object], key: str, *, envelope: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ApplianceControlProtocolError(f"invalid {envelope} envelope")
    return item


def _required_argv(value: dict[str, object], *, envelope: str) -> tuple[str, ...]:
    item = value.get("argv")
    if not isinstance(item, list) or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in item
    ):
        raise ApplianceControlProtocolError(f"invalid {envelope} envelope")
    return tuple(item)


def _validate_capture_action(value: str) -> None:
    if value != CONTROL_ACTION_CAPTURE_TEXT:
        raise ApplianceControlProtocolError("unsupported action")


def _validate_control_command(value: str) -> None:
    if value not in _CONTROLLED_COMMANDS:
        raise ApplianceControlProtocolError("unsupported action")


def _validate_recovery_operation(value: str) -> None:
    if value not in {"backup-create", "portable-export", "portable-import"}:
        raise ApplianceControlProtocolError("unsupported action")


def _validate_recovery_request_id(operation: str, value: str) -> None:
    prefix = {
        "backup-create": "backup",
        "portable-export": "export",
        "portable-import": "import",
    }.get(operation)
    if prefix is None:
        raise ApplianceControlProtocolError("unsupported action")
    marker = prefix + "_"
    if not isinstance(value, str) or not value.startswith(marker):
        raise ApplianceControlProtocolError("invalid request envelope")
    try:
        identifier = uuid.UUID(value.removeprefix(marker))
    except ValueError as error:
        raise ApplianceControlProtocolError("invalid request envelope") from error
    if identifier.version != 4 or value != f"{prefix}_{identifier}":
        raise ApplianceControlProtocolError("invalid request envelope")


def _validate_control_path(value: str, *, envelope: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or not Path(value).is_absolute()
    ):
        raise ApplianceControlProtocolError(f"invalid {envelope} envelope")


def _validate_argv(value: tuple[str, ...]) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(argument, str) or not argument or "\x00" in argument for argument in value
    ):
        raise ApplianceControlProtocolError("invalid request envelope")


def _validate_delivery_id(value: str, *, envelope: str) -> None:
    if not isinstance(value, str) or _DELIVERY_ID.fullmatch(value) is None:
        raise ApplianceControlProtocolError(f"invalid {envelope} envelope")


def _validate_timeout(value: float) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ValueError("invalid appliance control timeout")


def _validate_control_size(value: object, *, envelope: str) -> None:
    if len(canonical_json_bytes(value)) > MAXIMUM_CONTROL_ENVELOPE_BYTES:
        raise ApplianceControlProtocolError(f"{envelope} envelope is too large")


def _validate_portable_identifier(value: str, *, prefix: str) -> None:
    marker = prefix + "_"
    if not isinstance(value, str) or not value.startswith(marker):
        raise ApplianceControlProtocolError("invalid receipt envelope")
    try:
        identifier = uuid.UUID(value.removeprefix(marker))
    except ValueError as error:
        raise ApplianceControlProtocolError("invalid receipt envelope") from error
    if identifier.version != 4 or value != f"{prefix}_{identifier}":
        raise ApplianceControlProtocolError("invalid receipt envelope")


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _send_control_bytes(connection: socket.socket, payload: bytes) -> None:
    connection.sendall(payload)


def _lstat_path(path: Path) -> os.stat_result:
    return path.lstat()


def _parse_request(
    payload: bytes,
) -> ControlRequest | CliControlRequest | StatusControlRequest | RecoveryControlRequest:
    value = _parse_control_bytes(payload, envelope="request")
    action = _required_str(value, "action", envelope="request")
    if action == CONTROL_ACTION_CAPTURE_TEXT:
        return ControlRequest.from_bytes(payload)
    if action == CONTROL_ACTION_CLI_DISPATCH:
        return CliControlRequest.from_bytes(payload)
    if action == CONTROL_ACTION_STATUS_READ:
        return StatusControlRequest.from_bytes(payload)
    if action == CONTROL_ACTION_RECOVERY_REQUEST:
        return RecoveryControlRequest.from_bytes(payload)
    raise ApplianceControlProtocolError("unsupported action")


def _cli_receipt(
    request: CliControlRequest,
    application: ApplianceApplication,
) -> CliControlReceipt:
    adapter = application.cli_adapter(request.command)
    if adapter is None:
        return _failed_cli_receipt(request.command)
    try:
        result = adapter.dispatch(request.argv)
        envelope = validate_adapter_envelope(
            request.command,
            result.envelope,
            argv=request.argv,
        )
        return CliControlReceipt(
            command=request.command,
            envelope=envelope,
            exit_code=int(result.exit_code),
        )
    except Exception:
        return _failed_cli_receipt(request.command)


def _failed_cli_receipt(command: str) -> CliControlReceipt:
    return CliControlReceipt(
        command=command,
        envelope=adapter_failed_envelope(command),
        exit_code=int(ExitCode.FAILURE),
    )


def _status_receipt(self: ApplianceDaemon) -> StatusControlReceipt:
    configuration = self._http_configuration
    return StatusControlReceipt(
        envelope=read_appliance_status(
            self._root,
            bind=None if configuration is None else configuration.bind,
            allowed_origin=None if configuration is None else configuration.allowed_origin,
            external_encryption_terminated=(
                False if configuration is None else configuration.external_encryption_terminated
            ),
            daemon_authority_held=True,
        ).to_dict()
    )


def _capture_receipt(
    request: ControlRequest,
    application: ApplianceApplication,
) -> ControlReceipt:
    assert application.mutations is not None
    accepted = application.mutations.capture.accept(
        TextPayload(request.text),
        delivery_id=request.delivery_id,
    )
    return ControlReceipt(
        delivery_id=request.delivery_id,
        capture_id=accepted.capture_id,
        state=accepted.state,
    )


def _recovery_receipt(
    self: ApplianceDaemon,
    request: RecoveryControlRequest,
) -> RecoveryControlReceipt:
    application = self._application
    scheduler = self._scheduler
    if application is None or scheduler is None:
        raise RuntimeError("appliance daemon is not running")
    recovery = application.recovery(scheduler=scheduler)
    destination = Path(request.destination)
    if request.operation == "backup-create":
        submission = recovery.request_backup(destination, backup_id=request.request_id)
    elif request.operation == "portable-export":
        submission = recovery.request_portable_export(
            destination,
            export_id=request.request_id,
        )
    else:
        assert request.source is not None
        submission = recovery.request_portable_import(
            Path(request.source),
            destination,
            import_id=request.request_id,
        )
    return RecoveryControlReceipt(
        operation=request.operation,
        request_id=request.request_id,
        status=submission.status,
    )


def _dispatch_request(
    self: ApplianceDaemon,
    request: ControlRequest | CliControlRequest | StatusControlRequest | RecoveryControlRequest,
) -> ControlReceipt | CliControlReceipt | StatusControlReceipt | RecoveryControlReceipt:
    application = self._application
    if application is None:
        raise RuntimeError("appliance daemon is not running")
    if isinstance(request, ControlRequest):
        return _capture_receipt(request, application)
    if isinstance(request, CliControlRequest):
        return _cli_receipt(request, application)
    if isinstance(request, StatusControlRequest):
        return _status_receipt(self)
    return _recovery_receipt(self, request)


def main(
    argv: tuple[str, ...] | list[str] | None = None,
    *,
    environment: dict[str, str] | None = None,
    http_server_factory: HttpServerFactory | None = None,
    enable_http_listener: bool = True,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m open_brain.services.appliance_daemon")
    parser.add_argument("--root", required=True)
    namespace = parser.parse_args(tuple(sys.argv[1:] if argv is None else argv))
    root = Path(namespace.root)
    if not root.is_absolute():
        raise SystemExit(ExitCode.CONFIGURATION)
    env = os.environ if environment is None else environment
    try:
        http_configuration = (
            None
            if not enable_http_listener
            else appliance_http_configuration_from_environment(env)
        )
    except ServiceConfigurationError:
        return ExitCode.CONFIGURATION
    try:
        with ApplianceDaemon(root) as daemon:
            if http_configuration is not None:
                daemon.start_http_listener(
                    http_configuration,
                    server_factory=http_server_factory,
                )
            try:
                daemon.serve_until_stopped()
            except KeyboardInterrupt:
                daemon.stop()
    except (OSError, RuntimeError, ValueError):
        return ExitCode.CONFIGURATION
    return ExitCode.SUCCESS


__all__ = [
    "MAXIMUM_CONTROL_ENVELOPE_BYTES",
    "ApplianceControlProtocolError",
    "ApplianceControlSocketError",
    "ApplianceControlUnavailableError",
    "ApplianceDaemon",
    "ApplianceDaemonConflictError",
    "CliControlReceipt",
    "CliControlRequest",
    "CONTROL_ACTION_CAPTURE_TEXT",
    "CONTROL_ACTION_CLI_DISPATCH",
    "CONTROL_ACTION_RECOVERY_REQUEST",
    "CONTROL_ACTION_STATUS_READ",
    "ControlRequest",
    "ControlReceipt",
    "RecoveryControlReceipt",
    "RecoveryControlRequest",
    "StatusControlReceipt",
    "StatusControlRequest",
    "acquire_control_socket_authority",
    "cleanup_stale_control_socket",
    "main",
    "request_cli_dispatch",
    "request_control",
    "request_recovery_job",
    "request_status",
]


if __name__ == "__main__":
    raise SystemExit(main())
