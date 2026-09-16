from __future__ import annotations

import ctypes
import json
import resource
import subprocess
import sys
from pathlib import Path

import pytest
from open_brain_engine.capture.models import ExtractionFailure
from open_brain_engine.core.models import RawAssetRef

from open_brain_connectors.capture import media as media_module
from open_brain_connectors.capture.media import (
    DEFAULT_MEDIA_LIMITS,
    BoundedMediaRunner,
    MediaCommand,
    MediaLimits,
    collect_staged_media,
)

MIB = 1024 * 1024


def _runner(tmp_path: Path) -> BoundedMediaRunner:
    return BoundedMediaRunner(
        allowed_executables=(sys.executable,),
        staging_parent=tmp_path,
    )


def _command(script: str, *, limits: MediaLimits = DEFAULT_MEDIA_LIMITS) -> MediaCommand:
    return MediaCommand(argv=(sys.executable, "-c", script), limits=limits)


def _skip_if_sandbox_apply_denied() -> None:
    probe = subprocess.run(
        (
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(deny default)(allow process-exec)",
            "/usr/bin/true",
        ),
        capture_output=True,
        check=False,
    )
    if (
        probe.returncode == 71
        and b"sandbox-exec: sandbox_apply: Operation not permitted" in probe.stderr
    ):
        pytest.skip("sandbox-exec sandbox_apply denied by the execution environment")


def _enable_synthetic_process_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_module, "_resource_limits_supported", lambda: True)
    monkeypatch.setattr(
        media_module,
        "_sandboxed_media_command",
        lambda argv, stage: argv,
    )


def test_default_media_limits_match_the_brief_exactly() -> None:
    assert (
        MediaLimits(
            wall_seconds=60.0,
            cpu_seconds=30,
            memory_bytes=512 * MIB,
            max_processes=8,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=64 * 1024,
            max_single_file_bytes=50 * MIB,
            max_total_bytes=100 * MIB,
            max_files=8,
            max_videos=1,
        )
        == DEFAULT_MEDIA_LIMITS
    )


def test_darwin_sandbox_profile_denies_user_roots_and_allows_only_stage_write(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    profile = media_module._darwin_sandbox_profile(stage)

    assert "(allow process-exec)" in profile
    assert "(allow process*)" not in profile
    assert '(deny file-read* (subpath "/Users")' in profile
    assert '(subpath "/Volumes")' in profile
    assert '(subpath "/private/var/folders")' in profile
    assert f'(allow file-read* (subpath "{stage}"))' in profile
    assert f'(allow file-write* (subpath "{stage}"))' in profile
    assert "(allow network-outbound)" in profile
    assert "(allow file-write*)" not in profile


def test_linux_media_execution_fails_closed_without_a_verified_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert not media_module._resource_limits_supported()
    assert (
        media_module._sandboxed_media_command(
            ("/synthetic/yt-dlp", "--version"),
            Path("/synthetic/stage"),
        )
        is None
    )


def test_darwin_preexec_uses_parent_memory_monitor_instead_of_rlimit_as(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda kind, value: calls.append((kind, value)),
    )

    media_module._limit_resources(DEFAULT_MEDIA_LIMITS)()

    assert {kind for kind, _ in calls} == {
        resource.RLIMIT_CPU,
        resource.RLIMIT_FSIZE,
        resource.RLIMIT_NPROC,
    }
    assert resource.RLIMIT_AS not in {kind for kind, _ in calls}


def test_darwin_resident_memory_check_fails_closed_and_enforces_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(media_module, "_darwin_resident_bytes", lambda pid: None)
    assert not media_module._memory_within_limit(123, 512)

    monkeypatch.setattr(media_module, "_darwin_resident_bytes", lambda pid: 513)
    assert not media_module._memory_within_limit(123, 512)

    monkeypatch.setattr(media_module, "_darwin_resident_bytes", lambda pid: 512)
    assert media_module._memory_within_limit(123, 512)


def test_darwin_rusage_v2_layout_matches_the_installed_sdk_contract() -> None:
    assert ctypes.sizeof(media_module._DarwinRUsageInfoV2) == 160


def test_runner_binds_logical_tool_name_to_one_absolute_executable(tmp_path: Path) -> None:
    runner = _runner(tmp_path)

    assert runner._resolve_executable(Path(sys.executable).name) == str(
        Path(sys.executable).resolve()
    )
    assert runner._resolve_executable("not-approved") is None

    with pytest.raises(ValueError, match="executable allowlist"):
        BoundedMediaRunner(allowed_executables=("python",), staging_parent=tmp_path)


def test_runner_applies_resource_limits_returns_refs_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_synthetic_process_boundary(monkeypatch)
    script = """
import json, resource
from pathlib import Path
Path("clip.mp4").write_bytes(b"synthetic")
print(json.dumps({
    "cpu": resource.getrlimit(resource.RLIMIT_CPU)[0],
    "memory": resource.getrlimit(resource.RLIMIT_AS)[0],
    "processes": resource.getrlimit(resource.RLIMIT_NPROC)[0],
    "file": resource.getrlimit(resource.RLIMIT_FSIZE)[0],
}))
"""

    result = _runner(tmp_path).run(_command(script))

    assert result.failure is None
    assert result.reaped is True
    assert len(result.assets) == 1
    assert isinstance(result.assets[0], RawAssetRef)
    assert result.assets[0].media_type == "video/mp4"
    limits = json.loads(result.stdout.decode("utf-8"))
    assert limits["cpu"] == 30
    assert limits["processes"] == 8
    assert limits["file"] == 50 * MIB
    if sys.platform.startswith("linux"):
        assert limits["memory"] == 512 * MIB
    assert list(tmp_path.iterdir()) == []
    assert not any(isinstance(value, Path) for value in result.assets)


def test_timeout_kills_and_reaps_process_group_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_synthetic_process_boundary(monkeypatch)
    limits = MediaLimits(wall_seconds=0.05)
    script = (
        "from pathlib import Path; Path('partial.part').write_bytes(b'x'); "
        "import time; time.sleep(5)"
    )

    result = _runner(tmp_path).run(_command(script, limits=limits))

    assert result.failure is ExtractionFailure.TOOL_TIMEOUT
    assert result.reaped is True
    assert list(tmp_path.iterdir()) == []


def test_output_breach_is_closed_reaped_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_synthetic_process_boundary(monkeypatch)
    limits = MediaLimits(max_stdout_bytes=128, max_stderr_bytes=128)

    result = _runner(tmp_path).run(_command("print('x' * 1024)", limits=limits))

    assert result.failure is ExtractionFailure.TOOL_RESOURCE_LIMIT
    assert result.reaped is True
    assert result.stdout == b""
    assert result.stderr == b""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "partial",
    [
        True,
        False,
    ],
)
def test_partial_or_symlinked_staged_output_is_rejected_and_cleaned(
    tmp_path: Path, partial: bool
) -> None:
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    if partial:
        (staging_root / "clip.part").write_bytes(b"x")
    else:
        (staging_root / "target.jpg").write_bytes(b"x")
        (staging_root / "link.jpg").symlink_to("target.jpg")

    assets, failure = collect_staged_media(staging_root)

    assert failure is ExtractionFailure.MALFORMED_TOOL_OUTPUT
    assert assets == ()
    assert not staging_root.exists()


