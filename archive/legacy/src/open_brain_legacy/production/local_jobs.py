"""Bounded local snapshots and pure applications for operator-side jobs."""

from __future__ import annotations

import json
import os
import re
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.review.models import ReviewAggregate, ReviewState
from open_brain_engine.storage.filesystem import atomic_replace, read_confined

from open_brain_legacy._compat.open_brain.integrations.ports import HookInstallRequest, HookKind
from open_brain_legacy.integrations.hooks import HookPlan, TemporaryHookPlanner
from open_brain_legacy.operations.git_sync_runtime import GitCommand, GitCommandRunner
from open_brain_legacy.operations.writer_jobs import (
    EffectParameter,
    EffectRecord,
    PreparedEffect,
    ReviewBoundary,
    ScheduledEffect,
    WriterJobError,
    WriterJobInvocation,
)

_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_REVISION_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WIKILINK = re.compile(r"\[\[([^\]]+?)\]\]")
_MAX_CLOSE_DAY_REVIEWS = 50
_MAX_SIGNALS = 200
_MAX_WIKI_PAGES = 5_000
_MAX_WIKI_PAGE_BYTES = 2 * 1024 * 1024
_MAX_LINT_FINDINGS = 200
_VALID_WIKI_TYPES = frozenset(
    {
        "agent",
        "decision",
        "entity",
        "env",
        "environment",
        "evaluation",
        "integration",
        "learning",
        "mcp",
        "model",
        "pattern",
        "product",
        "project",
        "skill",
        "strategy",
        "tool",
        "workflow",
    }
)
_VALID_WIKI_STATUS = frozenset(
    {"adopted", "archived", "deprecated", "evaluating", "living", "rejected"}
)


