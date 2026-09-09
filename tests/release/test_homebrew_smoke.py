"""Exercise the production shell with an isolated, command-recording Homebrew."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from tools.open_brain_dev.base_native import ReleaseArtifact, write_release_manifest
from tools.open_brain_dev.homebrew_smoke import MARKER, OWNERSHIP

ROOT = Path(__file__).parents[2]
TAP = "open-brain-local/smoke"
FORMULA = f"{TAP}/open-brain-smoke"
PRODUCT = "example/tap/open-brain"

# Only brew and the expensive final journey are modeled. Formula rendering, guard,
# shell traps, temporary tap git creation, and cleanup execute production code.
FAKE = r"""
import json, os, pathlib, shutil, signal, subprocess, sys
root = pathlib.Path(os.environ["FAKE_BREW_ROOT"])
a = sys.argv[1:]
tap = "open-brain-local/smoke"
formula = tap + "/open-brain-smoke"
product = "example/tap/open-brain"
repo = root / "tap"
state_path = root / "installed.json"
state = json.loads(state_path.read_text())
mode = os.environ.get("FAKE_MODE", "success")
if pathlib.Path(sys.argv[0]).name == "python":
    if a[:3] != ["-m", "tools.open_brain_dev.base_native", "smoke"]:
        os.execv(os.environ["REAL_PYTHON"], [os.environ["REAL_PYTHON"], *a])
    assert a[-2:] == ["--artifact", str(root / "smoke/bin/open-brain")], a
    (root / "journey").write_text(json.dumps(a))
    if mode == "mutate":
        (root / "product/bin/open-brain").write_text("changed")
    if mode == "mutate-link":
        (root / "global/bin/open-brain").unlink()
        (root / "global/bin/open-brain").symlink_to(root / "smoke/bin/open-brain")
    if mode == "mutate-version":
        (root / "changed-version").touch()
    if mode == "wait":
        (root / "ready").touch()
        while True:
            signal.pause()
    sys.exit(7 if mode == "fail-smoke" else 0)
with (root / "commands.jsonl").open("a") as out:
    out.write(json.dumps(a) + "\n")
assert a[0] not in ("link", "unlink"), a
if a[0] == "uninstall":
    assert a == ["uninstall", "--force", formula], a
    expected = b"tap=open-brain-local/smoke\nformula=open-brain-smoke\nversion=1\n"
    assert (repo / ".open-brain-smoke-owned").read_bytes() == expected
    state.remove(formula)
    state_path.write_text(json.dumps(state))
    shutil.rmtree(root / "smoke", ignore_errors=True)
elif a[0] == "untap":
    assert a == ["untap", tap], a
    assert not any(n.startswith(tap + "/") for n in state)
    shutil.rmtree(repo)
elif a == ["list", "--formula", "--full-name"]:
    print("\n".join(state))
elif a == ["list", "--formula", "--versions", product]:
    assert product in state
    print(product + (" 0.0.10" if (root / "changed-version").exists() else " 0.0.9"))
elif a == ["--repository", tap]:
    print(repo)
elif a[0] == "--prefix":
    if len(a) == 1:
        print(root / "global")
    else:
        assert a[1] in state, a
        print(root / ("product" if a[1] == product else "smoke"))
elif a[0] == "tap":
    assert a[1] == tap and a[2].startswith("file://") and not repo.exists(), a
    shutil.copytree(a[2][7:], repo)
    (root / "temp-source").write_text(a[2][7:])
elif a == ["install", formula]:
    text = (repo / "Formula/open-brain-smoke.rb").read_text()
    assert "class OpenBrainSmoke < Formula" in text and "keg_only" in text
    assert 'bin.install "open-brain"' in text
    assert formula not in state
    state.append(formula)
    state_path.write_text(json.dumps(state))
    binary = root / "smoke/bin/open-brain"
    binary.parent.mkdir(parents=True)
    binary.write_text("synthetic smoke")
    if mode == "fail-install":
        sys.exit(8)
else:
    raise AssertionError(a)
