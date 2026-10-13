"""Portable facet codec preparation proofs; real E/P/V integration remains separate."""

from __future__ import annotations

import base64
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.portable_v6_evidence import (
    FACET_TABLES,
    serialize_portable_v6_facets,
    verify_portable_v6_facets,
)
from open_brain_engine.engine.portable_v6_restore import install_portable_v6_facets
from open_brain_engine.portable.facets_v6 import (
    FACET_EVENTS_PATH,
    FACET_OPERATIONS_PATH,
    FACET_SIDECAR_PATHS,
    FACET_STATE_PATH,
    FacetPortableReferences,
    PortableFacetBundle,
    decode_portable_facets,
    validate_facet_binding_record,
)
from open_brain_engine.portable.review_binding import review_binding_digest
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.storage.markdown import render_markdown

FIXTURES = Path(__file__).parents[1] / "fixtures/facets"
KIT: dict[str, Any] = json.loads((FIXTURES / "positive/portable-replay.json").read_bytes())
BRAIN_ID: str = KIT["state"]["brain_id"]
PRINCIPAL_ACTORS = {
    operation["principal_id"]: KIT["events"]["events"][0]["actor_id"]
    for operation in KIT["operations"]["operations"]
}


def _files(kit: dict[str, Any] | None = None) -> dict[str, bytes]:
    values = KIT if kit is None else kit
    return {
        path: canonical(values[key])
        for key, path in (
            ("events", FACET_EVENTS_PATH),
            ("operations", FACET_OPERATIONS_PATH),
            ("state", FACET_STATE_PATH),
        )
    }


def _references(**overrides: Any) -> FacetPortableReferences:
    source_id = KIT["state"]["subjects"][0]["subject_id"]
    args = next(
        operation["request_json"]["arguments"]
        for operation in KIT["operations"]["operations"]
        if operation["request_json"]["method"] == "facet.assignment.patch"
    )
    attachment = KIT["state"]["page_queries"][0]
    publication = next(
        operation["request_json"]["arguments"]["publication_id"]
        for operation in KIT["operations"]["operations"]
        if operation["request_json"]["method"] == "facet.query.attach.publication"
    )
    return FacetPortableReferences(
        **{
            "source_ids": frozenset({source_id}),
            "page_ids": frozenset({attachment["page_id"]}),
            "capture_sources": {args["record_id"]: source_id},
            "revision_pages": {},
            "space_ids": frozenset(),
            "publication_ids": frozenset({publication}),
            "principal_actors": PRINCIPAL_ACTORS,
            **overrides,
        }
    )


def _bundle() -> PortableFacetBundle:
    return decode_portable_facets(_files(), brain_id=BRAIN_ID, references=_references())


