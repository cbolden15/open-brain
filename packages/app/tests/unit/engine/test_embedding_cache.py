"""Derived owner-local embedding cache generations, rebuild fencing and load validation."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine, ReferencePayload, TextPayload
from open_brain_engine.engine import embedding_cache as cache
from open_brain_engine.engine.capture_lifecycle_contracts import LifecycleRequest
from open_brain_engine.engine.embedding_backend import (
    EmbeddedItem,
    EmbeddingChunk,
    EmbeddingFailure,
)
from open_brain_engine.engine.embedding_policy import EmbeddingPolicyError
from open_brain_engine.engine.records import RecordProjector
from open_brain_engine.engine.source_intake import SourceRevisionSubmission
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.storage.locks import LockBusyError

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.t03_adapters import agent_authority, owner_authority
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission

COMPONENT = "ab" * 32
OTHER = "cd" * 32
TEXTS = (
    "Synthetic garnet ledger covering quarterly orchard planning",
    "Synthetic basalt notebook about river survey routes",
    "Synthetic cobalt memo on greenhouse irrigation timing",
)


def _unit(seed: str) -> tuple[float, ...]:
    raw = b"".join(hashlib.sha256(f"{seed}:{index}".encode()).digest() for index in range(48))
    values = [(byte - 127.5) / 127.5 for byte in raw[:384]]
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _item(slot: int, text: str) -> EmbeddedItem:
    encoded = text.encode("utf-8")
    words = [
        (len(text[: match.start()].encode()), len(text[: match.end()].encode()))
        for match in re.finditer(r"\S+", text)
    ]
    chunks: list[EmbeddingChunk] = []
    start = 0
    while True:
        end = min(start + 4, len(words))
        span = encoded[words[start][0] : words[end - 1][1]].decode()
        chunks.append(
            EmbeddingChunk(len(chunks), start, end, words[start][0], words[end - 1][1], _unit(span))
        )
        if end == len(words):
            break
        start += 3
    return EmbeddedItem(slot, hashlib.sha256(encoded).hexdigest(), len(words), tuple(chunks))


class FakeBackend:
    """Deterministic in-process backend; a hook may mutate the Brain mid-rebuild."""

    def __init__(
        self, component_id: str = COMPONENT, hook: Callable[[int], None] | None = None
    ) -> None:
        self.component_id = component_id
        self.hook = hook
        self.calls: list[list[str]] = []

    def embed(self, purpose: str, texts: Sequence[str]) -> list[EmbeddedItem]:
        assert purpose == "document"
        assert 1 <= len(texts) <= 4
        self.calls.append(list(texts))
        if self.hook is not None:
            self.hook(len(self.calls))
        return [_item(slot, text) for slot, text in enumerate(texts)]

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return owner_authority(engine.tasks, session_id="owner-cache-test")


def _brain(tmp_path: Path, texts: Sequence[str] = TEXTS, *, enable: bool = True) -> BrainEngine:
    tmp_path.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    for index, text in enumerate(texts):
        engine.capture.accept(TextPayload(text), delivery_id=f"cache.{index}.{uuid4()}")
    if enable:
        engine.embedding_policy.enable(COMPONENT, authority=_owner(engine))
    return engine


def _rebuild(
    engine: BrainEngine,
    backend: FakeBackend | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> cache.RebuildReceipt:
    return cache.rebuild(
        engine,
        authority=_owner(engine),
        backend=FakeBackend() if backend is None else backend,
        cancelled=cancelled,
    )


def _root(engine: BrainEngine) -> Path:
    return engine.profile.root / ".open-brain" / "cache" / "embedding-v1"


def _generations(engine: BrainEngine) -> list[str]:
    return sorted(os.listdir(_root(engine) / "generations"))


def _active(engine: BrainEngine) -> cache.ActiveGeneration:
    active = cache.load_active(engine)
    assert active is not None
    return active


def _reason(engine: BrainEngine) -> str:
    status = cache.active_status(engine)
    assert isinstance(status, cache.CacheUnavailable)
    return status.reason


def _failure(category: str) -> pytest.RaisesExc[EmbeddingFailure]:
    return pytest.raises(EmbeddingFailure, match=f"^{category}$")


def _manifest(engine: BrainEngine) -> dict[str, object]:
    path = _root(engine) / "generations" / _active(engine).generation_id / "manifest.json"
    return dict(json.loads(path.read_bytes()))


def _owner_refs(engine: BrainEngine) -> set[cache.ChunkRef]:
    refs = set()
    with engine._store.connect() as connection:
        connection.row_factory = __import__("sqlite3").Row
        projector = RecordProjector(engine.profile, connection, _owner(engine))
        for (record_id,) in connection.execute("SELECT result_id FROM search_documents"):
            projected = projector.read(record_id)
            texts = cache.field_texts(projected)
            for name in cache.FIELDS:
                refs.add(
                    cache.ChunkRef(
                        projected.summary["record_type"],
                        projected.summary["record_id"],
                        projected.summary["revision_id"],
                        name,
                        cache.projection_sha256(texts[name]),
                    )
                )
    return refs


def _lifecycle(engine: BrainEngine, kind: str, payload: dict[str, object]) -> LifecycleRequest:
    with engine._store.connect() as connection:
        brain, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    return LifecycleRequest(str(uuid4()), kind, brain, epoch, payload)


def _retract(engine: BrainEngine, capture_id: str) -> None:
    request = _lifecycle(engine, "capture_retract", {"capture_id": capture_id})
    engine.lifecycle.retract(request, authority=_owner(engine))


class _Revisions:
    """One synthetic external source whose head revision can be replaced."""

    def __init__(self, engine: BrainEngine) -> None:
        self.engine = engine
        self.original = _public_submission(engine.tasks)
        self.head: str | None = None
        self.sequence = 0

    def submit(self, text: str) -> str:
        self.sequence += 1
        capture = replace(
            self.original, payload=ReferencePayload(self.original.source_reference, text)
        )
        receipt = self.engine.sources.submit_revision(
            SourceRevisionSubmission(
                capture=capture,
                namespace=dict(
                    connector_name="synthetic",
                    connection_id="one",
                    resource_id="one",
                    external_id="one",
                ),
                revision_key=str(self.sequence),
                canonical_sha256=capture.request_sha256(),
                expected_head=self.head,
                ordering={
                    "kind": "monotonic",
                    "provider_namespace": "synthetic",
                    "epoch": "one",
                    "sequence": self.sequence,
                },
                expected_control_epoch=0,
            )
        )
        assert receipt.capture_id is not None
        self.head = receipt.capture_id
        return receipt.capture_id


# -------------------------------------------------------------- build/activate


def test_rebuild_activates_private_generation_matching_owner_projection(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    backend = FakeBackend()
    receipt = _rebuild(engine, backend)
    assert receipt.records == 3
    assert receipt.dropped_changed == 0
    assert receipt.reused_chunks == 0
    assert receipt.embedded_chunks == receipt.chunks > 6
    assert all(len(call) <= 4 for call in backend.calls)
    active = _active(engine)
    assert active.generation_id == receipt.generation_id
    assert active.component_id == COMPONENT
    assert re.fullmatch(r"[0-9a-f]{64}", active.eligibility_snapshot_digest)
    assert set(active.chunks) == _owner_refs(engine)
    assert sum(len(vectors) for vectors in active.chunks.values()) == receipt.chunks
    for vectors in active.chunks.values():
        for vector in vectors:
            assert vector.typecode == "f" and len(vector) == 384
            assert math.isclose(math.sumprod(vector, vector), 1.0, abs_tol=1e-3)
    root = _root(engine)
    generation = root / "generations" / receipt.generation_id
    assert sorted(os.listdir(generation)) == ["index.jsonl", "manifest.json", "vectors.f32"]
    for path in (root, root / "generations", generation):
        info = path.lstat()
        assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.getuid()
    for path in (root / "active.json", *generation.iterdir()):
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_nlink == 1 and info.st_uid == os.getuid()
    manifest = json.loads((generation / "manifest.json").read_bytes())
    assert manifest["schema"] == "ob.embedding-cache.v1"
    assert manifest["state"] == "complete"
    assert manifest["component_id"] == COMPONENT
    assert manifest["expected_chunks"] == manifest["actual_chunks"] == receipt.chunks
    assert manifest["expected_records"] == manifest["actual_records"] == 3
    assert (generation / "vectors.f32").stat().st_size == receipt.chunks * 1536
    for name in ("index.jsonl", "manifest.json", "vectors.f32"):
        data = (generation / name).read_bytes()
        for text in TEXTS:
            for word in text.split():
                if len(word) >= 6:
                    assert word.encode() not in data


def test_rebuild_reuses_unchanged_items_and_refuses_reuse_after_edit(tmp_path: Path) -> None:
    engine = _brain(tmp_path, TEXTS[:2])
    source = _Revisions(engine)
    old_head = source.submit("Synthetic jasper draft about tidal gauges")
    first = _rebuild(engine)
    previous = _active(engine)
    assert {ref.result_id for ref in previous.chunks} >= {old_head}
    unchanged = _rebuild(engine, backend := FakeBackend())
    assert backend.calls == []
    assert unchanged.reused_chunks == unchanged.chunks == first.chunks
    assert set(_active(engine).chunks) == set(previous.chunks)
    assert _generations(engine) == [unchanged.generation_id]
    new_head = source.submit("Synthetic jasper revision about harbor sensors")
    edited = _rebuild(engine, backend := FakeBackend())
    assert backend.texts and all("harbor" in text or "jasper" in text for text in backend.texts)
    refs = set(_active(engine).chunks)
    assert {ref.result_id for ref in refs} == {
        ref.result_id for ref in previous.chunks if ref.result_id != old_head
    } | {new_head}
    assert edited.reused_chunks > 0 and edited.embedded_chunks > 0
    assert refs == _owner_refs(engine)


def test_restricted_or_non_owner_rebuild_is_refused_before_cache_creation(
    tmp_path: Path,
) -> None:
    engine = _brain(tmp_path)
    agent = agent_authority(
        principal_id="synthetic-agent", session_id="agent", grants=frozenset({"search"})
    )
    with pytest.raises(EmbeddingPolicyError, match="owner_required"):
        cache.rebuild(engine, authority=agent, backend=FakeBackend())
    assert not (engine.profile.root / ".open-brain" / "cache").exists()


def test_rebuild_requires_enabled_consent_for_the_backend_component(tmp_path: Path) -> None:
    engine = _brain(tmp_path, enable=False)
    with _failure("consent_required"):
        _rebuild(engine)
    engine.embedding_policy.enable(OTHER, authority=_owner(engine))
    with _failure("component_mismatch"):
        _rebuild(engine)
    assert _reason(engine) == "cache_incomplete"


def test_competing_rebuild_is_refused(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    outcome: list[BaseException] = []

    def nested(call: int) -> None:
        if call == 1:
            try:
                _rebuild(engine)
            except LockBusyError as error:
                outcome.append(error)

    _rebuild(engine, FakeBackend(hook=nested))
    assert len(outcome) == 1


# --------------------------------------------------------------------- consent


def test_consent_revoked_mid_rebuild_never_publishes(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    first = _rebuild(engine)
    _Revisions(engine).submit("Synthetic jasper draft about tidal gauges")

    def revoke(call: int) -> None:
        if call == 1:
            engine.embedding_policy.disable(authority=_owner(engine))

    with _failure("consent_required"):
        _rebuild(engine, FakeBackend(hook=revoke))
    assert _generations(engine) == [first.generation_id]
    assert _reason(engine) == "consent_required"


def test_consent_reissued_mid_rebuild_is_currentness_changed(tmp_path: Path) -> None:
    engine = _brain(tmp_path)

    def reissue(call: int) -> None:
        if call == 1:
            engine.embedding_policy.disable(authority=_owner(engine))
            engine.embedding_policy.enable(COMPONENT, authority=_owner(engine))

    with _failure("currentness_changed"):
        _rebuild(engine, FakeBackend(hook=reissue))
    assert _generations(engine) == []


def test_disable_after_activation_hides_generation_immediately(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    receipt = _rebuild(engine)
    engine.embedding_policy.disable(authority=_owner(engine))
    assert cache.load_active(engine) is None
    assert _reason(engine) == "consent_required"
    assert _generations(engine) == [receipt.generation_id]
    engine.embedding_policy.enable(COMPONENT, authority=_owner(engine))
    # Re-enabling is a new policy generation; the stale generation is never resurrected.
    assert _reason(engine) == "cache_incomplete"


# ------------------------------------------------------------------ currentness


def test_record_edited_between_snapshot_and_publish_is_dropped(tmp_path: Path) -> None:
    engine = _brain(tmp_path, TEXTS[:2])
    source = _Revisions(engine)
    old_head = source.submit("Synthetic jasper draft about tidal gauges")
    heads: list[str] = []

    def edit(call: int) -> None:
        if call == 1:
            heads.append(source.submit("Synthetic jasper revision about harbor sensors"))

    receipt = _rebuild(engine, FakeBackend(hook=edit))
    assert receipt.dropped_changed == 1
    assert receipt.uncovered == 1
    assert receipt.records == 2
    refs = set(_active(engine).chunks)
    assert {ref.result_id for ref in refs}.isdisjoint({old_head, heads[0]})
    manifest = _manifest(engine)
    assert manifest["uncovered_count"] == 1
    assert manifest["uncovered_digest"] == cache._digest(
        "ob.embedding-uncovered.v1",
        sorted(
            [ref.record_type, ref.result_id, ref.revision_id, ref.field]
            for ref in _owner_refs(engine)
            if ref.result_id == heads[0]
        ),
    )


def test_record_created_during_embedding_is_uncovered_not_claimed(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    before = {ref.result_id for ref in _owner_refs(engine)}

    def capture(call: int) -> None:
        if call == 1:
            engine.capture.accept(
                TextPayload("Synthetic zircon note captured mid rebuild"),
                delivery_id=f"cache.late.{uuid4()}",
            )

    receipt = _rebuild(engine, FakeBackend(hook=capture))
    created = sorted({ref.result_id for ref in _owner_refs(engine)} - before)
    assert len(created) == 1
    assert receipt.uncovered == 1 and receipt.dropped_changed == 0
    assert receipt.records == 3
    manifest = _manifest(engine)
    assert manifest["uncovered_count"] == 1
    assert manifest["expected_records"] == manifest["actual_records"] == 3
    active = _active(engine)
    assert created[0] not in {ref.result_id for ref in active.chunks}
    assert _rebuild(engine).uncovered == 0
    assert created[0] in {ref.result_id for ref in _active(engine).chunks}


def test_record_retracted_between_snapshot_and_publish_is_dropped(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    with engine._store.connect() as connection:
        target = connection.execute(
            "SELECT result_id FROM search_documents ORDER BY result_id LIMIT 1"
        ).fetchone()[0]

    def retract(call: int) -> None:
        if call == 1:
            _retract(engine, target)

    receipt = _rebuild(engine, FakeBackend(hook=retract))
    assert receipt.dropped_changed == 1
    assert target not in {ref.result_id for ref in _active(engine).chunks}
    again = _rebuild(engine)
    assert target not in {ref.result_id for ref in _active(engine).chunks}
    assert again.records == 2


def test_mutation_after_revalidation_fails_final_check(tmp_path: Path) -> None:
    engine = _brain(tmp_path, TEXTS[:2])
    first = _rebuild(engine)
    source = _Revisions(engine)
    calls = 0

    def mutate_last() -> bool:
        nonlocal calls
        calls += 1
        if calls == total:
            source.submit("Synthetic jasper note created right before the swap")
        return False

    counter = _Counter()
    _rebuild(engine, cancelled=counter)
    total = counter.calls
    previous = _active(engine)
    with _failure("currentness_changed"):
        _rebuild(engine, cancelled=mutate_last)
    assert _active(engine).generation_id == previous.generation_id
    assert first.generation_id != previous.generation_id
    assert _generations(engine) == [previous.generation_id]


def test_final_check_uses_the_authorized_snapshot_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path)
    from open_brain_engine.engine.paging import read_snapshot as original

    opened: list[bool] = []

    def tracked(target: BrainEngine):  # type: ignore[no-untyped-def]
        opened.append(True)
        return original(target)

    monkeypatch.setattr(cache, "read_snapshot", tracked)
    receipt = _rebuild(engine)
    # Preflight, plan, revalidation and the fenced final check all share one path.
    assert len(opened) == 4
    monkeypatch.undo()
    assert _active(engine).generation_id == receipt.generation_id


def test_commit_between_revalidation_and_lease_is_currentness_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path, TEXTS[:2])
    previous = _rebuild(engine)
    source = _Revisions(engine)
    original = cache._write_generation

    def write_then_commit(*args: object, **kwargs: object) -> str:
        digest = original(*args, **kwargs)  # type: ignore[arg-type]
        source.submit("Synthetic jasper note committed before the lease")
        return digest

    monkeypatch.setattr(cache, "_write_generation", write_then_commit)
    with _failure("currentness_changed"):
        _rebuild(engine)
    assert _active(engine).generation_id == previous.generation_id
    assert _generations(engine) == [previous.generation_id]


# ------------------------------------------------------------- failure safety


class _Counter:
    def __init__(self, cancel_at: int | None = None) -> None:
        self.calls = 0
        self.cancel_at = cancel_at

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls == self.cancel_at


def test_cancellation_at_each_phase_leaves_previous_generation_active(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    first = _rebuild(engine)
    counter = _Counter()
    _rebuild(engine, cancelled=counter)
    total = counter.calls
    assert total >= 4
    previous = _active(engine).generation_id
    for cancel_at in range(1, total + 1):
        with _failure("cancelled"):
            _rebuild(engine, cancelled=_Counter(cancel_at))
        assert _active(engine).generation_id == previous
        assert _generations(engine) == [previous]
    assert first.generation_id != previous


def test_cancellation_between_embedding_batches(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    backend = FakeBackend()
    # Calls: start, after the snapshot, then one before each of the two batches.
    with _failure("cancelled"):
        _rebuild(engine, backend, cancelled=_Counter(4))
    assert len(backend.calls) == 1
    assert _reason(engine) == "cache_incomplete"
    assert _generations(engine) == []


def test_helper_failure_leaves_previous_generation(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    first = _rebuild(engine)
    _Revisions(engine).submit("Synthetic jasper draft about tidal gauges")

    class Broken(FakeBackend):
        def embed(self, purpose: str, texts: Sequence[str]) -> list[EmbeddedItem]:
            raise EmbeddingFailure("worker_failed")

    with _failure("worker_failed"):
        _rebuild(engine, Broken())
    assert _active(engine).generation_id == first.generation_id


def test_disk_full_during_vector_write_keeps_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path)
    first = _rebuild(engine)
    _Revisions(engine).submit("Synthetic jasper draft about tidal gauges")
    original = cache._write_file

    def full(directory: int, name: str, data: bytes) -> None:
        if name == "vectors.f32":
            raise OSError(errno.ENOSPC, "synthetic disk full")
        original(directory, name, data)

    monkeypatch.setattr(cache, "_write_file", full)
    with _failure("cache_incomplete"):
        _rebuild(engine)
    assert _active(engine).generation_id == first.generation_id
    assert _generations(engine) == [first.generation_id]


class _Crash(BaseException):
    pass


def test_crash_between_staging_and_swap_keeps_previous_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path)
    first = _rebuild(engine)
    _Revisions(engine).submit("Synthetic jasper draft about tidal gauges")

    def crash(*_args: object) -> None:
        raise _Crash

    with monkeypatch.context() as patched:
        patched.setattr(cache, "_activate", crash)
        with pytest.raises(_Crash):
            _rebuild(engine)
    assert len(_generations(engine)) == 2
    assert _active(engine).generation_id == first.generation_id
    second = _rebuild(engine)
    assert _generations(engine) == [second.generation_id]
    assert _active(engine).generation_id == second.generation_id


def test_quota_preflight_and_streaming_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path)
    backend = FakeBackend()
    monkeypatch.setattr(cache, "MAX_CHUNKS", 5)
    with _failure("cache_incomplete"):
        _rebuild(engine, backend)
    assert backend.calls == []
    monkeypatch.setattr(cache, "MAX_CHUNKS", 7)
    with _failure("cache_incomplete"):
        _rebuild(engine, backend)
    assert backend.calls
    monkeypatch.setattr(cache, "MAX_CHUNKS", 100_000)
    receipt = _rebuild(engine)
    monkeypatch.setattr(cache, "MAX_GENERATION_SET_BYTES", receipt.chunks * 1536 + 100)
    _Revisions(engine).submit("Synthetic jasper draft about tidal gauges")
    with _failure("cache_incomplete"):
        _rebuild(engine)
    assert _active(engine).generation_id == receipt.generation_id
    monkeypatch.setattr(cache, "MAX_GENERATION_SET_BYTES", 512 * 1024 * 1024)
    existing = sum(path.lstat().st_size for path in _root(engine).rglob("*") if path.is_file())
    stage = _root(engine) / "stage" / COMPONENT
    stage.mkdir(parents=True, mode=0o700)
    (stage / "model.onnx").write_bytes(b"\0" * 4096)
    # The D7 helper stage counts toward the 1 GiB total: preflight refuses before any helper.
    monkeypatch.setattr(cache, "MAX_TOTAL_BYTES", existing + 4095)
    backend = FakeBackend()
    with _failure("cache_incomplete"):
        _rebuild(engine, backend)
    assert backend.calls == []
    assert _active(engine).generation_id == receipt.generation_id
    monkeypatch.setattr(cache, "MAX_TOTAL_BYTES", existing + 4096 + 1024 * 1024)
    assert _rebuild(engine).records == 4


LONG = "Synthetic onyx " + "survey " * 30 + "closing remark"


def _excluded(engine: BrainEngine) -> set[str]:
    return {ref.result_id for ref in _owner_refs(engine)} - {
        ref.result_id for ref in _active(engine).chunks
    }


def test_oversized_field_excludes_its_record_not_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path, (*TEXTS, LONG))
    monkeypatch.setattr(cache, "MAX_DOCUMENT_BYTES", 120)
    backend = FakeBackend()
    receipt = _rebuild(engine, backend)
    assert receipt.excluded_oversize == 1 and receipt.records == 3
    assert backend.texts and not any("onyx" in text for text in backend.texts)
    (excluded,) = _excluded(engine)
    manifest = _manifest(engine)
    assert manifest["excluded_oversize_count"] == 1
    assert manifest["excluded_oversize_digest"] == cache._digest(
        "ob.embedding-excluded-oversize.v1",
        sorted(
            [ref.record_type, ref.result_id, ref.revision_id, ref.field]
            for ref in _owner_refs(engine)
            if ref.result_id == excluded
        ),
    )
    assert manifest["uncovered_count"] == 0


class _Refusing(FakeBackend):
    """Refuses any exchange that carries the marker word, as the helper's ``input_limit``."""

    def embed(self, purpose: str, texts: Sequence[str]) -> list[EmbeddedItem]:
        self.calls.append(list(texts))
        if any("onyx" in text for text in texts):
            raise EmbeddingFailure("input_limit")
        return [_item(slot, text) for slot, text in enumerate(texts)]


