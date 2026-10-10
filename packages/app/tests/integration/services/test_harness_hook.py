"""Synthetic subprocess fixtures; these do not prove native hook inference."""

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest

ASSET = Path(__file__).parents[3] / "src/open_brain/assets/harness-hook.mjs"


def test_harness_hook_fixture(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for optional harness hook fixtures")
    runtime = tmp_path / "runtime"
    config = tmp_path / "config.json"
    config.write_text("{}")
    log = tmp_path / "calls.jsonl"
    durable = tmp_path / "custody"
    runtime.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"log={str(log)!r}\ncustody={str(durable)!r}\n"
        "action=sys.argv[2]\n"
        "raw=sys.stdin.read() if action!='drain' else ''\n"
        "with open(log,'a') as f: f.write(json.dumps({'args':sys.argv[1:],'raw':raw})+'\\n')\n"
        "payload=json.loads(raw) if raw else {}\n"
        "if payload.get('prompt')=='slow': time.sleep(10)\n"
        "if action=='enqueue':\n"
        " with open(custody,'w') as f: f.write('retained')\n"
        " print(json.dumps({'status':'queued'}))\n"
        "elif action=='drain': print(json.dumps({'status':'delivered'}))\n"
        "elif payload.get('prompt')=='failed':\n"
        " print(json.dumps({'status':'unavailable','secret_error':'never show'}));sys.exit(1)\n"
        "elif payload.get('prompt')=='empty': print(json.dumps({'status':'ok','results':[]}))\n"
        "else: print(json.dumps({'status':'ok','results':[\n"
        " {'text':'synthetic fact','citation':'{\"session_id\":\"fixture\"}'},\n"
        " {'text':'x'*9000,'citation':'complete oversized citation'}]}))\n"
    )
    runtime.chmod(0o700)

    def run(
        event: str, payload: dict[str, str], *, disabled: bool = False, client: str = "codex"
    ) -> dict[str, Any]:
        import os

        environment = dict(os.environ)
        environment.pop("OPEN_BRAIN_HARNESS_DISABLED", None)
        if disabled:
            environment["OPEN_BRAIN_HARNESS_DISABLED"] = "1"
        result = subprocess.run(
            [
                node,
                str(ASSET),
                "--runtime",
                str(runtime),
                "--config",
                str(config),
                "--client",
                client,
                "--event",
                event,
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=5,
            env=environment,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        return cast(dict[str, Any], json.loads(result.stdout))

    metadata = {
        "hook_event_name": "Stop",
        "session_id": "fixture",
        "cwd": "/synthetic",
        "transcript_path": "/synthetic/transcript",
    }
    assert run("capture", metadata, disabled=True) == {}
    assert not log.exists()
    capture = run("capture", metadata)
    assert "queued" in capture["systemMessage"]
    assert "delivered" not in capture["systemMessage"]
    assert durable.read_text() == "retained"
    for _ in range(100):
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        if len(calls) == 2:
            break
        time.sleep(0.01)
    assert [call["args"][1] for call in calls] == ["enqueue", "drain"]
    assert json.loads(calls[0]["raw"]) == metadata
    for call in calls:
        assert call["args"] == [
            "harness",
            call["args"][1],
            "--config",
            str(config),
            "--client",
            "codex",
        ]
    prompt = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "fixture",
        "cwd": "/synthetic",
        "prompt": "question",
    }
    recall = run("recall", prompt)
    output = recall["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert 'synthetic fact\nSource: {"session_id":"fixture"}' in output["additionalContext"]
    assert len(output["additionalContext"].encode()) <= 8192
    assert "omitted" in recall["systemMessage"]
    assert run("recall", dict(prompt, prompt="empty")) == {}
    failed = run("recall", dict(prompt, prompt="failed"))
    assert "unavailable" in failed["systemMessage"]
    assert "never show" not in json.dumps(failed)
    assert run("recall", dict(prompt, prompt="empty"), client="claude-code") == {}
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[-1]["args"][-1] == "claude-code"
    before = len(calls)
    oversized = run("recall", dict(prompt, prompt="x" * (256 * 1024)))
    assert "unavailable" in oversized["systemMessage"]
    assert len(log.read_text().splitlines()) == before
    for event, budget in [("capture", 2), ("recall", 3)]:
        start = time.monotonic()
        failed = run(event, dict(metadata if event == "capture" else prompt, prompt="slow"))
        assert "unavailable" in failed["systemMessage"]
        assert time.monotonic() - start < budget + 0.3
