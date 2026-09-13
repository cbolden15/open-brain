"""Bounded OS credential-store access for direct graph providers."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from open_brain_engine.engine import ManagedProvider

MAX_CREDENTIAL_BYTES = 4096
_SERVICE = "io.openbrain.api-key"
_DIRECT_PROVIDERS = frozenset(
    {
        ManagedProvider.OPENAI_API,
        ManagedProvider.ANTHROPIC_API,
        ManagedProvider.GEMINI_API,
    }
)


class CredentialStoreFailure(RuntimeError):
    """The selected OS credential store could not complete an operation."""


@dataclass(frozen=True, slots=True)
class CredentialCommandResult:
    returncode: int
    stdout: bytes


class CredentialCommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        input_bytes: bytes | None,
        environment: Mapping[str, str],
        capture_stdout: bool,
    ) -> CredentialCommandResult: ...


class BoundedCredentialCommandRunner:
    """Run one explicit credential helper without exposing output or arguments."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        input_bytes: bytes | None,
        environment: Mapping[str, str],
        capture_stdout: bool,
    ) -> CredentialCommandResult:
        process: subprocess.Popen[bytes] | None = None
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                    tuple(command),
                    stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                    stdout=output if capture_stdout else subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd="/",
                    env=dict(environment),
                    close_fds=True,
                    start_new_session=True,
                )
                process.communicate(input_bytes, timeout=15)
                if capture_stdout and output.tell() > MAX_CREDENTIAL_BYTES + 1:
                    raise CredentialStoreFailure("credential output exceeded its bound")
                output.seek(0)
                stdout = output.read(MAX_CREDENTIAL_BYTES + 2) if capture_stdout else b""
                return CredentialCommandResult(process.returncode, stdout)
            except subprocess.TimeoutExpired:
                if process is not None:
                    _terminate(process)
                raise CredentialStoreFailure("credential store timed out") from None
            except CredentialStoreFailure:
                raise
            except (OSError, subprocess.SubprocessError):
                if process is not None and process.poll() is None:
                    _terminate(process)
                raise CredentialStoreFailure("credential store unavailable") from None


class OsCredentialStore:
    """Provider-scoped access to macOS Keychain or Linux Secret Service."""

    def __init__(
        self,
        executable: Path,
        *,
        kind: str,
        environment: Mapping[str, str],
        runner: CredentialCommandRunner | None = None,
    ) -> None:
        if kind not in {"macos_keychain", "linux_secret_service"}:
            raise ValueError("invalid credential store kind")
        if not executable.is_absolute():
            raise ValueError("credential store executable must be absolute")
        self.executable = executable
        self.kind = kind
        self._environment = dict(environment)
        self._runner = BoundedCredentialCommandRunner() if runner is None else runner

    def store(self, provider: ManagedProvider, credential: str) -> None:
        _provider(provider)
        secret = _credential(credential)
        result = self._run(
            self._store_arguments(provider),
            input_bytes=secret.encode("utf-8") + (b"\n" if self.kind == "macos_keychain" else b""),
            capture_stdout=False,
        )
        if result.returncode != 0:
            raise CredentialStoreFailure("credential store rejected the key")

    def resolve(self, provider: ManagedProvider) -> str | None:
        _provider(provider)
        result = self._run(
            self._lookup_arguments(provider),
            input_bytes=None,
            capture_stdout=True,
        )
        if result.returncode != 0:
            return None
        payload = result.stdout.removesuffix(b"\n")
        try:
            return _credential(payload.decode("utf-8"))
        except (UnicodeDecodeError, CredentialStoreFailure):
            raise CredentialStoreFailure("credential store returned an invalid key") from None

    def contains(self, provider: ManagedProvider) -> bool:
        _provider(provider)
        result = self._run(
            self._lookup_arguments(provider, reveal=False),
            input_bytes=None,
            capture_stdout=False,
        )
        return result.returncode == 0

    def delete(self, provider: ManagedProvider) -> None:
        _provider(provider)
        result = self._run(
            self._delete_arguments(provider),
            input_bytes=None,
            capture_stdout=False,
        )
        if result.returncode not in ({0, 44} if self.kind == "macos_keychain" else {0}):
            raise CredentialStoreFailure("credential store rejected removal")

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        input_bytes: bytes | None,
        capture_stdout: bool,
    ) -> CredentialCommandResult:
        return self._runner(
            (os.fspath(self.executable), *arguments),
            input_bytes=input_bytes,
            environment=self._environment,
            capture_stdout=capture_stdout,
        )

    def _store_arguments(self, provider: ManagedProvider) -> tuple[str, ...]:
        if self.kind == "macos_keychain":
            return (
                "add-generic-password",
                "-a",
                provider.value,
                "-s",
                _SERVICE,
                "-U",
                "-w",
            )
        return (
            "store",
            "--label=Open Brain API key",
            "application",
            "open-brain",
            "provider",
            provider.value,
        )

    def _lookup_arguments(
        self, provider: ManagedProvider, *, reveal: bool = True
    ) -> tuple[str, ...]:
        if self.kind == "macos_keychain":
            suffix = ("-w",) if reveal else ()
            return (
                "find-generic-password",
                "-a",
                provider.value,
                "-s",
                _SERVICE,
                *suffix,
            )
        return (
            "lookup",
            "application",
            "open-brain",
            "provider",
            provider.value,
        )

    def _delete_arguments(self, provider: ManagedProvider) -> tuple[str, ...]:
        if self.kind == "macos_keychain":
            return (
                "delete-generic-password",
                "-a",
                provider.value,
                "-s",
                _SERVICE,
            )
        return (
            "clear",
            "application",
            "open-brain",
            "provider",
            provider.value,
        )


