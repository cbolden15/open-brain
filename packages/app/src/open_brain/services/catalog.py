"""Public, read-only capability catalog for the installed core application."""

from __future__ import annotations

import argparse
import importlib.metadata
import platform
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from open_brain_engine import __version__
from open_brain_engine.engine import PHASE1_STATE_SCHEMA_VERSION

from open_brain.services.local_runtime_session import RUNTIME_SESSION_VERSION
from open_brain.services.obsidian_plugin import (
    ObsidianPluginFailure,
    discover_obsidian_plugin_assets,
    load_obsidian_plugin_bundle,
)

CATALOG_SCHEMA_VERSION = 2
_MAX_METADATA_ITEMS = 64
_MAX_METADATA_TEXT = 256
_MAX_PUBLIC_VERSION = 64
_PUBLIC_VERSION = re.compile(
    r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){0,3}"
    r"(?:(?:a|b|rc)[0-9]{1,8})?(?:\.post[0-9]{1,8})?(?:\.dev[0-9]{1,8})?"
)
_METADATA_READ_ERRORS = (OSError, UnicodeError)
_METADATA_PROPERTY_ERRORS = (*_METADATA_READ_ERRORS, AttributeError, TypeError, ValueError)
SUPPORTED_ARTIFACT_PLATFORMS = ("linux-x86_64", "macos-arm64")
OPTIONAL_SOURCE_DESCRIPTORS = (
    ("agent_session", "agent sessions"),
    ("calendar", "calendars"),
    ("confluence", "Confluence"),
    ("github", "GitHub"),
    ("gitlab", "GitLab"),
    ("gmail", "Gmail"),
    ("google_drive", "Google Drive"),
    ("imessage", "iMessage"),
    ("jira", "Jira"),
    ("local_document", "local documents"),
    ("meeting_transcript", "meeting transcripts"),
    ("microsoft_mail", "Microsoft 365 Mail"),
    ("notion", "Notion"),
    ("slack", "Slack"),
    ("web_clip", "web clips"),
)
DEFERRED_CAPABILITIES = (
    "connected_source_continuity",
    "desktop_release",
    "google_oauth_provider_readiness",
    "linux_service_lifecycle",
    "local_pdf_docx_public_integration",
    "markdown_root_rebind",
    "retire_correct_selective_forget",
    "semantic_recall",
    "slack_public_integration",
)


class CatalogRequestError(ValueError):
    """A catalog request did not match the strict schema-2 request DTO."""


DistributionLookup = Callable[[str], importlib.metadata.Distribution]


def require_catalog_request(arguments: Mapping[str, object]) -> None:
    """Accept exactly one schema-2 request shape."""
    version = arguments.get("schema_version")
    if set(arguments) != {"schema_version"} or type(version) is not int or version != 2:
        raise CatalogRequestError("unsupported catalog request")


def cli_registrations(parser: argparse.ArgumentParser) -> tuple[dict[str, object], ...]:
    """Return deterministic leaf commands from the parser's registered subparsers."""
    leaves: list[dict[str, object]] = []

    def visit(current: argparse.ArgumentParser, words: tuple[str, ...]) -> None:
        if current.get_default("_catalog_discoverable") is False:
            return
        subparser_actions = [
            action for action in current._actions if isinstance(action, argparse._SubParsersAction)
        ]
        if not subparser_actions:
            choices: list[tuple[str, tuple[object, ...]]] = []
            for action in current._actions:
                if (
                    action.choices is not None
                    and not action.option_strings
                    and not isinstance(action, argparse._SubParsersAction)
                ):
                    choices.append((action.dest, tuple(sorted(action.choices))))
            if not choices:
                leaves.append({"command": list(words), "positional_choices": {}})
                return
            expanded: list[tuple[tuple[str, ...], dict[str, object]]] = [(words, {})]
            for destination, values in choices:
                expanded = [
                    ((*command, str(value)), {**selected, destination: value})
                    for command, selected in expanded
                    for value in values
                ]
            leaves.extend(
                {"command": list(command), "positional_choices": selected}
                for command, selected in expanded
            )
            return
        for subparsers in subparser_actions:
            for name, child in sorted(subparsers.choices.items()):
                visit(child, (*words, name))

    visit(parser, ())
    return tuple(leaves)