"""


class Harness:
    def __init__(self, root: Path, *, product: bool = True) -> None:
        self.root = root
        self.bin = root / "commands"
        self.bin.mkdir()
        for name in ("brew", "python"):
            script = self.bin / name
            script.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
            script.chmod(0o755)
        (root / "installed.json").write_text(json.dumps([PRODUCT] if product else []))
        (root / "global/bin").mkdir(parents=True)
        if product:
            binary = root / "product/bin/open-brain"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"original synthetic product")
            (root / "global/bin/open-brain").symlink_to(binary)
        self.manifest = write_release_manifest(
            (
                ReleaseArtifact(
                    "0.1.0", "macos-arm64", "a" * 64, "open-brain-0.1.0-macos-arm64.tar.gz"
                ),
            ),
            root / "manifest.txt",
        )
        (root / "temp").mkdir()

    def command(self) -> list[str]:
        return [
            "bash",
            str(ROOT / "tools/homebrew-smoke.sh"),
            str(ROOT),
            str(self.manifest),
            str(self.root),
        ]

    def environment(self, mode: str) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "REAL_PYTHON": sys.executable,
            "FAKE_BREW_ROOT": str(self.root),
            "FAKE_MODE": mode,
            "TMPDIR": str(self.root / "temp"),
        }

    def run(self, mode: str = "success") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(),
            env=self.environment(mode),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def commands(self) -> list[list[str]]:
        return [
            json.loads(line) for line in (self.root / "commands.jsonl").read_text().splitlines()
        ]

    def assert_preserved(self, *, product: bool = True) -> None:
        assert json.loads((self.root / "installed.json").read_text()) == (
            [PRODUCT] if product else []
        )
        assert not (self.root / "tap").exists()
        if product:
            binary = self.root / "product/bin/open-brain"
            assert binary.read_bytes() == b"original synthetic product"
            assert (self.root / "global/bin/open-brain").readlink() == binary
        else:
            assert not (self.root / "global/bin/open-brain").exists()
        for command in self.commands():
            assert command[0] not in ("link", "unlink")
            if command[0] == "uninstall":
                assert command == ["uninstall", "--force", FORMULA]
            if command[0] == "untap":
                assert command == ["untap", TAP]


@pytest.mark.parametrize("product", [True, False])
@pytest.mark.parametrize("mode", ["success", "fail-install", "fail-smoke"])
def test_shell_preserves_product_and_cleans_owned_state(
    tmp_path: Path,
    product: bool,
    mode: str,
) -> None:
    harness = Harness(tmp_path, product=product)
    result = harness.run(mode)
    assert result.returncode == {"success": 0, "fail-install": 8, "fail-smoke": 7}[mode], (
        result.stderr
    )
    harness.assert_preserved(product=product)
    assert not list((tmp_path / "temp").iterdir())
    assert f"existing_product: {'preserved' if product else 'absent'}" in result.stdout


@pytest.mark.parametrize("foreign", ["unmarked", "symlink-marker", "another-formula"])
def test_shell_refuses_foreign_tap_without_destructive_commands(
    tmp_path: Path, foreign: str
) -> None:
    harness = Harness(tmp_path)
    tap = tmp_path / "tap"
    tap.mkdir()
    marker = tap / MARKER
    if foreign == "symlink-marker":
        target = tmp_path / "foreign-marker"
        target.write_bytes(OWNERSHIP)
        marker.symlink_to(target)
    elif foreign == "another-formula":
        marker.write_bytes(OWNERSHIP)
        (tmp_path / "installed.json").write_text(json.dumps([PRODUCT, f"{TAP}/foreign"]))
    result = harness.run()
    assert result.returncode != 0
    assert "refusing cleanup" in result.stderr
    assert tap.is_dir()
    assert not any(c[0] in ("uninstall", "untap", "install", "tap") for c in harness.commands())


@pytest.mark.parametrize("formula", [True, False])
def test_shell_recovers_owned_startup_residue(tmp_path: Path, formula: bool) -> None:
    harness = Harness(tmp_path)
    tap = tmp_path / "tap"
    tap.mkdir()
    (tap / MARKER).write_bytes(OWNERSHIP)
    if formula:
        (tmp_path / "installed.json").write_text(json.dumps([PRODUCT, FORMULA]))
    result = harness.run()
    assert result.returncode == 0, result.stderr
    harness.assert_preserved()
    commands = harness.commands()
    assert commands.index(["untap", TAP]) < next(i for i, c in enumerate(commands) if c[0] == "tap")


@pytest.mark.parametrize("mode", ["mutate", "mutate-link", "mutate-version"])
def test_shell_detects_product_mutation(tmp_path: Path, mode: str) -> None:
    harness = Harness(tmp_path)
    result = harness.run(mode)
    assert result.returncode == 1
    assert "existing Open Brain installation changed" in result.stderr
    assert not (tmp_path / "tap").exists()


@pytest.mark.parametrize("sent_signal", [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])
def test_shell_signal_teardown_and_interrupted_run_recovery(
    tmp_path: Path,
    sent_signal: signal.Signals,
) -> None:
    harness = Harness(tmp_path)
    process = subprocess.Popen(
        harness.command(),
        env=harness.environment("wait"),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "ready").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline, "fake journey did not start"
            time.sleep(0.02)
        os.killpg(process.pid, sent_signal)
        stdout, stderr = process.communicate(timeout=15)
        if sent_signal == signal.SIGKILL:
            assert process.returncode == -signal.SIGKILL
            assert (tmp_path / "tap").exists()
            result = harness.run()
            assert result.returncode == 0, result.stderr
        else:
            assert process.returncode == 128 + sent_signal, (stdout, stderr)
            assert not list((tmp_path / "temp").iterdir())
        harness.assert_preserved()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)


@pytest.mark.parametrize("package", ["app", "engine", "connectors"])
def test_shipped_metadata_urls_are_canonical(package: str) -> None:
    project = tomllib.loads((ROOT / f"packages/{package}/pyproject.toml").read_text())["project"]
    origin = "https://github.com/cbolden15/open-brain"
    assert project["urls"] == {
        "Homepage": origin,
        "Repository": origin,
        "Documentation": origin + "/blob/main/README.md",
        "Issues": origin + "/issues",
        "Changelog": origin + "/blob/main/CHANGELOG.md",
    }
    assert (ROOT / "CHANGELOG.md").is_file()
