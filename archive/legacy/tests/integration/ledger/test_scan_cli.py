from __future__ import annotations

from pathlib import Path

from open_brain_legacy.cli.ledger import scan


def test_scan_cli_returns_a_stable_manifest_envelope(tmp_path: Path) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    (root / "b.md").write_text("Synthetic B\n")
    (root / "a.md").write_text("Synthetic A\n")

    first = scan(root=root)
    second = scan(root=root)

    assert first.exit_code == 0
    assert first.to_json() == second.to_json()
    assert first.envelope == {
        "command": "ledger.scan",
        "entry_count": 2,
        "manifest_digest_sha256": first.envelope["manifest_digest_sha256"],
        "manifest_id": first.envelope["manifest_id"],
        "status": "ok",
    }
    assert "a.md" not in first.to_json()
    assert "Synthetic A" not in first.to_json()


def test_scan_cli_returns_a_redacted_failure_envelope(tmp_path: Path) -> None:
    missing_root = tmp_path / "PRIVATE_SOURCE_ROOT"

    result = scan(root=missing_root)

    assert result.exit_code == 1
    assert result.envelope == {
        "command": "ledger.scan",
        "error": {
            "code": "ledger_operation_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }
    assert "PRIVATE_SOURCE_ROOT" not in result.to_json()
