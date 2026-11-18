"""The optional Homebrew candidate keeps the native host and its colocated runtime intact."""

import hashlib
import json
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOST = "open-brain-browser-host"


def _elf(machine: int = 62) -> bytes:
    return struct.pack("<16sHHI", b"\x7fELF\x02\x01\x01", 3, machine, 1).ljust(64, b"\x00")


def _build(tmp_path: Path, binary: bytes) -> Path:
    host = tmp_path / "native" / HOST
    runtime = host / "_internal"
    runtime.mkdir(parents=True)
    executable = host / HOST
    executable.write_bytes(binary)
    executable.chmod(0o755)
    (runtime / "Library").mkdir()
    lib = runtime / "Library/libpython.so"
    lib.write_bytes(b"Synthetic relocatable library")
    lib.chmod(0o755)
    (runtime / "alias.so").symlink_to("Library/libpython.so")
    return host


def _command(host: Path, output: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "tools.open_brain_dev.browser_host_archive",
        str(host),
        str(output),
    ]


@pytest.mark.parametrize(
    ("binary", "os_name", "arch"),
    [
        (struct.pack("<IIII", 0xFEEDFACF, 0x0100000C, 0, 2), "macos", "arm64"),
        (_elf(), "linux", "x86_64"),
    ],
)
def test_host_candidate_preserves_runtime_and_pins_archive(
    tmp_path: Path, binary: bytes, os_name: str, arch: str
) -> None:
    host = _build(tmp_path, binary)
    command = _command(host, tmp_path)
    first = subprocess.run(command, cwd=ROOT, capture_output=True, check=True, timeout=20)
    evidence = json.loads(first.stdout)
    archive = Path(evidence["archive"])
    formula = Path(evidence["formula"])
    content = archive.read_bytes()
    assert content[4:8] == bytes(4)
    assert hashlib.sha256(content).hexdigest() == evidence["sha256"]
    assert evidence["platform"] == f"{os_name}-{arch}"
    formula_text = formula.read_text()
    assert f'url "{archive.as_uri()}"' in formula_text
    assert f'sha256 "{evidence["sha256"]}"' in formula_text
    assert f'libexec.install "{HOST}/{HOST}", "{HOST}/_internal"' in formula_text
    assert f'bin.install_symlink libexec/"{HOST}"' in formula_text
    assert f"depends_on :{os_name}" in formula_text
    assert f"depends_on arch: :{arch}" in formula_text
    assert 'version "' + evidence["version"] + '"' in formula_text
    with tarfile.open(archive, "r:gz") as bundle:
        entries = {member.name: member for member in bundle.getmembers()}
        assert set(entries) == {
            "LICENSE",
            "NOTICE",
            "manifest.json",
            HOST,
            f"{HOST}/{HOST}",
            f"{HOST}/_internal",
            f"{HOST}/_internal/Library",
            f"{HOST}/_internal/Library/libpython.so",
            f"{HOST}/_internal/alias.so",
        }
        for member in entries.values():
            assert (member.uid, member.gid, member.mtime) == (0, 0, 0)
        assert entries[f"{HOST}/{HOST}"].mode == 0o755
        assert entries[f"{HOST}/_internal/Library/libpython.so"].mode == 0o755
        alias = entries[f"{HOST}/_internal/alias.so"]
        assert alias.issym() and alias.linkname == "Library/libpython.so"
        manifest = bundle.extractfile("manifest.json")
        assert manifest is not None
        metadata = json.load(manifest)
        assert metadata["platform"] == evidence["platform"]
        assert metadata["version"] == evidence["version"]
        assert metadata["executable_sha256"] == hashlib.sha256(binary).hexdigest()
        target = tmp_path / "extracted"
        bundle.extractall(target, filter="data")
    assert (target / HOST / "_internal/alias.so").read_bytes() == b"Synthetic relocatable library"
    (host / HOST).touch()
    subprocess.run(command, cwd=ROOT, capture_output=True, check=True, timeout=20)
    assert archive.read_bytes() == content
    assert formula.read_text() == formula_text


@pytest.mark.parametrize(
    "binary",
    [
        b"#!/bin/sh\n",
        struct.pack("<IIII", 0xFEEDFACF, 0x01000007, 0, 2),
        _elf(machine=183),
    ],
)
def test_host_candidate_refuses_an_unsupported_executable(tmp_path: Path, binary: bytes) -> None:
    host = _build(tmp_path, binary)
    result = subprocess.run(_command(host, tmp_path), cwd=ROOT, capture_output=True, timeout=20)
    assert result.returncode != 0
    assert list(tmp_path.glob("*.tar.gz")) == []
    assert list(tmp_path.glob("*.rb")) == []


@pytest.mark.parametrize("target", ["../../outside", "../open-brain-browser-host", "/opt/library"])
def test_host_candidate_refuses_links_outside_colocated_runtime(
    tmp_path: Path, target: str
) -> None:
    host = _build(tmp_path, _elf())
    (host.parent / "outside").write_bytes(b"Unpackaged file")
    (host / "_internal/unsafe").symlink_to(target)
    result = subprocess.run(_command(host, tmp_path), cwd=ROOT, capture_output=True, timeout=20)
    assert result.returncode != 0
    assert not list(tmp_path.glob("*.tar.gz"))
