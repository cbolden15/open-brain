"""Package-boundary contract for the optional collector."""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass

__all__ = [
    "COLLECTOR_BOUNDARY",
    "CollectorBoundary",
    "CollectorBoundaryError",
    "assert_collector_boundary",
]


class CollectorBoundaryError(RuntimeError):
    """The optional collector package crossed its initial D3 boundary."""


@dataclass(frozen=True, slots=True)
class CollectorBoundary:
    """Static contract for the opt-in D3 collector package boundary."""

    package_name: str
    module_name: str
    cli_name: str
    dependency_packages: tuple[str, ...]
    forbidden_runtime_modules: tuple[str, ...]
    service_install_enabled: bool
    unattended_enabled_by_default: bool
    network_enabled_by_default: bool

    def validate(self) -> None:
        if self.package_name != "open-brain-collector":
            raise CollectorBoundaryError("collector package name is invalid")
        if self.module_name != "open_brain_collector":
            raise CollectorBoundaryError("collector module name is invalid")
        if self.cli_name != "open-brain-collector":
            raise CollectorBoundaryError("collector cli name is invalid")
        if self.dependency_packages != ("open-brain-connectors", "open-brain-engine"):
            raise CollectorBoundaryError("collector dependency closure is invalid")
        if not self.service_install_enabled:
            raise CollectorBoundaryError("collector service install is not exposed")
        if self.unattended_enabled_by_default or self.network_enabled_by_default:
            raise CollectorBoundaryError("collector boundary is not inert")

    def distribution_dependencies(self) -> tuple[str, ...]:
        metadata = importlib.metadata.metadata(self.package_name)
        dependencies = metadata.get_all("Requires-Dist") or []
        return tuple(sorted(_distribution_name(dependency) for dependency in dependencies))


COLLECTOR_BOUNDARY = CollectorBoundary(
    package_name="open-brain-collector",
    module_name="open_brain_collector",
    cli_name="open-brain-collector",
    dependency_packages=("open-brain-connectors", "open-brain-engine"),
    forbidden_runtime_modules=(
        "argon2",
        "cryptography",
        "docker",
        "http.server",
        "keyring",
        "open_brain",
        "podman",
        "prctl",
        "socketserver",
        "sqlcipher3",
        "starlette",
        "systemd",
        "uvicorn",
    ),
    service_install_enabled=True,
    unattended_enabled_by_default=False,
    network_enabled_by_default=False,
)


def assert_collector_boundary() -> CollectorBoundary:
    """Return the collector boundary after checking its static contract."""

    COLLECTOR_BOUNDARY.validate()
    return COLLECTOR_BOUNDARY


def _distribution_name(dependency: str) -> str:
    return (
        dependency.split(";", 1)[0]
        .split("[", 1)[0]
        .split("=", 1)[0]
        .split("<", 1)[0]
        .split(">", 1)[0]
        .split("~", 1)[0]
        .strip()
    )
