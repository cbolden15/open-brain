"""Pure rendering checks and real-engine F20/F21 custody and content tests."""

from __future__ import annotations

import base64
import json
import sqlite3

# Real engine checks: no replacement store or hand-installed migration is permitted.
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.contracts import CaptureAction, LocalEngineContext, TextPayload
from open_brain_engine.engine.facet_schema_migration import facet_schema_available
from open_brain_engine.engine.facet_vault import (
    FacetProjectionConflict,
    FacetVaultTasks,
    IndexLink,
    ProjectedValue,
    content_without_registered_facets,
    facet_index_relative_path,
    index_write_status,
    reconcile_facets,
    render_facet_index,
    replace_registered_facets,
)
from open_brain_engine.engine.facet_vault_schema import FACET_VAULT_SCHEMA
from open_brain_engine.engine.facets import FacetTasks
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.managed_portability import export_managed_workspace_state
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.providers.base import ProviderMode
from open_brain_engine.storage.markdown import parse_markdown


def value(
    dimension_id: str,
    dimension_name: str,
    value_id: str,
    value_name: str,
    *,
    archived: bool = False,
) -> ProjectedValue:
    return ProjectedValue(dimension_id, dimension_name, value_id, value_name, archived)


def test_quoted_deterministic_frontmatter_preserves_other_content() -> None:
    original = b'---\npage_id: "page_1"\ntitle: "Owner title"\n---\n\nBody *owned*\n'
    values = [
        value("dim_b", "Work: next", "val_b", 'Needs "review"'),
        value("dim_a", "Area", "val_a", "[Home]"),
    ]
    projected = replace_registered_facets(original, values, registered=False)
    assert projected == (
        b'---\npage_id: "page_1"\ntitle: "Owner title"\n'
        b'facets: {"Area":["[Home]"],"Work: next":["Needs \\"review\\""]}'
        b"\n---\n\nBody *owned*\n"
    )
    assert parse_markdown(projected).fields["facets"] == {
        "Area": ["[Home]"],
        "Work: next": ['Needs "review"'],
    }
    assert replace_registered_facets(projected, values, registered=True) == projected
    assert content_without_registered_facets(projected, registered=True) == original


def test_unregistered_owner_facets_are_never_claimed() -> None:
    original = b'---\nfacets: {"Owner":["Handwritten"]}\npage_id: "page_1"\n---\n\nBody'
    with pytest.raises(FacetProjectionConflict):
        replace_registered_facets(original, [], registered=False)
    assert parse_markdown(original).fields["facets"] == {"Owner": ["Handwritten"]}
    assert content_without_registered_facets(original, registered=False) == original


def test_combined_edit_remains_visible_to_content_revision_flow() -> None:
    original = b'---\npage_id: "page_1"\n---\n\nOriginal body'
    generated = replace_registered_facets(
        original, [value("dim_1", "Area", "val_1", "Old")], registered=False
    )
    changed = generated.replace(b"Original body", b"Owner edit")
    assert content_without_registered_facets(generated, registered=True) == original
    assert content_without_registered_facets(changed, registered=True) != original


def test_rename_race_and_reused_old_label_report_ambiguity() -> None:
    previous = [value("dim_1", "Area", "val_1", "Old")]
    renamed = [value("dim_1", "Area", "val_1", "New")]
    retained = reconcile_facets(
        {"Area": ["Old"]},
        previous=previous,
        vocabulary=renamed,
        current_memberships=frozenset({"val_1"}),
    )
    assert retained.add_value_ids == retained.remove_value_ids == ()
    assert retained.conflicts == ()
    reused = renamed + [value("dim_1", "Area", "val_2", "Old")]
    ambiguous = reconcile_facets(
        {"Area": ["Old"]},
        previous=previous,
        vocabulary=reused,
        current_memberships=frozenset({"val_1"}),
    )
    assert [conflict.code for conflict in ambiguous.conflicts] == ["ambiguous_name"]


def test_missing_subtree_removes_prior_membership_only() -> None:
    previous = [value("dim_1", "Area", "val_1", "Old")]
    proposals: tuple[dict[str, object] | None, ...] = (None, {})
    for proposed in proposals:
        result = reconcile_facets(
            proposed,
            previous=previous,
            vocabulary=previous,
            current_memberships=frozenset({"val_1", "unprojected"}),
        )
        assert result.remove_value_ids == ("val_1",)
        assert result.add_value_ids == ()
        assert result.conflicts == ()


