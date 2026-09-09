"""Public-safe lexical retrieval and caller-scoped capability enforcement."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from open_brain_engine.storage.filesystem import read_confined
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown

from .contracts import (
    PageResult,
    PublicProvenance,
    RetrievalResult,
    RetrievalTask,
    _LocalEngineOperations,
)
from .local_store import rebuild_live_search_fts
from .normalization import _portable_id, _text
from .search_projection import (
    public_search_text,
    public_source_origin,
    source_search_title,
    upsert_search_document,
)

if TYPE_CHECKING:
    from .local import BrainEngine


_MAXIMUM_ALLOWED_SPACES = 256


@dataclass(frozen=True, slots=True)
class _CompiledQuery:
    disjunction: str
    title_phrase: str
    title_all: str
    phrase_any: str
    all_any: str


class RetrievalOperations(_LocalEngineOperations):
    def _upsert_source_search(self, connection: sqlite3.Connection, capture: sqlite3.Row) -> None:
        self._upsert_search(
            connection,
            result_id=cast(str, capture["capture_id"]),
            capture_id=cast(str, capture["capture_id"]),
            record_type="source",
            payload_family=cast(str, capture["payload_family"]),
            space_id=cast(str | None, capture["space_id"]),
            title=(
                cast(str, capture["title"])
                if capture["title"] is not None
                else source_search_title(
                    payload_family=cast(str, capture["payload_family"]),
                    body=cast(str, capture["search_text"]),
                )
            ),
            body=cast(str, capture["search_text"]),
            canonical_path=None,
            updated_at=cast(str, capture["accepted_at"]),
        )

    def _upsert_canonical_search(
        self,
        connection: sqlite3.Connection,
        *,
        result_id: str,
        capture_id: str,
        payload_family: str,
        space_id: str,
        title: str,
        body: str,
        canonical_path: str,
        updated_at: str,
    ) -> None:
        self._upsert_search(
            connection,
            result_id=result_id,
            capture_id=capture_id,
            record_type="canonical",
            payload_family=payload_family,
            space_id=space_id,
            title=title,
            body=body,
            canonical_path=canonical_path,
            updated_at=updated_at,
        )

    def _upsert_search(
        self,
        connection: sqlite3.Connection,
        *,
        result_id: str,
        capture_id: str,
        record_type: str,
        payload_family: str,
        space_id: str | None,
        title: str,
        body: str,
        canonical_path: str | None,
        updated_at: str,
    ) -> None:
        upsert_search_document(
            connection,
            result_id=result_id,
            capture_id=capture_id,
            record_type=record_type,
            payload_family=payload_family,
            space_id=space_id,
            title=title,
            body=body,
            canonical_path=canonical_path,
            updated_at=updated_at,
        )

    def _search(
        self,
        query: str,
        *,
        space_id: str | None,
        payload_family: str | None,
        record_type: str | None,
        limit: int,
        allowed_space_ids: frozenset[str] | None = None,
    ) -> tuple[RetrievalResult, ...]:
        compiled = _compile_query(query)
        if space_id is not None:
            _portable_id(space_id, "space")
        if payload_family is not None and payload_family not in {
            "text",
            "reference_or_file",
            "event",
            "measurement",
        }:
            raise ValueError("invalid payload-family filter")
        if record_type is not None and record_type not in {"source", "canonical"}:
            raise ValueError("invalid record-type filter")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid result limit")
        allowed_sql, allowed_parameters = _allowed_space_relation(allowed_space_ids)
        parameters: dict[str, object] = {
            **allowed_parameters,
            "all_any": compiled.all_any,
            "disjunction": compiled.disjunction,
            "limit": limit,
            "payload_family": payload_family,
            "phrase_any": compiled.phrase_any,
            "record_type": record_type,
            "scoped": int(allowed_space_ids is not None),
            "space_id": space_id,
            "title_all": compiled.title_all,
            "title_phrase": compiled.title_phrase,
        }
        sql = f"""
            WITH
            allowed(space_id) AS ({allowed_sql}),
            authorized AS MATERIALIZED (
                SELECT d.*, i.fts_rowid,
                       c.source_origin AS capture_source_origin,
                       c.source_reference AS protected_source_reference,
                       c.provenance_json AS capture_provenance_json
                FROM search_documents AS d
                JOIN search_fts_identity AS i USING (result_id)
                JOIN captures AS c ON c.capture_id = d.capture_id
                WHERE (:scoped = 0 OR d.space_id IN (SELECT space_id FROM allowed))
                  AND (:space_id IS NULL OR d.space_id = :space_id)
                  AND (:payload_family IS NULL OR d.payload_family = :payload_family)
                  AND (:record_type IS NULL OR d.record_type = :record_type)
            ),
            hits AS MATERIALIZED (
                SELECT a.*,
                       bm25(search_documents_fts, 0.0, 10.0, 1.0) AS fts_score,
                       snippet(search_documents_fts, -1, '[', ']', '…', 24) AS fts_excerpt
                FROM authorized AS a
                JOIN search_documents_fts
                  ON search_documents_fts.rowid = a.fts_rowid
                 AND search_documents_fts.result_id = a.result_id
                WHERE search_documents_fts MATCH :disjunction
            ),
            title_phrase AS (
                SELECT a.fts_rowid
                FROM authorized AS a
                JOIN search_documents_fts
                  ON search_documents_fts.rowid = a.fts_rowid
                 AND search_documents_fts.result_id = a.result_id
                WHERE search_documents_fts MATCH :title_phrase
            ),
            title_all AS (
                SELECT a.fts_rowid
                FROM authorized AS a
                JOIN search_documents_fts
                  ON search_documents_fts.rowid = a.fts_rowid
                 AND search_documents_fts.result_id = a.result_id
                WHERE search_documents_fts MATCH :title_all
            ),
            phrase_any AS (
                SELECT a.fts_rowid
                FROM authorized AS a
                JOIN search_documents_fts
                  ON search_documents_fts.rowid = a.fts_rowid
                 AND search_documents_fts.result_id = a.result_id
                WHERE search_documents_fts MATCH :phrase_any
            ),
            all_any AS (
                SELECT a.fts_rowid
                FROM authorized AS a
                JOIN search_documents_fts
                  ON search_documents_fts.rowid = a.fts_rowid
                 AND search_documents_fts.result_id = a.result_id
                WHERE search_documents_fts MATCH :all_any
            ),
            ranked AS (
                SELECT h.*,
                       CASE
                           WHEN tp.fts_rowid IS NOT NULL THEN 1
                           WHEN ta.fts_rowid IS NOT NULL THEN 2
                           WHEN pa.fts_rowid IS NOT NULL THEN 3
                           WHEN aa.fts_rowid IS NOT NULL THEN 4
                           ELSE 5
                       END AS relevance_tier,
                       CASE
                           WHEN ta.fts_rowid IS NOT NULL THEN 'title match'
                           WHEN pa.fts_rowid IS NOT NULL THEN 'exact phrase match'
                           ELSE 'lexical match'
                       END AS search_explanation
                FROM hits AS h
                LEFT JOIN title_phrase AS tp USING (fts_rowid)
                LEFT JOIN title_all AS ta USING (fts_rowid)
                LEFT JOIN phrase_any AS pa USING (fts_rowid)
                LEFT JOIN all_any AS aa USING (fts_rowid)
            )
            SELECT *
            FROM ranked
            ORDER BY relevance_tier ASC,
                     fts_score ASC,
                     CASE record_type WHEN 'canonical' THEN 0 ELSE 1 END ASC,
                     title COLLATE BINARY ASC,
                     result_id COLLATE BINARY ASC
            LIMIT :limit
        """
        connection = self._store.connect()
        try:
            rows = tuple(connection.execute(sql, parameters))
        finally:
            connection.close()
        results: list[RetrievalResult] = []
        for row in rows:
            result = self._retrieval_result(
                row,
                excerpt=cast(str, row["fts_excerpt"]),
                explanation=cast(str, row["search_explanation"]),
                require_current_projection=True,
            )
            if result is not None:
                results.append(result)
        return tuple(results)

    def _fetch(
        self, result_id: str, *, allowed_space_ids: frozenset[str] | None = None
    ) -> RetrievalResult | None:
        row = self._search_row(result_id, allowed_space_ids=allowed_space_ids)
        if row is None:
            return None
        return self._retrieval_result(
            row,
            excerpt=None,
            explanation="fetched by result identifier",
        )

    def _read_page(
        self,
        result_id: str,
        *,
        allowed_space_ids: frozenset[str] | None = None,
    ) -> PageResult | None:
        row = self._search_row(result_id, allowed_space_ids=allowed_space_ids)
        if row is None:
            return None
        document = self._resolved_document(row)
        if document is None:
            return None
        title, body = document
        protected_source_reference = cast(str, row["protected_source_reference"])
        return PageResult(
            page_id=cast(str, row["result_id"]),
            title=public_search_text(
                title,
                protected_source_reference=protected_source_reference,
            ),
            markdown=public_search_text(
                body,
                protected_source_reference=protected_source_reference,
            ),
            trust=cast(str, row["trust"]),
        )

    def _search_row(
        self,
        result_id: str,
        *,
        allowed_space_ids: frozenset[str] | None,
    ) -> sqlite3.Row | None:
        _validate_allowed_spaces(allowed_space_ids)
        if allowed_space_ids is not None:
            if not allowed_space_ids:
                return None
            space_ids = tuple(sorted(allowed_space_ids))
            sql = (
                "SELECT d.*, c.source_origin AS capture_source_origin, "
                "c.source_reference AS protected_source_reference, "
                "c.provenance_json AS capture_provenance_json "
                "FROM search_documents AS d JOIN captures AS c USING (capture_id) "
                "WHERE d.result_id = ? AND d.space_id IN ("
                + ", ".join("?" for _ in space_ids)
                + ")"
            )
            parameters: tuple[object, ...] = (result_id, *space_ids)
        else:
            sql = (
                "SELECT d.*, c.source_origin AS capture_source_origin, "
                "c.source_reference AS protected_source_reference, "
                "c.provenance_json AS capture_provenance_json "
                "FROM search_documents AS d JOIN captures AS c USING (capture_id) "
                "WHERE d.result_id = ?"
            )
            parameters = (result_id,)
        connection = self._store.connect()
        try:
            row = connection.execute(sql, parameters).fetchone()
        finally:
            connection.close()
        return cast(sqlite3.Row | None, row)

    def _retrieval_result(
        self,
        row: sqlite3.Row,
        *,
        excerpt: str | None,
        explanation: str,
        require_current_projection: bool = False,
    ) -> RetrievalResult | None:
        capture_id = cast(str, row["capture_id"])
        protected_source_reference = cast(str, row["protected_source_reference"])
        raw_title = cast(str, row["title"])
        raw_body = cast(str, row["body"])
        public_title = public_search_text(
            raw_title,
            protected_source_reference=protected_source_reference,
        )
        public_body = public_search_text(
            raw_body,
            protected_source_reference=protected_source_reference,
        )
        if require_current_projection and (
            public_title != raw_title or public_body != raw_body
        ):
            return None
        public_excerpt = (
            " ".join(public_body.split()) or "(empty)"
            if excerpt is None
            else public_search_text(
                excerpt,
                protected_source_reference=protected_source_reference,
            )
        )[:500]
        public_explanation = public_search_text(
            explanation,
            protected_source_reference=protected_source_reference,
        )
        source_origin = public_source_origin(
            {
                "provenance_json": row["capture_provenance_json"],
                "source_origin": row["capture_source_origin"],
                "source_reference": row["protected_source_reference"],
            }
        )
        trust = cast(str, row["trust"])
        if (
            trust not in {"owner", "third_party", "reviewed", "unverified"}
            or source_origin in {"mixed", "unknown"}
        ):
            trust = "unverified"
        return RetrievalResult(
            result_id=cast(str, row["result_id"]),
            capture_id=capture_id,
            record_type=cast(str, row["record_type"]),
            payload_family=cast(str, row["payload_family"]),
            space_id=cast(str | None, row["space_id"]),
            title=public_title,
            excerpt=public_excerpt,
            trust=trust,
            provenance=PublicProvenance(
                capture_id=capture_id,
                source_origin=source_origin,
            ),
            explanation=public_explanation,
        )

    def _resolved_document(self, row: sqlite3.Row) -> tuple[str, str] | None:
        title = cast(str, row["title"])
        body = cast(str, row["body"])
        canonical_path = cast(str | None, row["canonical_path"])
        if canonical_path is None:
            return title, body
        payload = read_confined(
            root=self.profile.root,
            relative=canonical_path,
            expected_root_identity=self.profile.root_identity,
        )
        if payload is None:
            return None
        try:
            parsed = parse_markdown(payload)
            return cast(str, parsed.fields["title"]), parsed.body
        except (KeyError, MarkdownFormatError, TypeError):
            return None

    def _rebuild_live_search_index(self) -> None:
        self._assert_root()
        with (
            self._writer_lease.acquire_shared_writer(),
            self._store.transaction() as connection,
        ):
            rebuild_live_search_fts(connection)


def _compile_query(value: str) -> _CompiledQuery:
    normalized = _text(value, field="query", maximum=500)
    chunks = tuple(
        chunk
        for chunk in normalized.split()
        if any(character.isalnum() for character in chunk)
    )
    if not chunks:
        raise ValueError("invalid query")
    literals = tuple(_fts_literal(chunk) for chunk in chunks)
    phrase = _fts_literal(" ".join(chunks))
    conjunction = " AND ".join(literals)
    disjunction = " OR ".join(literals)
    return _CompiledQuery(
        disjunction=disjunction,
        title_phrase=f"title : {phrase}",
        title_all=f"title : ({conjunction})",
        phrase_any=phrase,
        all_any=conjunction,
    )


def _fts_literal(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _allowed_space_relation(
    allowed_space_ids: frozenset[str] | None,
) -> tuple[str, dict[str, object]]:
    _validate_allowed_spaces(allowed_space_ids)
    if not allowed_space_ids:
        return "SELECT NULL WHERE 0", {}
    ordered = tuple(sorted(allowed_space_ids))
    names = tuple(f"allowed_{index}" for index in range(len(ordered)))
    values = ", ".join(f"(:{name})" for name in names)
    return "VALUES " + values, dict(zip(names, ordered, strict=True))


def _validate_allowed_spaces(allowed_space_ids: frozenset[str] | None) -> None:
    if allowed_space_ids is None:
        return
    if not isinstance(allowed_space_ids, frozenset):
        raise ValueError("invalid allowed spaces")
    if len(allowed_space_ids) > _MAXIMUM_ALLOWED_SPACES:
        raise ValueError("invalid allowed space limit")
    if any(_portable_id(identifier, "space") != identifier for identifier in allowed_space_ids):
        raise ValueError("invalid allowed space")


class RetrievalTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def search(
        self,
        query: str,
        *,
        space_id: str | None = None,
        payload_family: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> tuple[RetrievalResult, ...]:
        return self._engine._search(
            query,
            space_id=space_id,
            payload_family=payload_family,
            record_type=record_type,
            limit=limit,
        )

    def fetch(self, result_id: str) -> RetrievalResult | None:
        return self._engine._fetch(result_id)

    def read_page(self, result_id: str) -> PageResult | None:
        return self._engine._read_page(result_id)

    def scoped(self, *, allowed_space_ids: frozenset[str]) -> ScopedRetrieval:
        return ScopedRetrieval(self, allowed_space_ids=allowed_space_ids)


class ScopedRetrieval:
    """Read-only retrieval restricted to a caller's explicit space allow-list."""

    def __init__(self, retrieval: RetrievalTask, *, allowed_space_ids: frozenset[str]) -> None:
        _validate_allowed_spaces(allowed_space_ids)
        self._retrieval = cast(RetrievalTasks, retrieval)
        self._allowed_space_ids = allowed_space_ids

    def search(
        self,
        query: str,
        *,
        space_id: str | None = None,
        payload_family: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> tuple[RetrievalResult, ...]:
        return self._retrieval._engine._search(
            query,
            space_id=space_id,
            payload_family=payload_family,
            record_type=record_type,
            limit=limit,
            allowed_space_ids=self._allowed_space_ids,
        )

    def fetch(self, result_id: str) -> RetrievalResult | None:
        return self._retrieval._engine._fetch(
            result_id,
            allowed_space_ids=self._allowed_space_ids,
        )

    def read_page(self, result_id: str) -> PageResult | None:
        return self._retrieval._engine._read_page(
            result_id,
            allowed_space_ids=self._allowed_space_ids,
        )
