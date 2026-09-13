from __future__ import annotations

import os
import resource
import signal
import socket
import ssl
import subprocess
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    RawAssetRef,
)
from open_brain_engine.core.ports import FetchRequest, StagedExecutionRequest

from open_brain_legacy.capture.egress import OutboundFetcher, PinnedRequest, TransportResponse
from open_brain_legacy.production import (
    ContentAddressedDerivedAssetStore,
    DnsPinnedHttpTransport,
    ProductionRuntimeError,
    RuntimeFailureCode,
    RuntimeLimits,
    StagedLocalModelRuntime,
)


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="test-v1",
        authority=Authority(cloud=True, external_egress=True),
    )


@dataclass
class _Resolver:
    answers: dict[str, tuple[str, ...]]
    calls: list[str] = field(default_factory=list)

    def resolve(self, hostname: str) -> tuple[str, ...]:
        self.calls.append(hostname)
        return self.answers[hostname]


@dataclass
class _Stream:
    chunks: list[bytes]
    closed: bool = False

    def read(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) <= size:
            return chunk
        self.chunks.insert(0, chunk[size:])
        return chunk[:size]

    def close(self) -> None:
        self.closed = True


@dataclass
class _Connection:
    response: TransportResponse

    def request(self, request: PinnedRequest) -> TransportResponse:
        return self.response


@dataclass
class _ConnectionFactory:
    responses: list[TransportResponse]
    requests: list[PinnedRequest] = field(default_factory=list)

    def open(self, request: PinnedRequest) -> _Connection:
        self.requests.append(request)
        return _Connection(self.responses.pop(0))


def _response(*, status: int = 200, headers: dict[str, str] | None = None) -> TransportResponse:
    return TransportResponse(
        status=status,
        headers={"content-type": "text/html"} if headers is None else headers,
        stream=_Stream([b"synthetic"]),
    )


def test_dns_pinned_transport_keeps_redirects_cookies_and_tls_host_separate() -> None:
    factory = _ConnectionFactory(
        [
            _response(status=302, headers={"location": "https://child.good.example/next"}),
            _response(),
        ]
    )
    transport = DnsPinnedHttpTransport(enabled=True, connection_factory=factory)
    fetcher = OutboundFetcher(
        resolver=_Resolver(
            {
                "good.example": ("8.8.8.8",),
                "child.good.example": ("9.9.9.9",),
            }
        ),
        transport=transport,
        cookies={"good.example": "session=synthetic"},
    )

    result = fetcher.fetch(
        FetchRequest(
            request_id="fetch.synthetic-001",
            url="https://good.example/start",
            timeout_seconds=1.0,
            max_bytes=1024,
            max_redirects=1,
            allowed_cookie_domains=("good.example",),
        ),
        privacy=_privacy(),
    )

    assert result.final_url == "https://child.good.example/next"
    assert [request.pinned_address for request in factory.requests] == ["8.8.8.8", "9.9.9.9"]
    assert [request.hostname for request in factory.requests] == [
        "good.example",
        "child.good.example",
    ]
    assert all(request.scheme == "https" for request in factory.requests)
    assert all(request.headers["host"] == request.hostname for request in factory.requests)
    assert [request.headers.get("cookie") for request in factory.requests] == [
        "session=synthetic",
        "session=synthetic",
    ]


def test_https_connection_uses_the_pinned_ip_but_preserves_the_tls_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_legacy.production import transport as transport_module

    request = PinnedRequest(
        scheme="https",
        hostname="good.example",
        port=443,
        pinned_address="8.8.8.8",
        target="/",
        headers={"host": "good.example"},
        timeout_seconds=1.0,
    )
    calls: list[object] = []
    raw_socket = object()

    def create_connection(endpoint: object, timeout: object) -> socket.socket:
        calls.append((endpoint, timeout))
        return cast(socket.socket, raw_socket)

    monkeypatch.setattr(
        'open_brain_legacy.production.transport.socket.create_connection',
        create_connection,
    )
    context = _SyntheticTlsContext(calls)

    connection = transport_module._PinnedHTTPSConnection(
        request, cast(ssl.SSLContext, context)
    )
    connection.connect()

    assert calls == [(("8.8.8.8", 443), 1.0), (raw_socket, "good.example")]


@dataclass
class _SyntheticTlsContext:
    calls: list[object]

    def wrap_socket(self, raw_socket: object, *, server_hostname: str) -> object:
        self.calls.append((raw_socket, server_hostname))
        return raw_socket


