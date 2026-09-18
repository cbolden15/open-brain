"""Command-line entry point for the optional collector package boundary."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from time import time
from typing import cast

from open_brain_collector.boundary import assert_collector_boundary
from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorStateStore,
    CollectorStorageError,
    EngineCaptureSink,
)
from open_brain_collector.runner import (
    CollectorProcessRunner,
    DispatchingSourceRuntime,
    FixtureSourceRuntime,
    collector_capture_sink,
)
from open_brain_collector.service import CollectorLaunchdServiceManager, CollectorServiceError
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.github import GitHubSourceAdapter
from open_brain_connectors.runtime.live_common import LiveSourceError

__all__ = ["main", "run_cli"]


def main(argv: Sequence[str] | None = None) -> int:
    selected_argv = list(sys.argv[1:] if argv is None else argv)
    if selected_argv and selected_argv[0] == "sources":
        from open_brain_collector.sources_cli import main as sources_main

        return sources_main(selected_argv[1:])
    parser = argparse.ArgumentParser(prog="open-brain-collector")
    parser.add_argument(
        "--boundary-json",
        action="store_true",
        help="print the inert collector package-boundary contract",
    )
    subparsers = parser.add_subparsers(dest="command")
    run_once = subparsers.add_parser("run-once", help="run due sources once")
    run_once.add_argument("--state", required=True)
    run_once.add_argument("--brain-root", required=True)
    run_once.add_argument("--lease", required=True)
    run_once.add_argument("--fixture-runtime-json")
    run_once.add_argument("--source-runtime-dir")
    run_once.add_argument("--credential-dir")
    run_once.add_argument("--source-id")
    run_once.add_argument("--force", action="store_true")
    run_loop = subparsers.add_parser("run", help="run due sources in a bounded loop")
    run_loop.add_argument("--state", required=True)
    run_loop.add_argument("--brain-root", required=True)
    run_loop.add_argument("--lease", required=True)
    run_loop.add_argument("--fixture-runtime-json")
    run_loop.add_argument("--source-runtime-dir")
    run_loop.add_argument("--credential-dir")
    run_loop.add_argument("--poll-seconds", type=float, default=60.0)
    run_loop.add_argument("--max-iterations", type=int)
    run_loop.add_argument("--control", action="store_true")
    run_loop.add_argument("--background", action="store_true")
    lifecycle = subparsers.add_parser("source", help="manage explicit collector source state")
    lifecycle.add_argument("--state", required=True)
    lifecycle.add_argument("--brain-root")
    lifecycle_subparsers = lifecycle.add_subparsers(dest="source_command", required=True)
    enable = lifecycle_subparsers.add_parser("enable")
    enable.add_argument("--source-id", required=True)
    enable.add_argument("--connection-id", required=True)
    enable.add_argument("--owner", required=True)
    enable.add_argument("--repository", required=True)
    enable.add_argument("--credential-ref")
    enable.add_argument("--interval-seconds", required=True, type=int)
    for command in ("pause", "resume", "disable", "status", "sync-now"):
        item = lifecycle_subparsers.add_parser(command)
        item.add_argument("--source-id", required=True)
    schedule = lifecycle_subparsers.add_parser("schedule")
    schedule.add_argument("--source-id", required=True)
    schedule.add_argument("--interval-seconds", required=True, type=int)
    custody_status = lifecycle_subparsers.add_parser("custody-status")
    custody_status.add_argument("--source-id")
    custody_inspect = lifecycle_subparsers.add_parser("custody-inspect")
    custody_inspect.add_argument("--receipt-id", required=True)
    custody_retry = lifecycle_subparsers.add_parser("custody-retry")
    custody_retry.add_argument("--receipt-id", required=True)
    service = subparsers.add_parser("service", help="manage opt-in collector launchd service")
    service.add_argument("--plist-dir", required=True)
    service.add_argument("--label", required=True)
    service.add_argument("--owner-token", required=True)
    service_subparsers = service.add_subparsers(dest="service_command", required=True)
    install = service_subparsers.add_parser("install")
    install.add_argument("collector_command", nargs=argparse.REMAINDER)
    service_subparsers.add_parser("status")
    service_subparsers.add_parser("remove")
    args = parser.parse_args(argv)
    boundary = assert_collector_boundary()
    if args.boundary_json:
        print(
            json.dumps(
                {
                    "cli": boundary.cli_name,
                    "dependencies": list(boundary.dependency_packages),
                    "module": boundary.module_name,
                    "network_enabled_by_default": boundary.network_enabled_by_default,
                    "package": boundary.package_name,
                    "service_install_enabled": boundary.service_install_enabled,
                    "unattended_enabled_by_default": boundary.unattended_enabled_by_default,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "source":
        command = cast(str, args.source_command)
        try:
            controller = CollectorController(
                CollectorStateStore(Path(cast(str, args.state))),
                clock=lambda: int(time()),
                brain_root=(
                    Path(cast(str, args.brain_root)) if args.brain_root is not None else None
                ),
            )
            if command == "enable":
                selection = GitHubSourceAdapter().repository_selection(
                    connection_id=cast(str, args.connection_id),
                    owner=cast(str, args.owner),
                    repository=cast(str, args.repository),
                )
                command_result = controller.enable(
                    source_id=cast(str, args.source_id),
                    selection=selection,
                    credential_ref=cast(str | None, args.credential_ref),
                    interval_seconds=cast(int, args.interval_seconds),
                )
            elif command == "pause":
                command_result = controller.pause(cast(str, args.source_id))
            elif command == "resume":
                command_result = controller.resume(cast(str, args.source_id))
            elif command == "disable":
                command_result = controller.disable(cast(str, args.source_id))
            elif command == "sync-now":
                command_result = controller.sync_now(cast(str, args.source_id))
            elif command == "schedule":
                command_result = controller.schedule(
                    cast(str, args.source_id),
                    cast(int, args.interval_seconds),
                )
            elif command == "custody-status":
                result = controller.custody_status(cast(str | None, args.source_id))
                print(json.dumps(result, sort_keys=True))
                return 0
            elif command == "custody-inspect":
                result = controller.custody_inspect(cast(str, args.receipt_id))
                print(json.dumps(result, sort_keys=True))
                return 0
            elif command == "custody-retry":
                if args.brain_root is None:
                    raise LiveSourceError("source_brain_unavailable")
                result = controller.retry(
                    cast(str, args.receipt_id),
                    EngineCaptureSink(collector_capture_sink(Path(cast(str, args.brain_root)))),
                )
                print(json.dumps(result, sort_keys=True))
                return 0
            else:
                command_result = controller.status(cast(str, args.source_id))
        except (CollectorStorageError, ConnectorContractError, LiveSourceError) as error:
            print(
                json.dumps(
                    {
                        "failure_code": (
                            "storage_unavailable"
                            if isinstance(error, CollectorStorageError)
                            else error.code
                            if isinstance(error, LiveSourceError)
                            else str(error)
                        ),
                        "outcome": "failed",
                        "schema_version": 1,
                        "source_id": getattr(args, "source_id", None),
                    },
                    sort_keys=True,
                )
            )
            return 78
        except Exception:
            print(
                json.dumps(
                    {
                        "failure_code": "source_operation_failed",
                        "outcome": "failed",
                        "schema_version": 1,
                        "source_id": getattr(args, "source_id", None),
                    },
                    sort_keys=True,
                )
            )
            return 78
        print(json.dumps(command_result.to_dict(), sort_keys=True))
        return 0
    if args.command in {"run-once", "run"}:
        fixture_runtime = cast(str | None, args.fixture_runtime_json)
        source_runtime_dir = cast(str | None, args.source_runtime_dir)
        credential_dir = cast(str | None, args.credential_dir)
        runtime = (
            FixtureSourceRuntime(Path(fixture_runtime))
            if fixture_runtime is not None
            else DispatchingSourceRuntime(
                state_path=Path(cast(str, args.state)),
                local_runtime_root=(
                    Path(source_runtime_dir)
                    if source_runtime_dir is not None
                    else Path(cast(str, args.state)).parent / "runtime"
                ),
                credential_dir=Path(credential_dir) if credential_dir is not None else None,
            )
        )
        runner = CollectorProcessRunner(
            state_path=Path(cast(str, args.state)),
            brain_root=Path(cast(str, args.brain_root)),
            lease_path=Path(cast(str, args.lease)),
            runtime=runtime,
            control_enabled=bool(getattr(args, "control", False)),
            background=bool(getattr(args, "background", False)),
        )
        try:
            if args.command == "run-once":
                run_result = runner.run_once(
                    source_id=cast(str | None, args.source_id),
                    force=cast(bool, args.force),
                )
            else:
                run_result = runner.run_loop(
                    interval_seconds=cast(float, args.poll_seconds),
                    max_iterations=cast(int | None, args.max_iterations),
                )
        except (CollectorStorageError, ConnectorContractError, LiveSourceError) as error:
            print(
                json.dumps(
                    {
                        "failure_code": (
                            "storage_unavailable"
                            if isinstance(error, CollectorStorageError)
                            else str(error)
                        ),
                        "outcome": "failed",
                        "results": [],
                    },
                    sort_keys=True,
                )
            )
            return 78
        print(json.dumps(run_result, sort_keys=True))
        return 0
    if args.command == "service":
        manager = CollectorLaunchdServiceManager(plist_dir=Path(cast(str, args.plist_dir)))
        service_command = cast(str, args.service_command)
        label = cast(str, args.label)
        owner_token = cast(str, args.owner_token)
        try:
            if service_command == "install":
                collector_command = tuple(cast(list[str], args.collector_command))
                service_result = manager.install(
                    label=label,
                    owner_token=owner_token,
                    command=collector_command,
                )
            elif service_command == "remove":
                service_result = manager.remove_owned(label=label, owner_token=owner_token)
            else:
                service_result = {
                    "label": label,
                    "schema_version": 1,
                    "status": manager.status(label),
                }
        except (CollectorServiceError, ConnectorContractError) as error:
            print(
                json.dumps(
                    {
                        "failure_code": str(error),
                        "label": label,
                        "outcome": "failed",
                        "schema_version": 1,
                    },
                    sort_keys=True,
                )
            )
            return 78
        print(json.dumps(service_result, sort_keys=True))
        return 0
    parser.print_help()
    return 0


def run_cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run_cli()
