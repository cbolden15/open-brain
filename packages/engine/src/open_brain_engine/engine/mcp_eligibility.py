"""Transient MCP eligibility over retained engine-owned source lineage."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from open_brain_engine.storage.markdown import parse_markdown

from .capture_lifecycle_policy import lifecycle_permits_capture
from .t03_contracts import EffectiveAuthority, SessionOrigin


def implicit(authority: EffectiveAuthority | None) -> bool:
    return authority is not None and authority.session_origin is SessionOrigin.MCP_IMPLICIT


def lifecycle_capture_visible(
    connection: sqlite3.Connection, capture_id: str, *, history: bool = False
) -> bool:
    """Apply lifecycle barriers to retained dependency closure for every caller."""
    owned_snapshot = not connection.in_transaction
    try:
        if owned_snapshot:
            connection.execute("BEGIN")
        if not lifecycle_permits_capture(connection, capture_id, history=history):
            return False
        if connection.execute("PRAGMA user_version").fetchone()[0] < 18:
            return True
        pending = [capture_id]
        seen: set[str] = set()
        from .developments import development_commitments

        development_commitments(connection, (capture_id,))
        while pending:
            identity = pending.pop()
            if identity in seen:
                continue
            seen.add(identity)
            if len(seen) > 1024 or (
                identity != capture_id
                and not lifecycle_permits_capture(connection, identity, history=history)
            ):
                return False
            for table in ("historical_relations", "sharing_links"):
                pending.extend(
                    row[0]
                    for row in connection.execute(
                        f"SELECT original_capture_id FROM {table} WHERE copy_capture_id=?",
                        (identity,),
                    )
                )
            development = connection.execute(
                "SELECT receipt_json FROM development_evidence WHERE derived_capture_id=?",
                (identity,),
            ).fetchone()
            if development is not None:
                receipt = json.loads(development[0])
                for key in (
                    "supporting_capture_ids",
                    "supports_development_ids",
                    "supersedes_development_ids",
                ):
                    values = receipt.get(key, [])
                    if not isinstance(values, list) or any(
                        not isinstance(item, str) for item in values
                    ):
                        return False
                    pending.extend(values)
        return True
    except ValueError, TypeError, KeyError, AttributeError, sqlite3.Error:
        return False
    finally:
        if owned_snapshot and connection.in_transaction:
            connection.execute("ROLLBACK")


def capture_visible(
    connection: sqlite3.Connection,
    capture_id: str,
    authority: EffectiveAuthority | None,
    *,
    history: bool = False,
) -> bool:
    if not lifecycle_capture_visible(
        connection, capture_id, history=history and authority is not None and authority.owner
    ):
        return False
    if not implicit(authority):
        return True
    pending = [capture_id]
    seen: set[str] = set()
    try:
        from .developments import development_commitments

        # Reuse the frozen dependency validator for cycle and closure-size refusal.
        development_commitments(connection, (capture_id,))
        while pending:
            identity = pending.pop()
            if identity in seen:
                continue
            seen.add(identity)
            if len(seen) > 1024:
                return False
            if not lifecycle_permits_capture(connection, identity):
                return False
            row = connection.execute(
                "SELECT r.source_id,c.privacy_json,c.source_reference, "
                "json_extract(r.source_bytes,'$.privacy') AS retained_privacy, "
                "json_extract(r.source_bytes,'$.source.reference') AS retained_reference "
                "FROM source_revisions r "
                "LEFT JOIN captures c USING(capture_id) WHERE r.capture_id=?",
                (identity,),
            ).fetchone()
            if row is None:
                return False
            privacy = json.loads(row["privacy_json"])
            retained_privacy = json.loads(row["retained_privacy"])
            if (row["retained_reference"] or "").startswith("urn:open-brain:sharing-copy:v2:"):
                return False
            if retained_privacy.get("policy_version") == "sharing-slack-work-copy.v1":
                return False
            if (row["source_reference"] or "").startswith("urn:open-brain:sharing-copy:v2:"):
                return False
            # Independent copy custody signals fail closed even if another signal was damaged.
            if privacy.get("policy_version") == "sharing-slack-work-copy.v1":
                return False
            copies = connection.execute(
                "SELECT json_extract(p.preview_bytes,'$.dto_version') AS dto_version, "
                "json_extract(p.preview_bytes,'$.sharing_kind') AS sharing_kind "
                "FROM sharing_previews p "
                "LEFT JOIN sharing_decisions d USING(preview_id) "
                "LEFT JOIN sharing_links l USING(approval_id) "
                "WHERE d.copy_capture_id=? OR l.copy_capture_id=?",
                (identity, identity),
            )
            for copy in copies:
                if copy["dto_version"] == 2 or copy["sharing_kind"] == "slack_work_evidence":
                    return False
            evidence: list[Any] = [
                item[0]
                for item in connection.execute(
                    "SELECT namespace_json FROM source_namespaces WHERE source_id=?",
                    (row["source_id"],),
                )
            ]
            evidence.extend(
                item[0]
                for item in connection.execute(
                    "SELECT json_extract(envelope_bytes,'$.binding.namespace') "
                    "FROM managed_source_deliveries WHERE source_id=? "
                    "AND (receipt_json IS NULL OR "
                    "json_extract(receipt_json,'$.source_receipt.capture_id')=?) LIMIT 1025",
                    (row["source_id"], identity),
                )
            )
            evidence.extend(
                item[0]
                for item in connection.execute(
                    "SELECT json_extract(envelope_bytes,'$.binding.namespace') "
                    "FROM historical_baselines WHERE source_id=?",
                    (row["source_id"],),
                )
            )
            if len(evidence) > 1024:
                return False
            for raw in evidence:
                value = json.loads(raw)
                namespace = value.get("binding", {}).get("namespace", value)
                if namespace.get("connector_name") == "slack" and (
                    privacy.get("tier") == "work" or retained_privacy.get("tier") == "work"
                ):
                    return False
            historical = connection.execute(
                "SELECT original_capture_id FROM historical_relations WHERE copy_capture_id=?",
                (identity,),
            ).fetchall()
            pending.extend(item[0] for item in historical)
            development = connection.execute(
                "SELECT receipt_json FROM development_evidence WHERE derived_capture_id=?",
                (identity,),
            ).fetchone()
            if development is not None:
                receipt = json.loads(development[0])
                for key in (
                    "supporting_capture_ids",
                    "supports_development_ids",
                    "supersedes_development_ids",
                ):
                    values = receipt.get(key, [])
                    if not isinstance(values, list) or any(
                        not isinstance(item, str) for item in values
                    ):
                        return False
                    pending.extend(values)
        return True
    except ValueError, TypeError, KeyError, AttributeError, sqlite3.Error:
        return False


def record_visible(
    connection: sqlite3.Connection,
    record_type: str,
    record_id: str,
    authority: EffectiveAuthority | None,
    *,
    history: bool = False,
) -> bool:
    if record_type == "source":
        return capture_visible(connection, record_id, authority, history=history)
    if not implicit(authority):
        if history:
            # The projector checks the exact selected retained revision members.
            return True
        if connection.execute("PRAGMA user_version").fetchone()[0] < 18:
            return True
        from .search_projection import canonical_source_rows

        document = connection.execute(
            "SELECT capture_id FROM search_documents WHERE result_id=? AND record_type='canonical'",
            (record_id,),
        ).fetchone()
        if document is None:
            return False
        try:
            return all(
                capture_visible(connection, row["capture_id"], authority)
                for row in canonical_source_rows(
                    connection, result_id=record_id, capture_id=document["capture_id"]
                )
            )
        except ValueError, TypeError, KeyError, sqlite3.Error:
            return False
    # Include retained canonical revisions: graph quotes may refer to an older accepted body.
    members = connection.execute(
        "SELECT DISTINCT capture_id FROM canonical_revision_members WHERE page_id=?", (record_id,)
    ).fetchall()
    return bool(members) and all(
        capture_visible(connection, item[0], authority) for item in members
    )


def graph_visible(
    connection: sqlite3.Connection, note_id: str, authority: EffectiveAuthority | None
) -> bool:
    """Retained managed edits and accepted links cannot discard canonical restrictions."""
    if not implicit(authority) and connection.execute("PRAGMA user_version").fetchone()[0] < 18:
        return True
    pending = [note_id]
    seen: set[str] = set()
    revisions_seen = 0
    try:
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if len(seen) > 1024 or not record_visible(connection, "canonical", current, authority):
                return False
            revisions = connection.execute(
                "SELECT body_bytes FROM managed_note_revisions WHERE note_id=? LIMIT 1025",
                (current,),
            ).fetchall()
            if not revisions:
                return False
            for revision in revisions:
                revisions_seen += 1
                if revisions_seen > 1024:
                    return False
                fields = parse_markdown(bytes(revision[0])).fields
                members = fields.get("provenance")
                if not isinstance(members, list) or not members:
                    return False
                if not all(
                    isinstance(member, str) and capture_visible(connection, member, authority)
                    for member in members
                ):
                    return False
            pending.extend(
                item[0]
                for item in connection.execute(
                    "SELECT target_note_id FROM managed_links WHERE source_note_id=?", (current,)
                )
            )
        return True
    except ValueError, TypeError, KeyError, AttributeError, sqlite3.Error:
        return False


def install(connection: sqlite3.Connection, authority: EffectiveAuthority | None) -> None:
    connection.create_function(
        "mcp_graph_visible",
        1,
        lambda identity: int(
            isinstance(identity, str) and graph_visible(connection, identity, authority)
        ),
    )
    connection.create_function(
        "mcp_capture_visible",
        1,
        lambda identity: int(
            isinstance(identity, str) and capture_visible(connection, identity, authority)
        ),
    )
    connection.create_function(
        "mcp_record_visible",
        2,
        lambda kind, identity: int(
            isinstance(kind, str)
            and isinstance(identity, str)
            and record_visible(connection, kind, identity, authority)
        ),
    )
