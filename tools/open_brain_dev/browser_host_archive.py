"""Package the optional native browser host and a digest-pinned local Formula."""

import argparse
import gzip
import hashlib
import io
import json
import re
import struct
import tarfile
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

EXECUTABLE = "open-brain-browser-host"
_HOMEPAGE = "https://github.com/cbolden15/open-brain"


def _target(binary: bytes) -> tuple[str, str]:
    if len(binary) >= 16:
        magic, cpu, _, file_type = struct.unpack_from("<IIII", binary)
        if (magic, cpu, file_type) == (0xFEEDFACF, 0x0100000C, 2):
            return "macos", "arm64"
    if len(binary) >= 64 and binary[:7] == b"\x7fELF\x02\x01\x01" and binary[7] in (0, 3):
        file_type, machine, version = struct.unpack_from("<HHI", binary, 16)
        if file_type in (2, 3) and machine == 62 and version == 1:
            return "linux", "x86_64"
    raise ValueError("browser host must be a macOS arm64 or Linux x86_64 executable")


def _formula(archive: Path, digest: str, version: str, os_name: str, arch: str) -> str:
    return f"""# Generated local candidate. The archive lives on this machine.
class OpenBrainBrowserHost < Formula
  desc "Optional foreground Chrome native messaging host for Open Brain"
  homepage "{_HOMEPAGE}"
  url "{archive.as_uri()}"
  version "{version}"
  sha256 "{digest}"
  license "Apache-2.0"

  depends_on :{os_name}
  depends_on arch: :{arch}

  def install
    bin.install "{EXECUTABLE}"
    (pkgshare/"licenses").install "LICENSE", "NOTICE"
    pkgshare.install "manifest.json"
  end

  test do
    system bin/"{EXECUTABLE}", "--help"
  end
end
"""


def _write_archive(staged: Path, files: dict[str, bytes]) -> None:
    with (
        staged.open("xb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as bundle,
    ):
        for name, contents in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = 0o755 if name == EXECUTABLE else 0o644
            bundle.addfile(member, io.BytesIO(contents))


def package_host(root: Path, executable: Path, destination: Path) -> dict[str, str]:
    """Create a reproducible tarball and a formula that installs only the optional host."""
    if executable.is_symlink() or not executable.is_file() or not executable.stat().st_mode & 0o111:
        raise ValueError("browser host input must be an executable regular file")
    binary = executable.read_bytes()
    os_name, arch = _target(binary)
    project = tomllib.loads((root / "packages/browser-host/pyproject.toml").read_text())
    version = project["project"]["version"]
    if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ValueError("browser host version must be numeric major.minor.patch")
    platform = f"{os_name}-{arch}"
    metadata = {
        "product": EXECUTABLE,
        "version": version,
        "platform": platform,
        "executable_sha256": hashlib.sha256(binary).hexdigest(),
    }
    destination = destination.resolve(strict=True)
    archive = destination / f"{EXECUTABLE}-{version}-{platform}.tar.gz"
    formula = destination / f"{EXECUTABLE}.rb"
    files = {
        EXECUTABLE: binary,
        "LICENSE": (root / "LICENSE").read_bytes(),
        "NOTICE": (root / "NOTICE").read_bytes(),
        "manifest.json": (
            json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    }
    with TemporaryDirectory(dir=destination) as staging:
        staged_archive = Path(staging) / archive.name
        staged_formula = Path(staging) / formula.name
        _write_archive(staged_archive, files)
        with staged_archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        staged_formula.write_text(
            _formula(archive, digest, version, os_name, arch), encoding="utf-8"
        )
        staged_archive.replace(archive)
        staged_formula.replace(formula)
    return {
        "archive": str(archive),
        "formula": str(formula),
        "sha256": digest,
        "version": version,
        "platform": platform,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                package_host(
                    Path(__file__).resolve().parents[2], args.executable, args.destination
                ),
                sort_keys=True,
            )
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
