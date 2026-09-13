from __future__ import annotations

from pathlib import Path

from open_brain_legacy.cli.ledger import scan, stage
from open_brain_legacy.ledger.scan import LedgerSourceManifest


def test_stage_cli_is_transcript_safe_and_reports_missing_key_without_writing(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    canary = "TRANSCRIPT_CANARY_MUST_NOT_REACH_STAGE_CLI"
    (source_root / "source.md").write_text(
        "Synthetic body\n<!-- open-brain:transcript -->\n"
        + canary
        + "\n<!-- /open-brain:transcript -->\n"
    )
    manifest = scan(root=source_root).value
    assert isinstance(manifest, LedgerSourceManifest)
    scratch_root = tmp_path / "scratch"

    staged = stage(
        manifest=manifest,
        key=manifest.entries[0].key,
        source_root=source_root,
        scratch_root=scratch_root,
        dry_run=False,
    )
    missing = stage(
        manifest=manifest,
        key="source_" + "0" * 64,
        source_root=source_root,
        scratch_root=tmp_path / "missing",
        dry_run=False,
    )

    assert staged.exit_code == 0
    assert staged.envelope == {
        "command": "ledger.stage",
        "staged_digest_sha256": staged.envelope["staged_digest_sha256"],
        "status": "staged",
    }
    assert canary not in staged.to_json()
    staged_input = scratch_root / "staged" / manifest.entries[0].key / "input.md"
    assert canary not in staged_input.read_text()
    assert missing.exit_code == 1
    assert missing.envelope == {"command": "ledger.stage", "status": "missing"}
    assert not (tmp_path / "missing").exists()


def test_stage_cli_dry_run_does_not_write_or_expose_manifest_details(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    canary = "TRANSCRIPT_CANARY_MUST_NOT_REACH_DRY_RUN"
    (source_root / "source.md").write_text(
        "Synthetic body\n<!-- open-brain:transcript -->\n"
        + canary
        + "\n<!-- /open-brain:transcript -->\n"
    )
    manifest = scan(root=source_root).value
    assert isinstance(manifest, LedgerSourceManifest)
    scratch_root = tmp_path / "scratch"

    result = stage(
        manifest=manifest,
        key=manifest.entries[0].key,
        source_root=source_root,
        scratch_root=scratch_root,
        dry_run=True,
    )

    assert result.exit_code == 0
    assert result.envelope == {
        "command": "ledger.stage",
        "dry_run": True,
        "status": "dry_run",
    }
    assert canary not in result.to_json()
    assert manifest.entries[0].key not in result.to_json()
    assert not scratch_root.exists()
