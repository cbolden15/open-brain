"""Synthetic installed embedding prefixes and a scripted fake helper for transport tests.

The fake helper is a small Python script installed as the embedding executable. It reads
``component.json`` from its working directory like the real helper, tokenizes on Unicode
whitespace, applies the frozen window algorithm and emits unit vectors. Each test injects
an ``early`` snippet (sees the first request bytes) and a ``hook`` snippet (sees the parsed
request and the response document) to produce one exact violation. Never a product fallback.
"""

from __future__ import annotations

import hashlib
import os
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path

from open_brain.services import component_manifest_v2 as v2
from open_brain.services import embedding_helper

ROOT = "libexec/open-brain-embedding"
LICENSES = "share/open-brain/licenses/embedding"
VERSION = "0.1.0"
RUNTIME_NAME = "libonnxruntime.dylib" if sys.platform == "darwin" else "libonnxruntime.so"
RUNTIME = f"{ROOT}/runtime/{RUNTIME_NAME}"
EXECUTABLE = f"{ROOT}/open-brain-embedding"
DIRECTORIES = (
    "libexec",
    ROOT,
    f"{ROOT}/model",
    f"{ROOT}/runtime",
    f"{ROOT}/tokenizer",
    "share",
    "share/open-brain",
    "share/open-brain/inventories",
    "share/open-brain/licenses",
    LICENSES,
)
TRIPLES = {"macos-arm64": "aarch64-apple-darwin", "linux-x86_64": "x86_64-unknown-linux-gnu"}

_TEMPLATE = """#!{python} -IS
import hashlib, json, os, sys, time

LIMITS = {limits!r}
WS = set({whitespace!r})


def early(head):
{early}


def hook(op, request, doc, text):
{hook}


def spans(text):
    out, position, start = [], 0, None
    for character in text:
        width = len(character.encode("utf-8"))
        if character in WS:
            if start is not None:
                out.append((start, position))
                start = None
        elif start is None:
            start = position
        position += width
    if start is not None:
        out.append((start, position))
    return out


def windows(count):
    out, start = [], 0
    while True:
        end = min(start + 224, count)
        out.append((start, end))
        if end == count:
            return out
        start += 192


def vector(slot, ordinal):
    values = [0.0] * 384
    index = (slot * 7 + ordinal) % 383
    values[index] = 0.6
    values[index + 1] = 0.8
    return values


def main():
    head = os.read(0, 128)
    early(head)
    raw = head + sys.stdin.buffer.read()
    request = json.loads(raw)
    component_id = hashlib.sha256(open("component.json", "rb").read()).hexdigest()
    base = {{
        "protocol": "open-brain.embedding-helper.v1",
        "request_id": request["request_id"],
        "status": "ok",
        "component_id": component_id,
    }}
    if request["operation"] == "capabilities":
        op = "capabilities"
        doc = dict(base, limits=dict(LIMITS))
    else:
        op = "embed"
        items = []
        for item in request["items"]:
            text = item["text"]
            tokens = spans(text)
            ranges = [(0, len(tokens))] if request["purpose"] == "query" else windows(len(tokens))
            chunks = [
                {{
                    "ordinal": ordinal,
                    "token_start": start,
                    "token_end": end,
                    "byte_start": tokens[start][0],
                    "byte_end": tokens[end - 1][1],
                    "vector": vector(item["slot"], ordinal),
                }}
                for ordinal, (start, end) in enumerate(ranges)
            ]
            items.append(
                {{
                    "slot": item["slot"],
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "token_count": len(tokens),
                    "chunks": chunks,
                }}
            )
        doc = dict(base, purpose=request["purpose"], items=items)
    text = json.dumps(doc, separators=(",", ":")) + "\\n"
    result = hook(op, request, doc, text)
    if result is None:
        result = (json.dumps(doc, separators=(",", ":")) + "\\n", 0)
    body, code = result
    if isinstance(body, str):
        body = body.encode("utf-8")
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()
    sys.exit(code)


main()
"""


def fake_helper(*, hook: str = "return None", early: str = "return None") -> bytes:
    """Return fake helper script bytes with the given snippet bodies."""
    return _TEMPLATE.format(
        python=sys.executable,
        limits=dict(embedding_helper.LIMITS),
        whitespace="".join(sorted(embedding_helper._WHITESPACE)),
        early=textwrap.indent(textwrap.dedent(early).strip(), "    "),
        hook=textwrap.indent(textwrap.dedent(hook).strip(), "    "),
    ).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resource_files(helper: bytes) -> dict[str, bytes]:
    return {
        EXECUTABLE: helper,
        v2.MODEL_PATH: b"synthetic graph",
        f"{ROOT}/tokenizer/tokenizer.json": b"{}",
        f"{ROOT}/tokenizer/config.json": b'{"k":1}',
        f"{ROOT}/tokenizer/vocab.txt": b"[PAD]\n",
        RUNTIME: b"synthetic runtime",
        f"{LICENSES}/NOTICE.txt": b"synthetic notice",
    }


