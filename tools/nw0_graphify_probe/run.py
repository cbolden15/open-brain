"""Build and verify the bounded Graphify proof on a native supported runner."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from contextlib import suppress
from pathlib import Path

from tools.open_brain_dev import artifact_audit, base_native

SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    command: list[str],
    output: Path,
    environment: dict[str, str],
    label: str,
    *,
    timeout: float = 600,
    max_log_bytes: int = 4 * 1024 * 1024,
) -> None:
    deadline = time.monotonic() + timeout
    failed = False
    written = 0
    child = subprocess.Popen(
        command,
        cwd=REPO,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert child.stdout is not None
    try:
        with selectors.DefaultSelector() as selector, (output / (label + ".log")).open("wb") as log:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map() or child.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failed = True
                    break
                for key, _ in selector.select(min(0.1, remaining)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fd)
                    else:
                        log.write(data[: max_log_bytes - written])
                        written += len(data)
                        if written > max_log_bytes:
                            failed = True
                            break
                if failed:
                    break
    finally:
        # Kill the owned group even if its leader exited while a descendant kept a pipe open.
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.stdout.close()
        child.wait(timeout=2)
    if failed or child.returncode:
        raise RuntimeError(f"{label} failed; inspect its build-directory log")


def write_archive(helper: Path, archive: Path) -> None:
    members = {"open-brain-graphify": helper}
    members.update(
        {"licenses/graphify/" + path.name: path for path in (SOURCE / "licenses").iterdir()}
    )
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle,
    ):
        for name, source in sorted(members.items()):
            with source.open("rb") as stream:
                info = tarfile.TarInfo(name)
                info.size = source.stat().st_size
                info.mode = 0o755 if name == "open-brain-graphify" else 0o644
                bundle.addfile(info, stream)


def prepare(output: Path, environment: dict[str, str]) -> None:
    inputs = output / "inputs"
    run(
        [
            "uv",
            "--no-config",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(inputs),
            "--no-deps",
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--index-url",
            "https://pypi.org/simple",
            "-r",
            str(SOURCE / "requirements.txt"),
        ],
        output,
        environment,
        "install",
    )
    manifest = json.loads((SOURCE / "patch-manifest.json").read_text())
    for row in manifest:
        target = inputs / row["path"]
        original = digest(target) if target.exists() else None
        if original != row["original_sha256"]:
            raise ValueError("upstream source preimage mismatch")
        if row["changed"]:
            replacement = SOURCE / "overrides" / (row["path"] + ".txt")
            if digest(replacement) != row["candidate_sha256"]:
                raise ValueError("maintained patch digest mismatch")
            shutil.copyfile(replacement, target)
        if digest(target) != row["candidate_sha256"]:
            raise ValueError("staged source digest mismatch")
    for source in (SOURCE / "payload").glob("*.py.txt"):
        shutil.copyfile(source, output / source.name.removesuffix(".txt"))
    shutil.copytree(SOURCE / "fixtures", output, dirs_exist_ok=True)
    shutil.copyfile(SOURCE / "helper.spec", output / "helper.spec")


def audit(output: Path, baseline: Path) -> None:
    helper = output / "native/dist/open-brain-graphify"
    platform = base_native.native_platform_tag()
    signature = base_native._validate_native_executable(helper, platform)
    modules = base_native.archive_modules(helper)
    expected = {
        "graphify",
        "graphify.discovery_rules",
        "graphify.extractors",
        "graphify.extractors.base",
        "graphify.extractors.markdown",
        "graphify.ids",
        "graphify.metadata",
    }
    actual = {name for name in modules if name == "graphify" or name.startswith("graphify.")}
    forbidden = (
        *base_native._FORBIDDEN_MODULE_PREFIXES,
        "open_brain",
        "numpy",
        "networkx",
        "tree_sitter",
        "yaml._yaml",
        "yaml.cyaml",
        "frontmatter_probe",
    )
    if actual != expected or any(name.startswith(forbidden) for name in modules):
        raise ValueError("helper module closure mismatch")
    release = output / "release"
    release.mkdir()
    archive = release / f"open-brain-graphify-proof-{platform}.tar.gz"
    write_archive(helper, archive)
    repeat = output / archive.name
    write_archive(helper, repeat)
    if digest(archive) != digest(repeat):
        raise ValueError("archive is not reproducible from identical executable bytes")
    # This is the public generic audit. Owner-private deny terms are a separate gate.
    findings = artifact_audit.inspect_artifact(archive, ())
    if findings:
        raise ValueError(f"generic content audit failed: {findings!r}")
    result = {
        "passed": True,
        "platform": platform,
        "signature": signature,
        "helper_sha256": digest(helper),
        "archive_sha256": digest(archive),
        "archive_bytes": archive.stat().st_size,
        "modules": modules,
        "base_audit": base_native.audit_base_artifact(baseline).to_dict(),
        "model_calls": 0,
        "private_denylist_audit": False,
        "cold_startup_measured": False,
        "shipping_integration": False,
        "archive_reproducible_from_same_binary": True,
    }
    (release / "artifact-verification.json").write_text(json.dumps(result, indent=2) + "\n")
    shutil.copyfile(output / "runtime-verification.json", release / "runtime-verification.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / "build/nw0-graphify")
    parser.add_argument("--base", type=Path, default=REPO / "build/native/dist/open-brain")
    args = parser.parse_args()
    baseline = args.base.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "home").mkdir()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(output / "home"),
        "LANG": "en_US.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }
    prepare(output, environment)
    environment["PYTHONPATH"] = os.pathsep.join((str(output / "inputs"), str(output), str(REPO)))
    run(
        [sys.executable, "-m", "unittest", "test_helper", "test_syntax"],
        output,
        environment,
        "source-tests",
    )
    run(
        [sys.executable, str(output / "frontmatter_probe.py"), str(output)],
        output,
        environment,
        "frontmatter-source-tests",
    )
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(output / "native/dist"),
            "--workpath",
            str(output / "native/work"),
            str(output / "helper.spec"),
        ],
        output,
        environment,
        "freeze",
    )
    run(
        [sys.executable, str(output / "native_runtime.py"), str(baseline)],
        output,
        environment,
        "native-tests",
    )
    audit(output, baseline)
    print("NW0 Graphify native proof passed. Evidence: " + str(output / "release"))


if __name__ == "__main__":
    main()
