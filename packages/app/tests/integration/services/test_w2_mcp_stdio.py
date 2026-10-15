"""W2 authorization through the installed CLI stdio boundary, using synthetic data."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine import TextPayload

from packages.app.tests.integration.engine.test_slack_sharing import _single
from packages.app.tests.unit.test_session_authority import write_policy


@pytest.mark.parametrize("owner_elevation", [False, True])
@pytest.mark.parametrize("explicit_policy", [False, True])
def test_cli_mcp_policy_origin_covers_legacy_negotiated_and_guessed_reads(
    tmp_path: Path, owner_elevation: bool, explicit_policy: bool
) -> None:
    journey = _single(tmp_path)
    source = journey.sources()[0]
    raw_id = source["head_capture_id"]
    approved_id = journey.approve(source)[3].copy_capture_id
    control = journey.tasks.capture.accept(
        TextPayload("Onboarding ordinary control remains readable"),
        delivery_id="w2.stdio.control",
    )
    root = Path(__file__).resolve().parents[5]
    home = tmp_path / "client-home"
    home.mkdir(mode=0o700)
    args = [
        "mcp",
        "--data-dir",
        str(journey.tasks.profile.root),
        "--allow-search",
        "--allow-content-read",
    ]
    if owner_elevation:
        args += ["--allow-inbox-read"]
    if explicit_policy:
        policy = home / "policy.json"
        write_policy(policy, journey.tasks, read_tiers=("public", "work", "personal"))
        args += ["--session-policy", str(policy)]

    calls = [
        ("brain_search", {"query": "onboarding", "limit": 10}),
        ("brain_search_page", {"dto_version": 1, "query": "onboarding"}),
        ("brain_read", {"dto_version": 1, "record_id": raw_id, "expected_revision_id": raw_id}),
        (
            "brain_read",
            {"dto_version": 1, "record_id": approved_id, "expected_revision_id": approved_id},
        ),
    ]
    messages: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-w2", "version": "1"},
            },
        }
    ]
    for index, (name, arguments) in enumerate(calls, 1):
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from open_brain.services.local_entrypoints import run_cli; "
            "raise SystemExit(run_cli())",
            *args,
        ],
        cwd=root,
        env={
            "HOME": str(home),
            "PATH": os.defpath,
            "PYTHONPATH": os.pathsep.join(
                str(root / "packages" / package / "src") for package in ("app", "engine")
            ),
        },
        input="".join(json.dumps(message) + "\n" for message in messages),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses) == len(messages), result.stdout
    rendered = json.dumps(responses[1:])
    assert control.capture_id in rendered
    if explicit_policy:
        assert raw_id in json.dumps(responses[1:3])
        assert "structuredContent" in responses[3]["result"]
        assert "structuredContent" in responses[4]["result"]
    else:
        assert raw_id not in rendered
        assert approved_id not in rendered
        for response in responses[3:5]:
            assert response["result"]["isError"] is True
            assert response["result"]["content"] == [{"type": "text", "text": "not_found"}]