def build_catalog(
    request: Mapping[str, object],
    *,
    cli_commands: Sequence[Mapping[str, object]],
    mcp_registered: Sequence[Mapping[str, object]],
    mcp_authorized: Iterable[str] | None,
    mcp_discovery_context: str,
    bridge_base: Sequence[str],
    bridge_negotiated: Sequence[str],
    bridge_optional: Sequence[str],
    bridge_available: Iterable[str] | None,
    distribution_lookup: DistributionLookup | None = None,
    entry_points: Sequence[importlib.metadata.EntryPoint] | None = None,
    base_executable: Path | None = None,
    platform_name: str | None = None,
    machine: str | None = None,
) -> dict[str, object]:
    """Build deterministic JSON-safe public metadata without opening a Brain."""
    require_catalog_request(request)
    lookup = importlib.metadata.distribution if distribution_lookup is None else distribution_lookup
    installed = tuple(
        _package(name, required, scripts, dependencies, lookup)
        for name, required, scripts, dependencies in (
            (
                "open-brain",
                "0.1.0",
                {"open-brain": "open_brain.services.local_entrypoints:run_cli"},
                {"open-brain-engine": "==0.1.0"},
            ),
            ("open-brain-engine", "0.1.0", {}, {"rfc8785": ">=0.1.4,<0.2"}),
            (
                "open-brain-connectors",
                "0.1.0",
                {
                    "open-brain-source": "open_brain_connectors.runtime.source_cli:run_cli",
                    "open-brain-outbox": "open_brain_connectors.outbox.cli:run_cli",
                },
                {"open-brain-engine": "==0.1.0", "pypdf": "==6.18.1"},
            ),
            (
                "open-brain-collector",
                "0.1.0",
                {"open-brain-collector": "open_brain_collector.cli:run_cli"},
                {"open-brain-connectors": "==0.1.0", "open-brain-engine": "==0.1.0"},
            ),
        )
    )
    youtube_registered, extension_metadata_status = _youtube_registration(entry_points)
    current_platform = _artifact_platform(
        platform_name or sys.platform, machine or platform.machine()
    )
    assets = _obsidian_assets(base_executable)
    authorized = None if mcp_authorized is None else sorted(set(mcp_authorized))
    available = None if bridge_available is None else sorted(set(bridge_available))
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "product": {
            "name": "open-brain",
            "version": __version__,
            "public_acceptance": "not_certified",
        },
        "compatibility": {
            "bridge_protocol": 1,
            "catalog_schema": CATALOG_SCHEMA_VERSION,
            "portable_metadata": 5,
            "runtime_session": RUNTIME_SESSION_VERSION,
            "state_schema": PHASE1_STATE_SCHEMA_VERSION,
            "task_contract": "t03.v1",
        },
        "platforms": {
            "declared_artifacts": list(SUPPORTED_ARTIFACT_PLATFORMS),
            "current_observation": current_platform,
            "current_supported": current_platform in SUPPORTED_ARTIFACT_PLATFORMS,
        },
        "packages": list(installed),
        "surfaces": {
            "cli": {
                "acceptance": "developer_accepted",
                "implementation": "implemented",
                "scope": "core",
                "discovery_context": "installed_parser_registration",
                "commands": [dict(command) for command in cli_commands],
                "authorization": "not_a_session",
                "lifecycle": "one_shot",
                "readiness": "varies_by_command_unassessed",
            },
            "mcp": {
                "acceptance": "developer_accepted",
                "implementation": "implemented",
                "scope": "core",
                "discovery_context": mcp_discovery_context,
                "registered": [dict(tool) for tool in mcp_registered],
                "authorized_tools": authorized,
                "readiness": "unassessed",
                "transport": "stdio",
                "lifecycle": "foreground_session",
            },
            "plugin_bridge": {
                "acceptance": "developer_accepted",
                "implementation": "implemented",
                "scope": "core",
                "discovery_context": "dispatch_registries",
                "base_operations": sorted(bridge_base),
                "negotiated_operations": sorted(bridge_negotiated),
                "optional_collector_operations": sorted(bridge_optional),
                "available_in_context": available,
                "authorization": "unassessed",
                "ui_affordance_proven": False,
                "lifecycle": "foreground_session",
                "readiness": "unassessed",
            },
            "obsidian": {
                "implementation": "implemented",
                "scope": "core",
                "authorization": "unassessed",
                "readiness": (
                    "unassessed" if assets["packaged_assets"] == "available" else "unavailable"
                ),
                **assets,
            },
        },
        "optional_sources": [
            {
                "acceptance": "source_only",
                "authorization": "unassessed",
                "implementation": "implemented",
                "name": name,
                "readiness": "unassessed",
                "scope": "source_only",
                "title": title,
            }
            for name, title in OPTIONAL_SOURCE_DESCRIPTORS
        ],
        "optional_connector_extensions": {
            "acceptance": "source_only",
            "discovery_context": "installed_entry_point_metadata",
            "implementation": "implemented",
            "name": "youtube",
            "registered": youtube_registered,
            "metadata_status": extension_metadata_status,
            "scope": "source_only",
        },
        "capability_notes": {
            "collector_recovery": {
                "acceptance": "developer_accepted",
                "lifecycle": "optional_scheduled_collector",
                "readiness": "unassessed",
                "scope": "source_only",
            },
            "direct_provider_graph_refresh": {
                "acceptance": "source_only",
                "authorization": "unassessed",
                "implementation": "implemented",
                "readiness": "unassessed",
                "semantic_recall": False,
            },
            "graphify_structural_refresh": {
                "implementation": "implemented",
                "readiness": "unassessed",
            },
        },
        "deferred": [
            {
                "acceptance": "source_only",
                "implementation": "planned",
                "name": name,
                "authorization": "unassessed",
                "readiness": "unavailable",
                "scope": "deferred",
            }
            for name in DEFERRED_CAPABILITIES
        ],
        "acceptance": {
            "trusted_certifications": [],
            "evidence": [
                {
                    "path": (
                        "docs/ai/workstreams/20260918-open-brain-public-m3-"
                        "source-continuity-owner-control-c63538/T09-CHECKPOINT.json"
                    ),
                    "scope": "historical_source_verification",
                },
                {
                    "path": (
                        "docs/ai/workstreams/20260917-open-brain-public-m2-0fd6e9/"
                        "M2-CHECKPOINT.json"
                    ),
                    "scope": "historical_artifact_verification",
                },
            ],
        },
    }