def discover_credential_store(
    platform_name: str,
    environment: Mapping[str, object],
    *,
    runner: CredentialCommandRunner | None = None,
) -> OsCredentialStore | None:
    """Discover one supported store without searching PATH or touching secrets."""
    filtered = _credential_environment(environment)
    if platform_name == "darwin":
        executable = _regular_executable(Path("/usr/bin/security"))
        return (
            None
            if executable is None
            else OsCredentialStore(
                executable,
                kind="macos_keychain",
                environment=filtered,
                runner=runner,
            )
        )
    if platform_name.startswith("linux"):
        if "DBUS_SESSION_BUS_ADDRESS" not in filtered:
            return None
        for candidate in (Path("/usr/bin/secret-tool"), Path("/usr/local/bin/secret-tool")):
            executable = _regular_executable(candidate)
            if executable is not None:
                return OsCredentialStore(
                    executable,
                    kind="linux_secret_service",
                    environment=filtered,
                    runner=runner,
                )
    return None


def _regular_executable(path: Path) -> Path | None:
    try:
        selected = path.resolve(strict=True)
        metadata = selected.stat(follow_symlinks=False)
    except OSError:
        return None
    return (
        selected
        if stat.S_ISREG(metadata.st_mode) and metadata.st_mode & 0o111
        else None
    )


def _credential_environment(source: Mapping[str, object]) -> dict[str, str]:
    allowed = (
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
    )
    return {
        name: value
        for name in allowed
        if isinstance((value := source.get(name)), str) and value
    }


def _provider(provider: ManagedProvider) -> None:
    if provider not in _DIRECT_PROVIDERS:
        raise CredentialStoreFailure("unsupported credential provider")


def _credential(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_CREDENTIAL_BYTES
        or "\r" in value
        or "\n" in value
        or "\x00" in value
    ):
        raise CredentialStoreFailure("invalid credential")
    return value


def validate_credential(value: str) -> str:
    """Validate a session-only credential without persisting it."""
    return _credential(value)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            raise CredentialStoreFailure("credential helper cleanup failed") from None


__all__ = [
    "MAX_CREDENTIAL_BYTES",
    "BoundedCredentialCommandRunner",
    "CredentialCommandResult",
    "CredentialCommandRunner",
    "CredentialStoreFailure",
    "OsCredentialStore",
    "discover_credential_store",
    "validate_credential",
]
