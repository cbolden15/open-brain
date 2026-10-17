"""Build an embedding helper staging directory for tests and benchmarks.

The layout matches the fixed staged layout the helper reads from its working
directory: the executable, ``runtime/``, ``model/model.onnx``, ``tokenizer/`` and a
canonical ``component.json``. This is a contributor tool; it does not perform the
installed-resource verification the product runs before staging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

RUNTIME_NAME = "libonnxruntime.dylib" if sys.platform == "darwin" else "libonnxruntime.so"
HELPER_NAME = "open-brain-embedding"
PREPROCESS: dict[str, object] = {
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
# Staged path -> path relative to the asset directory.
MODEL_FILES = {
    "model/model.onnx": "onnx/model.onnx",
    "tokenizer/tokenizer.json": "tokenizer.json",
    "tokenizer/config.json": "config.json",
    "tokenizer/vocab.txt": "vocab.txt",
}


class StageError(RuntimeError):
    """The staging directory could not be built from the given inputs."""


def canonical_json(value: object) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8") + b"\n"


def _copy(source: Path, destination: Path, mode: int) -> str:
    data = source.read_bytes()
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    with open(destination, "xb") as handle:
        handle.write(data)
    os.chmod(destination, mode)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def stage(output: Path, *, helper: Path, runtime_library: Path, assets: Path) -> Path:
    """Create ``output`` (which must not exist) and return it."""
    sources = {staged: assets / relative for staged, relative in MODEL_FILES.items()}
    for source in (helper, runtime_library, *sources.values()):
        if not source.is_file():
            raise StageError(f"missing input file: {source}")
    if output.exists():
        raise StageError(f"output already exists: {output}")
    output.mkdir(mode=0o700, parents=True)
    os.chmod(output, 0o700)
    _copy(helper, output / HELPER_NAME, 0o500)
    hashes = {
        f"runtime/{RUNTIME_NAME}": _copy(runtime_library, output / "runtime" / RUNTIME_NAME, 0o500)
    }
    for staged, source in sources.items():
        hashes[staged] = _copy(source, output / staged, 0o400)
    component = {"assets": dict(sorted(hashes.items())), "preprocess": PREPROCESS}
    with open(output / "component.json", "xb") as handle:
        handle.write(canonical_json(component))
    os.chmod(output / "component.json", 0o400)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--runtime-library", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        out = stage(
            args.output.resolve(),
            helper=args.helper.resolve(),
            runtime_library=args.runtime_library.resolve(),
            assets=args.assets.resolve(),
        )
    except StageError as error:
        print(error, file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