def _package(
    name: str,
    required_version: str,
    scripts: Mapping[str, str],
    required_dependencies: Mapping[str, str],
    lookup: DistributionLookup,
) -> dict[str, object]:
    try:
        distribution = lookup(name)
    except importlib.metadata.PackageNotFoundError:
        return _package_observation(
            name,
            required_version,
            scripts,
            required_dependencies,
            installed=False,
            metadata_status="missing",
            readiness="unavailable",
        )
    except _METADATA_READ_ERRORS:
        return _package_observation(
            name,
            required_version,
            scripts,
            required_dependencies,
            installed="unknown",
            metadata_status="unreadable",
            readiness="unassessed",
        )

    try:
        raw_version = distribution.version
    except _METADATA_READ_ERRORS:
        raw_version = None
        version_status = "unreadable"
    except AttributeError, TypeError, ValueError:
        raw_version = None
        version_status = "invalid"
    else:
        version_status = "observed" if _public_version(raw_version) is not None else "invalid"
    version = _public_version(raw_version)

    try:
        registered = _registered_scripts(distribution.entry_points, scripts)
    except _METADATA_READ_ERRORS:
        registered = {script: False for script in scripts}
        entry_point_status = "unreadable"
    except AttributeError, TypeError, ValueError:
        registered = {script: False for script in scripts}
        entry_point_status = "invalid"
    else:
        entry_point_status = "observed"

    try:
        observed_requirements = _requirements(distribution.requires)
    except _METADATA_READ_ERRORS:
        observed_requirements = None
        requirements_status = "unreadable"
    except AttributeError, TypeError, ValueError:
        observed_requirements = None
        requirements_status = "invalid"
    else:
        requirements_status = "observed"
    declared_dependencies_match = observed_requirements is not None and all(
        _declared_requirement_matches(dependency, constraint, observed_requirements)
        for dependency, constraint in required_dependencies.items()
    )
    dependency_compatible = all(
        _installed_version_matches(dependency, constraint, lookup)
        for dependency, constraint in required_dependencies.items()
    )
    return {
        **_package_identity(name),
        "declared_dependencies_match": declared_dependencies_match,
        "dependency_compatible": dependency_compatible,
        "entry_points": registered,
        "entry_point_metadata_status": entry_point_status,
        "executable_available": "unassessed",
        "installed": True,
        "metadata_status": (
            "observed"
            if {version_status, entry_point_status, requirements_status} == {"observed"}
            else "incomplete"
        ),
        "name": name,
        "observed_version": version,
        "required_dependencies": _required_dependencies(required_dependencies),
        "required_version": required_version,
        "requirements_metadata_status": requirements_status,
        "version_compatible": version == required_version,
        "version_metadata_status": version_status,
        "authorization": "unassessed",
        "readiness": "unassessed",
    }


def _package_observation(
    name: str,
    required_version: str,
    scripts: Mapping[str, str],
    required_dependencies: Mapping[str, str],
    *,
    installed: bool | str,
    metadata_status: str,
    readiness: str,
) -> dict[str, object]:
    return {
        **_package_identity(name),
        "declared_dependencies_match": False,
        "dependency_compatible": False,
        "entry_points": {script: False for script in scripts},
        "entry_point_metadata_status": metadata_status,
        "executable_available": "unassessed",
        "installed": installed,
        "metadata_status": metadata_status,
        "name": name,
        "observed_version": None,
        "required_dependencies": _required_dependencies(required_dependencies),
        "required_version": required_version,
        "requirements_metadata_status": metadata_status,
        "version_compatible": False,
        "version_metadata_status": metadata_status,
        "authorization": "unassessed",
        "readiness": readiness,
    }


