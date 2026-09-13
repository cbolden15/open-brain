from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from open_brain_engine.core.ids import CaptureId, canonical_json_bytes
from open_brain_engine.core.models import Intent, PrivacyTier
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewProposal,
    ReviewState,
)

from open_brain_legacy._compat.open_brain.integrations.ports import HookInstallRequest, HookKind
from open_brain_legacy.integrations.hooks import TemporaryHookPlanner
from open_brain_legacy.operations.git_sync_runtime import GitCommand, GitCommandResult
from open_brain_legacy.operations.writer_jobs import WriterJobInvocation, get_writer_job_spec
from open_brain_legacy.production.local_jobs import (
    CloseDayPreparationApplication,
    FilesystemSignalCutoffStore,
    GitSignal,
    GitSignalScanner,
    HookSyncPlanApplication,
    SignalScanApplication,
    WorkWikiLintApplication,
    build_hook_plans,
    scan_work_wiki,
)

FIXED_TIME = datetime(2026, 8, 25, 12, tzinfo=UTC)


class _SignalPlanner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []

    def repository_root(self, repository_id: str) -> Path:
        self.calls.append(repository_id)
        return self.root


class _SignalRunner:
    def __init__(self) -> None:
        self.commands: list[GitCommand] = []

    def run(self, command: GitCommand) -> GitCommandResult:
        self.commands.append(command)
        return GitCommandResult(0, stdout=("a" * 40 + "\n").encode())


class _UnbornSignalRunner:
    def __init__(self) -> None:
        self.commands: list[GitCommand] = []

    def run(self, command: GitCommand) -> GitCommandResult:
        self.commands.append(command)
        if command.argv[3] == "log":
            return GitCommandResult(128)
        return GitCommandResult(0, stdout=b"## No commits yet on main\n")


def _invocation(job_id: str) -> WriterJobInvocation:
    spec = get_writer_job_spec(job_id)
    return WriterJobInvocation(
        job_id=job_id,
        command=spec.command,
        replay_key=f"{job_id.lower()}-fixture",
        effect=spec.effect,
        review_boundary=spec.review_boundary,
        local_only=spec.local_only,
        dry_run=spec.dry_run,
        apply_review_decisions=False,
        approved_records=(),
        approval_bindings=(),
        planned_actions=spec.planned_actions,
        personal_local_only=False,
        cutoff=FIXED_TIME if job_id == "JOB-007" else None,
    )


def _review(suffix: str, *, state: ReviewState = ReviewState.OPEN) -> ReviewAggregate:
    aggregate = ReviewAggregate.create(
        ReviewProposal.create(
            capture_id=CaptureId("cap_" + suffix * 64),
            source_ref="synthetic-source-ref",
            privacy_tier=PrivacyTier.WORK,
            proposed_intent=Intent.IDEA,
            proposal_reason="Synthetic proposal reason",
            capture_why="Synthetic owner context",
            created_at=FIXED_TIME,
            created_by=Actor(ActorKind.SYSTEM, "router"),
        )
    )
    if state is ReviewState.OPEN:
        return aggregate
    return aggregate.decide(
        ReviewDecisionCommand.create(
            decision_id=f"decision_{suffix}",
            target_state=state,
            reason="Synthetic terminal decision",
            occurred_at=FIXED_TIME,
            actor=Actor(ActorKind.OWNER, "owner"),
        )
    ).aggregate


def test_close_day_prepares_only_open_opaque_review_metadata() -> None:
    open_review = _review("a")
    prepared = CloseDayPreparationApplication(
        reviews=(open_review, _review("b", state=ReviewState.REJECTED)),
    ).prepare(_invocation("JOB-006"))
    payload = canonical_json_bytes(prepared.to_dict())

    assert prepared.review_item_ids == (str(open_review.proposal.review_id),)
    assert len(prepared.records) == 1
    assert b"Synthetic proposal reason" not in payload
    assert b"Synthetic owner context" not in payload


