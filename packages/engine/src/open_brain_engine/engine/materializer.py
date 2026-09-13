"""Fresh local operational materialization from validated Portable Brain bytes."""

from __future__ import annotations

import base64
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.v1 import PortableSnapshot
from open_brain_engine.providers.base import ProviderMode
from open_brain_engine.storage.filesystem import RootIdentity
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown

from .contracts import LocalEngineContext
from .local_store import _LocalStore
from .search_projection import source_search_title, upsert_search_document


@dataclass(frozen=True, slots=True)
class Materialization:
    profile: LocalEngineContext
    captures: int
    batches: int
    blobs: int
    history_records: int


def _json_records(
    files: Mapping[str, bytes], relative: str
) -> list[tuple[str, dict[str, object]]]:
    prefix = relative + "/"
    return [
        (path, cast(dict[str, object], json.loads(payload)))
        for path, payload in sorted(files.items())
        if path.startswith(prefix) and path.endswith(".json")
    ]


def _profile(root: Path, snapshot: PortableSnapshot) -> LocalEngineContext:
    value = tomllib.loads(snapshot.files["brain.toml"].decode("utf-8"))
    tenant_id = cast(str, value["tenant_id"])
    owner_actor_id = cast(str, value["owner_actor_id"])
    role_claim = {
        "actor_id": owner_actor_id,
        "capabilities": tuple(cast(list[str], value["owner_capabilities"])),
        "role_claim_id": cast(str, value["owner_role_claim_id"]),
        "role_id": cast(str, value["owner_role_id"]),
        "tenant_id": tenant_id,
    }
    return LocalEngineContext(
        root=root,
        root_identity=snapshot.root_identity,
        tenant_id=tenant_id,
        owner_actor_id=owner_actor_id,
        owner_role_claim=role_claim,
        provider_mode=ProviderMode.NONE,
        starter_spaces=(),
    )


def _derived_key(prefix: str, value: str) -> str:
    return f"{prefix}.{sha256(value.encode('utf-8')).hexdigest()}"


def _payload_search_text(payload: Mapping[str, object]) -> str:
    family = cast(str, payload["family"])
    if family == "text":
        return cast(str, payload["text"])
    if family == "reference_or_file":
        return " ".join(
            str(payload[key])
            for key in ("url", "file_name", "media_type", "supplied_text")
            if key in payload
        )
    if family == "event":
        attributes = cast(list[Mapping[str, object]], payload["attributes"])
        return " ".join(
            [cast(str, payload["event_type"]), str(payload["occurrence_at"] or "")]
            + [f"{item['name']} {item['value']}" for item in attributes]
        )
    dimensions = cast(list[Mapping[str, object]], payload["dimensions"])
    return " ".join(
        [
            cast(str, payload["value"]),
            cast(str, payload["unit"]),
            str(payload["occurrence_at"] or ""),
        ]
        + [f"{item['name']} {item['value']}" for item in dimensions]
    )


def _receipt_id(record: Mapping[str, object], kind: str) -> str:
    for value in cast(list[object], record["receipt_refs"]):
        receipt = cast(Mapping[str, object], value)
        if receipt.get("kind") == kind:
            return cast(str, receipt["receipt_id"])
    raise ValueError("validated Portable record is missing its receipt")


def _page_records(files: Mapping[str, bytes]) -> dict[str, tuple[str, Mapping[str, object], str]]:
    result: dict[str, tuple[str, Mapping[str, object], str]] = {}
    for path, payload in sorted(files.items()):
        parts = path.split("/")
        if (
            len(parts) != 5
            or parts[:2] != ["content", "spaces"]
            or parts[3] != "notes"
            or not parts[4].startswith("page_")
            or not parts[4].endswith(".md")
        ):
            continue
        parsed = parse_markdown(payload)
        page_id = cast(str, parsed.fields["page_id"])
        result[page_id] = (path, parsed.fields, parsed.body)
    return result


