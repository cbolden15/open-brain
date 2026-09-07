from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

from tools.phase4.move_manifest import (
    Finding,
    load_manifest,
    render_import_report,
    render_move_report,
    validate_manifest,
)

ROOT = Path(__file__).parents[2]
MANIFEST_PATH = ROOT / "docs/v0-package-classification.json"
WORKSTREAM = (
    ROOT / "docs/ai/workstreams/"
    "20260901-open-brain-public-execute-goal-63-through-phase-4-p4a-p4b-and-p4c-"
    "with-independently-reviewed-pack-8a3f9b"
)
MOVE_REPORT = WORKSTREAM / "P4-W0-MOVE-REPORT.md"
IMPORT_REPORT = WORKSTREAM / "P4-W0-IMPORT-REPORT.md"


def _manifest() -> dict[str, object]:
    return load_manifest(MANIFEST_PATH)


def _codes(findings: list[Finding]) -> set[str]:
    return {finding.code for finding in findings}


def _runtime(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], manifest["files"])


def _subjects(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    phase4 = cast(dict[str, object], manifest["phase4"])
    return cast(dict[str, dict[str, object]], phase4["subjects"])


def test_canonical_move_manifest_is_complete_and_valid() -> None:
    manifest = _manifest()

    assert validate_manifest(ROOT, manifest) == []
    assert len(_runtime(manifest)) == 279
    subjects = _subjects(manifest)
    assert sum(record["kind"] == "test" for record in subjects.values()) == 279
    assert sum(record["kind"] in {"schema", "fixture"} for record in subjects.values()) == 65


def test_product_split_authority_is_packaged_with_the_app_sdist() -> None:
    subjects = _subjects(_manifest())
    for path in (
        "docs/acceptance/five-minute-install.md",
        "docs/plans/product-roadmap.md",
        "docs/product-family.md",
    ):
        assert subjects[path]["artifact_disposition"] == ["app-sdist"]


def test_p4a_has_no_unresolved_movement_or_monolith_tree() -> None:
    manifest = _manifest()

    assert not (ROOT / "src/open_brain").exists()
    assert all(record["movement_state"] == "moved" for record in _runtime(manifest).values())
    assert all(record["movement_state"] == "moved" for record in _subjects(manifest).values())


def test_project_instructions_name_the_current_artifact_policy_tool() -> None:
    instructions = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "src/open_brain/dev/artifact_policy.py" not in instructions
    assert "tools/open_brain_dev/artifact_policy.py" in instructions
    assert (ROOT / "tools/open_brain_dev/artifact_policy.py").is_file()


def test_generated_move_and_import_reports_are_exact() -> None:
    manifest = _manifest()

    assert MOVE_REPORT.read_text(encoding="utf-8") == render_move_report(manifest)
    assert IMPORT_REPORT.read_text(encoding="utf-8") == render_import_report(manifest)


def test_validator_rejects_missing_and_stale_subjects() -> None:
    missing = deepcopy(_manifest())
    del _subjects(missing)["tests/conftest.py"]
    stale = deepcopy(_manifest())
    source = deepcopy(_subjects(stale)["tests/conftest.py"])
    source["current_path"] = "tests/removed.py"
    _subjects(stale)["tests/removed.py"] = source

    assert "P4M002" in _codes(validate_manifest(ROOT, missing))
    assert "P4M003" in _codes(validate_manifest(ROOT, stale))


def test_validator_requires_explicit_consistent_movement_state() -> None:
    missing = deepcopy(_manifest())
    del _runtime(missing)["core/models.py"]["movement_state"]
    invalid = deepcopy(_manifest())
    _runtime(invalid)["core/models.py"]["movement_state"] = "copied"
    inconsistent = deepcopy(_manifest())
    record = _runtime(inconsistent)["core/models.py"]
    record["movement_state"] = "moved"
    record["current_path"] = "src/open_brain/core/models.py"

    assert "P4M011" in _codes(validate_manifest(ROOT, missing))
    assert "P4M011" in _codes(validate_manifest(ROOT, invalid))
    assert "P4M011" in _codes(validate_manifest(ROOT, inconsistent))


def test_p4a_entry_points_are_moved_to_their_owning_distributions() -> None:
    entry_points = {
        path: record
        for path, record in _subjects(_manifest()).items()
        if record["kind"] == "entry-point"
    }

    connector_key = (
        "packages/connectors/pyproject.toml#project.entry-points.open_brain.connectors.v1.youtube"
    )

    assert connector_key in entry_points
    assert entry_points[connector_key]["target_distribution"] == "connectors"
    assert all(
        record["target_distribution"] == "app"
        for path, record in entry_points.items()
        if path != connector_key
    )
    assert all(record["movement_state"] == "moved" for record in entry_points.values())
    assert all(record["current_path"] == record["target_path"] for record in entry_points.values())


def test_validator_rejects_duplicate_and_out_of_distribution_destinations() -> None:
    duplicate = deepcopy(_manifest())
    runtime = _runtime(duplicate)
    runtime["core/models.py"]["target_path"] = runtime["core/ids.py"]["target_path"]
    outside = deepcopy(_manifest())
    _runtime(outside)["core/models.py"]["target_path"] = "packages/app/src/open_brain/models.py"

    assert "P4M004" in _codes(validate_manifest(ROOT, duplicate))
    assert "P4M005" in _codes(validate_manifest(ROOT, outside))


def test_validator_rejects_unresolved_import_and_old_path_dispositions() -> None:
    unresolved = deepcopy(_manifest())
    rewrite = cast(dict[str, object], _runtime(unresolved)["core/models.py"]["import_rewrite"])
    rewrite["to"] = ""
    old_path = deepcopy(_manifest())
    _runtime(old_path)["core/models.py"]["old_import_disposition"] = "pending"

    assert "P4M006" in _codes(validate_manifest(ROOT, unresolved))
    assert "P4M008" in _codes(validate_manifest(ROOT, old_path))


def test_validator_rejects_forbidden_graph_edges_and_shipping_leaks() -> None:
    graph = deepcopy(_manifest())
    phase4 = cast(dict[str, object], graph["phase4"])
    dependency_graph = cast(dict[str, list[str]], phase4["runtime_dependency_graph"])
    dependency_graph["engine"] = ["app"]
    leaked = deepcopy(_manifest())
    legacy = next(record for record in _runtime(leaked).values() if record.get("owner") == "legacy")
    legacy["artifact_disposition"] = ["app-wheel"]

    assert "P4M007" in _codes(validate_manifest(ROOT, graph))
    assert "P4M009" in _codes(validate_manifest(ROOT, leaked))


def test_validator_rejects_unowned_tests_and_identity_mismatch() -> None:
    unowned = deepcopy(_manifest())
    _subjects(unowned)["tests/conftest.py"]["test_owner"] = "unknown"
    mismatched = deepcopy(_manifest())
    phase4 = cast(dict[str, object], mismatched["phase4"])
    identity = cast(dict[str, object], phase4["release_identity"])
    identity["candidate_version"] = "9.9.9"

    assert "P4M011" in _codes(validate_manifest(ROOT, unowned))
    assert "P4M010" in _codes(validate_manifest(ROOT, mismatched))


def test_manifest_json_is_deterministically_formatted() -> None:
    manifest = _manifest()
    expected = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    assert MANIFEST_PATH.read_text(encoding="utf-8") == expected
