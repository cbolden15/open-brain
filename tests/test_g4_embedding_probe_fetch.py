"""Pinned asset verification for the G4 embedding feasibility probe."""

import hashlib
import json
from pathlib import Path

import pytest

from tools.g4_embedding_probe.fetch import (
    AssetError,
    fetch_candidate,
    git_blob_sha1,
    load_manifest,
)


def _candidate(payload: dict[str, bytes], *, lfs: str | None = None) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for path, data in sorted(payload.items()):
        entry: dict[str, object] = {"path": path, "bytes": len(data)}
        if path == lfs:
            entry["lfs_sha256"] = hashlib.sha256(data).hexdigest()
        else:
            entry["git_blob_sha1"] = git_blob_sha1(data)
        files.append(entry)
    return {
        "id": "synthetic",
        "repository": "example/model",
        "revision": "0" * 40,
        "license": "mit",
        "files": files,
    }


def test_git_blob_sha1_matches_git_object_format() -> None:
    # `printf 'hello\n' | git hash-object --stdin`
    assert git_blob_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_committed_manifest_pins_both_candidates() -> None:
    manifest = load_manifest()
    ids = [c["id"] for c in manifest["candidates"]]
    assert ids == ["minilm", "bge"]
    for candidate in manifest["candidates"]:
        assert len(candidate["revision"]) == 40
        graphs = [f for f in candidate["files"] if f["path"] == "onnx/model.onnx"]
        assert len(graphs) == 1 and "lfs_sha256" in graphs[0]


def test_fetch_verifies_and_records_measured_sha256(tmp_path: Path) -> None:
    payload = {"config.json": b"{}\n", "onnx/model.onnx": b"graph-bytes"}
    candidate = _candidate(payload, lfs="onnx/model.onnx")
    out = tmp_path / "assets"

    def serve(url: str) -> bytes:
        return payload[url.rsplit("/resolve/", 1)[1][41:]]

    receipt = fetch_candidate(candidate, out, serve)

    assert (out / "synthetic" / "onnx" / "model.onnx").read_bytes() == b"graph-bytes"
    measured = {f["path"]: f["sha256"] for f in receipt["files"]}
    assert measured["config.json"] == hashlib.sha256(b"{}\n").hexdigest()
    json.dumps(receipt)


@pytest.mark.parametrize("tamper", ["size", "content"])
def test_fetch_rejects_mismatch_and_leaves_no_file(tmp_path: Path, tamper: str) -> None:
    payload = {"vocab.txt": b"[PAD]\n[UNK]\n"}
    candidate = _candidate(payload)
    served = b"[PAD]\n[UNK]\nextra\n" if tamper == "size" else b"[PAD]\n[XXX]\n"

    with pytest.raises(AssetError):
        fetch_candidate(candidate, tmp_path / "assets", lambda url: served)

    assert not (tmp_path / "assets" / "synthetic" / "vocab.txt").exists()


def test_fetch_refuses_existing_output(tmp_path: Path) -> None:
    out = tmp_path / "assets"
    (out / "synthetic").mkdir(parents=True)
    with pytest.raises(AssetError):
        fetch_candidate(_candidate({"a.json": b"1"}), out, lambda url: b"1")


@pytest.mark.parametrize("bad", ["../escape", "/abs", "a/../b", ""])
def test_fetch_rejects_unsafe_paths(tmp_path: Path, bad: str) -> None:
    candidate = _candidate({"a.json": b"1"})
    files = candidate["files"]
    assert isinstance(files, list)
    files[0]["path"] = bad
    with pytest.raises(AssetError):
        fetch_candidate(candidate, tmp_path / "assets", lambda url: b"1")
