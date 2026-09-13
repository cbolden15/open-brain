import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import open_brain_legacy.operations.retention as retention_module
from open_brain_legacy.operations.retention import (
    RetentionApprovalError,
    RetentionArtifactKind,
    RetentionCandidate,
    RetentionDisposition,
    RetentionPathError,
    RetentionPlanStaleError,
    RetentionReplayConflictError,
    plan_retention,
    run_retention,
)


def test_job_024_defaults_to_dry_run_and_excludes_recovery_state(tmp_path: Path) -> None:
    root = tmp_path / "retention-root"
    root.mkdir()
    expired = root / "expired.bin"
    recovery = root / "recovery-state.json"
    expired.write_bytes(b"synthetic expired artifact")
    recovery.write_bytes(b"synthetic recovery state")
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)

    plan = plan_retention(
        root=root,
        cutoff=cutoff,
        candidates=(
            RetentionCandidate(
                artifact_id="artifact_expired",
                relative_path="expired.bin",
                expires_at=cutoff - timedelta(seconds=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            ),
            RetentionCandidate(
                artifact_id="artifact_recovery",
                relative_path="recovery-state.json",
                expires_at=cutoff - timedelta(days=1),
                kind=RetentionArtifactKind.RECOVERY_CRITICAL,
            ),
        ),
    )

    receipt = run_retention(root=root, plan=plan, replay_key="job-024-default")

    assert tuple(item.artifact_id for item in plan.deletions) == ("artifact_expired",)
    assert plan.protected_count == 1
    assert receipt.disposition is RetentionDisposition.DRY_RUN
    assert receipt.deleted_count == 0
    assert expired.exists()
    assert recovery.exists()


def test_job_024_apply_requires_exact_approval_and_replays_once(tmp_path: Path) -> None:
    root = tmp_path / "retention-root"
    root.mkdir()
    expired = root / "expired.bin"
    expired.write_bytes(b"synthetic expired artifact")
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)
    plan = plan_retention(
        root=root,
        cutoff=cutoff,
        candidates=(
            RetentionCandidate(
                artifact_id="artifact_expired",
                relative_path="expired.bin",
                expires_at=cutoff - timedelta(seconds=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            ),
        ),
    )

    with pytest.raises(RetentionApprovalError, match="approval"):
        run_retention(
            root=root,
            plan=plan,
            replay_key="job-024-apply",
            apply=True,
            approval_digest="0" * 64,
        )
    assert expired.exists()

    applied = run_retention(
        root=root,
        plan=plan,
        replay_key="job-024-apply",
        apply=True,
        approval_digest=plan.digest_sha256,
    )
    replayed = run_retention(
        root=root,
        plan=plan,
        replay_key="job-024-apply",
        apply=True,
        approval_digest=plan.digest_sha256,
    )

    assert applied.disposition is RetentionDisposition.APPLIED
    assert applied.deleted_count == 1
    assert applied.replayed is False
    assert replayed.disposition is RetentionDisposition.APPLIED
    assert replayed.deleted_count == 1
    assert replayed.replayed is True
    assert not expired.exists()

    replacement = root / "replacement.bin"
    replacement.write_bytes(b"synthetic replacement")
    replacement_plan = plan_retention(
        root=root,
        cutoff=cutoff,
        candidates=(
            RetentionCandidate(
                artifact_id="artifact_replacement",
                relative_path="replacement.bin",
                expires_at=cutoff - timedelta(seconds=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            ),
        ),
    )
    with pytest.raises(RetentionReplayConflictError, match="replay"):
        run_retention(
            root=root,
            plan=replacement_plan,
            replay_key="job-024-apply",
            apply=True,
            approval_digest=replacement_plan.digest_sha256,
        )
    assert replacement.exists()


def test_job_024_approved_plan_is_bound_to_its_exact_root(tmp_path: Path) -> None:
    planned_root = tmp_path / "planned-root"
    other_root = tmp_path / "other-root"
    planned_root.mkdir()
    other_root.mkdir()
    for root in (planned_root, other_root):
        (root / "expired.bin").write_bytes(b"synthetic identical artifact")
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)
    plan = plan_retention(
        root=planned_root,
        cutoff=cutoff,
        candidates=(
            RetentionCandidate(
                artifact_id="artifact_expired",
                relative_path="expired.bin",
                expires_at=cutoff - timedelta(seconds=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            ),
        ),
    )

    with pytest.raises(RetentionApprovalError, match="plan"):
        run_retention(
            root=other_root,
            plan=plan,
            replay_key="job-024-wrong-root",
            apply=True,
            approval_digest=plan.digest_sha256,
        )

    assert (planned_root / "expired.bin").exists()
    assert (other_root / "expired.bin").exists()


def test_job_024_rejects_stale_plan_before_any_deletion(tmp_path: Path) -> None:
    root = tmp_path / "retention-root"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"synthetic first")
    second.write_bytes(b"synthetic second")
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)
    plan = plan_retention(
        root=root,
        cutoff=cutoff,
        candidates=tuple(
            RetentionCandidate(
                artifact_id=f"artifact_{name}",
                relative_path=f"{name}.bin",
                expires_at=cutoff - timedelta(days=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            )
            for name in ("first", "second")
        ),
    )
    second.write_bytes(b"synthetic changed after approval")

    with pytest.raises(RetentionPlanStaleError, match="stale"):
        run_retention(
            root=root,
            plan=plan,
            replay_key="job-024-stale",
            apply=True,
            approval_digest=plan.digest_sha256,
        )

    assert first.exists()
    assert second.exists()


def test_job_024_refuses_regular_file_replacement_at_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "retention-root"
    root.mkdir()
    victim = root / "victim.bin"
    victim.write_bytes(b"approved bytes")
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)
    plan = plan_retention(
        root=root,
        cutoff=cutoff,
        candidates=(
            RetentionCandidate(
                artifact_id="artifact_victim",
                relative_path="victim.bin",
                expires_at=cutoff - timedelta(days=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            ),
        ),
    )
    actual_unlink = cast(Callable[..., None], retention_module._unlink_candidate)

    def replace_then_unlink(*args: object, **kwargs: object) -> None:
        replacement = root / "replacement.tmp"
        replacement.write_bytes(b"replacement bytes")
        os.replace(replacement, victim)
        actual_unlink(*args, **kwargs)

    monkeypatch.setattr(retention_module, "_unlink_candidate", replace_then_unlink)

    with pytest.raises(RetentionPlanStaleError, match="stale"):
        run_retention(
            root=root,
            plan=plan,
            replay_key="job-024-replacement-race",
            apply=True,
            approval_digest=plan.digest_sha256,
        )

    assert victim.read_bytes() == b"replacement bytes"


def test_job_024_rejects_path_escape_and_symlink_candidates(tmp_path: Path) -> None:
    root = tmp_path / "retention-root"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"synthetic outside")
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    nested_outside = outside_directory / "victim.bin"
    nested_outside.write_bytes(b"synthetic nested outside")
    (root / "linked.bin").symlink_to(outside)
    (root / "linked-directory").symlink_to(outside_directory, target_is_directory=True)
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)

    for relative_path in (
        "../outside.bin",
        "linked.bin",
        "linked-directory/victim.bin",
    ):
        with pytest.raises(RetentionPathError, match="candidate path"):
            plan_retention(
                root=root,
                cutoff=cutoff,
                candidates=(
                    RetentionCandidate(
                        artifact_id="artifact_escape",
                        relative_path=relative_path,
                        expires_at=cutoff - timedelta(days=1),
                        kind=RetentionArtifactKind.EXPIRABLE,
                    ),
                ),
            )

    assert outside.exists()
    assert nested_outside.exists()


