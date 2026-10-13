"""Entry point for the optional fast, foreground-only harness distribution."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from open_brain.services.harness_cli import add_harness_arguments, run_harness_cli


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="open-brain-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    add_harness_arguments(commands.add_parser("harness"))
    return run_harness_cli(parser.parse_args(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
