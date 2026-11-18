"""One authenticated, bounded exchange with a fixed canonical handler."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import secrets
import sqlite3
import struct
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, cast

from open_brain_engine.engine.t03_contracts import T03Error

from open_brain_connectors.runtime.saved_item import SavedItemRefusal

from .operations import Request, dispatch
from .policy import Policy, Refused, RemotePolicy, encoded, load, strict_json

_HEADER = struct.Struct("!4sI")
_MAGIC = b"OBR1"
_LIMIT = 256 * 1024


def _frame(value: object) -> bytes:
    body = encoded(value)
    if len(body) > _LIMIT:
        raise ValueError("oversized frame")
    return _HEADER.pack(_MAGIC, len(body)) + body


def _size(header: bytes) -> int:
    if len(header) != _HEADER.size:
        raise ValueError("truncated header")
    marker, size = _HEADER.unpack(header)
    if marker != _MAGIC or not 0 < size <= _LIMIT:
        raise ValueError("invalid remote frame")
    return int(size)


def _ready(policy: Policy) -> dict[str, object]:
    result = dispatch(policy, Request("describe", "", {}))
    return {"binding": result["binding_id"], "extension": policy.extension_id}


def handler(source: BinaryIO, sink: BinaryIO) -> int:
    policy = load()
    ready = _ready(policy)
    sink.write(_frame(ready))
    sink.flush()
    size = _size(source.read(_HEADER.size))
    raw = source.read(size)
    if len(raw) != size or source.read(1):
        return 2
    packet = strict_json(raw)
    if not isinstance(packet, dict) or set(packet) != {"nonce", "ready", "request"}:
        return 2
    if (
        packet["ready"] != ready
        or not isinstance(packet["nonce"], str)
        or len(packet["nonce"]) != 32
    ):
        return 2
    reply = {"nonce": packet["nonce"], "ready": ready, "digest": hashlib.sha256(raw).hexdigest()}
    try:
        request = Request.parse(packet["request"])
        if request.binding_id != ready["binding"]:
            raise Refused("binding_changed")
        reply["result"] = dispatch(policy, request)
    except Refused as error:
        reply["error"] = str(error)
    except PermissionError:
        reply["error"] = "browser_unbound"
    except SavedItemRefusal as error:
        reply["error"] = error.code
    except ValueError as error:
        code = str(error)
        reply["error"] = (
            code
            if code in {"browser_operation_conflict", "browser_pending_limit"}
            else "host_unavailable"
        )
    except OSError, RuntimeError, sqlite3.Error, T03Error:
        reply["error"] = "host_unavailable"
    sink.write(_frame(reply))
    sink.flush()
    return 0


def binding_id(policy: RemotePolicy) -> str:
    return hashlib.sha256(
        encoded(["remote.v1", policy.destination, policy.host_key, policy.canonical_id])
    ).hexdigest()


def ssh_command(policy: RemotePolicy, known_hosts: Path) -> list[str]:
    return [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-T",
        "-oBatchMode=yes",
        "-oControlMaster=no",
        "-oControlPath=none",
        "-oProxyCommand=none",
        "-oProxyJump=none",
        "-oConnectTimeout=2",
        "-oConnectionAttempts=1",
        "-oStrictHostKeyChecking=yes",
        "-oHostKeyAlias=open-brain-canonical",
        "-oHostKeyAlgorithms=ssh-ed25519",
        "-oUpdateHostKeys=no",
        "-oGlobalKnownHostsFile=/dev/null",
        "-oUserKnownHostsFile=" + str(known_hosts),
        "-oIdentitiesOnly=yes",
        "-oIdentityAgent=none",
        "-oPreferredAuthentications=publickey",
        "-oClearAllForwardings=yes",
        "-oForwardAgent=no",
        "-i",
        policy.identity_file,
        "--",
        policy.destination,
        "open-brain-browser-host remote-handler",
    ]


async def _read(stream: asyncio.StreamReader) -> object:
    size = _size(await stream.readexactly(_HEADER.size))
    return strict_json(await stream.readexactly(size))


_HANDLER_ERRORS = frozenset(
    {
        "invalid_request",
        "unsupported_version",
        "operation_denied",
        "binding_changed",
        "browser_unbound",
        "host_unavailable",
        "browser_operation_conflict",
        "browser_pending_limit",
        "annotation_masked",
        "invalid_url",
        "url_secret",
        "invalid_field",
        "invalid_why",
        "file_too_large",
        "invalid_json",
        "unknown_field",
        "missing_field",
        "payload_too_large",
        "import_chunk_conflict",
        "import_confirmation_mismatch",
        "import_digest_mismatch",
        "import_incomplete",
        "import_invalid_encoding",
        "import_not_confirmed",
        "import_not_found",
        "import_not_ready",
        "import_preview_limit",
        "import_staging_unavailable",
        "import_too_large",
    }
)
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _text(value: object, limit: int, *, empty: bool = False) -> bool:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return empty or bool(value.strip())


def _uuid(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID4.fullmatch(value))


def _annotation_row(row: object) -> bool:
    if not isinstance(row, dict) or set(row) != {
        "annotation_id",
        "quote",
        "prefix",
        "suffix",
        "note",
    }:
        return False
    return (
        _uuid(row["annotation_id"])
        and _text(row["quote"], 4096)
        and _text(row["prefix"], 128, empty=True)
        and _text(row["suffix"], 128, empty=True)
        and (row["note"] is None or _text(row["note"], 1024))
    )


def _space_row(row: object) -> bool:
    return (
        isinstance(row, dict)
        and set(row) == {"space_id", "name"}
        and _text(row["space_id"], 128)
        and _text(row["name"], 4096)
    )


def _rows_page(
    args: Mapping[str, object],
    rows: object,
    cursor: object,
    key: str,
    valid_row: Callable[[object], bool],
) -> bool:
    limit, offset = args["limit"], args["offset"]
    if not isinstance(rows, list) or type(limit) is not int or type(offset) is not int:
        return False
    return (
        len(rows) <= limit
        and all(valid_row(row) for row in rows)
        and len({row[key] for row in rows}) == len(rows)
        and (
            cursor is None or (type(cursor) is int and bool(rows) and cursor == offset + len(rows))
        )
    )


def _calendar(row: object) -> bool:
    from open_brain_engine.engine.browser_revisits import validate_calendar

    if (
        not isinstance(row, dict)
        or not isinstance(row.get("local_date"), str)
        or not isinstance(row.get("zone"), str)
    ):
        return False
    try:
        validate_calendar(row["local_date"], row["zone"], require_installed=False)
    except ValueError:
        return False
    return True


def _due_row(row: object) -> bool:
    return (
        isinstance(row, dict)
        and set(row) == {"capture_id", "zone", "local_date"}
        and _text(row["capture_id"], 128)
        and _calendar(row)
    )


def _recovery_page(args: Mapping[str, object], result: dict[str, object]) -> bool:
    if set(result) != {"intents", "next_after"}:
        return False
    rows, limit = result["intents"], args["limit"]
    if type(limit) is not int or not isinstance(rows, list) or len(rows) > limit:
        return False
    after = args.get("after") or ""
    if not isinstance(after, str):
        return False
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "state"}
            or not _uuid(row["id"])
            or row["id"] <= after
            or row["state"] not in ("pending", "accepted")
        ):
            return False
        after = row["id"]
    return result["next_after"] is None or (len(rows) == limit and result["next_after"] == after)


def validate_result(operation: str, args: Mapping[str, object], result: object) -> bool:
    """Validate terminal data from the pinned handler before acknowledging a browser operation."""
    if not isinstance(result, dict):
        return False
    if operation == "operation.list":
        return _recovery_page(args, result)
    if operation == "revisit.get":
        if set(result) != {"revisit"}:
            return False
        row = result["revisit"]
        return (
            row is None
            or isinstance(row, dict)
            and set(row) == {"zone", "local_date", "state"}
            and row["state"] in ("scheduled", "dismissed", "zone_unavailable")
            and _calendar(row)
        )
    if operation == "revisit.due":
        return set(result) == {"revisits", "next_offset"} and _rows_page(
            args, result["revisits"], result["next_offset"], "capture_id", _due_row
        )
    if operation.startswith("import."):
        from .bookmark_import import validate_import_result

        return validate_import_result(operation, dict(args), result)
    if operation == "annotation.list":
        return set(result) == {"annotations", "next_offset"} and _rows_page(
            args, result["annotations"], result["next_offset"], "annotation_id", _annotation_row
        )
    if operation == "space.list":
        return (
            set(result) == {"spaces", "next_offset", "current_space"}
            and (result["current_space"] is None or _space_row(result["current_space"]))
            and _rows_page(args, result["spaces"], result["next_offset"], "space_id", _space_row)
        )
    state = result.get("state")
    if operation == "page.lookup":
        if state == "absent":
            return set(result) == {"state"}
        return (
            set(result) == {"state", "capture_id", "space_id"}
            and state == "saved"
            and _text(result["capture_id"], 128)
            and (result["space_id"] is None or _text(result["space_id"], 128))
        )
    if operation not in {
        "page.save",
        "annotation.add",
        "page.file",
        "operation.status",
        "revisit.set",
        "revisit.dismiss",
        "revisit.clear",
    }:
        return False
    if state in ("pending", "refused"):
        return set(result) == {"state"}
    if state != "accepted" or not _text(result.get("capture_id"), 128):
        return False
    if operation.startswith("revisit.") or (
        operation == "operation.status" and "revisit_action" in result
    ):
        if set(result) != {"state", "capture_id", "revisit_action"}:
            return False
        action = result["revisit_action"]
        if operation == "operation.status":
            return action in ("set", "clear", "dismiss")
        return bool(
            action == operation.removeprefix("revisit.")
            and result["capture_id"] == args["capture_id"]
        )
    if operation == "annotation.add":
        return (
            set(result) == {"state", "capture_id", "annotation_id"}
            and result["capture_id"] == args["capture_id"]
            and result["annotation_id"] == args["annotation_id"]
            and _uuid(result["annotation_id"])
        )
    if operation == "page.file" or (operation == "operation.status" and "space_id" in result):
        return (
            set(result) == {"state", "capture_id", "space_id"}
            and _text(result["space_id"], 128)
            and (
                operation == "operation.status"
                or (
                    result["capture_id"] == args["capture_id"]
                    and result["space_id"] == args["space_id"]
                )
            )
        )
    if operation == "operation.status" and "annotation_id" in result:
        return set(result) == {"state", "capture_id", "annotation_id"} and _uuid(
            result["annotation_id"]
        )
    return set(result) == {"state", "capture_id"}


async def exchange(
    policy: RemotePolicy, value: dict[str, object], command: list[str]
) -> dict[str, object]:
    ready = {"binding": policy.canonical_id, "extension": policy.extension_id}
    nonce = secrets.token_hex(16)
    packet = _frame({"nonce": nonce, "ready": ready, "request": value})
    child = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    submitted = False
    try:
        assert child.stdin and child.stdout
        async with asyncio.timeout(3):
            if await _read(child.stdout) != ready:
                raise Refused("binding_changed")
        async with asyncio.timeout(5):
            submitted = True
            child.stdin.write(packet)
            await child.stdin.drain()
            child.stdin.close()
            reply = await _read(child.stdout)
            if await child.stdout.read(1) or await child.wait() != 0:
                raise ValueError("nonterminal remote response")
            expected = {
                "nonce": nonce,
                "ready": ready,
                "digest": hashlib.sha256(packet[_HEADER.size :]).hexdigest(),
            }
            if not isinstance(reply, dict) or any(reply.get(k) != v for k, v in expected.items()):
                raise ValueError("remote identity mismatch")
            if set(reply) == set(expected) | {"error"}:
                code = reply["error"]
                if isinstance(code, str) and code in _HANDLER_ERRORS:
                    raise Refused(code)
            if set(reply) != set(expected) | {"result"} or not validate_result(
                str(value["operation"]), cast(dict[str, object], value["args"]), reply["result"]
            ):
                raise ValueError("invalid remote response")
            return cast(dict[str, object], reply["result"])
    except TimeoutError, OSError, EOFError, ValueError, RecursionError:
        raise Refused(
            "outcome_unknown"
            if submitted and "operation_id" in value and value["operation"] != "operation.status"
            else "remote_unavailable"
        ) from None
    finally:
        if child.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                child.kill()
            # Discard any buffered stdout without retaining an unbounded response.
            assert child.stdout
            while await child.stdout.read(65536):
                pass
            await child.wait()


def forward(policy: RemotePolicy, value: object) -> dict[str, object]:
    request = Request.parse(value)
    pin = binding_id(policy)
    if request.operation == "describe":
        return {"destination": policy.destination, "binding_id": pin}
    if request.binding_id != pin:
        raise Refused("binding_changed")
    assert isinstance(value, dict)
    with tempfile.TemporaryDirectory(prefix="open-brain-host-", dir="/tmp") as temp:
        hosts = Path(temp) / "known_hosts"
        hosts.write_text("open-brain-canonical " + policy.host_key + "\n")
        try:
            return asyncio.run(
                exchange(
                    policy, {**value, "binding_id": policy.canonical_id}, ssh_command(policy, hosts)
                )
            )
        except OSError:
            raise Refused("remote_unavailable") from None