def _package_identity(name: str) -> dict[str, str]:
    core = name in {"open-brain", "open-brain-engine"}
    return {
        "acceptance": "developer_accepted" if core else "source_only",
        "scope": "core" if core else "source_only",
    }


def _required_dependencies(required: Mapping[str, str]) -> list[str]:
    return [name + constraint for name, constraint in sorted(required.items())]


def _public_version(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_PUBLIC_VERSION:
        return None
    return value if _PUBLIC_VERSION.fullmatch(value) is not None else None


def _registered_scripts(raw_entry_points: object, scripts: Mapping[str, str]) -> dict[str, bool]:
    if not isinstance(raw_entry_points, Sequence) or isinstance(
        raw_entry_points, (str, bytes, bytearray)
    ):
        raise TypeError("invalid entry-point metadata")
    if len(raw_entry_points) > _MAX_METADATA_ITEMS:
        raise ValueError("invalid entry-point metadata")
    result = {script: False for script in scripts}
    for entry in raw_entry_points:
        group = entry.group
        entry_name = entry.name
        value = entry.value
        if not all(
            isinstance(item, str) and len(item) <= _MAX_METADATA_TEXT
            for item in (group, entry_name, value)
        ):
            raise ValueError("invalid entry-point metadata")
        if group == "console_scripts" and scripts.get(entry_name) == value:
            result[entry_name] = True
    return result


def _requirements(raw_requirements: object) -> set[str]:
    if raw_requirements is None:
        return set()
    if not isinstance(raw_requirements, Sequence) or isinstance(
        raw_requirements, (str, bytes, bytearray)
    ):
        raise TypeError("invalid requirement metadata")
    if len(raw_requirements) > _MAX_METADATA_ITEMS:
        raise ValueError("invalid requirement metadata")
    normalized: set[str] = set()
    for requirement in raw_requirements:
        if not isinstance(requirement, str) or not 1 <= len(requirement) <= _MAX_METADATA_TEXT:
            raise ValueError("invalid requirement metadata")
        if not requirement.isascii() or any(character.isspace() for character in requirement):
            raise ValueError("invalid requirement metadata")
        normalized.add(requirement)
    return normalized


def _youtube_registration(
    supplied: Sequence[importlib.metadata.EntryPoint] | None,
) -> tuple[bool, str]:
    try:
        raw = importlib.metadata.entry_points() if supplied is None else supplied
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise TypeError("invalid entry-point metadata")
        if len(raw) > _MAX_METADATA_ITEMS * 16:
            raise ValueError("invalid entry-point metadata")
        for entry in raw:
            group = entry.group
            entry_name = entry.name
            value = entry.value
            if not all(
                isinstance(item, str) and len(item) <= _MAX_METADATA_TEXT
                for item in (group, entry_name, value)
            ):
                raise ValueError("invalid entry-point metadata")
            if (
                group == "open_brain.connectors.v1"
                and entry_name == "youtube"
                and value == "open_brain_connectors.conformance:connector"
            ):
                return True, "observed"
    except _METADATA_READ_ERRORS:
        return False, "unreadable"
    except AttributeError, TypeError, ValueError:
        return False, "invalid"
    return False, "observed"


def _declared_requirement_matches(name: str, constraint: str, observed: set[str]) -> bool:
    expected_parts = set(constraint.split(","))
    for requirement in observed:
        if requirement.startswith(name):
            return set(requirement.removeprefix(name).split(",")) == expected_parts
    return False


def _installed_version_matches(name: str, constraint: str, lookup: DistributionLookup) -> bool:
    try:
        raw_version = lookup(name).version
    except (importlib.metadata.PackageNotFoundError, *_METADATA_PROPERTY_ERRORS):
        return False
    version = _public_version(raw_version)
    if version is None:
        return False
    if constraint.startswith("=="):
        return version == constraint.removeprefix("==")
    if constraint == ">=0.1.4,<0.2":
        match = re.fullmatch(r"0\.1\.(\d+)", version)
        return match is not None and int(match.group(1)) >= 4
    return False


def _obsidian_assets(base_executable: Path | None) -> dict[str, object]:
    try:
        bundle = load_obsidian_plugin_bundle(discover_obsidian_plugin_assets(base_executable))
    except ObsidianPluginFailure:
        return {
            "acceptance": "source_only",
            "enabled": "unassessed",
            "packaged_assets": "unavailable",
            "version": None,
        }
    return {
        "acceptance": "source_only",
        "enabled": "unassessed",
        "packaged_assets": "available",
        "version": bundle.version,
    }


def _artifact_platform(platform_name: str, machine: str) -> str:
    normalized_machine = machine.lower()
    if platform_name == "darwin" and normalized_machine in {"arm64", "aarch64"}:
        return "macos-arm64"
    if platform_name.startswith("linux") and normalized_machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    return "unknown"
