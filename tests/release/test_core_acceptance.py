from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest

from tools.open_brain_dev.core_acceptance import (
    PUBLIC_CANARY,
    AcceptanceError,
    safe_extract,
    validate_custody,
    validate_export_result,
    validate_final_receipt,
    validate_mcp_failure_responses,
    validate_reused_evidence,
    validate_search_result,
    validate_source_preservation,
)


def _contract() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    return cast(
        dict[str, Any],
        json.loads((root / "docs/acceptance/core-v01-t23.json").read_text()),
    )


def _passing_receipt(contract: dict[str, Any]) -> dict[str, Any]:
    required = contract["receipt_contract"]["required_rows"]
    rows = [
        {
            "row_id": row_id,
            "acceptance_ids": ["A17"],
            "source_commit": (
                contract["source"]["t21_implementation"]
                if row_id == "reused-documentation-journey"
                else contract["source"]["artifact_source"]
            ),
            "artifact_sha256": contract["artifacts"]["base"]["executable_sha256"],
            "archive_sha256": contract["artifacts"]["base"]["archive_sha256"],
            "artifact_hashes": {
                "base": contract["artifacts"]["base"]["executable_sha256"],
                "graphify": contract["artifacts"]["graphify"]["executable_sha256"],
            },
            "archive_hashes": {
                "base": contract["artifacts"]["base"]["archive_sha256"],
                "graphify": contract["artifacts"]["graphify"]["archive_sha256"],
            },
            "obsidian_asset_hashes": contract["artifacts"]["obsidian_assets"],
            "manifest_sha256": contract["artifacts"]["manifest"]["sha256"],
            "platform": "macos-arm64",
            "fixture_id": "synthetic-core-v01-t23-v1",
            "command": ["synthetic"],
            "expected": "passed",
            "actual": "passed",
            "status": "passed",
            "elapsed_seconds": 0.1,
            "open_reason": None,
        }
        for row_id in required
    ]
    by_id = {row["row_id"]: row for row in rows}
    for row_id, row_contract in contract["receipt_contract"]["new_row_contracts"].items():
        by_id[row_id].update(copy.deepcopy(row_contract))
        by_id[row_id]["actual"] = row_contract["expected"]
    by_id["reused-native-homebrew"].update(
        {
            "acceptance_ids": ["A14", "A15", "A17"],
            "fixture_id": "T20 retained native/Homebrew receipt",
            "command": ["make", "-j1", "native-audit", "homebrew-smoke"],
            "elapsed_seconds": 79.73,
            "expected": "native audit and local-file Homebrew journey passed",
            "actual": "native audit and local-file Homebrew journey passed",
            "reused_receipt": contract["reused_evidence"]["T20-NATIVE-HOMEBREW.json"][
                "sha256"
            ],
            "original_started_at": contract["reused_evidence"][
                "T20-NATIVE-HOMEBREW.json"
            ]["required_fields"]["started_at"],
        }
    )
    by_id["reused-documentation-journey"].update(
        {
            "acceptance_ids": ["A01", "A04", "A05", "A14", "A16", "A17"],
            "fixture_id": "T21 retained 11-block synthetic journey",
            "command": [
                "uv",
                "run",
                "--frozen",
                "python",
                "-m",
                "tools.open_brain_dev.documentation_examples",
                "--artifact",
                "/synthetic/artifact",
                "--root",
                "/synthetic/root",
                "--output",
                "/synthetic/output",
            ],
            "elapsed_seconds": 26.891,
            "expected": "all 11 reviewed documentation blocks completed",
            "actual": "all 11 reviewed documentation blocks completed",
            "reused_receipt": contract["reused_evidence"]["T21-COMMITTED-GATE.json"][
                "sha256"
            ],
            "original_started_at": contract["reused_evidence"][
                "T21-COMMITTED-GATE-RUN.json"
            ]["required_fields"]["started_at"],
            "shared_aggregate_timing": True,
            "completed_block_count": 11,
        }
    )
    by_id["default-home-journey"]["phase_seconds"] = {
        "init": 0.02,
        "capture": 0.02,
        "search": 0.02,
        "export": 0.02,
    }
    by_id["mcp-failure-redaction"]["phase_seconds"] = {
        "authorized_mcp_denials": 0.02,
        "no_grant_refusal": 0.02,
        "post_denial_public_search": 0.02,
        "post_denial_denied_text_search": 0.02,
    }
    by_id["custody-export"]["phase_seconds"] = {
        "post_denial_verified_export": 0.02,
        "role_path_and_inventory_scan": 0.02,
    }
    by_id["custody-export"]["custody"] = {
        "owner_capture_source_files": 1,
        "owner_database_files": 1,
        "first_export_capture_files": 1,
        "post_denial_export_capture_files": 1,
        "compared_nonvolatile_files": 3,
    }
    return {
        "schema_version": 1,
        "result": "certified",
        "certification_scope": "local macOS arm64 T23 only",
        "global_acceptance_complete": False,
        "head": contract["source"]["baseline"],
        "baseline": contract["source"]["baseline"],
        "baseline_ancestor": True,
        "dirty_paths": [],
        "platform": "macos-arm64",
        "hardware": {"machine": "arm64", "model": "synthetic", "memory_gib": 24},
        "artifacts": contract["artifacts"],
        "contract_sha256": "a" * 64,
        "harness_sha256": "b" * 64,
        "input_evidence_sha256": {
            name: specification["sha256"]
            for name, specification in contract["reused_evidence"].items()
        },
        "reviewed_file_count": 21,
        "reviewed_files_aggregate_sha256": "c" * 64,
        "utc_started_at": "2026-09-18T18:00:00+00:00",
        "utc_finished_at": "2026-09-18T18:00:01+00:00",
        "monotonic_elapsed_seconds": 1.0,
        "rows": rows,
        "counts": {"new": 5, "reused": 2, "open": len(contract["open_gates"])},
        "open_gates": [
            {"status": "open", "reason": reason} for reason in contract["open_gates"]
        ],
        "staging_to_export_seconds": 0.2,
        "claims": {
            "historical_300_second_gui_provider_clock": "not_claimed",
            "hostile_same_user_isolation": "not_claimed",
            "production_credential_custody": "not_claimed",
            "public_readiness": "not_claimed",
        },
    }


