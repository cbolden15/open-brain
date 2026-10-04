"""Recovery requires actual baseline bytes, not an inventory digest promise."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import (
    BrainEngine,
    CaptureFault,
    CaptureSubmission,
    InjectedFault,
    TextPayload,
)
from open_brain_engine.engine.capture_recovery import emit_owner_capture_plan
from open_brain_engine.engine.custody_recovery import emit_owner_custody_plan
from open_brain_engine.engine.recovery_closure import RecoveryClosure, RecoveryClosureBounds
from open_brain_engine.engine.recovery_journal import RecoveryBaseline, RecoveryHead, RecoveryRecord
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.v8_custody import CUSTODY_PATH
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local


@pytest.fixture
def closure(tmp_path: Path) -> RecoveryClosure:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "primary"))
    engine.capture.accept(TextPayload("Synthetic baseline body"), delivery_id="baseline.owner")
    archive = tmp_path / "baseline"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    files = tuple(sorted(validated_portable_snapshot(archive).files.items()))
    with engine._store.connect() as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    baseline = RecoveryBaseline(
        identity[0], identity[1], sha256(dict(files)["portable-manifest.json"]).hexdigest()
    )
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "recovery", frozenset(), None, owner=True
    )
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=TextPayload("Synthetic pending body"),
        delivery_id="new.owner",
    )
    engine.ingestion.enqueue(submission)
    custody = emit_owner_custody_plan(engine, submission, baseline=baseline, authority=owner)
    engine._faults.add(CaptureFault.AFTER_CAPTURE_RESERVATION)
    with pytest.raises(InjectedFault):
        engine.capture.submit(submission)
    capture = emit_owner_capture_plan(engine, submission, baseline=baseline, authority=owner)
    dependency = b"synthetic independent dependency"
    first = RecoveryRecord(
        baseline=baseline, sequence=1, previous_sha256=baseline.artifact_sha256,
        kind="capture_custody", payload=custody.to_bytes(),
        dependencies=(sha256(dependency).hexdigest(),),
    )
    second = RecoveryRecord(
        baseline=baseline, sequence=2, previous_sha256=first.record_sha256,
        kind="capture", payload=capture.to_bytes(), dependencies=(first.record_sha256,),
    )
    return RecoveryClosure(
        bounds=RecoveryClosureBounds(4096, 16 * 1024 * 1024, 32, 16 * 1024 * 1024),
        baseline_files=files, records=(first, second),
        expected_head=RecoveryHead(baseline, 2, second.record_sha256),
        dependency_payloads=(dependency,),
    )


def test_actual_complete_bytes_have_stable_commitment(closure: RecoveryClosure) -> None:
    assert replace(closure).closure_sha256 == closure.closure_sha256
    assert len(closure.closure_sha256) == 64
    assert closure.records[0].kind == "capture_custody"
    assert closure.records[1].kind == "capture"


def test_self_consistent_hashes_do_not_admit_invalid_portable_semantics(
    closure: RecoveryClosure,
) -> None:
    files = dict(closure.baseline_files)
    files[CUSTODY_PATH] = b"{}"
    manifest = json.loads(files["portable-manifest.json"])
    for entry in manifest["files"]:
        entry["sha256"] = sha256(files[entry["path"]]).hexdigest()
    files["portable-manifest.json"] = canonical(manifest)
    baseline = replace(
        closure.expected_head.baseline,
        artifact_sha256=sha256(files["portable-manifest.json"]).hexdigest(),
    )
    # No journal needed: even an empty, correctly committed closure must reject
    # a semantically corrupt required sidecar before a backend can consume it.
    with pytest.raises(ValueError, match="baseline evidence"):
        replace(closure, baseline_files=tuple(sorted(files.items())), records=(),
                expected_head=RecoveryHead(baseline, 0, baseline.artifact_sha256))


def test_dependency_bytes_share_total_journal_bound(closure: RecoveryClosure) -> None:
    record_bytes = sum(len(record.to_bytes()) for record in closure.records)
    with pytest.raises(ValueError, match="journal exceeds"):
        replace(closure, bounds=replace(closure.bounds, max_journal_bytes=record_bytes))


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_bounds_require_positive_integers(invalid: object) -> None:
    with pytest.raises(ValueError, match="closure bounds"):
        RecoveryClosureBounds(invalid, 1, 1, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("damage", [
    "missing", "extra", "body", "manifest", "order", "duplicate", "prefix", "head",
    "dependency", "no_dependency", "epoch", "brain", "file_count", "baseline_bytes",
    "record_count", "journal_bytes",
])
def test_incomplete_or_unbound_bytes_refuse(closure: RecoveryClosure, damage: str) -> None:
    with pytest.raises(ValueError):
        if damage in {"missing", "extra", "body", "manifest", "order", "duplicate"}:
            files = dict(closure.baseline_files)
            if damage == "missing":
                files.pop(next(path for path in files if path != "portable-manifest.json"))
            elif damage == "extra":
                files["extra.bin"] = b"synthetic"
            elif damage == "body":
                files[next(path for path in files if path != "portable-manifest.json")] += b"!"
            elif damage == "manifest":
                files["portable-manifest.json"] += b" "
            entries = tuple(sorted(files.items()))
            if damage == "order":
                entries = tuple(reversed(entries))
            elif damage == "duplicate":
                entries += (entries[-1],)
            replace(closure, baseline_files=entries)
        elif damage == "prefix":
            replace(closure, records=closure.records[:1])
        elif damage == "head":
            replace(closure, expected_head=replace(closure.expected_head, record_sha256="0" * 64))
        elif damage in {"dependency", "no_dependency"}:
            replace(closure, dependency_payloads=(b"corrupted",) if damage == "dependency" else ())
        elif damage in {"epoch", "brain"}:
            baseline = replace(closure.expected_head.baseline, **(
                {"issuer_epoch": 2} if damage == "epoch" else {"brain_id": "brn_" + "a" * 26}
            ))
            replace(closure, expected_head=replace(closure.expected_head, baseline=baseline))
        else:
            field = {
                "file_count": "max_baseline_files", "baseline_bytes": "max_baseline_bytes",
                "record_count": "max_records", "journal_bytes": "max_journal_bytes",
            }[damage]
            replace(closure, bounds=replace(closure.bounds, **{field: 1}))
