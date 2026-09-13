"""Deterministic public command registry for the CLI composition root."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from open_brain_legacy._compat.open_brain.cli._common import CommandFamilyAdapter
from open_brain_legacy.operations.catalog import get_job


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A command family that will later be wired to an application service."""

    name: str
    summary: str


ScheduledAdapterKind = Literal["capture", "writer", "optional"]


@dataclass(frozen=True, slots=True)
class ScheduledRouteSpec:
    """One exact scheduled argv route and its typed application adapter family."""

    job_id: str
    path: tuple[str, ...]
    options: frozenset[str]
    adapter: ScheduledAdapterKind


COMMANDS = tuple(
    sorted(
        (
            CommandSpec("capture", "Capture content for review."),
            CommandSpec("config", "Show safe configuration metadata."),
            CommandSpec("cron", "Report scheduled application status."),
            CommandSpec("digest", "Render a redacted digest."),
            CommandSpec("doctor", "Run deterministic diagnostic checks."),
            CommandSpec("explain", "Explain the no-network policy."),
            CommandSpec("inbox", "List durable captures awaiting owner action."),
            CommandSpec("ledger", "Manage the public ledger lifecycle."),
            CommandSpec("migrate", "Run a safe content migration."),
            CommandSpec("okf", "Check or export the Open Knowledge Format."),
            CommandSpec("proposals", "List or resolve review proposals."),
            CommandSpec("query", "Query work-scoped knowledge."),
            CommandSpec("registry", "Show public operator registry metadata."),
            CommandSpec("retention", "Preview or apply retention policy."),
            CommandSpec("review", "Review captured content."),
            CommandSpec("share", "Ingest a shared capture."),
            CommandSpec("social", "Operate optional social-learning compatibility."),
            CommandSpec("spaces", "Create, rename, list, or route user-defined spaces."),
            CommandSpec("status", "Show metadata-only status."),
        ),
        key=lambda command: command.name,
    )
)


@dataclass(frozen=True, slots=True)
class CommandAdapterRegistry:
    """Immutable dependency-injected adapters keyed by public command family."""

    adapters: Mapping[str, CommandFamilyAdapter]

    def __post_init__(self) -> None:
        registered = {command.name for command in COMMANDS}
        if any(name not in registered for name in self.adapters):
            raise ValueError("adapter registered for unknown command family")
        object.__setattr__(self, "adapters", MappingProxyType(dict(self.adapters)))

    def get(self, name: str) -> CommandFamilyAdapter | None:
        """Select one adapter without loading configuration or runtime services."""
        return self.adapters.get(name)


def _scheduled_route(job_id: str, adapter: ScheduledAdapterKind) -> ScheduledRouteSpec:
    arguments = get_job(job_id).command[1:]
    return ScheduledRouteSpec(
        job_id=job_id,
        path=tuple(argument for argument in arguments if not argument.startswith("-")),
        options=frozenset(argument for argument in arguments if argument.startswith("--")),
        adapter=adapter,
    )


SCHEDULED_ROUTES = (
    _scheduled_route("JOB-001", "optional"),
    _scheduled_route("JOB-002", "optional"),
    _scheduled_route("JOB-003", "optional"),
    _scheduled_route("JOB-004", "optional"),
    _scheduled_route("JOB-005", "capture"),
    _scheduled_route("JOB-006", "writer"),
    _scheduled_route("JOB-007", "writer"),
    _scheduled_route("JOB-008", "writer"),
    _scheduled_route("JOB-009", "writer"),
    _scheduled_route("JOB-010", "writer"),
    _scheduled_route("JOB-011", "writer"),
    _scheduled_route("JOB-012", "writer"),
    _scheduled_route("JOB-013", "optional"),
    _scheduled_route("JOB-014", "writer"),
    _scheduled_route("JOB-015", "writer"),
    _scheduled_route("JOB-016", "writer"),
    _scheduled_route("JOB-017", "optional"),
    _scheduled_route("JOB-018", "optional"),
    _scheduled_route("JOB-019", "optional"),
    _scheduled_route("JOB-020", "optional"),
    _scheduled_route("JOB-021", "optional"),
    _scheduled_route("JOB-022", "writer"),
    _scheduled_route("JOB-023", "writer"),
    _scheduled_route("JOB-024", "optional"),
    _scheduled_route("JOB-025", "writer"),
    _scheduled_route("JOB-026", "optional"),
    _scheduled_route("JOB-027", "capture"),
    _scheduled_route("JOB-028", "capture"),
    _scheduled_route("JOB-029", "capture"),
    _scheduled_route("JOB-030", "optional"),
)


def command_names() -> tuple[str, ...]:
    """Return deferred command-family names in stable lexical order."""
    return tuple(command.name for command in COMMANDS)


def command_spec(name: str) -> CommandSpec | None:
    """Find a registered command without loading configuration or services."""
    return next((command for command in COMMANDS if command.name == name), None)


def parser_commands() -> tuple[CommandSpec, ...]:
    """Return every top-level family exposed by the public parser."""
    commands = {command.name: command for command in COMMANDS}
    for route in SCHEDULED_ROUTES:
        family = route.path[0]
        commands.setdefault(family, CommandSpec(family, "Run a scheduled application."))
    return tuple(sorted(commands.values(), key=lambda command: command.name))


def scheduled_route_spec(
    arguments: tuple[str, ...],
    *,
    job_id: str | None = None,
) -> ScheduledRouteSpec | None:
    """Resolve a complete scheduled argv independently of option ordering."""
    path: list[str] = []
    options: set[str] = set()
    for argument in arguments:
        if argument.startswith("--"):
            if argument in options:
                return None
            options.add(argument)
        elif argument.startswith("-"):
            return None
        else:
            path.append(argument)
    matches = tuple(
        route
        for route in SCHEDULED_ROUTES
        if route.path == tuple(path) and route.options == frozenset(options)
    )
    if job_id is None:
        return matches[0] if matches else None
    return next((route for route in matches if route.job_id == job_id), None)
