from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from open_brain.services.local_entrypoints import run_cli

ITEM = "followup_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
CAPTURE = "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
EVIDENCE = json.dumps(
    {
        "capture_id": CAPTURE,
        "revision_id": CAPTURE,
        "quote_start": 0,
        "quote_end": 5,
        "quote_sha256": "a" * 64,
        "role": "origin",
    }
)
PROPOSE = ("followup", "propose", "--kind", "commitment", "--summary", "x")
DECIDE = ("followup", "decide", ITEM, "--expected-version", "1")


def test_followup_usage_failures_exit_two_without_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    for arguments in (
        ("followup", "show", "proposal_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"),
        (*PROPOSE, "--owner", "other", "--evidence", EVIDENCE),
        (*PROPOSE, "--owner", "self", "--evidence", "{not json"),
        (*PROPOSE, "--owner", "self", "--evidence", EVIDENCE, "--project", "p" * 129),
        (*DECIDE, "--action", "snooze"),
        (*DECIDE, "--action", "correct", "--set", "ticket_key=x"),
        (*DECIDE, "--action", "link", "--ticket-key", "k" * 201),
        ("followup", "list", "--limit", "0"),
    ):
        assert run_cli((*arguments, "--json", "--data-dir", str(root))) == 2
        captured = capsys.readouterr()
        assert json.loads(captured.out)["status"] == "failed"
        assert not root.exists()


def test_followup_list_show_and_decide_on_fresh_brain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    assert run_cli(("followup", "list", "--json", "--data-dir", str(root))) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed == {
        "outcome": "ok",
        "data": {"items": [], "next_cursor": None, "truncated": False},
        "code": None,
    }

    assert run_cli(("followup", "list", "--data-dir", str(root))) == 0
    assert "No open follow-ups." in capsys.readouterr().out

    assert run_cli(("followup", "show", ITEM, "--json", "--data-dir", str(root))) == 1
    shown = json.loads(capsys.readouterr().out)
    assert shown["status"] == "failed" and shown["error"]["code"] == "not_found"

    assert run_cli((*DECIDE, "--action", "confirm", "--data-dir", str(root))) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not found" in captured.err.lower()


def test_followup_propose_with_unverifiable_quote_is_refused_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    code = run_cli(
        (
            "followup",
            "propose",
            "--kind",
            "question",
            "--summary",
            "Which vendor?",
            "--owner",
            "unknown",
            "--evidence",
            EVIDENCE,
            "--json",
            "--data-dir",
            str(root),
        )
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] in {"not_found", "evidence_mismatch"}
    assert "Which vendor?" not in json.dumps(payload["error"])


def test_followup_owner_round_trip_on_a_real_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = str(tmp_path / "brain")
    text = "Synthetic note: I will send the deck."
    assert run_cli(("capture", text, "--json", "--data-dir", root)) == 0
    capture_id = json.loads(capsys.readouterr().out)["capture_id"]
    start = text.index("I will")
    quote = text[start:]
    evidence = json.dumps(
        {
            "capture_id": capture_id,
            "revision_id": capture_id,
            "quote_start": start,
            "quote_end": len(text),
            "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            "role": "origin",
        }
    )
    arguments = ("--kind", "commitment", "--summary", "Send the deck", "--owner", "self")
    propose = ("followup", "propose", *arguments, "--evidence", evidence)
    assert run_cli((*propose, "--json", "--data-dir", root)) == 0
    item = json.loads(capsys.readouterr().out)["data"]["item"]
    assert item["state"] == "suggested" and item["version"] == 1

    assert run_cli(("followup", "show", item["item_id"], "--json", "--data-dir", root)) == 0
    shown = json.loads(capsys.readouterr().out)["data"]
    assert [(row["quote"], row["status"]) for row in shown["evidence"]] == [(quote, "current")]
    assert [row["action"] for row in shown["decisions"]] == ["propose"]

    decide = ("followup", "decide", item["item_id"], "--action", "confirm")
    assert run_cli((*decide, "--expected-version", "1", "--data-dir", root)) == 0
    assert "[confirmed]" in capsys.readouterr().out
    assert run_cli((*decide, "--expected-version", "1", "--json", "--data-dir", root)) == 1
    refused = json.loads(capsys.readouterr().out)["error"]
    assert refused["code"] == "version_conflict" and refused["current_version"] == 2
