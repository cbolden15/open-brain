"""Appliance entry points for the Phase 3 daemon-owned appliance runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from open_brain_engine import __version__
from open_brain_engine.engine import ReadViewUnavailableError
from open_brain_engine.storage.operational import StorageError

from open_brain.cli._common import (
    ExitCode,
    adapter_failed_envelope,
    invalid_envelope,
    redacted_error,
    unavailable_envelope,
    write_envelope,
)
from open_brain.profile import ProfileError
from open_brain.services.appliance_application import ApplianceApplication
from open_brain.services.appliance_daemon import (
    MAXIMUM_CONTROL_ENVELOPE_BYTES,
    ApplianceControlProtocolError,
    ApplianceControlUnavailableError,
)
from open_brain.services.appliance_daemon import main as run_appliance_daemon
from open_brain.services.appliance_init import ApplianceInitError, initialize_appliance
from open_brain.services.appliance_lifecycle import (
    ApplianceLifecycleError,
    ApplianceUninstallReceipt,
    ApplianceUpgradeReceipt,
    ArtifactCandidate,
    OwnerLifecycleRequest,
    dispatch_phase1_command,
    read_status_via_control,
    run_supervisor_action,
)
from open_brain.services.appliance_status import read_appliance_status
from open_brain.services.appliance_supervisors import SupervisorCommandError
from open_brain.services.mcp_stdio import serve_stdio_mcp

_PHASE1_COMMANDS = frozenset({"capture", "inbox", "proposals", "query", "review", "spaces"})
_MCP_SPACE_ID = re.compile(r"space_[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class LifecycleCommandPort(Protocol):
    def upgrade(
        self,
        *,
        owner_request: OwnerLifecycleRequest | None,
        candidate: ArtifactCandidate,
        backup_destination: Path,
        disposable_root: Path,
    ) -> ApplianceUpgradeReceipt: ...

    def uninstall(
        self,
        *,
        owner_request: OwnerLifecycleRequest | None,
    ) -> ApplianceUninstallReceipt: ...


def run_cli(
    argv: tuple[str, ...] | list[str] | None = None,
    *,
    environment: Mapping[str, object] | None = None,
    lifecycle: LifecycleCommandPort | None = None,
) -> int:
    env = os.environ if environment is None else environment
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    if (
        any(not isinstance(argument, str) or "\x00" in argument for argument in arguments)
        or arguments.count("--json") > 1
        or arguments.count("--dry-run") > 1
    ):
        write_envelope(invalid_envelope(), json_output=json_output, stream=sys.stdout)
        return ExitCode.USAGE
    root_free_exit = _root_free_exit(arguments)
    if root_free_exit is not None:
        return root_free_exit
    if "--dry-run" in arguments:
        write_envelope(invalid_envelope(), json_output=json_output, stream=sys.stdout)
        return ExitCode.USAGE
    command_index = next(
        (index for index, argument in enumerate(arguments) if not argument.startswith("-")),
        None,
    )
    if command_index is None:
        write_envelope(invalid_envelope(), json_output=json_output, stream=sys.stdout)
        return ExitCode.USAGE
    command = arguments[command_index]
    command_argv = tuple(
        argument for argument in arguments[command_index + 1 :] if argument != "--json"
    )
    try:
        root = _root_from_environment(env)
    except ValueError:
        _write_json(_configuration_failure())
        return ExitCode.CONFIGURATION
    try:
        if command == "daemon":
            if command_argv:
                raise ValueError("invalid daemon arguments")
            return int(
                run_appliance_daemon(
                    ("--root", str(root)),
                    environment=_string_environment(env),
                )
            )
        if command == "init":
            receipt = initialize_appliance(root, starter_spaces=_starter_spaces(command_argv))
            _write_json(receipt.to_dict())
            return ExitCode.SUCCESS
        if command == "status":
            _write_json(_status_payload(root))
            return ExitCode.SUCCESS
        if command == "supervisor":
            _write_json(_supervisor_payload(root, command_argv))
            return ExitCode.SUCCESS
        if command in {"upgrade", "uninstall"}:
            if lifecycle is None:
                write_envelope(
                    unavailable_envelope(command),
                    json_output=json_output,
                    stream=sys.stdout,
                )
                return ExitCode.FAILURE
            lifecycle_receipt = _run_lifecycle_command(lifecycle, command, command_argv)
            _write_json(lifecycle_receipt.to_dict())
            return ExitCode.SUCCESS
        if command == "query":
            return _query(root, command_argv, json_output=json_output)
        if command in _PHASE1_COMMANDS:
            return _dispatch_controlled_phase1(
                root,
                command,
                command_argv,
                json_output=json_output,
            )
    except ApplianceInitError as error:
        _write_json(error.receipt.to_dict())
        return ExitCode.CONFIGURATION
    except ApplianceLifecycleError as error:
        _write_json(error.receipt.to_dict())
        return ExitCode.FAILURE
    except (ProfileError, ReadViewUnavailableError):
        _write_json(_configuration_failure())
        return ExitCode.CONFIGURATION
    except (
        ApplianceControlProtocolError,
        OSError,
        StorageError,
        SupervisorCommandError,
        TimeoutError,
    ):
        write_envelope(
            adapter_failed_envelope(command),
            json_output=json_output,
            stream=sys.stdout,
        )
        return ExitCode.FAILURE
    except ValueError:
        write_envelope(invalid_envelope(), json_output=json_output, stream=sys.stdout)
        return ExitCode.USAGE
    write_envelope(invalid_envelope(), json_output=json_output, stream=sys.stdout)
    return ExitCode.USAGE


def run_mcp() -> int:
    try:
        application = ApplianceApplication.open_read_only(
            _root_from_environment(os.environ),
            allowed_space_ids=_mcp_allowed_space_ids(os.environ),
        )
        serve_stdio_mcp(
            application.mcp_adapter(),
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout.buffer,
        )
    except (ProfileError, ReadViewUnavailableError, ValueError):
        return ExitCode.CONFIGURATION
    return ExitCode.SUCCESS


def run_http() -> int:
    return ExitCode.CONFIGURATION


def _root_free_exit(arguments: tuple[str, ...]) -> ExitCode | None:
    if tuple(argument for argument in arguments if argument != "--json") == ("--version",):
        print(f"open-brain {__version__}")
        return ExitCode.SUCCESS
    if "--help" in arguments or "-h" in arguments:
        try:
            _parser().parse_args(tuple(argument for argument in arguments if argument != "--json"))
        except SystemExit as error:
            return ExitCode.SUCCESS if error.code == 0 else ExitCode.USAGE
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-brain",
        description="Phase 3 appliance CLI with daemon-owned mutations and read-only MCP.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--json", action="store_true", help="Write JSON output.")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser("daemon", help="Run the appliance daemon in the foreground.")
    init_parser = subparsers.add_parser("init", help="Initialize one appliance root.")
    init_parser.add_argument(
        "--starter-space",
        action="append",
        default=[],
        help="Create one optional starter space.",
    )
    subparsers.add_parser("status", help="Read bounded appliance status.")
    subparsers.add_parser(
        "supervisor",
        help="Discover or control the local appliance daemon unit.",
    )
    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Run owner-approved artifact upgrade orchestration.",
    )
    for option in (
        "request-id",
        "requested-at",
        "candidate-id",
        "version",
        "backup-destination",
        "disposable-root",
    ):
        upgrade_parser.add_argument(f"--{option}")
    upgrade_parser.add_argument(
        "--artifact-kind",
        choices=("native-onedir", "source-checkout"),
        default="source-checkout",
    )
    upgrade_parser.add_argument("--confirm-owner", action="store_true")
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Remove source-checkout app state while preserving Brain data.",
    )
    uninstall_parser.add_argument("--request-id")
    uninstall_parser.add_argument("--requested-at")
    uninstall_parser.add_argument("--confirm-owner", action="store_true")
    for command in sorted(_PHASE1_COMMANDS):
        subparsers.add_parser(command, help=f"{command} commands")
    return parser


def _root_from_environment(environment: Mapping[str, object]) -> Path:
    root = environment.get("OPEN_BRAIN_ROOT")
    if not isinstance(root, str) or not root or "\x00" in root or not Path(root).is_absolute():
        raise ValueError("invalid OPEN_BRAIN_ROOT")
    return Path(root)


def _string_environment(environment: Mapping[str, object]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(value, str):
            raise ValueError("invalid daemon environment")
        selected[key] = value
    return selected


def _mcp_allowed_space_ids(environment: Mapping[str, object]) -> frozenset[str]:
    raw = environment.get("OPEN_BRAIN_MCP_ALLOWED_SPACE_IDS")
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 8_192:
        raise ValueError("invalid MCP allow-list")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("invalid MCP allow-list") from error
    if (
        not isinstance(value, list)
        or len(value) > 128
        or any(
            not isinstance(space_id, str) or _MCP_SPACE_ID.fullmatch(space_id) is None
            for space_id in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid MCP allow-list")
    return frozenset(value)


def _write_json(value: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n")


def _configuration_failure() -> dict[str, object]:
    return {
        "error": redacted_error("configuration_unavailable"),
        "status": "failed",
    }


def _starter_spaces(argv: tuple[str, ...]) -> tuple[str, ...]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--starter-space", action="append", default=[])
    namespace, extras = parser.parse_known_args(argv)
    if extras:
        raise ValueError("invalid init arguments")
    return tuple(namespace.starter_space)


def _status_payload(root: Path) -> dict[str, object]:
    if not _control_active(root):
        return read_appliance_status(root).to_dict()
    try:
        return read_status_via_control(root).envelope
    except ApplianceControlUnavailableError:
        return read_appliance_status(root).to_dict()


def _dispatch_controlled_phase1(
    root: Path,
    command: str,
    argv: tuple[str, ...],
    *,
    json_output: bool,
) -> int:
    if not _control_active(root):
        write_envelope(unavailable_envelope(command), json_output=json_output, stream=sys.stdout)
        return ExitCode.FAILURE
    try:
        receipt = dispatch_phase1_command(root, command=command, argv=argv)
    except ApplianceControlUnavailableError:
        write_envelope(unavailable_envelope(command), json_output=json_output, stream=sys.stdout)
        return ExitCode.FAILURE
    write_envelope(receipt.envelope, json_output=json_output, stream=sys.stdout)
    return int(receipt.exit_code)


def _query(root: Path, argv: tuple[str, ...], *, json_output: bool) -> int:
    if _control_active(root):
        try:
            receipt = dispatch_phase1_command(root, command="query", argv=argv)
            write_envelope(receipt.envelope, json_output=json_output, stream=sys.stdout)
            return int(receipt.exit_code)
        except ApplianceControlUnavailableError:
            pass
    application = ApplianceApplication.open_read_only(root)
    try:
        envelope = _offline_query_envelope(application, argv)
    except ValueError:
        write_envelope(invalid_envelope(), json_output=json_output, stream=sys.stdout)
        return ExitCode.USAGE
    if len(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")) > (
        MAXIMUM_CONTROL_ENVELOPE_BYTES
    ):
        write_envelope(
            adapter_failed_envelope("query"),
            json_output=json_output,
            stream=sys.stdout,
        )
        return ExitCode.FAILURE
    write_envelope(envelope, json_output=json_output, stream=sys.stdout)
    return ExitCode.SUCCESS


def _control_active(root: Path) -> bool:
    try:
        metadata = (root / ".open-brain" / "run" / "control.sock").lstat()
    except OSError:
        return False
    return stat.S_ISSOCK(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _supervisor_payload(root: Path, argv: tuple[str, ...]) -> dict[str, object]:
    if len(argv) != 1:
        raise ValueError("invalid supervisor arguments")
    return run_supervisor_action(root, action=argv[0])


def _run_lifecycle_command(
    lifecycle: LifecycleCommandPort,
    command: str,
    argv: tuple[str, ...],
) -> ApplianceUpgradeReceipt | ApplianceUninstallReceipt:
    positional, options, flags = _request(argv)
    if positional or flags != {"confirm-owner"}:
        raise ValueError("explicit owner lifecycle request is required")
    if command == "uninstall":
        if set(options) != {"request-id", "requested-at"}:
            raise ValueError("invalid uninstall request")
        return lifecycle.uninstall(
            owner_request=OwnerLifecycleRequest(
                request_id=options["request-id"],
                requested_at=options["requested-at"],
            )
        )
    required_options = {
        "backup-destination",
        "candidate-id",
        "disposable-root",
        "request-id",
        "requested-at",
        "version",
    }
    if command != "upgrade" or set(options) not in {
        frozenset(required_options),
        frozenset((*required_options, "artifact-kind")),
    }:
        raise ValueError("invalid upgrade request")
    return lifecycle.upgrade(
        owner_request=OwnerLifecycleRequest(
            request_id=options["request-id"],
            requested_at=options["requested-at"],
        ),
        candidate=ArtifactCandidate(
            candidate_id=options["candidate-id"],
            version=options["version"],
            artifact_kind=options.get("artifact-kind", "source-checkout"),
        ),
        backup_destination=Path(options["backup-destination"]),
        disposable_root=Path(options["disposable-root"]),
    )


def _offline_query_envelope(
    application: ApplianceApplication,
    argv: tuple[str, ...],
) -> dict[str, object]:
    positional, options, flags = _request(argv)
    if len(positional) != 1 or flags or set(options) - {"space", "family", "type", "limit"}:
        raise ValueError("invalid query arguments")
    results = application.retrieval.search(
        positional[0],
        space_id=options.get("space"),
        payload_family=options.get("family"),
        record_type=options.get("type"),
        limit=int(options.get("limit", "10")),
    )
    return {
        "command": "query",
        "results": [
            {
                "capture_id": result.capture_id,
                "excerpt": result.excerpt,
                "explanation": result.explanation,
                "payload_family": result.payload_family,
                "provenance": result.provenance.as_dict(),
                "record_type": result.record_type,
                "result_id": result.result_id,
                "space_id": result.space_id,
                "title": result.title,
                "trust": result.trust,
            }
            for result in results
        ],
        "status": "ok",
    }


def _request(
    argv: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, str], frozenset[str]]:
    if not isinstance(argv, tuple) or any(
        not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv
    ):
        raise ValueError("invalid Phase 1 request")
    positional: list[str] = []
    options: dict[str, str] = {}
    flags: set[str] = set()
    for argument in argv:
        if argument == "--json":
            continue
        if argument.startswith("--"):
            key, marker, value = argument[2:].partition("=")
            if marker:
                if not key or not value or key in options or key in flags:
                    raise ValueError("invalid Phase 1 request")
                options[key] = value
            else:
                if not key or key in flags or key in options:
                    raise ValueError("invalid Phase 1 request")
                flags.add(key)
            continue
        if argument.startswith("-"):
            raise ValueError("invalid Phase 1 request")
        positional.append(argument)
    return tuple(positional), options, frozenset(flags)
