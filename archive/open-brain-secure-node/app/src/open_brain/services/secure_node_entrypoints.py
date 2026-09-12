"""Lazy entry points for the opt-in Secure Node profile."""

from __future__ import annotations

import importlib.metadata
import sys
from collections.abc import Mapping

_UNAVAILABLE = "Secure Node is not installed. Install open-brain[secure-node]."
_REQUIRED_DISTRIBUTIONS = (
    "argon2-cffi",
    "cryptography",
    "keyring",
    "sqlcipher3",
    "starlette",
    "uvicorn",
)


def run_cli(
    argv: tuple[str, ...] | list[str] | None = None,
    *,
    environment: Mapping[str, object] | None = None,
) -> int:
    """Run the retained appliance CLI only when the Secure Node extra is present."""
    if not _secure_node_dependencies_available():
        print(_UNAVAILABLE, file=sys.stderr)
        return 2
    from open_brain.services.appliance_entrypoints import run_cli as run_appliance_cli

    return int(run_appliance_cli(argv, environment=environment))


def run_mcp() -> int:
    """Run the retained Secure Node MCP path only when its dependencies are present."""
    if not _secure_node_dependencies_available():
        print(_UNAVAILABLE, file=sys.stderr)
        return 2
    from open_brain.services.appliance_entrypoints import run_mcp as run_appliance_mcp

    return int(run_appliance_mcp())


def _secure_node_dependencies_available() -> bool:
    required = list(_REQUIRED_DISTRIBUTIONS)
    if sys.platform == "darwin":
        required.append("pyobjc-framework-LocalAuthentication")
    try:
        for distribution in required:
            importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


__all__ = ["run_cli", "run_mcp"]
