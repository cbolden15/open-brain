"""Reference vectors for the ignored parity test, from the G4 `OrtEmbedder`.

Run only with the isolated probe interpreter:

    build/g4-ref/bin/python -I parity_reference.py REFERENCE ASSETS INPUT OUTPUT

INPUT is a JSON list of {"purpose", "texts"} requests. OUTPUT is a flat JSON list
with one row per chunk, in request/item/chunk order:
[token_count, token_start, token_end, byte_start, byte_end, *vector].
"""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

BATCH_ROWS = 16  # the helper runs at most this many rows per session call


def load_reference(path: Path) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("g4_reference", str(path))
    spec = importlib.util.spec_from_loader("g4_reference", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def main() -> int:
    reference, assets, source, target = (Path(a) for a in sys.argv[1:5])
    ref = load_reference(reference)
    profile = ref.PROFILES["minilm"]
    embedder = ref.OrtEmbedder(assets, profile, threads=2)
    rows: list[list[Any]] = []
    for request in json.loads(source.read_text(encoding="utf-8")):
        id_lists: list[list[int]] = []
        meta: list[list[int]] = []
        for text in request["texts"]:
            enc = embedder.tok.encode(text, add_special_tokens=False)
            ids = list(enc.ids)
            # Python tokenizers reports character offsets; the helper reports UTF-8 bytes.
            byte_at = [0]
            for ch in text:
                byte_at.append(byte_at[-1] + len(ch.encode("utf-8")))
            offsets = [(byte_at[s], byte_at[e]) for s, e in enc.offsets]
            spans = embedder.windows(text, request["purpose"])
            start = 0
            for span in spans:
                end = start + len(span)
                assert ids[start:end] == span
                byte_start = min(o[0] for o in offsets[start:end])
                byte_end = max(o[1] for o in offsets[start:end])
                id_lists.append(span)
                meta.append([len(ids), start, end, byte_start, byte_end])
                start += profile["stride"]
        vectors: list[list[float]] = []
        for i in range(0, len(id_lists), BATCH_ROWS):
            vectors.extend(embedder.run(id_lists[i : i + BATCH_ROWS]).tolist())
        rows.extend(m + v for m, v in zip(meta, vectors, strict=True))
    target.write_text(json.dumps(rows), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
