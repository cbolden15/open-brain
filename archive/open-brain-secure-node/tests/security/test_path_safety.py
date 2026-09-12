from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    atomic_write_new,
    resolve_generated_path,
)
from open_brain_engine.storage.sqlite import connect_database

from open_brain.integrations.ports import PageDocument, PageReadRequest
from open_brain.integrations.ui import UiHandler, UiRequest


class UiLookupRecorder:
    def __init__(self) -> None:
        self.requests: list[PageReadRequest] = []

    def read(self, request: PageReadRequest) -> PageDocument | None:
        self.requests.append(request)
        return None


@pytest.mark.parametrize(
    "relative",
    [
        "../escape.json",
        "/absolute.json",
        "raw//empty.json",
        "raw/./dot.json",
        "raw\\separator.json",
        "raw/nul" + "\x00" + ".json",
    ],
)
def test_path_traversal_is_rejected(tmp_path: Path, relative: str) -> None:
    with pytest.raises(RootConfinementError):
        atomic_write_new(root=tmp_path, relative=relative, data=b"synthetic")


def test_symlinked_intermediate_and_final_targets_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RootConfinementError):
        atomic_write_new(root=tmp_path, relative="linked/value.json", data=b"synthetic")

    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "value.json").symlink_to(outside / "escaped.json")
    with pytest.raises(RootConfinementError):
        atomic_write_new(root=tmp_path, relative="safe/value.json", data=b"synthetic")
    assert not (outside / "escaped.json").exists()


def test_symlink_root_is_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual, target_is_directory=True)

    with pytest.raises(RootConfinementError):
        atomic_write_new(root=linked_root, relative="safe/value.json", data=b"synthetic")


def test_safe_existing_relative_path_resolves_beneath_root(tmp_path: Path) -> None:
    (tmp_path / "safe").mkdir(mode=0o700)
    resolved = resolve_generated_path(tmp_path, "safe/value.json")

    assert os.path.commonpath((tmp_path, resolved)) == str(tmp_path)


def test_generated_path_rejects_a_final_symlink(tmp_path: Path) -> None:
    safe = tmp_path / "safe"
    safe.mkdir()
    target = tmp_path / "target.json"
    (safe / "value.json").symlink_to(target)

    with pytest.raises(RootConfinementError):
        resolve_generated_path(tmp_path, "safe/value.json")


def test_percent_encoded_traversal_database_name_is_rejected_before_io(
    tmp_path: Path,
) -> None:
    database_name = "%" + "2e" + "%" + "2e" + "%" + "2f" + "events.sqlite3"
    connection: sqlite3.Connection | None = None

    try:
        with pytest.raises(RootConfinementError):
            connection = connect_database(root=tmp_path, database_name=database_name)
    finally:
        if connection is not None:
            connection.close()

    assert not (tmp_path / database_name).exists()


@pytest.mark.parametrize(
    "identifier",
    ["", "../escape", "%2e%2e%2fescape", "nested/page", "nested\\page"],
)
def test_ui_rejects_non_opaque_page_identifiers_before_lookup(identifier: str) -> None:
    reader = UiLookupRecorder()
    handler = UiHandler(expected_bearer_token="synthetic-ui-token", page_reader=reader)

    response = handler.handle(
        UiRequest(
            method="GET",
            path=f"/pages/{identifier}",
            headers=(("Authorization", "Bearer synthetic-ui-token"),),
        )
    )

    assert response.status == 404
    assert reader.requests == []
