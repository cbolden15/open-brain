"""Frozen entry point for the default, daemonless Open Brain artifact."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from open_brain_engine import __version__

from open_brain.services.local_entrypoints import run_cli

_SELF_CHECK_COMMAND = "__open-brain-self-check"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local CLI, with one state-free installer self-check."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments == (_SELF_CHECK_COMMAND,):
        return _self_check()
    return int(run_cli(arguments))


def _self_check() -> int:
    frozen = bool(getattr(sys, "frozen", False))
    payload = {
        "daemon_running": False,
        "frozen": frozen,
        "profile": "local",
        "status": "ok" if frozen else "failed",
        "version": __version__,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if frozen else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
