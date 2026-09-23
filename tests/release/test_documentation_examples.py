from __future__ import annotations

from pathlib import Path

import pytest
from open_brain_engine.engine import PHASE1_STATE_SCHEMA_VERSION

from open_brain.services.local_runtime_session import RUNTIME_SESSION_VERSION
from tools.open_brain_dev.documentation_examples import (
    COMPATIBILITY_COORDINATES,
    DOCTOR_IDS,
    FIRST_USE_IDS,
    DocumentationExampleError,
    Example,
    execute_examples,
    extract_examples,
    read_readme_mcp_configs,
    require_examples,
    validate_compatibility_matrix,
    validate_example_contract,
)


def test_repository_guides_have_the_frozen_execution_inventory() -> None:
    root = Path(__file__).resolve().parents[2]
    first_use = extract_examples(root / "docs/first-use.md")
    doctor = extract_examples(root / "docs/doctor.md")

    require_examples(first_use, FIRST_USE_IDS)
    require_examples(doctor, DOCTOR_IDS)
    assert all(example.body.strip() and len(example.sha256) == 64 for example in first_use + doctor)


def test_command_mutation_changes_identity_and_fails_execution(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "<!-- open-brain-example:journey -->\n```sh\ntrue\n```\n",
        encoding="utf-8",
    )
    original = extract_examples(guide)
    guide.write_text(
        "<!-- open-brain-example:journey -->\n```sh\nfalse\n```\n",
        encoding="utf-8",
    )
    mutated = extract_examples(guide)

    assert original[0].identifier == mutated[0].identifier == "journey"
    assert original[0].sha256 != mutated[0].sha256

    execute_examples(original, cwd=tmp_path, environment={"PATH": "/usr/bin:/bin"}, timeout=5)
    with pytest.raises(DocumentationExampleError, match="exit 1"):
        execute_examples(
            (Example("journey", "false"),),
            cwd=tmp_path,
            environment={"PATH": "/usr/bin:/bin"},
            timeout=5,
        )


