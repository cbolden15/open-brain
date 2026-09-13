"""Public Open Brain CLI composition scaffold."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from open_brain_engine import __version__

from open_brain_legacy._compat.open_brain.cli._common import (
    CommandAdapterLookup,
    ExitCode,
    adapter_failed_envelope,
    invalid_envelope,
    unavailable_envelope,
    validate_adapter_envelope,
    write_envelope,
)
from open_brain_legacy.cli._registry import (
    command_spec,
    parser_commands,
    scheduled_route_spec,
)
from open_brain_legacy.cli.scheduled import (
    ScheduledApplicationAdapters,
    ScheduledDispatchResult,
    UnavailableScheduledAdapters,
    dispatch_scheduled_route,
    write_scheduled_result,
)


def build_parser() -> argparse.ArgumentParser:
    """Build a deterministic parser exposing public command families only."""
    parser = argparse.ArgumentParser(
        prog="open-brain",
        description="Local-first capture, provenance, review, and knowledge pipeline.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("--json", action="store_true", help="Write a JSON envelope.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Declare a non-mutating requested execution.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for command in parser_commands():
        subparsers.add_parser(command.name, help=command.summary, description=command.summary)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    command_adapters: CommandAdapterLookup | None = None,
    scheduled_adapters: ScheduledApplicationAdapters | None = None,
    scheduled_job_id: str | None = None,
) -> ExitCode:
    """Select injected adapters without loading services or configuration."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return ExitCode.SUCCESS

    json_output = "--json" in arguments
    malformed = (
        any(not isinstance(argument, str) or "\x00" in argument for argument in arguments)
        or arguments.count("--json") > 1
        or arguments.count("--dry-run") > 1
    )
    if malformed:
        write_envelope(invalid_envelope(), json_output=json_output, stream=sys.stdout)
        return ExitCode.USAGE
    if "--help" in arguments or "-h" in arguments:
        build_parser().print_help()
        return ExitCode.SUCCESS
    if arguments == ("--version",):
        print(f"open-brain {__version__}")
        return ExitCode.SUCCESS

    scheduled_route = scheduled_route_spec(arguments, job_id=scheduled_job_id)
    interactive_family_override = (
        scheduled_job_id is None
        and scheduled_route is not None
        and scheduled_route.path[0] in {"doctor", "retention"}
        and command_adapters is not None
        and command_adapters.get(scheduled_route.path[0]) is not None
    )
    if scheduled_route is not None and not interactive_family_override:
        adapters = (
            UnavailableScheduledAdapters()
            if scheduled_adapters is None
            else scheduled_adapters
        )
        try:
            scheduled_result = dispatch_scheduled_route(scheduled_route, adapters)
        except Exception:
            scheduled_result = ScheduledDispatchResult.failed(scheduled_route.job_id)
        write_scheduled_result(
            scheduled_result,
            json_output="--json" in arguments,
            stream=sys.stdout,
        )
        return ExitCode(scheduled_result.exit_code)

    dry_run = "--dry-run" in arguments
    command_index: int | None = None
    command_name: str | None = None
    invalid = False
    for index, argument in enumerate(arguments):
        if command_index is not None:
            break
        if argument == "--json":
            json_output = True
        elif argument == "--dry-run":
            dry_run = True
        elif argument.startswith("-"):
            invalid = True
        else:
            command_index = index
            command_name = argument

    command = command_spec(command_name) if command_name is not None else None
    if invalid or command is None:
        write_envelope(invalid_envelope(), json_output=json_output, stream=sys.stdout)
        return ExitCode.USAGE

    if command_index is None:
        write_envelope(invalid_envelope(), json_output=json_output, stream=sys.stdout)
        return ExitCode.USAGE
    remaining = arguments[command_index + 1 :]
    adapter = command_adapters.get(command.name) if command_adapters is not None else None
    if adapter is not None:
        adapter_argv = remaining
        if dry_run and "--dry-run" not in adapter_argv:
            adapter_argv = (*adapter_argv, "--dry-run")
        try:
            adapter_result = adapter.dispatch(adapter_argv)
            result_exit_code = adapter_result.exit_code
            result_envelope = adapter_result.envelope
            if isinstance(result_exit_code, bool):
                raise ValueError("invalid command adapter result")
            exit_code = ExitCode(result_exit_code)
            envelope = validate_adapter_envelope(
                command.name,
                result_envelope,
                argv=adapter_argv,
            )
            write_envelope(envelope, json_output=json_output, stream=sys.stdout)
        except Exception:
            write_envelope(
                adapter_failed_envelope(command.name),
                json_output=json_output,
                stream=sys.stdout,
            )
            return ExitCode.FAILURE
        return exit_code

    write_envelope(
        unavailable_envelope(command.name),
        json_output=json_output,
        stream=sys.stdout,
    )
    return ExitCode.FAILURE