def test_final_receipt_rejects_missing_row_and_premature_success() -> None:
    contract = _contract()
    receipt = _passing_receipt(contract)
    receipt["rows"] = receipt["rows"][:-1]
    with pytest.raises(AcceptanceError, match="mandatory row"):
        validate_final_receipt(receipt, contract)

    receipt = _passing_receipt(contract)
    receipt["dirty_paths"] = ["tools/open_brain_dev/core_acceptance.py"]
    with pytest.raises(AcceptanceError, match="premature certification"):
        validate_final_receipt(receipt, contract)


def test_final_receipt_rejects_canary_leak_and_failed_actual_result() -> None:
    contract = _contract()
    receipt = _passing_receipt(contract)
    receipt["rows"][0]["actual"] = "SYNTHETIC_T23_SECRET_CANARY"
    with pytest.raises(AcceptanceError, match="canary leak"):
        validate_final_receipt(receipt, contract)

    receipt = _passing_receipt(contract)
    receipt["rows"][0]["actual"] = "failed"
    with pytest.raises(AcceptanceError, match="row did not pass"):
        validate_final_receipt(receipt, contract)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("artifact_sha256", "0" * 64, "artifact mismatch"),
        ("manifest_sha256", "0" * 64, "manifest mismatch"),
        ("source_commit", "0" * 40, "source mismatch"),
        ("platform", "linux-x86_64", "platform mismatch"),
    ],
)
def test_final_receipt_rejects_identity_drift(field: str, value: str, message: str) -> None:
    contract = _contract()
    receipt = _passing_receipt(contract)
    receipt["rows"][0][field] = value
    with pytest.raises(AcceptanceError, match=message):
        validate_final_receipt(receipt, contract)


@pytest.mark.parametrize(
    "missing",
    [
        "head",
        "dirty_paths",
        "contract_sha256",
        "harness_sha256",
        "input_evidence_sha256",
        "utc_started_at",
        "utc_finished_at",
        "monotonic_elapsed_seconds",
    ],
)
def test_final_receipt_rejects_missing_mandatory_header(missing: str) -> None:
    contract = _contract()
    receipt = _passing_receipt(contract)
    receipt.pop(missing)
    with pytest.raises(AcceptanceError, match="mandatory receipt header"):
        validate_final_receipt(receipt, contract)


