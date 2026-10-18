"""Build an embedding helper staging directory for tests and benchmarks.

The layout matches the fixed staged layout the helper reads from its working
directory: the executable (ONNX Runtime linked in), ``model/model.onnx``, ``tokenizer/`` and a
canonical ``component.json``: by default the probe's asset-hash document, or, given an
inventory, the manifest ``component`` object itself with no trailing newline (so the
helper's component ID equals the manifest ``component_id``). This is a contributor tool;
it does not perform the installed-resource verification the product runs before staging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

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


def component_bytes(inventory: Path) -> bytes:
    """Canonical bytes of the embedding ``component`` object of an inventory file."""
    # Imported here: the parity test runs this file with ``python -I`` outside the workspace.
    from tools.open_brain_dev.component_manifest_v2 import ComponentManifestError, parse_inventory

    try:
        component = parse_inventory(inventory.read_bytes()).component
    except (OSError, ComponentManifestError) as error:
        raise StageError(f"invalid inventory: {inventory}") from error
    if component is None:
        raise StageError(f"inventory has no component: {inventory}")
    return canonical_json(component).removesuffix(b"\n")


def stage(
    output: Path,
    *,
    helper: Path,
    assets: Path,
    inventory: Path | None = None,
) -> Path:
    """Create ``output`` (which must not exist) and return it."""
    sources = {staged: assets / relative for staged, relative in MODEL_FILES.items()}
    for source in (helper, *sources.values()):
        if not source.is_file():
            raise StageError(f"missing input file: {source}")
    document = component_bytes(inventory) if inventory is not None else None
    if output.exists():
        raise StageError(f"output already exists: {output}")
    output.mkdir(mode=0o700, parents=True)
    os.chmod(output, 0o700)
    _copy(helper, output / HELPER_NAME, 0o500)
    hashes = {}
    for staged, source in sources.items():
        hashes[staged] = _copy(source, output / staged, 0o400)
    if document is None:
        component = {"assets": dict(sorted(hashes.items())), "preprocess": PREPROCESS}
        document = canonical_json(component)
    with open(output / "component.json", "xb") as handle:
        handle.write(document)
    os.chmod(output / "component.json", 0o400)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, help="write the manifest component (D9)")
    args = parser.parse_args(argv)
    try:
        out = stage(
            args.output.resolve(),
            helper=args.helper.resolve(),
            assets=args.assets.resolve(),
            inventory=args.inventory.resolve() if args.inventory else None,
        )
    except StageError as error:
        print(error, file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
