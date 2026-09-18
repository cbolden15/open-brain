"""Portable Brain v3 review history over unchanged v1 record shapes."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    assert_root_identity,
    capture_root_identity,
    open_root_descriptor,
)
from open_brain_engine.storage.markdown import parse_markdown

from .managed_v2 import validate_managed_workspace_record
from .review_binding import review_binding_digest, validate_review_binding
from .v1 import (
    PortableSnapshot,
    PortableValidationError,
    _decision_terminal_payload,
    _digest,
    _identifier,
    _portable_record,
    _snapshot_directory,
    _timestamp,
    _validate_blob_address,
    _validate_relative_path,
    validate_portable_file_set,
)

_BINDING_PATH = re.compile(
    r"history/review-bindings/(?P<year>[0-9]{4})/(?P<month>0[1-9]|1[0-2])/"
    r"(?P<proposal>proposal_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json"
)
_MANAGED_PATH = re.compile(
    r"history/managed-workspace/"
    r"(?P<workspace>workspace_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json"
)
PORTABLE_V3_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(
        {
            "base": "portable-brain-v1-exact-record-shapes",
            "managed_workspace_record": 1,
            "review_binding_record": 1,
            "schema_version": 3,
        }
    )
).hexdigest()


def _require_manifest(manifest: Mapping[str, object]) -> None:
    if set(manifest) != {
        "compatibility",
        "contract_version",
        "created_at",
        "export_id",
        "files",
        "layout_version",
        "schema_catalog_digest",
        "schema_version",
        "tenant_id",
    }:
        raise PortableValidationError("unsupported manifest shape")
    if (
        manifest.get("layout_version") != 3
        or manifest.get("schema_version") != 3
        or manifest.get("contract_version") != "3"
        or manifest.get("compatibility")
        != {"maximum_contract_version": "3", "minimum_contract_version": "1"}
        or manifest.get("schema_catalog_digest") != PORTABLE_V3_SCHEMA_CATALOG_DIGEST
    ):
        raise PortableValidationError("unsupported Portable v3 manifest")
    if not isinstance(manifest.get("files"), list) or not manifest["files"]:
        raise PortableValidationError("manifest files are required")
    _identifier(manifest.get("export_id"), "export", "manifest export")
    _identifier(manifest.get("tenant_id"), "tenant", "manifest tenant")
    _timestamp(manifest.get("created_at"), "manifest created")


def _records(files: Mapping[str, bytes], prefix: str, label: str) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for path, payload in sorted(files.items()):
        if not path.startswith(prefix) or not path.endswith(".json"):
            continue
        record = _portable_record(payload, label)
        key = path.rsplit("/", 1)[-1].removesuffix(".json")
        if key in result:
            raise PortableValidationError(f"{label} identity is duplicated")
        result[key] = record
    return result


def _legacy_decision_projection(
    base: Mapping[str, bytes], bindings: Mapping[str, dict[str, object]]
) -> dict[str, bytes]:
    """Project v3 decision digests to the unchanged v1 digest rule for base validation."""
    result = dict(base)
    proposals = _records(base, "history/proposals/", "proposal")
    for path, payload in sorted(base.items()):
        if not path.startswith("history/decisions/") or not path.endswith(".json"):
            continue
        decision = _portable_record(payload, "decision")
        proposal_id = cast(str, decision.get("proposal_id"))
        binding = bindings.get(proposal_id)
        if binding is None:
            continue
        proposal = proposals.get(proposal_id)
        if proposal is None:
            raise PortableValidationError("review binding proposal is missing")
        if decision.get("expected_state_digest") != binding["review_digest"]:
            raise PortableValidationError("decision review binding digest mismatch")
        edited = decision.get("edited_content")
        edited_digest = (
            cast(str, cast(Mapping[str, object], edited)["sha256"])
            if isinstance(edited, Mapping)
            else None
        )
        projected = dict(decision)
        projected["expected_state_digest"] = sha256(
            portable_canonical_json_bytes(proposal)
        ).hexdigest()
        projected["terminal_digest"] = sha256(
            portable_canonical_json_bytes(_decision_terminal_payload(projected, edited_digest))
        ).hexdigest()
        result[path] = portable_canonical_json_bytes(projected)
    return result


def _published_bytes(publication: Mapping[str, object]) -> bytes:
    return base64.b64decode(cast(str, publication["published_bytes_base64"]), validate=True)


def _apply_patch(body: str, patch: Mapping[str, object]) -> str:
    result = body.encode("utf-8")
    for operation in reversed(cast(list[Mapping[str, object]], patch["operations"])):
        start = cast(int, operation["start_byte"])
        end = cast(int, operation["end_byte"])
        result = result[:start] + cast(str, operation["replacement"]).encode("utf-8") + result[end:]
    try:
        return result.decode("utf-8")
    except UnicodeDecodeError:
        raise PortableValidationError("review patch content mismatch") from None


def _recorded_before(value: object, boundary: object) -> bool:
    recorded_at = datetime.fromisoformat(cast(str, value).removesuffix("Z") + "+00:00")
    boundary_at = datetime.fromisoformat(cast(str, boundary).removesuffix("Z") + "+00:00")
    return recorded_at < boundary_at


def _route_matches_frozen_state(
    routes: Mapping[str, dict[str, object]],
    *,
    capture: Mapping[str, object],
    state: Mapping[str, object],
    proposed_at: object,
) -> bool:
    capture_id = state.get("capture_id")
    chosen_id = state.get("route_id")
    capture_routes = {
        route_id: route
        for route_id, route in routes.items()
        if route.get("capture_id") == capture_id
    }
    if chosen_id is None:
        return capture.get("space_id") == state.get("space_id") and not any(
            _recorded_before(route["recorded_at"], proposed_at) for route in capture_routes.values()
        )
    chosen = capture_routes.get(cast(str, chosen_id))
    if (
        chosen is None
        or chosen.get("space_id") != state.get("space_id")
        or _recorded_before(proposed_at, chosen["recorded_at"])
    ):
        return False
    current_id = cast(str, chosen_id)
    while True:
        successor = next(
            (
                (route_id, route)
                for route_id, route in capture_routes.items()
                if route.get("supersedes") == current_id
            ),
            None,
        )
        if successor is None:
            return True
        if _recorded_before(successor[1]["recorded_at"], proposed_at):
            return False
        current_id = successor[0]


def _validate_binding_semantics(
    files: Mapping[str, bytes], bindings: Mapping[str, dict[str, object]]
) -> None:
    proposals = _records(files, "history/proposals/", "proposal")
    decisions = _records(files, "history/decisions/", "decision")
    publications = _records(files, "history/publications/", "publication")
    captures = _records(files, "sources/captures/", "capture")
    routes = _records(files, "history/routes/", "routing")
    decisions_by_proposal = {cast(str, value["proposal_id"]): value for value in decisions.values()}
    publications_by_decision = {
        cast(str, value["decision_id"]): value for value in publications.values()
    }
    page_payloads: dict[str, bytes] = {}
    page_paths: dict[str, str] = {}
    for path, payload in files.items():
        if path.startswith("content/spaces/") and "/notes/" in path and path.endswith(".md"):
            page_id = cast(str, parse_markdown(payload).fields["page_id"])
            page_payloads[page_id] = payload
            page_paths[page_id] = path

    publication_by_id = {
        cast(str, publication["publication_id"]): publication
        for publication in publications.values()
    }
    approved_children: dict[str, str] = {}
    published_bindings: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    for proposal_id, binding in bindings.items():
        proposal = proposals.get(proposal_id)
        if proposal is None or proposal.get("proposed_kind") != "page_update":
            raise PortableValidationError("review binding proposal is missing or unsupported")
        for key in ("actor_id", "role_claim", "recorded_at", "tenant_id"):
            if binding[key] != proposal.get(key):
                raise PortableValidationError("review binding proposal metadata mismatch")
        if binding["review_digest"] != review_binding_digest(proposal, binding):
            raise PortableValidationError("review binding digest mismatch")
        provenance = cast(list[str], binding["provenance"])
        selected = cast(list[str], binding["selected_capture_ids"])
        if proposal.get("capture_ids") != provenance:
            raise PortableValidationError("review binding proposal provenance mismatch")
        evidence = cast(list[Mapping[str, object]], proposal["evidence"])
        if [item.get("capture_id") for item in evidence] != provenance:
            raise PortableValidationError("review proposal evidence coverage mismatch")
        content = cast(Mapping[str, object], proposal["proposed_content"])
        proposed_page = parse_markdown(
            base64.b64decode(cast(str, content["bytes_base64"]), validate=True)
        )
        if (
            proposed_page.fields.get("page_id") != binding["page_id"]
            or proposed_page.fields.get("provenance") != provenance
            or proposed_page.fields.get("space_id") != proposal.get("space_id")
            or proposed_page.fields.get("tenant_id") != proposal.get("tenant_id")
            or proposed_page.fields.get("actor_id") != proposal.get("actor_id")
            or proposed_page.fields.get("role_claim") != proposal.get("role_claim")
            or proposed_page.fields.get("privacy") != proposal.get("privacy")
            or proposed_page.fields.get("modified_at") != proposal.get("recorded_at")
            or proposed_page.fields.get("status") != "active"
            or proposed_page.fields.get("trust") != "reviewed"
        ):
            raise PortableValidationError("review proposed page binding mismatch")
        source_states = cast(list[Mapping[str, object]], binding["source_states"])
        for capture_id, state in zip(provenance, source_states, strict=True):
            capture = captures.get(capture_id)
            if capture is None:
                raise PortableValidationError("review source state is unavailable")
            capture_path = next(
                path
                for path in files
                if path.startswith("sources/captures/") and path.endswith(f"/{capture_id}.json")
            )
            if (
                state.get("sha256") != sha256(files[capture_path]).hexdigest()
                or not _route_matches_frozen_state(
                    routes,
                    capture=capture,
                    state=state,
                    proposed_at=binding["recorded_at"],
                )
                or state.get("space_id") != proposal.get("space_id")
            ):
                raise PortableValidationError("review source state changed")

        operation = cast(str, binding["operation"])
        expected_id = cast(str | None, binding["expected_publication_id"])
        if operation == "create":
            if provenance != selected:
                raise PortableValidationError("review create provenance is malformed")
        else:
            parent = publication_by_id.get(cast(str, expected_id))
            if parent is None or parent.get("page_id") != binding["page_id"]:
                raise PortableValidationError("review predecessor publication is missing")
            if parent.get("published_sha256") != binding["expected_page_sha256"]:
                raise PortableValidationError("review predecessor digest mismatch")
            parent_page = parse_markdown(_published_bytes(parent))
            parent_provenance = cast(list[str], parent_page.fields["provenance"])
            if binding.get("schema_version") == 4:
                patch = cast(Mapping[str, object], binding["patch"])
                base_body = cast(str, patch["base_body"])
                if parent_page.body != base_body or proposed_page.body != _apply_patch(
                    base_body, patch
                ):
                    raise PortableValidationError("review patch content mismatch")
            expected_provenance = list(parent_provenance)
            expected_provenance.extend(item for item in selected if item not in expected_provenance)
            if provenance != expected_provenance:
                raise PortableValidationError("review cumulative provenance is malformed")

        decision = decisions_by_proposal.get(proposal_id)
        if decision is None:
            continue
        if decision.get("expected_state_digest") != binding["review_digest"]:
            raise PortableValidationError("decision review binding digest mismatch")
        publication = publications_by_decision.get(cast(str, decision["decision_id"]))
        if decision.get("outcome") in {"approved", "edited"}:
            if publication is None or publication.get("page_id") != binding["page_id"]:
                raise PortableValidationError("approved review publication is missing")
            published_page = parse_markdown(_published_bytes(publication))
            expected_fields = {
                key: value for key, value in proposed_page.fields.items() if key != "modified_at"
            }
            actual_fields = {
                key: value for key, value in published_page.fields.items() if key != "modified_at"
            }
            if (
                actual_fields != expected_fields
                or decision.get("outcome") == "edited"
                and published_page.fields.get("modified_at") != decision.get("recorded_at")
                or publication.get("published_path")
                != page_paths.get(cast(str, binding["page_id"]))
            ):
                raise PortableValidationError("review published page binding mismatch")
            publication_id = cast(str, publication["publication_id"])
            published_bindings[publication_id] = (binding, publication)
            if expected_id is not None:
                previous_child = approved_children.setdefault(expected_id, publication_id)
                if previous_child != publication_id:
                    raise PortableValidationError("review publication chain forks")
        elif publication is not None:
            raise PortableValidationError("rejected review has a publication")

    for publication_id in published_bindings:
        visited: set[str] = set()
        current_id: str | None = publication_id
        while current_id in published_bindings:
            if current_id in visited:
                raise PortableValidationError("review publication chain cycles")
            visited.add(current_id)
            current_id = cast(
                str | None, published_bindings[current_id][0]["expected_publication_id"]
            )
        if current_id is not None and current_id not in publication_by_id:
            raise PortableValidationError("review publication chain is incomplete")

    head_ids = set(published_bindings) - set(approved_children)
    head_pages: set[str] = set()
    for publication_id, (binding, publication) in published_bindings.items():
        if publication_id not in head_ids:
            continue
        page_id = cast(str, binding["page_id"])
        if page_id in head_pages:
            raise PortableValidationError("review page has multiple publication heads")
        head_pages.add(page_id)
        current_page = page_payloads.get(page_id)
        if current_page is None or current_page != _published_bytes(publication):
            raise PortableValidationError("current canonical page is not the review chain head")


def validate_portable_file_set_v3(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    """Validate exact Portable v3 payload bytes without an export manifest."""
    binding_paths = [path for path in files if _BINDING_PATH.fullmatch(path)]
    managed_paths = [path for path in files if _MANAGED_PATH.fullmatch(path)]
    if not binding_paths or len(managed_paths) > 1:
        raise PortableValidationError("Portable v3 review bindings are invalid")
    bindings: dict[str, dict[str, object]] = {}
    for path in binding_paths:
        binding = validate_review_binding(_portable_record(files[path], "review binding"))
        match = _BINDING_PATH.fullmatch(path)
        assert match is not None
        proposal_id = cast(str, binding["proposal_id"])
        if (
            match.group("proposal") != proposal_id
            or match.group("year") != cast(str, binding["recorded_at"])[:4]
            or match.group("month") != cast(str, binding["recorded_at"])[5:7]
            or proposal_id in bindings
        ):
            raise PortableValidationError("review binding path is malformed")
        bindings[proposal_id] = binding
    base = {
        path: payload
        for path, payload in files.items()
        if path not in {*binding_paths, *managed_paths}
    }
    validate_portable_file_set(_legacy_decision_projection(base, bindings), tenant_id=tenant_id)
    _validate_binding_semantics(base, bindings)
    if managed_paths:
        page_ids = {
            cast(str, parse_markdown(payload).fields["page_id"])
            for path, payload in base.items()
            if path.startswith("content/spaces/") and "/notes/" in path and path.endswith(".md")
        }
        managed = validate_managed_workspace_record(
            files[managed_paths[0]], tenant_id=tenant_id, page_ids=page_ids
        )
        match = _MANAGED_PATH.fullmatch(managed_paths[0])
        if match is None or managed.get("workspace_id") != match.group("workspace"):
            raise PortableValidationError("managed workspace path is invalid")


def validated_portable_snapshot_v3(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> PortableSnapshot:
    """Validate v3 bindings while preserving every unchanged v1/v2 byte."""
    try:
        root_identity = (
            capture_root_identity(root)
            if expected_root_identity is None
            else expected_root_identity
        )
        root_fd = open_root_descriptor(root, root_identity)
    except (OSError, RootConfinementError) as error:
        raise PortableValidationError("Portable root must be a real directory") from error
    files: dict[str, bytes] = {}
    try:
        _snapshot_directory(root_fd, (), files)
    finally:
        os.close(root_fd)
    manifest_payload = files.get("portable-manifest.json")
    if manifest_payload is None:
        raise PortableValidationError("manifest is missing")
    manifest = _portable_record(manifest_payload, "manifest")
    _require_manifest(manifest)
    entries = cast(list[object], manifest["files"])
    previous: str | None = None
    declared: set[str] = set()
    binding_paths: list[str] = []
    managed_paths: list[str] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise PortableValidationError("manifest entry must be an object")
        path = raw_entry.get("path")
        if not isinstance(path, str):
            raise PortableValidationError("manifest entry is malformed")
        digest = _digest(raw_entry.get("sha256"), "manifest entry")
        _validate_relative_path(path)
        if previous is not None and path <= previous:
            raise PortableValidationError("manifest paths must be sorted and unique")
        previous = path
        declared.add(path)
        payload = files.get(path)
        if payload is None or sha256(payload).hexdigest() != digest:
            raise PortableValidationError("manifest checksum mismatch")
        _validate_blob_address(path, digest)
        if _BINDING_PATH.fullmatch(path):
            binding_paths.append(path)
        elif _MANAGED_PATH.fullmatch(path):
            managed_paths.append(path)
    if declared != set(files) - {"portable-manifest.json"}:
        raise PortableValidationError("manifest does not describe the portable root exactly")
    if not binding_paths or len(managed_paths) > 1:
        raise PortableValidationError("Portable v3 review bindings are invalid")

    tenant_id = cast(str, manifest["tenant_id"])
    validate_portable_file_set_v3(
        {path: payload for path, payload in files.items() if path != "portable-manifest.json"},
        tenant_id=tenant_id,
    )
    try:
        assert_root_identity(root, root_identity)
    except RootConfinementError as error:
        raise PortableValidationError("Portable root identity changed") from error
    return PortableSnapshot(
        root_identity=root_identity,
        manifest=manifest,
        files=MappingProxyType(files),
    )


__all__ = [
    "PORTABLE_V3_SCHEMA_CATALOG_DIGEST",
    "validate_portable_file_set_v3",
    "validated_portable_snapshot_v3",
]
