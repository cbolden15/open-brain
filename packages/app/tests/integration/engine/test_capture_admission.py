from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    AdmissionLimits,
    BrainEngine,
    CaptureAdmissionError,
    CaptureAdmissionResult,
    FilePayload,
    TextPayload,
)

from open_brain.profile import compile_single_user_local

ENVELOPE_ONLY_LIMITS = AdmissionLimits(max_envelope_bytes=64, max_body_bytes=64)
BODY_ONLY_LIMITS = AdmissionLimits(max_envelope_bytes=8192, max_body_bytes=64)


def _engine(root: Path, *, limits: AdmissionLimits | None = None) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(root), admission_limits=limits)


def _count(root: Path, table: str) -> int:
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        return cast(int, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _capture_artifacts(root: Path) -> int:
    """Count admitted blob and capture-record files; bootstrap writes logical-sources.json."""
    total = 0
    for relative in ("blobs", "captures"):
        tree = root / "sources" / relative
        if tree.exists():
            total += sum(1 for path in tree.rglob("*") if path.is_file())
    return total


def _assert_nothing_was_admitted(root: Path) -> None:
    assert _count(root, "captures") == 0
    assert _count(root, "search_documents") == 0
    assert _capture_artifacts(root) == 0


def test_oversized_envelope_text_capture_is_refused_before_materialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=ENVELOPE_ONLY_LIMITS)
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.accept(TextPayload("synthetic"), delivery_id="admission-envelope-1")
    assert raised.value.result is CaptureAdmissionResult.ENVELOPE_TOO_LARGE
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_oversized_body_text_capture_is_refused_before_materialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=BODY_ONLY_LIMITS)
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.accept(
            TextPayload("synthetic-body " * 10), delivery_id="admission-text-body-1"
        )
    assert raised.value.result is CaptureAdmissionResult.BODY_TOO_LARGE
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_oversized_file_capture_is_refused_before_materialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=BODY_ONLY_LIMITS)
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.accept(
            FilePayload("synthetic.bin", "application/octet-stream", b"s" * 128),
            delivery_id="admission-file-1",
        )
    assert raised.value.result is CaptureAdmissionResult.BODY_TOO_LARGE
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_the_same_submissions_succeed_under_default_limits(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    text_receipt = engine.capture.accept(
        TextPayload("synthetic-body " * 10), delivery_id="admission-text-body-1"
    )
    file_receipt = engine.capture.accept(
        FilePayload("synthetic.bin", "application/octet-stream", b"s" * 128),
        delivery_id="admission-file-1",
    )
    assert text_receipt.duplicate is False
    assert file_receipt.duplicate is False
    assert _count(root, "captures") == 2
    assert _count(root, "search_documents") >= 1
    assert _capture_artifacts(root) >= 1


def test_markdown_import_of_an_oversized_note_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "oversized.md").write_text(
        "# Oversized\n" + "synthetic-import-body " * 12, encoding="utf-8"
    )
    engine = _engine(root, limits=BODY_ONLY_LIMITS)
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.markdown_import.import_directory(str(vault), confirm=lambda _: True)
    assert raised.value.result is CaptureAdmissionResult.BODY_TOO_LARGE
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_markdown_import_of_a_small_note_still_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "small.md").write_text("# Tiny\nsynthetic-tiny-token\n", encoding="utf-8")
    engine = _engine(root, limits=BODY_ONLY_LIMITS)
    summary = engine.markdown_import.import_directory(str(vault), confirm=lambda _: True)
    assert summary.imported == 1
    assert _count(root, "captures") == 1
    assert _count(root, "search_documents") >= 1
    assert engine.retrieval.search("synthetic-tiny-token") != ()
