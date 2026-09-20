from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

from open_brain_engine.portable.v5 import V5_SIDECAR_PATHS

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
    synthetic_root = Path(cast(str, report["synthetic_root"]))
    assert synthetic_root.is_relative_to(tmp_path)
    manifest = json.loads(
        (synthetic_root / "verified-export/portable-manifest.json").read_bytes()
    )
    assert manifest["schema_version"] == 5
    export = synthetic_root / "verified-export"
    assert all((export / relative).is_file() for relative in V5_SIDECAR_PATHS)
    assert not any(".open-brain" in path.parts for path in export.rglob("*"))
    assert not any(path.suffix in {".sqlite", ".sqlite3"} for path in export.rglob("*"))
    with sqlite3.connect(synthetic_root / "brain/.open-brain/state/phase1.sqlite3") as state:
        assert state.execute("PRAGMA user_version").fetchone()[0] == 9


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
