from __future__ import annotations

import os
from pathlib import Path

import pytest
from open_brain_engine.engine.contracts import (
    AdmissionLimits,
    CaptureAdmissionError,
    CaptureAdmissionResult,
)
from open_brain_engine.storage.watermarks import (
    StorageUsage,
    classify_storage,
    probe_storage_usage,
)

DEFAULT_LIMITS = AdmissionLimits()
_GIB = 1024 * 1024 * 1024
_MIB = 1024 * 1024


def _usage_with_free(free_bytes: int, total_bytes: int = 100 * _GIB) -> StorageUsage:
    return StorageUsage(
        total_bytes=total_bytes, used_bytes=total_bytes - free_bytes, free_bytes=free_bytes
    )


def test_classification_boundaries_at_the_exact_free_byte_thresholds() -> None:
    exactly_high = classify_storage(
        _usage_with_free(DEFAULT_LIMITS.storage_high_free_bytes), DEFAULT_LIMITS
    )
    one_below_high = classify_storage(
        _usage_with_free(DEFAULT_LIMITS.storage_high_free_bytes - 1), DEFAULT_LIMITS
    )
    exactly_critical = classify_storage(
        _usage_with_free(DEFAULT_LIMITS.storage_critical_free_bytes), DEFAULT_LIMITS
    )
    one_below_critical = classify_storage(
        _usage_with_free(DEFAULT_LIMITS.storage_critical_free_bytes - 1), DEFAULT_LIMITS
    )
    assert exactly_high is None
    assert one_below_high is CaptureAdmissionResult.STORAGE_HIGH
    assert exactly_critical is CaptureAdmissionResult.STORAGE_HIGH
    assert one_below_critical is CaptureAdmissionResult.STORAGE_CRITICAL


def test_a_large_mostly_full_disk_with_generous_free_bytes_is_admitted() -> None:
    usage = StorageUsage(total_bytes=460 * _GIB, used_bytes=390 * _GIB, free_bytes=70 * _GIB)
    assert classify_storage(usage, DEFAULT_LIMITS) is None


def test_storage_high_is_retryable_and_critical_is_terminal() -> None:
    assert CaptureAdmissionError(CaptureAdmissionResult.STORAGE_HIGH).retryable is True
    assert CaptureAdmissionError(CaptureAdmissionResult.STORAGE_CRITICAL).retryable is False


def test_storage_usage_keeps_ratio_inside_the_unit_interval() -> None:
    usage = StorageUsage(total_bytes=100, used_bytes=37, free_bytes=63)
    assert usage.ratio == pytest.approx(0.37)


def test_invalid_storage_usage_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        StorageUsage(total_bytes=0, used_bytes=0, free_bytes=0)
    with pytest.raises(ValueError):
        StorageUsage(total_bytes=100, used_bytes=-1, free_bytes=101)
    with pytest.raises(ValueError):
        StorageUsage(total_bytes=100, used_bytes=101, free_bytes=-1)
    with pytest.raises(ValueError):
        StorageUsage(total_bytes=100, used_bytes=50, free_bytes=101)


def test_real_probe_returns_bounded_usage_for_the_probed_root(tmp_path: Path) -> None:
    usage = probe_storage_usage(tmp_path)
    assert usage.total_bytes > 0
    assert 0 <= usage.used_bytes <= usage.total_bytes
    assert 0 <= usage.free_bytes <= usage.total_bytes
    assert 0.0 <= usage.ratio <= 1.0


def test_probe_without_statvfs_raises_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "statvfs")
    with pytest.raises(RuntimeError, match="statvfs"):
        probe_storage_usage(tmp_path)