def test_runtime_primitives_are_disabled_by_default(tmp_path: Path) -> None:
    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    asset_store = ContentAddressedDerivedAssetStore(root=assets_root)
    with pytest.raises(ProductionRuntimeError) as asset_error:
        asset_store.put(data=b"synthetic", media_type="application/octet-stream")
    assert asset_error.value.code is RuntimeFailureCode.DISABLED

    transport = DnsPinnedHttpTransport()
    with pytest.raises(ProductionRuntimeError) as transport_error:
        transport.request(
            PinnedRequest(
                scheme="https",
                hostname="good.example",
                port=443,
                pinned_address="8.8.8.8",
                target="/",
                headers={"host": "good.example"},
                timeout_seconds=1.0,
            )
        )
    assert transport_error.value.code is RuntimeFailureCode.DISABLED

    runtime = StagedLocalModelRuntime(
        command=("/synthetic/model",),
        asset_reader=_SyntheticReader({}),
    )
    with pytest.raises(ProductionRuntimeError) as runtime_error:
        runtime.execute(_staged_request(()), privacy=_privacy())
    assert runtime_error.value.code is RuntimeFailureCode.DISABLED


def test_derived_asset_store_confines_paths_and_replays_only_immutable_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    root.mkdir()
    store = ContentAddressedDerivedAssetStore(root=root, enabled=True)
    ref = store.put(data=b"synthetic", media_type="application/octet-stream")

    assert store.replay(ref) == b"synthetic"
    digest = sha256(b"synthetic").hexdigest()
    object_path = root / "sha256" / digest[:2] / digest
    object_path.write_bytes(b"modified")
    with pytest.raises(ProductionRuntimeError) as mutation_error:
        store.replay(ref)
    assert mutation_error.value.code is RuntimeFailureCode.INTEGRITY

    object_path.write_bytes(b"synthetic")
    object_path.unlink()
    object_path.symlink_to(tmp_path / "outside")
    with pytest.raises(ProductionRuntimeError) as symlink_error:
        store.replay(ref)
    assert symlink_error.value.code is RuntimeFailureCode.CONFINEMENT

    object_path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"synthetic")
    os.link(outside, object_path)
    with pytest.raises(ProductionRuntimeError) as hardlink_error:
        store.replay(ref)
    assert hardlink_error.value.code is RuntimeFailureCode.CONFINEMENT

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root, target_is_directory=True)
    linked_store = ContentAddressedDerivedAssetStore(root=linked_root, enabled=True)
    with pytest.raises(ProductionRuntimeError) as root_error:
        linked_store.put(data=b"another", media_type="application/octet-stream")
    assert root_error.value.code is RuntimeFailureCode.CONFINEMENT


@dataclass
class _SyntheticReader:
    blobs: dict[str, bytes]

    def read(self, asset: RawAssetRef) -> bytes:
        return self.blobs[str(asset.asset_id)]


def _staged_request(assets: tuple[RawAssetRef, ...]) -> StagedExecutionRequest:
    return StagedExecutionRequest(
        request_id="runtime.synthetic-001",
        purpose="synthetic",
        prompt="synthetic prompt",
        readable_assets=assets,
        allowed_network_hosts=(),
        timeout_seconds=1.0,
        max_output_bytes=64,
    )


def test_staged_runtime_fails_closed_and_plans_full_linux_confinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_legacy.production import runtime as runtime_module

    runtime = StagedLocalModelRuntime(
        command=("/synthetic/model", "--fixed-flag"),
        asset_reader=_SyntheticReader({}),
        enabled=True,
        limits=RuntimeLimits(
            wall_seconds=2.0,
            cpu_seconds=1,
            memory_bytes=4 * 1024 * 1024,
            max_processes=2,
            max_stdout_bytes=64,
            max_stderr_bytes=64,
        ),
        bubblewrap_path="/synthetic/bwrap",
    )
    monkeypatch.setattr(runtime_module, "_runtime_controls_supported", lambda: False)
    with pytest.raises(ProductionRuntimeError) as unsupported_error:
        runtime.execute(_staged_request(()), privacy=_privacy())
    assert unsupported_error.value.code is RuntimeFailureCode.UNSUPPORTED_CONTROL

    monkeypatch.setattr(runtime_module, "_runtime_controls_supported", lambda: True)
    command = runtime._sandbox_command(Path("/synthetic/stage"))
    assert command[:2] == ("/synthetic/bwrap", "--die-with-parent")
    assert {"--new-session", "--unshare-all", "--clearenv", "--tmpfs", "--ro-bind"} <= set(command)
    assert "/synthetic/stage" in command and "/input/assets" in command

    calls: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        'open_brain_legacy.production.runtime.os.killpg',
        lambda pid, sig: calls.append((pid, sig)),
    )
    runtime_module._kill_process_group(_FakeProcess(pid=123))
    assert calls == [(123, signal.SIGKILL)]


