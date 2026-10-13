"""Install validated facet evidence into an empty hidden schema-11 stage.

The caller owns writer fencing, stage lifetime, base evidence restoration,
search regeneration, reopen verification and promotion. This installer uses a
savepoint inside the caller transaction; it never commits or copies live SQLite.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Mapping

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.facets_v6 import (
    FACET_EVENTS_PATH,
    FACET_OPERATIONS_PATH,
    FACET_STATE_PATH,
    PortableFacetBundle,
    decode_portable_facets,
)
from open_brain_engine.portable.v1 import PortableValidationError

from .portable_v6_evidence import (
    FACET_TABLES,
    facet_references_from_connection,
    require_facet_snapshot,
    verify_portable_v6_facets,
)


def install_portable_v6_facets(
    connection: sqlite3.Connection,
    *,
    bundle: PortableFacetBundle,
    principal_actors: Mapping[str, str] | None = None,
) -> None:
    """Revalidate references, install exact ledger bytes, and audit the projection."""
    require_facet_snapshot(connection, brain_id=bundle.brain_id)
    if any(
        connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in FACET_TABLES
    ):
        raise PortableValidationError("facet restore requires empty destination tables")
    if principal_actors is None:
        principal_actors = {
            operation["principal_id"]: operation["principal_id"]
            for operation in bundle.document(FACET_OPERATIONS_PATH)["operations"]
        }
    references = facet_references_from_connection(connection, principal_actors=principal_actors)
    # Do not trust even an explicitly constructed PortableFacetBundle without revalidation.
    validated = decode_portable_facets(
        bundle.sidecars, brain_id=bundle.brain_id, references=references
    )
    if validated != bundle:
        raise PortableValidationError("facet restore bundle commitment is invalid")
    connection.execute("SAVEPOINT portable_v6_facets")
    try:
        state = bundle.document(FACET_STATE_PATH)
        for row in state["dimensions"]:
            connection.execute(
                "INSERT INTO facet_dimensions "
                "(dimension_id,name,name_key,archived,version,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    row["dimension_id"],
                    row["name"],
                    unicodedata.normalize("NFC", row["name"].casefold()),
                    int(row["archived"]),
                    row["version"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        for row in state["values"]:
            connection.execute(
                "INSERT INTO facet_values "
                "(value_id,dimension_id,name,name_key,archived,version,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["value_id"],
                    row["dimension_id"],
                    row["name"],
                    unicodedata.normalize("NFC", row["name"].casefold()),
                    int(row["archived"]),
                    row["version"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        for row in state["subjects"]:
            connection.execute(
                "INSERT INTO facet_subject_versions "
                "(subject_kind,subject_id,version) VALUES (?,?,?)",
                (row["subject_kind"], row["subject_id"], row["version"]),
            )
        for row in state["queries"]:
            connection.execute(
                "INSERT INTO facet_queries "
                "(query_id,name,name_key,selector_json,archived,version,selection_version,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    row["query_id"],
                    row["name"],
                    unicodedata.normalize("NFC", row["name"].casefold()),
                    canonical(row["selector"]).decode("utf-8"),
                    int(row["archived"]),
                    row["version"],
                    row["selection_version"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        for event in bundle.document(FACET_EVENTS_PATH)["events"]:
            connection.execute(
                "INSERT INTO facet_events "
                "(event_id,sequence,operation_id,actor_id,recorded_at,kind,event_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    event["event_id"],
                    event["sequence"],
                    event["operation_id"],
                    event["actor_id"],
                    event["recorded_at"],
                    event["kind"],
                    canonical(event).decode("utf-8"),
                ),
            )
        for row in state["assignments"]:
            connection.execute(
                "INSERT INTO facet_assignments "
                "(subject_kind,subject_id,value_id,present,membership_version,last_event_sequence) "
                "VALUES (?,?,?,?,?,?)",
                (
                    row["subject_kind"],
                    row["subject_id"],
                    row["value_id"],
                    int(row["present"]),
                    row["membership_version"],
                    row["last_event_sequence"],
                ),
            )
        for row in state["page_queries"]:
            connection.execute(
                "INSERT INTO facet_page_queries (page_id,query_id,version) VALUES (?,?,?)",
                (row["page_id"], row["query_id"], row["version"]),
            )
        for operation in bundle.document(FACET_OPERATIONS_PATH)["operations"]:
            connection.execute(
                "INSERT INTO facet_operations "
                "(operation_id,principal_id,request_sha256,request_json,receipt_json,event_id) "
                "VALUES (?,?,?,?,?,?)",
                (
                    operation["operation_id"],
                    operation["principal_id"],
                    operation["request_sha256"],
                    canonical(operation["request_json"]).decode("utf-8"),
                    canonical(operation["receipt_json"]).decode("utf-8"),
                    operation["event_id"],
                ),
            )
        verify_portable_v6_facets(connection, bundle=bundle, principal_actors=principal_actors)
        connection.execute("RELEASE SAVEPOINT portable_v6_facets")
    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT portable_v6_facets")
        connection.execute("RELEASE SAVEPOINT portable_v6_facets")
        raise


__all__ = ["install_portable_v6_facets"]
