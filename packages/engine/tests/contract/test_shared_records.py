from __future__ import annotations

import shutil
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path

import pytest
from open_brain_engine.portability import (
    PORTABLE_BLOB_STAGING_BYTES,
    PortabilityMappingError,
    SharedBlob,
    shared_brain_from_snapshot,
)
from open_brain_engine.portable.v1 import validated_portable_snapshot


def _fixture_root(tmp_path: Path) -> Path:
    destination = tmp_path / "brain-root"
    resource = files("open_brain_engine.portable").joinpath(
        "conformance", "v1", "brain-root"
    )
    with as_file(resource) as source:
        shutil.copytree(source, destination)
    return destination


def test_portable_snapshot_maps_to_shared_records_without_secure_node_code(
    tmp_path: Path,
) -> None:
    snapshot = validated_portable_snapshot(_fixture_root(tmp_path))
    shared = shared_brain_from_snapshot(snapshot)

    assert len(shared.records) == 18
    assert {record.family for record in shared.records} == {
        "action",
        "brain",
        "capture",
        "decision",
        "event",
        "measurement",
        "page",
        "proposal",
        "publication",
        "route",
        "space",
    }
    assert shared.reconstruct_file_set() == {
        path: payload
        for path, payload in snapshot.files.items()
        if path != "portable-manifest.json"
    }


def test_shared_blob_streaming_uses_the_portable_boundary_limit() -> None:
    payload = b"x" * (PORTABLE_BLOB_STAGING_BYTES + 1)
    digest = sha256(payload).hexdigest()
    blob = SharedBlob(
        path=f"sources/blobs/sha256/{digest[:2]}/{digest}",
        sha256=digest,
        data=payload,
    )

    chunks = blob.iter_chunks()

    assert len(next(chunks)) == PORTABLE_BLOB_STAGING_BYTES
    assert next(chunks) == b"x"
    with pytest.raises(StopIteration):
        next(chunks)
    with pytest.raises(PortabilityMappingError, match="blob binding"):
        SharedBlob(path=blob.path, sha256=blob.sha256, data=payload + b"tampered")
