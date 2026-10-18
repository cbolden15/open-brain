"""Download pinned candidate model assets and verify publisher-bound hashes.

Each file is checked against its pinned byte count and either the publisher's LFS
SHA-256 or its git blob SHA-1, then the measured SHA-256 of the downloaded bytes
is recorded. Output directories must be new so failed evidence is preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST = Path(__file__).with_name("assets.json")
REFERENCE_MANIFEST = Path(__file__).with_name("reference-assets.json")
MAX_FILE_BYTES = 160 * 1024 * 1024
Fetcher = Callable[[str], bytes]


class AssetError(RuntimeError):
    """A pinned asset could not be fetched or did not match its pin."""


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "ob.g4-probe-assets.v1":
        raise AssetError("unsupported asset manifest schema")
    return manifest


def _safe_relative(path: object) -> PurePosixPath:
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        raise AssetError("unsafe asset path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in path.split("/")):
        raise AssetError("unsafe asset path")
    return pure


def _verify(entry: dict[str, Any], data: bytes) -> None:
    if len(data) != entry["bytes"]:
        raise AssetError(f"size mismatch for {entry['path']}")
    if "lfs_sha256" in entry:
        if hashlib.sha256(data).hexdigest() != entry["lfs_sha256"]:
            raise AssetError(f"sha256 mismatch for {entry['path']}")
    elif git_blob_sha1(data) != entry.get("git_blob_sha1"):
        raise AssetError(f"git blob mismatch for {entry['path']}")


def http_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "open-brain-g4-probe"})
    with urllib.request.urlopen(request, timeout=300) as response:
        data: bytes = response.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise AssetError("asset exceeds per-file ceiling")
    return data


def fetch_candidate(candidate: dict[str, Any], output: Path, fetch: Fetcher) -> dict[str, Any]:
    """Fetch every pinned file for one candidate into ``output/<id>``; return a receipt."""
    target = output / candidate["id"]
    entries = [(_safe_relative(entry["path"]), entry) for entry in candidate["files"]]
    if target.exists():
        raise AssetError(f"output already exists: {target}")
    target.mkdir(parents=True)
    files = []
    for relative, entry in entries:
        url = (
            f"https://huggingface.co/{candidate['repository']}/resolve/"
            f"{candidate['revision']}/{relative}"
        )
        data = fetch(url)
        _verify(entry, data)
        destination = target.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "xb") as handle:
            handle.write(data)
        os.chmod(destination, 0o444)
        files.append(
            {"path": str(relative), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    return {
        "id": candidate["id"],
        "repository": candidate["repository"],
        "revision": candidate["revision"],
        "files": files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--reference",
        action="store_true",
        help="fetch the PyTorch weights used only for publisher-parity checks",
    )
    args = parser.parse_args(argv)
    output: Path = args.output.resolve()
    if output.exists():
        print(f"refusing existing output {output}", file=sys.stderr)
        return 2
    manifest = load_manifest(REFERENCE_MANIFEST if args.reference else MANIFEST)
    receipts = [fetch_candidate(c, output, http_fetch) for c in manifest["candidates"]]
    receipt = {"schema": "ob.g4-probe-asset-receipt.v1", "candidates": receipts}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