@dataclass(frozen=True, slots=True)
class CloseDayPreparationApplication:
    reviews: tuple[ReviewAggregate, ...] = field(repr=False)

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        _require_invocation(
            invocation,
            job_id="JOB-006",
            effect=ScheduledEffect.OPERATOR_ARTIFACT,
            dry_run=True,
            review_boundary=ReviewBoundary.PREPARATION_ONLY,
        )
        open_reviews = tuple(
            sorted(
                (
                    review
                    for review in self.reviews
                    if isinstance(review, ReviewAggregate)
                    and review.proposal.state is ReviewState.OPEN
                ),
                key=lambda review: str(review.proposal.review_id),
            )
        )
        selected = open_reviews[:_MAX_CLOSE_DAY_REVIEWS]
        records = tuple(
            EffectRecord(
                record_id="close_day_"
                + sha256(str(review.proposal.review_id).encode()).hexdigest(),
                digest_sha256=sha256(
                    canonical_json_bytes(review.to_dict())
                ).hexdigest(),
            )
            for review in selected
        )
        return PreparedEffect(
            effect=ScheduledEffect.OPERATOR_ARTIFACT,
            records=records,
            review_item_ids=tuple(str(review.proposal.review_id) for review in selected),
            parameters=(
                EffectParameter("candidate_count", str(len(selected))),
                EffectParameter(
                    "truncated",
                    "1" if len(open_reviews) > len(selected) else "0",
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class GitSignal:
    signal_id: str
    digest_sha256: str
    repository_id: str = field(repr=False)
    revision_id: str = field(repr=False)

    @classmethod
    def create(cls, *, repository_id: str, revision_id: str) -> GitSignal:
        if (
            not isinstance(repository_id, str)
            or _OPAQUE_ID.fullmatch(repository_id) is None
            or not isinstance(revision_id, str)
            or _REVISION_ID.fullmatch(revision_id) is None
        ):
            raise WriterJobError("invalid local Git signal")
        identity = canonical_json_bytes(
            {
                "schema_version": 1,
                "repository_id": repository_id,
                "revision_id": revision_id,
            }
        )
        return cls(
            signal_id="signal_" + sha256(identity).hexdigest(),
            digest_sha256=sha256(b"open-brain-git-signal-v1\0" + identity).hexdigest(),
            repository_id=repository_id,
            revision_id=revision_id,
        )

    def to_effect_record(self) -> EffectRecord:
        return EffectRecord(self.signal_id, self.digest_sha256)


class RepositoryRootPlanner(Protocol):
    def repository_root(self, repository_id: str) -> Path: ...


@dataclass(frozen=True, slots=True)
class GitSignalScanner:
    repository_ids: tuple[str, ...]
    planner: RepositoryRootPlanner
    runner: GitCommandRunner

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository_ids, tuple)
            or not 1 <= len(self.repository_ids) <= 64
            or any(
                not isinstance(item, str) or _OPAQUE_ID.fullmatch(item) is None
                for item in self.repository_ids
            )
            or len(set(self.repository_ids)) != len(self.repository_ids)
            or not callable(getattr(self.planner, "repository_root", None))
            or not callable(getattr(self.runner, "run", None))
        ):
            raise WriterJobError("invalid local Git signal scanner")

    def scan(self, *, since: datetime, until: datetime) -> tuple[GitSignal, ...]:
        if (
            not isinstance(since, datetime)
            or since.tzinfo is None
            or since.utcoffset() is None
            or not isinstance(until, datetime)
            or until.tzinfo is None
            or until.utcoffset() is None
            or since >= until
        ):
            raise WriterJobError("invalid local Git signal window")
        signals: dict[str, GitSignal] = {}
        for repository_id in sorted(self.repository_ids):
            repository_root = self.planner.repository_root(repository_id)
            result = self.runner.run(
                GitCommand(
                    cwd=repository_root,
                    argv=(
                        "git",
                        "-c",
                        "core.askPass=",
                        "log",
                        "--since=" + _git_timestamp(since),
                        "--until=" + _git_timestamp(until),
                        "--format=%H",
                        "--max-count=100",
                        "--no-merges",
                    ),
                )
            )
            if result.returncode != 0:
                status = self.runner.run(
                    GitCommand(
                        cwd=repository_root,
                        argv=(
                            "git",
                            "-c",
                            "core.askPass=",
                            "status",
                            "--porcelain=v1",
                            "--branch",
                        ),
                    )
                )
                if (
                    result.returncode == 128
                    and status.returncode == 0
                    and status.stdout.startswith(b"## No commits yet on ")
                ):
                    continue
                raise WriterJobError("local Git signal scan failed")
            try:
                revisions = tuple(
                    line for line in result.stdout.decode("ascii").splitlines() if line
                )
            except UnicodeDecodeError:
                raise WriterJobError("local Git signal scan failed") from None
            if (
                len(revisions) > 100
                or len(set(revisions)) != len(revisions)
                or any(_REVISION_ID.fullmatch(item) is None for item in revisions)
            ):
                raise WriterJobError("local Git signal scan failed")
            for revision_id in revisions:
                signal = GitSignal.create(
                    repository_id=repository_id,
                    revision_id=revision_id,
                )
                signals[signal.signal_id] = signal
        if len(signals) > _MAX_SIGNALS:
            raise WriterJobError("local Git signal batch exceeded limit")
        return tuple(sorted(signals.values(), key=lambda item: item.signal_id))


@dataclass(frozen=True, slots=True)
class FilesystemSignalCutoffStore:
    root: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.root, Path)
            or not self.root.is_absolute()
            or not self.root.is_dir()
            or self.root.is_symlink()
        ):
            raise WriterJobError("signal cutoff root unavailable")

    def load(self, *, default: datetime) -> datetime:
        _require_timestamp(default)
        path = self.root / "signals" / "cutoff.json"
        if not path.exists():
            return default.astimezone(UTC)
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_nlink != 1
            ):
                raise ValueError
            payload = read_confined(root=self.root, relative="signals/cutoff.json")
            if payload is None:
                raise ValueError
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, dict) or set(value) != {"schema_version", "cutoff"}:
                raise ValueError
            cutoff = _parse_timestamp(value["cutoff"])
            if value["schema_version"] != 1 or payload != _cutoff_bytes(cutoff):
                raise ValueError
            return cutoff
        except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise WriterJobError("signal cutoff unavailable") from None

    def save(self, cutoff: datetime) -> None:
        _require_timestamp(cutoff)
        try:
            atomic_replace(
                root=self.root,
                relative="signals/cutoff.json",
                data=_cutoff_bytes(cutoff),
            )
        except Exception:
            raise WriterJobError("signal cutoff write failed") from None


