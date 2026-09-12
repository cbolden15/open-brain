"""Explicit production composition for bounded local and optional cloud providers."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.providers.base import (
    CloudFactory,
    ProviderService,
    SecretResolver,
    lazy_cloud_factory,
    unavailable_cloud_factory,
)

from open_brain_legacy._compat.open_brain.config import (
    AppConfig,
    NamedSecretRef,
    SecretResolutionError,
    resolve_secret,
)
from open_brain_legacy.providers.local import LocalProvider, LocalTransport

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_MODULE = re.compile(r"open_brain_legacy\.providers(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_MAXIMUM_WIRE_OVERHEAD_BYTES = 16_384
_MAXIMUM_REQUEST_BYTES = 2 * 1024 * 1024
_MAXIMUM_CONFIG_BYTES = 64 * 1024
_CREDENTIAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")


class ProviderTransportError(ConnectionError):
    """A closed local-provider transport failure without response residue."""

    def __init__(self) -> None:
        super().__init__("local provider transport failed")


class ProductionProviderConfigError(ValueError):
    """The private provider selection or credential reference is invalid."""


class JsonModelResponse(Protocol):
    status: int

    def read(self, maximum_bytes: int) -> bytes: ...


class JsonModelConnection(Protocol):
    def request(
        self,
        method: str,
        target: str,
        body: bytes,
        headers: dict[str, str],
    ) -> JsonModelResponse: ...

    def close(self) -> None: ...


class JsonModelConnectionFactory(Protocol):
    def open(
        self,
        *,
        scheme: str,
        hostname: str,
        port: int,
        timeout: float,
    ) -> JsonModelConnection: ...


@dataclass(frozen=True, slots=True)
class LocalProviderRuntimeConfig:
    """Non-secret provider selection and endpoint configuration."""

    provider_name: str
    cloud_enabled: bool
    local_endpoint: str
    local_model: str
    cloud_module: str
    cloud_model: str

    def __post_init__(self) -> None:
        if self.provider_name not in {"local", "cloud"} or type(self.cloud_enabled) is not bool:
            raise ValueError("invalid provider runtime configuration")
        _endpoint(self.local_endpoint)
        if (
            not isinstance(self.local_model, str)
            or _MODEL.fullmatch(self.local_model) is None
        ):
            raise ValueError("invalid local provider model")
        if (
            not isinstance(self.cloud_module, str)
            or _MODULE.fullmatch(self.cloud_module) is None
            or not isinstance(self.cloud_model, str)
            or _MODEL.fullmatch(self.cloud_model) is None
        ):
            raise ValueError("invalid cloud provider module")


@dataclass(frozen=True, slots=True)
class PrivateProviderConfig:
    """Non-secret, owner-only provider settings used by the composition root."""

    local_endpoint: str
    local_model: str
    cloud_module: str
    cloud_model: str
    credential_name: str

    def __post_init__(self) -> None:
        try:
            _endpoint(self.local_endpoint)
        except ValueError as error:
            raise ProductionProviderConfigError("invalid private provider config") from error
        if (
            not isinstance(self.local_model, str)
            or _MODEL.fullmatch(self.local_model) is None
            or not isinstance(self.cloud_module, str)
            or _MODULE.fullmatch(self.cloud_module) is None
            or not isinstance(self.cloud_model, str)
            or _MODEL.fullmatch(self.cloud_model) is None
            or not isinstance(self.credential_name, str)
            or _CREDENTIAL_NAME.fullmatch(self.credential_name) is None
        ):
            raise ProductionProviderConfigError("invalid private provider config")

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> PrivateProviderConfig:
        try:
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "schema_version",
                    "local_endpoint",
                    "local_model",
                    "cloud_module",
                    "cloud_model",
                    "credential_name",
                }
                or value["schema_version"] != 1
                or any(
                    not isinstance(value[name], str)
                    for name in (
                        "local_endpoint",
                        "local_model",
                        "cloud_module",
                        "cloud_model",
                        "credential_name",
                    )
                )
            ):
                raise ProductionProviderConfigError("invalid private provider config")
            result = cls(
                local_endpoint=value["local_endpoint"],
                local_model=value["local_model"],
                cloud_module=value["cloud_module"],
                cloud_model=value["cloud_model"],
                credential_name=value["credential_name"],
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            if isinstance(error, ProductionProviderConfigError):
                raise
            raise ProductionProviderConfigError("invalid private provider config") from error
        if result.canonical_bytes() != payload:
            raise ProductionProviderConfigError("invalid private provider config")
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "local_endpoint": self.local_endpoint,
                "local_model": self.local_model,
                "cloud_module": self.cloud_module,
                "cloud_model": self.cloud_model,
                "credential_name": self.credential_name,
            }
        )


@dataclass(frozen=True, slots=True)
class ProviderComposition:
    """Build exactly one provider service from explicit dependencies."""

    config: LocalProviderRuntimeConfig
    local_transport: LocalTransport
    resolve_cloud_secret: SecretResolver

    def __post_init__(self) -> None:
        if (
            not isinstance(self.config, LocalProviderRuntimeConfig)
            or not callable(getattr(self.local_transport, "complete", None))
            or not callable(self.resolve_cloud_secret)
        ):
            raise ValueError("invalid provider composition")

    def build(self) -> ProviderService:
        config = self.config
        cloud_factory: CloudFactory = unavailable_cloud_factory
        if (
            config.provider_name == "cloud"
            and config.cloud_enabled
            and config.cloud_module == "open_brain_legacy.providers.optional_cloud"
        ):
            from open_brain_legacy.providers.optional_cloud import create_provider

            cloud_factory = lazy_cloud_factory(create_provider, model=config.cloud_model)
        return ProviderService(
            provider_name=config.provider_name,
            cloud_enabled=config.cloud_enabled,
            local_factory=lambda: LocalProvider(
                endpoint=config.local_endpoint,
                model=config.local_model,
                transport=self.local_transport,
            ),
            cloud_factory=cloud_factory,
            resolve_cloud_secret=self.resolve_cloud_secret,
        )


def load_private_provider_config(path: Path) -> PrivateProviderConfig:
    return PrivateProviderConfig.from_canonical_bytes(_read_owner_file(path))


def compose_production_provider(
    *,
    app_config: AppConfig,
    config_path: Path,
    environment: Mapping[str, str],
    file_reader: Callable[[Path], str],
    local_transport: LocalTransport | None = None,
) -> ProviderService:
    """Bind AppConfig selection to owner-only settings and a lazy secret reference."""

    if (
        not isinstance(app_config, AppConfig)
        or app_config.provider not in {"local", "cloud"}
        or not isinstance(environment, Mapping)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()
        )
        or not callable(file_reader)
    ):
        raise ProductionProviderConfigError("invalid provider composition")
    if app_config.provider == "cloud" and (
        not app_config.cloud_enabled or not app_config.egress_enabled
    ):
        raise ProductionProviderConfigError("cloud provider authority unavailable")
    private = load_private_provider_config(config_path)
    references = tuple(
        reference
        for reference in app_config.secret_refs
        if reference.name == private.credential_name
    )
    if app_config.provider == "cloud" and (
        len(references) != 1 or not isinstance(references[0], NamedSecretRef)
    ):
        raise ProductionProviderConfigError("cloud provider credential unavailable")

    def resolve_cloud_secret() -> str | None:
        if len(references) != 1:
            return None
        try:
            return resolve_secret(
                references[0].reference,
                environment=environment,
                file_reader=file_reader,
            )
        except SecretResolutionError:
            return None

    return ProviderComposition(
        config=LocalProviderRuntimeConfig(
            provider_name=app_config.provider,
            cloud_enabled=app_config.cloud_enabled,
            local_endpoint=private.local_endpoint,
            local_model=private.local_model,
            cloud_module=private.cloud_module,
            cloud_model=private.cloud_model,
        ),
        local_transport=local_transport or StdlibLocalModelTransport(),
        resolve_cloud_secret=resolve_cloud_secret,
    ).build()


class StdlibLocalModelTransport:
    """A bounded JSON-over-HTTP transport for an operator-configured local model."""

    def __init__(
        self,
        *,
        connection_factory: JsonModelConnectionFactory | None = None,
    ) -> None:
        self._connection_factory = connection_factory or _StdlibConnectionFactory()

    def complete(
        self,
        *,
        endpoint: str,
        model: str,
        prompt: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> str:
        scheme, hostname, port, target = _endpoint(endpoint)
        if (
            not isinstance(model, str)
            or _MODEL.fullmatch(model) is None
            or not isinstance(prompt, str)
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
            or not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or max_output_bytes < 1
        ):
            raise ProviderTransportError
        body = json.dumps(
            {
                "format": "json",
                "model": model,
                "options": {"temperature": 0},
                "prompt": prompt,
                "stream": False,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(body) > _MAXIMUM_REQUEST_BYTES:
            raise ProviderTransportError
        connection: JsonModelConnection | None = None
        try:
            connection = self._connection_factory.open(
                scheme=scheme,
                hostname=hostname,
                port=port,
                timeout=float(timeout_seconds),
            )
            response = connection.request(
                "POST",
                target,
                body,
                {"Content-Type": "application/json"},
            )
            if not isinstance(response.status, int) or response.status != 200:
                raise ProviderTransportError
            maximum_wire_bytes = max_output_bytes + _MAXIMUM_WIRE_OVERHEAD_BYTES
            payload = response.read(maximum_wire_bytes + 1)
            if not isinstance(payload, bytes) or len(payload) > maximum_wire_bytes:
                raise ProviderTransportError
            value = json.loads(payload)
            if not isinstance(value, dict) or not isinstance(value.get("response"), str):
                raise ProviderTransportError
            text = unicodedata.normalize("NFC", value["response"])
            if not text.strip() or len(text.encode("utf-8")) > max_output_bytes:
                raise ProviderTransportError
            return text
        except ProviderTransportError:
            raise
        except Exception:
            raise ProviderTransportError from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.close()


class _StdlibConnection:
    def __init__(self, connection: http.client.HTTPConnection) -> None:
        self._connection = connection

    def request(
        self,
        method: str,
        target: str,
        body: bytes,
        headers: dict[str, str],
    ) -> JsonModelResponse:
        self._connection.request(method, target, body=body, headers=headers)
        return self._connection.getresponse()

    def close(self) -> None:
        self._connection.close()


class _StdlibConnectionFactory:
    def open(
        self,
        *,
        scheme: str,
        hostname: str,
        port: int,
        timeout: float,
    ) -> JsonModelConnection:
        connection_type = (
            http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        )
        return _StdlibConnection(connection_type(hostname, port, timeout=timeout))


def _endpoint(value: str) -> tuple[str, str, int, str]:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValueError("invalid local provider endpoint")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("invalid local provider endpoint") from None
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path
        or parsed.path == "/"
        or not parsed.path.startswith("/")
        or "\\" in parsed.path
        or ".." in parsed.path.split("/")
    ):
        raise ValueError("invalid local provider endpoint")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        raise ValueError("invalid local provider endpoint") from None
    if not address.is_loopback:
        raise ValueError("invalid local provider endpoint")
    effective_port = port if port is not None else 443 if parsed.scheme == "https" else 80
    return parsed.scheme, hostname, effective_port, parsed.path


def _read_owner_file(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ProductionProviderConfigError("invalid private provider config")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= _MAXIMUM_CONFIG_BYTES
        ):
            raise ProductionProviderConfigError("invalid private provider config")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAXIMUM_CONFIG_BYTES + 1)
    except ProductionProviderConfigError:
        raise
    except OSError as error:
        raise ProductionProviderConfigError("invalid private provider config") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > _MAXIMUM_CONFIG_BYTES:
        raise ProductionProviderConfigError("invalid private provider config")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ProductionProviderConfigError("invalid private provider config")
        value[key] = item
    return value


__all__ = [
    "LocalProviderRuntimeConfig",
    "PrivateProviderConfig",
    "ProductionProviderConfigError",
    "ProviderComposition",
    "ProviderTransportError",
    "StdlibLocalModelTransport",
    "compose_production_provider",
    "load_private_provider_config",
]
