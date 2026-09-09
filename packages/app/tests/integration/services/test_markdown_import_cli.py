from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import sys
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    MarkdownImportEntry,
    MarkdownImportInterrupted,
    MarkdownImportSummary,
)

from open_brain.profile import open_existing_single_user_local
from open_brain.services import local_entrypoints
from open_brain.services.local_entrypoints import run_cli


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _private_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


def _brain(home: Path) -> Path:
    return home / ".local/share/open-brain/brain"


def _root_count(brain: Path) -> int:
    connection = sqlite3.connect(brain / ".open-brain/state/phase1.sqlite3")
    try:
        return cast(
            int,
            connection.execute("SELECT COUNT(*) FROM markdown_import_roots").fetchone()[0],
        )
    finally:
        connection.close()


class _TTYInput:
    def __init__(self, value: str) -> None:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, value.encode("utf-8"))
        os.close(write_fd)
        self._stream = os.fdopen(read_fd, encoding="utf-8")

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._stream.fileno()

    def readline(self) -> str:
        return self._stream.readline()


class _NoFilenoTTYInput:
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError

    def readline(self) -> str:
        raise AssertionError("a TTY-like stream without fileno must not block")


class _TTYOutput(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_json_import_requires_yes_once_then_reruns_without_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _private_home(tmp_path)
    environment = {"HOME": str(home)}
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# CLI note\ncli-import-token", encoding="utf-8")

    assert (
        run_cli(
            ("import", str(vault), "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    refused = cast(dict[str, object], json.loads(capsys.readouterr().out))
    error = cast(dict[str, object], refused["error"])
    assert error["code"] == "import_confirmation_required"
    assert cast(dict[str, object], error["details"])["missing_finalized"] is False
    assert str(vault) not in json.dumps(refused)
    assert _root_count(_brain(home)) == 0

    assert (
        run_cli(
            ("import", str(vault), "--yes", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    first = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert first["status"] == "completed"
    assert first["imported"] == 1
    assert first["history_retained_after_source_removal"] is True

    previous_handler = signal.getsignal(signal.SIGINT)
    assert (
        run_cli(
            ("import", str(vault), "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert signal.getsignal(signal.SIGINT) is previous_handler
    repeated = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert repeated["unchanged"] == 1
    assert repeated["imported"] == repeated["updated"] == 0

    assert (
        run_cli(
            ("search", "cli-import-token", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    search = cast(dict[str, object], json.loads(capsys.readouterr().out))
    result = cast(list[dict[str, object]], search["results"])[0]
    assert (result["title"], result["trust"], result["source_origin"]) == (
        "CLI note",
        "unverified",
        "unknown",
    )


def test_partial_json_summary_is_bounded_and_redacts_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _private_home(tmp_path)
    vault = tmp_path / "private-vault-name"
    vault.mkdir()
    (vault / "good.md").write_text("safe-search-token", encoding="utf-8")
    (vault / "bad.md").write_bytes(b"private-content-\xff")

    assert (
        run_cli(
            ("import", str(vault), "--yes", "--json"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 1
    )
    output = capsys.readouterr()
    summary = cast(dict[str, object], json.loads(output.out))
    assert output.err == ""
    assert (summary["status"], summary["selected"], summary["failed"], summary["imported"]) == (
        "partial",
        2,
        1,
        1,
    )
    assert summary["missing_finalized"] is True
    assert str(vault) not in output.out
    assert "private-content" not in output.out


@pytest.mark.parametrize("answer", ("n\n", "\n", ""))
def test_interactive_cancellation_writes_no_import_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    home = _private_home(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("cancelled", encoding="utf-8")
    stderr = _TTYOutput()
    monkeypatch.setattr(sys, "stdin", _TTYInput(answer))
    monkeypatch.setattr(sys, "stderr", stderr)

    assert (
        run_cli(
            ("import", str(vault)),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert (
        "Imported revisions remain in history and export after source removal." in stderr.getvalue()
    )
    assert stderr.getvalue().endswith("Markdown import cancelled; no import state was written.\n")
    assert _root_count(_brain(home)) == 0


def test_import_busy_and_interrupted_have_closed_exit_envelopes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    environment = {"HOME": str(home)}
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("busy", encoding="utf-8")
    assert (
        run_cli(
            ("init", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capsys.readouterr()
    engine = BrainEngine.open(open_existing_single_user_local(_brain(home)))
    with engine._writer_lease.acquire_shared_writer():
        assert (
            run_cli(
                ("import", str(vault), "--yes", "--json"),
                environment=environment,
                platform_name="linux",
                filesystem_type_probe=_filesystem,
            )
            == 75
        )
    busy = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert cast(dict[str, object], busy["error"])["code"] == "local_operation_busy"

    def interrupt(*_args: object, **_kwargs: object) -> object:
        raise MarkdownImportInterrupted

    monkeypatch.setattr(
        "open_brain_engine.engine.markdown_import.MarkdownImportTasks.import_directory",
        interrupt,
    )
    previous_handler = signal.getsignal(signal.SIGINT)
    assert (
        run_cli(
            ("import", str(vault), "--yes", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 130
    )
    assert signal.getsignal(signal.SIGINT) is previous_handler
    interrupted = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert interrupted == {
        "error": {
            "code": "import_interrupted",
            "details": {"missing_finalized": False},
            "message": (
                "Markdown import interrupted. Completed file commits were kept; "
                "rerun the same command to resume."
            ),
        },
        "status": "interrupted",
    }


def test_keyboard_interrupt_during_brain_open_is_closed_and_restores_handler(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()

    def interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(local_entrypoints, "open_local_brain", interrupt)
    previous_handler = signal.getsignal(signal.SIGINT)

    assert (
        run_cli(
            ("import", str(vault), "--yes", "--json"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 130
    )
    assert signal.getsignal(signal.SIGINT) is previous_handler
    assert cast(dict[str, object], json.loads(capsys.readouterr().out))["status"] == "interrupted"


def test_interactive_prompt_without_fileno_cancels_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("cancel-no-fileno", encoding="utf-8")
    stderr = _TTYOutput()
    monkeypatch.setattr(sys, "stdin", _NoFilenoTTYInput())
    monkeypatch.setattr(sys, "stderr", stderr)

    assert (
        run_cli(
            ("import", str(vault)),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert stderr.getvalue().endswith("Markdown import cancelled; no import state was written.\n")
    assert _root_count(_brain(home)) == 0


def test_invalid_directory_is_redacted_and_returns_fatal_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _private_home(tmp_path)
    supplied = "relative/private-directory"
    assert (
        run_cli(
            ("import", supplied, "--yes", "--json"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    output = capsys.readouterr()
    failure = cast(dict[str, object], json.loads(output.out))
    assert cast(dict[str, object], failure["error"])["code"] == "import_directory_unavailable"
    assert supplied not in output.out
    assert output.err == ""


def test_sigint_handler_remains_deferred_through_summary_delivery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("summary-handler-token", encoding="utf-8")
    previous_handler = signal.getsignal(signal.SIGINT)
    observed: list[bool] = []
    original = local_entrypoints._write_import_summary

    def inspect_handler(summary: MarkdownImportSummary, *, json_output: bool) -> None:
        observed.append(signal.getsignal(signal.SIGINT) is not previous_handler)
        original(summary, json_output=json_output)

    monkeypatch.setattr(local_entrypoints, "_write_import_summary", inspect_handler)

    assert (
        run_cli(
            ("import", str(vault), "--yes", "--json"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert observed == [True]
    assert signal.getsignal(signal.SIGINT) is previous_handler
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_unexpected_import_failure_uses_closed_json_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("private exception detail")

    monkeypatch.setattr(
        "open_brain_engine.engine.markdown_import.MarkdownImportTasks.import_directory",
        fail,
    )

    assert (
        run_cli(
            ("import", str(vault), "--yes", "--json"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "error": {
            "code": "local_operation_failed",
            "details": {"missing_finalized": False},
            "message": "Open Brain could not complete the local command.",
        },
        "status": "failed",
    }
    assert output.err == ""
    assert "private exception detail" not in output.out


def test_json_summary_caps_entries_at_one_hundred_with_failures_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _private_home(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    for index in range(5):
        (vault / f"asset-{index:03d}.txt").write_text("synthetic", encoding="utf-8")
    for index in range(101):
        (vault / f"bad-{index:03d}.md").write_bytes(b"synthetic-\xff")

    assert (
        run_cli(
            ("import", str(vault), "--yes", "--json"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 1
    )
    summary = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert (summary["failed"], summary["skipped"]) == (101, 5)
    entries = cast(list[dict[str, object]], summary["entries"])
    assert len(entries) == 100
    assert {entry["outcome"] for entry in entries} == {"failed"}
    assert summary["entries_omitted"] == 6


@pytest.mark.parametrize(
    ("omitted", "message"),
    (
        (1, "1 additional result omitted; counts above include the full run."),
        (2, "2 additional results omitted; counts above include the full run."),
    ),
)
def test_human_summary_uses_exact_omission_message(
    omitted: int,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = MarkdownImportSummary(
        entries=(MarkdownImportEntry(outcome="skipped", path="asset.txt", reason="non_markdown"),),
        entries_omitted=omitted,
        failed=0,
        imported=0,
        missing=0,
        missing_finalized=True,
        selected=0,
        skipped=omitted + 1,
        unchanged=0,
        updated=0,
    )

    local_entrypoints._write_import_summary(summary, json_output=False)

    assert capsys.readouterr().out.rstrip().endswith(message)


@pytest.mark.parametrize("answer", ("y\n", "yes\n", "Y\n", "YES\n"))
def test_interactive_confirmation_accepts_ascii_yes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    home = _private_home(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("accepted", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _TTYInput(answer))
    monkeypatch.setattr(sys, "stderr", _TTYOutput())

    assert (
        run_cli(
            ("import", str(vault)),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert _root_count(_brain(home)) == 1
