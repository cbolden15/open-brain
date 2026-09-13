"""Compatibility delegates for the Phase 3 appliance entry points."""

from __future__ import annotations

from collections.abc import Mapping

from .appliance_entrypoints import run_cli as appliance_run_cli
from .appliance_entrypoints import run_http as appliance_run_http
from .appliance_entrypoints import run_mcp as appliance_run_mcp
from .runtime import (
    RESERVED_APPLIANCE_CLI_ENTRYPOINT,
    RESERVED_APPLIANCE_HTTP_ENTRYPOINT,
    RESERVED_APPLIANCE_MCP_ENTRYPOINT,
)


def reserved_appliance_entrypoints() -> tuple[str, str, str]:
    return (
        RESERVED_APPLIANCE_CLI_ENTRYPOINT,
        RESERVED_APPLIANCE_HTTP_ENTRYPOINT,
        RESERVED_APPLIANCE_MCP_ENTRYPOINT,
    )


def run_cli(
    argv: tuple[str, ...] | list[str] | None = None,
    *,
    environment: Mapping[str, object] | None = None,
) -> int:
    return appliance_run_cli(argv, environment=environment)


def run_mcp() -> int:
    return appliance_run_mcp()


def run_http() -> int:
    return appliance_run_http()


__all__ = ["reserved_appliance_entrypoints", "run_cli", "run_http", "run_mcp"]