def test_valid_staged_output_becomes_a_ref_and_never_returns_a_path(tmp_path: Path) -> None:
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    (staging_root / "image.jpg").write_bytes(b"synthetic-image")

    assets, failure = collect_staged_media(staging_root)

    assert failure is None
    assert len(assets) == 1
    assert isinstance(assets[0], RawAssetRef)
    assert assets[0].media_type == "image/jpeg"
    assert not any(isinstance(value, Path) for value in assets)
    assert not staging_root.exists()


@pytest.mark.parametrize(
    ("files", "limits"),
    [
        (
            (("large.jpg", 5),),
            MediaLimits(max_single_file_bytes=4, max_total_bytes=20),
        ),
        (
            (("boundary.jpg", 4),),
            MediaLimits(max_single_file_bytes=4, max_total_bytes=20),
        ),
        (
            (("0.jpg", 3), ("1.jpg", 3), ("2.jpg", 3)),
            MediaLimits(max_single_file_bytes=4, max_total_bytes=8),
        ),
        (
            (("0.jpg", 1), ("1.jpg", 1), ("2.jpg", 1)),
            MediaLimits(max_files=2),
        ),
        (
            (("a.mp4", 1), ("b.mp4", 1)),
            MediaLimits(max_videos=1),
        ),
    ],
)
def test_staged_file_limits_are_closed_and_always_cleaned(
    tmp_path: Path, files: tuple[tuple[str, int], ...], limits: MediaLimits
) -> None:
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    for name, size in files:
        (staging_root / name).write_bytes(b"x" * size)

    assets, failure = collect_staged_media(staging_root, limits=limits)

    assert failure is ExtractionFailure.MEDIA_LIMIT
    assert assets == ()
    assert not staging_root.exists()


def test_unallowlisted_executable_fails_closed_without_starting_or_staging(tmp_path: Path) -> None:
    result = _runner(tmp_path).run(MediaCommand(argv=("yt-dlp", "--version")))

    assert result.failure is ExtractionFailure.TOOL_UNAVAILABLE
    assert result.reaped is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS confinement behavior")
def test_macos_runner_uses_sandbox_and_parent_memory_monitor(
    tmp_path: Path,
) -> None:
    _skip_if_sandbox_apply_denied()
    runner = BoundedMediaRunner(
        allowed_executables=("/usr/bin/printf",),
        staging_parent=tmp_path,
    )
    result = runner.run(MediaCommand(argv=("/usr/bin/printf", "synthetic\n")))

    assert result.failure is None
    assert result.stdout == b"synthetic\n"
    assert result.reaped is True
    assert list(tmp_path.iterdir()) == []
