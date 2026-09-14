from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from open_brain_engine.engine import ManagedProvider

import open_brain.services.provider_credentials as credential_module
from open_brain.services.provider_credentials import (
    CredentialCommandResult,
    CredentialStoreFailure,
    OsCredentialStore,
    discover_credential_store,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes | None, dict[str, str], bool]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        input_bytes: bytes | None,
        environment: Mapping[str, str],
        capture_stdout: bool,
    ) -> CredentialCommandResult:
        self.calls.append((tuple(command), input_bytes, dict(environment), capture_stdout))
        operation = command[1]
        if operation in {"find-generic-password", "lookup"} and capture_stdout:
            return CredentialCommandResult(0, b"synthetic-provider-secret\n")
        return CredentialCommandResult(0, b"")


@pytest.mark.parametrize(
    ("kind", "executable", "store_operation", "lookup_operation", "delete_operation"),
    (
        (
            "macos_keychain",
            Path("/usr/bin/security"),
            "add-generic-password",
            "find-generic-password",
            "delete-generic-password",
        ),
        (
            "linux_secret_service",
            Path("/usr/bin/secret-tool"),
            "store",
            "lookup",
            "clear",
        ),
    ),
)
def test_store_operations_keep_credentials_out_of_arguments_environment_and_output(
    kind: str,
    executable: Path,
    store_operation: str,
    lookup_operation: str,
    delete_operation: str,
) -> None:
    runner = FakeRunner()
    store = OsCredentialStore(
        executable,
        kind=kind,
        environment={"HOME": "/synthetic/home", "UNSAFE": "private"},
        runner=runner,
    )
    provider = ManagedProvider.OPENAI_API

    store.store(provider, "synthetic-provider-secret")
    assert store.contains(provider)
    assert store.resolve(provider) == "synthetic-provider-secret"
    store.delete(provider)

    assert [call[0][1] for call in runner.calls] == [
        store_operation,
        lookup_operation,
        lookup_operation,
        delete_operation,
    ]
    for command, _input, environment, _capture in runner.calls:
        assert "synthetic-provider-secret" not in command
        assert "synthetic-provider-secret" not in environment.values()
    assert runner.calls[0][1] in {
        b"synthetic-provider-secret",
        b"synthetic-provider-secret\n",
    }
    assert runner.calls[0][3] is False
    assert runner.calls[1][3] is False
    assert runner.calls[2][3] is True
    if kind == "macos_keychain":
        assert runner.calls[0][0][-1] == "-w"


def test_credential_validation_and_missing_lookup_fail_closed() -> None:
    class MissingRunner(FakeRunner):
        def __call__(
            self,
            command: Sequence[str],
            *,
            input_bytes: bytes | None,
            environment: Mapping[str, str],
            capture_stdout: bool,
        ) -> CredentialCommandResult:
            super().__call__(
                command,
                input_bytes=input_bytes,
                environment=environment,
                capture_stdout=capture_stdout,
            )
            return CredentialCommandResult(44, b"")

    store = OsCredentialStore(
        Path("/usr/bin/security"),
        kind="macos_keychain",
        environment={},
        runner=MissingRunner(),
    )

    assert store.resolve(ManagedProvider.ANTHROPIC_API) is None
    assert not store.contains(ManagedProvider.ANTHROPIC_API)
    with pytest.raises(CredentialStoreFailure, match="invalid credential"):
        store.store(ManagedProvider.ANTHROPIC_API, "secret\nargument")
    with pytest.raises(CredentialStoreFailure, match="unsupported"):
        store.contains(ManagedProvider.CLAUDE_SUBSCRIPTION)


def test_linux_store_discovery_requires_an_explicit_binary_and_session_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        credential_module,
        "_regular_executable",
        lambda path: path if path == Path("/usr/bin/secret-tool") else None,
    )

    assert discover_credential_store("linux", {"HOME": "/home/openbrain"}) is None
    store = discover_credential_store(
        "linux",
        {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "HOME": "/home/openbrain",
            "OPENAI_API_KEY": "must-not-pass",
        },
        runner=FakeRunner(),
    )

    assert store is not None
    assert store.kind == "linux_secret_service"
    assert "OPENAI_API_KEY" not in store._environment  # noqa: SLF001
