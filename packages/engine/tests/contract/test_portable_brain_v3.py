from __future__ import annotations

import base64
import copy
import json
import shutil
import sqlite3
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.materializer import materialize_portable_root
from open_brain_engine.engine.portability_ports import LocalPortableWrites
from open_brain_engine.portability import shared_brain_from_snapshot
from open_brain_engine.portable import PortableValidationError, validate_portable_root
from open_brain_engine.portable.review_binding import (
    review_binding_digest,
    validate_review_binding,
)
from open_brain_engine.portable.v3 import PORTABLE_V3_SCHEMA_CATALOG_DIGEST
from open_brain_engine.portable.versioned import validated_portable_snapshot
from open_brain_engine.storage.filesystem import WriteState, capture_root_identity
from open_brain_engine.storage.markdown import parse_markdown, render_markdown


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    resource = files("open_brain_engine.portable").joinpath("conformance/v1/brain-root")
    with as_file(resource) as source:
        shutil.copytree(source, root)
    return root


def _json_path(root: Path, family: str, identifier: str) -> Path:
    return next((root / family).rglob(f"{identifier}.json"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(portable_canonical_json_bytes(value))


def _write_manifest(root: Path) -> None:
    prior = json.loads((root / "portable-manifest.json").read_bytes())
    paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "portable-manifest.json"
        and ".open-brain" not in path.parts
    )
    manifest = {
        **prior,
        "compatibility": {
            "maximum_contract_version": "3",
            "minimum_contract_version": "1",
        },
        "contract_version": "3",
        "files": [
            {"path": path, "sha256": sha256((root / path).read_bytes()).hexdigest()}
            for path in paths
        ],
        "layout_version": 3,
        "schema_catalog_digest": PORTABLE_V3_SCHEMA_CATALOG_DIGEST,
        "schema_version": 3,
    }
    _write_json(root / "portable-manifest.json", manifest)


def _v3_root(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = _fixture(tmp_path)
    proposal_path = next((root / "history/proposals").rglob("*.json"))
    while True:
        proposal = cast(dict[str, object], json.loads(proposal_path.read_bytes()))
        if proposal["proposed_kind"] == "page_update":
            break
        proposal_path = next(
            path
            for path in (root / "history/proposals").rglob("*.json")
            if path != proposal_path
        )
    proposal_id = cast(str, proposal["proposal_id"])
    capture_id = cast(list[str], proposal["capture_ids"])[0]
    page_path = next((root / "content/spaces").rglob("page_*.md"))
    page = parse_markdown(page_path.read_bytes())
    page_id = cast(str, page.fields["page_id"])
    page_bytes = render_markdown(
        fields={
            **page.fields,
            "actor_id": proposal["actor_id"],
            "modified_at": proposal["recorded_at"],
            "privacy": proposal["privacy"],
            "provenance": [capture_id],
            "role_claim": proposal["role_claim"],
            "space_id": proposal["space_id"],
            "tenant_id": proposal["tenant_id"],
            "trust": "reviewed",
        },
        body=page.body,
    ).encode()
    page_path.write_bytes(page_bytes)
    content_digest = sha256(page_bytes).hexdigest()
    proposal["proposed_content"] = {
        "bytes_base64": base64.b64encode(page_bytes).decode("ascii"),
        "media_type": "text/markdown",
        "sha256": content_digest,
    }
    receipt = cast(dict[str, object], proposal["expected_receipt"])
    receipt_payload = cast(dict[str, object], receipt["payload"])
    receipt_payload["proposed_content_sha256"] = content_digest
    receipt["sha256"] = sha256(portable_canonical_json_bytes(receipt_payload)).hexdigest()
    _write_json(proposal_path, proposal)

    capture_path = _json_path(root, "sources/captures", capture_id)
    binding: dict[str, object] = {
        "actor_id": proposal["actor_id"],
        "expected_page_sha256": None,
        "expected_publication_id": None,
        "operation": "create",
        "page_id": page_id,
        "proposal_id": proposal_id,
        "provenance": [capture_id],
        "recorded_at": proposal["recorded_at"],
        "review_digest": "0" * 64,
        "role_claim": proposal["role_claim"],
        "schema_version": 3,
        "selected_capture_ids": [capture_id],
        "source_states": [
            {
                "capture_id": capture_id,
                "route_id": None,
                "sha256": sha256(capture_path.read_bytes()).hexdigest(),
                "space_id": proposal["space_id"],
            }
        ],
        "tenant_id": proposal["tenant_id"],
    }
    binding["review_digest"] = review_binding_digest(proposal, binding)
    recorded = cast(str, binding["recorded_at"])
    binding_path = (
        root
        / "history/review-bindings"
        / recorded[:4]
        / recorded[5:7]
        / f"{proposal_id}.json"
    )
    _write_json(binding_path, binding)

    decision_path = next(
        path
        for path in (root / "history/decisions").rglob("*.json")
        if json.loads(path.read_bytes())["proposal_id"] == proposal_id
    )
    decision = cast(dict[str, object], json.loads(decision_path.read_bytes()))
    decision["expected_receipt"] = copy.deepcopy(receipt)
    decision["expected_state_digest"] = binding["review_digest"]
    decision["outcome"] = "approved"
    decision["edited_content"] = None
    terminal = {
        "decision_id": decision["decision_id"],
        "edited_content_sha256": None,
        "expected_state_digest": decision["expected_state_digest"],
        "outcome": decision["outcome"],
        "proposal_id": proposal_id,
    }
    decision["terminal_digest"] = sha256(portable_canonical_json_bytes(terminal)).hexdigest()
    _write_json(decision_path, decision)
    publication_path = next(
        path
        for path in (root / "history/publications").rglob("*.json")
        if json.loads(path.read_bytes())["decision_id"] == decision["decision_id"]
    )
    publication = cast(dict[str, object], json.loads(publication_path.read_bytes()))
    publication["published_bytes_base64"] = base64.b64encode(page_bytes).decode("ascii")
    publication["published_sha256"] = sha256(page_bytes).hexdigest()
    _write_json(publication_path, publication)
    _write_manifest(root)
    return root, binding


def test_review_binding_helpers_are_closed_bounded_and_digest_exact() -> None:
    proposal = {"proposal_id": "synthetic", "value": "draft"}
    binding = {
        "actor_id": "actor_123e4567-e89b-42d3-a456-426614174001",
        "expected_page_sha256": None,
        "expected_publication_id": None,
        "operation": "create",
        "page_id": "page_123e4567-e89b-42d3-a456-426614174005",
        "proposal_id": "proposal_123e4567-e89b-42d3-a456-426614174008",
        "provenance": ["capture_123e4567-e89b-42d3-a456-426614174100"],
        "recorded_at": "2026-08-30T12:05:00Z",
        "review_digest": "0" * 64,
        "role_claim": {
            "actor_id": "actor_123e4567-e89b-42d3-a456-426614174001",
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_123e4567-e89b-42d3-a456-426614174003",
            "role_id": "role_123e4567-e89b-42d3-a456-426614174002",
            "tenant_id": "tenant_123e4567-e89b-42d3-a456-426614174000",
        },
        "schema_version": 3,
        "selected_capture_ids": ["capture_123e4567-e89b-42d3-a456-426614174100"],
        "source_states": [
            {
                "capture_id": "capture_123e4567-e89b-42d3-a456-426614174100",
                "route_id": None,
                "sha256": "1" * 64,
                "space_id": "space_123e4567-e89b-42d3-a456-426614174004",
            }
        ],
        "tenant_id": "tenant_123e4567-e89b-42d3-a456-426614174000",
    }
    first = review_binding_digest(proposal, binding)
    binding["review_digest"] = first
    assert validate_review_binding(binding)["review_digest"] == first
    assert review_binding_digest(proposal, binding) == first

    invalid = {**binding, "unknown": True}
    with pytest.raises(PortableValidationError, match="shape"):
        validate_review_binding(invalid)


def test_v3_validates_and_materializes_frozen_context_and_page_head(tmp_path: Path) -> None:
    root, binding = _v3_root(tmp_path)

    manifest = validate_portable_root(root)
    snapshot = validated_portable_snapshot(root)
    materialization = materialize_portable_root(
        root,
        snapshot=snapshot,
        expected_root_identity=snapshot.root_identity,
    )
    shared = shared_brain_from_snapshot(snapshot)
    connection = materialization.profile.root.joinpath(
        ".open-brain/state/phase1.sqlite3"
    )
    database = sqlite3.connect(connection)
    try:
        context = database.execute(
            "SELECT binding_json, proposal_json FROM review_contexts WHERE proposal_id = ?",
            (binding["proposal_id"],),
        ).fetchone()
        sources = database.execute(
            "SELECT capture_id, ordinal FROM review_sources WHERE proposal_id = ?",
            (binding["proposal_id"],),
        ).fetchall()
        head = database.execute(
            "SELECT page_id, publication_id, published_sha256 FROM review_page_heads"
        ).fetchone()
    finally:
        database.close()

    assert manifest["schema_version"] == 3
    assert len(shared.extensions) == 1
    assert shared.reconstruct_file_set() == {
        path: payload
        for path, payload in snapshot.files.items()
        if path != "portable-manifest.json"
    }
    assert context is not None
    assert json.loads(context[0]) == binding
    assert sources == [(cast(list[str], binding["provenance"])[0], 0)]
    assert head is not None and head[0] == binding["page_id"]


def test_v3_rejects_binding_tamper_even_with_updated_manifest(tmp_path: Path) -> None:
    root, binding = _v3_root(tmp_path)
    binding["expected_page_sha256"] = "2" * 64
    binding_path = next((root / "history/review-bindings").rglob("*.json"))
    _write_json(binding_path, binding)
    _write_manifest(root)

    with pytest.raises(PortableValidationError):
        validate_portable_root(root)


def test_v3_rejects_edited_publication_that_drops_bound_provenance(
    tmp_path: Path,
) -> None:
    root, binding = _v3_root(tmp_path)
    proposal_id = cast(str, binding["proposal_id"])
    decision_path = next(
        path
        for path in (root / "history/decisions").rglob("*.json")
        if json.loads(path.read_bytes())["proposal_id"] == proposal_id
    )
    decision = cast(dict[str, object], json.loads(decision_path.read_bytes()))
    publication_path = next(
        path
        for path in (root / "history/publications").rglob("*.json")
        if json.loads(path.read_bytes())["decision_id"] == decision["decision_id"]
    )
    publication = cast(dict[str, object], json.loads(publication_path.read_bytes()))
    page_path = root / cast(str, publication["published_path"])
    page = parse_markdown(page_path.read_bytes())
    other_capture_id = next(
        json.loads(path.read_bytes())["capture_id"]
        for path in (root / "sources/captures").rglob("*.json")
        if json.loads(path.read_bytes())["capture_id"]
        not in cast(list[str], binding["provenance"])
    )
    edited_bytes = render_markdown(
        fields={
            **page.fields,
            "modified_at": decision["recorded_at"],
            "provenance": [other_capture_id],
        },
        body="Owner edited body with substituted provenance.\n",
    ).encode()
    edited_sha256 = sha256(edited_bytes).hexdigest()
    decision["outcome"] = "edited"
    decision["edited_content"] = {
        "bytes_base64": base64.b64encode(edited_bytes).decode("ascii"),
        "sha256": edited_sha256,
    }
    decision["terminal_digest"] = sha256(
        portable_canonical_json_bytes(
            {
                "decision_id": decision["decision_id"],
                "edited_content_sha256": edited_sha256,
                "expected_state_digest": decision["expected_state_digest"],
                "outcome": "edited",
                "proposal_id": proposal_id,
            }
        )
    ).hexdigest()
    publication["published_bytes_base64"] = base64.b64encode(edited_bytes).decode("ascii")
    publication["published_sha256"] = edited_sha256
    _write_json(decision_path, decision)
    _write_json(publication_path, publication)
    page_path.write_bytes(edited_bytes)
    _write_manifest(root)

    with pytest.raises(PortableValidationError, match="published page binding"):
        validate_portable_root(root)


def test_portable_page_replace_is_compare_and_swap_and_replay_safe(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    page_path = next((root / "content/spaces").rglob("page_*.md"))
    relative = page_path.relative_to(root).as_posix()
    current = page_path.read_bytes()
    parsed = parse_markdown(current)
    replacement = render_markdown(
        fields=parsed.fields, body=parsed.body + "\nSynthetic revision.\n"
    ).encode()
    tenant_id = cast(str, parsed.fields["tenant_id"])
    writes = LocalPortableWrites(root, tenant_id, capture_root_identity(root))

    assert writes.replace_page(
        relative, replacement, expected_sha256=sha256(current).hexdigest()
    ) is WriteState.CREATED
    assert writes.replace_page(
        relative, replacement, expected_sha256=sha256(current).hexdigest()
    ) is WriteState.ALREADY_EXISTS
    with pytest.raises(ValueError, match="canonical page revision conflict"):
        writes.replace_page(
            relative,
            render_markdown(fields=parsed.fields, body="# Competing\n").encode(),
            expected_sha256=sha256(current).hexdigest(),
        )
    with pytest.raises(ValueError, match="canonical page revision conflict"):
        writes.replace_page(
            relative.replace(
                f"{parsed.fields['page_id']}.md",
                "page_123e4567-e89b-42d3-a456-426614174099.md",
            ),
            render_markdown(
                fields={
                    **parsed.fields,
                    "page_id": "page_123e4567-e89b-42d3-a456-426614174099",
                },
                body=parsed.body,
            ).encode(),
            expected_sha256=sha256(current).hexdigest(),
        )
