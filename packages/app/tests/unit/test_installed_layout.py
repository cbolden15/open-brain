from __future__ import annotations

from pathlib import Path

import pytest

from open_brain.services.installed_layout import (
    BASE_EXECUTABLE,
    BASE_LINK,
    base_prefix,
)


def test_release_layout_is_the_one_folder_executable_with_a_bin_link() -> None:
    assert BASE_EXECUTABLE == "libexec/open-brain/open-brain"
    assert BASE_LINK == "bin/open-brain"


@pytest.mark.parametrize("relative", [BASE_EXECUTABLE, BASE_LINK])
def test_base_prefix_accepts_release_and_one_file_layouts(tmp_path: Path, relative: str) -> None:
    prefix = tmp_path / "Cellar/open-brain/0.1.0"
    assert base_prefix(prefix / relative) == prefix


@pytest.mark.parametrize(
    "relative",
    [
        "libexec/open-brain-harness/open-brain-harness",
        "libexec/open-brain-graphify",
        "libexec/other/open-brain",
        "open-brain/open-brain",
        "bin/open-brain-smoke",
        "lib/open-brain",
    ],
)
def test_base_prefix_rejects_other_locations(tmp_path: Path, relative: str) -> None:
    assert base_prefix(tmp_path / relative) is None


def test_base_prefix_rejects_relative_paths() -> None:
    assert base_prefix(Path(BASE_EXECUTABLE)) is None
    assert base_prefix(Path(BASE_LINK)) is None