@dataclass
class _FakeProcess:
    pid: int
    returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_staged_runtime_rejects_network_authority_and_invalid_resource_limits() -> None:
    with pytest.raises(ValueError, match="invalid runtime limits"):
        RuntimeLimits(
            wall_seconds=0.0,
            cpu_seconds=1,
            memory_bytes=1,
            max_processes=1,
            max_stdout_bytes=1,
            max_stderr_bytes=1,
        )

    runtime = StagedLocalModelRuntime(
        command=("/synthetic/model",),
        asset_reader=_SyntheticReader({}),
        enabled=True,
        bubblewrap_path="/synthetic/bwrap",
    )
    request = StagedExecutionRequest(
        request_id="runtime.synthetic-002",
        purpose="synthetic",
        prompt="synthetic prompt",
        readable_assets=(),
        allowed_network_hosts=("good.example",),
        timeout_seconds=1.0,
        max_output_bytes=64,
    )
    with pytest.raises(ProductionRuntimeError) as error:
        runtime.execute(request, privacy=_privacy())
    assert error.value.code is RuntimeFailureCode.UNSUPPORTED_CONTROL


def test_staged_runtime_bounds_output_timeout_resources_and_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from open_brain_legacy.production import runtime as runtime_module

    limits = RuntimeLimits(
        wall_seconds=1.0,
        cpu_seconds=2,
        memory_bytes=3 * 1024 * 1024,
        max_processes=4,
        max_stdout_bytes=16,
        max_stderr_bytes=16,
    )
    resource_calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        'open_brain_legacy.production.runtime.resource.setrlimit',
        lambda kind, value: resource_calls.append((kind, value)),
    )
    runtime_module._apply_limits(limits)()
    assert resource_calls == [
        (resource.RLIMIT_CPU, (2, 2)),
        (resource.RLIMIT_AS, (3 * 1024 * 1024, 3 * 1024 * 1024)),
        (resource.RLIMIT_NPROC, (4, 4)),
        (resource.RLIMIT_FSIZE, (16, 16)),
    ]

    captured: dict[str, object] = {}

    def fake_popen(*args: object, **kwargs: object) -> _PipeProcess:
        captured.update(kwargs)
        return _PipeProcess(stdout=b"ok", stderr=b"")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert runtime_module._run_sandboxed(("synthetic",), tmp_path, limits) == b"ok"
    assert captured["env"] == {}
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True

    output_process = _PipeProcess(stdout=b"too-large", stderr=b"")
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        'open_brain_legacy.production.runtime.os.killpg',
        lambda pid, sig: killed.append((pid, sig)),
    )
    _, output_failure = runtime_module._bounded_output(
        output_process,
        RuntimeLimits(
            wall_seconds=1.0,
            cpu_seconds=1,
            memory_bytes=1,
            max_processes=1,
            max_stdout_bytes=2,
            max_stderr_bytes=2,
        ),
    )
    assert output_failure is RuntimeFailureCode.OUTPUT_LIMIT
    assert killed == [(987, signal.SIGKILL)]

    timeout_process = _PipeProcess(stdout=None, stderr=None)
    _, timeout_failure = runtime_module._bounded_output(
        timeout_process,
        RuntimeLimits(
            wall_seconds=0.001,
            cpu_seconds=1,
            memory_bytes=1,
            max_processes=1,
            max_stdout_bytes=2,
            max_stderr_bytes=2,
        ),
    )
    timeout_process.close_writers()
    assert timeout_failure is RuntimeFailureCode.TIMEOUT


class _PipeProcess:
    def __init__(self, *, stdout: bytes | None, stderr: bytes | None) -> None:
        self.pid = 987
        self.returncode: int | None = None
        self.stdout, self._stdout_writer = _pipe_with(stdout)
        self.stderr, self._stderr_writer = _pipe_with(stderr)

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    def close_writers(self) -> None:
        self._stdout_writer.close()
        self._stderr_writer.close()


def _pipe_with(value: bytes | None) -> tuple[BinaryIO, BinaryIO]:
    reader, writer = os.pipe()
    stream = os.fdopen(reader, "rb", closefd=True)
    writer_stream = os.fdopen(writer, "wb", closefd=True)
    if value is not None:
        writer_stream.write(value)
        writer_stream.close()
    return stream, writer_stream
