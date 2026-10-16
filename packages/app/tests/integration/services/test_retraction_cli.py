"""Public owner retraction grammar, durable receipts and bounded diagnostics."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine, TextPayload

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli


def _cli(root: Path, *args: str) -> int:
    return run_cli(
        (*args, "--data-dir", str(root), "--json"),
        platform_name="darwin",
        filesystem_type_probe=lambda _path, _platform: "apfs",
    )


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "PRIVATE_SYNTHETIC" not in captured.out
    return cast(dict[str, object], json.loads(captured.out))


def test_cli_preview_commit_and_lost_ack_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    capture = engine.capture.accept(
        TextPayload("PRIVATE_SYNTHETIC_CLI_BODY"), delivery_id="cli.retract"
    ).capture_id
    assert _cli(root, "retract", "preview", "--capture", capture) == 0
    preview = _output(capsys)
    assert set(preview) == {
        "dto_version",
        "status",
        "preview_id",
        "expires_at",
        "target",
        "counts",
        "items",
        "item_page",
        "item_pages",
        "next_item_page",
        "history_retained",
    }
    identity = str(preview["preview_id"])
    assert _cli(root, "retract", "preview", "--preview-id", identity, "--item-page", "0") == 0
    assert _output(capsys) == preview
    operation = str(uuid4())
    assert (
        _cli(root, "retract", "commit", "--preview-id", identity, "--operation-id", operation) == 0
    )
    receipt = _output(capsys)
    assert _cli(root, "retract", "status", "--operation-id", operation) == 0
    assert _output(capsys) == receipt
    assert (
        _cli(root, "retract", "commit", "--preview-id", identity, "--operation-id", operation) == 0
    )
    assert _output(capsys) == receipt
    assert engine.retrieval.search("PRIVATE_SYNTHETIC") == ()
    assert _cli(root, "export", str(tmp_path / "archive"), "--verify") == 0
    assert _output(capsys)["schema_version"] == 14
    assert _cli(root, "status") == 0
    assert _output(capsys)["portable_export"] == "verified"


@pytest.mark.parametrize(
    "args",
    [
        ("preview",),
        ("preview", "--capture", "PRIVATE_SYNTHETIC_BAD_ID"),
        ("preview", "--capture", "capture_" + str(uuid4()), "--item-page", "1"),
        ("preview", "--capture", "capture_" + str(uuid4()), "--session", "capsess_" + str(uuid4())),
        ("commit", "--preview-id", "PRIVATE_SYNTHETIC_BAD_ID", "--operation-id", str(uuid4())),
        ("status", "--operation-id", "PRIVATE_SYNTHETIC_BAD_ID"),
    ],
)
def test_bad_cli_grammar_refuses_before_bootstrap_without_argument_echo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
) -> None:
    root = tmp_path / "absent"
    assert _cli(root, "retract", *args) == 2
    assert _output(capsys) == {
        "dto_version": 1,
        "status": "failed",
        "error": {
            "code": "invalid_arguments",
            "retryable": False,
            "item_token": None,
        },
    }
    assert not root.exists()


def test_unknown_operation_status_uses_closed_body_free_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(tmp_path / "brain", "retract", "status", "--operation-id", str(uuid4())) == 78
    assert _output(capsys)["error"] == {
        "code": "operation_not_found",
        "retryable": False,
        "item_token": None,
    }
    assert not (tmp_path / "brain").exists()


def test_root_options_preserve_retraction_usage_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "absent"
    assert run_cli(("--json", "--data-dir", str(root), "retract", "preview")) == 2
    assert _output(capsys)["error"] == {
        "code": "invalid_arguments",
        "retryable": False,
        "item_token": None,
    }
    assert not root.exists()


def test_read_commands_do_not_run_startup_recovery_or_change_canonical_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    capture = engine.capture.accept(
        TextPayload("PRIVATE_SYNTHETIC_READ_ONLY"), delivery_id="read.only"
    ).capture_id
    from open_brain_engine.engine import source_store
    from open_brain_engine.engine.sharing import SharingTasks

    from packages.app.tests.unit.engine.test_capture_lifecycle_engine import _owner

    identity = str(
        engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))[
            "preview_id"
        ]
    )
    with closing(engine._store.connect()) as connection:
        before = tuple(connection.iterdump())

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("startup recovery must not run for owner retraction")

    monkeypatch.setattr(BrainEngine, "_recover", fail)
    monkeypatch.setattr(SharingTasks, "recover_pending", fail)
    monkeypatch.setattr(source_store, "publish_source_metadata", fail)
    assert _cli(root, "retract", "preview", "--preview-id", identity, "--item-page", "0") == 0
    assert _output(capsys)["status"] == "preview"
    assert _cli(root, "retract", "status", "--operation-id", str(uuid4())) == 78
    assert _output(capsys)["error"] == {
        "code": "operation_not_found",
        "retryable": False,
        "item_token": None,
    }
    with closing(engine._store.connect()) as connection:
        assert tuple(connection.iterdump()) == before