def _proposal_page_id(record: Mapping[str, object]) -> str | None:
    if record["proposed_kind"] != "page_update":
        return None
    content = cast(Mapping[str, object], record["proposed_content"])
    payload = base64.b64decode(cast(str, content["bytes_base64"]))
    try:
        return cast(str, parse_markdown(payload).fields["page_id"])
    except MarkdownFormatError:
        return None


def _proposal_text(record: Mapping[str, object]) -> tuple[str, str]:
    content = cast(Mapping[str, object], record["proposed_content"])
    payload = base64.b64decode(cast(str, content["bytes_base64"]))
    if record["proposed_kind"] == "page_update":
        try:
            parsed = parse_markdown(payload)
        except MarkdownFormatError:
            return "Imported page update", payload.decode("utf-8")
        return cast(str, parsed.fields["title"]), parsed.body
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return cast(str, record["proposed_kind"]), ""
    if not isinstance(value, Mapping):
        return cast(str, record["proposed_kind"]), ""
    body = value.get("text")
    if isinstance(body, str):
        return cast(str, record["proposed_kind"]), body
    return cast(str, record["proposed_kind"]), portable_canonical_json_bytes(value).decode()


def _siblings(record: Mapping[str, object]) -> tuple[str, ...]:
    context = cast(Mapping[str, object], record["sibling_context"])
    return tuple(cast(list[str], context["proposal_ids"]))


def _decision_bytes(decision: Mapping[str, object], proposal: Mapping[str, object]) -> bytes | None:
    if decision["outcome"] == "rejected":
        return None
    edited = decision["edited_content"]
    if edited is not None:
        return base64.b64decode(cast(str, cast(Mapping[str, object], edited)["bytes_base64"]))
    content = cast(Mapping[str, object], proposal["proposed_content"])
    return base64.b64decode(cast(str, content["bytes_base64"]))