def test_helper_input_limit_isolates_the_refused_item_singly(tmp_path: Path) -> None:
    engine = _brain(tmp_path, (*TEXTS, LONG))
    backend = _Refusing()
    receipt = _rebuild(engine, backend)
    assert receipt.excluded_oversize == 1 and receipt.records == 3
    refused = [call for call in backend.calls if any("onyx" in text for text in call)]
    multi = [call for call in refused if len(call) > 1]
    singles = [call for call in backend.calls if len(call) == 1]
    # At most one extra single exchange per item of each refused batch.
    assert multi and len(singles) <= sum(len(call) for call in multi)
    assert len(_excluded(engine)) == 1
    assert _manifest(engine)["excluded_oversize_count"] == 1


def test_single_retries_stay_cancellable(tmp_path: Path) -> None:
    engine = _brain(tmp_path, (*TEXTS, LONG))
    _rebuild(engine, _Refusing())
    counter = _Counter()
    first = _rebuild(engine, _Refusing(), cancelled=counter)
    for cancel_at in range(1, counter.calls + 1):
        with _failure("cancelled"):
            _rebuild(engine, _Refusing(), cancelled=_Counter(cancel_at))
        assert _active(engine).generation_id == first.generation_id
        assert _generations(engine) == [first.generation_id]


