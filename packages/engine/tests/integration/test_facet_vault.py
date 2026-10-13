"""Preparation checks for vault rendering and reconciliation, not engine persistence."""

from __future__ import annotations

import sqlite3
from hashlib import sha256

import pytest
from open_brain_engine.engine.facet_vault import (
    FacetProjectionConflict,
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
            "facet_projection_state",
            "facet_projection_writes",
            "facet_projection_conflict_files",
            "facet_vault_previews",
            "facet_vault_apply_operations",
        }
    finally:
        connection.close()
