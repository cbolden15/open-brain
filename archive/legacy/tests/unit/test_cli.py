import json
import sys
from dataclasses import dataclass, field

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
from open_brain_legacy.cli._registry import CommandAdapterRegistry, command_names
from open_brain_legacy.cli.main import main


@dataclass(frozen=True)
class FakeCliResult:
    exit_code: ExitCode
    envelope: dict[str, object]


@dataclass
class RecordingCommandAdapter:
    command: str = "capture"
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def dispatch(self, argv: tuple[str, ...]) -> FakeCliResult:
        self.calls.append(argv)
        return FakeCliResult(
            ExitCode.SUCCESS,
            {"command": self.command, "status": "ok"},
        )


class RaisingCommandAdapter:
    def dispatch(self, argv: tuple[str, ...]) -> FakeCliResult:
        raise RuntimeError("token=synthetic-secret /private/path")


@dataclass(frozen=True)
class ForgedResultAdapter:
    envelope: dict[str, object]

    def dispatch(self, argv: tuple[str, ...]) -> FakeCliResult:
        return FakeCliResult(ExitCode.SUCCESS, self.envelope)


def test_empty_cli_exits_successfully() -> None:
    assert main([]) == 0


def test_main_defaults_to_process_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["open-brain", "--json", "capture"])

    assert main() == ExitCode.FAILURE


def test_registered_family_receives_exact_remaining_argv_through_injected_adapter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = RecordingCommandAdapter()
    adapters = CommandAdapterRegistry({"capture": adapter})

    exit_code = main(
        ["capture", "text", "--why=synthetic", "--json", "--dry-run"],
        command_adapters=adapters,
    )

    assert exit_code is ExitCode.SUCCESS
    assert adapter.calls == [("text", "--why=synthetic", "--json", "--dry-run")]
    assert json.loads(capsys.readouterr().out) == {
        "command": "capture",
        "status": "ok",
    }


def test_registered_family_without_dry_run_receives_exact_remaining_argv(
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = RecordingCommandAdapter()
    adapters = CommandAdapterRegistry({"capture": adapter})

    exit_code = main(
        ["capture", "text", "--why=synthetic", "--json"],
        command_adapters=adapters,
    )

    assert exit_code is ExitCode.SUCCESS
    assert adapter.calls == [("text", "--why=synthetic", "--json")]
    assert json.loads(capsys.readouterr().out) == {
        "command": "capture",
        "status": "ok",
    }


@pytest.mark.parametrize(
    ("argv", "family", "output_command", "expected_argv"),
    [
        (
            ["--dry-run", "config", "migrate", "--apply", "--json"],
            "config",
            "config",
            ("migrate", "--apply", "--json", "--dry-run"),
        ),
        (
            ["--json", "--dry-run", "config", "migrate", "--apply"],
            "config",
            "config",
            ("migrate", "--apply", "--dry-run"),
        ),
        (
            ["--dry-run", "migrate", "state", "--apply", "--json"],
            "migrate",
            "migration",
            ("state", "--apply", "--json", "--dry-run"),
        ),
        (
            ["--json", "--dry-run", "migrate", "state", "--apply"],
            "migrate",
            "migration",
            ("state", "--apply", "--dry-run"),
        ),
    ],
)
def test_prefix_dry_run_is_normalized_once_into_adapter_argv(
    argv: list[str],
    family: str,
    output_command: str,
    expected_argv: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = RecordingCommandAdapter(command=output_command)
    adapters = CommandAdapterRegistry({family: adapter})

    assert main(argv, command_adapters=adapters) is ExitCode.SUCCESS
    assert adapter.calls == [expected_argv]
    assert json.loads(capsys.readouterr().out) == {
        "command": output_command,
        "status": "ok",
    }


def test_command_registry_is_deterministic_and_covers_public_families() -> None:
    assert command_names() == tuple(sorted(command_names()))
    assert command_names() == (
        "capture",
        "config",
        "cron",
        "digest",
        "doctor",
        "explain",
        "inbox",
        "ledger",
        "migrate",
        "okf",
        "proposals",
        "query",
        "registry",
        "retention",
        "review",
        "share",
        "social",
        "spaces",
        "status",
    )


def test_redacted_error_never_includes_exception_text() -> None:
    error = redacted_error("service_deferred", ValueError("token=synthetic-secret /private/path"))

    assert error == {
        "code": "service_deferred",
        "message": "operation unavailable; details redacted",
        "redacted": True,
    }
    assert "synthetic-secret" not in json.dumps(error)


def test_adapter_failure_returns_deterministic_redacted_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapters = CommandAdapterRegistry({"query": RaisingCommandAdapter()})

    assert (
        main(
            ["query", "token=synthetic-secret", "--json"],
            command_adapters=adapters,
        )
        == ExitCode.FAILURE
    )

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "command": "query",
        "error": {
            "code": "command_adapter_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }
    assert "synthetic-secret" not in json.dumps(output)


@pytest.mark.parametrize(
    "envelope",
    [
        {
            "command": "capture",
            "status": "ok",
            "detail": "token=synthetic-secret /private/path synthetic exception text",
        },
        {"command": "query", "status": "ok"},
    ],
)
def test_forged_adapter_result_fails_closed_without_echoing_payload(
    envelope: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapters = CommandAdapterRegistry({"capture": ForgedResultAdapter(envelope)})

    assert (
        main(
            ["capture", "token=synthetic-secret", "/private/path", "--json"],
            command_adapters=adapters,
        )
        == ExitCode.FAILURE
    )

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "command": "capture",
        "error": {
            "code": "command_adapter_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }
    rendered = json.dumps(output)
    assert "synthetic-secret" not in rendered
    assert "/private/path" not in rendered
    assert "synthetic exception text" not in rendered


@pytest.mark.parametrize("action", ["edit", "archive"])
def test_legacy_review_actions_dispatch_only_to_the_injected_review_adapter(
    action: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = RecordingCommandAdapter(command="review")
    adapters = CommandAdapterRegistry({"review": adapter})

    exit_code = main(
        ["review", action, "review_synthetic", "token=synthetic-secret", "--json"],
        command_adapters=adapters,
    )

    assert exit_code is ExitCode.SUCCESS
    assert adapter.calls == [
        (action, "review_synthetic", "token=synthetic-secret", "--json")
    ]
    output = json.loads(capsys.readouterr().out)
    assert output == {"command": "review", "status": "ok"}
    assert "synthetic-secret" not in json.dumps(output)


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "--json", "capture"],
        ["--json", "--unknown", "capture"],
        ["--json", "--dry-run", "--unknown", "capture"],
        ["--dry-run", "--dry-run", "capture", "token=synthetic-secret", "--json"],
        ["--dry-run", "capture", "--dry-run", "token=synthetic-secret", "--json"],
    ],
)
def test_malformed_top_level_argv_is_usage(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = RecordingCommandAdapter()

    assert (
        main(argv, command_adapters=CommandAdapterRegistry({"capture": adapter}))
        is ExitCode.USAGE
    )
    assert adapter.calls == []
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "invalid"
    assert "synthetic-secret" not in output
