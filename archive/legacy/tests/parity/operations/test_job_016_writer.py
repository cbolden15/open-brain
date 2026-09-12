from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import LockScope
from open_brain_engine.storage.filesystem import DurabilityError, atomic_write_new

from open_brain_legacy.operations.index import IndexRoots, check_index
from open_brain_legacy.operations.index_writer import IndexEffectCapability, IndexWriterApplication
from open_brain_legacy.operations.models import DeploymentTarget
from open_brain_legacy.operations.replay_journal import SqliteReplayJournal
from open_brain_legacy.operations.writer_jobs import (
    EffectCommand,
    JobRunDisposition,
    JobRunResult,
    ReplayJournal,
    WriterJobError,
    WriterJobInvocation,
    get_writer_job_spec,
    run_writer_job,
)
from tests.unit.storage._factories import FixedClock


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


class DeterministicEmbedder:
    model_id = "synthetic-local-v1"
    requires_cloud_authority = False
    requires_external_egress = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        digest = sha256(text.encode("utf-8")).digest()
        return tuple(round(value / 255, 8) for value in digest[:4])


class FailFirstCompletionJournal(ReplayJournal):
    def __init__(self, journal: ReplayJournal) -> None:
        self._journal = journal
        self.fail_completion = True

    def completed(self, job_id: str, replay_key: str) -> JobRunResult | None:
        return self._journal.completed(job_id, replay_key)

    def begin(self, job_id: str, replay_key: str, request_digest_sha256: str) -> None:
        self._journal.begin(job_id, replay_key, request_digest_sha256)

    def complete(self, result: JobRunResult) -> None:
        if self.fail_completion:
            self.fail_completion = False
            raise RuntimeError("synthetic crash before journal completion")
        self._journal.complete(result)


def _roots(tmp_path: Path) -> IndexRoots:
    pages = tmp_path / "pages"
    captures = tmp_path / "captures"
    state = tmp_path / "state"
    pages.mkdir()
    captures.mkdir()
    state.mkdir()
    (pages / "alpha.md").write_text("Synthetic index page.\n", encoding="utf-8")
    return IndexRoots(pages_root=pages, captures_root=captures, output_root=state)


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _parts(
    tmp_path: Path,
) -> tuple[
    IndexRoots,
    DeterministicEmbedder,
    IndexWriterApplication,
    IndexEffectCapability,
]:
    roots = _roots(tmp_path)
    embedder = DeterministicEmbedder()
    application = IndexWriterApplication(
        database_name=roots.database_name,
        embedding_model_id=embedder.model_id,
    )
    capability = IndexEffectCapability(
        root=roots.output_root,
        roots=roots,
        embedder=embedder,
        privacy=_privacy(),
    )
    return roots, embedder, application, capability