def _connection(path: Path | str = ":memory:") -> sqlite3.Connection:
    """Normative SQLite fixture only; this is not E's verified schema migration."""
    connection = sqlite3.connect(path)
    connection.executescript("""
    PRAGMA foreign_keys=ON;
    PRAGMA user_version=11;
    CREATE TABLE brain_identity(singleton INTEGER PRIMARY KEY,brain_id TEXT);
    CREATE TABLE logical_sources(source_id TEXT PRIMARY KEY);
    CREATE TABLE source_revisions(capture_id TEXT PRIMARY KEY,source_id TEXT);
    CREATE TABLE canonical_revision_members(revision_id TEXT,page_id TEXT);
    CREATE TABLE search_documents(result_id TEXT,record_type TEXT);
    CREATE TABLE review_page_heads(page_id TEXT);
    CREATE TABLE spaces(space_id TEXT);
    CREATE TABLE captures(publication_id TEXT);
    CREATE TABLE decisions(publication_id TEXT);
    CREATE TABLE facet_dimensions(dimension_id TEXT PRIMARY KEY,name TEXT,name_key TEXT,
        archived INTEGER,version INTEGER,created_at TEXT,updated_at TEXT);
    CREATE TABLE facet_values(value_id TEXT PRIMARY KEY,dimension_id TEXT,name TEXT,name_key TEXT,
        archived INTEGER,version INTEGER,created_at TEXT,updated_at TEXT,
        FOREIGN KEY(dimension_id) REFERENCES facet_dimensions(dimension_id));
    CREATE TABLE facet_subject_versions(subject_kind TEXT,subject_id TEXT,version INTEGER,
        PRIMARY KEY(subject_kind,subject_id));
    CREATE TABLE facet_assignments(subject_kind TEXT,subject_id TEXT,value_id TEXT,present INTEGER,
        membership_version INTEGER,last_event_sequence INTEGER,
        PRIMARY KEY(subject_kind,subject_id,value_id),
        FOREIGN KEY(subject_kind,subject_id)
            REFERENCES facet_subject_versions(subject_kind,subject_id),
        FOREIGN KEY(value_id) REFERENCES facet_values(value_id));
    CREATE TABLE facet_queries(query_id TEXT PRIMARY KEY,name TEXT,name_key TEXT,selector_json TEXT,
        archived INTEGER,version INTEGER,selection_version INTEGER,created_at TEXT,updated_at TEXT);
    CREATE TABLE facet_page_queries(page_id TEXT PRIMARY KEY,query_id TEXT,version INTEGER,
        FOREIGN KEY(query_id) REFERENCES facet_queries(query_id));
    CREATE TABLE facet_events(event_id TEXT PRIMARY KEY,sequence INTEGER UNIQUE,
        operation_id TEXT UNIQUE,actor_id TEXT,recorded_at TEXT,kind TEXT,event_json TEXT);
    CREATE TABLE facet_operations(operation_id TEXT PRIMARY KEY,principal_id TEXT,
        request_sha256 TEXT,request_json TEXT,receipt_json TEXT,event_id TEXT,
        FOREIGN KEY(event_id) REFERENCES facet_events(event_id));
    """)
    refs = _references()
    connection.execute("INSERT INTO brain_identity VALUES(1,?)", (BRAIN_ID,))
    connection.executemany("INSERT INTO logical_sources VALUES(?)", [(s,) for s in refs.source_ids])
    connection.executemany("INSERT INTO source_revisions VALUES(?,?)", refs.capture_sources.items())
    connection.executemany("INSERT INTO review_page_heads VALUES(?)", [(p,) for p in refs.page_ids])
    connection.executemany("INSERT INTO captures VALUES(?)", [(p,) for p in refs.publication_ids])
    connection.commit()
    connection.execute("BEGIN")
    return connection


def test_canonical_sidecars_and_bundle_are_detached() -> None:
    files = _files()
    bundle = decode_portable_facets(files, brain_id=BRAIN_ID, references=_references())
    assert bundle.next_sequence == max(event["sequence"] for event in KIT["events"]["events"]) + 1
    assert set(bundle.sidecars) == FACET_SIDECAR_PATHS
    assert bundle.document(FACET_STATE_PATH) == KIT["state"]
    files[FACET_STATE_PATH] = b"{}"
    detached = bundle.document(FACET_STATE_PATH)
    detached["assignments"].clear()
    assert bundle.document(FACET_STATE_PATH) == KIT["state"]
    with pytest.raises(TypeError):
        bundle.sidecars[FACET_STATE_PATH] = b"{}"  # type: ignore[index]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "unknown",
        "whitespace",
        "duplicate",
        "fractional",
        "brain",
        "state",
        "principal",
        "lineage",
    ],
)
def test_altered_portable_evidence_is_refused(mutation: str) -> None:
    files = _files()
    refs = _references()
    brain_id = BRAIN_ID
    if mutation == "missing":
        del files[FACET_STATE_PATH]
    elif mutation == "unknown":
        files["content/facets/local-preview.json"] = b"{}"
    elif mutation == "whitespace":
        files[FACET_STATE_PATH] += b"\n"
    elif mutation == "duplicate":
        files[FACET_STATE_PATH] = files[FACET_STATE_PATH].replace(
            b'"schema_version":1', b'"schema_version":1,"schema_version":1'
        )
    elif mutation == "fractional":
        files[FACET_EVENTS_PATH] = files[FACET_EVENTS_PATH].replace(
            b'"sequence":1}', b'"sequence":1e0}', 1
        )
    elif mutation == "brain":
        brain_id = "brn_" + "b" * 26
    elif mutation == "state":
        kit = deepcopy(KIT)
        kit["state"]["assignments"][0]["membership_version"] -= 1
        files = _files(kit)
    elif mutation == "principal":
        refs = _references(principal_actors={})
    else:
        refs = _references(capture_sources={})
    with pytest.raises(PortableValidationError):
        decode_portable_facets(files, brain_id=brain_id, references=refs)


