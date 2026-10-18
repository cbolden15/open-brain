"""CLI start-up imports only what the command needs (T5.5 warm budget)."""

from __future__ import annotations

import json
import subprocess
import sys

from open_brain_engine import __version__

_VERSION_LINE = f"open-brain {__version__}\n"

# Measured on the search path: none of these is needed to build the parser or search.
_SEARCH_UNUSED = (
    "email",
    "http.client",
    "importlib.metadata",
    "open_brain.services.embedding_health",
    "open_brain.services.managed_providers",
    "ssl",
)


def _loaded(code: str) -> dict[str, object]:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and inline probe
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return dict(json.loads(completed.stdout.splitlines()[-1]))


def test_version_imports_no_engine() -> None:
    result = _loaded(
        "import contextlib,io,json,sys\n"
        "from open_brain.services.local_native_entrypoint import main\n"
        "out = io.StringIO()\n"
        "with contextlib.redirect_stdout(out):\n"
        "    code = main(('--version',))\n"
        "print(json.dumps({'code': code, 'out': out.getvalue(),"
        " 'engine': 'open_brain_engine.engine' in sys.modules,"
        " 'cli': 'open_brain.services.local_entrypoints' in sys.modules}))"
    )
    assert result == {"code": 0, "out": _VERSION_LINE, "engine": False, "cli": False}


def test_version_output_matches_the_parser() -> None:
    result = _loaded(
        "import contextlib,io,json\n"
        "from open_brain.services.local_entrypoints import run_cli\n"
        "out = io.StringIO()\n"
        "with contextlib.redirect_stdout(out):\n"
        "    code = run_cli(('--version',))\n"
        "print(json.dumps({'code': code, 'out': out.getvalue()}))"
    )
    assert result == {"code": 0, "out": _VERSION_LINE}


def test_parser_construction_skips_search_unused_modules() -> None:
    result = _loaded(
        "import json,sys\n"
        "from open_brain.services.local_entrypoints import _parser\n"
        "_parser()\n"
        f"print(json.dumps({{name: name in sys.modules for name in {_SEARCH_UNUSED!r}}}))"
    )
    assert result == dict.fromkeys(_SEARCH_UNUSED, False)
