"""Install validated facet evidence into an empty hidden schema-11 stage.

The caller owns writer fencing, stage lifetime, base evidence restoration,
search regeneration, reopen verification and promotion. This installer uses a
savepoint inside the caller transaction; it never commits or copies live SQLite.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.facets_v6 import (
    FACET_EVENTS_PATH,
    FACET_OPERATIONS_PATH,
    FACET_STATE_PATH,
    PortableFacetBundle,
    decode_portable_facets,
    references_from_portable_files,
)
from open_brain_engine.portable.v1 import PortableSnapshot, PortableValidationError
from open_brain_engine.storage.filesystem import RootIdentity

from .contracts import LocalEngineContext
from .portable_v5_restore import V5RestoreBundle, ValidatedV5IssuerSeed, audit_restored_v5
from .portable_v6_evidence import (
    FACET_TABLES,
    facet_references_from_connection,
    require_facet_snapshot,
    verify_portable_v6_facets,
)

if TYPE_CHECKING:
    from .materializer import Materialization


def install_portable_v6_facets(
    connection: sqlite3.Connection,
    *,
    bundle: PortableFacetBundle,
    principal_actors: Mapping[str, str] | None = None,
    owner_actor_id: str | None = None,
) -> None:
    """Revalidate references, install exact ledger bytes, and audit the projection."""
    require_facet_snapshot(connection, brain_id=bundle.brain_id)
    if any(
        connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in FACET_TABLES
    ):
        raise PortableValidationError("facet restore requires empty destination tables")
    if principal_actors is None:
        principal_actors = {
            operation["principal_id"]: (
                owner_actor_id if owner_actor_id is not None else operation["principal_id"]
            )
            for operation in bundle.document(FACET_OPERATIONS_PATH)["operations"]
        }
    references = facet_references_from_connection(
        connection,
        principal_actors=principal_actors,
        owner_actor_id=owner_actor_id,
    )
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


@dataclass(frozen=True, slots=True)
class V6RestoreBundle(V5RestoreBundle):
    """Actual v6 snapshot with the unchanged inherited lossless restore machinery."""

    facets: PortableFacetBundle

    @classmethod
    def decode(
        cls,
        snapshot: PortableSnapshot,
        *,
        checkpoint: Callable[[str], None],
    ) -> V6RestoreBundle:
        from open_brain_engine.portable.v5 import (
            EFFECTIVE_PRIVACY_PATH,
            ISSUER_MIGRATION_PATH,
            LEGACY_BINDINGS_PATH,
            decode_retained_privacy_value,
        )
        from open_brain_engine.portable.v6 import validate_portable_file_set_v6

        manifest = snapshot.manifest
        if (
            type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 6
            or manifest.get("layout_version") != 6
            or manifest.get("contract_version") != "6"
        ):
            raise PortableValidationError("v6 restore requires a validated v6 snapshot")
        from open_brain_engine.portable.v5 import _manifest
        from open_brain_engine.portable.v6_catalog import PORTABLE_V6_SCHEMA_CATALOG_DIGEST

        payloads = {
            path: data for path, data in snapshot.files.items() if path != "portable-manifest.json"
        }
        inventory = _manifest(manifest, version=6, catalog=PORTABLE_V6_SCHEMA_CATALOG_DIGEST)
        if canonical(manifest) != snapshot.files.get("portable-manifest.json") or inventory != {
            path: sha256(data).hexdigest() for path, data in payloads.items()
        }:
            raise PortableValidationError("v6 restore snapshot commitment mismatch")
        validate_portable_file_set_v6(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        issuer = json.loads(snapshot.files[ISSUER_MIGRATION_PATH])
        for row in json.loads(snapshot.files[EFFECTIVE_PRIVACY_PATH])["retained_privacy"]:
            retained = decode_retained_privacy_value(row["privacy_json"])
            if type(retained) in (int, float):
                raise PortableValidationError("v6 numeric retained privacy is not restorable")
        facets = decode_portable_facets(
            payloads,
            brain_id=issuer["brain_id"],
            references=references_from_portable_files(payloads),
        )
        return cls(
            snapshot,
            ValidatedV5IssuerSeed(
                snapshot.files[ISSUER_MIGRATION_PATH], snapshot.files[LEGACY_BINDINGS_PATH]
            ),
            checkpoint,
            facets,
        )

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        # Preserve the real v6 snapshot; only the inherited evidence algorithm is reused.
        V5RestoreBundle.install(self, connection, profile=profile)
        install_portable_v6_facets(
            connection,
            bundle=self.facets,
            owner_actor_id=profile.owner_actor_id,
        )
        self.checkpoint("facets_restored")
        verify_portable_v6_facets(
            connection,
            bundle=self.facets,
            owner_actor_id=profile.owner_actor_id,
        )
        self.checkpoint("facet_semantics_verified")


def audit_restored_v6(
    profile: LocalEngineContext,
    *,
    snapshot: PortableSnapshot,
) -> PortableFacetBundle:
    """Audit inherited content/privacy and facets through fresh read connections."""
    from .local_schema import open_local_database_read_only

    bundle = V6RestoreBundle.decode(snapshot, checkpoint=lambda _stage: None)
    audit_restored_v5(profile, snapshot=snapshot)
    connection = open_local_database_read_only(profile)
    try:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        return verify_portable_v6_facets(
            connection,
            bundle=bundle.facets,
            owner_actor_id=profile.owner_actor_id,
        )
    finally:
        connection.close()


def restore_portable_v6_root(
    root: Path,
    *,
    snapshot: PortableSnapshot,
    expected_root_identity: RootIdentity,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> Materialization:
    """Materialize a copied v6 archive into the caller's hidden current-schema stage.

    The outer portability operation retains its writer fence and promotes only
    after this function returns, index rebuild and ready-record verification.
    """
    from .materializer import materialize_portable_root

    bundle = V6RestoreBundle.decode(snapshot, checkpoint=checkpoint)
    materialization = materialize_portable_root(
        root,
        snapshot=snapshot,
        expected_root_identity=expected_root_identity,
        _v5_restore=bundle,
    )
    audit_restored_v6(materialization.profile, snapshot=snapshot)
    checkpoint("facet_semantics_reopened")
    return materialization


__all__ = [
    "V6RestoreBundle",
    "audit_restored_v6",
    "install_portable_v6_facets",
    "restore_portable_v6_root",
]
