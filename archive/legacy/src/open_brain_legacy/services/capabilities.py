"""App-owned capability factories used by the application composition root."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from open_brain_engine.capture.models import CaptureWorkItem, ShareRequest, ShareResponse
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.core.ports import PutResult
from open_brain_engine.engine import CaptureAction, CaptureReceipt, Payload

from open_brain_legacy._compat.open_brain.cli._common import (
    CommandDispatchResult,
    ExitCode,
    redacted_error,
)
from open_brain_legacy._compat.open_brain.config import AppConfig
from open_brain_legacy._compat.open_brain.integrations.ports import PageDocument, PageReadRequest
from open_brain_legacy.capture.queue import FilesystemCaptureQueue, read_pending_queue_snapshot
from open_brain_legacy.cli.ledger import scan as scan_ledger
from open_brain_legacy.cli.operations import (
    DigestOutputMode,
    DigestReport,
    OkfAction,
    OkfReport,
    RetentionService,
)
from open_brain_legacy.cli.production_adapters import ProductionCommandDependencies
from open_brain_legacy.integrations.retrieval import (
    FilesystemWorkRetriever,
    MetadataOnlyRetrievalFeedback,
)
from open_brain_legacy.operations.runlog import RunMetadata, RunOutcome
from open_brain_legacy.operations.runlog_store import FilesystemRunLogStore
from open_brain_legacy.operations.status import (
    StatusMetric,
    StatusReading,
    StatusResult,
    collect_status,
)
from open_brain_legacy.review.store import SqliteReviewStore


@dataclass(frozen=True, slots=True)
class ProductionApplication:
    """Unstarted local capabilities shared by CLI, MCP, and HTTP composition."""

    command_dependencies: ProductionCommandDependencies
    capture_queue: ConfiguredCaptureQueue
    retriever: FilesystemWorkRetriever
    feedback: MetadataOnlyRetrievalFeedback
    page_reader: RetrievalPageReader


@dataclass(frozen=True, slots=True)
class ConfiguredCaptureQueue:
    """Open the durable queue only when a selected command submits a capture."""

    root: Path

    def enqueue(
        self,
        item: CaptureWorkItem,
        *,
        item_id: str,
        payload_digest: str,
    ) -> PutResult:
        return FilesystemCaptureQueue(self.root).enqueue(
            item,
            item_id=item_id,
            payload_digest=payload_digest,
        )

    def accept(
        self,
        _payload: Payload,
        *,
        delivery_id: str,
        action: CaptureAction = CaptureAction.QUICK,
        space_id: str | None = None,
        intent: str | None = None,
        capture_why: str | None = None,
        title: str | None = None,
    ) -> CaptureReceipt:
        del delivery_id, action, space_id, intent, capture_why, title
        raise ValueError("single-user-local capture tasks are required")


@dataclass(frozen=True, slots=True)
class ConfiguredShareSubmitter:
    queue: ConfiguredCaptureQueue
    clock: Callable[[], datetime]

    def submit(self, request: ShareRequest) -> ShareResponse:
        del request
        raise ValueError("legacy share composition is unavailable")


@dataclass(frozen=True, slots=True)
class RetrievalPageReader:
    """Expose only opaque snapshots produced by the work-only retriever."""

    retriever: FilesystemWorkRetriever

    def read(self, request: PageReadRequest) -> PageDocument | None:
        if not isinstance(request, PageReadRequest):
            raise ValueError("invalid work page request")
        snapshot = self.retriever.snapshot(request.page_id)
        if snapshot is None:
            return None
        return PageDocument(
            page_id=snapshot.result_id,
            title=snapshot.title,
            markdown=snapshot.markdown,
            trust=snapshot.trust,
        )


@dataclass(frozen=True, slots=True)
class ConfiguredProposalReader:
    config: AppConfig
    clock: Callable[[], datetime]

    def list(self) -> Iterable[object]:
        with SqliteReviewStore(
            root=self.config.state_root,
            database_name="review/review.sqlite3",
            clock=_CallableClock(self.clock),
        ) as store:
            return store.active_reviews()


@dataclass(frozen=True, slots=True)
class ConfiguredStatusService:
    config: AppConfig
    clock: Callable[[], datetime]
    feedback: MetadataOnlyRetrievalFeedback

    def collect(self, *, strict: bool) -> StatusResult:
        now = _utc(self.clock())
        queue = read_pending_queue_snapshot(self.config.capture_root)
        review_count = len(
            tuple(ConfiguredProposalReader(self.config, self.clock).list())
        )
        probes = {
            StatusMetric.CAPTURES_TODAY: _reading(queue.pending_count, now),
            StatusMetric.OPEN_REVIEWS: _reading(review_count, now),
            StatusMetric.INDEX_AGE: _reading(
                _age_seconds(self.config.state_root / "index", now), now
            ),
            StatusMetric.FAILED_JOBS: _reading(
                _failed_job_count(self.config.state_root, now), now
            ),
            StatusMetric.EVENT_BACKLOG: _reading(
                queue.pending_count + queue.malformed_count, now
            ),
            StatusMetric.STALE_REVIEWS: _reading(0, now),
            StatusMetric.BACKUP_AGE: _reading(
                _age_seconds(self.config.backup_root, now), now
            ),
            StatusMetric.RETRIEVAL_EVENTS: _reading(len(self.feedback.records), now),
        }
        return collect_status(
            probes=probes,
            timeout_seconds=5.0,
            strict=strict,
        )


@dataclass(frozen=True, slots=True)
class ConfiguredCronReader:
    """Read bounded metadata-only scheduler reports from confined state."""

    state_root: Path
    clock: Callable[[], datetime]

    def reports(self, *, window_seconds: int) -> tuple[RunMetadata, ...]:
        if not isinstance(window_seconds, int) or isinstance(window_seconds, bool):
            raise ValueError("invalid run-log window")
        return FilesystemRunLogStore(root=self.state_root).reports(
            now=self.clock(),
            window_seconds=window_seconds,
        )


@dataclass(frozen=True, slots=True)
class ConfiguredDigestService:
    """Render metadata for the local work digest without returning content."""

    event_count: Callable[[], int]

    def render(
        self,
        *,
        tier: PrivacyTier,
        output_mode: DigestOutputMode,
    ) -> DigestReport:
        if tier is not PrivacyTier.WORK or not isinstance(output_mode, DigestOutputMode):
            raise ValueError("invalid digest request")
        count = self.event_count()
        return DigestReport(
            event_count=count,
            output_mode=output_mode,
            redacted_count=0,
            replayed=False,
            tier=tier,
        )


@dataclass(frozen=True, slots=True)
class ConfiguredOkfService:
    work_root: Path

    def run(self, *, action: OkfAction) -> OkfReport:
        if not isinstance(action, OkfAction):
            raise ValueError("invalid OKF request")
        return OkfReport(
            action=action,
            record_count=_markdown_count(self.work_root / "pages"),
            replayed=False,
            schema_version=1,
        )


@dataclass(frozen=True, slots=True)
class ConfiguredLedgerFamily:
    work_root: Path

    def dispatch(self, argv: tuple[str, ...]) -> CommandDispatchResult:
        if argv != ("scan",):
            return _family_failure("ledger", "invalid_ledger_request", usage=True)
        return scan_ledger(root=self.work_root)


@dataclass(frozen=True, slots=True)
class _FamilyResult:
    exit_code: ExitCode
    envelope: dict[str, object]


@dataclass(frozen=True, slots=True)
class _CallableClock:
    value: Callable[[], datetime]

    def now(self) -> datetime:
        return self.value()


def compose_production_application(
    *,
    config: AppConfig,
    clock: Callable[[], datetime],
    retention: RetentionService | None = None,
) -> ProductionApplication:
    """Bind every public command dependency without starting listeners or jobs."""
    if not isinstance(config, AppConfig) or not callable(clock):
        raise ValueError("invalid production application configuration")
    queue = ConfiguredCaptureQueue(config.capture_root)
    feedback = MetadataOnlyRetrievalFeedback()
    retriever = FilesystemWorkRetriever(
        work_root=config.work_root,
        feedback=feedback,
    )
    page_reader = RetrievalPageReader(retriever)
    dependencies = ProductionCommandDependencies(
        capture_queue=queue,
        clock=clock,
        share_submitter=None,
        retriever=retriever,
        status=ConfiguredStatusService(config, clock, feedback),
        proposals=ConfiguredProposalReader(config, clock),
        retention=retention,
        cron=ConfiguredCronReader(config.state_root, clock),
        digest=ConfiguredDigestService(lambda: _event_count(config.state_root)),
        ledger=ConfiguredLedgerFamily(config.work_root),
        okf=ConfiguredOkfService(config.work_root),
    )
    return ProductionApplication(
        command_dependencies=dependencies,
        capture_queue=queue,
        retriever=retriever,
        feedback=feedback,
        page_reader=page_reader,
    )


def _reading(value: int, observed_at: datetime) -> Callable[[float], StatusReading]:
    def probe(_timeout_seconds: float) -> StatusReading:
        return StatusReading.available(value=value, observed_at=observed_at)

    return probe


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid production timestamp")
    return value.astimezone(UTC)


def _age_seconds(path: Path, now: datetime) -> int:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("unsafe status path")
        modified = datetime.fromtimestamp(metadata.st_mtime, tz=UTC)
        return max(0, int((now - modified).total_seconds()))
    except FileNotFoundError:
        return 0


def _failed_job_count(state_root: Path, now: datetime) -> int:
    return sum(
        report.outcome in {RunOutcome.CONFIGURATION_FAILED, RunOutcome.FAILED}
        for report in FilesystemRunLogStore(root=state_root).reports(
            now=now,
            window_seconds=604_800,
        )
    )


def _event_count(state_root: Path) -> int:
    root = state_root / "events"
    try:
        return sum(
            1
            for entry in root.iterdir()
            if entry.name.endswith(".json") and stat.S_ISREG(entry.lstat().st_mode)
        )
    except FileNotFoundError:
        return 0


def _markdown_count(root: Path) -> int:
    try:
        if not stat.S_ISDIR(root.lstat().st_mode) or root.is_symlink():
            return 0
    except FileNotFoundError:
        return 0
    count = 0
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        names[:] = [
            name
            for name in names
            if not (directory_path / name).is_symlink()
        ]
        count += sum(
            file_name.endswith(".md")
            and stat.S_ISREG((directory_path / file_name).lstat().st_mode)
            for file_name in files
        )
    return count


def _family_failure(
    family: str,
    code: str,
    *,
    usage: bool,
) -> _FamilyResult:
    return _FamilyResult(
        ExitCode.USAGE if usage else ExitCode.FAILURE,
        {
            "command": family,
            "error": redacted_error(code),
            "status": "invalid" if usage else "failed",
        },
    )