def component_for(
    files: Mapping[str, bytes], platform: str, preprocess: Mapping[str, object] | None = None
) -> dict[str, object]:
    return {
        "protocol": v2.PROTOCOL,
        "helper_build": {
            "source_commit": "1" * 40,
            "recipe_sha256": sha(b"helper recipe"),
            "executable_sha256": sha(files[EXECUTABLE]),
            "target_triple": TRIPLES[platform],
            "minimum_os": "14.0",
            "cpu_features": ["neon"],
            "dependency_lock_sha256": sha(b"lock"),
        },
        "runtime_build": {
            "upstream_commit": "2" * 40,
            "recipe_sha256": sha(b"runtime recipe"),
            "library_path": RUNTIME,
            "library_bytes": len(files[RUNTIME]),
            "library_sha256": sha(files[RUNTIME]),
            "cpu_only": True,
            "telemetry_compiled": False,
            "dependency_inventory_sha256": sha(b"deps"),
        },
        "model": {
            "repository": "example/synthetic-model",
            "revision": "3" * 40,
            "path": v2.MODEL_PATH,
            "bytes": len(files[v2.MODEL_PATH]),
            "sha256": sha(files[v2.MODEL_PATH]),
        },
        "preprocess": {
            "profile_id": "synthetic.profile.v1",
            "dimensions": 384,
            "pooling": "mean",
            "max_query_tokens": 254,
            "document_window_tokens": 224,
            "document_overlap_tokens": 32,
            "cls_token_id": 101,
            "sep_token_id": 102,
            "pad_token_id": 0,
            "query_prefix": "",
            "tokenizer_build": "synthetic-tokenizer 0.0.0",
            "segmenter_version": "1",
            **dict(preprocess or {}),
            **{key: sha(files[path]) for key, path in v2.TOKENIZER_PATHS.items()},
        },
        "notices_sha256": sha(b"notices"),
    }


def inventory_for(
    files: Mapping[str, bytes], platform: str, preprocess: Mapping[str, object] | None = None
) -> v2.Inventory:
    executable = {EXECUTABLE, RUNTIME}
    rows = [v2.Member(path, "directory", 0o755, 0, None) for path in DIRECTORIES]
    rows += [
        v2.Member(path, "file", 0o755 if path in executable else 0o644, len(data), sha(data))
        for path, data in files.items()
    ]
    return v2.Inventory(
        VERSION,
        "embedding",
        platform,
        tuple(sorted(rows, key=lambda row: row.path)),
        component_for(files, platform, preprocess),
    )


def manifest_for(inventory_bytes: bytes, platform: str, executable_sha256: str) -> bytes:
    resources = []
    for each in v2.PLATFORMS:
        for role in ("base", "graphify", "embedding"):
            real = role == "embedding" and each == platform
            resources.append(
                v2.ResourceV2(
                    role,
                    each,
                    v2.archive_filename(role, VERSION, each),
                    sha(f"archive {role} {each}".encode()),
                    1000,
                    v2.EXECUTABLES[role],
                    executable_sha256 if real else sha(f"exe {role}".encode()),
                    v2.inventory_path(role),
                    sha(inventory_bytes) if real else sha(f"inv {role} {each}".encode()),
                )
            )
    return v2.render_manifest_v2(v2.ManifestV2(VERSION, tuple(resources)))


def install(
    root: Path, files: Mapping[str, bytes], preprocess: Mapping[str, object] | None = None
) -> Path:
    """Write a synthetic installed prefix; return the base executable path."""
    platform = embedding_helper.current_platform()
    prefix = root / "Cellar" / "open-brain" / VERSION
    for directory in ("bin", *DIRECTORIES):
        (prefix / directory).mkdir(parents=True, exist_ok=True)
        os.chmod(prefix / directory, 0o755)
    base = prefix / "bin" / "open-brain"
    base.write_bytes(b"synthetic base")
    os.chmod(base, 0o755)
    inventory = inventory_for(files, platform, preprocess)
    for member in inventory.members:
        if member.kind == "file":
            path = prefix / member.path
            path.write_bytes(files[member.path])
            os.chmod(path, member.mode)
    inventory_bytes = v2.render_inventory(inventory)
    (prefix / v2.inventory_path("embedding")).write_bytes(inventory_bytes)
    (prefix / embedding_helper.MANIFEST_PATH).write_bytes(
        manifest_for(inventory_bytes, platform, sha(files[EXECUTABLE]))
    )
    return base
