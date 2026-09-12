"""Shared, app-owned HTTP, MCP, configuration, and secret composition helpers."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from open_brain_engine.engine import EngineTaskSet, PublicJobCaptureSink
from open_brain_engine.engine.contracts import DaemonMutationPath

from open_brain.capture.http import CaptureAcceptor, ShareHttpHandler
from open_brain.config import (
    AppConfig,
    NamedSecretRef,
    SecretResolutionError,
    resolve_secret,
)
from open_brain.integrations.mcp import EngineMcpAdapter
from open_brain.integrations.phase1_ui import Phase1UiHandler
from open_brain.integrations.ui import UiBindConfig

from .appliance_auth import allowed_origin_for_host
from .composition import (
    HttpLifecycle,
    StdioMcpLifecycle,
)
from .http_server import HttpRouteMode, HttpService, HttpServiceConfig

_SERVICE_SECRET_NAME = "service_token"
_SERVICE_SECRET_NAMES = frozenset(
    {_SERVICE_SECRET_NAME, "ui_service_token", "ingress_service_token"}
)
_MAXIMUM_SECRET_BYTES = 4_096
APPLIANCE_RUN_DIRECTORY_MODE = 0o700
APPLIANCE_CONTROL_SOCKET_MODE = 0o600
RESERVED_APPLIANCE_APPLICATION_MODULE = "open_brain.services.appliance_application"
RESERVED_APPLIANCE_ENTRYPOINT_MODULE = "open_brain.services.appliance_entrypoints"
RESERVED_APPLIANCE_CLI_ENTRYPOINT = f"{RESERVED_APPLIANCE_ENTRYPOINT_MODULE}:run_cli"
RESERVED_APPLIANCE_HTTP_ENTRYPOINT = f"{RESERVED_APPLIANCE_ENTRYPOINT_MODULE}:run_http"
RESERVED_APPLIANCE_MCP_ENTRYPOINT = f"{RESERVED_APPLIANCE_ENTRYPOINT_MODULE}:run_mcp"
_EXTERNAL_TLS_TERMINATION = "OPEN_BRAIN_UI_EXTERNAL_TLS_TERMINATION"
_EXTERNAL_ORIGIN = "OPEN_BRAIN_UI_EXTERNAL_ORIGIN"


class ServiceConfigurationError(RuntimeError):
    """A service cannot start from the supplied non-secret configuration."""


@dataclass(frozen=True, slots=True)
class ApplianceHttpConfiguration:
    bind: UiBindConfig
    allowed_origin: str
    external_encryption_terminated: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bind, UiBindConfig)
            or not isinstance(self.allowed_origin, str)
            or not self.allowed_origin
            or type(self.external_encryption_terminated) is not bool
        ):
            raise ValueError("invalid HTTP service configuration")
        try:
            if self.bind.allow_private_network:
                valid = (
                    self.external_encryption_terminated
                    and _https_origin(self.allowed_origin) == self.allowed_origin
                )
            else:
                valid = (
                    not self.external_encryption_terminated
                    and self.allowed_origin
                    == allowed_origin_for_host(self.bind.host, self.bind.port)
                )
        except ServiceConfigurationError:
            valid = False
        if not valid:
            raise ValueError("invalid HTTP service configuration")


@dataclass(frozen=True, slots=True)
class ApplianceControlPlane:
    application_module: str
    entrypoint_module: str
    cli_entrypoint: str
    http_entrypoint: str
    mcp_entrypoint: str
    daemon_mutation_path: DaemonMutationPath


class _SingleUserApplication(Protocol):
    @property
    def tasks(self) -> EngineTaskSet: ...

    def mcp_adapter(
        self, *, allowed_space_ids: frozenset[str] = frozenset()
    ) -> EngineMcpAdapter: ...

    def ui_handler(self, expected_bearer_token: str) -> Phase1UiHandler: ...


def reserved_appliance_control_plane(
    daemon_mutation_path: DaemonMutationPath,
) -> ApplianceControlPlane:
    if not isinstance(daemon_mutation_path, DaemonMutationPath):
        raise ValueError("invalid appliance control plane")
    return ApplianceControlPlane(
        application_module=RESERVED_APPLIANCE_APPLICATION_MODULE,
        entrypoint_module=RESERVED_APPLIANCE_ENTRYPOINT_MODULE,
        cli_entrypoint=RESERVED_APPLIANCE_CLI_ENTRYPOINT,
        http_entrypoint=RESERVED_APPLIANCE_HTTP_ENTRYPOINT,
        mcp_entrypoint=RESERVED_APPLIANCE_MCP_ENTRYPOINT,
        daemon_mutation_path=daemon_mutation_path,
    )


def compose_mcp_from_config(
    *,
    application: _SingleUserApplication,
    allowed_space_ids: frozenset[str] = frozenset(),
) -> StdioMcpLifecycle:
    """Compose scoped MCP from one app-owned engine task set."""
    if (
        not _is_single_user_application(application)
        or not isinstance(allowed_space_ids, frozenset)
    ):
        raise ServiceConfigurationError("invalid MCP service configuration")
    try:
        adapter = application.mcp_adapter(allowed_space_ids=allowed_space_ids)
    except Exception as error:
        raise ServiceConfigurationError("invalid MCP service configuration") from error
    return StdioMcpLifecycle(adapter)


def compose_http_from_config(
    *,
    config: AppConfig,
    application: _SingleUserApplication,
    environment: Mapping[str, str],
    file_reader: Callable[[Path], str],
    bind: UiBindConfig | None = None,
    secret_name: str = _SERVICE_SECRET_NAME,
    route_mode: HttpRouteMode = HttpRouteMode.COMBINED,
    capture: CaptureAcceptor | PublicJobCaptureSink | None = None,
) -> HttpLifecycle:
    """Resolve one named credential, then compose authenticated UI/share HTTP."""
    if (
        not isinstance(config, AppConfig)
        or not _is_single_user_application(application)
        or not isinstance(environment, Mapping)
        or not callable(file_reader)
        or secret_name not in _SERVICE_SECRET_NAMES
        or not isinstance(route_mode, HttpRouteMode)
        or (
            capture is not None
            and not callable(getattr(capture, "accept", None))
            and not isinstance(capture, PublicJobCaptureSink)
        )
    ):
        raise ServiceConfigurationError("invalid HTTP service configuration")
    token = _service_token(
        config,
        secret_name=secret_name,
        environment=environment,
        file_reader=file_reader,
    )
    try:
        selected_capture = application.tasks.capture if capture is None else capture
        http = HttpService(
            ui_handler=application.ui_handler(token),
            share_handler_factory=lambda body_reader: ShareHttpHandler(
                expected_bearer_token=token,
                capture=selected_capture,
                body_reader=body_reader,
                clock=_utc_now,
            ),
            config=HttpServiceConfig(
                bind=bind or UiBindConfig(),
                route_mode=route_mode,
            ),
        )
    except Exception as error:
        raise ServiceConfigurationError("invalid HTTP service configuration") from error
    return HttpLifecycle(http)


def _is_single_user_application(value: object) -> bool:
    tasks = getattr(value, "tasks", None)
    capture = getattr(tasks, "capture", None)
    return (
        callable(getattr(value, "mcp_adapter", None))
        and callable(getattr(value, "ui_handler", None))
        and callable(getattr(capture, "accept", None))
    )


def read_private_service_secret(path: Path) -> str:
    payload = _read_owner_file(path)
    value = payload.decode("utf-8").rstrip("\r\n")
    if not value:
        raise OSError("invalid secret file")
    return value


def load_private_http_bind_config(path: Path) -> UiBindConfig:
    """Load one canonical owner-only, non-secret HTTP bind configuration."""
    try:
        payload = _read_owner_file(path)
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if (
            not isinstance(value, dict)
            or set(value)
            != {"schema_version", "host", "port", "allow_private_network"}
            or value["schema_version"] != 1
            or not isinstance(value["host"], str)
            or not isinstance(value["port"], int)
            or isinstance(value["port"], bool)
            or type(value["allow_private_network"]) is not bool
            or _json_bytes(value) != payload
        ):
            raise ValueError
        return UiBindConfig(
            host=value["host"],
            port=value["port"],
            allow_private_network=value["allow_private_network"],
        )
    except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ServiceConfigurationError("invalid HTTP service configuration") from None


def bind_from_environment(environment: Mapping[str, str]) -> UiBindConfig:
    """Build the public HTTP bind settings from bounded process configuration."""
    host = environment.get("OPEN_BRAIN_UI_BIND", "127.0.0.1")
    raw_port = environment.get("OPEN_BRAIN_UI_PORT", "8788")
    raw_private = environment.get("OPEN_BRAIN_UI_ALLOW_PRIVATE", "false")
    if not isinstance(host, str) or not isinstance(raw_port, str) or not isinstance(
        raw_private, str
    ):
        raise ServiceConfigurationError("invalid HTTP service configuration")
    if not raw_port.isascii() or not raw_port.isdigit():
        raise ServiceConfigurationError("invalid HTTP service configuration")
    if raw_private not in {"true", "false"}:
        raise ServiceConfigurationError("invalid HTTP service configuration")
    try:
        return UiBindConfig(
            host=host,
            port=int(raw_port),
            allow_private_network=raw_private == "true",
        )
    except ValueError:
        raise ServiceConfigurationError("invalid HTTP service configuration") from None


def appliance_http_configuration_from_environment(
    environment: Mapping[str, str],
) -> ApplianceHttpConfiguration:
    bind = bind_from_environment(environment)
    raw_terminated = environment.get(_EXTERNAL_TLS_TERMINATION, "false")
    raw_external_origin = environment.get(_EXTERNAL_ORIGIN)
    if not isinstance(raw_terminated, str) or raw_terminated not in {"true", "false"}:
        raise ServiceConfigurationError("invalid HTTP service configuration")
    terminated = raw_terminated == "true"
    if bind.allow_private_network:
        if not terminated:
            raise ServiceConfigurationError("invalid HTTP service configuration")
        allowed_origin = _https_origin(raw_external_origin)
    else:
        if terminated or raw_external_origin not in {None, ""}:
            raise ServiceConfigurationError("invalid HTTP service configuration")
        allowed_origin = allowed_origin_for_host(bind.host, bind.port)
    try:
        return ApplianceHttpConfiguration(
            bind=bind,
            allowed_origin=allowed_origin,
            external_encryption_terminated=terminated,
        )
    except ValueError:
        raise ServiceConfigurationError("invalid HTTP service configuration") from None


def _https_origin(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise ServiceConfigurationError("invalid HTTP service configuration")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ServiceConfigurationError("invalid HTTP service configuration") from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or hostname is None
    ):
        raise ServiceConfigurationError("invalid HTTP service configuration")
    if port is not None and not 1 <= port <= 65_535:
        raise ServiceConfigurationError("invalid HTTP service configuration")
    return value


def _service_token(
    config: AppConfig,
    *,
    secret_name: str,
    environment: Mapping[str, str],
    file_reader: Callable[[Path], str],
) -> str:
    references = tuple(
        reference
        for reference in config.secret_refs
        if reference.name == secret_name
    )
    if len(references) != 1 or not isinstance(references[0], NamedSecretRef):
        raise ServiceConfigurationError("service credential unavailable")
    try:
        value = resolve_secret(
            references[0].reference,
            environment=environment,
            file_reader=file_reader,
        )
    except SecretResolutionError:
        raise ServiceConfigurationError("service credential unavailable") from None
    if len(value.encode("utf-8")) > _MAXIMUM_SECRET_BYTES:
        raise ServiceConfigurationError("service credential unavailable")
    return value


def _read_owner_file(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise OSError("invalid secret file")
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_size > _MAXIMUM_SECRET_BYTES
    ):
        raise OSError("invalid secret file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(path, flags)
    try:
        current = os.fstat(file_descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OSError("secret file changed")
        payload = os.read(file_descriptor, _MAXIMUM_SECRET_BYTES + 1)
    finally:
        os.close(file_descriptor)
    if len(payload) > _MAXIMUM_SECRET_BYTES:
        raise OSError("invalid secret file")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _utc_now() -> datetime:
    return datetime.now(UTC)
