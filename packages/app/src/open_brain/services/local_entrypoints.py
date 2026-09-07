"""Direct local CLI for the default Open Brain product."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping

from open_brain_engine import __version__

from open_brain.local_data import FilesystemTypeProbe, select_local_root
from open_brain.services.local_bootstrap import initialize_local_brain


def run_cli(
    argv: tuple[str, ...] | list[str] | None = None,
    *,
    environment: Mapping[str, object] | None = None,
    platform_name: str | None = None,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
) -> int:
    """Run one daemonless local command without loading Secure Node composition."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        parsed = _parser().parse_args(arguments)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    if parsed.command is None:
        _parser().print_help(sys.stderr)
        return 2
    selected_environment = os.environ if environment is None else environment
    try:
        selection = select_local_root(
            data_dir=getattr(parsed, "data_dir", None),
            environment=selected_environment,
            platform_name=platform_name,
        )
        receipt = initialize_local_brain(
            selection,
            filesystem_type_probe=filesystem_type_probe,
        )
    except Exception:
        _write_failure(json_output=bool(getattr(parsed, "json", False)))
        return 78
    if bool(getattr(parsed, "json", False)):
        print(json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":")))
    else:
        print(
            f"Open Brain {receipt.status}. Profile: local. Storage: SQLite. "
            "Application encryption: false."
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-brain",
        description="Private, daemonless local Brain.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_local_options(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    init_parser = subparsers.add_parser(
        "init", help="Create or reopen one private local Brain."
    )
    _add_local_options(init_parser)
    return parser


def _add_local_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write JSON output.",
    )
    parser.add_argument(
        "--data-dir",
        default=argparse.SUPPRESS,
        help="Use this absolute Brain root instead of the platform-local default.",
    )


def _write_failure(*, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "private_data_directory_unavailable",
                        "message": "Open Brain could not use the private data directory.",
                    },
                    "status": "failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        print("Open Brain could not use the private data directory.", file=sys.stderr)


__all__ = ["run_cli"]