@dataclass(frozen=True, slots=True)
class SignalScanApplication:
    signals: tuple[GitSignal, ...]

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        _require_invocation(
            invocation,
            job_id="JOB-007",
            effect=ScheduledEffect.APPEND_ONLY_SIGNALS,
            dry_run=False,
            review_boundary=ReviewBoundary.NONE,
        )
        if invocation.cutoff is None:
            raise WriterJobError("signal scan cutoff unavailable")
        selected = tuple(sorted(self.signals, key=lambda signal: signal.signal_id))
        if (
            len(selected) > _MAX_SIGNALS
            or any(not isinstance(signal, GitSignal) for signal in selected)
            or len({signal.signal_id for signal in selected}) != len(selected)
        ):
            raise WriterJobError("invalid local Git signal batch")
        return PreparedEffect(
            effect=ScheduledEffect.APPEND_ONLY_SIGNALS,
            records=tuple(signal.to_effect_record() for signal in selected),
            parameters=(
                EffectParameter("cutoff", _timestamp(invocation.cutoff)),
                EffectParameter("signal_count", str(len(selected))),
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkWikiLintFinding:
    finding_id: str
    digest_sha256: str

    def __post_init__(self) -> None:
        if (
            _OPAQUE_ID.fullmatch(self.finding_id) is None
            or _SHA256.fullmatch(self.digest_sha256) is None
        ):
            raise WriterJobError("invalid work-wiki lint finding")


@dataclass(frozen=True, slots=True)
class WorkWikiLintSnapshot:
    page_count: int
    findings: tuple[WorkWikiLintFinding, ...]
    truncated: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.page_count, int)
            or isinstance(self.page_count, bool)
            or not 0 <= self.page_count <= _MAX_WIKI_PAGES
            or not isinstance(self.findings, tuple)
            or len(self.findings) > _MAX_LINT_FINDINGS
            or any(not isinstance(item, WorkWikiLintFinding) for item in self.findings)
            or len({item.finding_id for item in self.findings}) != len(self.findings)
            or type(self.truncated) is not bool
        ):
            raise WriterJobError("invalid work-wiki lint snapshot")

    @property
    def finding_count(self) -> int:
        return len(self.findings)

    def digest_sha256(self) -> str:
        return sha256(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "page_count": self.page_count,
                    "findings": [
                        {
                            "finding_id": item.finding_id,
                            "digest_sha256": item.digest_sha256,
                        }
                        for item in self.findings
                    ],
                    "truncated": self.truncated,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkWikiLintApplication:
    snapshot: WorkWikiLintSnapshot

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        _require_invocation(
            invocation,
            job_id="JOB-008",
            effect=ScheduledEffect.DIAGNOSTICS,
            dry_run=False,
            review_boundary=ReviewBoundary.NONE,
        )
        if not isinstance(self.snapshot, WorkWikiLintSnapshot):
            raise WriterJobError("invalid work-wiki lint snapshot")
        return PreparedEffect(
            effect=ScheduledEffect.DIAGNOSTICS,
            records=(
                EffectRecord("work_wiki_lint_report", self.snapshot.digest_sha256()),
                *(
                    EffectRecord(item.finding_id, item.digest_sha256)
                    for item in self.snapshot.findings
                ),
            ),
            parameters=(
                EffectParameter("finding_count", str(self.snapshot.finding_count)),
                EffectParameter("page_count", str(self.snapshot.page_count)),
                EffectParameter("truncated", "1" if self.snapshot.truncated else "0"),
            ),
        )


@dataclass(frozen=True, slots=True)
class HookSyncPlanApplication:
    plans: tuple[HookPlan, ...]
    inventory_digest_sha256: str

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        _require_invocation(
            invocation,
            job_id="JOB-009",
            effect=ScheduledEffect.HOOK_SYNC_PLAN,
            dry_run=True,
            review_boundary=ReviewBoundary.NONE,
            planned_actions=("backup", "replace", "prune"),
        )
        plans = tuple(sorted(self.plans, key=lambda plan: plan.repository_id))
        if (
            _SHA256.fullmatch(self.inventory_digest_sha256) is None
            or len(plans) > 64
            or any(not isinstance(plan, HookPlan) for plan in plans)
            or len({plan.repository_id for plan in plans}) != len(plans)
        ):
            raise WriterJobError("invalid local hook plan batch")
        return PreparedEffect(
            effect=ScheduledEffect.HOOK_SYNC_PLAN,
            records=tuple(
                EffectRecord(
                    "hook_plan_" + sha256(plan.repository_id.encode()).hexdigest(),
                    plan.template_digest,
                )
                for plan in plans
            ),
            parameters=(
                EffectParameter(
                    "inventory_digest_sha256",
                    self.inventory_digest_sha256,
                ),
                EffectParameter("plan_count", str(len(plans))),
            ),
        )


def build_hook_plans(
    *,
    repository_ids: tuple[str, ...],
    planner: TemporaryHookPlanner | None = None,
) -> tuple[HookPlan, ...]:
    if (
        not isinstance(repository_ids, tuple)
        or len(repository_ids) > 64
        or any(
            not isinstance(item, str) or _OPAQUE_ID.fullmatch(item) is None
            for item in repository_ids
        )
        or len(set(repository_ids)) != len(repository_ids)
    ):
        raise WriterJobError("invalid local hook repositories")
    selected = planner or TemporaryHookPlanner()
    return tuple(
        selected.plan(
            HookInstallRequest(
                repository_id=repository_id,
                hook_kind=HookKind.POST_COMMIT,
                dry_run=True,
            )
        )
        for repository_id in sorted(repository_ids)
    )


def scan_work_wiki(*, root: Path, as_of: datetime) -> WorkWikiLintSnapshot:
    """Read one work root and return only bounded, path-free diagnostics."""
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or not root.is_dir()
        or root.is_symlink()
        or not isinstance(as_of, datetime)
        or as_of.tzinfo is None
        or as_of.utcoffset() is None
    ):
        raise WriterJobError("work-wiki lint root unavailable")
    pages_root = root / "pages"
    if not pages_root.is_dir() or pages_root.is_symlink():
        finding = _lint_finding("missing-pages-root", "pages", "missing")
        return WorkWikiLintSnapshot(0, (finding,), False)
    try:
        discovered = sorted(pages_root.rglob("*.md"))
    except OSError:
        raise WriterJobError("work-wiki lint read failed") from None
    truncated = len(discovered) > _MAX_WIKI_PAGES
    paths = discovered[:_MAX_WIKI_PAGES]
    pages: dict[str, str] = {}
    findings: dict[str, WorkWikiLintFinding] = {}
    frontmatter: dict[str, dict[str, str]] = {}
    for path in paths:
        relative = _relative_subject(root, path)
        try:
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > _MAX_WIKI_PAGE_BYTES
            ):
                raise OSError
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            _add_finding(findings, _lint_finding("unreadable-page", relative, "read"))
            continue
        pages[relative] = text
        fields = _parse_simple_frontmatter(text)
        frontmatter[relative] = fields
        missing = sorted({"title", "type", "status"} - set(fields))
        if missing:
            _add_finding(
                findings,
                _lint_finding("frontmatter-missing", relative, ",".join(missing)),
            )
        if fields.get("type") and fields["type"] not in _VALID_WIKI_TYPES:
            _add_finding(
                findings,
                _lint_finding("frontmatter-type", relative, "invalid"),
            )
        if fields.get("status") and fields["status"] not in _VALID_WIKI_STATUS:
            _add_finding(
                findings,
                _lint_finding("frontmatter-status", relative, "invalid"),
            )
        if fields.get("confidence") == "low":
            _add_finding(findings, _lint_finding("low-confidence", relative, "low"))
        if _outside_fences_contains(text, "~~"):
            _add_finding(
                findings,
                _lint_finding("unlogged-contradiction", relative, "strike"),
            )

    basenames: dict[str, list[str]] = defaultdict(list)
    for relative in pages:
        basenames[Path(relative).stem].append(relative)
    inbound: dict[str, int] = defaultdict(int)
    for source, text in pages.items():
        for target in _wikilinks(text):
            matches = _resolve_wikilink(source, target, pages, basenames)
            if not matches:
                _add_finding(
                    findings,
                    _lint_finding(
                        "broken-wikilink",
                        source,
                        sha256(target.encode()).hexdigest(),
                    ),
                )
            elif len(matches) > 1:
                _add_finding(
                    findings,
                    _lint_finding(
                        "ambiguous-wikilink",
                        source,
                        sha256(target.encode()).hexdigest(),
                    ),
                )
            else:
                inbound[matches[0]] += 1
    for relative in pages:
        fields = frontmatter[relative]
        if (
            inbound[relative] == 0
            and fields.get("status") != "archived"
            and fields.get("derived", "").casefold() != "true"
        ):
            _add_finding(findings, _lint_finding("orphan-page", relative, "orphan"))

    ordered = tuple(sorted(findings.values(), key=lambda item: item.finding_id))
    if len(ordered) > _MAX_LINT_FINDINGS:
        truncated = True
        ordered = ordered[:_MAX_LINT_FINDINGS]
    return WorkWikiLintSnapshot(len(pages), ordered, truncated)


