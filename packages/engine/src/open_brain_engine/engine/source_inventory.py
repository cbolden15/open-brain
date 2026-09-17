"""Read-only durable preflight for immutable source-history migration."""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.v1 import validate_portable_file_set
from open_brain_engine.portable.v3 import validate_portable_file_set_v3
from open_brain_engine.storage.markdown import parse_markdown

from .contracts import LocalEngineContext
from .portability_ports import LocalTenantStorage
from .t03_contracts import T03Error


def canonical_revision_id(publication_id: str) -> str:
    return "revision_" + str(uuid5(NAMESPACE_URL, "open-brain-publication:" + publication_id))


@dataclass(frozen=True, slots=True)
class DurableSourceInventory:
    """Exact immutable bytes and proven SQL identities, held inside exclusive admission."""

    files: dict[str, bytes]
    captures: dict[str, tuple[str, bytes, dict[str, Any]]]
    current_rows: dict[str, dict[str, Any]]
    aliases: dict[str, str]
    memberships: tuple[tuple[str, str, str, int, str], ...]

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
            and not path.startswith("history/managed-workspace/")
        }
        validator = (
            validate_portable_file_set_v3
            if any(path.startswith("history/review-bindings/") for path in portable)
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
            current[capture_id] = dict(row)
            aliases[str(row["delivery_id"])] = capture_id
        for row in connection.execute("SELECT capture_id FROM search_documents"):
            if row[0] not in current:
                raise ValueError("search capture evidence missing")
        for table in ("review_sources", "route_operations", "proposals"):
            for row in connection.execute(f"SELECT capture_id FROM {table}"):
                if row[0] not in captures:
                    raise ValueError("referenced capture evidence missing")
        # The public source record is immutable. Routes may legitimately differ in SQL.
        memberships: list[tuple[str, str, str, int, str]] = []
        for path, payload in sorted(portable.items()):
            if not path.startswith("history/publications/"):
                continue
            publication = json.loads(payload)
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
        return DurableSourceInventory(files, captures, current, aliases, tuple(memberships))
    except ValueError, TypeError, KeyError, sqlite3.Error, OSError:
        raise T03Error("operation_pending") from None
