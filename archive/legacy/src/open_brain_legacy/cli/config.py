"""Secret-free configuration and registry serializers for public CLI callers."""

from __future__ import annotations

import json
from dataclasses import dataclass

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
from open_brain_legacy._compat.open_brain.config import AppConfig
from open_brain_legacy.cli._registry import COMMANDS, CommandSpec


@dataclass(frozen=True, slots=True)
class ConfigCliResult:
    """A deterministic envelope containing allow-listed metadata only."""

    exit_code: ExitCode
    envelope: dict[str, object]

    def to_json(self) -> str:
        """Serialize metadata with stable key ordering."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


def show_config(*, config: AppConfig) -> ConfigCliResult:
    """Serialize non-secret config metadata without exposing configured roots."""
    if not isinstance(config, AppConfig):
        return _failed("config", "configuration_unavailable")
    return ConfigCliResult(
        exit_code=ExitCode.SUCCESS,
        envelope={
            "cloud_enabled": config.cloud_enabled,
            "command": "config",
            "egress_enabled": config.egress_enabled,
            "ledger_route_count": len(config.ledger.taxonomy.routes),
            "provider": config.provider,
            "status": "ok",
        },
    )


def show_registry(
    *,
    commands: tuple[CommandSpec, ...] = COMMANDS,
    degraded: bool = False,
) -> ConfigCliResult:
    """Serialize normalized command names and fixed public policy metadata."""
    if (
        not isinstance(commands, tuple)
        or any(not isinstance(command, CommandSpec) for command in commands)
        or type(degraded) is not bool
    ):
        return _failed("registry", "registry_unavailable")
    return ConfigCliResult(
        exit_code=ExitCode.FAILURE if degraded else ExitCode.SUCCESS,
        envelope={
            "command": "registry",
            "commands": sorted(command.name for command in commands),
            "policy": {
                "configuration": "explicit-only",
                "network": "disabled",
                "output": "redacted",
            },
            "status": "degraded" if degraded else "ok",
        },
    )


def _failed(command: str, code: str) -> ConfigCliResult:
    return ConfigCliResult(
        exit_code=ExitCode.FAILURE,
        envelope={
            "command": command,
            "error": redacted_error(code),
            "status": "failed",
        },
    )
