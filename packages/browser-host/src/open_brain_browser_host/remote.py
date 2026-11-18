"""One authenticated, bounded exchange with a fixed canonical handler."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import sqlite3
import struct
import tempfile
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
                if (
                    isinstance(code, str)
                    and code.isidentifier()
                    and code.isascii()
                    and len(code) <= 64
                ):
                    raise Refused(code)
            if set(reply) != set(expected) | {"result"} or not isinstance(reply["result"], dict):
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