def test_final_receipt_rejects_reuse_row_spoof_and_promoted_claims() -> None:
    contract = _contract()
    receipt = _passing_receipt(contract)
    receipt["rows"][0].pop("reused_receipt")
    receipt["rows"][0].pop("original_started_at")
    with pytest.raises(AcceptanceError, match="T20 reused row provenance"):
        validate_final_receipt(receipt, contract)

    receipt = _passing_receipt(contract)
    receipt.update(
        {
            "global_acceptance_complete": True,
            "platform": "linux-x86_64",
            "result": "certified",
        }
    )
    with pytest.raises(AcceptanceError, match="scope or source header"):
        validate_final_receipt(receipt, contract)


def test_final_receipt_rejects_wrong_fixture_empty_command_and_nan_timing() -> None:
    contract = _contract()
    for mutation, message in (
        ({"fixture_id": "wrong"}, "command, fixture, or acceptance mapping"),
        ({"command": []}, "invalid row command"),
        ({"elapsed_seconds": float("nan")}, "invalid row timing"),
    ):
        receipt = _passing_receipt(contract)
        receipt["rows"][2].update(mutation)
        with pytest.raises(AcceptanceError, match=message):
            validate_final_receipt(receipt, contract)


def test_final_receipt_rejects_pseudo_command_and_a06_custody_mapping() -> None:
    contract = _contract()
    receipt = _passing_receipt(contract)
    rows = {row["row_id"]: row for row in receipt["rows"]}
    rows["default-home-journey"]["command"] = ["open-brain init/capture/search/export"]
    with pytest.raises(AcceptanceError, match="command, fixture, or acceptance mapping"):
        validate_final_receipt(receipt, contract)

    receipt = _passing_receipt(contract)
    rows = {row["row_id"]: row for row in receipt["rows"]}
    rows["custody-export"]["acceptance_ids"].insert(1, "A06")
    with pytest.raises(AcceptanceError, match="command, fixture, or acceptance mapping"):
        validate_final_receipt(receipt, contract)


def test_search_export_and_mcp_false_success_shapes_are_rejected() -> None:
    capture_id = "capture_123e4567-e89b-42d3-a456-426614174000"
    with pytest.raises(AcceptanceError, match="search result mismatch"):
        validate_search_result({"status": "ok", "results": [], "query": PUBLIC_CANARY}, capture_id)
    with pytest.raises(AcceptanceError, match="verified export result mismatch"):
        validate_export_result(
            {"status": "exported", "verification": "not_requested"}, "synthetic"
        )
    false_responses = [
        {"jsonrpc": "2.0", "id": 99, "error": {"code": -32603}},
        {"jsonrpc": "2.0", "id": 98, "result": {"isError": True}},
        {"jsonrpc": "2.0", "id": 97, "error": {"code": -32603}},
    ]
    with pytest.raises(AcceptanceError, match="MCP initialize"):
        validate_mcp_failure_responses(false_responses)


def _custody_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    capture_id = "capture_123e4567-e89b-42d3-a456-426614174000"
    relative = Path("sources/captures/2026/09") / f"{capture_id}.json"
    data = tmp_path / "data"
    first = tmp_path / "first"
    post = tmp_path / "post"
    source = json.dumps({"payload": {"text": PUBLIC_CANARY}}).encode()
    for root in (data, first, post):
        (root / relative).parent.mkdir(parents=True)
        (root / relative).write_bytes(source)
    database = data / ".open-brain/state/phase1.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(PUBLIC_CANARY.encode())
    for root in (first, post):
        (root / "brain.toml").write_text("schema = 7\n", encoding="utf-8")
        (root / "portable-manifest.json").write_text(
            json.dumps({"volatile": root.name}), encoding="utf-8"
        )
        logical = root / "sources/logical-sources.json"
        logical.write_text('{"sources":[]}', encoding="utf-8")
    return data, first, post, capture_id


@pytest.mark.parametrize(
    "mutation", ["missing_source", "misplaced", "path_config", "logical_change"]
)
def test_custody_rejects_misplaced_path_and_logical_mutations(
    tmp_path: Path, mutation: str
) -> None:
    data, first, post, capture_id = _custody_fixture(tmp_path)
    if mutation == "missing_source":
        relative = next(path for path in data.rglob("capture_*.json"))
        relative.write_text('{"payload":{"text":"missing"}}', encoding="utf-8")
        (data / "unexpected-cache.txt").write_text(PUBLIC_CANARY, encoding="utf-8")
    elif mutation == "misplaced":
        (first / "unexpected-cache.txt").write_text(PUBLIC_CANARY, encoding="utf-8")
    elif mutation == "path_config":
        (post / "config.json").write_text(os.fspath(tmp_path), encoding="utf-8")
    else:
        (post / "sources/logical-sources.json").write_text(
            '{"sources":["changed"]}', encoding="utf-8"
        )
    with pytest.raises(AcceptanceError):
        validate_custody(
            data,
            first,
            post,
            capture_id=capture_id,
            private_paths=(tmp_path,),
        )


