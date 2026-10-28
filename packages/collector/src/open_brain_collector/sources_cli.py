"""Headless access to the same source operations used by the desktop."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn, cast

from open_brain_collector.control import control_endpoint, control_request
from open_brain_collector.live_manager import LiveSourceManager
from open_brain_collector.service import CollectorLaunchdServiceManager
from open_brain_collector.source_metadata import read_source_metadata
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


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


def _leaf_command(argv: Sequence[str]) -> str | None:
    """Return the subcommand token using the same option arity as the main parser."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"--state", "--brain-root", "--guide-guard-fd"}:
            index += 2
        elif token == "--foreground" or token.startswith(
            ("--state=", "--brain-root=", "--guide-guard-fd=")
        ):
            index += 1
        else:
            return token
    return None


def _guide_selection_file(path: str) -> SourceResourceSelection:
    """Read one strict owner-private Slack selection without following links."""
    location = Path(path)
    if not location.is_absolute():
        raise LiveSourceError("source_invalid_arguments")
    try:
        parent = os.open(location.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise LiveSourceError("source_invalid_arguments") from None
    try:
        directory = os.fstat(parent)
        if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077:
            raise LiveSourceError("source_invalid_arguments")
        try:
            fd = os.open(
                location.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        except OSError:
            raise LiveSourceError("source_invalid_arguments") from None
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise LiveSourceError("source_invalid_arguments")
        raw = handle.read(65_537)
    if len(raw) > 65_536:
        raise LiveSourceError("source_invalid_arguments")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError
        return value

    def finite(_value: str) -> object:
        raise ValueError

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=finite)
        if (
            not isinstance(value, dict)
            or set(value) != {"connector_name", "connection_id", "resource_id", "resource_type"}
            or any(type(item) is not str for item in value.values())
            or value["connector_name"] != "slack"
        ):
            raise ValueError
        return SourceResourceSelection(**value)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise LiveSourceError("source_invalid_arguments") from None


def _guide_metadata(argv: Sequence[str]) -> int:
    """Read-only guide health; dispatched before any manager, IPC or engine bootstrap."""
    # The frozen argv form is exact: no abbreviations and no --flag=value spellings.
    if any(token.startswith(("--state=", "--brain-root=")) for token in argv):
        return _guide_failure("source_invalid_arguments")
    parser = _StrictParser(prog="open-brain-collector sources", add_help=False, allow_abbrev=False)
    parser.add_argument("--state", required=True)
    parser.add_argument("--brain-root", required=True)
    parser.add_argument("--foreground", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    leaf = commands.add_parser("guide-metadata", add_help=False, allow_abbrev=False)
    locator = leaf.add_mutually_exclusive_group(required=True)
    locator.add_argument("--source-id")
    locator.add_argument("--selection-file")
    try:
        args = parser.parse_args(argv)
        state, brain = Path(args.state), Path(args.brain_root)
        if (
            not args.foreground
            or argv.count("--state") != 1
            or argv.count("--brain-root") != 1
            or not state.is_absolute()
            or not brain.is_absolute()
        ):
            raise LiveSourceError("source_invalid_arguments")
        try:
            report = read_source_metadata(
                live_root=state.parent / "live",
                brain_root=brain,
                source_id=args.source_id,
                selection=(
                    None
                    if args.selection_file is None
                    else _guide_selection_file(args.selection_file)
                ),
            )
        except ValueError:
            raise LiveSourceError("source_invalid_arguments") from None
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 0
    except Exception as error:
        return _guide_failure(
            error.code if isinstance(error, LiveSourceError) else "source_operation_failed"
        )


def _guide_failure(code: str) -> int:
    print(json.dumps({"outcome": "failed", "failure_code": code}))
    return 78


class _StrictParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise LiveSourceError("source_invalid_arguments")


def _validate_guard(fd: int) -> None:
    """Keep an inherited guide lifetime guard; legacy callers never pass one."""
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise LiveSourceError("collector_guard_invalid")
        # The shared open file description already holds, or now takes, the
        # exclusive guard; it stays held until this process exits.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise LiveSourceError("collector_guard_invalid") from None


def main(argv: Sequence[str]) -> int:
    if _leaf_command(argv) == "guide-metadata":
        return _guide_metadata(argv)
    parser = argparse.ArgumentParser(prog="open-brain-collector sources")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--brain-root", required=True, type=Path)
    parser.add_argument("--guide-guard-fd", type=int)
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="operate directly without a running collector (scheduling stays opt-in)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    health = commands.add_parser("health")
    health.add_argument("--interval-seconds", type=int)
    health.add_argument("--grace-seconds", type=int)
    for name in ("capture-status", "capture-process", "capture-candidates", "capture-review"):
        commands.add_parser(name)
    commands.add_parser("capture-setup").add_argument("--arguments-file", required=True)
    candidate_decide = commands.add_parser("capture-candidate-decide")
    candidate_decide.add_argument("--candidate-id", required=True)
    candidate_decide.add_argument(
        "--action", required=True, choices=("map", "create", "reject", "defer")
    )
    candidate_decide.add_argument("--value-id")
    candidate_decide.add_argument("--deferred-until")
    candidate_retry = commands.add_parser("capture-candidate-retry")
    candidate_retry.add_argument("--candidate-id", required=True)
    candidate_retry.add_argument("--replan", action="store_true")
    defer = commands.add_parser("capture-review-defer")
    defer.add_argument("--proposal-id", required=True)
    defer.add_argument("--until", required=True)
    commands.add_parser("capture-review-dismiss").add_argument("--proposal-id", required=True)
    commands.add_parser("capture-discover").add_argument("--connection-id", required=True)
    track = commands.add_parser("capture-track-thread")
    track.add_argument("--connection-id", required=True)
    track.add_argument("--channel-id", required=True)
    track.add_argument("--thread-ts", required=True)
    track.add_argument(
        "--privacy-tier", choices=("public", "work", "personal", "secret", "unknown")
    )
    custody_status = commands.add_parser("custody-status")
    custody_status.add_argument("--source-id")
    for name in ("custody-inspect", "custody-retry"):
        commands.add_parser(name).add_argument("--receipt-id", required=True)
    commands.add_parser("endpoint")
    commands.add_parser("background-enable")
    commands.add_parser("background-disable")
    for name in ("accounts", "connect", "disconnect", "resources"):
        item = commands.add_parser(name)
        item.add_argument("--provider", required=True, choices=("gmail", "google_drive", "slack"))
        if name == "connect":
            item.add_argument("--client-config", required=True)
            item.add_argument(
                "--capability",
                action="append",
                dest="capabilities",
                choices=("owner_messages", "mentions", "dm", "group_dm"),
            )
        if name in {"disconnect", "resources"}:
            item.add_argument("--connection-id", required=True)
        if name == "resources":
            item.add_argument("--cursor")
            item.add_argument(
                "--resource-type",
                choices=("drive_file", "drive_folder"),
                help="kind of Drive resource to list (google_drive only; default drive_file)",
            )
    for name in ("configure", "session-preview"):
        commands.add_parser(name).add_argument("--arguments-file", required=True)
    slack_setup = commands.add_parser("slack-policy-setup")
    slack_setup.add_argument("--connection-id", required=True)
    slack_setup.add_argument("--keyword", action="append", dest="keywords")
    slack_setup.add_argument("--allow-channel", action="append", dest="allowlist")
    slack_setup.add_argument("--lookback-hours", type=int)
    slack_setup.add_argument("--activity-weight", type=int)
    slack_setup.add_argument("--keyword-weight", type=int)
    slack_setup.add_argument("--threshold", type=int)
    slack_setup.add_argument("--proposal-opt-in", action="store_true")
    slack_setup.add_argument("--proposal-opt-out", action="store_true")
    slack_setup.add_argument("--continue-without-keywords", action="store_true")
    mapping_add = commands.add_parser("slack-mapping-add")
    mapping_add.add_argument("--connection-id", required=True)
    mapping_add.add_argument("--channel-id", required=True)
    mapping_add.add_argument("--page-id", required=True)
    mapping_add.add_argument("--keyword")
    mapping_list = commands.add_parser("slack-mapping-list")
    mapping_list.add_argument("--connection-id", required=True)
    mapping_remove = commands.add_parser("slack-mapping-remove")
    mapping_remove.add_argument("--connection-id", required=True)
    mapping_remove.add_argument("--mapping-id", required=True)
    suggestions = commands.add_parser("slack-suggestions")
    suggestions.add_argument("--connection-id", required=True)
    for name in ("slack-suggestion-approve", "slack-suggestion-dismiss"):
        item = commands.add_parser(name)
        item.add_argument("--connection-id", required=True)
        item.add_argument("--channel-id", required=True)
    commands.add_parser("slack-status")
    discover = commands.add_parser("slack-discover")
    discover.add_argument("--connection-id", required=True)
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
    saved_item = commands.add_parser("saved-item-select")
    saved_item.add_argument("--folder-path", required=True)
    saved_item.add_argument(
        "--privacy-tier", choices=("public", "work", "personal", "secret"), default="personal"
    )
    request = commands.add_parser("request")
    request.add_argument("operation")
    request.add_argument("--arguments-file", required=True)
    args = parser.parse_args(argv)
    if args.command == "slack-policy-setup":
        if args.proposal_opt_in and args.proposal_opt_out:
            parser.error("--proposal-opt-in and --proposal-opt-out are mutually exclusive")
        if args.keywords is None and not args.continue_without_keywords:
            parser.error("provide --keyword or explicitly pass --continue-without-keywords")
    try:
        if not args.state.is_absolute() or not args.brain_root.is_absolute():
            raise LiveSourceError("collector_invalid_path")
        if args.guide_guard_fd is not None:
            _validate_guard(args.guide_guard_fd)
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
                else _command_arguments(args)
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


def _command_arguments(args: argparse.Namespace) -> dict[str, object]:
    values = vars(args)
    ignored = {
        "state",
        "brain_root",
        "guide_guard_fd",
        "foreground",
        "command",
        "arguments_file",
        "continue_without_keywords",
        "lookback_hours",
        "proposal_opt_in",
        "proposal_opt_out",
    }
    result = {
        key: value for key, value in values.items() if key not in ignored and value is not None
    }
    if (
        args.command == "resources"
        and args.provider == "google_drive"
        and result.get("resource_type") is None
    ):
        result["resource_type"] = "drive_file"
    if args.command == "slack-policy-setup":
        if args.keywords is None and args.continue_without_keywords:
            result["keywords"] = []
        if args.lookback_hours is not None:
            result["lookback_seconds"] = args.lookback_hours * 3_600
        if args.proposal_opt_in:
            result["proposal_opt_in"] = True
        elif args.proposal_opt_out:
            result["proposal_opt_in"] = False
    return cast(dict[str, object], result)
