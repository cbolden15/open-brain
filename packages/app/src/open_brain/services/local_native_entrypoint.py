"""Frozen entry point for the default, daemonless Open Brain artifact."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import PurePath

from open_brain_engine import __version__

_SELF_CHECK_COMMAND = "__open-brain-self-check"


def run_cli(arguments: tuple[str, ...]) -> int:
    """Import the full CLI only for a command that needs it."""
    from open_brain.services.local_entrypoints import run_cli as run_local_cli

    return int(run_local_cli(arguments))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local CLI, with one state-free installer self-check."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments == (_SELF_CHECK_COMMAND,):
        return _self_check()
    if arguments == ("--version",):
        # The parser's version action prints exactly this; skip loading the engine for it.
        print(f"open-brain {__version__}")
        return 0
    return int(run_cli(arguments))


def _self_check() -> int:
    frozen = bool(getattr(sys, "frozen", False))
    valid = frozen and _runtime_metadata_is_relocated() and _compiled_decimal_is_available()
    payload = {
        "daemon_running": False,
        "frozen": frozen,
        "profile": "local",
        "status": "ok" if valid else "failed",
        "version": __version__,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if valid else 1



def _compiled_decimal_is_available() -> bool:
    """The native closure requires working C decimal instead of its Python fallback."""
    try:
        import _decimal
        from decimal import Decimal

        return (
            Decimal is _decimal.Decimal
            and Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
        )
    except ImportError, ArithmeticError, AttributeError, TypeError:
        return False


def _runtime_metadata_is_relocated() -> bool:
    import ctypes
    import sysconfig

    try:
        variables = sysconfig.get_config_vars()
        roots = (sys.prefix, sys.exec_prefix)
        return (
            variables["SIZEOF_VOID_P"] == ctypes.sizeof(ctypes.c_void_p)
            and isinstance(variables["SOABI"], str)
            and bool(variables["SOABI"])
            and all(
                isinstance(variables[key], str)
                and any(PurePath(variables[key]).is_relative_to(root) for root in roots)
                for key in ("BINDIR", "LIBDIR")
            )
        )
    except ImportError, AttributeError, KeyError, TypeError, ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
