from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine import BrainEngine

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli


def _json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


def test_brief_usage_failures_exit_two_without_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    for arguments in (
        ("brief", "--stale-days", "0"),
        ("brief", "--stale-days", "366"),
        ("brief", "generate", "--stale-days", "x"),
        ("brief", "ack", "followup:3e6e8e2c"),
        ("brief", "ack", "family:"),
        ("brief", "ack", "family: leading-space"),
        ("brief", "--stale-days", "3", "last"),
        ("brief", "--stale-days", "3", "ack", "family:x"),
        ("brief", "--space", ""),
        ("brief", "--space", "s" * 129),
    ):
        status = run_cli((*arguments, "--json", "--data-dir", str(root)))
        captured = capsys.readouterr()
        assert status == 2, (arguments, captured.out, captured.err)
        assert json.loads(captured.out)["status"] == "failed"
        assert not root.exists()


def test_brief_help_names_the_grants_and_the_log(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(("brief", "--help"), environment={}) == 0
    output = " ".join(capsys.readouterr().out.split())  # argparse wraps the description
    for phrase in (
        "brief-read",
        "brief-ack",
        "--space",
        "--stale-days",
        "no model call",
        "not in version 1",
    ):
        assert phrase in output, phrase


def test_brief_on_a_fresh_brain_reports_nothing_logs_and_shows_last(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    assert run_cli(("brief", "--json", "--data-dir", str(root))) == 0
    first = _json(capsys)
    assert first["outcome"] == "ok" and first["code"] is None
    data = first["data"]
    assert data["items"] == [] and data["nothing_needs_attention"] is True
    assert data["model"] == "none" and data["saved"] is True and data["since"] is None
    assert data["coverage"]["collector"] == "not_read" and data["coverage"]["followups"] == "read"
    assert data["coverage"]["log"] == {"entries": 0, "bytes": 0, "unavailable": False}
    briefs = root / ".open-brain" / "state" / "briefs"
    assert len(list(briefs.glob("*.json"))) == 1

    assert run_cli(("brief", "--data-dir", str(root))) == 0
    text = capsys.readouterr().out
    assert "Nothing needs attention." in text and "Brief 20" in text and "Coverage:" in text

    assert (
        run_cli(("brief", "generate", "--stale-days", "30", "--json", "--data-dir", str(root))) == 0
    )
    third = _json(capsys)["data"]
    assert third["since"] is not None, "the text-mode run above was logged too"
    assert third["coverage"]["log"]["entries"] == 2

    assert run_cli(("brief", "last", "--json", "--data-dir", str(root))) == 0
    last = _json(capsys)
    assert last["outcome"] == "ok"
    assert last["data"]["generated_at"] == third["generated_at"]
    assert last["data"]["items"] == [] and last["data"]["model"] == "none"
    assert last["data"]["log"]["entries"] == 3
    assert run_cli(("brief", "last", "--data-dir", str(root))) == 0
    assert "Last brief" in capsys.readouterr().out

    assert run_cli(("brief", "--space", "no-such-space", "--json", "--data-dir", str(root))) == 2
    unknown = _json(capsys)
    assert unknown["status"] == "failed" and unknown["error"]["code"] == "invalid_request"
    assert (
        run_cli(("brief", "ack", "family:consumer:nope:space_x", "--json", "--data-dir", str(root)))
        == 1
    )
    missing = _json(capsys)
    assert missing["status"] == "failed" and missing["error"]["code"] == "not_found"
    assert run_cli(("brief", "ack", "family:consumer:nope:space_x", "--data-dir", str(root))) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "not found" in captured.err.lower()
    assert (
        run_cli(("brief", "last", "--space", "no-such-space", "--json", "--data-dir", str(root)))
        == 2
    )


def test_brief_space_selection_resolves_names_slugs_and_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root, starter_spaces=()))
    space = engine.tasks.spaces.create_space("Synthetic Space", delivery_id="brief-cli-space")
    other = engine.tasks.spaces.create_space("Other Space", delivery_id="brief-cli-other")
    for selector in ("Synthetic Space", space.slug, space.space_id):
        assert run_cli(("brief", "--space", selector, "--json", "--data-dir", str(root))) == 0
        data = _json(capsys)["data"]
        assert data["scope"]["space_ids"] == [space.space_id], selector
    assert (
        run_cli(
            (
                "brief",
                "--space",
                "Synthetic Space",
                "--space",
                other.slug,
                "--json",
                "--data-dir",
                str(root),
            )
        )
        == 0
    )
    both = _json(capsys)["data"]
    assert both["scope"]["space_ids"] == sorted([space.space_id, other.space_id])
    assert both["since"] is None, "a different selection is a different visibility key"
    assert (
        run_cli(("brief", "last", "--space", space.space_id, "--json", "--data-dir", str(root)))
        == 0
    )
    last = _json(capsys)["data"]
    assert last is not None and last["log"]["entries"] == 3
    assert (
        run_cli(("brief", "last", "--space", other.space_id, "--json", "--data-dir", str(root)))
        == 0
    )
    assert _json(capsys)["data"] is None
    assert run_cli(("brief", "last", "--data-dir", str(root))) == 0
    assert "No saved brief" in capsys.readouterr().out