def test_missing_reordered_and_duplicate_blocks_fail_closed(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "<!-- open-brain-example:a -->\n```sh\ntrue\n```\n"
        "<!-- open-brain-example:b -->\n```sh\nfalse\n```\n",
        encoding="utf-8",
    )
    examples = extract_examples(guide)
    with pytest.raises(DocumentationExampleError, match="inventory drift"):
        require_examples(examples, ("b", "a"))
    with pytest.raises(DocumentationExampleError, match="inventory drift"):
        require_examples(examples, ("a", "b", "c"))

    guide.write_text(
        "<!-- open-brain-example:a -->\n```sh\ntrue\n```\n"
        "<!-- open-brain-example:a -->\n```sh\ntrue\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(DocumentationExampleError, match="duplicate"):
        extract_examples(guide)


def test_strict_guide_assertions_cover_outputs_ids_and_continuation() -> None:
    root = Path(__file__).resolve().parents[2]
    body = "\n".join(example.body for example in extract_examples(root / "docs/first-use.md"))

    for field in ("capture_id", "space_id", "proposal_id", "review_token", "page_id"):
        assert f".{field}" in body
    assert "next_cursor" in body
    assert "READ_CHUNKS" in body and 'test "$READ_CHUNKS" -gt 1' in body
    assert 'cmp "$EXPECTED_PROJECTED" "$RECONSTRUCTED"' in body
    assert "Zażółć gęślą jaźń 🌌" in body
    assert "unknown tool" in body


def test_output_truncation_and_false_success_are_rejected(tmp_path: Path) -> None:
    environment = {"PATH": "/usr/bin:/bin"}
    truncation = Example(
        "truncated",
        "VALUE='partial'; test \"$VALUE\" = 'complete Unicode output 🌌'",
    )
    false_success = Example("false-success", 'STATUS=failed; test "$STATUS" = ok')

    for example in (truncation, false_success):
        with pytest.raises(DocumentationExampleError, match="exit 1"):
            execute_examples((example,), cwd=tmp_path, environment=environment, timeout=5)


def test_successful_early_exit_cannot_claim_later_blocks(tmp_path: Path) -> None:
    examples = (
        Example("first-use-environment", "exit 0"),
        Example("doctor-search-index", "true"),
    )

    with pytest.raises(DocumentationExampleError, match="example completion drift"):
        execute_examples(
            examples,
            cwd=tmp_path,
            environment={"PATH": "/usr/bin:/bin"},
            timeout=5,
        )


def test_noop_doctor_and_reduced_agent_grants_fail_contract_validation() -> None:
    root = Path(__file__).resolve().parents[2]
    examples = extract_examples(root / "docs/first-use.md") + extract_examples(
        root / "docs/doctor.md"
    )
    doctor_mutant = tuple(
        Example(example.identifier, ":") if example.identifier == "doctor-search-index" else example
        for example in examples
    )
    with pytest.raises(DocumentationExampleError, match="doctor-search-index"):
        validate_example_contract(doctor_mutant)

    removed = (
        " --allow-search --allow-content-read --allow-history-read --allow-inbox-read"
        " --allow-organize --allow-review-read --allow-review-propose --allow-review-decide"
    )
    agent_mutant = tuple(
        Example(example.identifier, example.body.replace(removed, ""))
        if example.identifier == "agent-setup-lifecycle"
        else example
        for example in examples
    )
    with pytest.raises(DocumentationExampleError, match="agent grant coverage drift"):
        validate_example_contract(agent_mutant)

    widened_agent = tuple(
        Example(
            example.identifier,
            example.body.replace(
                "--allow-review-decide --runtime",
                "--allow-review-decide --allow-workspace-read --runtime",
            ),
        )
        if example.identifier == "agent-setup-lifecycle"
        else example
        for example in examples
    )
    with pytest.raises(DocumentationExampleError, match="agent grant coverage drift"):
        validate_example_contract(widened_agent)


def test_compatibility_values_are_bound_to_named_rows() -> None:
    root = Path(__file__).resolve().parents[2]
    matrix = (root / "docs/core-v01-features.md").read_text(encoding="utf-8")
    validate_compatibility_matrix(matrix)

    runtime_mutant = matrix.replace("| Runtime session | `5` |", "| Runtime session | `999` |")
    with pytest.raises(DocumentationExampleError, match="compatibility matrix drift"):
        validate_compatibility_matrix(runtime_mutant)

    catalog_mutant = matrix.replace("| Catalog schema | `2` |", "| Catalog schema | `999` |")
    with pytest.raises(DocumentationExampleError, match="compatibility matrix drift"):
        validate_compatibility_matrix(catalog_mutant)


def test_documentation_and_desktop_bind_the_current_runtime_compatibility() -> None:
    root = Path(__file__).resolve().parents[2]
    assert COMPATIBILITY_COORDINATES["Local state schema"] == str(
        PHASE1_STATE_SCHEMA_VERSION
    )
    assert COMPATIBILITY_COORDINATES["Runtime session"] == str(RUNTIME_SESSION_VERSION)

    desktop_runtime = (root / "packages/desktop/src-tauri/src/runtime.rs").read_text(
        encoding="utf-8"
    )
    assert (
        f"const SUPPORTED_STATE_SCHEMA: u64 = {PHASE1_STATE_SCHEMA_VERSION};"
        in desktop_runtime
    )
    assert (
        f"const SUPPORTED_RUNTIME_SESSION: u64 = {RUNTIME_SESSION_VERSION};"
        in desktop_runtime
    )


def test_readme_mcp_examples_reject_missing_widened_and_reordered_cases() -> None:
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    read_readme_mcp_configs(readme)

    widened = readme.replace(
        '["mcp", "--allow-search"]',
        '["mcp", "--allow-search", "--allow-review-decide"]',
    )
    missing = readme.replace("### Search only", "### Search template")
    first = readme.index("### Capture only")
    second = readme.index("### Search only")
    third = readme.index("### Capture and search")
    end = readme.index("`brain_capture` accepts", third)
    reordered = (
        readme[:first]
        + readme[second:third]
        + readme[first:second]
        + readme[third:end]
        + readme[end:]
    )
    for mutant in (widened, missing, reordered):
        with pytest.raises(DocumentationExampleError, match="README MCP configuration drift"):
            read_readme_mcp_configs(mutant)
