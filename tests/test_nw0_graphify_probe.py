"""Failure controls for the experimental build supervisor and artifact envelope."""

import os
import sys
import tarfile
import time
from pathlib import Path

import pytest

from tools.nw0_graphify_probe.run import run, write_archive


def test_build_log_overflow_rejects_and_caps_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        run(
            [sys.executable, "-c", "print('x'*10000)"],
            tmp_path,
            {},
            "overflow",
            max_log_bytes=100,
            timeout=3,
        )
    assert (tmp_path / "overflow.log").stat().st_size == 100


def test_build_timeout_kills_descendant_holding_pipe(tmp_path: Path) -> None:
    marker = tmp_path / "escaped"
    grandchild = f"import time;time.sleep(1);open({str(marker)!r},'w').write('escaped')"
    parent = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{grandchild!r}])"
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        run([sys.executable, "-c", parent], tmp_path, {}, "timeout", timeout=0.3)
    assert time.monotonic() - started < 2
    time.sleep(1.1)
    assert not marker.exists()


def test_build_success_and_failure(tmp_path: Path) -> None:
    run([sys.executable, "-c", "print('ok')"], tmp_path, {}, "success", timeout=3)
    assert (tmp_path / "success.log").read_text() == "ok\n"
    with pytest.raises(RuntimeError):
        run([sys.executable, "-c", "raise SystemExit(4)"], tmp_path, {}, "exit", timeout=3)


def test_helper_archive_identity_notices_and_reproducibility(tmp_path: Path) -> None:
    helper = tmp_path / "helper"
    helper.write_bytes(b"synthetic executable")
    one, two = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    write_archive(helper, one)
    os.utime(helper, (100, 100))
    write_archive(helper, two)
    assert one.read_bytes() == two.read_bytes()
    with tarfile.open(one) as archive:
        assert set(archive.getnames()) == {
            "open-brain-graphify",
            "licenses/graphify/LICENSE",
            "licenses/graphify/LICENSE-MIT",
            "licenses/graphify/NOTICE",
            "licenses/graphify/OPEN-BRAIN-NW0-NOTICE",
        }
        assert archive.getmember("open-brain-graphify").mode == 0o755
        assert all(member.uid == member.gid == member.mtime == 0 for member in archive.getmembers())
