"""The optional Homebrew candidate is deterministic and separately installable."""

import hashlib
import json
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _elf(machine: int = 62) -> bytes:
    return struct.pack("<16sHHI", b"\x7fELF\x02\x01\x01", 3, machine, 1).ljust(64, b"\x00")


@pytest.mark.parametrize(
    ("binary", "os_name", "arch"),
    [
        (struct.pack("<IIII", 0xFEEDFACF, 0x0100000C, 0, 2), "macos", "arm64"),
        (_elf(), "linux", "x86_64"),
    ],
)
def test_browser_host_candidate_is_reproducible_and_pins_its_archive(
    tmp_path: Path, binary: bytes, os_name: str, arch: str
) -> None:
    executable = tmp_path / "open-brain-browser-host"
    executable.write_bytes(binary)
    executable.chmod(0o755)
    command = [
        sys.executable,
        "-m",
        "tools.open_brain_dev.browser_host_archive",
        str(executable),
        str(tmp_path),
    ]
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
    assert 'bin.install "open-brain-browser-host"' in formula_text
    assert f"depends_on :{os_name}" in formula_text
    assert f"depends_on arch: :{arch}" in formula_text
    assert 'version "' + evidence["version"] + '"' in formula_text
    with tarfile.open(archive, "r:gz") as bundle:
        entries = bundle.getmembers()
        assert [member.name for member in entries] == [
            "LICENSE",
            "NOTICE",
            "manifest.json",
            "open-brain-browser-host",
        ]
        for member in entries:
            assert member.isfile() and (member.uid, member.gid, member.mtime) == (0, 0, 0)
        assert entries[-1].mode == 0o755
        manifest = bundle.extractfile("manifest.json")
        assert manifest is not None
        metadata = json.load(manifest)
        assert metadata["platform"] == evidence["platform"]
        assert metadata["version"] == evidence["version"]
        assert metadata["executable_sha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()
    executable.touch()
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
    executable = tmp_path / "open-brain-browser-host"
    executable.write_bytes(binary)
    executable.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.open_brain_dev.browser_host_archive",
            str(executable),
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert list(tmp_path.glob("*.tar.gz")) == []
    assert list(tmp_path.glob("*.rb")) == []
