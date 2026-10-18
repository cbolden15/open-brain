"""Optional fast foreground harness resource with a complete archive inventory."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath

SCHEMA = "open-brain-harness-inventory.v1"
INVENTORY = "harness/inventory.json"
EXECUTABLE = "open-brain-harness/open-brain-harness"
ASSETS = frozenset({"harness/harness-hook.mjs", "harness/pi-memory.mjs", "harness/pi-entry.mjs"})


def _record(
    path: Path, name: str, *, label: str = "harness"
) -> tuple[dict[str, object], bytes | None]:
    from .base_native import BaseNativeError

    info = path.lstat()
    if info.st_mode & 0o022 and not stat.S_ISLNK(info.st_mode):
        raise BaseNativeError(f"{label} resource permissions are invalid")
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        return {"path": name, "kind": "symlink", "mode": 0o777, "target": target}, None
    if stat.S_ISDIR(info.st_mode):
        return {"path": name, "kind": "directory", "mode": 0o755}, None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024 * 1024:
        raise BaseNativeError(f"{label} resource file is invalid")
    data = path.read_bytes()
    if len(data) != info.st_size:
        raise BaseNativeError(f"{label} resource changed during read")
    mode = 0o755 if info.st_mode & 0o111 else 0o644
    return {
        "path": name,
        "kind": "file",
        "mode": mode,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }, data


def write_harness_archive(runtime: Path, assets: Path, destination: Path, *, version: str) -> Path:
    """Audit shared modules, inventory every member, then scan the whole archive."""
    from .artifact_audit import inspect_artifact
    from .base_native import BaseNativeError, audit_base_artifact

    if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", version) is None:
        raise BaseNativeError("harness resource version is invalid")
    if runtime.is_symlink() or not runtime.is_dir():
        raise BaseNativeError("harness runtime directory is invalid")
    audit = audit_base_artifact(runtime / "open-brain-harness")
    root = runtime.resolve(strict=True)
    files = [(root, "open-brain-harness")]
    files.extend(
        (path, "open-brain-harness/" + path.relative_to(root).as_posix())
        for path in sorted(root.rglob("*"))
    )
    files.extend((assets / PurePosixPath(name).name, name) for name in sorted(ASSETS))
    records: list[dict[str, object]] = [{"path": "harness", "kind": "directory", "mode": 0o755}]
    payloads: dict[str, bytes] = {}
    try:
        for path, name in files:
            record, payload = _record(path, name)
            records.append(record)
            if payload is not None:
                payloads[name] = payload
    except OSError:
        raise BaseNativeError("harness resource is unavailable") from None
    records.sort(key=lambda record: str(record["path"]))
    inventory = (
        json.dumps(
            {
                "schema_version": SCHEMA,
                "version": version,
                "platform": audit.platform_tag,
                "members": records,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    if len(inventory) > 64 * 1024:
        raise BaseNativeError("harness inventory exceeds audit bounds")
    payloads[INVENTORY] = inventory
    entries = records + [{"path": INVENTORY, "kind": "file", "mode": 0o644, "size": len(inventory)}]
    selected = destination.resolve()
    selected.mkdir(parents=True, exist_ok=True)
    target = selected / f"open-brain-harness-{version}-{audit.platform_tag}.tar.gz"
    write_records_archive(target, entries, payloads)
    if inspect_artifact(target, ()):
        raise BaseNativeError("harness archive failed the strict content audit")
    return target


def write_records_archive(
    target: Path, entries: list[dict[str, object]], payloads: dict[str, bytes]
) -> None:
    """Reproducible USTAR archive of directory, file and relative symlink records."""
    with (
        target.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive,
    ):
        for record in sorted(entries, key=lambda row: str(row["path"])):
            info = tarfile.TarInfo(str(record["path"]))
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            mode = record["mode"]
            assert isinstance(mode, int)
            info.mode = mode
            kind = record["kind"]
            if kind == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = str(record["target"])
                archive.addfile(info)
            else:
                data = payloads[str(record["path"])]
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
