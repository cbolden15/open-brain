"""Staging-directory builder for embedding helper tests and benchmarks."""

import hashlib
import json
import stat
from pathlib import Path

import pytest

from tools.g4_embedding_probe.stage import RUNTIME_NAME, StageError, stage


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    assets = tmp_path / "assets"
    (assets / "onnx").mkdir(parents=True)
    (assets / "onnx" / "model.onnx").write_bytes(b"graph")
    (assets / "tokenizer.json").write_bytes(b"{}")
    (assets / "config.json").write_bytes(b'{"a":1}')
    (assets / "vocab.txt").write_bytes(b"[PAD]\n")
    helper = tmp_path / "helper-bin"
    helper.write_bytes(b"#!helper")
    runtime = tmp_path / "libort.test"
    runtime.write_bytes(b"native")
    return assets, helper, runtime


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_stage_lays_out_files_with_fixed_modes(tmp_path: Path) -> None:
    assets, helper, runtime = _inputs(tmp_path)
    out = stage(tmp_path / "staged", helper=helper, runtime_library=runtime, assets=assets)

    assert out == tmp_path / "staged"
    assert (out / "open-brain-embedding").read_bytes() == b"#!helper"
    assert (out / "runtime" / RUNTIME_NAME).read_bytes() == b"native"
    assert (out / "model" / "model.onnx").read_bytes() == b"graph"
    for name in ("tokenizer.json", "config.json", "vocab.txt"):
        assert (out / "tokenizer" / name).read_bytes() == (assets / name).read_bytes()
    assert _mode(out / "open-brain-embedding") == 0o500
    assert _mode(out / "runtime" / RUNTIME_NAME) == 0o500
    for path in ("model/model.onnx", "tokenizer/tokenizer.json", "component.json"):
        assert _mode(out / path) == 0o400, path
    assert _mode(out) == 0o700


def test_component_json_is_canonical_and_binds_staged_assets(tmp_path: Path) -> None:
    assets, helper, runtime = _inputs(tmp_path)
    out = stage(tmp_path / "staged", helper=helper, runtime_library=runtime, assets=assets)

    raw = (out / "component.json").read_bytes()
    component = json.loads(raw)
    canonical = json.dumps(component, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert raw == canonical.encode("utf-8") + b"\n"
    assert component["preprocess"] == {
        "profile_id": "ob.minilm-wordpiece-mean.v1",
        "dimensions": 384,
        "pooling": "mean",
        "max_query_tokens": 254,
        "document_window_tokens": 224,
        "document_overlap_tokens": 32,
        "cls_token_id": 101,
        "sep_token_id": 102,
        "pad_token_id": 0,
        "query_prefix": "",
    }
    expected = {
        path: hashlib.sha256((out / path).read_bytes()).hexdigest()
        for path in (
            "model/model.onnx",
            f"runtime/{RUNTIME_NAME}",
            "tokenizer/config.json",
            "tokenizer/tokenizer.json",
            "tokenizer/vocab.txt",
        )
    }
    assert component["assets"] == expected
    assert set(component) == {"assets", "preprocess"}


def test_stage_refuses_existing_output(tmp_path: Path) -> None:
    assets, helper, runtime = _inputs(tmp_path)
    (tmp_path / "staged").mkdir()
    with pytest.raises(StageError):
        stage(tmp_path / "staged", helper=helper, runtime_library=runtime, assets=assets)


def test_stage_refuses_missing_asset(tmp_path: Path) -> None:
    assets, helper, runtime = _inputs(tmp_path)
    (assets / "vocab.txt").unlink()
    with pytest.raises(StageError):
        stage(tmp_path / "staged", helper=helper, runtime_library=runtime, assets=assets)
