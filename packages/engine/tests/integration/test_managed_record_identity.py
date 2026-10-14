"""Real managed custody lookup; no substitute stores or frontmatter identity mapping."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import DecisionOutcome, ProposalDraft
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.contracts import CaptureAction, LocalEngineContext, TextPayload
from open_brain_engine.engine.facet_queries import resolve_subject
from open_brain_engine.engine.facet_vault import FacetVaultTasks, facet_index_relative_path
from open_brain_engine.engine.facets import FacetTasks
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.managed_record_identity import (
    ManagedRecordIdentity,
    resolve_managed_record,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.providers.base import ProviderMode


class IdentityBrain:
    def __init__(self, directory: Path) -> None:
        root = directory / "brain"
        root.mkdir(mode=0o700)
        (root / ".open-brain").mkdir(mode=0o700)
        actor = "actor_00000000-0000-4000-8000-000000000002"
        tenant = "tenant_00000000-0000-4000-8000-000000000001"
        self.engine = BrainEngine.open(
            LocalEngineContext(
                root=root,
                root_identity=(root.stat().st_dev, root.stat().st_ino),
                tenant_id=tenant,
                owner_actor_id=actor,
                owner_role_claim={
                    "actor_id": actor,
                    "capabilities": ["owner"],
                    "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000003",
                    "role_id": "role_00000000-0000-4000-8000-000000000004",
                    "tenant_id": tenant,
                },
                provider_mode=ProviderMode.NONE,
                starter_spaces=("Notes",),
            ),
            clock=lambda: datetime(2026, 10, 13, 12, tzinfo=UTC),
        )
        self.owner = EffectiveAuthority(
            principal_id="opaque-plugin-owner",
            session_id="identity-test",
            capabilities=frozenset(),
            space_ids=None,
            owner=True,
        )
        self.engine.capture.accept(
            TextPayload("Canonical custody proof"),
            delivery_id="identity.capture",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=self.engine.inbox.spaces()[0].space_id,
        )
        self.page_id = self.engine.retrieval.search(
            "Canonical custody proof", record_type="canonical"
        )[0].result_id
        self.root = directory / "vault"
        self.root.mkdir(mode=0o700)
        self.workspace_id = self.engine.managed_workspace.setup(
            str(self.root), operation_id="identity.setup"
        ).workspace_id
        self.path = next(self.root.rglob(f"{self.page_id}.md"))
        self.relative_path = self.path.relative_to(self.root).as_posix()
        self.vault = FacetVaultTasks(self.engine)

    def observe(self) -> None:
        self.engine.managed_workspace.observe(self.workspace_id)

    def resolve(self, **changes: Any) -> ManagedRecordIdentity:
        return resolve_managed_record(
            self.engine,
            **{
                "workspace_id": self.workspace_id,
                "relative_path": self.relative_path,
                "authority": self.owner,
                **changes,
            },
        )

    def database(self) -> tuple[str, ...]:
        connection = self.engine._store.connect()
        try:
            return tuple(connection.iterdump())
        finally:
            connection.close()

    def assert_unavailable(self, **changes: Any) -> None:
        before = self.database()
        with pytest.raises(fc.FacetError) as error:
            self.resolve(**changes)
        assert error.value.code == "not_found"
        assert str(error.value) == "not_found"
        assert self.database() == before


def test_real_canonical_identity_uses_managed_custody_without_effects(tmp_path: Path) -> None:
    brain = IdentityBrain(tmp_path)
    brain.observe()
    before = brain.database()
    payload = brain.path.read_bytes()
    identity = brain.resolve()
    connection = brain.engine._store.connect()
    try:
        expected = resolve_subject(connection, brain.engine.profile, brain.page_id, brain.owner)
    finally:
        connection.close()
    assert identity.to_wire() == {
        "status": "ok",
        "dto_version": 1,
        "workspace_id": brain.workspace_id,
        "record_id": brain.page_id,
        "revision_id": expected.revision_id,
        "subject": {"kind": "canonical", "id": brain.page_id},
    }
    assert brain.database() == before
    assert brain.path.read_bytes() == payload


def test_missing_observation_requires_explicit_observe_not_refresh(tmp_path: Path) -> None:
    brain = IdentityBrain(tmp_path)
    brain.assert_unavailable()
    brain.engine.managed_workspace.refresh(brain.workspace_id, operation_id="identity.refresh")
    brain.assert_unavailable()
    brain.observe()
    assert brain.resolve().record_id == brain.page_id


def test_registered_facets_edit_is_resolvable_without_preview_or_classification(
    tmp_path: Path,
) -> None:
    brain = IdentityBrain(tmp_path)
    brain.vault.register_record(brain.workspace_id, brain.page_id, authority=brain.owner)
    brain.observe()
    original = brain.resolve()
    brain.path.write_bytes(
        brain.path.read_bytes().replace(b"facets: {}", b'facets: {"Area":["Owner edit"]}')
    )
    before = brain.database()
    assert brain.resolve() == original
    assert brain.database() == before
    assert b"Owner edit" in brain.path.read_bytes()
    # Preview remains a distinct explicit action, with unknown names reported as conflicts.
    preview = brain.vault.vault_preview(
        fc.VaultPreviewRequest(workspace_id=brain.workspace_id, record_id=brain.page_id),
        authority=brain.owner,
    ).to_wire()
    assert {item["code"] for item in cast(list[dict[str, object]], preview["conflicts"])} == {
        "unknown_name"
    }


@pytest.mark.parametrize("edit", ["page_id", "title", "body", "unregistered_facets"])
def test_identity_and_content_tampering_is_unavailable(tmp_path: Path, edit: str) -> None:
    brain = IdentityBrain(tmp_path)
    brain.observe()
    payload = brain.path.read_bytes()
    if edit == "page_id":
        payload = payload.replace(brain.page_id.encode(), b"page_owner_forgery")
    elif edit == "title":
        payload = payload.replace(b"title: ", b'title: "Forged" # ')
    elif edit == "body":
        payload += b"\nOwner body edit"
    else:
        payload = payload.replace(b"\n---\n\n", b'\nfacets: {"Owner":["Unregistered"]}\n---\n\n')
    brain.path.write_bytes(payload)
    brain.assert_unavailable()
    assert brain.path.read_bytes() == payload


@pytest.mark.parametrize(
    "path", ["../outside.md", "/outside.md", "a/../b.md", "a\\b.md", "", "./note.md", "missing.md"]
)
def test_confined_path_and_unknown_file_refusals(tmp_path: Path, path: str) -> None:
    brain = IdentityBrain(tmp_path)
    brain.observe()
    brain.assert_unavailable(relative_path=path)


def test_unregistered_copy_and_generated_index_do_not_supply_identity(tmp_path: Path) -> None:
    brain = IdentityBrain(tmp_path)
    brain.observe()
    copied = brain.root / "owner-copy.md"
    copied.write_bytes(brain.path.read_bytes())
    brain.assert_unavailable(relative_path="owner-copy.md")
    facets = FacetTasks(brain.engine)
    dimension = cast(
        str,
        facets.dimension_create(
            fc.DimensionCreateRequest(operation_id="identity.dim", expected_version=0, name="Area"),
            authority=brain.owner,
        ).to_wire()["subject_id"],
    )
    value = cast(
        str,
        facets.value_create(
            fc.ValueCreateRequest(
                operation_id="identity.value",
                expected_version=0,
                dimension_id=dimension,
                expected_dimension_version=1,
                name="Work",
            ),
            authority=brain.owner,
        ).to_wire()["subject_id"],
    )
    brain.vault.refresh_indexes(brain.workspace_id, authority=brain.owner)
    index = facet_index_relative_path(dimension, value)
    assert (brain.root / index).is_file()
    brain.assert_unavailable(relative_path=index)


def test_wrong_workspace_and_nonowner_refuse_without_disclosing_identity(tmp_path: Path) -> None:
    brain = IdentityBrain(tmp_path)
    brain.observe()
    brain.assert_unavailable(workspace_id="workspace_00000000-0000-4000-8000-000000000009")
    before = brain.database()
    with pytest.raises(fc.FacetError) as error:
        brain.resolve(authority=replace(brain.owner, owner=False))
    assert error.value.code == "unsupported_capability"
    assert brain.database() == before


@pytest.mark.parametrize("swap", ["file", "symlink", "root", "brain_root", "hardlink"])
def test_retained_file_and_root_identity_swaps_are_unavailable(tmp_path: Path, swap: str) -> None:
    brain = IdentityBrain(tmp_path)
    brain.observe()
    payload = brain.path.read_bytes()
    if swap == "file":
        replacement = brain.path.with_suffix(".replacement")
        replacement.write_bytes(payload)
        replacement.replace(brain.path)
    elif swap == "symlink":
        outside = tmp_path / "outside.md"
        outside.write_bytes(payload)
        brain.path.unlink()
        brain.path.symlink_to(outside)
    elif swap == "hardlink":
        os.link(brain.path, tmp_path / "alias.md")
    elif swap == "root":
        brain.root.rename(tmp_path / "retained-vault")
        brain.root.mkdir(mode=0o700)
        replacement = brain.root / brain.relative_path
        replacement.parent.mkdir(parents=True, exist_ok=True)
        replacement.write_bytes(payload)
    else:
        root = brain.engine.profile.root
        root.rename(tmp_path / "retained-brain")
        root.mkdir(mode=0o700)
        with pytest.raises(fc.FacetError) as error:
            brain.resolve()
        assert error.value.code == "not_found"
        assert not (root / ".open-brain").exists()
        return
    brain.assert_unavailable()


def test_real_source_capture_has_no_managed_mapping_but_keeps_facet_identity(
    tmp_path: Path,
) -> None:
    brain = IdentityBrain(tmp_path)
    capture = brain.engine.capture.accept(
        TextPayload("Source evidence"), delivery_id="identity.source"
    )
    connection = brain.engine._store.connect()
    try:
        subject = resolve_subject(connection, brain.engine.profile, capture.capture_id, brain.owner)
        assert subject.kind == "source"
        assert subject.id.startswith("source_")
        assert (
            connection.execute(
                "SELECT 1 FROM managed_notes WHERE note_id=?", (capture.capture_id,)
            ).fetchone()
            is None
        )
        assert all(
            row[0].startswith("page_")
            for row in connection.execute("SELECT note_id FROM managed_notes")
        )
    finally:
        connection.close()
    brain.observe()
    brain.assert_unavailable(relative_path=f"{capture.capture_id}.md")
    assert brain.resolve().record_id == brain.page_id


def test_inactive_managed_note_is_unavailable(tmp_path: Path) -> None:
    brain = IdentityBrain(tmp_path)
    brain.observe()
    brain.engine.managed_workspace.deactivate(
        brain.workspace_id, brain.page_id, operation_id="identity.deactivate"
    )
    brain.assert_unavailable()


def test_accepted_local_body_edit_cannot_claim_current_canonical_revision(tmp_path: Path) -> None:
    brain = IdentityBrain(tmp_path)
    brain.path.write_bytes(brain.path.read_bytes() + b"\nAccepted local edit")
    observed = brain.engine.managed_workspace.observe(brain.workspace_id)
    brain.engine.managed_workspace.accept_observed(
        brain.workspace_id,
        brain.page_id,
        generation=observed.generation,
        operation_id="identity.accept",
    )
    brain.observe()
    brain.assert_unavailable()


def test_new_canonical_revision_requires_materialization_and_observation(tmp_path: Path) -> None:
    brain = IdentityBrain(tmp_path)
    brain.observe()
    old = brain.resolve()
    capture = brain.engine.capture.accept(
        TextPayload("Current source evidence"),
        delivery_id="identity.successor.source",
        space_id=brain.engine.inbox.spaces()[0].space_id,
    )
    proposal = brain.engine.review.propose(
        (capture.capture_id,),
        (ProposalDraft("Updated canonical", "Approved replacement body"),),
        delivery_id="identity.successor.propose",
        target_page_id=brain.page_id,
    )[0]
    brain.engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="identity.successor.approve",
        expected_review_digest=proposal.review_digest,
    )
    brain.assert_unavailable()
    brain.engine.managed_workspace.refresh(
        brain.workspace_id, operation_id="identity.successor.refresh"
    )
    brain.assert_unavailable()
    brain.observe()
    current = brain.resolve()
    assert current.record_id == old.record_id
    assert current.revision_id != old.revision_id


@pytest.mark.parametrize("swap", ["file", "workspace_root"])
def test_real_swap_during_lookup_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    brain = IdentityBrain(tmp_path)
    brain.observe()
    reader = FacetVaultTasks._read_file
    calls = 0

    def read_then_swap(workspace: Any, relative_path: str) -> tuple[bytes, str]:
        nonlocal calls
        result = reader(workspace, relative_path)
        calls += 1
        if calls == 1:
            if swap == "file":
                replacement = brain.path.with_suffix(".replacement")
                replacement.write_bytes(result[0])
                replacement.replace(brain.path)
            else:
                brain.root.rename(tmp_path / "retained-vault")
                brain.root.mkdir(mode=0o700)
        return result

    # Hook the real reader only to perform an actual filesystem race; no output is fabricated.
    monkeypatch.setattr(FacetVaultTasks, "_read_file", staticmethod(read_then_swap))
    brain.assert_unavailable()
