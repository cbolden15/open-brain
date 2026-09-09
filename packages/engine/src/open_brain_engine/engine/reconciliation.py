"""Bounded direct-Markdown reconciliation for canonical owner content."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from open_brain_engine.portable.v1 import validate_portable_write
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    StorageError,
    read_confined_tree,
)
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown

from .contracts import ReconciliationReceipt
from .normalization import _portable_id
from .search_projection import (
    project_search_document,
    source_search_title,
    upsert_search_document,
)

if TYPE_CHECKING:
    from .local import BrainEngine

_MAXIMUM_MARKDOWN_BYTES = 64 * 1024
_MAXIMUM_SCANNED_FILES = 256
_MAXIMUM_TOTAL_MARKDOWN_BYTES = _MAXIMUM_MARKDOWN_BYTES * _MAXIMUM_SCANNED_FILES


@dataclass(frozen=True, slots=True)
class _SpaceUpdate:
    space_id: str
    name: str


@dataclass(frozen=True, slots=True)
class _PageUpdate:
    page_id: str
    capture_id: str
    payload_family: str
    space_id: str
    title: str
    body: str
    updated_at: str
    canonical_path: str


@dataclass(frozen=True, slots=True)
class _ProjectionInput:
    result_id: str
    capture_id: str
    record_type: str
    payload_family: str
    space_id: str | None
    title: str
    body: str
    canonical_path: str | None
    updated_at: str


class ReconciliationTasks:
    """Refresh derived retrieval state from owner-edited canonical Markdown bytes."""

    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def reconcile(self) -> ReconciliationReceipt:
        self._engine._assert_root()
        with self._engine._writer_lease.acquire_shared_writer():
            scanned_files, space_updates, page_updates = self._scan()
            if not space_updates and not page_updates:
                return ReconciliationReceipt(
                    status="noop",
                    scanned_files=scanned_files,
                    page_updates=0,
                    space_updates=0,
                )
            with self._engine._store.transaction() as connection:
                for space_update in space_updates:
                    connection.execute(
                        "UPDATE spaces SET name = ? WHERE space_id = ?",
                        (space_update.name, space_update.space_id),
                    )
                for page_update in page_updates:
                    self._engine._upsert_canonical_search(
                        connection,
                        result_id=page_update.page_id,
                        capture_id=page_update.capture_id,
                        payload_family=page_update.payload_family,
                        space_id=page_update.space_id,
                        title=page_update.title,
                        body=page_update.body,
                        canonical_path=page_update.canonical_path,
                        updated_at=page_update.updated_at,
                    )
            return ReconciliationReceipt(
                status="reconciled",
                scanned_files=scanned_files,
                page_updates=len(page_updates),
                space_updates=len(space_updates),
            )

    def _scan(self) -> tuple[int, tuple[_SpaceUpdate, ...], tuple[_PageUpdate, ...]]:
        connection = self._engine._store.connect()
        try:
            known_spaces = {
                cast(str, row["space_id"]): (cast(str, row["name"]), cast(str, row["slug"]))
                for row in connection.execute("SELECT space_id, name, slug FROM spaces")
            }
            canonical_rows = {
                cast(str, row["canonical_path"]): row
                for row in connection.execute(
                    """
                    SELECT result_id, capture_id, payload_family, space_id, title, body, trust,
                           updated_at, canonical_path
                    FROM search_documents
                    WHERE record_type = 'canonical'
                    """
                )
            }
        finally:
            connection.close()
        try:
            files = read_confined_tree(
                root=self._engine.profile.root,
                relative="content/spaces",
                expected_root_identity=self._engine.profile.root_identity,
                maximum_entries=_MAXIMUM_SCANNED_FILES,
                maximum_file_bytes=_MAXIMUM_MARKDOWN_BYTES,
                maximum_total_bytes=_MAXIMUM_TOTAL_MARKDOWN_BYTES,
            )
        except RootConfinementError as error:
            raise ValueError("canonical Markdown symlink or unsafe path is not allowed") from error
        except StorageError as error:
            raise ValueError("canonical Markdown exceeds the bounded size or file count") from error
        space_updates: list[_SpaceUpdate] = []
        page_updates: list[_PageUpdate] = []
        seen_spaces: set[str] = set()
        seen_pages: set[str] = set()
        for relative, payload in files:
            parts = relative.parts
            canonical_path = f"content/spaces/{relative.as_posix()}"
            fields = _parse(payload)
            _require_owner_identity(fields, self._engine)
            try:
                validate_portable_write(
                    canonical_path,
                    payload,
                    self._engine.profile.tenant_id,
                )
            except ValueError as error:
                raise ValueError("canonical Markdown is invalid") from error
            if len(parts) == 2 and parts[1] == "_space.md":
                slug = parts[0]
                space_id = _required_string(fields, "space_id")
                expected = known_spaces.get(space_id)
                if expected is None:
                    raise ValueError("canonical space identity is unknown")
                if _required_string(fields, "slug") != slug or expected[1] != slug:
                    raise ValueError("canonical space slug changed")
                seen_spaces.add(space_id)
                name = _required_string(fields, "name")
                if name != expected[0]:
                    space_updates.append(_SpaceUpdate(space_id=space_id, name=name))
                continue
            if len(parts) != 3 or parts[1] != "notes":
                raise ValueError("canonical Markdown inventory is invalid")
            page_id = _required_string(fields, "page_id")
            if parts[2] != f"{page_id}.md":
                raise ValueError("canonical page identity changed")
            row = canonical_rows.get(canonical_path)
            if row is None or cast(str, row["result_id"]) != page_id:
                raise ValueError("canonical page provenance changed")
            space_id = _required_string(fields, "space_id")
            capture_id = cast(str, row["capture_id"])
            provenance = fields.get("provenance")
            if (
                space_id != cast(str, row["space_id"])
                or space_id not in seen_spaces
                or provenance != [capture_id]
            ):
                raise ValueError("canonical page provenance changed")
            seen_pages.add(canonical_path)
            parsed = parse_markdown(payload)
            title = _required_string(fields, "title")
            trust = _required_string(fields, "trust")
            updated_at = _required_string(fields, "modified_at")
            projection_connection = self._engine._store.connect()
            try:
                projection = project_search_document(
                    projection_connection,
                    result_id=page_id,
                    capture_id=capture_id,
                    record_type="canonical",
                    title=title,
                    body=parsed.body,
                    canonical_path=canonical_path,
                )
            finally:
                projection_connection.close()
            if trust != projection.canonical_frontmatter_trust:
                raise ValueError("canonical page trust changed")
            if (
                projection.title != cast(str, row["title"])
                or projection.body != cast(str, row["body"])
                or projection.trust != cast(str, row["trust"])
                or updated_at != cast(str, row["updated_at"])
            ):
                page_updates.append(
                    _PageUpdate(
                        page_id=page_id,
                        capture_id=capture_id,
                        payload_family=cast(str, row["payload_family"]),
                        space_id=space_id,
                        title=title,
                        body=parsed.body,
                        updated_at=updated_at,
                        canonical_path=canonical_path,
                    )
                )
        if seen_spaces != set(known_spaces):
            raise ValueError("canonical space Markdown is missing")
        missing_pages = set(canonical_rows) - seen_pages
        if missing_pages:
            raise ValueError("canonical page Markdown is missing")
        return len(files), tuple(space_updates), tuple(page_updates)


def rederive_live_search_projection(engine: BrainEngine) -> None:
    """Atomically rebuild every live search row from durable captures and pages."""
    engine._assert_root()
    with (
        engine._writer_lease.acquire_shared_writer(),
        engine._store.transaction() as connection,
    ):
        inputs = _projection_inputs(engine, connection)
        for item in inputs:
            project_search_document(
                connection,
                result_id=item.result_id,
                capture_id=item.capture_id,
                record_type=item.record_type,
                title=item.title,
                body=item.body,
                canonical_path=item.canonical_path,
            )
        connection.execute("DELETE FROM search_documents")
        connection.execute("DELETE FROM search_documents_fts")
        connection.execute("DELETE FROM search_fts_identity")
        for item in inputs:
            upsert_search_document(
                connection,
                result_id=item.result_id,
                capture_id=item.capture_id,
                record_type=item.record_type,
                payload_family=item.payload_family,
                space_id=item.space_id,
                title=item.title,
                body=item.body,
                canonical_path=item.canonical_path,
                updated_at=item.updated_at,
            )


def _projection_inputs(
    engine: BrainEngine,
    connection: sqlite3.Connection,
) -> tuple[_ProjectionInput, ...]:
    known_spaces = {
        cast(str, row["space_id"]): cast(str, row["slug"])
        for row in connection.execute("SELECT space_id, slug FROM spaces")
    }
    captures = tuple(
        connection.execute(
            """
            SELECT capture_id, payload_family, space_id, search_text, title, accepted_at,
                   source_origin, source_reference, provenance_json,
                   (
                       NOT EXISTS (
                           SELECT 1
                           FROM markdown_import_revisions AS r
                           WHERE r.delivery_id = c.delivery_id
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM markdown_import_revisions AS r
                           JOIN markdown_import_files AS f
                             ON f.file_id = r.file_id
                            AND f.active_revision_id = r.revision_id
                           WHERE r.delivery_id = c.delivery_id
                             AND r.capture_id = c.capture_id
                       )
                   ) AS source_projection_active
            FROM captures AS c
            ORDER BY capture_id
            """
        )
    )
    capture_by_id: dict[str, sqlite3.Row] = {}
    inputs: list[_ProjectionInput] = []
    for capture in captures:
        capture_id = _row_string(capture, "capture_id")
        _portable_id(capture_id, "capture")
        payload_family = _row_string(capture, "payload_family")
        if payload_family not in {"text", "reference_or_file", "event", "measurement"}:
            raise ValueError("search projection capture is invalid")
        body = _row_string(capture, "search_text", allow_empty=True)
        space_id = _row_optional_string(capture, "space_id")
        if space_id is not None and space_id not in known_spaces:
            raise ValueError("search projection capture space is unavailable")
        capture_by_id[capture_id] = capture
        if capture["source_projection_active"] not in {0, 1}:
            raise ValueError("search projection capture state is invalid")
        if not bool(capture["source_projection_active"]):
            continue
        inputs.append(
            _ProjectionInput(
                result_id=capture_id,
                capture_id=capture_id,
                record_type="source",
                payload_family=payload_family,
                space_id=space_id,
                title=(
                    _row_string(capture, "title")
                    if capture["title"] is not None
                    else source_search_title(payload_family=payload_family, body=body)
                ),
                body=body,
                canonical_path=None,
                updated_at=_row_string(capture, "accepted_at"),
            )
        )

    expected_pages: dict[str, tuple[str, str]] = {}
    page_ids: set[str] = set()
    for row in connection.execute(
        """
        SELECT canonical_path, page_id, capture_id
        FROM captures
        WHERE canonical_path IS NOT NULL
        UNION ALL
        SELECT d.canonical_path, d.page_id, p.capture_id
        FROM decisions AS d
        JOIN proposals AS p USING (proposal_id)
        WHERE d.canonical_path IS NOT NULL
          AND d.page_id IS NOT NULL
          AND d.publication_id IS NOT NULL
          AND d.publication_path IS NOT NULL
          AND d.outcome IN ('approved', 'edited')
        """
    ):
        canonical_path = _row_string(row, "canonical_path")
        page_id = _row_string(row, "page_id")
        capture_id = _row_string(row, "capture_id")
        _portable_id(page_id, "page")
        _portable_id(capture_id, "capture")
        if (
            capture_id not in capture_by_id
            or canonical_path in expected_pages
            or page_id in page_ids
        ):
            raise ValueError("canonical page provenance is invalid")
        expected_pages[canonical_path] = (page_id, capture_id)
        page_ids.add(page_id)

    files = _read_canonical_tree(engine)
    parsed_files: list[tuple[PurePosixPath, str, dict[str, object], str]] = []
    for relative, payload in files:
        canonical_path = f"content/spaces/{relative.as_posix()}"
        try:
            parsed = parse_markdown(payload)
            fields = dict(parsed.fields)
        except MarkdownFormatError as error:
            raise ValueError("canonical Markdown is invalid") from error
        _require_owner_identity(fields, engine)
        try:
            validate_portable_write(canonical_path, payload, engine.profile.tenant_id)
        except ValueError as error:
            raise ValueError("canonical Markdown is invalid") from error
        parsed_files.append((relative, canonical_path, fields, parsed.body))

    seen_spaces: set[str] = set()
    for relative, _canonical_path, fields, _body in parsed_files:
        parts = relative.parts
        if len(parts) == 2 and parts[1] == "_space.md":
            space_id = _required_string(fields, "space_id")
            slug = _required_string(fields, "slug")
            if space_id in seen_spaces or known_spaces.get(space_id) != slug or parts[0] != slug:
                raise ValueError("canonical space provenance is invalid")
            seen_spaces.add(space_id)
    if seen_spaces != set(known_spaces):
        raise ValueError("canonical space Markdown is missing")

    seen_pages: set[str] = set()
    for relative, canonical_path, fields, body in parsed_files:
        parts = relative.parts
        if len(parts) == 2 and parts[1] == "_space.md":
            continue
        if len(parts) != 3 or parts[1] != "notes":
            raise ValueError("canonical Markdown inventory is invalid")
        expected = expected_pages.get(canonical_path)
        page_id = _required_string(fields, "page_id")
        space_id = _required_string(fields, "space_id")
        if (
            expected is None
            or canonical_path in seen_pages
            or expected[0] != page_id
            or parts[2] != f"{page_id}.md"
            or known_spaces.get(space_id) != parts[0]
            or fields.get("provenance") != [expected[1]]
        ):
            raise ValueError("canonical page provenance is invalid")
        seen_pages.add(canonical_path)
        capture = capture_by_id[expected[1]]
        projection = project_search_document(
            connection,
            result_id=page_id,
            capture_id=expected[1],
            record_type="canonical",
            title=_required_string(fields, "title"),
            body=body,
            canonical_path=canonical_path,
        )
        if _required_string(fields, "trust") != projection.canonical_frontmatter_trust:
            raise ValueError("canonical page trust changed")
        inputs.append(
            _ProjectionInput(
                result_id=page_id,
                capture_id=expected[1],
                record_type="canonical",
                payload_family=_row_string(capture, "payload_family"),
                space_id=space_id,
                title=_required_string(fields, "title"),
                body=body,
                canonical_path=canonical_path,
                updated_at=_required_string(fields, "modified_at"),
            )
        )
    if seen_pages != set(expected_pages):
        raise ValueError("canonical page Markdown is missing")
    if len({item.result_id for item in inputs}) != len(inputs):
        raise ValueError("search projection identity is invalid")
    return tuple(sorted(inputs, key=lambda item: item.result_id))


def _read_canonical_tree(engine: BrainEngine) -> tuple[tuple[PurePosixPath, bytes], ...]:
    try:
        return read_confined_tree(
            root=engine.profile.root,
            relative="content/spaces",
            expected_root_identity=engine.profile.root_identity,
            maximum_entries=_MAXIMUM_SCANNED_FILES,
            maximum_file_bytes=_MAXIMUM_MARKDOWN_BYTES,
            maximum_total_bytes=_MAXIMUM_TOTAL_MARKDOWN_BYTES,
        )
    except RootConfinementError as error:
        raise ValueError("canonical Markdown symlink or unsafe path is not allowed") from error
    except StorageError as error:
        raise ValueError("canonical Markdown exceeds the bounded size or file count") from error


def _row_string(row: sqlite3.Row, key: str, *, allow_empty: bool = False) -> str:
    value = row[key]
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("search projection durable state is invalid")
    return value


def _row_optional_string(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    if value is not None and not isinstance(value, str):
        raise ValueError("search projection durable state is invalid")
    return value


def _parse(payload: bytes) -> dict[str, object]:
    try:
        return dict(parse_markdown(payload).fields)
    except MarkdownFormatError as error:
        raise ValueError("canonical Markdown is invalid") from error


def _required_string(fields: dict[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("canonical Markdown is invalid")
    return value


def _require_owner_identity(fields: dict[str, object], engine: BrainEngine) -> None:
    role_claim = fields.get("role_claim")
    expected = engine.profile.owner_role_claim
    capabilities = role_claim.get("capabilities") if isinstance(role_claim, Mapping) else None
    expected_capabilities = expected.get("capabilities")
    if (
        _required_string(fields, "tenant_id") != engine.profile.tenant_id
        or _required_string(fields, "actor_id") != engine.profile.owner_actor_id
        or not isinstance(role_claim, Mapping)
        or any(
            role_claim.get(key) != expected.get(key)
            for key in ("actor_id", "role_claim_id", "role_id", "tenant_id")
        )
        or not isinstance(capabilities, (list, tuple))
        or not isinstance(expected_capabilities, (list, tuple))
        or tuple(capabilities) != tuple(expected_capabilities)
    ):
        raise ValueError("canonical Markdown owner identity changed")
