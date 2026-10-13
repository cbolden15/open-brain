"""Disposable user settings; fixture proof does not certify native inference."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from open_brain.services.agent_setup import AgentSetupFailure
from open_brain.services.harness_setup import apply_harness_setup, preview_harness_setup
from open_brain.services.local_entrypoints import run_cli

ASSETS = Path(__file__).parents[3] / "src/open_brain/assets"


def options(tmp_path: Path, client: str) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    runtime = tmp_path / "prefix/bin/open-brain"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\nexit 0\n")
    runtime.chmod(0o700)
    config = tmp_path / "bridge.json"
    config.write_text("{}\n")
    return dict(
        client=client,
        action="configure",
        runtime_path=runtime,
        node_path=runtime,
        config_path=config,
        environment={"HOME": str(home)},
        assets_dir=ASSETS,
    )


def apply(opts: dict[str, Any]) -> dict[str, object]:
    preview = preview_harness_setup(**opts)
    return apply_harness_setup(**opts, preview_id=preview["preview_id"])


def test_warm_failure_does_not_activate_hooks(tmp_path: Path) -> None:
    opts = options(tmp_path, "codex")
    opts["runtime_path"].write_text("#!/bin/sh\nexit 1\n")
    preview = preview_harness_setup(**opts)
    with pytest.raises(AgentSetupFailure, match="operation_failed"):
        apply_harness_setup(**opts, preview_id=preview["preview_id"])
    assert not list(Path(opts["environment"]["HOME"]).iterdir())


def test_preview_rechecked_after_warmup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opts = options(tmp_path, "codex")
    preview = preview_harness_setup(**opts)

    def warm(*args: object, **kwargs: object) -> None:
        assert args[0] == [str(opts["runtime_path"]), "harness", "--help"]
        opts["config_path"].write_text('{"changed_during_warmup":true}\n')

    monkeypatch.setattr("open_brain.services.harness_setup.subprocess.run", warm)
    with pytest.raises(AgentSetupFailure, match="setup_preview_stale"):
        apply_harness_setup(**opts, preview_id=preview["preview_id"])
    assert not list(Path(opts["environment"]["HOME"]).iterdir())


@pytest.mark.parametrize("client", ["codex", "claude-code", "pi"])
def test_cli_setup_uses_explicit_disposable_home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], client: str
) -> None:
    opts = options(tmp_path, client)
    args = ["harness", "setup", "--client", client, "--config", str(opts["config_path"]),
            "--runtime", str(opts["runtime_path"]), "--node", str(opts["node_path"])]
    assert run_cli([*args, "--mode", "preview"], environment=opts["environment"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "preview"
    home = Path(opts["environment"]["HOME"])
    assert all(Path(item["path"]).is_relative_to(home) for item in preview["changes"])
    assert run_cli([*args, "--mode", "apply", "--preview-id", preview["preview_id"]],
                   environment=opts["environment"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "configured"


def changes(preview: dict[str, object]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], preview["changes"])


@pytest.mark.parametrize("client", ["codex", "claude-code", "pi"])
def test_clean_home_install_idempotence_and_uninstall(tmp_path: Path, client: str) -> None:
    opts = options(tmp_path, client)
    home = Path(opts["environment"]["HOME"])
    preview = preview_harness_setup(**opts)
    assert not list(home.iterdir())
    assert apply(opts)["status"] == "configured"
    assert apply(opts)["status"] == "unchanged"
    paths = [Path(change["path"]) for change in changes(preview)]
    assert all(path.is_file() for path in paths)
    assert all(path.stat().st_mode & 0o077 == 0 for path in paths)
    opts["action"] = "remove"
    assert apply(opts)["status"] == "removed"
    assert not any(path.exists() for path in paths)
    assert apply(opts)["status"] == "unchanged"


@pytest.mark.parametrize("client", ["codex", "claude-code"])
def test_preserves_unrelated_hooks_and_settings(tmp_path: Path, client: str) -> None:
    opts = options(tmp_path, client)
    home = Path(opts["environment"]["HOME"])
    root = home / (".codex" if client == "codex" else ".claude")
    root.mkdir(mode=0o700)
    hook_path = root / ("hooks.json" if client == "codex" else "settings.json")
    existing: dict[str, Any] = {
        "unrelated": {"value": "synthetic_setting"},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo unrelated"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo keep"}]}],
        },
    }
    hook_path.write_text(json.dumps(existing))
    mcp_path = root / "config.toml" if client == "codex" else home / ".claude.json"
    mcp_before = (
        b'# preserve comment\nmodel = "synthetic"\n'
        if client == "codex"
        else (b'{"mcpServers":{"other":{"command":"synthetic"}},"another":42}')
    )
    mcp_path.write_bytes(mcp_before)
    preview = preview_harness_setup(**opts)
    assert "synthetic_setting" not in json.dumps(preview)
    apply(opts)
    installed = json.loads(hook_path.read_text())
    assert installed["hooks"]["Stop"][0] == existing["hooks"]["Stop"][0]
    assert installed["hooks"]["SessionStart"] == existing["hooks"]["SessionStart"]
    hook_path.write_text(json.dumps({**installed, "added_later": True}))
    opts["action"] = "remove"
    apply(opts)
    assert json.loads(hook_path.read_text()) == {**existing, "added_later": True}
    if client == "codex":
        assert mcp_path.read_bytes() == mcp_before
    else:
        assert json.loads(mcp_path.read_bytes()) == json.loads(mcp_before)


@pytest.mark.parametrize("mutate", ["settings", "config", "runtime", "assets"])
def test_preview_binds_preimages(tmp_path: Path, mutate: str) -> None:
    opts = options(tmp_path, "codex")
    assets = tmp_path / "assets"
    shutil.copytree(ASSETS, assets)
    opts["assets_dir"] = assets
    apply(opts)
    preview = preview_harness_setup(**opts)
    if mutate == "settings":
        path = Path(opts["environment"]["HOME"]) / ".codex/hooks.json"
        value = json.loads(path.read_text())
        value["later"] = True
        path.write_text(json.dumps(value))
    elif mutate == "assets":
        with (assets / "harness-hook.mjs").open("a") as stream:
            stream.write("\n// synthetic changed asset\n")
    else:
        with opts["config_path" if mutate == "config" else "runtime_path"].open("a") as stream:
            stream.write("\n")
    with pytest.raises(AgentSetupFailure, match="setup_preview_stale"):
        apply_harness_setup(**opts, preview_id=preview["preview_id"])


@pytest.mark.parametrize("client", ["codex", "claude-code", "pi"])
def test_edited_owned_fragments_refused(tmp_path: Path, client: str) -> None:
    opts = options(tmp_path, client)
    apply(opts)
    preview = preview_harness_setup(**opts)
    path = Path(changes(preview)[0]["path"])
    text = (
        path.read_text().replace("--runtime", "--changed")
        if client != "pi"
        else (path.read_text().replace("export default", "export const changed"))
    )
    path.write_text(text)
    opts["action"] = "remove"
    with pytest.raises(AgentSetupFailure, match="setup_conflict"):
        preview_harness_setup(**opts)
    assert path.read_text() == text


def test_atomic_rollback_on_second_write_failure(tmp_path: Path) -> None:
    opts = options(tmp_path, "codex")
    preview = preview_harness_setup(**opts)
    calls = 0

    def writer(path: Path, payload: bytes | None) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic failure")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert payload is not None
        path.write_bytes(payload)
        path.chmod(0o600)

    with pytest.raises(AgentSetupFailure, match="operation_failed"):
        apply_harness_setup(**opts, preview_id=preview["preview_id"], write_file=writer)
    assert not any(Path(change["path"]).exists() for change in changes(preview))


@pytest.mark.parametrize("before", [b"", b'model = "synthetic"', b'model = "synthetic"\n'])
def test_codex_toml_exact_preimage_restored(tmp_path: Path, before: bytes) -> None:
    opts = options(tmp_path, "codex")
    root = Path(opts["environment"]["HOME"]) / ".codex"
    root.mkdir(mode=0o700)
    path = root / "config.toml"
    path.write_bytes(before)
    apply(opts)
    opts["action"] = "remove"
    apply(opts)
    assert path.read_bytes() == before


def test_symlink_target_refused(tmp_path: Path) -> None:
    opts = options(tmp_path, "codex")
    home = Path(opts["environment"]["HOME"])
    root = home / ".codex"
    root.mkdir(mode=0o700)
    target = tmp_path / "other.json"
    target.write_text("{}")
    (root / "hooks.json").symlink_to(target)
    with pytest.raises(AgentSetupFailure, match="unsafe_config_path"):
        preview_harness_setup(**opts)
    assert target.read_text() == "{}"


def test_unowned_existing_hook_producer_refused(tmp_path: Path) -> None:
    opts = options(tmp_path, "codex")
    root = Path(opts["environment"]["HOME"]) / ".codex"
    root.mkdir(mode=0o700)
    path = root / "hooks.json"
    before = json.dumps(
        {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "node /synthetic/harness-hook.mjs --event capture",
                            }
                        ]
                    }
                ]
            }
        }
    )
    path.write_text(before)
    with pytest.raises(AgentSetupFailure, match="setup_conflict"):
        preview_harness_setup(**opts)
    assert path.read_text() == before


def test_fast_helper_and_custom_profiles(tmp_path: Path) -> None:
    opts = options(tmp_path, "claude-code")
    helper = opts["runtime_path"].parent.parent / "libexec/open-brain-harness/open-brain-harness"
    helper.parent.mkdir(parents=True)
    shutil.copy2(opts["runtime_path"], helper)
    custom = tmp_path / "alternate-claude"
    opts["environment"]["CLAUDE_CONFIG_DIR"] = str(custom)
    opts["action"] = "install"
    preview = preview_harness_setup(**opts)
    assert str(helper) in json.dumps(preview)
    assert str(custom / ".claude.json") in json.dumps(preview)
    apply(opts)
    opts["action"] = "uninstall"
    assert apply(opts)["status"] == "removed"


def test_pi_entry_subprocess_fixture_optout_and_private_denial(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for optional Pi adapter fixture")
    runtime = tmp_path / "runtime"
    log = tmp_path / "calls"
    runtime.write_text(
        "#!/usr/bin/env python3\nimport json,sys\n"
        f"log={str(log)!r}\n"
        "action=sys.argv[2]\n"
        "payload=json.loads(sys.stdin.read()) if action!='drain' else {}\n"
        "with open(log,'a') as f:f.write(json.dumps({'action':action,'payload':payload})+'\\n')\n"
        "if payload.get('query')=='private':\n"
        " print(json.dumps({'status':'refused','requires_local_only':True}));sys.exit(1)\n"
        "print(json.dumps({'status':'queued'} if action=='enqueue' else "
        "{'status':'ok','results':[{'text':'synthetic fact','citation':'synthetic source'}]}))\n"
    )
    runtime.chmod(0o700)
    script = tmp_path / "fixture.mjs"
    entry_options = json.dumps({"runtime": str(runtime), "config": str(tmp_path / "config")})
    script.write_text(
        f"import configure from {json.dumps((ASSETS / 'pi-entry.mjs').as_uri())};\n"
        "const events={},tools={},warnings=[];\n"
        "const pi={on:(name,fn)=>events[name]=fn,registerTool:t=>tools[t.name]=t};\n"
        f"configure(pi,{entry_options});\n"
        "const ctx={sessionManager:{getSessionId:()=> 'fixture',getBranch:()=>[\n"
        " {type:'message',id:'message1',timestamp:'2026-10-09T00:00:01.000Z',\n"
        " message:{role:'user',content:'synthetic statement'}}],\n"
        " getHeader:()=>({id:'fixture',timestamp:'2026-10-09T00:00:00.000Z'})},\n"
        " ui:{notify:(text)=>warnings.push(text)}};\n"
        "process.env.OPEN_BRAIN_HARNESS_DISABLED='1';\n"
        "if(await events.before_agent_start({prompt:'disabled'},ctx))throw Error('optout');\n"
        "const disabled=await tools.brain_search_page.execute("
        "'id',{query:'disabled'},null,null,ctx);\n"
        "if(disabled.details.status!=='disabled')throw Error('tool optout');\n"
        "delete process.env.OPEN_BRAIN_HARNESS_DISABLED;\n"
        "const result=await events.before_agent_start({prompt:'eligible'},ctx);\n"
        "if(!result.message.content.includes('synthetic source'))throw Error('recall');\n"
        "await events.agent_end({},ctx);\n"
        "const denied=await events.before_agent_start({prompt:'private'},ctx);\n"
        "if(denied!==undefined||!warnings.some(t=>t.includes('refused')))"
        "throw Error('private denial');\n"
        "await events.agent_end({},ctx);\n"
        "const after=await events.before_agent_start({prompt:'eligible'},ctx);\n"
        "if(!after?.message?.content.includes('synthetic fact'))throw Error('sticky denial');\n"
    )
    environment = dict(os.environ)
    environment.pop("OPEN_BRAIN_HARNESS_DISABLED", None)
    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(row["payload"].get("query") == "disabled" for row in rows)
    assert all(row["payload"].get("local_only", False) is False for row in rows)
    capture = next(row for row in rows if row["action"] == "enqueue")
    assert capture["payload"]["messages"][0]["source_id"] == "message1"
    assert [row["payload"]["query"] for row in rows if row["action"] == "recall"] == [
        "eligible",
        "private",
        "eligible",
    ]
    assert len([row for row in rows if row["action"] == "enqueue"]) == 2