def test_signal_scan_binds_cutoff_and_opaque_revision_records() -> None:
    prepared = SignalScanApplication(
        signals=(
            GitSignal.create(repository_id="repo_a", revision_id="a" * 40),
            GitSignal.create(repository_id="repo_b", revision_id="b" * 40),
        )
    ).prepare(_invocation("JOB-007"))

    assert len(prepared.records) == 2
    assert [parameter.name for parameter in prepared.parameters] == [
        "cutoff",
        "signal_count",
    ]
    assert b"repo_a" not in canonical_json_bytes(prepared.to_dict())


def test_git_signal_scanner_uses_one_closed_bounded_history_window(
    tmp_path: Path,
) -> None:
    planner = _SignalPlanner(tmp_path)
    runner = _SignalRunner()
    scanner = GitSignalScanner(
        repository_ids=("repo_a",),
        planner=planner,
        runner=runner,
    )

    signals = scanner.scan(
        since=datetime(2026, 8, 24, 12, tzinfo=UTC),
        until=FIXED_TIME,
    )

    assert len(signals) == 1
    assert planner.calls == ["repo_a"]
    assert runner.commands[0].argv == (
        "git",
        "-c",
        "core.askPass=",
        "log",
        "--since=2026-08-24T12:00:00Z",
        "--until=2026-08-25T12:00:00Z",
        "--format=%H",
        "--max-count=100",
        "--no-merges",
    )


def test_git_signal_scanner_accepts_only_verified_unborn_repository(
    tmp_path: Path,
) -> None:
    runner = _UnbornSignalRunner()
    scanner = GitSignalScanner(
        repository_ids=("repo_a",),
        planner=_SignalPlanner(tmp_path),
        runner=runner,
    )

    assert scanner.scan(
        since=datetime(2026, 8, 24, 12, tzinfo=UTC),
        until=FIXED_TIME,
    ) == ()
    assert runner.commands[1].argv == (
        "git",
        "-c",
        "core.askPass=",
        "status",
        "--porcelain=v1",
        "--branch",
    )


def test_signal_cutoff_is_owner_only_canonical_and_advances_after_success(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    store = FilesystemSignalCutoffStore(root=state_root)
    default = FIXED_TIME - timedelta(days=1)

    assert store.load(default=default) == default
    store.save(FIXED_TIME)

    assert store.load(default=default) == FIXED_TIME
    cutoff_path = state_root / "signals" / "cutoff.json"
    assert cutoff_path.stat().st_mode & 0o777 == 0o600
    assert b"schema_version" in cutoff_path.read_bytes()


def test_work_wiki_lint_is_bounded_and_persists_no_page_content(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "valid.md").write_text(
        "---\ntitle: Valid\ntype: tool\nstatus: adopted\n---\n\n"
        "# Valid\n\n[[missing-page]]\n"
    )
    (pages / "missing-frontmatter.md").write_text(
        "# Private synthetic body\n\n~~old claim~~\n"
    )

    snapshot = scan_work_wiki(root=tmp_path, as_of=FIXED_TIME)
    prepared = WorkWikiLintApplication(snapshot=snapshot).prepare(
        _invocation("JOB-008")
    )
    payload = canonical_json_bytes(prepared.to_dict())

    assert snapshot.page_count == 2
    assert snapshot.finding_count >= 3
    assert prepared.review_item_ids == ()
    assert b"Private synthetic body" not in payload
    assert b"missing-frontmatter.md" not in payload
    assert b"missing-page" not in payload


def test_hook_sync_persists_only_dry_run_plan_digests() -> None:
    planner = TemporaryHookPlanner()
    plans = build_hook_plans(
        repository_ids=("repo_a", "repo_b"),
        planner=planner,
    )
    prepared = HookSyncPlanApplication(
        plans=plans,
        inventory_digest_sha256="c" * 64,
    ).prepare(_invocation("JOB-009"))
    payload = canonical_json_bytes(prepared.to_dict())

    assert len(prepared.records) == 2
    assert [parameter.name for parameter in prepared.parameters] == [
        "inventory_digest_sha256",
        "plan_count",
    ]
    assert b"post-commit" not in payload
    assert plans == tuple(
        planner.plan(
            HookInstallRequest(
                repository_id=repository_id,
                hook_kind=HookKind.POST_COMMIT,
                dry_run=True,
            )
        )
        for repository_id in ("repo_a", "repo_b")
    )
