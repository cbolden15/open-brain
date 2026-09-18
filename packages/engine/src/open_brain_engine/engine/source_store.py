"""Immutable capture registration and Portable source metadata projection."""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.v1 import validate_portable_write
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH, canonical_revision_id
from open_brain_engine.storage.filesystem import atomic_replace, read_confined
from open_brain_engine.storage.markdown import parse_markdown

from .contracts import LocalEngineContext
from .t03_contracts import T03Error


def register_completed_captures(
    connection: sqlite3.Connection, profile: LocalEngineContext
) -> None:
    """Run within the existing writer transaction; never replace retained evidence."""
    rows = list(
        connection.execute(
            "SELECT c.* FROM captures c LEFT JOIN source_revisions r USING(capture_id) "
            "WHERE c.stage=3 AND r.capture_id IS NULL ORDER BY c.capture_id"
        )
    )
    for row in rows:
        payload = read_confined(
            root=profile.root,
            relative=row["source_path"],
            expected_root_identity=profile.root_identity,
        )
        if payload is None:
            raise T03Error("operation_pending")
        validate_portable_write(row["source_path"], payload, profile.tenant_id)
        record = json.loads(payload)
        if record["capture_id"] != row["capture_id"] or portable_canonical_json_bytes(
            record["payload"]
        ) != bytes(row["payload_json"]):
            raise T03Error("operation_pending")
        intake = connection.execute(
            "SELECT * FROM source_intakes WHERE delivery_id=?", (row["delivery_id"],)
        ).fetchone()
        if intake is not None:
            from .source_intake import register_intake

            register_intake(connection, row, payload, intake)
            continue
        source_id = "source_" + str(uuid4())
        connection.execute(
            "INSERT INTO logical_sources VALUES(?,?,0,?,0,1,'active','available')",
            (source_id, row["capture_id"], row["space_id"]),
        )
        connection.execute(
            "INSERT INTO source_revisions VALUES(?,?,1,NULL,?,?,?, ?,NULL,NULL,?,NULL)",
            (
                row["capture_id"],
                source_id,
                row["source_path"],
                sha256(payload).hexdigest(),
                payload,
                row["request_sha256"],
                row["accepted_at"],
            ),
        )
        connection.execute(
            "INSERT INTO source_aliases VALUES(?,?,?)",
            (row["delivery_id"], source_id, row["request_sha256"]),
        )
    # Existing legacy routing remains reflected in the logical current-head route.
    for row in connection.execute(
        "SELECT s.source_id,s.space_id,c.space_id AS current_space "
        "FROM logical_sources s JOIN captures c ON c.capture_id=s.head_capture_id "
        "WHERE s.space_id IS NOT c.space_id"
    ):
        connection.execute(
            "UPDATE logical_sources SET space_id=?,route_version=route_version+1 WHERE source_id=?",
            (row["current_space"], row["source_id"]),
        )
    connection.execute(
        "DELETE FROM search_documents WHERE record_type='source' AND capture_id IN ("
        "SELECT r.capture_id FROM source_revisions r JOIN logical_sources s USING(source_id) "
        "WHERE s.head_capture_id != r.capture_id OR s.historical_only=1 "
        "OR s.lifecycle!='active' OR s.availability!='available')"
    )


def source_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    sources = []
    for row in connection.execute("SELECT * FROM logical_sources ORDER BY source_id"):
        source = dict(row)
        source["historical_only"] = bool(source["historical_only"])
        sources.append(source)
    revisions = [
        dict(row)
        for row in connection.execute(
            "SELECT capture_id,source_id,sequence,predecessor_capture_id,source_path,source_sha256,"
            "recorded_at,diagnostic FROM source_revisions ORDER BY source_id,sequence,capture_id"
        )
    ]
    members = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM canonical_revision_members ORDER BY publication_id,ordinal"
        )
    ]
    return {
        "schema_version": 1,
        "sources": sources,
        "revisions": revisions,
        "canonical_members": members,
    }


def publish_source_metadata(connection: sqlite3.Connection, profile: LocalEngineContext) -> None:
    manifest = read_confined(
        root=profile.root,
        relative="portable-manifest.json",
        expected_root_identity=profile.root_identity,
    )
    if manifest is not None and json.loads(manifest).get("schema_version") in {1, 2, 3}:
        # A clean legacy import retains its exact validated input set and retry manifest.
        # Its next v4 export obtains the source sidecar from committed schema-seven rows.
        return
    metadata = portable_canonical_json_bytes(source_metadata(connection))
    current = read_confined(
        root=profile.root,
        relative=SOURCE_METADATA_PATH,
        expected_root_identity=profile.root_identity,
    )
    if current != metadata:
        atomic_replace(
            root=profile.root,
            relative=SOURCE_METADATA_PATH,
            data=metadata,
            expected_root_identity=profile.root_identity,
        )


def register_publication_members(
    connection: sqlite3.Connection, profile: LocalEngineContext
) -> None:
    import base64

    publications = list(
        connection.execute(
            "SELECT publication_id,publication_path FROM captures "
            "WHERE publication_id IS NOT NULL AND stage>=2 "
            "UNION SELECT publication_id,publication_path FROM decisions "
            "WHERE publication_id IS NOT NULL AND stage>=2"
        )
    )
    for row in publications:
        if connection.execute(
            "SELECT 1 FROM canonical_revision_members WHERE publication_id=?",
            (row["publication_id"],),
        ).fetchone():
            continue
        payload = read_confined(
            root=profile.root,
            relative=row["publication_path"],
            expected_root_identity=profile.root_identity,
        )
        if payload is None:
            raise T03Error("operation_pending")
        validate_portable_write(row["publication_path"], payload, profile.tenant_id)
        publication = json.loads(payload)
        revision_id = canonical_revision_id(publication["publication_id"])
        if connection.execute(
            "SELECT 1 FROM canonical_revision_members WHERE revision_id=?", (revision_id,)
        ).fetchone():
            continue
        page = parse_markdown(
            base64.b64decode(publication["published_bytes_base64"], validate=True)
        )
        members = cast(list[str], page.fields["provenance"])
        if any(
            connection.execute(
                "SELECT 1 FROM source_revisions WHERE capture_id=?", (capture_id,)
            ).fetchone()
            is None
            for capture_id in members
        ):
            continue
        for ordinal, capture_id in enumerate(members):
            connection.execute(
                "INSERT INTO canonical_revision_members VALUES(?,?,?,?,?)",
                (
                    revision_id,
                    publication["page_id"],
                    publication["publication_id"],
                    ordinal,
                    capture_id,
                ),
            )
