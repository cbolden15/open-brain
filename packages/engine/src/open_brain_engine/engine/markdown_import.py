"""Idempotent, descriptor-confined Markdown directory import."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    Intent,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)

from .contracts import (
    CaptureSubmission,
    FilePayload,
    MarkdownImportCancelled,
    MarkdownImportEntry,
    MarkdownImportFailure,
    MarkdownImportInterrupted,
    MarkdownImportPreflight,
    MarkdownImportProgress,
    MarkdownImportSummary,
    PublicJobCaptureContext,
)
from .markdown_import_fs import (
    ImportDirectoryUnavailable,
    ImportInterrupted,
    ImportScanIncomplete,
    MarkdownCandidate,
    PinnedImportRoot,
    RootSnapshot,
    ScanInventory,
    ScanLimitExceeded,
    ScanLimits,
    enumerate_markdown,
    pin_import_root,
    read_markdown_candidate,
    revalidate_import_root,
    roots_overlap,
    snapshot_directory,
)
from .normalization import _new_id, _portable_id, _timestamp

if TYPE_CHECKING:
    from .local import BrainEngine

MAX_SUMMARY_ENTRIES = 100
_IMPORT_DELIVERY_PREFIX = "markdown-import."
_IMPORT_ACTOR_ID = "actor_00000000-0000-4000-8000-000000000401"
_IMPORT_ROLE_CLAIM_ID = "role_claim_00000000-0000-4000-8000-000000000402"
_IMPORT_ROLE_ID = "role_00000000-0000-4000-8000-000000000403"
_ATX_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)\s*#*\s*$")


@dataclass(frozen=True, slots=True)
class _RootSelection:
    root_id: str
    is_new: bool


@dataclass(frozen=True, slots=True)
class _Reservation:
    revision_id: str
    capture_id: str | None
    outcome: str


class MarkdownImportTasks:
    """Import one Markdown tree through the normal immutable capture model."""

    def __init__(self, engine: BrainEngine, *, scan_limits: ScanLimits | None = None) -> None:
        self._engine = engine
        self._scan_limits = scan_limits or ScanLimits()

    def import_directory(
        self,
        directory: str,
        *,
        allow_large_vault: bool = False,
        confirm: Callable[[MarkdownImportPreflight], bool] | None = None,
        progress: Callable[[MarkdownImportProgress], None] | None = None,
        interrupted: Callable[[], bool] | None = None,
    ) -> MarkdownImportSummary:
        if type(allow_large_vault) is not bool:
            raise ValueError("invalid Markdown import request")
        if confirm is not None and not callable(confirm):
            raise ValueError("invalid Markdown import confirmation")
        if progress is not None and not callable(progress):
            raise ValueError("invalid Markdown import progress callback")
        should_interrupt: Callable[[], bool] = (
            (lambda: False) if interrupted is None else interrupted
        )
        if not callable(should_interrupt):
            raise ValueError("invalid Markdown import interruption callback")

        try:
            with self._engine._writer_lease.acquire_shared_writer():
                if should_interrupt():
                    raise MarkdownImportInterrupted
                self._engine._assert_root()
                with pin_import_root(directory) as pinned:
                    selection = self._classify_root(pinned.snapshot)
                    scan_id = _new_id("import_scan")
                    inventory = self._enumerate(
                        pinned,
                        allow_large_vault=allow_large_vault,
                        interrupted=should_interrupt,
                        progress=progress,
                    )
                    if selection.is_new:
                        if confirm is None:
                            raise MarkdownImportFailure(
                                "import_confirmation_required",
                                details={
                                    "aggregate_bytes": inventory.aggregate_bytes,
                                    "history_retained_after_source_removal": True,
                                    "missing_finalized": False,
                                    "selected_markdown_files": inventory.selected_files,
                                },
                            )
                        if not confirm(
                            MarkdownImportPreflight(
                                canonical_path=pinned.snapshot.canonical_path,
                                selected_markdown_files=inventory.selected_files,
                                aggregate_bytes=inventory.aggregate_bytes,
                            )
                        ):
                            raise MarkdownImportCancelled
                    if should_interrupt():
                        raise MarkdownImportInterrupted
                    self._revalidate_selection(pinned.snapshot, selection)
                    if should_interrupt():
                        raise MarkdownImportInterrupted
                    self._register_root(pinned.snapshot, selection)
                    registered_selection = _RootSelection(selection.root_id, False)
                    return self._process_inventory(
                        pinned,
                        registered_selection,
                        inventory,
                        scan_id=scan_id,
                        should_interrupt=should_interrupt,
                        progress=progress,
                    )
        except MarkdownImportFailure, MarkdownImportCancelled, MarkdownImportInterrupted:
            raise
        except ImportInterrupted:
            raise MarkdownImportInterrupted from None
        except ImportDirectoryUnavailable:
            raise MarkdownImportFailure(
                "import_directory_unavailable", details={"missing_finalized": False}
            ) from None
        except ImportScanIncomplete:
            raise MarkdownImportFailure(
                "import_scan_incomplete", details={"missing_finalized": False}
            ) from None
        except ScanLimitExceeded as error:
            raise MarkdownImportFailure(
                "large_vault_confirmation_required",
                details={
                    "exceeded": list(error.exceeded),
                    "limits": {
                        "aggregate_bytes": self._scan_limits.aggregate_bytes,
                        "selected_markdown_files": self._scan_limits.selected_files,
                        "visited_entries": self._scan_limits.visited_entries,
                    },
                    "missing_finalized": False,
                    "observed": {
                        "aggregate_bytes": error.aggregate_bytes,
                        "selected_markdown_files": error.selected_files,
                        "visited_entries": error.visited_entries,
                    },
                },
            ) from None
        except KeyboardInterrupt:
            raise MarkdownImportInterrupted from None

    def _enumerate(
        self,
        pinned: PinnedImportRoot,
        *,
        allow_large_vault: bool,
        interrupted: Callable[[], bool],
        progress: Callable[[MarkdownImportProgress], None] | None,
    ) -> ScanInventory:
        return enumerate_markdown(
            pinned.descriptor,
            limits=self._scan_limits,
            allow_large_vault=allow_large_vault,
            interrupted=interrupted,
            progress=(
                None
                if progress is None
                else lambda visited: progress(
                    MarkdownImportProgress(
                        visited_entries=visited,
                        processed_markdown_files=0,
                    )
                )
            ),
        )

    def _classify_root(
        self,
        candidate: RootSnapshot,
        *,
        prospective_root_id: str | None = None,
    ) -> _RootSelection:
        brain = snapshot_directory(self._engine.profile.root)
        if brain.identity != self._engine.profile.root_identity:
            raise ImportDirectoryUnavailable
        if roots_overlap(candidate, brain):
            raise MarkdownImportFailure(
                "overlapping_import_root",
                details={"conflict": "brain", "missing_finalized": False},
            )

        connection = self._engine._store.connect()
        try:
            rows = tuple(
                connection.execute(
                    "SELECT root_id, canonical_path, device, inode "
                    "FROM markdown_import_roots ORDER BY root_id"
                )
            )
        finally:
            connection.close()
        matched_root_id: str | None = None
        for row in rows:
            root_id = _portable_id(cast(str, row["root_id"]), "import_root")
            stored_path = Path(cast(str, row["canonical_path"]))
            stored_identity = (int(cast(str, row["device"])), int(cast(str, row["inode"])))
            try:
                stored = snapshot_directory(stored_path)
            except ImportDirectoryUnavailable:
                if candidate.identity == stored_identity or candidate.canonical_path == stored_path:
                    self._raise_root_changed(root_id)
                if _lexically_overlap(candidate.canonical_path, stored_path):
                    self._raise_overlap(root_id)
                continue
            if stored.identity != stored_identity:
                self._raise_root_changed(root_id)
            if candidate.identity == stored_identity:
                matched_root_id = root_id
                continue
            if roots_overlap(candidate, stored):
                self._raise_overlap(root_id)
        if matched_root_id is not None:
            return _RootSelection(matched_root_id, False)
        return _RootSelection(prospective_root_id or _new_id("import_root"), True)

    def _revalidate_selection(
        self,
        snapshot: RootSnapshot,
        selection: _RootSelection,
    ) -> None:
        try:
            revalidate_import_root(snapshot)
        except ImportDirectoryUnavailable:
            self._raise_root_changed(selection.root_id)
        current = self._classify_root(
            snapshot,
            prospective_root_id=selection.root_id,
        )
        if current != selection:
            self._raise_root_changed(selection.root_id)

    def _register_root(self, snapshot: RootSnapshot, selection: _RootSelection) -> None:
        if not selection.is_new:
            return
        with self._engine._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO markdown_import_roots (
                    root_id, canonical_path, device, inode, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    selection.root_id,
                    os.fspath(snapshot.canonical_path),
                    str(snapshot.identity[0]),
                    str(snapshot.identity[1]),
                    _timestamp(self._engine._clock()),
                ),
            )

    def _process_inventory(
        self,
        pinned: PinnedImportRoot,
        selection: _RootSelection,
        inventory: ScanInventory,
        *,
        scan_id: str,
        should_interrupt: Callable[[], bool],
        progress: Callable[[MarkdownImportProgress], None] | None,
    ) -> MarkdownImportSummary:
        root_id = selection.root_id
        outcomes = [
            MarkdownImportEntry(
                outcome="failed" if item.failed else "skipped",
                path=item.path,
                reason=item.reason,
            )
            for item in inventory.outcomes
        ]
        for item in inventory.outcomes:
            if not item.path.startswith("<invalid-path-"):
                self._before_import_mutation(
                    pinned.snapshot,
                    selection,
                    should_interrupt=should_interrupt,
                )
                self._mark_existing_path_observed(root_id, item.path, scan_id)
                if should_interrupt():
                    raise MarkdownImportInterrupted

        processed = 0
        for candidate in inventory.candidates:
            try:
                payload = read_markdown_candidate(
                    pinned.descriptor,
                    candidate,
                    maximum_bytes=self._scan_limits.file_bytes,
                )
            except ValueError as error:
                reason = str(error)
                if reason not in {"file_changed", "unreadable"}:
                    raise
                self._before_import_mutation(
                    pinned.snapshot,
                    selection,
                    should_interrupt=should_interrupt,
                )
                self._mark_existing_path_observed(root_id, candidate.relative_path, scan_id)
                outcomes.append(
                    MarkdownImportEntry(
                        outcome="failed", path=candidate.relative_path, reason=reason
                    )
                )
                processed += 1
                self._after_file(
                    inventory,
                    processed,
                    should_interrupt=should_interrupt,
                    progress=progress,
                )
                continue
            if should_interrupt():
                raise MarkdownImportInterrupted
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                self._before_import_mutation(
                    pinned.snapshot,
                    selection,
                    should_interrupt=should_interrupt,
                )
                self._mark_existing_path_observed(
                    root_id,
                    candidate.relative_path,
                    scan_id,
                    candidate=candidate,
                )
                outcomes.append(
                    MarkdownImportEntry(
                        outcome="failed",
                        path=candidate.relative_path,
                        reason="invalid_utf8",
                    )
                )
            else:
                if "\x00" in text:
                    self._before_import_mutation(
                        pinned.snapshot,
                        selection,
                        should_interrupt=should_interrupt,
                    )
                    self._mark_existing_path_observed(
                        root_id,
                        candidate.relative_path,
                        scan_id,
                        candidate=candidate,
                    )
                    outcomes.append(
                        MarkdownImportEntry(
                            outcome="failed",
                            path=candidate.relative_path,
                            reason="invalid_content",
                        )
                    )
                else:
                    outcomes.append(
                        self._capture_candidate(
                            root_id=root_id,
                            scan_id=scan_id,
                            pinned=pinned,
                            selection=selection,
                            candidate=candidate,
                            payload=payload,
                            text=text,
                            should_interrupt=should_interrupt,
                        )
                    )
            processed += 1
            self._after_file(
                inventory,
                processed,
                should_interrupt=should_interrupt,
                progress=progress,
            )

        self._before_import_mutation(
            pinned.snapshot,
            selection,
            should_interrupt=should_interrupt,
        )
        outcomes.extend(self._finalize_scan(root_id, scan_id))
        return _summary(inventory.selected_files, outcomes)

    def _before_import_mutation(
        self,
        snapshot: RootSnapshot,
        selection: _RootSelection,
        *,
        should_interrupt: Callable[[], bool],
    ) -> None:
        if should_interrupt():
            raise MarkdownImportInterrupted
        self._revalidate_selection(snapshot, selection)
        if should_interrupt():
            raise MarkdownImportInterrupted

    def _after_file(
        self,
        inventory: ScanInventory,
        processed: int,
        *,
        should_interrupt: Callable[[], bool],
        progress: Callable[[MarkdownImportProgress], None] | None,
    ) -> None:
        if progress is not None and processed % 100 == 0:
            progress(
                MarkdownImportProgress(
                    visited_entries=inventory.visited_entries,
                    processed_markdown_files=processed,
                )
            )
        if should_interrupt():
            raise MarkdownImportInterrupted

    def _capture_candidate(
        self,
        *,
        root_id: str,
        scan_id: str,
        pinned: PinnedImportRoot,
        selection: _RootSelection,
        candidate: MarkdownCandidate,
        payload: bytes,
        text: str,
        should_interrupt: Callable[[], bool],
    ) -> MarkdownImportEntry:
        digest = sha256(payload).hexdigest()
        delivery_id = _delivery_id(root_id, candidate.relative_path, digest)
        source_reference = (
            f"urn:open-brain:markdown-import:{root_id}:{quote(candidate.relative_path, safe='/')}"
        )
        submission = CaptureSubmission.for_public_job(
            context=_import_context(self._engine),
            payload=FilePayload(
                candidate.relative_path.rsplit("/", 1)[-1],
                "text/markdown",
                payload,
            ),
            delivery_id=delivery_id,
            source_origin=ContentOrigin.UNKNOWN,
            source_reference=source_reference,
            provenance=Provenance.create(
                source_ref=source_reference,
                content_origin=ContentOrigin.UNKNOWN,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            ),
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.PERSONAL,
                reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
                policy_version="privacy-v1",
                authority=Authority(cloud=False, external_egress=False),
            ),
            intent=Intent.HOLD,
            title=extract_markdown_title(text, candidate.relative_path),
        )
        self._before_import_mutation(
            pinned.snapshot,
            selection,
            should_interrupt=should_interrupt,
        )
        reservation = self._reserve_revision(
            root_id=root_id,
            scan_id=scan_id,
            candidate=candidate,
            content_sha256=digest,
            delivery_id=delivery_id,
            request_sha256=submission.request_sha256(),
        )
        if reservation.outcome == "unchanged":
            return MarkdownImportEntry(outcome="unchanged", path=candidate.relative_path)
        capture_id = reservation.capture_id
        if capture_id is None:
            self._before_import_mutation(
                pinned.snapshot,
                selection,
                should_interrupt=should_interrupt,
            )
            capture_id = self._engine._submit_capture(submission).capture_id
        self._before_import_mutation(
            pinned.snapshot,
            selection,
            should_interrupt=should_interrupt,
        )
        self._activate_revision(
            root_id=root_id,
            scan_id=scan_id,
            candidate=candidate,
            reservation=reservation,
            capture_id=capture_id,
            request_sha256=submission.request_sha256(),
        )
        return MarkdownImportEntry(outcome=reservation.outcome, path=candidate.relative_path)

    def _reserve_revision(
        self,
        *,
        root_id: str,
        scan_id: str,
        candidate: MarkdownCandidate,
        content_sha256: str,
        delivery_id: str,
        request_sha256: str,
    ) -> _Reservation:
        with self._engine._store.transaction() as connection:
            file_row = connection.execute(
                "SELECT * FROM markdown_import_files WHERE root_id = ? AND relative_path = ?",
                (root_id, candidate.relative_path),
            ).fetchone()
            new_file = file_row is None
            if file_row is None:
                file_id = _new_id("import_file")
                connection.execute(
                    """
                    INSERT INTO markdown_import_files (
                        file_id, root_id, relative_path, active_revision_id,
                        last_observed_scan_id, last_observed_device, last_observed_inode
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        file_id,
                        root_id,
                        candidate.relative_path,
                        scan_id,
                        str(candidate.observed.device),
                        str(candidate.observed.inode),
                    ),
                )
                active_revision_id = None
            else:
                file_id = _portable_id(cast(str, file_row["file_id"]), "import_file")
                active_revision_id = cast(str | None, file_row["active_revision_id"])
                connection.execute(
                    """
                    UPDATE markdown_import_files
                    SET last_observed_scan_id = ?, last_observed_device = ?,
                        last_observed_inode = ?
                    WHERE file_id = ?
                    """,
                    (
                        scan_id,
                        str(candidate.observed.device),
                        str(candidate.observed.inode),
                        file_id,
                    ),
                )
            if active_revision_id is not None:
                active = connection.execute(
                    "SELECT content_sha256, capture_id FROM markdown_import_revisions "
                    "WHERE file_id = ? AND revision_id = ?",
                    (file_id, active_revision_id),
                ).fetchone()
                if active is None or active["capture_id"] is None:
                    raise RuntimeError("invalid active Markdown import revision")
                if cast(str, active["content_sha256"]) == content_sha256:
                    return _Reservation(
                        revision_id=active_revision_id,
                        capture_id=cast(str, active["capture_id"]),
                        outcome="unchanged",
                    )
            revision = connection.execute(
                "SELECT * FROM markdown_import_revisions WHERE file_id = ? AND content_sha256 = ?",
                (file_id, content_sha256),
            ).fetchone()
            if revision is None:
                revision_id = _new_id("import_revision")
                connection.execute(
                    """
                    INSERT INTO markdown_import_revisions (
                        revision_id, file_id, content_sha256, delivery_id,
                        request_sha256, capture_id, first_observed_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        revision_id,
                        file_id,
                        content_sha256,
                        delivery_id,
                        request_sha256,
                        _timestamp(self._engine._clock()),
                    ),
                )
                capture_id = None
            else:
                revision_id = _portable_id(cast(str, revision["revision_id"]), "import_revision")
                if (
                    cast(str, revision["delivery_id"]) != delivery_id
                    or cast(str, revision["request_sha256"]) != request_sha256
                ):
                    raise RuntimeError("invalid Markdown import reservation")
                capture_id = cast(str | None, revision["capture_id"])
            captured_revision_count = cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM markdown_import_revisions "
                    "WHERE file_id = ? AND capture_id IS NOT NULL",
                    (file_id,),
                ).fetchone()[0],
            )
            return _Reservation(
                revision_id=revision_id,
                capture_id=capture_id,
                outcome="imported" if new_file or captured_revision_count == 0 else "updated",
            )

    def _activate_revision(
        self,
        *,
        root_id: str,
        scan_id: str,
        candidate: MarkdownCandidate,
        reservation: _Reservation,
        capture_id: str,
        request_sha256: str,
    ) -> None:
        with self._engine._store.transaction() as connection:
            file_row = connection.execute(
                "SELECT * FROM markdown_import_files WHERE root_id = ? AND relative_path = ?",
                (root_id, candidate.relative_path),
            ).fetchone()
            revision = connection.execute(
                "SELECT * FROM markdown_import_revisions WHERE revision_id = ?",
                (reservation.revision_id,),
            ).fetchone()
            if file_row is None or revision is None:
                raise RuntimeError("Markdown import reservation is unavailable")
            if (
                cast(str, revision["file_id"]) != cast(str, file_row["file_id"])
                or cast(str, revision["request_sha256"]) != request_sha256
                or revision["capture_id"] not in {None, capture_id}
            ):
                raise RuntimeError("Markdown import reservation changed")
            previous = cast(str | None, file_row["active_revision_id"])
            if previous is not None and previous != reservation.revision_id:
                previous_capture = connection.execute(
                    "SELECT capture_id FROM markdown_import_revisions WHERE revision_id = ?",
                    (previous,),
                ).fetchone()
                if previous_capture is None or previous_capture["capture_id"] is None:
                    raise RuntimeError("active Markdown import capture is unavailable")
                connection.execute(
                    "DELETE FROM search_documents WHERE result_id = ?",
                    (previous_capture["capture_id"],),
                )
            connection.execute(
                "UPDATE markdown_import_revisions SET capture_id = ? WHERE revision_id = ?",
                (capture_id, reservation.revision_id),
            )
            connection.execute(
                """
                UPDATE markdown_import_files
                SET active_revision_id = ?, last_observed_scan_id = ?,
                    last_observed_device = ?, last_observed_inode = ?
                WHERE file_id = ?
                """,
                (
                    reservation.revision_id,
                    scan_id,
                    str(candidate.observed.device),
                    str(candidate.observed.inode),
                    file_row["file_id"],
                ),
            )
            capture = connection.execute(
                "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
            if capture is None:
                raise RuntimeError("Markdown import capture is unavailable")
            self._engine._upsert_source_search(connection, capture)

    def _mark_existing_path_observed(
        self,
        root_id: str,
        path: str,
        scan_id: str,
        *,
        candidate: MarkdownCandidate | None = None,
    ) -> None:
        with self._engine._store.transaction() as connection:
            if candidate is None:
                connection.execute(
                    """
                    UPDATE markdown_import_files
                    SET last_observed_scan_id = ?
                    WHERE root_id = ? AND relative_path = ?
                    """,
                    (scan_id, root_id, path),
                )
            else:
                connection.execute(
                    """
                    UPDATE markdown_import_files
                    SET last_observed_scan_id = ?, last_observed_device = ?,
                        last_observed_inode = ?
                    WHERE root_id = ? AND relative_path = ?
                    """,
                    (
                        scan_id,
                        str(candidate.observed.device),
                        str(candidate.observed.inode),
                        root_id,
                        path,
                    ),
                )

    def _finalize_scan(self, root_id: str, scan_id: str) -> list[MarkdownImportEntry]:
        missing: list[MarkdownImportEntry] = []
        try:
            with self._engine._store.transaction() as connection:
                rows = tuple(
                    connection.execute(
                        """
                        SELECT f.file_id, f.relative_path, r.capture_id
                        FROM markdown_import_files AS f
                        JOIN markdown_import_revisions AS r
                          ON r.file_id = f.file_id
                         AND r.revision_id = f.active_revision_id
                        WHERE f.root_id = ?
                          AND f.active_revision_id IS NOT NULL
                          AND f.last_observed_scan_id <> ?
                        ORDER BY f.relative_path
                        """,
                        (root_id, scan_id),
                    )
                )
                for row in rows:
                    capture_id = cast(str, row["capture_id"])
                    connection.execute(
                        "DELETE FROM search_documents WHERE result_id = ?", (capture_id,)
                    )
                    connection.execute(
                        "UPDATE markdown_import_files SET active_revision_id = NULL "
                        "WHERE file_id = ?",
                        (row["file_id"],),
                    )
                    missing.append(
                        MarkdownImportEntry(outcome="missing", path=cast(str, row["relative_path"]))
                    )
                connection.execute(
                    """
                    UPDATE markdown_import_roots
                    SET last_complete_scan_id = ?, last_complete_scan_at = ?
                    WHERE root_id = ?
                    """,
                    (scan_id, _timestamp(self._engine._clock()), root_id),
                )
        except KeyboardInterrupt:
            connection = self._engine._store.connect()
            try:
                row = connection.execute(
                    "SELECT last_complete_scan_id FROM markdown_import_roots WHERE root_id = ?",
                    (root_id,),
                ).fetchone()
            finally:
                connection.close()
            if row is None or row["last_complete_scan_id"] != scan_id:
                raise MarkdownImportInterrupted from None
        return missing

    @staticmethod
    def _raise_root_changed(root_id: str) -> None:
        raise MarkdownImportFailure(
            "import_root_changed",
            details={"missing_finalized": False, "root_id": root_id},
        )

    @staticmethod
    def _raise_overlap(root_id: str) -> None:
        raise MarkdownImportFailure(
            "overlapping_import_root",
            details={
                "conflict": "registered_import",
                "missing_finalized": False,
                "root_id": root_id,
            },
        )


def capture_submission_is_reserved(
    engine: BrainEngine,
    submission: CaptureSubmission,
) -> bool:
    """Validate the reserved importer namespace at the private capture boundary."""
    if not submission.delivery_id.startswith(_IMPORT_DELIVERY_PREFIX):
        return False
    connection = engine._store.connect()
    try:
        row = connection.execute(
            """
            SELECT request_sha256, capture_id
            FROM markdown_import_revisions
            WHERE delivery_id = ?
            """,
            (submission.delivery_id,),
        ).fetchone()
    finally:
        connection.close()
    if (
        row is None
        or row["capture_id"] is not None
        or row["request_sha256"] != submission.request_sha256()
    ):
        raise ValueError("invalid reserved capture delivery")
    return True


def capture_projection_is_active(
    connection: sqlite3.Connection,
    *,
    delivery_id: str,
    capture_id: str,
) -> bool:
    """Return whether a source capture may enter the live search projection."""
    revision = connection.execute(
        """
        SELECT r.revision_id, r.capture_id, f.active_revision_id
        FROM markdown_import_revisions AS r
        JOIN markdown_import_files AS f USING (file_id)
        WHERE r.delivery_id = ?
        """,
        (delivery_id,),
    ).fetchone()
    if revision is None:
        return True
    return bool(
        revision["capture_id"] == capture_id
        and revision["active_revision_id"] == revision["revision_id"]
    )


def extract_markdown_title(text: str, relative_path: str) -> str:
    """Extract the bounded display title without interpreting Markdown content."""
    source = text.removeprefix("\ufeff")
    lines = source.splitlines(keepends=True)
    body_start = 0
    title: str | None = None
    if lines and lines[0].rstrip("\r\n") == "---":
        consumed = len(lines[0].encode("utf-8"))
        closing: int | None = None
        for index, line in enumerate(lines[1:100], start=1):
            consumed += len(line.encode("utf-8"))
            if consumed > 65_536:
                break
            if line.rstrip("\r\n") == "---":
                closing = index
                break
        if closing is not None:
            body_start = closing + 1
            candidates = [
                parsed
                for line in lines[1:closing]
                if (parsed := _frontmatter_title(line.rstrip("\r\n"))) is not None
            ]
            if len(candidates) == 1:
                title = candidates[0]
    if title is None:
        for line in lines[body_start:]:
            match = _ATX_HEADING.fullmatch(line.rstrip("\r\n"))
            if match is not None:
                title = match.group(1)
                break
    if title is None:
        title = relative_path.rsplit("/", 1)[-1].removesuffix(".md")
    return _sanitize_title(title)


def _frontmatter_title(line: str) -> str | None:
    if not line.startswith("title:") or line[:1].isspace():
        return None
    raw = line.removeprefix("title:").strip()
    if not raw or raw[0] in "!&*[{|>":
        return None
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, str) else None
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'"):
            return None
        return raw[1:-1].replace("''", "'")
    return raw


def _sanitize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    replaced = "".join(
        " "
        if (
            ord(character) <= 0x1F
            or 0x7F <= ord(character) <= 0x9F
            or unicodedata.category(character) == "Cf"
        )
        else character
        for character in normalized
    )
    bounded = " ".join(replaced.split())[:200]
    return bounded or "Untitled note"


def _import_context(engine: BrainEngine) -> PublicJobCaptureContext:
    return PublicJobCaptureContext.create(
        profile=engine.profile,
        actor_id=_IMPORT_ACTOR_ID,
        role_claim={
            "actor_id": _IMPORT_ACTOR_ID,
            "capabilities": ["capture.accept"],
            "role_claim_id": _IMPORT_ROLE_CLAIM_ID,
            "role_id": _IMPORT_ROLE_ID,
            "tenant_id": engine.profile.tenant_id,
        },
    )


def _delivery_id(root_id: str, relative_path: str, content_sha256: str) -> str:
    value = {
        "domain": "open-brain-markdown-import-v1",
        "content_sha256": content_sha256,
        "relative_path": relative_path,
        "root_id": root_id,
    }
    return _IMPORT_DELIVERY_PREFIX + sha256(portable_canonical_json_bytes(value)).hexdigest()


def _lexically_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _summary(
    selected: int,
    outcomes: list[MarkdownImportEntry],
) -> MarkdownImportSummary:
    failures = sorted(
        (entry for entry in outcomes if entry.outcome == "failed"),
        key=_entry_sort_key,
    )
    other = sorted(
        (entry for entry in outcomes if entry.outcome != "failed"),
        key=lambda entry: (*_entry_sort_key(entry), entry.outcome),
    )
    ordered = failures + other
    entries = tuple(ordered[:MAX_SUMMARY_ENTRIES])
    counts = {
        outcome: sum(entry.outcome == outcome for entry in outcomes)
        for outcome in ("failed", "imported", "missing", "skipped", "unchanged", "updated")
    }
    return MarkdownImportSummary(
        entries=entries,
        entries_omitted=len(ordered) - len(entries),
        failed=counts["failed"],
        imported=counts["imported"],
        missing=counts["missing"],
        missing_finalized=True,
        selected=selected,
        skipped=counts["skipped"],
        unchanged=counts["unchanged"],
        updated=counts["updated"],
    )


def _entry_sort_key(entry: MarkdownImportEntry) -> tuple[bool, str]:
    return (entry.path.startswith("<invalid-path-"), entry.path)


__all__ = [
    "capture_projection_is_active",
    "capture_submission_is_reserved",
    "extract_markdown_title",
    "MarkdownImportTasks",
    "MAX_SUMMARY_ENTRIES",
]
