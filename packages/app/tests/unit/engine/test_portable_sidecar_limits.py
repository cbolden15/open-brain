"""Portable sidecar byte limits: one shared value, and a named refusal over it."""

import pytest
from open_brain_engine.portable import v1, v8, v8_capture_metadata, v8_custody, v9
from open_brain_engine.portable.v1 import PortableSidecarTooLarge

LIMIT = 256 * 1024 * 1024


def test_every_whole_file_sidecar_shares_the_256_mib_limit() -> None:
    assert v1.MAX_PORTABLE_SIDECAR_BYTES == LIMIT
    assert v8_capture_metadata.MAX_CAPTURE_METADATA_BYTES == LIMIT
    assert v8_custody.MAX_CUSTODY_BYTES == LIMIT
    assert v8.MAX_HISTORICAL_SIDECAR_BYTES == LIMIT
    assert v9.MAX_HISTORICAL_SIDECAR_BYTES == LIMIT


def test_too_large_error_names_the_sidecar_size_and_limit() -> None:
    error = PortableSidecarTooLarge("history/example.json", 11, 10)
    assert isinstance(error, ValueError)
    assert (error.path, error.size, error.limit) == ("history/example.json", 11, 10)
    assert "history/example.json" in str(error) and "11" in str(error) and "10" in str(error)


def test_capture_metadata_writer_refuses_over_the_limit_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v8_capture_metadata, "MAX_CAPTURE_METADATA_BYTES", 10)
    with pytest.raises(PortableSidecarTooLarge) as caught:
        v8_capture_metadata.capture_metadata_bytes([])
    assert caught.value.path == v8_capture_metadata.CAPTURE_METADATA_PATH
    assert caught.value.limit == 10 and caught.value.size > 10


def test_custody_writer_refuses_over_the_limit_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v8_custody, "MAX_CUSTODY_BYTES", 10)
    with pytest.raises(PortableSidecarTooLarge) as caught:
        v8_custody.custody_bytes({"schema_version": 1, "sequences": {}})
    assert caught.value.path == v8_custody.CUSTODY_PATH
    assert caught.value.limit == 10 and caught.value.size > 10