def test_rebuild_budget_is_a_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _brain(tmp_path)
    monkeypatch.setattr(cache, "REBUILD_SECONDS", -1.0)
    with _failure("deadline"):
        _rebuild(engine)
    assert _generations(engine) == []


def test_fsync_failure_after_pointer_swap_keeps_the_named_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path)
    first = _rebuild(engine)
    _Revisions(engine).submit("Synthetic jasper draft about tidal gauges")
    real_replace, real_fsync = os.replace, os.fsync
    swapped: list[bool] = []

    def replace(*args: object, **kwargs: object) -> None:
        real_replace(*args, **kwargs)  # type: ignore[arg-type]
        if args[1] == "active.json":
            swapped.append(True)

    def fsync(descriptor: int) -> None:
        if swapped:
            swapped.clear()
            raise OSError(errno.EIO, "synthetic fsync failure")
        real_fsync(descriptor)

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", replace)
        patched.setattr(os, "fsync", fsync)
        with _failure("cache_incomplete"):
            _rebuild(engine)
    pointer = json.loads((_root(engine) / "active.json").read_bytes())
    assert pointer["generation_id"] != first.generation_id
    assert pointer["generation_id"] in _generations(engine)
    assert _active(engine).generation_id == pointer["generation_id"]
    again = _rebuild(engine)
    assert _generations(engine) == [again.generation_id]


