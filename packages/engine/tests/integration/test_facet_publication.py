"""Pure v5 publication preparation; real-engine producer tests follow E."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine.facet_publication import (
    build_facet_binding,
    ordered_provenance,
    selected_capture_ids,
    verify_facet_snapshot,
)
from open_brain_engine.engine.review import ReviewOperations
from open_brain_engine.engine.review_bound import validate_current_binding
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.review_binding import review_binding_digest, validate_review_binding
from open_brain_engine.portable.v1 import PortableValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "facets"


def _fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / name).read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "name",
    [
        "positive/review-create-cross-space.json",
        "positive/review-unrestricted-record-types.json",
        "positive/review-update.json",
    ],
)
def test_v5_accepts_c0_fixture(name: str) -> None:
    binding = _fixture(name)
    assert validate_review_binding(binding) == binding


@pytest.mark.parametrize(
    "name",
    [
        "negative/v5-patch.json",
        "negative/unsorted-binding-memberships.json",
        "negative/create-attachment-version.json",
    ],
)
def test_v5_rejects_c0_fixture(name: str) -> None:
    with pytest.raises(PortableValidationError):
        validate_review_binding(_fixture(name))


@pytest.mark.parametrize("version", [3, 4])
def test_historical_bindings_still_reject_cross_space_sources(version: int) -> None:
    binding = _fixture("positive/review-update.json")
    for key in (
        "selection_kind", "destination_space_id", "query_id", "query_version",
        "query_selection_version", "selector", "attachment_version", "facet_source_states",
    ):
        binding.pop(key)
    binding["schema_version"] = version
    if version == 4:
        body = "synthetic body\n"
        binding["patch"] = {
            "base_body": body,
            "base_body_sha256": sha256(body.encode()).hexdigest(),
            "target_page_sha256": binding["expected_page_sha256"],
            "operations": [{"start_byte": 0, "end_byte": 0, "replacement": "x"}],
        }
    with pytest.raises(PortableValidationError, match="review source"):
        validate_review_binding(binding)


def test_v5_binding_digest_covers_selection_and_proposed_bytes() -> None:
    base = _fixture("positive/review-create-cross-space.json")
    for key in (
        "selection_kind", "destination_space_id", "query_id", "query_version",
        "query_selection_version", "selector", "attachment_version", "facet_source_states",
    ):
        base.pop(key)
    proposal = {"synthetic_content": "first"}
    fixture = _fixture("positive/review-create-cross-space.json")
    binding = build_facet_binding(
        proposal,
        base,
        destination_space_id=fixture["destination_space_id"],
        query_id=fixture["query_id"],
        query_version=fixture["query_version"],
        query_selection_version=fixture["query_selection_version"],
        selector=fixture["selector"],
        attachment_version=fixture["attachment_version"],
        facet_source_states=fixture["facet_source_states"],
    )
    assert binding["review_digest"] == review_binding_digest(proposal, binding)
    assert (
        review_binding_digest({"synthetic_content": "changed"}, binding)
        != binding["review_digest"]
    )
    changed = deepcopy(binding)
    changed["destination_space_id"] = fixture["source_states"][1]["space_id"]
    assert review_binding_digest(proposal, changed) != binding["review_digest"]


def test_v5_decision_and_commit_fail_closed_until_transactional_e_seam() -> None:
    binding = _fixture("positive/review-create-cross-space.json")
    with pytest.raises(ValueError, match="transactional snapshot"):
        validate_current_binding(object(), binding)
    with pytest.raises(ValueError, match="transactional attachment"):
        ReviewOperations._update_bound_page_head(
            cast(Any, object()), cast(Any, None),
            row=cast(Any, None), proposal=cast(Any, None), binding=binding,
            canonical_path="synthetic", published_sha256="0" * 64,
        )


def _snapshot(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": {
            "query_id": binding["query_id"],
            "archived": False,
            "version": binding["query_version"],
            "selection_version": binding["query_selection_version"],
            "selector": binding["selector"],
        },
        "attachment": None,
        "selected_heads": {
            state["source_id"]: {
                "capture_id": state["capture_id"],
                "head_version": state["head_version"],
                "memberships": {
                    item["value_id"]: item["membership_version"]
                    for item in state["memberships"]
                },
            }
            for state in binding["facet_source_states"]
        },
        "provenance_states": {
            state["capture_id"]: {
                key: state[key] for key in ("sha256", "route_id", "space_id")
            }
            for state in binding["source_states"]
        },
        "authorized_capture_ids": frozenset(binding["provenance"]),
        "authority": EffectiveAuthority(
            principal_id="synthetic-owner", session_id="synthetic-session",
            capabilities=frozenset(), space_ids=None, owner=True,
        ),
        "target": None,
    }


def test_snapshot_ignores_display_rename_new_match_and_unrelated_membership() -> None:
    binding = _fixture("positive/review-create-cross-space.json")
    current = _snapshot(binding)
    current["query"]["version"] += 1
    current["query"]["name"] = "Renamed query"
    current["selected_heads"]["source_extra"] = {"new_match": True}
    head = next(iter(current["selected_heads"].values()))
    head["memberships"]["facetval_unrelated"] = 1
    verify_facet_snapshot(binding, **current)


@pytest.mark.parametrize("change", ["membership", "head", "route", "query", "authority"])
def test_snapshot_rejects_relevant_drift(change: str) -> None:
    binding = _fixture("positive/review-create-cross-space.json")
    current = _snapshot(binding)
    head = next(iter(current["selected_heads"].values()))
    if change == "membership":
        value_id = binding["selector"]["all_value_ids"][0]
        head["memberships"][value_id] += 2  # remove and restore
    elif change == "head":
        head["head_version"] += 1
    elif change == "route":
        capture_id = binding["provenance"][0]
        current["provenance_states"][capture_id]["route_id"] = None
    elif change == "query":
        current["query"]["selection_version"] += 1
    else:
        current["authorized_capture_ids"] = frozenset()
    with pytest.raises(ValueError, match="conflict"):
        verify_facet_snapshot(binding, **current)


def test_update_attachment_and_target_are_precise() -> None:
    binding = _fixture("positive/review-update.json")
    current = _snapshot(binding)
    current["attachment"] = {
        "page_id": binding["page_id"], "query_id": binding["query_id"],
        "version": binding["attachment_version"],
    }
    current["target"] = {
        "space_id": binding["destination_space_id"],
        "publication_id": binding["expected_publication_id"],
        "sha256": binding["expected_page_sha256"],
    }
    verify_facet_snapshot(binding, **current)
    current["attachment"]["version"] += 1
    with pytest.raises(ValueError, match="target conflict"):
        verify_facet_snapshot(binding, **current)


def test_selection_and_cumulative_provenance_are_bounded() -> None:
    assert ordered_provenance(("old", "shared"), ("shared", "new")) == (
        "old", "shared", "new"
    )
    assert selected_capture_ids(({"capture_id": "one", "source_id": "source-one"},)) == (
        "one",
    )
    with pytest.raises(ValueError, match="selected no sources"):
        selected_capture_ids(())
    with pytest.raises(ValueError, match="limit exceeded"):
        selected_capture_ids(
            tuple({"capture_id": str(n), "source_id": str(n)} for n in range(33))
        )
    with pytest.raises(ValueError, match="limit exceeded"):
        ordered_provenance(tuple(str(n) for n in range(32)), ("new",))