def test_unknown_name_and_archived_addition_conflict() -> None:
    archived = value("dim_1", "Area", "val_1", "Past", archived=True)
    result = reconcile_facets(
        {"Area": ["Past", "Missing"]},
        previous=[],
        vocabulary=[archived],
        current_memberships=frozenset(),
    )
    assert [(item.code, item.value_id) for item in result.conflicts] == [
        ("unknown_name", None),
        ("archived_addition", "val_1"),
    ]
    retained = reconcile_facets(
        {"Area": ["Past"]},
        previous=[archived],
        vocabulary=[archived],
        current_memberships=frozenset({"val_1"}),
    )
    assert retained.conflicts == ()


def test_index_rendering_and_owner_modified_preservation() -> None:
    rendered = render_facet_index(
        title="Area [A]",
        effectively_archived=True,
        links=[
            IndexLink("Page [one]", "spaces/one.md"),
            IndexLink("Page [one]", "spaces/one.md"),
            IndexLink("Page two", "spaces/two (2).md"),
        ],
    )
    assert rendered == (
        b"# Area \\[A\\] (archived)\n\n"
        b"- [Page \\[one\\]](../../spaces/one.md)\n"
        b"- [Page two](../../spaces/two%20%282%29.md)\n"
    )
    previous = sha256(rendered).hexdigest()
    assert (
        index_write_status(previous_digest=previous, existing=rendered, proposed=rendered)
        == "current"
    )
    assert (
        index_write_status(previous_digest=previous, existing=rendered, proposed=b"new") == "write"
    )
    assert (
        index_write_status(previous_digest=previous, existing=rendered + b"owner", proposed=b"new")
        == "conflict"
    )
    assert (
        index_write_status(previous_digest=None, existing=rendered, proposed=b"new") == "conflict"
    )


def test_index_refuses_parent_escape() -> None:
    with pytest.raises(ValueError, match="unsafe index link"):
        render_facet_index(
            title="Area", effectively_archived=False, links=[IndexLink("bad", "../private.md")]
        )


def test_index_path_uses_ids() -> None:
    dimension_id = "facetdim_00000000-0000-4000-8000-000000000001"
    value_id = "facetval_00000000-0000-4000-8000-000000000002"
    assert facet_index_relative_path(dimension_id, value_id) == (
        f"Facets/{dimension_id}/{value_id}.md"
    )
    with pytest.raises(ValueError, match="invalid facet index identity"):
        facet_index_relative_path("Area", value_id)


def test_additive_local_custody_schema_is_valid_sql() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE TABLE managed_workspaces(workspace_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE managed_notes(note_id TEXT PRIMARY KEY)")
        for statement in FACET_VAULT_SCHEMA:
            connection.execute(statement)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'facet_%'"
            )
        }
        assert names == {
            "facet_content_preimages",
            "facet_projection_state",
            "facet_projection_writes",
            "facet_projection_conflict_files",
            "facet_vault_previews",
            "facet_vault_apply_operations",
        }
    finally:
        connection.close()