# --------------------------------------------------------------- load checks


def _generation_file(engine: BrainEngine, name: str) -> Path:
    return _root(engine) / "generations" / _active(engine).generation_id / name


@pytest.mark.parametrize("name", ["vectors.f32", "index.jsonl", "manifest.json"])
def test_corrupted_generation_file_is_invalid(tmp_path: Path, name: str) -> None:
    engine = _brain(tmp_path)
    _rebuild(engine)
    path = _generation_file(engine, name)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    assert cache.load_active(engine) is None
    assert _reason(engine) == "cache_invalid"


def test_repeat_load_reuses_decoded_generation_and_revocation_is_immediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path)
    _rebuild(engine)
    original = cache._load_generation
    loads: list[str] = []

    def counted(directory: int, generation_id: str, manifest_sha256: str) -> object:
        loads.append(generation_id)
        return original(directory, generation_id, manifest_sha256)

    monkeypatch.setattr(cache, "_load_generation", counted)
    first = _active(engine)
    assert _active(engine) is first
    assert loads == [first.generation_id]
    engine.embedding_policy.disable(authority=_owner(engine))
    assert cache.load_active(engine) is None
    assert _reason(engine) == "consent_required"
    engine.embedding_policy.enable(COMPONENT, authority=_owner(engine))
    assert _reason(engine) == "cache_incomplete"
    second = _rebuild(engine)
    assert _active(engine).generation_id == second.generation_id
    assert _active(engine) is _active(engine)
    assert loads.count(second.generation_id) == 1
    pointer = _root(engine) / "active.json"
    os.utime(pointer, ns=(1, 1))
    assert _active(engine).generation_id == second.generation_id
    assert loads.count(second.generation_id) == 2


