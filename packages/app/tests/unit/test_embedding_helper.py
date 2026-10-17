"""Embedding helper transport: discovery, verification, persistent stage and protocol v1.

Every installed prefix, inventory and helper here is synthetic. The fake helper is a
Python script written into the synthetic prefix; one real-helper test runs only when the
contributor build products exist under ``build/`` and ``release/open-brain-embedding``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen as RealPopen
from typing import Any

import pytest
from open_brain_engine.engine.embedding_backend import EmbeddingBackend, EmbeddingFailure

from open_brain.services import component_manifest_v2 as v2
from open_brain.services import embedding_helper as eh
from packages.app.tests import embedding_fake as fake

process = pytest.mark.integration
QUERY = "Where did we discuss local memory?"
REPO = Path(__file__).resolve().parents[4]


@dataclass
class Env:
    installed: eh.InstalledEmbedding
    brain: Path
    prefix: Path

    def backend(self, **options: Any) -> eh.HelperEmbeddingBackend:
        return eh.HelperEmbeddingBackend(self.installed, self.brain, **options)

    @property
    def stage(self) -> Path:
        return self.brain.joinpath(*eh.STAGE_PARTS, self.backend().component_id)


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Env]]:
    counter = iter(range(1000))

    def build(hook: str = "return None", early: str = "return None") -> Env:
        root = tmp_path / f"r{next(counter)}"
        root.mkdir()
        base = fake.install(root, fake.resource_files(fake.fake_helper(hook=hook, early=early)))
        brain = root / "brain"
        (brain / ".open-brain").mkdir(parents=True)
        os.chmod(brain / ".open-brain", 0o700)
        return Env(eh.discover_embedding_resource(base), brain, base.parent.parent)

    yield build


def failure(call: Callable[[], object]) -> str:
    with pytest.raises(EmbeddingFailure) as caught:
        call()
    return caught.value.category


def f32(value: float) -> float:
    result: float = struct.unpack("<f", struct.pack("<f", value))[0]
    return result


# ----------------------------------------------------------------- discovery


def test_source_execution_is_resource_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert failure(eh.discover_embedding_resource) == "resource_missing"


def test_discovery_resolves_the_base_symlink_once(make: Callable[..., Env], tmp_path: Path) -> None:
    env = make()
    link = tmp_path / "bin" / "open-brain"
    link.parent.mkdir()
    link.symlink_to(env.prefix / "bin" / "open-brain")
    assert eh.discover_embedding_resource(link).prefix == env.prefix
    assert eh.discover_embedding_resource(env.prefix / "bin" / "open-brain") == env.installed


@pytest.mark.parametrize("relative", [eh.MANIFEST_PATH, eh.INVENTORY_PATH])
def test_discovery_requires_fixed_regular_paths(make: Callable[..., Env], relative: str) -> None:
    env = make()
    base = env.prefix / "bin" / "open-brain"
    (env.prefix / relative).unlink()
    assert failure(lambda: eh.discover_embedding_resource(base)) == "resource_missing"
    (env.prefix / relative).symlink_to(env.prefix / "bin" / "open-brain")
    assert failure(lambda: eh.discover_embedding_resource(base)) == "resource_untrusted"


def test_discovery_requires_the_base_layout(tmp_path: Path) -> None:
    other = tmp_path / "tool"
    other.write_bytes(b"x")
    assert failure(lambda: eh.discover_embedding_resource(other)) == "resource_missing"
    assert failure(lambda: eh.discover_embedding_resource(Path("bin/open-brain"))) == (
        "resource_missing"
    )


# -------------------------------------------------------------- verification


def test_metadata_binds_the_canonical_component(make: Callable[..., Env]) -> None:
    env = make()
    verified = eh.verify_embedding_metadata(env.installed)
    assert verified.component_bytes == json.dumps(
        verified.inventory.component, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert not verified.component_bytes.endswith(b"\n")
    assert verified.component_id == v2.component_id(verified.inventory.component or {})
    assert (
        verified.inventory_sha256
        == hashlib.sha256((env.prefix / eh.INVENTORY_PATH).read_bytes()).hexdigest()
    )


def test_expected_inventory_pin_hook(make: Callable[..., Env]) -> None:
    env = make()
    digest = eh.verify_embedding_metadata(env.installed).inventory_sha256
    assert eh.verify_embedding_metadata(env.installed, expected_inventory_sha256=digest)
    assert (
        failure(
            lambda: eh.verify_embedding_metadata(env.installed, expected_inventory_sha256="0" * 64)
        )
        == "resource_untrusted"
    )
    with pytest.raises(ValueError):
        eh.verify_embedding_metadata(env.installed, expected_inventory_sha256="XYZ")


def test_source_pin_default_is_empty() -> None:
    from open_brain.services import embedding_pin

    assert dict(embedding_pin.EXPECTED_EMBEDDING_INVENTORY_SHA256) == {}


def test_build_pin_is_the_default_expected_inventory(
    make: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain.services import embedding_pin

    env = make()
    digest = eh.verify_embedding_metadata(env.installed).inventory_sha256
    pins = {eh.current_platform(): digest}
    monkeypatch.setattr(embedding_pin, "EXPECTED_EMBEDDING_INVENTORY_SHA256", pins)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert eh.verify_embedding_metadata(env.installed).inventory_sha256 == digest
    assert env.backend().component_id

    pins[eh.current_platform()] = "0" * 64
    assert failure(lambda: eh.verify_embedding_metadata(env.installed)) == "resource_untrusted"
    assert failure(lambda: env.backend()) == "resource_untrusted"
    # A frozen base without a pin for this platform fails closed.
    pins.clear()
    assert failure(lambda: eh.verify_embedding_metadata(env.installed)) == "resource_untrusted"
    # Source execution keeps the installed manifest as the trust root.
    monkeypatch.setattr(sys, "frozen", False)
    assert eh.verify_embedding_metadata(env.installed).inventory_sha256 == digest


def test_inventory_hash_must_match_the_manifest(make: Callable[..., Env]) -> None:
    env = make()
    path = env.prefix / eh.INVENTORY_PATH
    document = json.loads(path.read_bytes())
    document["component"]["notices_sha256"] = "f" * 64
    path.write_bytes(json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    assert failure(lambda: eh.verify_embedding_metadata(env.installed)) == "resource_untrusted"


def test_noncanonical_manifest_is_untrusted(make: Callable[..., Env]) -> None:
    env = make()
    path = env.prefix / eh.MANIFEST_PATH
    path.write_bytes(path.read_bytes() + b" ")
    assert failure(lambda: eh.verify_embedding_metadata(env.installed)) == "resource_untrusted"


def test_unsupported_platform(make: Callable[..., Env], monkeypatch: pytest.MonkeyPatch) -> None:
    env = make()
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    assert failure(lambda: eh.verify_embedding_metadata(env.installed)) == "unsupported_platform"


@pytest.mark.parametrize("target", [eh.MANIFEST_PATH, "share/open-brain"])
def test_group_writable_metadata_is_untrusted(make: Callable[..., Env], target: str) -> None:
    env = make()
    os.chmod(env.prefix / target, stat.S_IMODE((env.prefix / target).stat().st_mode) | 0o020)
    assert failure(lambda: eh.verify_embedding_metadata(env.installed)) == "resource_untrusted"


# ------------------------------------------------------------- golden success


@process
def test_golden_query_and_stage_layout(make: Callable[..., Env]) -> None:
    env = make()
    backend = env.backend()
    accepted: EmbeddingBackend = backend
    [item] = accepted.embed("query", [QUERY])
    assert item.slot == 0
    assert item.text_sha256 == hashlib.sha256(QUERY.encode()).hexdigest()
    assert item.token_count == 6
    [chunk] = item.chunks
    assert (chunk.ordinal, chunk.token_start, chunk.token_end) == (0, 0, 6)
    assert (chunk.byte_start, chunk.byte_end) == (0, len(QUERY.encode()))
    assert chunk.vector[:3] == (f32(0.6), f32(0.8), 0.0)
    assert len(chunk.vector) == 384

    stage = env.stage
    assert stat.S_IMODE(stage.stat().st_mode) == 0o700
    assert stat.S_IMODE((stage / "runtime").stat().st_mode) == 0o700
    assert stat.S_IMODE((stage / "open-brain-embedding").stat().st_mode) == 0o500
    assert stat.S_IMODE((stage / "runtime" / fake.RUNTIME_NAME).stat().st_mode) == 0o500
    assert stat.S_IMODE((stage / "model" / "model.onnx").stat().st_mode) == 0o400
    assert stat.S_IMODE((stage / "component.json").stat().st_mode) == 0o400
    component = (stage / "component.json").read_bytes()
    assert hashlib.sha256(component).hexdigest() == backend.component_id == stage.name
    assert sorted(p.name for p in stage.iterdir()) == [
        "component.json",
        "model",
        "open-brain-embedding",
        "runtime",
        "tokenizer",
    ]
    staged = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
    assert eh.stage_bytes(env.brain) == staged > 0


@process
def test_golden_documents_window_and_persist_stage(make: Callable[..., Env]) -> None:
    env = make()
    backend = env.backend()
    long_text = " ".join(f"w{index}" for index in range(300))
    items = backend.embed("document", ["café ünïcode   text", long_text])
    assert [item.slot for item in items] == [0, 1]
    assert [(c.token_start, c.token_end) for c in items[1].chunks] == [(0, 224), (192, 300)]
    second = items[1].chunks[1]
    assert long_text.encode()[second.byte_start : second.byte_end].startswith(b"w192")
    assert items[0].chunks[0].byte_end == len("café ünïcode   text".encode())
    inode = (env.stage / "model" / "model.onnx").stat().st_ino
    backend.embed("query", [QUERY])
    assert (env.stage / "model" / "model.onnx").stat().st_ino == inode


@process
def test_negative_zero_is_canonicalized(make: Callable[..., Env]) -> None:
    env = make(
        hook="""
        if op == "embed":
            doc["items"][0]["chunks"][0]["vector"][5] = -0.0
        return None
        """
    )
    [item] = env.backend().embed("query", [QUERY])
    assert str(item.chunks[0].vector[5]) == "0.0"


# -------------------------------------------------------- response violations


def _set(path: str, value: str) -> str:
    return f"""
    if op == "embed":
        doc{path} = {value}
    return None
    """


def _text(old: str, new: str, op: str = "embed") -> str:
    return f"""
    if op == {op!r}:
        return (text.replace({old!r}, {new!r}, 1), 0)
    return None
    """


CHUNK = '["items"][0]["chunks"][0]'
VIOLATIONS: dict[str, tuple[str, str]] = {
    "duplicate_key": (_text('{"protocol"', '{"protocol":"x","protocol"'), "protocol_invalid"),
    "nan": (_text('"vector":[', '"vector":[NaN,'), "protocol_invalid"),
    "infinity": (_text('"vector":[0.6', '"vector":[Infinity'), "protocol_invalid"),
    "boolean_slot": (_text('"slot":0', '"slot":false'), "protocol_invalid"),
    "boolean_token_start": (_text('"token_start":0', '"token_start":false'), "protocol_invalid"),
    "extra_key": (_set('["extra"]', "1"), "protocol_invalid"),
    "missing_key": (
        """
        if op == "embed":
            del doc["purpose"]
        return None
        """,
        "protocol_invalid",
    ),
    "extra_chunk_key": (_set(f'{CHUNK}["note"]', "1"), "protocol_invalid"),
    "protocol": (_set('["protocol"]', '"open-brain.embedding-helper.v2"'), "protocol_invalid"),
    "request_id": (_set('["request_id"]', '"f" * 32'), "protocol_invalid"),
    "component_id": (_set('["component_id"]', '"e" * 64'), "component_mismatch"),
    "purpose": (_set('["purpose"]', '"document"'), "protocol_invalid"),
    "slot": (_set('["items"][0]["slot"]', "1"), "protocol_invalid"),
    "extra_item": (_set('["items"]', 'doc["items"] * 2'), "protocol_invalid"),
    "missing_item": (_set('["items"]', "[]"), "protocol_invalid"),
    "text_sha256": (_set('["items"][0]["text_sha256"]', '"0" * 64'), "protocol_invalid"),
    "token_count_zero": (_set('["items"][0]["token_count"]', "0"), "protocol_invalid"),
    "query_tokens_above_cap": (_set('["items"][0]["token_count"]', "255"), "protocol_invalid"),
    "ordinal": (_set(f'{CHUNK}["ordinal"]', "1"), "protocol_invalid"),
    "token_end": (_set(f'{CHUNK}["token_end"]', "5"), "protocol_invalid"),
    "second_query_chunk": (
        _set('["items"][0]["chunks"]', 'doc["items"][0]["chunks"] * 2'),
        "protocol_invalid",
    ),
    "byte_end_short": (_set(f'{CHUNK}["byte_end"]', f"{len(QUERY) - 1}"), "protocol_invalid"),
    "byte_end_beyond": (_set(f'{CHUNK}["byte_end"]', f"{len(QUERY) + 1}"), "protocol_invalid"),
    "byte_start_whitespace": (_set(f'{CHUNK}["byte_start"]', "5"), "protocol_invalid"),
    "byte_empty": (_set(f'{CHUNK}["byte_start"]', f"{len(QUERY)}"), "protocol_invalid"),
    "dimensions": (_set(f'{CHUNK}["vector"]', f'doc{CHUNK}["vector"][:383]'), "protocol_invalid"),
    "component_range": (_set(f'{CHUNK}["vector"]', "[1.0002] + [0.0] * 383"), "protocol_invalid"),
    "norm": (_set(f'{CHUNK}["vector"]', "[0.05] * 384"), "protocol_invalid"),
    "zero_vector": (_set(f'{CHUNK}["vector"]', "[0.0] * 384"), "protocol_invalid"),
    "string_component": (_set(f'{CHUNK}["vector"][0]', '"0.6"'), "protocol_invalid"),
    "boolean_component": (_set(f'{CHUNK}["vector"]', "[True] + [0.0] * 383"), "protocol_invalid"),
    "subnormal_component": (_set(f'{CHUNK}["vector"][9]', "1e-40"), "protocol_invalid"),
    "depth": (_set('["items"][0]["chunks"][0]["vector"][0]', "[[[[0.6]]]]"), "protocol_invalid"),
    "missing_lf": (
        """
        if op == "embed":
            return (text[:-1], 0)
        return None
        """,
        "protocol_invalid",
    ),
    "second_frame": (
        """
        if op == "embed":
            return (text + text, 0)
        return None
        """,
        "protocol_invalid",
    ),
    "trailing_bytes": (
        """
        if op == "embed":
            return (text + " ", 0)
        return None
        """,
        "protocol_invalid",
    ),
    "bom": (
        """
        if op == "embed":
            return (b"\\xef\\xbb\\xbf" + text.encode(), 0)
        return None
        """,
        "protocol_invalid",
    ),
    "invalid_utf8": (_text('"status"', '"st\\udcffatus"'), "protocol_invalid"),
    "capabilities_limit": (
        """
        if op == "capabilities":
            doc["limits"]["max_items"] = 5
        return None
        """,
        "protocol_invalid",
    ),
    "capabilities_boolean_limit": (
        """
        if op == "capabilities":
            doc["limits"]["max_items"] = True
        return None
        """,
        "protocol_invalid",
    ),
    "capabilities_extra_limit": (
        """
        if op == "capabilities":
            doc["limits"]["max_threads"] = 2
        return None
        """,
        "protocol_invalid",
    ),
    "capabilities_component": (
        """
        if op == "capabilities":
            doc["component_id"] = "e" * 64
        return None
        """,
        "component_mismatch",
    ),
}


@process
@pytest.mark.parametrize("name", sorted(VIOLATIONS))
def test_response_violation_invalidates_the_exchange(make: Callable[..., Env], name: str) -> None:
    hook, expected = VIOLATIONS[name]
    if name == "invalid_utf8":
        hook = """
        if op == "embed":
            return (text.encode().replace(b'"status"', b'"st\\xffatus"', 1), 0)
        return None
        """
    env = make(hook=hook)
    assert failure(lambda: env.backend().embed("query", [QUERY])) == expected


DOCUMENT_VIOLATIONS: dict[str, str] = {
    "window_start": '["items"][0]["chunks"][1]["token_start"] = 200',
    "window_missing": '["items"][0]["chunks"] = doc["items"][0]["chunks"][:1]',
    "byte_not_char_boundary": '["items"][1]["chunks"][0]["byte_end"] = 4',
    "byte_order": '["items"][0]["chunks"][1]["byte_end"] = 2',
    # A hostile token count must be refused before planning windows from it.
    "token_count_above_bytes": '["items"][0]["token_count"] = 10**12',
}


@process
@pytest.mark.parametrize("name", sorted(DOCUMENT_VIOLATIONS))
def test_document_chunk_violations(make: Callable[..., Env], name: str) -> None:
    edit = DOCUMENT_VIOLATIONS[name]
    env = make(
        hook=f"""
        if op == "embed":
            doc{edit}
        return None
        """
    )
    texts = [" ".join(f"w{index}" for index in range(300)), "café x"]
    assert failure(lambda: env.backend().embed("document", texts)) == "protocol_invalid"


# ----------------------------------------------------------- exit and frames


EXIT_CASES: dict[str, tuple[str, str]] = {
    "ok_frame_exit_two": (
        """
        if op == "embed":
            return (text, 2)
        return None
        """,
        "worker_failed",
    ),
    "error_frame_exit_zero": (
        """
        if op == "embed":
            frame = {"protocol": doc["protocol"], "request_id": doc["request_id"],
                     "status": "error", "error": {"code": "input_limit", "retryable": False}}
            return (json.dumps(frame) + "\\n", 0)
        return None
        """,
        "worker_failed",
    ),
    "exit_one": (
        """
        if op == "embed":
            return (text, 1)
        return None
        """,
        "worker_failed",
    ),
    "zero_without_frame": (
        """
        if op == "embed":
            return (b"", 0)
        return None
        """,
        "worker_failed",
    ),
    "signal": (
        """
        if op == "embed":
            os.kill(os.getpid(), 9)
        return None
        """,
        "worker_failed",
    ),
    "framed_input_limit": (
        """
        if op == "embed":
            frame = {"protocol": doc["protocol"], "request_id": doc["request_id"],
                     "status": "error", "error": {"code": "input_limit", "retryable": False}}
            return (json.dumps(frame) + "\\n", 2)
        return None
        """,
        "input_limit",
    ),
    "framed_tokenization": (
        """
        if op == "embed":
            frame = {"protocol": doc["protocol"], "request_id": doc["request_id"],
                     "status": "error",
                     "error": {"code": "tokenization_failed", "retryable": False}}
            return (json.dumps(frame) + "\\n", 2)
        return None
        """,
        "worker_failed",
    ),
    "framed_unreadable_zero_id": (
        """
        if op == "capabilities":
            frame = {"protocol": doc["protocol"], "request_id": "0" * 32,
                     "status": "error", "error": {"code": "request_invalid", "retryable": False}}
            return (json.dumps(frame) + "\\n", 2)
        return None
        """,
        "protocol_invalid",
    ),
    "framed_component_mismatch": (
        """
        if op == "capabilities":
            frame = {"protocol": doc["protocol"], "request_id": doc["request_id"],
                     "status": "error",
                     "error": {"code": "component_mismatch", "retryable": False}}
            return (json.dumps(frame) + "\\n", 2)
        return None
        """,
        "component_mismatch",
    ),
    "framed_retryable": (
        """
        if op == "embed":
            frame = {"protocol": doc["protocol"], "request_id": doc["request_id"],
                     "status": "error", "error": {"code": "input_limit", "retryable": True}}
            return (json.dumps(frame) + "\\n", 2)
        return None
        """,
        "protocol_invalid",
    ),
    "framed_unknown_code": (
        """
        if op == "embed":
            frame = {"protocol": doc["protocol"], "request_id": doc["request_id"],
                     "status": "error", "error": {"code": "panic at x.rs", "retryable": False}}
            return (json.dumps(frame) + "\\n", 2)
        return None
        """,
        "protocol_invalid",
    ),
    "framed_wrong_id": (
        """
        if op == "embed":
            frame = {"protocol": doc["protocol"], "request_id": "a" * 32,
                     "status": "error", "error": {"code": "input_limit", "retryable": False}}
            return (json.dumps(frame) + "\\n", 2)
        return None
        """,
        "protocol_invalid",
    ),
}


@process
@pytest.mark.parametrize("name", sorted(EXIT_CASES))
def test_exit_code_and_frame_rules(make: Callable[..., Env], name: str) -> None:
    hook, expected = EXIT_CASES[name]
    env = make(hook=hook)
    assert failure(lambda: env.backend().embed("query", [QUERY])) == expected


# ------------------------------------------------- floods, stalls and deadlines


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    def record(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child: subprocess.Popen[bytes] = original(*args, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", record)
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.fixture
def short_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(eh._DEADLINE_SECONDS, "capabilities", 2.0)
    monkeypatch.setitem(eh._DEADLINE_SECONDS, "query", 1.0)
    monkeypatch.setitem(eh._DEADLINE_SECONDS, "document", 1.0)


def test_deadlines_equal_the_protocol_table() -> None:
    assert eh._DEADLINE_SECONDS == {"capabilities": 2.0, "query": 10.0, "document": 60.0}


@process
def test_stdout_flood_kills_the_child(
    make: Callable[..., Env], popen: list[subprocess.Popen[bytes]]
) -> None:
    env = make(
        hook="""
        if op == "embed":
            sys.stdout.write("x" * 4_000_000)
            sys.stdout.flush()
            time.sleep(30)
        return None
        """
    )
    assert failure(lambda: env.backend().embed("query", [QUERY])) == "protocol_invalid"
    assert popen[-1].returncode is not None


@process
def test_stderr_flood_kills_the_child(
    make: Callable[..., Env], popen: list[subprocess.Popen[bytes]]
) -> None:
    env = make(
        hook="""
        if op == "embed":
            sys.stderr.write("secret native detail " * 1000)
            sys.stderr.flush()
            time.sleep(30)
        return None
        """
    )
    assert failure(lambda: env.backend().embed("query", [QUERY])) == "worker_failed"
    assert popen[-1].returncode is not None


@process
def test_small_stderr_is_discarded(make: Callable[..., Env]) -> None:
    env = make(
        hook="""
        sys.stderr.write("diagnostic")
        return None
        """
    )
    assert env.backend().embed("query", [QUERY])


@process
@pytest.mark.usefixtures("short_deadlines")
def test_stalled_stdin_hits_the_deadline(
    make: Callable[..., Env], popen: list[subprocess.Popen[bytes]]
) -> None:
    env = make(
        early="""
        if b'"operation":"embed"' in head:
            time.sleep(30)
        return None
        """
    )
    texts = ["word " * 13000, "word " * 13000]
    started = time.monotonic()
    assert failure(lambda: env.backend().embed("document", texts)) == "deadline"
    assert time.monotonic() - started < 6
    assert popen[-1].returncode is not None


@process
@pytest.mark.usefixtures("short_deadlines")
def test_slow_helper_hits_the_deadline_and_is_reaped(
    make: Callable[..., Env], popen: list[subprocess.Popen[bytes]]
) -> None:
    env = make(
        hook="""
        if op == "embed":
            time.sleep(30)
        return None
        """
    )
    assert failure(lambda: env.backend().embed("query", [QUERY])) == "deadline"
    assert len(popen) == 2
    assert popen[-1].returncode is not None


@process
def test_cancel_kills_and_reaps(
    make: Callable[..., Env], popen: list[subprocess.Popen[bytes]]
) -> None:
    env = make(
        hook="""
        if op == "embed":
            time.sleep(30)
        return None
        """
    )
    started = time.monotonic()
    backend = env.backend(cancelled=lambda: len(popen) == 2 and time.monotonic() - started > 0.3)
    assert failure(lambda: backend.embed("query", [QUERY])) == "cancelled"
    assert popen[-1].returncode is not None


@process
def test_competing_stage_lock_obeys_cancellation(
    make: Callable[..., Env], popen: list[subprocess.Popen[bytes]]
) -> None:
    env = make()
    parent = env.brain.joinpath(*eh.STAGE_PARTS)
    parent.mkdir(parents=True, mode=0o700)
    for directory in [parent, *parent.parents]:
        if directory == env.brain:
            break
        os.chmod(directory, 0o700)
    holder = os.open(parent / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.monotonic()
        backend = env.backend(cancelled=lambda: time.monotonic() - started > 0.3)
        assert failure(lambda: backend.embed("query", [QUERY])) == "cancelled"
        assert time.monotonic() - started < 5
        assert popen == []
    finally:
        os.close(holder)


def test_group_writable_ancestor_is_refused_unless_sticky(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    (shared / "brain").mkdir(parents=True)
    os.chmod(shared, 0o777)
    with pytest.raises(OSError):
        os.close(eh._open_absolute(shared / "brain"))
    os.chmod(shared, 0o1777)
    os.close(eh._open_absolute(shared / "brain"))


def test_user_owned_group_writable_ancestor_is_admitted(tmp_path: Path) -> None:
    # Homebrew's Cellar is 0775 and owned by the installing user (D15).
    cellar = tmp_path / "Cellar"
    (cellar / "open-brain").mkdir(parents=True)
    os.chmod(cellar, 0o775)
    os.close(eh._open_absolute(cellar / "open-brain"))


def test_resource_under_group_writable_cellar_verifies(make: Callable[..., Env]) -> None:
    env = make()
    assert env.prefix.parent.parent.name == "Cellar"
    os.chmod(env.prefix.parent.parent, 0o775)
    assert eh.verify_embedding_metadata(env.installed) is not None
    os.chmod(env.prefix.parent.parent, 0o777)
    assert failure(lambda: eh.verify_embedding_metadata(env.installed)) == "resource_missing"


@process
def test_cancel_before_launch(make: Callable[..., Env], popen: list[Any]) -> None:
    env = make()
    assert failure(lambda: env.backend(cancelled=lambda: True).embed("query", [QUERY])) == (
        "cancelled"
    )
    assert popen == []


@process
def test_unkillable_child_is_cleanup_failed(
    make: Callable[..., Env], monkeypatch: pytest.MonkeyPatch, popen: list[Any]
) -> None:
    env = make(
        hook="""
        if op == "embed":
            time.sleep(30)
        return None
        """
    )
    started = time.monotonic()
    original_wait = RealPopen.wait

    def stuck(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        if timeout == eh._REAP_SECONDS:
            raise subprocess.TimeoutExpired("helper", timeout)
        return original_wait(self, timeout)

    monkeypatch.setattr(RealPopen, "wait", stuck)
    backend = env.backend(cancelled=lambda: len(popen) == 2 and time.monotonic() - started > 0.3)
    assert failure(lambda: backend.embed("query", [QUERY])) == "cleanup_failed"


# ------------------------------------------------------- environment and fds

REPORT_HOOK = """
fds = []
for fd in range(3, 256):
    try:
        os.fstat(fd)
    except OSError:
        continue
    fds.append(fd)
