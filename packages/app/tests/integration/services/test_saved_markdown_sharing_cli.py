from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingRevokeRequest,
)

import open_brain.services.local_entrypoints as entrypoints
from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


@pytest.mark.parametrize("hostile", ["\x1b[2J", "\x9b2J", "\u202e", "\u200b"])
def test_owner_sharing_human_output_filters_hostile_terminal_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    text = f"Synthetic before {hostile} after 漢字\n"
    tasks, request, _owner, _text = _managed_source(tmp_path, text=text)
    request_file = tmp_path / "preview.json"
    request_file.write_text(json.dumps(request.value()), encoding="utf-8")
    arguments = (
        "sharing",
        "preview",
        "--request-file",
        str(request_file),
        "--data-dir",
        str(tasks.profile.root),
    )
    assert entrypoints.run_cli((*arguments, "--json"), filesystem_type_probe=_filesystem) == 0
    raw = capsys.readouterr()
    assert raw.err == ""
    preview = json.loads(raw.out)
    assert preview["text"] == text
    for invocation in (
        arguments,
        ("sharing", "inspect", preview["preview_id"], "--data-dir", str(tasks.profile.root)),
    ):
        assert entrypoints.run_cli(invocation, filesystem_type_probe=_filesystem) == 0
        rendered = capsys.readouterr()
        assert rendered.err == ""
        assert hostile not in rendered.out
        assert "Synthetic before" in rendered.out and "after 漢字" in rendered.out
        assert "\x1b" not in rendered.out and "\x9b" not in rendered.out


def test_owner_sharing_cli_preview_and_inspect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tasks, request, _owner, text = _managed_source(tmp_path)
    request_file = tmp_path / "preview.json"
    request_file.write_text(json.dumps(request.value()))
    code = entrypoints.run_cli(
        (
            "sharing",
            "preview",
            "--request-file",
            str(request_file),
            "--data-dir",
            str(tasks.profile.root),
            "--json",
        ),
        filesystem_type_probe=_filesystem,
    )
    output = capsys.readouterr()
    assert output.err == ""
    assert code == 0
    preview = json.loads(output.out)
    assert preview["text"] == text
    code = entrypoints.run_cli(
        (
            "sharing",
            "inspect",
            preview["preview_id"],
            "--data-dir",
            str(tasks.profile.root),
            "--json",
        ),
        filesystem_type_probe=_filesystem,
    )
    output = capsys.readouterr()
    assert output.err == ""
    assert code == 0
    assert json.loads(output.out)["preview"]["text"] == text


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_owner_cli_decision_replay_inspection_revoke_and_conflicting_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], decision: str
) -> None:
    tasks, preview_request, _owner, _text = _managed_source(tmp_path)
    request_file = tmp_path / "request.json"

    def invoke(action: str, value: dict[str, object] | str) -> tuple[int, dict[str, Any]]:
        arguments: tuple[str, ...]
        if isinstance(value, str):
            arguments = (value,)
        else:
            request_file.write_text(json.dumps(value), encoding="utf-8")
            arguments = ("--request-file", str(request_file))
        code = entrypoints.run_cli(
            ("sharing", action, *arguments, "--data-dir", str(tasks.profile.root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        output = capsys.readouterr()
        assert output.err == ""
        return code, json.loads(output.out)

    code, preview = invoke("preview", preview_request.value())
    assert code == 0
    request = SharingDecisionRequest(
        operation_id=f"sharing.cli.{decision}",
        preview_id=preview["preview_id"],
        preview_sha256=preview["preview_sha256"],
        brain_id=preview_request.brain_id,
        issuer_epoch=preview_request.issuer_epoch,
        destination_brain_id=preview_request.brain_id,
        expected_decision_version=0,
        decision=decision,
    )
    code, receipt = invoke(decision, request.value())
    assert code == 0
    assert receipt["state"] == ("captured" if decision == "approve" else "rejected")
    assert receipt["decision"] == decision
    assert invoke(decision, request.value()) == (0, receipt)
    code, inspection = invoke("inspect", receipt["approval_id"])
    assert code == 0
    assert inspection["decision_receipt"] == receipt
    other = "reject" if decision == "approve" else "approve"
    code, error = invoke(other, replace(request, decision=other).value())
    assert code == 2
    assert error["error"]["code"] == "invalid_arguments"
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == (
            1 if decision == "approve" else 0
        )
    if decision == "approve":
        revoke = SharingRevokeRequest(
            operation_id="sharing.cli.revoke",
            approval_id=receipt["approval_id"],
            expected_approval_version=1,
            brain_id=preview_request.brain_id,
            issuer_epoch=preview_request.issuer_epoch,
            destination_brain_id=preview_request.brain_id,
            reason="owner_choice",
        )
        code, revoked = invoke("revoke", revoke.value())
        assert code == 0 and revoked["state"] == "revoked"
        assert invoke("revoke", revoke.value()) == (0, revoked)
        code, inspection = invoke("inspect", receipt["approval_id"])
        assert code == 0 and inspection["revoked"] is True
        assert inspection["decision_receipt"] == receipt
        assert inspection["revoke_receipt"] == revoked
        assert invoke(decision, request.value()) == (0, receipt)


@pytest.mark.parametrize("action", ["preview", "approve", "reject", "revoke"])
@pytest.mark.parametrize(
    "raw",
    [b'{"dto_version":1,"dto_version":1}', b"\xff", b"{}" + b" " * 65_535],
)
def test_malformed_sharing_request_refuses_before_brain_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str, raw: bytes
) -> None:
    root = tmp_path / "brain"
    request_file = tmp_path / "bad.json"
    request_file.write_bytes(raw)
    code = entrypoints.run_cli(
        (
            "sharing",
            action,
            "--request-file",
            str(request_file),
            "--data-dir",
            str(root),
            "--json",
        ),
        filesystem_type_probe=_filesystem,
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"
    assert not root.exists()
