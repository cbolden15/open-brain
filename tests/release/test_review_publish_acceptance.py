from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import cast

from tools.open_brain_dev.review_publish_acceptance import main

ROOT = Path(__file__).parents[2]


def _source_executable(path: Path) -> Path:
    source_roots = (ROOT / "packages/app/src", ROOT / "packages/engine/src")
    path.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import sys",
                f"sys.path[:0] = {[str(root) for root in source_roots]!r}",
                "from open_brain.services.local_entrypoints import run_cli",
                "raise SystemExit(run_cli())",
                "",
            )
        ),
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_source_executable_passes_required_review_publication_checks(tmp_path: Path) -> None:
    executable = _source_executable(tmp_path / "open-brain")
    report_path = tmp_path / "private/report.json"

    assert main(("--executable", str(executable), "--output", str(report_path))) == 0

    report = cast(dict[str, object], json.loads(report_path.read_bytes()))
    checks = cast(list[dict[str, object]], report["checks"])
    evidence = cast(dict[str, object], report["evidence"])
    assert report["status"] == "passed"
    assert report["executable_sha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert checks and all(check["status"] == "passed" for check in checks)
    assert evidence["selected_capture_count"] == 7
    assert evidence["canonical_page_count"] == 3
    assert evidence["skipped_mandatory_checks"] == 0
    assert Path(cast(str, report["synthetic_root"])).is_relative_to(tmp_path)


def test_candidate_failure_is_reported_and_returns_nonzero(tmp_path: Path) -> None:
    executable = tmp_path / "broken-open-brain"
    executable.write_text("#!/bin/sh\nexit 19\n", encoding="utf-8")
    executable.chmod(0o700)
    report_path = tmp_path / "private/failure.json"

    assert main(("--executable", str(executable), "--output", str(report_path))) == 1

    report = cast(dict[str, object], json.loads(report_path.read_bytes()))
    checks = cast(list[dict[str, object]], report["checks"])
    assert report["status"] == "failed"
    assert any(check["status"] == "failed" for check in checks)
    assert any(check["status"] == "skipped" for check in checks)
    assert len(checks) == report["mandatory_check_count"]
    assert report["failed_checks"]