report = {"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "fds": fds}
return (json.dumps(report) + "\\n", 0)
"""


@process
def test_child_environment_argv_cwd_and_fds(make: Callable[..., Env], tmp_path: Path) -> None:
    env = make(hook=REPORT_HOOK)
    backend = env.backend()
    assert failure(lambda: backend.embed("query", [QUERY])) == "protocol_invalid"
    leaked = os.open(tmp_path, os.O_RDONLY)
    os.set_inheritable(leaked, True)
    try:
        returncode, raw = eh._run_child(
            env.stage,
            eh._request(
                {"protocol": eh.PROTOCOL, "request_id": "1" * 32, "operation": "capabilities"}
            ),
            eh._Clock(time.monotonic() + 5, lambda: False),
        )
    finally:
        os.close(leaked)
    assert returncode == 0
    report = json.loads(raw)
    assert report["argv"] == [str(env.stage / "open-brain-embedding"), "--stdio-v1"]
    if sys.platform == "darwin":
        # libSystem adds this to every new macOS process; the parent never passes it.
        report["env"].pop("__CF_USER_TEXT_ENCODING", None)
    assert report["env"] == dict(eh.HELPER_ENV)
    assert os.path.realpath(report["cwd"]) == os.path.realpath(env.stage)
    assert report["fds"] == []


# ---------------------------------------------------------------- stage tamper


def tamper_content(stage: Path) -> None:
    path = stage / "model" / "model.onnx"
    os.chmod(path, 0o600)
    data = path.read_bytes()
    path.write_bytes(data[:-1] + b"X")
    os.chmod(path, 0o400)


def tamper_mode(stage: Path) -> None:
    os.chmod(stage / "tokenizer" / "vocab.txt", 0o600)


def tamper_symlink(stage: Path) -> None:
    path = stage / "model" / "model.onnx"
    outside = stage.parent.parent / "outside.onnx"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)


def tamper_extra_link(stage: Path) -> None:
    os.link(stage / "component.json", stage.parent.parent / "extra-link")


def tamper_extra_file(stage: Path) -> None:
    (stage / "runtime" / "libextra.dylib").write_bytes(b"x")


def tamper_directory_mode(stage: Path) -> None:
    os.chmod(stage / "runtime", 0o755)


TAMPERS = {
    "content": tamper_content,
    "mode": tamper_mode,
    "symlink": tamper_symlink,
    "extra_link": tamper_extra_link,
    "extra_file": tamper_extra_file,
    "directory_mode": tamper_directory_mode,
}


@process
@pytest.mark.parametrize("name", sorted(TAMPERS))
def test_tampered_stage_is_recreated_before_launch(make: Callable[..., Env], name: str) -> None:
    env = make()
    backend = env.backend()
    backend.embed("query", [QUERY])
    stage = env.stage
    TAMPERS[name](stage)
    assert backend.embed("query", [QUERY])
    model = stage / "model" / "model.onnx"
    assert not model.is_symlink()
    assert model.stat().st_nlink == 1
    assert (stage / "component.json").stat().st_nlink == 1
    assert stat.S_IMODE((stage / "tokenizer" / "vocab.txt").stat().st_mode) == 0o400
    assert not (stage / "runtime" / "libextra.dylib").exists()
    if name == "symlink":
        assert (stage.parent.parent / "outside.onnx").read_bytes() == b"synthetic graph"
    if name == "extra_link":
        assert (stage.parent.parent / "extra-link").exists()


@process
def test_tamper_between_verification_and_launch_is_caught(
    make: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make()
    backend = env.backend()
    backend.embed("query", [QUERY])
    original = eh._Stage.verify
    calls: list[int] = []
    builds: list[int] = []
    original_build = eh._Stage.build

    def verify_then_tamper(self: eh._Stage, clock: eh._Clock) -> dict[str, Any]:
        identities = original(self, clock)
        calls.append(1)
        if len(calls) == 1:
            tamper_content(self.path)
        return identities

    def counting_build(self: eh._Stage, clock: eh._Clock) -> None:
        builds.append(1)
        original_build(self, clock)

    monkeypatch.setattr(eh._Stage, "verify", verify_then_tamper)
    monkeypatch.setattr(eh._Stage, "build", counting_build)
    assert backend.embed("query", [QUERY])
    assert builds == [1]


@process
def test_each_embed_call_hashes_the_stage_once(
    make: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make()
    backend = env.backend()
    backend.embed("query", [QUERY])
    original = eh._Stage._verify_file
    original_run = eh._run_child
    hashed: list[str] = []
    launches: list[int] = []

    def counting(self: eh._Stage, directory_fd: int, leaf: str, item: Any) -> Any:
        hashed.append(item.relative)
        return original(self, directory_fd, leaf, item)

    def run(stage: Path, request: bytes, clock: eh._Clock) -> tuple[int, bytes]:
        launches.append(1)
        if touch and len(launches) == 1:
            # A stat-identity change between the two launches forces a full re-verify.
            os.utime(stage / "tokenizer" / "vocab.txt", ns=(1, 1))
        return original_run(stage, request, clock)

    monkeypatch.setattr(eh._Stage, "_verify_file", counting)
    monkeypatch.setattr(eh, "_run_child", run)
    files = len(eh._Stage(-1, env.stage.parent, eh.verify_embedding_metadata(env.installed)).files)
    for touch in (False, False, True):
        hashed.clear()
        launches.clear()
        assert backend.embed("query", [QUERY])
        assert launches == [1, 1]
        assert len(hashed) == files * (2 if touch else 1)
    assert len(set(hashed)) == files


@process
def test_persistent_tamper_after_verification_is_untrusted(
    make: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make()
    original = eh._Stage.verify

    def verify_then_tamper(self: eh._Stage, clock: eh._Clock) -> dict[str, Any]:
        identities = original(self, clock)
        tamper_mode(self.path)
        return identities

    monkeypatch.setattr(eh._Stage, "verify", verify_then_tamper)
    assert failure(lambda: env.backend().embed("query", [QUERY])) == "resource_untrusted"


@pytest.mark.parametrize(
    "edit",
    ["content", "symlink", "group_writable", "hardlink", "group_writable_parent"],
)
def test_untrusted_installed_bytes_cannot_be_staged(
    make: Callable[..., Env], edit: str, tmp_path: Path
) -> None:
    env = make()
    path = env.prefix / v2.MODEL_PATH
    if edit == "content":
        path.write_bytes(b"synthetic grapX")
    elif edit == "symlink":
        copy = tmp_path / "graph"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)
    elif edit == "group_writable":
        os.chmod(path, 0o664)
    elif edit == "hardlink":
        os.link(path, tmp_path / "graph-link")
    else:
        os.chmod(path.parent, 0o775)
    backend = env.backend()
    assert failure(lambda: backend.embed("query", [QUERY])) == "resource_untrusted"
    assert not env.stage.exists()
    assert [p.name for p in env.stage.parent.iterdir()] == [".lock"]


@pytest.mark.parametrize("edit", ["group_writable", "symlink"])
def test_untrusted_stage_parent_is_refused(
    make: Callable[..., Env], edit: str, tmp_path: Path
) -> None:
    env = make()
    cache = env.brain / ".open-brain" / "cache"
    if edit == "group_writable":
        cache.mkdir(mode=0o700)
        os.chmod(cache, 0o770)
    else:
        (tmp_path / "elsewhere").mkdir()
        cache.symlink_to(tmp_path / "elsewhere")
    assert failure(lambda: env.backend().embed("query", [QUERY])) == "resource_untrusted"
    if edit == "symlink":
        assert list((tmp_path / "elsewhere").iterdir()) == []


def test_stage_bytes_without_a_stage(tmp_path: Path) -> None:
    assert eh.stage_bytes(tmp_path) == 0


# --------------------------------------------------------------- caller input


@pytest.mark.parametrize(
    ("purpose", "texts", "expected"),
    [
        ("query", ["x" * 8193], "input_limit"),
        ("document", ["x" * 65537], "input_limit"),
        ("document", ["a"] * 5, "input_limit"),
        ("document", ["word " * 6600] * 4, "input_limit"),
    ],
)
def test_input_limits_fail_before_any_launch(
    make: Callable[..., Env], popen: list[Any], purpose: str, texts: list[str], expected: str
) -> None:
    env = make()
    assert failure(lambda: env.backend().embed(purpose, texts)) == expected  # type: ignore[arg-type]
    assert popen == []


@pytest.mark.parametrize(
    ("purpose", "texts"),
    [
        ("summary", ["x"]),
        ("query", ["a", "b"]),
        ("query", []),
        ("query", "text"),
        ("query", ["   "]),
        ("query", ["e\u0301"]),
        ("query", ["\ud800"]),
    ],
)
def test_invalid_caller_input_is_rejected(
    make: Callable[..., Env], purpose: str, texts: object
) -> None:
    env = make()
    with pytest.raises(ValueError):
        env.backend().embed(purpose, texts)  # type: ignore[arg-type]


# ----------------------------------------------------------- real helper run

# Recorded 2026-10-17 on macOS arm64 from the contributor release helper, the
# telemetry-free ONNX Runtime 1.31.0 build and pinned MiniLM assets, for QUERY.
PINNED_QUERY_TOKENS = 7
PINNED_QUERY_PREFIX = (
    0.103176,
    0.008670,
    -0.073328,
    0.036998,
    0.010868,
    0.035714,
    -0.005467,
    0.035704,
)


def _real_inputs() -> dict[str, Path] | None:
    runtime = (
        REPO / "build/g4-ort-macos/Release/libonnxruntime.1.31.0.dylib"
        if sys.platform == "darwin"
        else REPO / "build/g4-ort-linux/Release/libonnxruntime.so.1.31.0"
    )
    assets = REPO / "build/g4-assets-1/minilm"
    inputs = {
        fake.EXECUTABLE: REPO / "release/open-brain-embedding/target/release/open-brain-embedding",
        fake.RUNTIME: runtime,
        v2.MODEL_PATH: assets / "onnx/model.onnx",
        f"{fake.ROOT}/tokenizer/tokenizer.json": assets / "tokenizer.json",
        f"{fake.ROOT}/tokenizer/config.json": assets / "config.json",
        f"{fake.ROOT}/tokenizer/vocab.txt": assets / "vocab.txt",
    }
    return inputs if all(path.is_file() for path in inputs.values()) else None


@pytest.mark.integration
def test_real_helper_query_vector(tmp_path: Path) -> None:
    inputs = _real_inputs()
    if inputs is None:
        pytest.skip("contributor helper, runtime or MiniLM assets are not built")
    files = {path: source.read_bytes() for path, source in inputs.items()}
    files[f"{fake.LICENSES}/NOTICE.txt"] = b"synthetic notice"
    base = fake.install(
        tmp_path,
        files,
        {"profile_id": "ob.minilm-wordpiece-mean.v1", "tokenizer_build": "tokenizers 0.22.1"},
    )
    brain = tmp_path / "brain"
    (brain / ".open-brain").mkdir(parents=True)
    os.chmod(brain / ".open-brain", 0o700)
    backend = eh.HelperEmbeddingBackend(eh.discover_embedding_resource(base), brain)
    [item] = backend.embed("query", [QUERY])
    [chunk] = item.chunks
    assert (chunk.token_start, chunk.byte_start, chunk.byte_end) == (0, 0, len(QUERY))
    assert item.token_count == chunk.token_end == PINNED_QUERY_TOKENS
    assert chunk.vector[:8] == pytest.approx(PINNED_QUERY_PREFIX, abs=1e-4)
