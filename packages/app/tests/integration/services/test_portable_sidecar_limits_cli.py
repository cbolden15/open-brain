"""Portable export of Brains past the old 64 MiB sidecar bound, and the named refusal."""

import json
import os
import sqlite3
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine, CaptureSubmission, FilePayload, TextPayload
from open_brain_engine.portable import v8_capture_metadata, v8_custody, v9

from open_brain.local_data import select_local_root
from open_brain.profile import compile_single_user_local
from open_brain.services import local_entrypoints
from open_brain.services.local_bootstrap import open_local_brain
from open_brain.services.local_entrypoints import run_cli

OLD_BOUND = 64 * 1024 * 1024
FILE_BYTES = 1_048_576


def _count(root: Path) -> int:
    database = root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        return int(db.execute("SELECT count(*) FROM captures").fetchone()[0])


@pytest.mark.integration
def test_export_verify_and_restore_past_the_old_64_mib_capture_metadata_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    profile = compile_single_user_local(root)
    engine = BrainEngine.open(profile)
    # 50 synthetic 1 MiB files: base64 in the capture-metadata sidecar puts it past 64 MiB.
    for index in range(50):
        body = f"synthetic-{index:04d}|".encode() * (FILE_BYTES // 15)
        engine.capture.submit(
            CaptureSubmission.for_local_owner(
                profile=profile,
                payload=FilePayload(f"synthetic-{index}.bin", "application/octet-stream", body),
                delivery_id=f"limits.file.{index}",
            )
        )
    captures = _count(root)
    assert captures == 50

    archive = tmp_path / "export"
    assert run_cli(("export", str(archive), "--verify", "--json", "--data-dir", str(root))) == 0
    exported = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert exported["verification"] == "verified"
    assert exported["captures"] == captures
    sidecar = archive / v8_capture_metadata.CAPTURE_METADATA_PATH
    assert sidecar.stat().st_size > OLD_BOUND

    restored = tmp_path / "restored"
    assert (
        run_cli(("restore", str(archive), str(restored), "--import-id", f"import_{uuid4()}")) == 0
    )
    assert _count(restored) == captures


@pytest.mark.parametrize(
    ("module", "name", "path"),
    (
        (
            v8_capture_metadata,
            "MAX_CAPTURE_METADATA_BYTES",
            v8_capture_metadata.CAPTURE_METADATA_PATH,
        ),
        (v8_custody, "MAX_CUSTODY_BYTES", v8_custody.CUSTODY_PATH),
        (v9, "MAX_HISTORICAL_SIDECAR_BYTES", v9.HISTORICAL_AUTHORITY_PATH),
    ),
)
def test_export_over_a_sidecar_limit_fails_by_name_and_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    path: str,
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    engine.capture.accept(TextPayload("Synthetic limit body"), delivery_id="limits.text")
    monkeypatch.setattr(module, name, 8)
    archive = tmp_path / "export"

    code = run_cli(("export", str(archive), "--verify", "--json", "--data-dir", str(root)))

    assert code == 78
    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert payload["status"] == "failed"
    error = cast(dict[str, object], payload["error"])
    assert error["code"] == "portable_export_too_large"
    assert error["sidecar"] == path
    assert error["limit_bytes"] == 8
    assert cast(int, error["size_bytes"]) > 8
    assert path in cast(str, error["message"])
    assert not archive.exists()
    assert not list(tmp_path.glob(f".{archive.name}.portable-stage-*"))


def test_verified_export_record_reads_a_manifest_over_one_mib(tmp_path: Path) -> None:
    # About 170 manifest bytes per file: a Brain of ~6,200 files has a manifest over 1 MiB.
    # Building one takes minutes, so pad a real export's manifest instead; the recorder
    # reads only its schema version and digest.
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    engine.capture.accept(TextPayload("Synthetic manifest body"), delivery_id="limits.manifest")
    archive = tmp_path / "export"
    assert run_cli(("export", str(archive), "--json", "--data-dir", str(root))) == 0
    path = archive / "portable-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["synthetic_padding"] = "x" * (2 * 1024 * 1024)
    path.chmod(0o600)
    path.write_bytes(json.dumps(manifest).encode())

    selection = select_local_root(data_dir=str(root), environment=os.environ)
    with open_local_brain(selection) as session:
        local_entrypoints._record_verified_export(
            session, archive, export_id="export_" + str(uuid4())
        )

    evidence = json.loads((root / ".open-brain/state/local-export-evidence.json").read_bytes())
    assert evidence["schema_version"] == manifest["schema_version"]
