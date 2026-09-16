"""Headless access to the same source operations used by the desktop."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from open_brain_collector.control import control_endpoint, control_request
from open_brain_collector.live_manager import LiveSourceManager
from open_brain_collector.service import CollectorLaunchdServiceManager
from open_brain_connectors.runtime.live_common import LiveSourceError


def _arguments_file(path: str) -> dict[str, object]:
    with Path(path).open("rb") as handle:
        raw = handle.read(65_537)
    if len(raw) > 65_536:
        raise LiveSourceError("source_invalid_arguments")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise LiveSourceError("source_invalid_arguments")
    return cast(dict[str, object], value)


def background_service(state: Path, brain: Path, *, enable: bool) -> dict[str, object]:
    if sys.platform != "darwin":
        raise LiveSourceError("collector_service_setup_required")
    endpoint = control_endpoint(state, brain)
    suffix = endpoint.parent.name.removeprefix("open-brain-collector-")
    label, owner = f"open-brain.collector.{suffix}", f"collector-owned:{suffix}"
    manager = CollectorLaunchdServiceManager(plist_dir=Path.home() / "Library/LaunchAgents")
    if not enable:
        return _stop_background(manager, label, owner)
    # Validate the existing Brain before installing an opt-in user service.
    LiveSourceManager(state.parent / "live", brain)
    command = (
        sys.executable,
        "-I",
        "-m",
        "open_brain_collector.cli",
        "run",
        "--state",
        str(state),
        "--brain-root",
        str(brain),
        "--lease",
        str(state.parent / "collector.lock"),
        "--control",
        "--background",
        "--poll-seconds",
        "1",
    )
    result = manager.install(label=label, owner_token=owner, command=command, keep_alive=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            status = control_request(state, brain, "sources.status", {}, timeout_seconds=1)
            collector = status.get("collector")
            if isinstance(collector, dict) and collector.get("background") is True:
                return {"status": "enabled", "service": result}
        except LiveSourceError:
            pass
        time.sleep(0.1)
    try:
        _stop_background(manager, label, owner)
    except Exception:
        raise LiveSourceError("collector_service_cleanup_unconfirmed") from None
    raise LiveSourceError("collector_service_start_failed")


def _stop_background(
    manager: CollectorLaunchdServiceManager, label: str, owner: str
) -> dict[str, object]:
    removed = manager.remove_owned(label=label, owner_token=owner)
    deadline = time.monotonic() + 5
    while manager.status(label) == "loaded":
        if time.monotonic() >= deadline:
            raise LiveSourceError("collector_service_stop_unconfirmed")
        time.sleep(0.05)
    return {**removed, "status": "not_loaded"}


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="open-brain-collector sources")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--brain-root", required=True, type=Path)
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="operate directly without a running collector (scheduling stays opt-in)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("endpoint")
    commands.add_parser("background-enable")
    commands.add_parser("background-disable")
    for name in ("accounts", "connect", "disconnect", "resources"):
        item = commands.add_parser(name)
        item.add_argument("--provider", required=True, choices=("gmail", "google_drive", "slack"))
        if name == "connect":
            item.add_argument("--client-config", required=True)
        if name in {"disconnect", "resources"}:
            item.add_argument("--connection-id", required=True)
        if name == "resources":
            item.add_argument("--cursor")
    for name in ("configure", "session-preview"):
        commands.add_parser(name).add_argument("--arguments-file", required=True)
    for name in ("preview", "import", "control"):
        item = commands.add_parser(name)
        item.add_argument("--source-id", required=True)
        if name == "import":
            item.add_argument("--preview-id", required=True)
        if name == "control":
            item.add_argument(
                "--action",
                required=True,
                choices=("enable", "pause", "resume", "disable", "schedule", "sync_now"),
            )
            item.add_argument("--interval-seconds", type=int)
    commands.add_parser("session-apply").add_argument("--preview-id", required=True)
    request = commands.add_parser("request")
    request.add_argument("operation")
    request.add_argument("--arguments-file", required=True)
    args = parser.parse_args(argv)
    try:
        if not args.state.is_absolute() or not args.brain_root.is_absolute():
            raise LiveSourceError("collector_invalid_path")
        if args.command == "endpoint":
            result: dict[str, object] = {
                "socket": str(control_endpoint(args.state, args.brain_root))
            }
        elif args.command.startswith("background-"):
            result = background_service(
                args.state, args.brain_root, enable=args.command == "background-enable"
            )
        else:
            values = vars(args)
            operation = (
                args.operation
                if args.command == "request"
                else "sources." + args.command.replace("-", "_")
            )
            arguments = (
                _arguments_file(args.arguments_file)
                if "arguments_file" in values
                else {
                    key: value
                    for key, value in values.items()
                    if key not in {"state", "brain_root", "foreground", "command"}
                    and value is not None
                }
            )
            result = (
                LiveSourceManager(args.state.parent / "live", args.brain_root).dispatch(
                    operation, arguments
                )
                if args.foreground
                else control_request(args.state, args.brain_root, operation, arguments)
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "outcome": "failed",
                    "failure_code": error.code
                    if isinstance(error, LiveSourceError)
                    else "source_operation_failed",
                }
            )
        )
        return 78