def test_restore_commit_reopen_and_reexport_preserve_exact_facets(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite"
    connection = _connection(path)
    bundle = _bundle()
    install_portable_v6_facets(connection, bundle=bundle, principal_actors=PRINCIPAL_ACTORS)
    assert connection.in_transaction
    assert (
        serialize_portable_v6_facets(
            connection, brain_id=BRAIN_ID, principal_actors=PRINCIPAL_ACTORS
        ).sidecars
        == bundle.sidecars
    )
    assert connection.execute(
        "SELECT membership_version FROM facet_assignments ORDER BY value_id"
    ).fetchall() == [(3,), (1,)]
    with pytest.raises(PortableValidationError, match="empty destination"):
        install_portable_v6_facets(connection, bundle=bundle, principal_actors=PRINCIPAL_ACTORS)
    connection.commit()
    connection.close()
    with sqlite3.connect(path) as reopened:
        reopened.execute("BEGIN")
        actual = verify_portable_v6_facets(
            reopened, bundle=bundle, principal_actors=PRINCIPAL_ACTORS
        )
        assert actual.evidence_sha256 == bundle.evidence_sha256
        assert actual.operations_sha256 == bundle.operations_sha256


def test_restore_fault_rolls_back_only_its_savepoint() -> None:
    connection = _connection()
    connection.execute(
        "CREATE TRIGGER fixture_fault BEFORE INSERT ON facet_operations "
        "BEGIN SELECT RAISE(ABORT,'synthetic late fault'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic late fault"):
        install_portable_v6_facets(connection, bundle=_bundle(), principal_actors=PRINCIPAL_ACTORS)
    assert connection.in_transaction
    assert all(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        for table in FACET_TABLES
    )
    assert connection.execute("SELECT count(*) FROM logical_sources").fetchone()[0] == 1
    connection.execute("DROP TRIGGER fixture_fault")
    install_portable_v6_facets(connection, bundle=_bundle(), principal_actors=PRINCIPAL_ACTORS)


@pytest.mark.parametrize("mutation", ["transaction", "schema", "brain", "commitment"])
def test_destination_boundary_and_constructed_bundle_are_revalidated(mutation: str) -> None:
    connection = _connection()
    bundle = _bundle()
    if mutation == "transaction":
        connection.commit()
    elif mutation == "schema":
        connection.execute("PRAGMA user_version=10")
    elif mutation == "brain":
        connection.execute("UPDATE brain_identity SET brain_id='wrong'")
    else:
        bundle = replace(bundle, state_sha256="0" * 64)
    with pytest.raises(PortableValidationError):
        install_portable_v6_facets(connection, bundle=bundle, principal_actors=PRINCIPAL_ACTORS)


@pytest.mark.parametrize("mutation", ["name_key", "event_columns", "receipt", "projection"])
def test_export_detects_store_drift(mutation: str) -> None:
    connection = _connection()
    bundle = _bundle()
    install_portable_v6_facets(connection, bundle=bundle, principal_actors=PRINCIPAL_ACTORS)
    sql = {
        "name_key": "UPDATE facet_dimensions SET name_key='drift'",
        "event_columns": "UPDATE facet_events SET kind='query.archive' WHERE sequence=1",
        "receipt": "UPDATE facet_operations SET receipt_json='{}' WHERE event_id IS NULL",
        "projection": "UPDATE facet_assignments SET present=0",
    }
    connection.execute(sql[mutation])
    with pytest.raises(PortableValidationError):
        serialize_portable_v6_facets(
            connection, brain_id=BRAIN_ID, principal_actors=PRINCIPAL_ACTORS
        )


def test_facet_binding_refuses_wrong_digest_before_touching_routes() -> None:
    binding = json.loads((FIXTURES / "positive/review-create-cross-space.json").read_bytes())
    proposal = {
        key: binding[key]
        for key in ("proposal_id", "actor_id", "role_claim", "recorded_at", "tenant_id")
    }
    with pytest.raises(PortableValidationError, match="digest mismatch"):
        validate_facet_binding_record(
            binding,
            proposal=proposal,
            files={},
            references=_references(),
            tenant_id=binding["tenant_id"],
        )


def test_pending_binding_preserves_cross_space_routes_and_independent_destination() -> None:
    binding = json.loads((FIXTURES / "positive/review-create-cross-space.json").read_bytes())
    files: dict[str, bytes] = {}
    for state in binding["source_states"]:
        capture_id = state["capture_id"]
        capture = {"capture_id": capture_id, "space_id": state["space_id"]}
        path = f"sources/captures/2026/10/{capture_id}.json"
        files[path] = canonical(capture)
        state["sha256"] = sha256(files[path]).hexdigest()
        route = {
            "route_id": state["route_id"],
            "capture_id": capture_id,
            "space_id": state["space_id"],
            "recorded_at": "2026-10-13T11:00:00Z",
            "supersedes": None,
        }
        files[f"history/routes/2026/10/{state['route_id']}.json"] = canonical(route)
    privacy = {
        "tier": "work",
        "policy_version": "1",
        "reason": "Synthetic fixture",
        "confirmation_ref": None,
        "authority": {"cloud": False, "external_egress": False},
    }
    fields = {key: binding[key] for key in ("page_id", "tenant_id", "actor_id", "role_claim")}
    fields.update(
        {
            "provenance": binding["provenance"],
            "space_id": binding["destination_space_id"],
            "privacy": privacy,
            "modified_at": binding["recorded_at"],
            "status": "active",
            "trust": "reviewed",
        }
    )
    markdown = render_markdown(fields=fields, body="# Synthetic facet page\n").encode()
    proposal = {
        key: binding[key]
        for key in ("proposal_id", "actor_id", "role_claim", "recorded_at", "tenant_id")
    }
    proposal.update(
        {
            "capture_ids": binding["provenance"],
            "privacy": privacy,
            "space_id": binding["destination_space_id"],
            "evidence": [{"capture_id": capture_id} for capture_id in binding["provenance"]],
            "proposed_content": {
                "bytes_base64": base64.b64encode(markdown).decode(),
                "sha256": sha256(markdown).hexdigest(),
            },
        }
    )
    binding["review_digest"] = review_binding_digest(proposal, binding)
    refs = _references(
        space_ids=frozenset({binding["destination_space_id"]}),
        capture_sources={
            state["capture_id"]: state["source_id"] for state in binding["facet_source_states"]
        },
    )
    actual = validate_facet_binding_record(
        binding, proposal=proposal, files=files, references=refs, tenant_id=binding["tenant_id"]
    )
    assert actual == binding
    assert actual["source_states"][0]["space_id"] is None
    assert actual["source_states"][1]["space_id"] != actual["destination_space_id"]
    # A changed exact draft or changed frozen route must still fail validation.
    bad_proposal = {**proposal, "space_id": "space_00000000-0000-4000-8000-000000000090"}
    with pytest.raises(PortableValidationError):
        validate_facet_binding_record(
            binding,
            proposal=bad_proposal,
            files=files,
            references=refs,
            tenant_id=binding["tenant_id"],
        )
    bad_files = dict(files)
    route_path = next(path for path in files if path.startswith("history/routes/"))
    route = json.loads(files[route_path])
    route["space_id"] = "space_00000000-0000-4000-8000-000000000090"
    bad_files[route_path] = canonical(route)
    with pytest.raises(PortableValidationError, match="source state"):
        validate_facet_binding_record(
            binding,
            proposal=proposal,
            files=bad_files,
            references=refs,
            tenant_id=binding["tenant_id"],
        )


def _empty_facet_archive() -> dict[str, bytes]:
    from open_brain_engine.portable.v5 import ISSUER_MIGRATION_PATH

    from packages.engine.tests.contract.test_portable_brain_v5 import _fixture

    files = _fixture()
    brain_id = json.loads(files[ISSUER_MIGRATION_PATH])["brain_id"]
    files[FACET_EVENTS_PATH] = canonical({"schema_version": 1, "brain_id": brain_id, "events": []})
    files[FACET_OPERATIONS_PATH] = canonical(
        {"schema_version": 1, "brain_id": brain_id, "operations": []}
    )
    files[FACET_STATE_PATH] = canonical(
        {
            "schema_version": 1,
            "brain_id": brain_id,
            "dimensions": [],
            "values": [],
            "subjects": [],
            "assignments": [],
            "queries": [],
            "page_queries": [],
        }
    )
    return files


def _write_v6(root: Path, files: dict[str, bytes]) -> None:
    from open_brain_engine.portable.v6 import manifest_v6

    from packages.engine.tests.contract.test_portable_brain_v5 import EXPORT, NOW, TENANT

    for path, payload in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (root / "portable-manifest.json").write_bytes(
        canonical(manifest_v6(files, tenant_id=TENANT, export_id=EXPORT, created_at=NOW))
    )


def test_v6_snapshot_validates_inherited_lossless_evidence_and_pins_bytes(tmp_path: Path) -> None:
    from open_brain_engine.engine.portable_v6_restore import V6RestoreBundle
    from open_brain_engine.portable.v6 import validated_portable_snapshot_v6

    files = _empty_facet_archive()
    _write_v6(tmp_path, files)
    snapshot = validated_portable_snapshot_v6(tmp_path)
    assert snapshot.manifest["schema_version"] == 6
    bundle = V6RestoreBundle.decode(snapshot, checkpoint=lambda _stage: None)
    assert bundle.snapshot is snapshot
    assert bundle.facets.next_sequence == 1
    assert all(snapshot.files[path] == files[path] for path in files)
    (tmp_path / FACET_STATE_PATH).write_bytes(b"{}")
    assert V6RestoreBundle.decode(snapshot, checkpoint=lambda _stage: None).facets == bundle.facets
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v6(tmp_path)


@pytest.mark.parametrize("mutation", ["missing", "privacy", "catalog", "newer", "duplicate"])
def test_full_v6_snapshot_refuses_inherited_and_manifest_drift(
    tmp_path: Path, mutation: str
) -> None:
    from open_brain_engine.portable.v5 import EFFECTIVE_PRIVACY_PATH
    from open_brain_engine.portable.v6 import validated_portable_snapshot_v6

    files = _empty_facet_archive()
    if mutation == "missing":
        del files[FACET_OPERATIONS_PATH]
    elif mutation == "privacy":
        privacy = json.loads(files[EFFECTIVE_PRIVACY_PATH])
        privacy["resolved_search"][0]["effective_privacy"]["authority"]["cloud"] = True
        files[EFFECTIVE_PRIVACY_PATH] = canonical(privacy)
    _write_v6(tmp_path, files)
    manifest_path = tmp_path / "portable-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if mutation == "catalog":
        manifest["schema_catalog_digest"] = "0" * 64
    elif mutation == "newer":
        manifest["schema_version"] = 7
    elif mutation == "duplicate":
        manifest["files"].append(manifest["files"][0])
    manifest_path.write_bytes(canonical(manifest))
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v6(tmp_path)