def test_reused_evidence_rejects_source_hash_and_required_result_drift(tmp_path: Path) -> None:
    contract = _contract()
    evidence = contract["reused_evidence"]
    identifiers = [f"block-{number}" for number in range(11)]
    for name, specification in evidence.items():
        payload = dict(specification["required_fields"])
        if name == "T21-COMMITTED-GATE.json":
            payload.update(
                {
                    "completed_examples": identifiers,
                    "examples": [
                        {"id": identifier, "sha256": f"{number:064x}"}
                        for number, identifier in enumerate(identifiers, start=1)
                    ],
                    "runtime_isolation": {
                        "ambient_credentials": False,
                        "cwd": "temporary",
                        "home": "temporary",
                        "path": "/bin:/usr/bin",
                        "pythonpath": False,
                    },
                }
            )
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        specification["sha256"] = __import__("hashlib").sha256(path.read_bytes()).hexdigest()

    validate_reused_evidence(tmp_path, contract)

    gate = tmp_path / "T21-COMMITTED-GATE.json"
    payload = json.loads(gate.read_text())
    payload["result"] = "failed"
    gate.write_text(json.dumps(payload), encoding="utf-8")
    contract["reused_evidence"]["T21-COMMITTED-GATE.json"]["sha256"] = (
        __import__("hashlib").sha256(gate.read_bytes()).hexdigest()
    )
    with pytest.raises(AcceptanceError, match="required evidence field"):
        validate_reused_evidence(tmp_path, contract)


def test_safe_extract_rejects_traversal_and_links(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        traversal = tarfile.TarInfo("../escape")
        traversal.size = 1
        bundle.addfile(traversal, io.BytesIO(b"x"))
    with pytest.raises(AcceptanceError, match="unsafe archive member"):
        safe_extract(archive, tmp_path / "stage")

    with tarfile.open(archive, "w:gz") as bundle:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp/outside"
        bundle.addfile(link)
    with pytest.raises(AcceptanceError, match="unsupported archive member"):
        safe_extract(archive, tmp_path / "stage")


def test_source_preservation_accepts_scoped_descendant_and_rejects_dirty_drift(
    tmp_path: Path,
) -> None:
    message_file = tmp_path.parent / f"{tmp_path.name}-commit-message.txt"

    def git(*arguments: str) -> str:
        process = subprocess.run(
            ("git", *arguments), cwd=tmp_path, text=True, capture_output=True, check=True
        )
        return process.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "user.name", "Synthetic T23")
    (tmp_path / "seed").write_text("artifact\n", encoding="utf-8")
    git("add", "seed")
    message_file.write_text("artifact\n", encoding="utf-8")
    git("commit", "-q", "-F", str(message_file))
    artifact_source = git("rev-parse", "HEAD")

    documentation = tmp_path / "tools/open_brain_dev/documentation_examples.py"
    documentation.parent.mkdir(parents=True)
    documentation.write_text("# retained\n", encoding="utf-8")
    git("add", ".")
    message_file.write_text("baseline\n", encoding="utf-8")
    git("commit", "-q", "-F", str(message_file))
    baseline = git("rev-parse", "HEAD")

    harness = tmp_path / "tools/open_brain_dev/core_acceptance.py"
    harness.write_text("# scoped\n", encoding="utf-8")
    git("add", ".")
    message_file.write_text("scoped descendant\n", encoding="utf-8")
    git("commit", "-q", "-F", str(message_file))
    contract = {
        "source": {"artifact_source": artifact_source, "baseline": baseline},
        "allowed_repository_paths": ["tools/open_brain_dev/core_acceptance.py"],
    }
    head, dirty = validate_source_preservation(tmp_path, contract)
    assert head == git("rev-parse", "HEAD")
    assert dirty == []

    (tmp_path / "README.md").write_text("out of scope\n", encoding="utf-8")
    with pytest.raises(AcceptanceError, match="dirty product or build input: README.md"):
        validate_source_preservation(tmp_path, contract)
