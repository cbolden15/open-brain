"""Read-only durable preflight for immutable source-history migration."""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.managed_v2 import validate_portable_file_set_v2
from open_brain_engine.portable.v1 import validate_portable_file_set
from open_brain_engine.portable.v3 import validate_portable_file_set_v3
from open_brain_engine.portable.v4 import (
    SOURCE_METADATA_PATH,
    canonical_revision_id,
    validate_portable_file_set_v4,
)
from open_brain_engine.storage.filesystem import capture_root_identity, read_confined
from open_brain_engine.storage.markdown import parse_markdown

from .contracts import LocalEngineContext
from .portability_ports import LocalTenantStorage
from .t03_contracts import T03Error


@dataclass(frozen=True, slots=True)
class DurableSourceInventory:
    """Exact immutable bytes and proven SQL identities, held inside exclusive admission."""

    files: dict[str, bytes]
    captures: dict[str, tuple[str, bytes, dict[str, Any]]]
    current_rows: dict[str, dict[str, Any]]
    aliases: dict[str, str]
    memberships: tuple[tuple[str, str, str, int, str], ...]
    publication_paths: dict[str, str]

    def evidence(self) -> list[dict[str, object]]:
        return [
            {"path": path, "bytes": len(payload), "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(self.files.items())
        ]


def inventory_sources(
    profile: LocalEngineContext, connection: sqlite3.Connection
) -> DurableSourceInventory:
    """Fail before mutation on missing, contradictory or unsafe durable evidence.

    A delivery alias proves its SQL capture only. Equal text, references and acceptance
    timestamps never establish logical-source grouping or provider chronology.
    """
    try:
        files = dict(
            LocalTenantStorage(
                profile.root,
                profile.tenant_id,
                profile.root_identity,
            ).portable_files()
        )
        portable = {
            path: payload
            for path, payload in files.items()
            if (path == "brain.toml" or path.startswith(("content/", "history/", "sources/")))
        }
        validator = (
            validate_portable_file_set_v4
            if SOURCE_METADATA_PATH in portable
            else validate_portable_file_set_v3
            if any(path.startswith("history/review-bindings/") for path in portable)
            else validate_portable_file_set_v2
            if any(path.startswith("history/managed-workspace/") for path in portable)
            else validate_portable_file_set
        )
        validator(portable, tenant_id=profile.tenant_id)
        captures: dict[str, tuple[str, bytes, dict[str, Any]]] = {}
        for path, payload in portable.items():
            if not path.startswith("sources/captures/"):
                continue
            record = json.loads(payload)
            capture_id = record["capture_id"]
            if capture_id in captures:
                raise ValueError("duplicate capture")
            captures[capture_id] = (path, payload, record)
        current: dict[str, dict[str, Any]] = {}
        aliases: dict[str, str] = {}
        for row in connection.execute("SELECT * FROM captures ORDER BY capture_id"):
            capture_id = str(row["capture_id"])
            if capture_id not in captures or row["stage"] < 3:
                raise ValueError("capture requires recovery")
            path, _, record = captures[capture_id]
            if (
                row["source_path"] != path
                or bytes(row["payload_json"]) != portable_canonical_json_bytes(record["payload"])
                or row["source_reference"] != record["source"]["reference"]
                or row["source_origin"] != record["source"]["origin"]
                or row["accepted_at"] != record["accepted_at"]
                or row["actor_id"] != record["actor_id"]
            ):
                raise ValueError("capture evidence conflicts")
            for column, field in (
                ("privacy_json", "privacy"),
                ("provenance_json", "provenance"),
                ("role_claim_json", "role_claim"),
            ):
                stored = json.loads(row[column])
                if column == "provenance_json":
                    stored = dict(stored)
                    stored.setdefault("transformation_receipts", [])
                if column == "provenance_json" and row["submission_path"] == "owner":
                    expected_context = (
                        "owner_authored" if row["capture_why"] is not None else "automation_absent"
                    )
                    if stored.get("owner_context") != expected_context:
                        raise ValueError("capture authority evidence conflicts")
                    durable_context = (
                        "automation_absent"
                        if row["source_origin"] == "third_party"
                        else "owner_authored"
                    )
                    if record[field]["owner_context"] != durable_context:
                        raise ValueError("capture authority evidence conflicts")
                    stored = dict(stored, owner_context=durable_context)
                if stored != record[field]:
                    raise ValueError("capture authority evidence conflicts")
            if row["intent"] != record["intent"] or row["capture_why"] != record["capture_why"]:
                raise ValueError("capture annotation evidence conflicts")
            current[capture_id] = dict(row)
            aliases[str(row["delivery_id"])] = capture_id
        for row in connection.execute("SELECT capture_id FROM search_documents"):
            if row[0] not in current:
                raise ValueError("search capture evidence missing")
        for table in ("review_sources", "route_operations", "proposals"):
            for row in connection.execute(f"SELECT capture_id FROM {table}"):
                if row[0] not in captures:
                    raise ValueError("referenced capture evidence missing")
        bindings = {
            record["proposal_id"]: record
            for path, payload in portable.items()
            if path.startswith("history/review-bindings/")
            for record in [json.loads(payload)]
        }
        sql_proposals = {
            row[0] for row in connection.execute("SELECT proposal_id FROM review_contexts")
        }
        if sql_proposals != set(bindings):
            raise ValueError("review binding evidence conflicts")
        for proposal_id, binding in bindings.items():
            sql_members = list(
                connection.execute(
                    "SELECT ordinal,capture_id FROM review_sources "
                    "WHERE proposal_id=? ORDER BY ordinal",
                    (proposal_id,),
                )
            )
            if [tuple(row) for row in sql_members] != list(enumerate(binding["provenance"])):
                raise ValueError("review membership evidence conflicts")
        # The public source record is immutable. Routes may legitimately differ in SQL.
        memberships: list[tuple[str, str, str, int, str]] = []
        publication_paths: dict[str, str] = {}
        for path, payload in sorted(portable.items()):
            if not path.startswith("history/publications/"):
                continue
            publication = json.loads(payload)
            decision_id = publication["decision_id"]
            decision = connection.execute(
                "SELECT * FROM decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if decision is None or decision["publication_id"] != publication["publication_id"]:
                raise ValueError("publication SQL identity conflicts")
            if decision["publication_path"] != path:
                legacy_key = "import.decision." + sha256(decision_id.encode()).hexdigest()
                if (
                    decision["delivery_id"] != legacy_key
                    or decision["publication_path"] != publication["published_path"]
                ):
                    raise ValueError("publication SQL path conflicts")
                publication_paths[decision_id] = path
            page = parse_markdown(
                base64.b64decode(publication["published_bytes_base64"], validate=True)
            )
            members = page.fields["provenance"]
            if not isinstance(members, list) or not 1 <= len(members) <= 32:
                raise ValueError("publication members missing")
            for ordinal, capture_id in enumerate(members):
                if capture_id not in captures:
                    raise ValueError("publication capture missing")
                memberships.append(
                    (
                        canonical_revision_id(publication["publication_id"]),
                        publication["page_id"],
                        publication["publication_id"],
                        ordinal,
                        capture_id,
                    )
                )
        publication_members: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for _, page_id, publication_id, ordinal, capture_id in memberships:
            publication_members.setdefault((page_id, publication_id), []).append(
                (ordinal, capture_id)
            )
        for head in connection.execute("SELECT * FROM review_page_heads"):
            expected = list(enumerate(bindings[head["proposal_id"]]["provenance"]))
            if publication_members.get((head["page_id"], head["publication_id"])) != expected:
                raise ValueError("publication membership evidence conflicts")
            if expected[0][1] != head["capture_id"]:
                raise ValueError("publication representative evidence conflicts")
        return DurableSourceInventory(
            files, captures, current, aliases, tuple(memberships), publication_paths
        )
    except ValueError, TypeError, KeyError, sqlite3.Error, OSError:
        raise T03Error("operation_pending") from None


def inventory_private_state(
    connection: sqlite3.Connection, *, publication_paths: dict[str, str] | None = None
) -> dict[str, object]:
    """Hash all historical SQL, validating private managed bytes and root bindings.

    These operational receipts remain private. They are never source identity evidence
    and are not serialized into Portable metadata.
    """
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise T03Error("operation_pending")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise T03Error("operation_pending")
    tables: dict[str, object] = {}
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        table = str(row[0])
        if not table.replace("_", "").isalnum():
            raise T03Error("operation_pending")
        if table in {"schema_migrations", "runtime_compatibility"}:
            continue
        encoded_rows = []
        for values in connection.execute(f'SELECT * FROM "{table}"'):
            projected = list(values)
            if table == "decisions" and publication_paths:
                replacement = publication_paths.get(values["decision_id"])
                if replacement is not None:
                    projected[values.keys().index("publication_path")] = replacement
            encoded_rows.append(
                portable_canonical_json_bytes(
                    [
                        {"blob": base64.b64encode(value).decode()}
                        if isinstance(value, bytes)
                        else value
                        for value in projected
                    ]
                )
            )
        digest = sha256(b"\n".join(sorted(encoded_rows))).hexdigest()
        tables[table] = {"rows": len(encoded_rows), "sha256": digest}
    for row in connection.execute("SELECT body_bytes,body_sha256 FROM managed_note_revisions"):
        if sha256(row[0]).hexdigest() != row[1]:
            raise T03Error("operation_pending")
    for row in connection.execute(
        "SELECT candidate_body_bytes,candidate_sha256 FROM managed_conflicts"
    ):
        if sha256(row[0]).hexdigest() != row[1]:
            raise T03Error("operation_pending")
    managed_files = []
    for workspace in connection.execute("SELECT * FROM managed_workspaces"):
        if workspace["root_path"] is None:
            continue
        root = Path(workspace["root_path"])
        identity = (int(workspace["device"]), int(workspace["inode"]))
        if capture_root_identity(root) != identity:
            raise T03Error("operation_pending")
        for row in connection.execute(
            "SELECT note_id,relative_path FROM managed_notes WHERE workspace_id=? AND active=1",
            (workspace["workspace_id"],),
        ):
            if row["relative_path"] is None:
                continue
            payload = read_confined(
                root=root, relative=row["relative_path"], expected_root_identity=identity
            )
            if payload is None:
                raise T03Error("operation_pending")
            managed_files.append(
                {
                    "note_id": row["note_id"],
                    "bytes": len(payload),
                    "sha256": sha256(payload).hexdigest(),
                }
            )
    return {"tables": tables, "managed_files": managed_files}