def materialize_portable_root(
    root: Path,
    *,
    snapshot: PortableSnapshot,
    expected_root_identity: RootIdentity,
) -> Materialization:
    """Create private projections from one already-validated immutable snapshot."""
    if snapshot.root_identity != expected_root_identity:
        raise ValueError("Portable snapshot identity does not match its materialization root")
    files = snapshot.files
    profile = _profile(root, snapshot)
    captures = _json_records(files, "sources/captures")
    proposals = _json_records(files, "history/proposals")
    decisions = _json_records(files, "history/decisions")
    publications = _json_records(files, "history/publications")
    actions = _json_records(files, "history/actions")
    routes = _json_records(files, "history/routes")
    pages = _page_records(files)
    proposal_by_id = {cast(str, record["proposal_id"]): record for _, record in proposals}
    publication_by_decision = {
        cast(str, record["decision_id"]): record for _, record in publications
    }
    decision_by_proposal = {cast(str, record["proposal_id"]): record for _, record in decisions}
    page_id_by_proposal: dict[str, str | None] = {}
    for proposal_id, proposal in proposal_by_id.items():
        decision = decision_by_proposal.get(proposal_id)
        if decision is not None:
            publication = publication_by_decision.get(cast(str, decision["decision_id"]))
            page_id_by_proposal[proposal_id] = (
                cast(str, publication["page_id"])
                if publication is not None
                else _proposal_page_id(proposal)
            )
        elif proposal.get("proposed_kind") == "page_update":
            page_id_by_proposal[proposal_id] = _proposal_page_id(proposal)
    canonical_captures = {
        cast(list[str], record["capture_ids"])[0]
        for _, record in proposals
        if record.get("supplied_reason") == "explicit canonical-note action"
    }
    store = _LocalStore(profile)
    with store.transaction() as connection:
        for table in (
            "search_documents",
            "decisions",
            "proposals",
            "proposal_sets",
            "route_operations",
            "space_operations",
            "spaces",
            "captures",
        ):
            connection.execute(f"DELETE FROM {table}")
        for path, space_payload in sorted(files.items()):
            if not path.endswith("/_space.md"):
                continue
            fields = parse_markdown(space_payload).fields
            connection.execute(
                "INSERT INTO spaces (space_id, name, slug, updated_at) VALUES (?, ?, ?, ?)",
                (
                    fields["space_id"],
                    fields["name"],
                    fields["slug"],
                    "1970-01-01T00:00:00Z",
                ),
            )
        for path, record in captures:
            capture_id = cast(str, record["capture_id"])
            payload = cast(Mapping[str, object], record["payload"])
            source = cast(Mapping[str, object], record["source"])
            role_claim = cast(Mapping[str, object], record["role_claim"])
            canonical_path = next(
                (
                    page_path
                    for page_path, fields, _ in pages.values()
                    if capture_id in cast(list[str], fields["provenance"])
                ),
                None,
            )
            connection.execute(
                """
                INSERT INTO captures (
                    delivery_id, request_sha256, capture_id, accepted_receipt_id,
                    payload_family, payload_json, search_text, file_bytes, source_origin,
                    source_reference, space_id, intent, capture_why, action, title,
                    accepted_at, stage, source_path, canonical_path, enrichment_state,
                    actor_id, role_claim_json, privacy_json, provenance_json, submission_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _derived_key("import.capture", capture_id),
                    sha256(portable_canonical_json_bytes(record)).hexdigest(),
                    capture_id,
                    _receipt_id(record, "capture_accepted"),
                    payload["family"],
                    portable_canonical_json_bytes(payload),
                    _payload_search_text(payload),
                    None,
                    source["origin"],
                    source["reference"],
                    record["space_id"],
                    record["intent"],
                    record["capture_why"],
                    "canonical_note" if capture_id in canonical_captures else "quick",
                    None,
                    record["accepted_at"],
                    path,
                    canonical_path,
                    "pending_enrichment",
                    record["actor_id"],
                    portable_canonical_json_bytes(role_claim).decode(),
                    portable_canonical_json_bytes(record["privacy"]).decode(),
                    portable_canonical_json_bytes(record["provenance"]).decode(),
                    "import",
                ),
            )
            upsert_search_document(
                connection,
                result_id=capture_id,
                capture_id=capture_id,
                record_type="source",
                payload_family=cast(str, payload["family"]),
                space_id=cast(str | None, record["space_id"]),
                title=source_search_title(
                    payload_family=cast(str, payload["family"]),
                    body=_payload_search_text(payload),
                ),
                body=_payload_search_text(payload),
                canonical_path=None,
                updated_at=cast(str, record["accepted_at"]),
            )
        for _, record in routes:
            route_id = cast(str, record["route_id"])
            capture_id = cast(str, record["capture_id"])
            space_id = cast(str, record["space_id"])
            receipt = cast(Mapping[str, object], record["receipt"])
            connection.execute(
                """
                INSERT INTO route_operations (
                    delivery_id, request_sha256, route_id, supersedes_route_id,
                    capture_id, space_id, receipt_id, recorded_at, stage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 2)
                """,
                (
                    _derived_key("import.route", route_id),
                    sha256(
                        portable_canonical_json_bytes(
                            {"capture_id": capture_id, "space_id": space_id}
                        )
                    ).hexdigest(),
                    route_id,
                    record["supersedes"],
                    capture_id,
                    space_id,
                    receipt["receipt_id"],
                    record["recorded_at"],
                ),
            )
        groups: dict[tuple[str, ...], str] = {}
        for _, record in proposals:
            sibling_ids = _siblings(record)
            groups.setdefault(
                sibling_ids,
                _derived_key("import.proposal-set", ",".join(sibling_ids)),
            )
        for siblings, delivery_id in groups.items():
            first = proposal_by_id[siblings[0]]
            connection.execute(
                """
                INSERT INTO proposal_sets (
                    delivery_id, request_sha256, capture_id, recorded_at, stage
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (
                    delivery_id,
                    sha256(
                        portable_canonical_json_bytes({"proposal_ids": list(siblings)})
                    ).hexdigest(),
                    cast(list[str], first["capture_ids"])[0],
                    first["recorded_at"],
                ),
            )
        for _, record in proposals:
            proposal_id = cast(str, record["proposal_id"])
            siblings = _siblings(record)
            page_id = page_id_by_proposal.get(proposal_id)
            title, body = _proposal_text(record)
            content = cast(Mapping[str, object], record["proposed_content"])
            proposed_bytes = base64.b64decode(cast(str, content["bytes_base64"]))
            connection.execute(
                """
                INSERT INTO proposals (
                    proposal_id, set_delivery_id, capture_id, proposed_kind, title, body,
                    proposed_bytes, supplied_reason, space_id, receipt_id, page_id,
                    canonical_path, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    proposal_id,
                    groups[siblings],
                    cast(list[str], record["capture_ids"])[0],
                    record["proposed_kind"],
                    title,
                    body,
                    proposed_bytes,
                    record["supplied_reason"],
                    record["space_id"],
                    cast(Mapping[str, object], record["expected_receipt"])["receipt_id"],
                    page_id,
                    pages[page_id][0] if page_id is not None and page_id in pages else None,
                ),
            )
        for _, record in decisions:
            decision_id = cast(str, record["decision_id"])
            proposal_id = cast(str, record["proposal_id"])
            proposal = proposal_by_id[proposal_id]
            page_id = page_id_by_proposal.get(proposal_id)
            publication = publication_by_decision.get(decision_id)
            connection.execute(
                """
                INSERT INTO decisions (
                    delivery_id, request_sha256, decision_id, decision_receipt_id,
                    proposal_id, outcome, effective_bytes, recorded_at, page_id,
                    publication_id, canonical_path, publication_path, stage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3)
                """,
                (
                    _derived_key("import.decision", decision_id),
                    sha256(portable_canonical_json_bytes(record)).hexdigest(),
                    decision_id,
                    _derived_key("import.decision-receipt", decision_id),
                    proposal_id,
                    record["outcome"],
                    _decision_bytes(record, proposal),
                    record["recorded_at"],
                    page_id,
                    publication["publication_id"] if publication is not None else None,
                    pages[page_id][0] if page_id is not None and page_id in pages else None,
                    publication["published_path"] if publication is not None else None,
                ),
            )
            connection.execute(
                "UPDATE proposals SET status = ?, terminal_decision_id = ? WHERE proposal_id = ?",
                (record["outcome"], decision_id, proposal_id),
            )
        for page_id, (page_path, fields, body) in pages.items():
            if fields["status"] == "archived":
                continue
            provenance = cast(list[str], fields["provenance"])
            capture_id = provenance[0]
            capture = next(record for _, record in captures if record["capture_id"] == capture_id)
            payload = cast(Mapping[str, object], capture["payload"])
            upsert_search_document(
                connection,
                result_id=page_id,
                capture_id=capture_id,
                record_type="canonical",
                payload_family=cast(str, payload["family"]),
                space_id=cast(str, fields["space_id"]),
                title=cast(str, fields["title"]),
                body=body,
                canonical_path=page_path,
                updated_at=cast(str, fields["modified_at"]),
            )
        superseded_routes = {
            cast(str, record["supersedes"])
            for _, record in routes
            if record["supersedes"] is not None
        }
        for _, record in routes:
            if record["route_id"] in superseded_routes:
                continue
            connection.execute(
                "UPDATE captures SET space_id = ? WHERE capture_id = ?",
                (record["space_id"], record["capture_id"]),
            )
            connection.execute(
                "UPDATE search_documents SET space_id = ?, updated_at = ? WHERE capture_id = ?",
                (record["space_id"], record["recorded_at"], record["capture_id"]),
            )
    batch_count = sum(
        path.startswith("sources/batches/") and path.endswith(".jsonl") for path in files
    )
    blob_count = sum(path.startswith("sources/blobs/") for path in files)
    return Materialization(
        profile=profile,
        captures=len(captures),
        batches=batch_count,
        blobs=blob_count,
        history_records=(
            len(proposals) + len(decisions) + len(publications) + len(actions) + len(routes)
        ),
    )
