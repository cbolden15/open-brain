"""Acceptance proof validation uses synthetic disposable stores only."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from tools.open_brain_dev import harness_acceptance as acceptance


@pytest.fixture(scope="module")
def fixture_report(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("harness-acceptance").resolve()
    report = root / "report.json"
    summary = acceptance.run_fixture(report_path=report, evidence_dir=root / "evidence")
    assert summary["pair_count"] == 9
    assert summary["goal_complete"] is False
    return report


def _changed_report(tmp_path: Path, source: Path, mutate: Any) -> Path:
    report = acceptance._load(source)
    mutate(report)
    path = tmp_path / "changed.json"
    acceptance._write(path, report)
    return path


def test_fixture_runs_nine_real_public_interface_pairs(fixture_report: Path) -> None:
    assert acceptance.validate_fixture(report_path=fixture_report)["status"] == "passed"
    report = acceptance._load(fixture_report)
    assert {(row["source"], row["destination"]) for row in report["pairs"]} == {
        (source, destination) for source in acceptance.CLIENTS for destination in acceptance.CLIENTS
    }
    assert len({row["fresh_session_id"] for row in report["pairs"]}) == 9
    assert report["incomplete_required_criteria"] == list(acceptance.INCOMPLETE)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda r: r.update(mode="native"), "mode_mismatch"),
        (lambda r: r.update(goal_complete=True), "promoted_claim"),
        (lambda r: r.update(incomplete_required_criteria=[]), "promoted_claim"),
        (lambda r: r.update(source_commit="0" * 40), "source_binding_mismatch"),
        (lambda r: r.update(source_tree_sha256="0" * 64), "source_binding_mismatch"),
        (lambda r: r.update(fixture_runtime_sha256="0" * 64), "runtime_binding_mismatch"),
        (
            lambda r: r.update(finished_at=(datetime.now(UTC) - timedelta(days=2)).isoformat()),
            "stale_or_invalid_run",
        ),
        (lambda r: r.update(pairs=r["pairs"][:-1]), "missing_pairs"),
        (lambda r: r.update(sources=r["sources"][:-1]), "missing_sources"),
        (
            lambda r: r["sources"][1].update(session_id=r["sources"][0]["session_id"]),
            "duplicate_source_identity",
        ),
        (lambda r: r["pairs"][0].pop("observation"), "invalid_or_missing_evidence"),
        (lambda r: r["pairs"].__setitem__(1, r["pairs"][0]), "duplicate_or_invalid_pair"),
        (
            lambda r: r["pairs"][0].update(fresh_session_id=r["sources"][0]["session_id"]),
            "destination_session_not_fresh",
        ),
        (lambda r: r["pairs"][0].update(capture_id="wrong"), "capture_binding_mismatch"),
        (lambda r: r["pairs"][0].update(recall_seconds=3.01), "recall_deadline_failed"),
        (lambda r: r["sources"][0].update(enqueue_seconds=2.01), "enqueue_deadline_failed"),
        (lambda r: r["sources"][0]["receipt"].update(sha256="0" * 64), "evidence_digest_mismatch"),
        (lambda r: r["scenarios"].update(required_nodeids=[]), "missing_scenarios"),
        (lambda r: r["scenarios"].update(returncode=1), "missing_scenarios"),
        (lambda r: r["scenarios"].update(command=["true"]), "scenario_command_mismatch"),
    ],
)
def test_validator_rejects_missing_forged_stale_duplicate_and_mismatched_evidence(
    fixture_report: Path,
    tmp_path: Path,
    mutation: Any,
    code: str,
) -> None:
    changed = _changed_report(tmp_path, fixture_report, mutation)
    with pytest.raises(acceptance.AcceptanceError, match=code):
        acceptance.validate_fixture(report_path=changed)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda r: r["recall"].update(results=[]), "missing_observed_results"),
        (lambda r: r["search"].update(isError=True), "tool_failed"),
        (
            lambda r: r["source"]["structuredContent"]["results"][0].update(
                source_context="invented"
            ),
            "source_context_mismatch",
        ),
        (
            lambda r: r["recall"]["results"][0].update(citation='{"harness":"pi"}'),
            "citation_binding_mismatch",
        ),
    ],
)
def test_rehashed_observations_still_require_actual_results_and_source_bindings(
    fixture_report: Path,
    tmp_path: Path,
    mutation: Any,
    code: str,
) -> None:
    report = acceptance._load(fixture_report)
    observation = acceptance._load(Path(report["pairs"][0]["observation"]["path"]))
    mutation(observation)
    directory = tmp_path / "evidence"
    directory.mkdir(mode=0o700)
    # Preserve all other artifacts; each reference is copied into a new isolated bundle.
    original = Path(report["evidence_dir"])
    for file in original.iterdir():
        destination = directory / file.name
        destination.write_bytes(file.read_bytes())
        destination.chmod(0o600)
    for source in report["sources"]:
        for kind in ("request", "receipt"):
            source[kind]["path"] = str(directory / Path(source[kind]["path"]).name)
    for pair in report["pairs"]:
        pair["observation"]["path"] = str(directory / Path(pair["observation"]["path"]).name)
    path = Path(report["pairs"][0]["observation"]["path"])
    path.write_bytes(acceptance._canonical(observation))
    report["pairs"][0]["observation"]["sha256"] = sha256(path.read_bytes()).hexdigest()
    report["evidence_dir"] = str(directory)
    report_path = tmp_path / "changed.json"
    acceptance._write(report_path, report)
    with pytest.raises(acceptance.AcceptanceError, match=code):
        acceptance.validate_fixture(report_path=report_path)


def test_missing_and_skipped_mandatory_scenarios_are_rejected() -> None:
    for raw in (
        b"<testsuites/>",
        b'<testsuite><testcase name="one"><skipped/></testcase></testsuite>',
    ):
        with pytest.raises(acceptance.AcceptanceError):
            acceptance._junit_checks(raw)


def test_source_binding_includes_active_test_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    before = acceptance._source_binding()
    original = Path.read_bytes
    conftest = acceptance.ROOT / "packages/app/tests/conftest.py"

    def changed_bytes(path: Path) -> bytes:
        raw = original(path)
        return raw + b"\n# synthetic configuration change\n" if path == conftest else raw

    monkeypatch.setattr(Path, "read_bytes", changed_bytes)
    after = acceptance._source_binding()
    assert before[0] == after[0]
    assert before[1] != after[1]


def test_missing_parametrized_case_and_duplicate_case_are_rejected() -> None:
    rows = []
    for selector in acceptance.SCENARIOS:
        file, name = selector.split("::")
        classname = file.removesuffix(".py").replace("/", ".")
        rows.append(f'<testcase classname="{classname}" name="{name}"/>')
    raw = ("<testsuite>" + "".join(rows) + "</testsuite>").encode()
    with pytest.raises(acceptance.AcceptanceError, match="missing_scenario_parametrizations"):
        acceptance._junit_checks(raw, set(acceptance.SCENARIOS) | {"missing[parameter]"})
    repeated = ("<testsuite>" + "".join([*rows, rows[0]]) + "</testsuite>").encode()
    with pytest.raises(acceptance.AcceptanceError, match="duplicate_scenario"):
        acceptance._junit_checks(repeated)


def test_fixture_cannot_be_promoted_by_native_flag(
    fixture_report: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert acceptance.main(["--mode", "native", "--validate", "--report", str(fixture_report)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["error_code"] == "mode_mismatch"
    assert "SyntheticEditor" not in json.dumps(output)


def test_missing_native_collector_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing(name: str) -> Any:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    assert (
        acceptance.main(
            [
                "--mode",
                "native",
                "--report",
                str(tmp_path / "report.json"),
                "--evidence-dir",
                str(tmp_path / "evidence"),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error_code"] == "native_collection_unavailable"
    assert not (tmp_path / "report.json").exists()


def test_cli_errors_never_print_paths_or_parser_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "private-fixture.json"
    path.write_text('{"private-transcript":bad}')
    path.chmod(0o600)
    assert acceptance.main(["--mode", "fixture", "--validate", "--report", str(path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error_code"] == "invalid_evidence_json"
    assert "private-transcript" not in output
    assert str(tmp_path) not in output


def test_ambiguous_json_and_unbounded_or_symlink_evidence_fail(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text('{"mode":"fixture","mode":"native"}')
    path.chmod(0o600)
    with pytest.raises(acceptance.AcceptanceError, match="duplicate_json_key"):
        acceptance._load(path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(acceptance.AcceptanceError, match="unsafe_evidence_path"):
        acceptance._load(link)
    path.write_bytes(b"x" * (acceptance.MAX_JSON + 1))
    with pytest.raises(acceptance.AcceptanceError, match="unsafe_or_oversized_evidence"):
        acceptance._load(path)