def test_job_016_writer_builds_once_and_replays_verified_index(tmp_path: Path) -> None:
    roots, embedder, application, capability = _parts(tmp_path)
    lease = RecordingLease()
    with SqliteReplayJournal(root=roots.output_root, clock=FixedClock()) as journal:
        first = run_writer_job(
            job_id="JOB-016",
            root=roots.output_root,
            replay_key="index-2026-08-16",
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )
        replay = run_writer_job(
            job_id="JOB-016",
            root=roots.output_root,
            replay_key="index-2026-08-16",
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    checked = check_index(target=DeploymentTarget.CANONICAL_WRITER, roots=roots)
    assert first.disposition is JobRunDisposition.APPLIED
    assert first.effect_count == 1
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert checked.available is True
    assert embedder.calls == ["Synthetic index page.\n"]
    assert lease.scopes == [LockScope.INDEX, LockScope.INDEX]


def test_job_016_recovers_after_crash_before_journal_completion(tmp_path: Path) -> None:
    roots, embedder, application, capability = _parts(tmp_path)
    lease = RecordingLease()
    with SqliteReplayJournal(root=roots.output_root, clock=FixedClock()) as durable:
        journal = FailFirstCompletionJournal(durable)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            run_writer_job(
                job_id="JOB-016",
                root=roots.output_root,
                replay_key="index-journal-crash",
                journal=journal,
                application=application,
                effect_capability=capability,
                lease=lease,
            )

        replay = run_writer_job(
            job_id="JOB-016",
            root=roots.output_root,
            replay_key="index-journal-crash",
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    assert replay.disposition is JobRunDisposition.REPLAYED
    assert embedder.calls == ["Synthetic index page.\n"]


def test_job_016_recovers_when_applied_pointer_write_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, embedder, application, capability = _parts(tmp_path)
    lease = RecordingLease()
    failed = False

    def fail_first_pointer_write(
        *, root: Path, relative: str | PurePosixPath, data: bytes
    ) -> object:
        nonlocal failed
        if str(relative).endswith(".applied.json") and not failed:
            failed = True
            raise DurabilityError("synthetic pointer crash")
        return atomic_write_new(root=root, relative=relative, data=data)

    monkeypatch.setattr(
        'open_brain_legacy.operations.index_writer.atomic_write_new',
        fail_first_pointer_write,
    )
    with SqliteReplayJournal(root=roots.output_root, clock=FixedClock()) as journal:
        with pytest.raises(DurabilityError, match="synthetic pointer crash"):
            run_writer_job(
                job_id="JOB-016",
                root=roots.output_root,
                replay_key="index-pointer-crash",
                journal=journal,
                application=application,
                effect_capability=capability,
                lease=lease,
            )

        replay = run_writer_job(
            job_id="JOB-016",
            root=roots.output_root,
            replay_key="index-pointer-crash",
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    assert replay.disposition is JobRunDisposition.REPLAYED
    assert embedder.calls == ["Synthetic index page.\n"]


def test_job_016_reservation_rejects_request_digest_conflict(tmp_path: Path) -> None:
    roots, _embedder, application, capability = _parts(tmp_path)
    spec = get_writer_job_spec("JOB-016")
    invocation = WriterJobInvocation(
        job_id=spec.job_id,
        command=spec.command,
        replay_key="index-reservation-conflict",
        effect=spec.effect,
        review_boundary=spec.review_boundary,
        local_only=spec.local_only,
        dry_run=spec.dry_run,
        apply_review_decisions=False,
        approved_records=(),
        approval_bindings=(),
        planned_actions=spec.planned_actions,
        personal_local_only=False,
        cutoff=None,
    )
    prepared = application.prepare(invocation)
    capability.reserve(EffectCommand("JOB-016", invocation.replay_key, "a" * 64, prepared))

    with pytest.raises(WriterJobError, match="reservation conflict"):
        capability.reserve(EffectCommand("JOB-016", invocation.replay_key, "b" * 64, prepared))

    assert capability.root == roots.output_root


def test_job_016_capability_rejects_mismatched_preparation_config(tmp_path: Path) -> None:
    _roots_value, _embedder, _application, capability = _parts(tmp_path)
    spec = get_writer_job_spec("JOB-016")
    invocation = WriterJobInvocation(
        job_id=spec.job_id,
        command=spec.command,
        replay_key="index-config-conflict",
        effect=spec.effect,
        review_boundary=spec.review_boundary,
        local_only=spec.local_only,
        dry_run=spec.dry_run,
        apply_review_decisions=False,
        approved_records=(),
        approval_bindings=(),
        planned_actions=spec.planned_actions,
        personal_local_only=False,
        cutoff=None,
    )
    mismatched = IndexWriterApplication(
        database_name="other-index.sqlite3",
        embedding_model_id="other-model-v1",
    ).prepare(invocation)

    with pytest.raises(WriterJobError, match="invalid index effect command"):
        capability.reserve(EffectCommand("JOB-016", invocation.replay_key, "a" * 64, mismatched))
