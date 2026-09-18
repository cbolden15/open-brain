"""Private, root-bound continuation records authenticated by a local rotating key."""

from __future__ import annotations

import base64
import hmac
import json
import math
import os
import re
import secrets
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    StorageError,
    _open_child_directory,
    _open_root,
)

from .contracts import LocalEngineContext
from .t03_contracts import T03Error

if TYPE_CHECKING:
    from .local import BrainEngine

_DIRECTORY = ".open-brain/cursors"
_DOMAIN = b"open-brain-cursor-v1"
_RECORD = re.compile(r"[0-9a-f]{64}\.json")
MAX_HANDLES = 2000
TTL_SECONDS = 900


class CursorStore:
    """All callers hold the unchanged Brain writer lease during access and allocation."""

    def __init__(self, profile: LocalEngineContext) -> None:
        self.profile = profile

    @contextmanager
    def _directory(self, *, create: bool) -> Iterator[int]:
        root = state = directory = -1
        try:
            root = _open_root(self.profile.root, self.profile.root_identity)
            state = _open_child_directory(root, ".open-brain", create=create)
            directory = _open_child_directory(state, "cursors", create=create)
            for descriptor in (state, directory):
                info = os.fstat(descriptor)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise T03Error("operation_pending")
            for name in os.listdir(directory):
                if re.fullmatch(r"\.write-[0-9a-f]{32}", name):
                    self._read(directory, name)
                    os.unlink(name, dir_fd=directory)
            yield directory
        except OSError, StorageError:
            raise T03Error("operation_pending") from None
        finally:
            for descriptor in (directory, state, root):
                if descriptor >= 0:
                    os.close(descriptor)

    def _read(self, directory: int, name: str, *, maximum: int = 65536) -> bytes | None:
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > maximum
            ):
                raise T03Error("operation_pending")
            data = os.read(descriptor, maximum + 1)
            if len(data) != info.st_size:
                raise T03Error("operation_pending")
            return data
        finally:
            os.close(descriptor)

    def _write(self, name: str, data: bytes) -> None:
        # The caller already owns the Brain writer lease. Keep cursor writes and
        # their temporary files wholly inside operational storage (no root lock).
        with self._directory(create=True) as directory:
            self._read(directory, name)
            temporary = ".write-" + secrets.token_hex(16)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                if os.write(descriptor, data) != len(data):
                    raise T03Error("operation_pending")
                os.fsync(descriptor)
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)

    def _validate_key(self, raw: bytes) -> dict[str, Any]:
        key = json.loads(raw)
        if (
            set(key) != {"version", "epoch", "key", "root"}
            or type(key["version"]) is not int
            or key["version"] != 1
            or type(key["epoch"]) is not int
            or not 1 <= key["epoch"] < 2**64
            or not isinstance(key["key"], str)
            or re.fullmatch(r"[0-9a-f]{64}", key["key"]) is None
            or not isinstance(key["root"], list)
            or len(key["root"]) != 2
            or any(type(value) is not int for value in key["root"])
        ):
            raise T03Error("operation_pending")
        return cast(dict[str, Any], key)

    def _finish_rotation(self, directory: int, pending: bytes) -> dict[str, Any]:
        key = self._validate_key(pending)
        if key["root"] != list(self.profile.root_identity):
            raise T03Error("operation_pending")
        epoch_bytes = self._read(directory, "epoch", maximum=8)
        if epoch_bytes is not None and len(epoch_bytes) != 8:
            raise T03Error("operation_pending")
        epoch = 0 if epoch_bytes is None else int.from_bytes(epoch_bytes, "big")
        if epoch not in {key["epoch"] - 1, key["epoch"]}:
            raise T03Error("operation_pending")
        self._write("epoch", key["epoch"].to_bytes(8, "big"))
        self._write("key.json", pending)
        os.unlink("rotation.pending", dir_fd=directory)
        os.fsync(directory)
        return key

    def _key(self, directory: int, *, create: bool) -> dict[str, Any]:
        pending = self._read(directory, "rotation.pending", maximum=1024)
        if pending is not None:
            return self._finish_rotation(directory, pending)
        raw = self._read(directory, "key.json", maximum=1024)
        epoch = self._read(directory, "epoch", maximum=8)
        if raw is None:
            if (
                not create
                or epoch is not None
                or any(_RECORD.fullmatch(p) for p in os.listdir(directory))
            ):
                raise T03Error("operation_pending")
            key = {
                "version": 1,
                "epoch": 1,
                "key": secrets.token_hex(32),
                "root": list(self.profile.root_identity),
            }
            # Exclusive creation prevents ever adopting another process's unsigned key.
            descriptor = os.open(
                "rotation.pending",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            data = portable_canonical_json_bytes(key)
            try:
                if os.write(descriptor, data) != len(data):
                    raise T03Error("operation_pending")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory)
            return self._finish_rotation(directory, data)
        key = self._validate_key(raw)
        if epoch is None or len(epoch) != 8 or int.from_bytes(epoch, "big") != key["epoch"]:
            raise T03Error("operation_pending")
        if key["root"] != list(self.profile.root_identity):
            raise T03Error("cursor_stale")
        return key

    def bind_root(self, engine: BrainEngine) -> None:
        """Persist an operational root binding; a copied root gets a fresh incarnation.

        The old binding stays until both SQL and key rotation are durable. Recovery
        may rotate again after interruption, but can never accept the old cursor.
        """
        try:
            with self._directory(create=True) as directory:
                raw = self._read(directory, "identity.json", maximum=1024)
                key_raw = self._read(directory, "key.json", maximum=1024)
                identity = None if raw is None else json.loads(raw)
                if identity is not None and (
                    not isinstance(identity, dict)
                    or set(identity) != {"root", "incarnation"}
                    or not isinstance(identity["root"], list)
                    or len(identity["root"]) != 2
                    or any(type(value) is not int for value in identity["root"])
                    or type(identity["incarnation"]) is not str
                ):
                    raise T03Error("operation_pending")
                moved = identity is not None and identity["root"] != list(
                    self.profile.root_identity
                )
                if key_raw is not None and identity is None:
                    key = self._validate_key(key_raw)
                    moved = moved or key["root"] != list(self.profile.root_identity)
                from .local_schema import open_local_database

                connection = open_local_database(self.profile, clock=engine._clock)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    if moved:
                        connection.execute(
                            "UPDATE engine_generations SET incarnation=? WHERE singleton=1",
                            (str(uuid.uuid4()),),
                        )
                    incarnation = connection.execute(
                        "SELECT incarnation FROM engine_generations WHERE singleton=1"
                    ).fetchone()[0]
                    if (
                        identity is not None
                        and not moved
                        and identity["incarnation"] != incarnation
                    ):
                        raise T03Error("operation_pending")
                    connection.commit()
                finally:
                    connection.close()
                if moved:
                    self.rotate()
                    for name in os.listdir(directory):
                        if _RECORD.fullmatch(name):
                            self._read(directory, name)
                            os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                self._write(
                    "identity.json",
                    portable_canonical_json_bytes(
                        {
                            "root": list(self.profile.root_identity),
                            "incarnation": incarnation,
                        }
                    ),
                )
        except T03Error:
            raise
        except ValueError, TypeError, KeyError, OSError, StorageError:
            raise T03Error("operation_pending") from None

    def rotate(self) -> int:
        """Called only through explicit trusted-owner recovery/rotation authority."""
        try:
            with self._directory(create=True) as directory:
                epoch_bytes = self._read(directory, "epoch", maximum=8)
                if epoch_bytes is not None and len(epoch_bytes) != 8:
                    raise T03Error("operation_pending")
                epoch = 0 if epoch_bytes is None else int.from_bytes(epoch_bytes, "big")
                if epoch >= 2**64 - 1:
                    raise T03Error("operation_pending")
                # Validate custody even if the key's contents require authorized recovery.
                self._read(directory, "key.json", maximum=1024)
                pending = portable_canonical_json_bytes(
                    {
                        "version": 1,
                        "epoch": epoch + 1,
                        "key": secrets.token_hex(32),
                        "root": list(self.profile.root_identity),
                    }
                )
                self._write("rotation.pending", pending)
                return int(self._finish_rotation(directory, pending)["epoch"])
        except T03Error:
            raise
        except ValueError, TypeError, KeyError, OSError, StorageError:
            raise T03Error("operation_pending") from None

    @staticmethod
    def _record(raw: bytes) -> dict[str, Any]:
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or any(
                type(value.get(name)) not in (float, int) or not math.isfinite(value[name])
                for name in ("issued_at", "expires_at")
            )
            or type(value.get("key_epoch")) is not int
        ):
            raise T03Error("cursor_invalid")
        return value

    def allocate(self, record: dict[str, Any], *, now: float) -> str:
        try:
            with self._directory(create=True) as directory:
                key = self._key(directory, create=True)
                live = 0
                for name in os.listdir(directory):
                    if name in {"key.json", "epoch", "identity.json"}:
                        continue
                    if _RECORD.fullmatch(name) is None:
                        raise T03Error("operation_pending")
                    raw = self._read(directory, name)
                    if raw is None:
                        raise T03Error("operation_pending")
                    prior = self._record(raw)
                    if prior["expires_at"] <= now or prior["key_epoch"] != key["epoch"]:
                        os.unlink(name, dir_fd=directory)
                    else:
                        live += 1
                if live >= MAX_HANDLES:
                    raise T03Error("operation_pending")
                handle = secrets.token_bytes(32)
                private = dict(
                    record,
                    issued_at=int(now),
                    expires_at=int(now) + TTL_SECONDS,
                    key_epoch=key["epoch"],
                )
                self._write(handle.hex() + ".json", portable_canonical_json_bytes(private))
                header = b"\x01" + key["epoch"].to_bytes(8, "big") + handle
                tag = hmac.digest(bytes.fromhex(key["key"]), _DOMAIN + header, "sha256")
                return base64.urlsafe_b64encode(header + tag).rstrip(b"=").decode("ascii")
        except T03Error:
            raise
        except ValueError, TypeError, KeyError, OSError, StorageError:
            raise T03Error("operation_pending") from None

    def resolve(self, cursor: str, *, now: float) -> dict[str, Any]:
        try:
            if (
                not isinstance(cursor, str)
                or len(cursor) != 98
                or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None
            ):
                raise T03Error("cursor_invalid")
            decoded = base64.urlsafe_b64decode(cursor + "==")
            if (
                len(decoded) != 73
                or decoded[0] != 1
                or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != cursor
            ):
                raise T03Error("cursor_invalid")
            with self._directory(create=False) as directory:
                key = self._key(directory, create=False)
                if int.from_bytes(decoded[1:9], "big") != key["epoch"]:
                    raise T03Error("cursor_stale")
                tag = hmac.digest(bytes.fromhex(key["key"]), _DOMAIN + decoded[:41], "sha256")
                if not hmac.compare_digest(tag, decoded[41:]):
                    raise T03Error("cursor_invalid")
                raw = self._read(directory, decoded[9:41].hex() + ".json")
                if raw is None:
                    raise T03Error("cursor_invalid")
                record = self._record(raw)
                if record["expires_at"] <= now or record["key_epoch"] != key["epoch"]:
                    raise T03Error("cursor_stale")
                return record
        except T03Error:
            raise
        except ValueError, TypeError, KeyError, OSError, StorageError:
            raise T03Error("cursor_invalid") from None


def binding_digest(value: object) -> str:
    return sha256(portable_canonical_json_bytes(value)).hexdigest()