def test_job_024_partial_failure_resumes_without_redeleting_completed_items(
    tmp_path: Path,
) -> None:
    root = tmp_path / "retention-root"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"synthetic first")
    second.write_bytes(b"synthetic second")
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)
    plan = plan_retention(
        root=root,
        cutoff=cutoff,
        candidates=tuple(
            RetentionCandidate(
                artifact_id=f"artifact_{name}",
                relative_path=f"{name}.bin",
                expires_at=cutoff - timedelta(days=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            )
            for name in ("first", "second")
        ),
    )
    calls: list[str] = []

    def fail_second_once(path: Path) -> None:
        calls.append(path.name)
        if path.name == "second.bin":
            raise OSError("synthetic delete failure with private path")
        path.unlink()

    partial = run_retention(
        root=root,
        plan=plan,
        replay_key="job-024-partial",
        apply=True,
        approval_digest=plan.digest_sha256,
        deleter=fail_second_once,
    )
    resumed = run_retention(
        root=root,
        plan=plan,
        replay_key="job-024-partial",
        apply=True,
        approval_digest=plan.digest_sha256,
    )

    assert partial.disposition is RetentionDisposition.PARTIAL_FAILURE
    assert partial.deleted_count == 1
    assert partial.failure_count == 1
    assert calls == ["first.bin", "second.bin"]
    assert resumed.disposition is RetentionDisposition.APPLIED
    assert resumed.deleted_count == 2
    assert resumed.replayed is True
    assert not first.exists()
    assert not second.exists()


def test_job_024_recovers_when_interrupted_after_delete(tmp_path: Path) -> None:
    class SyntheticInterruption(BaseException):
        pass

    root = tmp_path / "retention-root"
    root.mkdir()
    expired = root / "expired.bin"
    expired.write_bytes(b"synthetic expired")
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)
    plan = plan_retention(
        root=root,
        cutoff=cutoff,
        candidates=(
            RetentionCandidate(
                artifact_id="artifact_expired",
                relative_path="expired.bin",
                expires_at=cutoff - timedelta(days=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            ),
        ),
    )

    def delete_then_interrupt(path: Path) -> None:
        path.unlink()
        raise SyntheticInterruption

    with pytest.raises(SyntheticInterruption):
        run_retention(
            root=root,
            plan=plan,
            replay_key="job-024-interrupted",
            apply=True,
            approval_digest=plan.digest_sha256,
            deleter=delete_then_interrupt,
        )

    resumed = run_retention(
        root=root,
        plan=plan,
        replay_key="job-024-interrupted",
        apply=True,
        approval_digest=plan.digest_sha256,
    )

    assert resumed.disposition is RetentionDisposition.APPLIED
    assert resumed.deleted_count == 1
    assert resumed.replayed is True
    assert not expired.exists()


def test_job_024_recovers_quarantine_after_interruption_before_unlink(
    tmp_path: Path,
) -> None:
    class SyntheticInterruption(BaseException):
        pass

    root = tmp_path / "retention-root"
    root.mkdir()
    expired = root / "expired.bin"
    expired.write_bytes(b"synthetic quarantined")
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)
    plan = plan_retention(
        root=root,
        cutoff=cutoff,
        candidates=(
            RetentionCandidate(
                artifact_id="artifact_quarantine",
                relative_path="expired.bin",
                expires_at=cutoff - timedelta(days=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            ),
        ),
    )

    def interrupt_after_quarantine() -> None:
        raise SyntheticInterruption

    with pytest.raises(SyntheticInterruption):
        run_retention(
            root=root,
            plan=plan,
            replay_key="job-024-quarantine-crash",
            apply=True,
            approval_digest=plan.digest_sha256,
            after_quarantine=interrupt_after_quarantine,
        )

    journal = root / ".open-brain-retention"
    assert not expired.exists()
    assert tuple(journal.glob(".delete-*"))

    resumed = run_retention(
        root=root,
        plan=plan,
        replay_key="job-024-quarantine-crash",
        apply=True,
        approval_digest=plan.digest_sha256,
    )

    assert resumed.disposition is RetentionDisposition.APPLIED
    assert resumed.deleted_count == 1
    assert resumed.replayed is True
    assert not expired.exists()
    assert not tuple(journal.glob(".delete-*"))


def test_job_024_receipts_redact_paths_ids_replay_keys_and_errors(tmp_path: Path) -> None:
    root = tmp_path / "private-customer-root"
    root.mkdir()
    expired = root / "customer-secret.bin"
    expired.write_bytes(b"synthetic expired")
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)
    artifact_id = "artifact_customer_secret"
    replay_key = "job-024-private-replay"
    plan = plan_retention(
        root=root,
        cutoff=cutoff,
        candidates=(
            RetentionCandidate(
                artifact_id=artifact_id,
                relative_path=expired.name,
                expires_at=cutoff - timedelta(days=1),
                kind=RetentionArtifactKind.EXPIRABLE,
            ),
        ),
    )

    def fail_with_sensitive_error(path: Path) -> None:
        raise OSError(f"cannot delete {path}")

    receipt = run_retention(
        root=root,
        plan=plan,
        replay_key=replay_key,
        apply=True,
        approval_digest=plan.digest_sha256,
        deleter=fail_with_sensitive_error,
    )
    serialized = json.dumps(receipt.to_dict(), sort_keys=True) + repr(receipt)

    assert receipt.disposition is RetentionDisposition.PARTIAL_FAILURE
    assert str(root) not in serialized
    assert expired.name not in serialized
    assert artifact_id not in serialized
    assert replay_key not in serialized
    assert "cannot delete" not in serialized
