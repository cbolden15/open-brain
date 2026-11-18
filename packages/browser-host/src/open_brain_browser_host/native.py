"""Run a single Chrome native message with bounded foreground lifetime."""

from __future__ import annotations

import signal
import sqlite3
import struct
import sys
from types import FrameType
from typing import BinaryIO

from open_brain_engine.engine.t03_contracts import T03Error

from open_brain_connectors.runtime.saved_item import SavedItemRefusal

from . import setup
from .operations import VERSION, Request, dispatch
from .policy import Refused, encoded, extension_origin, load, strict_json

_HEADER = struct.Struct("=I")
_FRAME_LIMIT = 256 * 1024
_FOREGROUND_SECONDS = 10
_PERMISSION_CODES = {"browser_unbound", "browser_operation_denied"}
_OPERATION_CODES = {"browser_operation_conflict", "browser_pending_limit"}


class _Deadline(BaseException):
    pass


def _expire(_signum: int, _frame: FrameType | None) -> None:
    raise _Deadline


def _read(source: BinaryIO) -> object:
    header = source.read(_HEADER.size)
    if len(header) != _HEADER.size:
        raise Refused("invalid_frame")
    (size,) = _HEADER.unpack(header)
    if size > _FRAME_LIMIT:
        raise Refused("frame_too_large")
    body = source.read(size)
    if len(body) != size:
        raise Refused("invalid_frame")
    try:
        return strict_json(body)
    except ValueError, RecursionError:
        raise Refused("invalid_frame") from None


def _invoke(argv: list[str]) -> dict[str, object]:
    policy = load()
    if argv != [extension_origin(policy.extension_id)]:
        raise Refused("origin_denied")
    return dispatch(policy, Request.parse(_read(sys.stdin.buffer)))


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in setup.OWNER_COMMANDS | {"--help", "-h"}:
        return setup.main(argv)
    response: dict[str, object] = {"version": VERSION}
    signal.signal(signal.SIGALRM, _expire)
    signal.setitimer(signal.ITIMER_REAL, _FOREGROUND_SECONDS)
    try:
        response["result"] = _invoke(argv)
    except _Deadline:
        response["error"] = {"code": "deadline_exceeded"}
    except Refused as error:
        response["error"] = {"code": str(error)}
    except PermissionError as error:
        code = str(error)
        response["error"] = {"code": code if code in _PERMISSION_CODES else "browser_unbound"}
    except SavedItemRefusal as error:
        response["error"] = {"code": error.code}
    except ValueError as error:
        code = str(error)
        response["error"] = {"code": code if code in _OPERATION_CODES else "host_unavailable"}
    except OSError, RuntimeError, sqlite3.Error, T03Error:
        response["error"] = {"code": "host_unavailable"}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    payload = encoded(response)
    try:
        sys.stdout.buffer.write(_HEADER.pack(len(payload)) + payload)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        return 1
    return 0
