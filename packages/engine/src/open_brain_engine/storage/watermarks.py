"""Brain-root storage watermark probing and classification for capture admission."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from open_brain_engine.engine.contracts import AdmissionLimits, CaptureAdmissionResult


@dataclass(frozen=True, slots=True)
class StorageUsage:
    """One filesystem usage sample; every field stays inside its bounds."""

    total_bytes: int
    used_bytes: int
    free_bytes: int

    def __post_init__(self) -> None:
        if type(self.total_bytes) is not int or self.total_bytes <= 0:
            raise ValueError("invalid storage usage")
        if type(self.used_bytes) is not int or not 0 <= self.used_bytes <= self.total_bytes:
            raise ValueError("invalid storage usage")
        if type(self.free_bytes) is not int or not 0 <= self.free_bytes <= self.total_bytes:
            raise ValueError("invalid storage usage")

    @property
    def ratio(self) -> float:
        """Used bytes over total bytes; only ``free_bytes`` decides a watermark."""
        return self.used_bytes / self.total_bytes


def probe_storage_usage(path: Path) -> StorageUsage:
    """Sample the filesystem holding ``path`` through the standard ``os.statvfs``.

    Portable across macOS and Linux with no third-party dependency. Free
    bytes come from ``f_bavail`` (the space available to this unprivileged
    user), matching what the runtime can actually still write. Where the
    platform lacks ``statvfs``, guessing usage would misclassify a
    watermark, so a clear error is raised instead.
    """
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        raise RuntimeError("storage watermark probing requires os.statvfs on this platform")
    statistics = statvfs(os.fspath(path))
    total_bytes = statistics.f_blocks * statistics.f_frsize
    free_bytes = statistics.f_bavail * statistics.f_frsize
    used_bytes = total_bytes - statistics.f_bfree * statistics.f_frsize
    return StorageUsage(total_bytes=total_bytes, used_bytes=used_bytes, free_bytes=free_bytes)


def classify_storage(usage: StorageUsage, limits: AdmissionLimits) -> CaptureAdmissionResult | None:
    """Classify one usage sample against the configured free-byte watermarks.

    Returns ``storage_critical`` below the critical free-byte watermark,
    then ``storage_high`` below the high free-byte watermark, else ``None``.
    A local notebook with a large but mostly full disk keeps working: the
    ratio of used space never decides a refusal, only the bytes still
    writable by this user.
    """
    # The engine contracts import stays at call time: importing them at
    # module scope would make this the first storage module with a
    # module-level engine dependency and would cycle through
    # ``engine.capture`` whenever watermarks is imported before the engine
    # package finishes initializing.
    from open_brain_engine.engine.contracts import CaptureAdmissionResult

    if usage.free_bytes < limits.storage_critical_free_bytes:
        return CaptureAdmissionResult.STORAGE_CRITICAL
    if usage.free_bytes < limits.storage_high_free_bytes:
        return CaptureAdmissionResult.STORAGE_HIGH
    return None