class VaultBrain:
    def __init__(self, tmp_path: Path) -> None:
        root = tmp_path / "brain"
        root.mkdir(mode=0o700)
        (root / ".open-brain").mkdir(mode=0o700)
        actor = "actor_00000000-0000-4000-8000-000000000002"
        tenant = "tenant_00000000-0000-4000-8000-000000000001"
        profile = LocalEngineContext(
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
        )
        self.now = datetime(2026, 10, 13, 12, tzinfo=UTC)
        self.engine = BrainEngine.open(profile, clock=lambda: self.now)
        with self.engine._store.transaction() as connection:
            assert facet_schema_available(connection), "real schema 11 registration required"
            assert connection.execute("SELECT 1 FROM facet_content_preimages").fetchall() == []
        self.owner = EffectiveAuthority(
            principal_id="opaque-local-owner",
            session_id="vault-test",
            capabilities=frozenset(),
            space_ids=None,
            owner=True,
        )
        self.facets = FacetTasks(self.engine)
        self.vault = FacetVaultTasks(self.engine)
        self.protocol: fc.FacetVaultTasks = self.vault
        self.sequence = 0
        self.engine.capture.accept(
            TextPayload("Original managed body"),
            delivery_id="vault.capture",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=self.engine.inbox.spaces()[0].space_id,
        )
        self.page_id = self.engine.retrieval.search(
            "Original managed body", record_type="canonical"
        )[0].result_id
        self.root = tmp_path / "vault"
        self.root.mkdir(mode=0o700)
        self.workspace_id = self.engine.managed_workspace.setup(
            str(self.root), operation_id="vault.setup"
        ).workspace_id
        self.path = next(self.root.rglob(f"{self.page_id}.md"))
        self.original = self.path.read_bytes()
        self.dimension = self.write("facet.dimension.create", expected_version=0, name="Area")[
            "subject_id"
        ]
        self.value = self.write(
            "facet.value.create",
            expected_version=0,
            dimension_id=self.dimension,
            expected_dimension_version=1,
            name="Work",
        )["subject_id"]

    def call(self, operation: str, **args: Any) -> dict[str, Any]:
        request = fc.request_from_wire(operation, {"dto_version": 1, **args})
        return cast(
            dict[str, Any],
            getattr(self.facets, fc.OPERATION_METHODS[operation])(
                request, authority=self.owner
            ).to_wire(),
        )

    def write(self, operation: str, **args: Any) -> dict[str, Any]:
        self.sequence += 1
        return self.call(operation, operation_id=f"vault.operation.{self.sequence}", **args)

    def register(self) -> None:
        self.vault.register_record(self.workspace_id, self.page_id, authority=self.owner)

    def preview(self) -> dict[str, Any]:
        return self.protocol.vault_preview(
            fc.VaultPreviewRequest(workspace_id=self.workspace_id, record_id=self.page_id),
            authority=self.owner,
        ).to_wire()

    def apply(self, preview: dict[str, Any], operation: str = "vault.apply") -> dict[str, Any]:
        return self.protocol.vault_apply(
            fc.VaultApplyRequest(preview_id=preview["preview_id"], operation_id=operation),
            authority=self.owner,
        ).to_wire()

    def rows(self, sql: str) -> list[sqlite3.Row]:
        connection = self.engine._store.connect()
        try:
            return list(connection.execute(sql))
        finally:
            connection.close()

    def edit_facets(self, names: str = '"Work"') -> None:
        self.path.write_bytes(
            self.path.read_bytes().replace(b"facets: {}", f'facets: {{"Area":[{names}]}}'.encode())
        )