def test_corrupted_or_missing_pointer_and_manifest(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    _rebuild(engine)
    pointer = _root(engine) / "active.json"
    manifest = _generation_file(engine, "manifest.json")
    saved_manifest = manifest.read_bytes()
    manifest.unlink()
    assert _reason(engine) == "cache_incomplete"
    manifest.write_bytes(saved_manifest)
    manifest.chmod(0o600)
    saved = pointer.read_bytes()
    pointer.write_bytes(b"{not json")
    assert _reason(engine) == "cache_invalid"
    pointer.write_bytes(saved.replace(b'"generation_id":"', b'"generation_id":"0'))
    assert _reason(engine) == "cache_invalid"
    pointer.unlink()
    assert _reason(engine) == "cache_incomplete"
    pointer.write_bytes(saved)
    pointer.chmod(0o600)
    assert cache.load_active(engine) is not None


def test_incomplete_manifest_state_is_never_selected(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    _rebuild(engine)
    manifest = _generation_file(engine, "manifest.json")
    value = json.loads(manifest.read_bytes())
    value["state"] = "staging"
    from open_brain_engine.core.ids import portable_canonical_json_bytes

    raw = portable_canonical_json_bytes(value)
    manifest.write_bytes(raw)
    pointer = _root(engine) / "active.json"
    pointer_value = json.loads(pointer.read_bytes())
    pointer_value["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    pointer.write_bytes(portable_canonical_json_bytes(pointer_value))
    assert _reason(engine) == "cache_incomplete"


def test_links_and_modes_are_rejected(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    _rebuild(engine)
    vectors = _generation_file(engine, "vectors.f32")
    vectors.chmod(0o644)
    assert _reason(engine) == "cache_invalid"
    vectors.chmod(0o600)
    alias = tmp_path / "alias"
    os.link(vectors, alias)
    assert _reason(engine) == "cache_invalid"
    alias.unlink()
    moved = tmp_path / "moved"
    vectors.rename(moved)
    vectors.symlink_to(moved)
    assert _reason(engine) == "cache_invalid"
    vectors.unlink()
    moved.rename(vectors)
    assert cache.load_active(engine) is not None
    _root(engine).chmod(0o755)
    assert _reason(engine) == "cache_invalid"
    with _failure("cache_invalid"):
        _rebuild(engine)
    _root(engine).chmod(0o700)
    generations = _root(engine) / "generations"
    stray = generations / ("0" * 32)
    stray.mkdir(mode=0o700)
    (stray / "evidence.txt").write_bytes(b"not ours")
    with _failure("cache_invalid"):
        _rebuild(engine)
    assert (stray / "evidence.txt").exists()


# -------------------------------------------------------------------- Portable


def test_portable_export_excludes_cache_and_restore_starts_without_vectors(
    tmp_path: Path,
) -> None:
    engine = _brain(tmp_path)
    receipt = _rebuild(engine)
    (_root(engine) / "stage" / COMPONENT).mkdir(parents=True, mode=0o700)
    (_root(engine) / "stage" / COMPONENT / "component.json").write_bytes(b"{}")
    vectors = (_root(engine) / "generations" / receipt.generation_id / "vectors.f32").read_bytes()
    archive, destination = tmp_path / "archive", tmp_path / "restored"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    for path in archive.rglob("*"):
        assert "embedding-v1" not in str(path.relative_to(archive))
        if path.is_file():
            data = path.read_bytes()
            assert b"embedding-v1" not in data
            assert vectors[:1536] not in data
            assert receipt.generation_id.encode() not in data
    engine.portability.import_clean(archive, destination, import_id="import_" + str(uuid4()))
    restored = BrainEngine.open(open_existing_single_user_local(destination))
    assert not (destination / ".open-brain" / "cache" / "embedding-v1").exists()
    assert _reason(restored) == "consent_required"
    restored.embedding_policy.enable(COMPONENT, authority=_owner(restored))
    assert _reason(restored) == "cache_incomplete"
    assert _rebuild(restored).records == 3


# ----------------------------------------------------------- real helper run


@pytest.mark.integration
def test_real_helper_rebuild_on_twenty_record_brain(tmp_path: Path) -> None:
    from open_brain.services import embedding_helper as eh
    from packages.app.tests import embedding_fake as fake
    from packages.app.tests.unit.test_embedding_helper import _real_inputs

    inputs = _real_inputs()
    if inputs is None:
        pytest.skip("contributor helper, runtime or MiniLM assets are not built")
    files = {path: source.read_bytes() for path, source in inputs.items()}
    files[f"{fake.LICENSES}/NOTICE.txt"] = b"synthetic notice"
    base = fake.install(
        tmp_path / "install",
        files,
        {"profile_id": "ob.minilm-wordpiece-mean.v1", "tokenizer_build": "tokenizers 0.22.1"},
    )
    backend = eh.HelperEmbeddingBackend(eh.discover_embedding_resource(base), tmp_path / "brain")
    texts = tuple(
        f"Synthetic field note {index}: {topic} observations recorded for review."
        for index, topic in enumerate(["orchard", "harbor", "glacier", "meadow", "canyon"] * 4)
    )
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    for index, text in enumerate(texts):
        engine.capture.accept(TextPayload(text), delivery_id=f"real.{index}")
    engine.embedding_policy.enable(backend.component_id, authority=_owner(engine))
    receipt = cache.rebuild(engine, authority=_owner(engine), backend=backend)
    assert receipt.records == 20 and receipt.dropped_changed == 0
    active = _active(engine)
    assert set(active.chunks) == _owner_refs(engine)
    for vectors in active.chunks.values():
        for vector in vectors:
            assert math.isclose(math.sumprod(vector, vector), 1.0, abs_tol=1e-3)
    assert eh.stage_bytes(engine.profile.root) > 0


def test_manifest_with_consistent_hash_but_wrong_shape_is_invalid(tmp_path: Path) -> None:
    from open_brain_engine.core.ids import portable_canonical_json_bytes

    engine = _brain(tmp_path)
    _rebuild(engine)
    manifest = _generation_file(engine, "manifest.json")
    value = json.loads(manifest.read_bytes())
    del value["policy_generation"]
    raw = portable_canonical_json_bytes(value)
    manifest.write_bytes(raw)
    pointer = _root(engine) / "active.json"
    pointer_value = json.loads(pointer.read_bytes())
    pointer_value["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    pointer.write_bytes(portable_canonical_json_bytes(pointer_value))
    assert _reason(engine) == "cache_invalid"
