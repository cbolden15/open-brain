"""One hermetic storage-usage default for the whole app test tree.

The engine default probes the real Brain-root filesystem, so a development
disk with less free space than the admission watermark would turn unrelated
tests into storage refusals. This autouse default keeps the suite
disk-independent; watermark tests inject their own engine probe, and the
real probe is covered directly by its unit test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from open_brain_engine.storage import watermarks


def _healthy_probe(path: Path) -> watermarks.StorageUsage:
    total_bytes = 100 * 1024 * 1024 * 1024
    used_bytes = 40 * 1024 * 1024 * 1024
    return watermarks.StorageUsage(
        total_bytes=total_bytes, used_bytes=used_bytes, free_bytes=total_bytes - used_bytes
    )


@pytest.fixture(autouse=True)
def _hermetic_storage_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watermarks, "probe_storage_usage", _healthy_probe)