def test_real_f20_apply_replay_and_f21_content_identity(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    brain.register()
    revision = brain.rows("SELECT accepted_revision_id FROM managed_notes")[0][0]
    brain.edit_facets()
    observation = brain.engine.managed_workspace.observe(brain.workspace_id)
    assert not observation.notes[0].changed
    preview = brain.preview()
    assert preview["conflicts"] == []
    assert preview["add_value_ids"] == [brain.value]
    applied = brain.apply(preview)
    assert applied["projection_status"] == "current"
    brain.now += timedelta(minutes=16)
    assert brain.apply(preview)["receipt"] == applied["receipt"]
    assert brain.rows("SELECT accepted_revision_id FROM managed_notes")[0][0] == revision
    assert len(brain.rows("SELECT * FROM managed_note_revisions")) == 1
    assert (
        content_without_registered_facets(brain.path.read_bytes(), registered=True)
        == brain.original
    )
    assert (
        brain.rows("SELECT actor_id FROM facet_events ORDER BY sequence DESC LIMIT 1")[0][0]
        == brain.engine.profile.owner_actor_id
    )
    exported = export_managed_workspace_state(brain.engine)
    assert exported is not None
    assert b"facetpreview_" not in exported[1]
    assert b"facet-navigation" not in exported[1]


def test_real_f20_expiry_missing_mapping_and_file_race(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    with pytest.raises(fc.FacetError, match="projection_conflict"):
        brain.preview()
    brain.register()
    brain.edit_facets()
    expired = brain.preview()
    brain.now += timedelta(minutes=15)
    with pytest.raises(fc.FacetError, match="preview_stale"):
        brain.apply(expired)
    changed = brain.preview()
    brain.path.write_bytes(brain.path.read_bytes() + b"owner edit")
    with pytest.raises(fc.FacetError, match="preview_stale"):
        brain.apply(changed)
    assert len(brain.rows("SELECT * FROM facet_vault_apply_operations")) == 0


def test_real_f21_combined_edit_requires_content_acceptance(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    brain.path.write_bytes(
        brain.path.read_bytes().replace(b"Original managed body", b"Accepted owner edit")
    )
    assert {item["code"] for item in brain.preview()["conflicts"]} == {"content_changed"}
    observation = brain.engine.managed_workspace.observe(brain.workspace_id)
    assert observation.notes[0].changed
    brain.engine.managed_workspace.accept_observed(
        brain.workspace_id,
        brain.page_id,
        generation=observation.generation,
        operation_id="vault.accept-content",
    )
    assert len(brain.rows("SELECT * FROM managed_note_revisions")) == 2
    assert brain.apply(brain.preview())["projection_status"] == "current"
    assert b"Accepted owner edit" in brain.path.read_bytes()
    assert all(
        b"facets:" not in row[0]
        for row in brain.rows("SELECT body_bytes FROM managed_note_revisions")
    )
    assert export_managed_workspace_state(brain.engine) is not None


def test_real_f21_index_preservation_and_navigation_custody(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    query = brain.write(
        "facet.query.create",
        expected_version=0,
        name="Composite",
        selector={
            "all_value_ids": [brain.value],
            "space_ids": [],
            "payload_families": [],
            "record_types": ["source", "canonical"],
        },
    )["subject_id"]
    subject = brain.call("facet.assignment.get", record_id=brain.page_id)["item"]
    brain.write(
        "facet.query.attach",
        expected_version=0,
        page_id=brain.page_id,
        expected_revision_id=subject["revision_id"],
        query_id=query,
        expected_query_version=1,
    )
    brain.register()
    assert b"facet-navigation" in brain.path.read_bytes()
    assert f"../Facets/{brain.dimension}/{brain.value}.md".encode() in brain.path.read_bytes()
    brain.vault.refresh_indexes(brain.workspace_id, authority=brain.owner)
    index = brain.root / facet_index_relative_path(brain.dimension, brain.value)
    assert brain.path.name.encode() in index.read_bytes()
    assert not brain.engine.managed_workspace.observe(brain.workspace_id).notes[0].changed
    exported = export_managed_workspace_state(brain.engine)
    assert exported is not None
    revisions = json.loads(exported[1])["notes"][0]["revisions"]
    assert all(base64.b64decode(row["body_base64"]) == brain.original for row in revisions)
    snapshot = brain.engine.managed_workspace.graph_snapshot(brain.workspace_id)
    assert len(snapshot.sources) == 1
    assert snapshot.sources[0].body.encode() == brain.original
    assert all(
        b"facet-navigation" not in row[0]
        for row in brain.rows("SELECT body_bytes FROM managed_note_revisions")
    )
    index.write_bytes(index.read_bytes() + b"Owner index content")
    with pytest.raises(fc.FacetError, match="projection_conflict"):
        brain.vault.refresh_indexes(brain.workspace_id, authority=brain.owner)
    assert index.read_bytes().endswith(b"Owner index content")
    with pytest.raises(ValueError, match="unresolved facet projection edit"):
        export_managed_workspace_state(brain.engine)


def _publish(brain: VaultBrain, version: str) -> bytes:
    from open_brain_engine.engine import DecisionOutcome, ProposalDraft

    engine = brain.engine
    capture = engine.capture.accept(
        TextPayload(f"Evidence {version}"),
        delivery_id=f"vault.source.{version}",
        space_id=engine.inbox.spaces()[0].space_id,
    )
    proposal = engine.review.propose(
        (capture.capture_id,),
        (ProposalDraft("Refresh publication", f"Approved {version}"),),
        delivery_id=f"vault.propose.{version}",
        target_page_id=brain.page_id,
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id=f"vault.approve.{version}",
        expected_review_digest=proposal.review_digest,
    )
    return next((engine.profile.root / "content/spaces").rglob(f"{brain.page_id}.md")).read_bytes()


def test_real_f21_canonical_refresh_uses_projected_preimage(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    brain.apply(brain.preview())
    canonical = _publish(brain, "next")
    brain.engine.managed_workspace.refresh(brain.workspace_id, operation_id="vault.refresh")
    assert content_without_registered_facets(brain.path.read_bytes(), registered=True) == canonical
    assert parse_markdown(brain.path.read_bytes()).fields["facets"] == {"Area": ["Work"]}
    assert len(brain.rows("SELECT * FROM managed_note_revisions")) == 2
    assert brain.rows("SELECT * FROM managed_conflicts") == []
    assert brain.preview()["conflicts"] == []
    assert export_managed_workspace_state(brain.engine) is not None


def test_real_f21_projection_recovers_after_write_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_engine.engine import facet_vault as module

    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    preview = brain.preview()
    # Force a different generated byte sequence so the filesystem boundary executes.
    brain.path.write_bytes(brain.path.read_bytes().replace(b'"Work"', b' "Work" '))
    preview = brain.preview()
    from open_brain_engine.storage.filesystem import atomic_replace

    original = atomic_replace

    def interrupted(**kwargs: Any) -> None:
        original(**kwargs)
        raise OSError("synthetic interruption after atomic replacement")

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_replace", interrupted)
        result = brain.apply(preview)
    assert result["projection_status"] == "refresh_required"
    assert len(brain.rows("SELECT * FROM facet_assignments WHERE present=1")) == 1
    assert len(brain.rows("SELECT * FROM facet_projection_writes WHERE status='prepared'")) == 1
    reopened = BrainEngine.open(brain.engine.profile, clock=lambda: brain.now)
    recovered = FacetVaultTasks(reopened)
    result2 = recovered.vault_apply(
        fc.VaultApplyRequest(preview_id=preview["preview_id"], operation_id="vault.apply"),
        authority=brain.owner,
    ).to_wire()
    assert result2["receipt"] == result["receipt"]
    assert result2["projection_status"] == "current"
    assert brain.rows("SELECT * FROM facet_projection_writes WHERE status!='completed'") == []
    assert len(brain.rows("SELECT * FROM managed_note_revisions")) == 1


def test_real_f20_rename_ambiguity_archived_add_and_missing_subtree(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    brain.apply(brain.preview())
    preview = brain.preview()
    brain.write("facet.value.rename", expected_version=1, value_id=brain.value, name="Renamed")
    with pytest.raises(fc.FacetError, match="preview_stale"):
        brain.apply(preview, "vault.rename-race")
    assert brain.preview()["conflicts"] == []
    reused = brain.write(
        "facet.value.create",
        expected_version=0,
        dimension_id=brain.dimension,
        expected_dimension_version=1,
        name="Work",
    )["subject_id"]
    assert brain.preview()["conflicts"] == [{"code": "ambiguous_name", "value_id": None}]
    brain.path.write_bytes(
        content_without_registered_facets(brain.path.read_bytes(), registered=True)
    )
    missing = brain.preview()
    assert missing["remove_value_ids"] == [brain.value]
    brain.apply(missing, "vault.remove")
    brain.write("facet.value.archive", expected_version=1, value_id=reused)
    brain.edit_facets()
    assert brain.preview()["conflicts"] == [{"code": "archived_addition", "value_id": reused}]


def test_real_unregistered_owner_facets_are_retained_as_content(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    brain.path.write_bytes(
        brain.original.replace(b"\n---\n", b'\nfacets: {"Owner":["Handwritten"]}\n---\n')
    )
    observation = brain.engine.managed_workspace.observe(brain.workspace_id)
    brain.engine.managed_workspace.accept_observed(
        brain.workspace_id,
        brain.page_id,
        generation=observation.generation,
        operation_id="vault.owner-facets",
    )
    payload = brain.path.read_bytes()
    with pytest.raises(fc.FacetError, match="projection_conflict"):
        brain.register()
    assert brain.path.read_bytes() == payload
    assert (
        b'facets: {"Owner"'
        in brain.rows("SELECT body_bytes FROM managed_note_revisions WHERE kind='edit'")[0][0]
    )


@pytest.mark.parametrize(
    "point", ["after_operation_prepared", "after_target_write", "after_operation_promoted"]
)
def test_real_managed_refresh_recovery_retains_projected_preimage(
    tmp_path: Path, point: str
) -> None:
    from open_brain_engine.engine import InjectedFault, ManagedWorkspaceFault

    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    brain.apply(brain.preview())
    canonical = _publish(brain, "recovery")
    fault = next(item for item in ManagedWorkspaceFault if item.value.endswith(point))
    interrupted = BrainEngine.open(brain.engine.profile, clock=lambda: brain.now, faults={fault})
    with pytest.raises(InjectedFault):
        interrupted.managed_workspace.refresh(
            brain.workspace_id, operation_id="vault.refresh-fault"
        )
    reopened = BrainEngine.open(brain.engine.profile, clock=lambda: brain.now)
    assert content_without_registered_facets(brain.path.read_bytes(), registered=True) == canonical
    assert parse_markdown(brain.path.read_bytes()).fields["facets"] == {"Area": ["Work"]}
    assert brain.rows("SELECT * FROM managed_conflicts") == []
    assert brain.rows("SELECT * FROM facet_projection_writes WHERE status='prepared'") == []
    assert export_managed_workspace_state(reopened) is not None


def test_real_projection_conflict_preserves_post_commit_owner_edit_and_can_settle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    preview = brain.preview()
    materialize = brain.vault._materialize

    def raced(operation_id: str) -> None:
        brain.path.write_bytes(brain.path.read_bytes() + b"\nOwner raced the projection\n")
        materialize(operation_id)

    with monkeypatch.context() as patch:
        patch.setattr(brain.vault, "_materialize", raced)
        result = brain.apply(preview)
    assert result["projection_status"] == "refresh_required"
    assert brain.apply(preview)["receipt"] == result["receipt"]
    assert brain.path.read_bytes().endswith(b"Owner raced the projection\n")
    assert len(brain.rows("SELECT * FROM facet_assignments WHERE present=1")) == 1
    with pytest.raises(ValueError, match="settled facet projections"):
        export_managed_workspace_state(brain.engine)
    observation = brain.engine.managed_workspace.observe(brain.workspace_id)
    brain.engine.managed_workspace.accept_observed(
        brain.workspace_id,
        brain.page_id,
        generation=observation.generation,
        operation_id="vault.accept-race",
    )
    brain.register()
    assert brain.apply(preview)["projection_status"] == "current"
    assert export_managed_workspace_state(brain.engine) is not None
    assert len(brain.rows("SELECT * FROM managed_note_revisions")) == 2


def test_real_apply_rejects_identity_authority_and_version_changes(tmp_path: Path) -> None:
    from dataclasses import replace

    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    preview = brain.preview()
    request = fc.VaultApplyRequest(preview_id=preview["preview_id"], operation_id="vault.apply")
    with pytest.raises(fc.FacetError, match="unsupported_capability"):
        brain.vault.vault_apply(request, authority=replace(brain.owner, owner=False))
    with pytest.raises(fc.FacetError, match="preview_stale"):
        brain.vault.vault_apply(
            request, authority=replace(brain.owner, principal_id="different-owner-session")
        )
    replacement = brain.path.with_suffix(".replacement")
    replacement.write_bytes(brain.path.read_bytes())
    replacement.replace(brain.path)
    with pytest.raises(fc.FacetError, match="preview_stale"):
        brain.apply(preview)
    fresh = brain.preview()
    item = brain.call("facet.assignment.get", record_id=brain.page_id)["item"]
    brain.write(
        "facet.assignment.patch",
        record_id=brain.page_id,
        expected_revision_id=item["revision_id"],
        expected_version=0,
        add_value_ids=[brain.value],
    )
    with pytest.raises(fc.FacetError, match="preview_stale"):
        brain.apply(fresh)
    assert brain.rows("SELECT * FROM facet_vault_apply_operations") == []


def test_real_managed_refresh_registers_records_without_content_revision(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    brain.engine.managed_workspace.refresh(
        brain.workspace_id, operation_id="vault.projection-refresh"
    )
    assert parse_markdown(brain.path.read_bytes()).fields["facets"] == {}
    assert brain.preview()["conflicts"] == []
    assert (brain.root / facet_index_relative_path(brain.dimension, brain.value)).exists()
    assert len(brain.rows("SELECT * FROM managed_note_revisions")) == 1


def test_real_apply_rolls_back_semantics_when_projection_custody_cannot_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    preview = brain.preview()
    before = len(brain.rows("SELECT * FROM facet_events"))

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic failure before custody commit")

    with monkeypatch.context() as patch:
        patch.setattr(brain.vault, "_prepare_record", interrupted)
        with pytest.raises(RuntimeError, match="before custody commit"):
            brain.apply(preview)
    assert brain.rows("SELECT * FROM facet_assignments") == []
    assert brain.rows("SELECT * FROM facet_vault_apply_operations") == []
    assert len(brain.rows("SELECT * FROM facet_events")) == before
    assert brain.apply(preview)["projection_status"] == "current"
    with pytest.raises(fc.FacetError, match="idempotency_conflict"):
        brain.apply(brain.preview())


def test_real_unknown_names_return_unique_conflicts(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets('"Unknown A", "Unknown B"')
    preview = brain.preview()
    assert preview["conflicts"] == [{"code": "unknown_name", "value_id": None}]
    with pytest.raises(fc.FacetError, match="preview_stale"):
        brain.apply(preview)
    assert brain.rows("SELECT * FROM facet_assignments") == []


def test_real_custodied_navigation_edit_cannot_enter_content_evidence(tmp_path: Path) -> None:
    from open_brain_engine.engine.contracts import ManagedWorkspaceFailure

    brain = VaultBrain(tmp_path)
    query = brain.write(
        "facet.query.create",
        expected_version=0,
        name="Composite",
        selector={"all_value_ids": [brain.value]},
    )["subject_id"]
    item = brain.call("facet.assignment.get", record_id=brain.page_id)["item"]
    brain.write(
        "facet.query.attach",
        expected_version=0,
        page_id=brain.page_id,
        expected_revision_id=item["revision_id"],
        query_id=query,
        expected_query_version=1,
    )
    brain.register()
    brain.path.write_bytes(
        brain.path.read_bytes().replace(b"- [Area: Work]", b"- [Owner changed navigation]")
    )
    modified = brain.path.read_bytes()
    assert brain.preview()["conflicts"] == [{"code": "invalid_frontmatter", "value_id": None}]
    with pytest.raises(ManagedWorkspaceFailure, match="invalid_observation"):
        brain.engine.managed_workspace.observe(brain.workspace_id)
    with pytest.raises(ValueError, match="unresolved facet projection edit"):
        export_managed_workspace_state(brain.engine)
    assert brain.path.read_bytes() == modified
    assert len(brain.rows("SELECT * FROM managed_note_revisions")) == 1


def test_real_refresh_conflict_retains_only_content_and_exact_file_guard(tmp_path: Path) -> None:
    from open_brain_engine.engine.contracts import ManagedWorkspaceFailure

    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    brain.apply(brain.preview())
    brain.path.write_bytes(brain.path.read_bytes() + b"Owner content branch\n")
    _publish(brain, "conflict")
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        brain.engine.managed_workspace.refresh(
            brain.workspace_id, operation_id="vault.refresh-conflict"
        )
    retained = brain.rows("SELECT candidate_body_bytes FROM managed_conflicts")[0][0]
    assert b"facets:" not in retained
    assert retained.endswith(b"Owner content branch\n")
    assert (
        brain.rows("SELECT file_sha256 FROM facet_projection_conflict_files")[0][0]
        == sha256(brain.path.read_bytes()).hexdigest()
    )
    brain.engine.managed_workspace.resolve_conflict(
        brain.workspace_id, brain.page_id, choice="workspace", operation_id="vault.resolve-content"
    )
    brain.register()
    assert export_managed_workspace_state(brain.engine) is not None
    assert all(
        b"facets:" not in row[0]
        for row in brain.rows("SELECT body_bytes FROM managed_note_revisions")
    )


def test_real_owner_append_after_navigation_keeps_only_content_in_history(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    query = brain.write(
        "facet.query.create",
        expected_version=0,
        name="Composite",
        selector={"all_value_ids": [brain.value]},
    )["subject_id"]
    item = brain.call("facet.assignment.get", record_id=brain.page_id)["item"]
    brain.write(
        "facet.query.attach",
        expected_version=0,
        page_id=brain.page_id,
        expected_revision_id=item["revision_id"],
        query_id=query,
        expected_query_version=1,
    )
    brain.register()
    owner_addition = b"\nOwner addition below navigation\n"
    brain.path.write_bytes(brain.path.read_bytes() + owner_addition)
    assert brain.preview()["conflicts"] == [{"code": "content_changed", "value_id": None}]
    observation = brain.engine.managed_workspace.observe(brain.workspace_id)
    brain.engine.managed_workspace.accept_observed(
        brain.workspace_id,
        brain.page_id,
        generation=observation.generation,
        operation_id="vault.append",
    )
    accepted = brain.rows("SELECT body_bytes FROM managed_note_revisions WHERE kind='edit'")[0][0]
    assert accepted == brain.original + owner_addition
    brain.register()
    assert brain.preview()["conflicts"] == []
    assert brain.path.read_bytes().count(b"<!-- open-brain:facet-navigation -->") == 1
    assert export_managed_workspace_state(brain.engine) is not None


def test_real_registered_file_swap_updates_custody_atomically(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    brain.register()
    preview = brain.preview()
    brain.engine.capture.accept(
        TextPayload("Second managed page"),
        delivery_id="vault.second",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=brain.engine.inbox.spaces()[0].space_id,
    )
    second_id = brain.engine.retrieval.search("Second managed page", record_type="canonical")[
        0
    ].result_id
    brain.engine.managed_workspace.refresh(brain.workspace_id, operation_id="vault.add-second")
    second_path = next(brain.root.rglob(f"{second_id}.md"))
    temporary = brain.path.with_suffix(".swap")
    brain.path.rename(temporary)
    second_path.rename(brain.path)
    temporary.rename(second_path)
    observed = brain.engine.managed_workspace.observe(brain.workspace_id)
    assert len(observed.notes) == 2
    assert all(not note.changed for note in observed.notes)
    assert {
        row["relative_path"]
        for row in brain.rows("SELECT * FROM facet_projection_state WHERE file_kind='record'")
    } == {note.relative_path for note in observed.notes}
    with pytest.raises(fc.FacetError, match="preview_stale"):
        brain.apply(preview)
    brain.path = second_path
    assert brain.preview()["conflicts"] == []


def test_real_replay_receipt_is_immutable_while_projection_status_changes(tmp_path: Path) -> None:
    brain = VaultBrain(tmp_path)
    brain.register()
    brain.edit_facets()
    preview = brain.preview()
    initial = brain.apply(preview)
    brain.write("facet.value.rename", expected_version=1, value_id=brain.value, name="Renamed")
    replay = brain.apply(preview)
    assert replay["receipt"] == initial["receipt"]
    assert replay["projection_status"] == "refresh_required"
    brain.register()
    assert brain.apply(preview)["projection_status"] == "current"
    assert parse_markdown(brain.path.read_bytes()).fields["facets"] == {"Area": ["Renamed"]}
    moved = brain.path.with_name("owner-renamed.md")
    brain.path.rename(moved)
    brain.engine.managed_workspace.observe(brain.workspace_id)
    brain.path = moved
    brain.register()
    assert brain.apply(preview)["projection_status"] == "current"


def test_real_recovery_retains_generated_custody_across_semantic_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_engine.engine import facet_vault as module
    from open_brain_engine.storage.filesystem import atomic_replace

    brain = VaultBrain(tmp_path)
    item = brain.call("facet.assignment.get", record_id=brain.page_id)["item"]
    brain.write(
        "facet.assignment.patch",
        record_id=brain.page_id,
        expected_revision_id=item["revision_id"],
        expected_version=0,
        add_value_ids=[brain.value],
    )

    def interrupted(**kwargs: Any) -> None:
        atomic_replace(**kwargs)
        raise OSError("synthetic crash after initial registration write")

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_replace", interrupted)
        with pytest.raises(fc.FacetError, match="operation_pending"):
            brain.register()
    assert brain.rows("SELECT * FROM facet_projection_state") == []
    brain.write("facet.value.rename", expected_version=1, value_id=brain.value, name="Renamed")
    reopened = BrainEngine.open(brain.engine.profile, clock=lambda: brain.now)
    assert len(brain.rows("SELECT * FROM facet_projection_state")) == 1
    assert parse_markdown(brain.path.read_bytes()).fields["facets"] == {"Area": ["Work"]}
    FacetVaultTasks(reopened).register_record(
        brain.workspace_id, brain.page_id, authority=brain.owner
    )
    assert parse_markdown(brain.path.read_bytes()).fields["facets"] == {"Area": ["Renamed"]}
    assert len(brain.rows("SELECT * FROM managed_note_revisions")) == 1


def test_real_accepted_projected_successor_cancels_interrupted_content_write(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine import InjectedFault, ManagedWorkspaceFault

    brain = VaultBrain(tmp_path)
    brain.register()
    interrupted = BrainEngine.open(
        brain.engine.profile,
        clock=lambda: brain.now,
        faults={ManagedWorkspaceFault.AFTER_OPERATION_PREPARED},
    )
    with pytest.raises(InjectedFault):
        interrupted.managed_workspace.materialize(
            brain.workspace_id, brain.page_id, operation_id="vault.pending-content"
        )
    brain.path.write_bytes(brain.path.read_bytes() + b"Accepted newer body\n")
    observation = brain.engine.managed_workspace.observe(brain.workspace_id)
    brain.engine.managed_workspace.accept_observed(
        brain.workspace_id,
        brain.page_id,
        generation=observation.generation,
        operation_id="vault.accept-successor",
    )
    expected = brain.path.read_bytes()
    BrainEngine.open(brain.engine.profile, clock=lambda: brain.now)
    assert brain.path.read_bytes() == expected
    assert (
        brain.rows(
            "SELECT status FROM managed_operations WHERE operation_id='vault.pending-content'"
        )[0][0]
        == "cancelled"
    )
    assert len(brain.rows("SELECT * FROM managed_note_revisions")) == 2