def _require_invocation(
    invocation: WriterJobInvocation,
    *,
    job_id: str,
    effect: ScheduledEffect,
    dry_run: bool,
    review_boundary: ReviewBoundary,
    planned_actions: tuple[str, ...] = (),
) -> None:
    if (
        not isinstance(invocation, WriterJobInvocation)
        or invocation.job_id != job_id
        or invocation.effect is not effect
        or invocation.dry_run is not dry_run
        or invocation.review_boundary is not review_boundary
        or invocation.local_only is not True
        or invocation.apply_review_decisions is not False
        or invocation.approved_records
        or invocation.approval_bindings
        or invocation.planned_actions != planned_actions
    ):
        raise WriterJobError("invalid local scheduled application invocation")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _git_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_timestamp(value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise WriterJobError("invalid signal cutoff timestamp")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _cutoff_bytes(value: datetime) -> bytes:
    return canonical_json_bytes(
        {"schema_version": 1, "cutoff": _timestamp(value)}
    )


def _relative_subject(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        raise WriterJobError("work-wiki lint path escaped root") from None


def _lint_finding(category: str, subject: str, detail: str) -> WorkWikiLintFinding:
    source = canonical_json_bytes(
        {
            "schema_version": 1,
            "category": category,
            "subject_sha256": sha256(subject.encode()).hexdigest(),
            "detail_sha256": sha256(detail.encode()).hexdigest(),
        }
    )
    return WorkWikiLintFinding(
        finding_id="lint_" + sha256(source).hexdigest(),
        digest_sha256=sha256(b"open-brain-work-wiki-lint-v1\0" + source).hexdigest(),
    )


def _add_finding(
    findings: dict[str, WorkWikiLintFinding],
    finding: WorkWikiLintFinding,
) -> None:
    findings[finding.finding_id] = finding


def _parse_simple_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}
    block = text[4:].split("\n---\n", 1)[0]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        normalized = value.strip().strip("\"'")
        if key and key not in fields:
            fields[key] = normalized
    return fields


def _outside_fences_contains(text: str, needle: str) -> bool:
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and needle in line:
            return True
    return False


def _wikilinks(text: str) -> tuple[str, ...]:
    links: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        without_code = re.sub(r"`[^`]*`", "", line)
        for match in _WIKILINK.finditer(without_code):
            target = re.split(r"\\?\|", match.group(1), maxsplit=1)[0].strip()
            target = target.split("#", 1)[0].strip()
            if target:
                links.append(target)
    return tuple(links)


def _resolve_wikilink(
    source: str,
    target: str,
    pages: dict[str, str],
    basenames: dict[str, list[str]],
) -> tuple[str, ...]:
    target_path = target if target.endswith(".md") else target + ".md"
    candidates = (target_path, "pages/" + target_path)
    for candidate in candidates:
        if candidate in pages:
            return (candidate,)
    source_candidate = (Path(source).parent / target_path).as_posix()
    if source_candidate in pages:
        return (source_candidate,)
    return tuple(sorted(basenames.get(Path(target_path).stem, ())))
