"""Private Unix-socket control for the optional collector; no core listener."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import socketserver
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import cast

from open_brain_collector.live_manager import LiveSourceManager
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore

_MAX_REQUEST = 65_536
_MAX_RESPONSE = 262_144


def control_endpoint(state_path: Path, brain_root: Path) -> Path:
    if not state_path.is_absolute() or not brain_root.is_absolute():
        raise LiveSourceError("collector_invalid_path")
    identity = f"{state_path.resolve()}\x00{brain_root.resolve()}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    # A fixed, short path avoids macOS's 104-byte sockaddr_un limit. Ownership,
    # directory mode and socket type are checked before every connection.
    return Path("/tmp").resolve() / f"open-brain-collector-{digest}" / "control.sock"


def _receive(
    connection: socket.socket, maximum: int, *, timeout_seconds: float = 2
) -> dict[str, object]:
    chunks = bytearray()
    deadline = time.monotonic() + timeout_seconds
    while len(chunks) <= maximum:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LiveSourceError("collector_lost_response")
        connection.settimeout(remaining)
        part = connection.recv(min(4096, maximum + 1 - len(chunks)))
        if not part:
            raise LiveSourceError("collector_lost_response")
        chunks.extend(part)
        if b"\n" in part:
            break
    if len(chunks) > maximum or not chunks.endswith(b"\n") or b"\n" in chunks[:-1]:
        raise LiveSourceError("collector_invalid_frame")
    try:
        value = json.loads(chunks)
        if not isinstance(value, dict):
            raise ValueError
        return cast(dict[str, object], value)
    except ValueError, UnicodeError, RecursionError:
        raise LiveSourceError("collector_invalid_frame") from None


def control_request(
    state_path: Path,
    brain_root: Path,
    operation: str,
    arguments: dict[str, object],
    *,
    timeout_seconds: float = 240,
) -> dict[str, object]:
    endpoint = control_endpoint(state_path, brain_root)
    PrivateJsonStore(endpoint.parent)
    try:
        info = endpoint.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise LiveSourceError("collector_unsafe_socket")
        request_id = uuid.uuid4().hex
        payload = bounded_json(
            {
                "schema_version": 1,
                "request_id": request_id,
                "operation": operation,
                "arguments": arguments,
            },
            _MAX_REQUEST,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(endpoint))
            connection.sendall(payload + b"\n")
            response = _receive(connection, _MAX_RESPONSE, timeout_seconds=timeout_seconds)
    except OSError, TimeoutError:
        raise LiveSourceError("collector_unavailable") from None
    if response.get("schema_version") != 1 or response.get("request_id") != request_id:
        raise LiveSourceError("collector_invalid_response")
    if response.get("error") is not None:
        code = response["error"]
        raise LiveSourceError(code if type(code) is str else "collector_failed")
    result = response.get("result")
    if not isinstance(result, dict):
        raise LiveSourceError("collector_invalid_response")
    return cast(dict[str, object], result)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    manager: LiveSourceManager
    slots: threading.BoundedSemaphore

    def process_request(self, request: socket.socket, client_address: str) -> None:  # type: ignore[override]
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: str) -> None:  # type: ignore[override]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: socket.socket, client_address: str) -> None:  # type: ignore[override]
        # Incoming requests/provider failures may contain private metadata.
        pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        connection = cast(socket.socket, self.request)
        connection.settimeout(2)
        request_id: object = None
        try:
            request = _receive(connection, _MAX_REQUEST)
            request_id = request.get("request_id")
            if (
                set(request) != {"schema_version", "request_id", "operation", "arguments"}
                or request["schema_version"] != 1
                or type(request_id) is not str
                or len(request_id) > 64
                or type(request["operation"]) is not str
            ):
                raise LiveSourceError("collector_invalid_request")
            result = cast(_Server, self.server).manager.dispatch(
                request["operation"], request["arguments"]
            )
            response: dict[str, object] = {
                "schema_version": 1,
                "request_id": request_id,
                "result": result,
                "error": None,
            }
        except Exception as error:
            response = {
                "schema_version": 1,
                "request_id": request_id,
                "result": None,
                "error": error.code
                if isinstance(error, LiveSourceError)
                else "source_operation_failed",
            }
        with suppress(OSError, LiveSourceError):
            connection.sendall(bounded_json(response, _MAX_RESPONSE) + b"\n")


@contextmanager
def control_server(
    state_path: Path, brain_root: Path, *, background: bool = False
) -> Iterator[None]:
    endpoint = control_endpoint(state_path, brain_root)
    store = PrivateJsonStore(endpoint.parent)
    with store.lock("server", timeout_seconds=0):
        if endpoint.exists() or endpoint.is_symlink():
            info = endpoint.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise LiveSourceError("collector_unsafe_socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(str(endpoint))
                except ConnectionRefusedError:
                    endpoint.unlink()
                else:
                    raise LiveSourceError("collector_already_owned")
        manager = LiveSourceManager(state_path.parent / "live", brain_root, background=background)
        with _Server(str(endpoint), _Handler) as server:
            endpoint.chmod(0o600)
            original = endpoint.stat()
            server.manager = manager
            server.slots = threading.BoundedSemaphore(4)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
            )
            thread.start()
            try:
                yield
            finally:
                server.shutdown()
                thread.join(timeout=2)
                current = endpoint.lstat()
                if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                    endpoint.unlink()
