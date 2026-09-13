from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
)
from open_brain_engine.core.ports import TextModelRequest, TextModelResult

from open_brain_legacy._compat.open_brain.config import (
    AppConfig,
    NamedSecretRef,
    RetainedRoots,
    SecretRef,
)
from open_brain_legacy.production.providers import (
    LocalProviderRuntimeConfig,
    ProductionProviderConfigError,
    ProviderComposition,
    ProviderTransportError,
    StdlibLocalModelTransport,
    compose_production_provider,
    load_private_provider_config,
)


@dataclass
class _Response:
    status: int
    payload: bytes
    reads: list[int] = field(default_factory=list)

    def read(self, maximum_bytes: int) -> bytes:
        self.reads.append(maximum_bytes)
        return self.payload


@dataclass
class _Connection:
    response: _Response
    requests: list[tuple[str, str, bytes, dict[str, str]]] = field(default_factory=list)
    closed: bool = False

    def request(
        self, method: str, target: str, body: bytes, headers: dict[str, str]
    ) -> _Response:
        self.requests.append((method, target, body, headers))
        return self.response

    def close(self) -> None:
        self.closed = True


@dataclass
class _Factory:
    connection: _Connection
    calls: list[tuple[str, str, int, float]] = field(default_factory=list)

    def open(self, *, scheme: str, hostname: str, port: int, timeout: float) -> _Connection:
        self.calls.append((scheme, hostname, port, timeout))
        return self.connection


def _request() -> TextModelRequest:
    return TextModelRequest.create(
        request_id="request.synthetic-001",
        purpose="synthetic",
        prompt="Synthetic prompt",
        timeout_seconds=2.0,
        max_output_bytes=64,
    )


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _cloud_privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_CONFIRMED,
        policy_version="privacy-v1",
        authority=Authority(cloud=True, external_egress=False),
        confirmation_ref="confirmation.synthetic-001",
    )


def _app_config(tmp_path: Path) -> AppConfig:
    roots = {
        name: tmp_path / name
        for name in ("work", "personal", "capture", "saved", "state", "backup")
    }
    for root in roots.values():
        root.mkdir()
    return AppConfig(
        roots=RetainedRoots(
            work=roots["work"],
            personal=roots["personal"],
            capture=roots["capture"],
            saved_content=roots["saved"],
            state=roots["state"],
        ),
        backup=roots["backup"],
        provider="local",
    )


def _private_provider_config(tmp_path: Path, *, endpoint: str) -> Path:
    path = tmp_path / "provider.json"
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "local_endpoint": endpoint,
                "local_model": "synthetic-model",
                "cloud_module": "open_brain_legacy.providers.optional_cloud",
                "cloud_model": "synthetic-cloud-model",
                "credential_name": "provider_token",
            }
        )
    )
    path.chmod(0o600)
    return path


def test_enabled_optional_cloud_loads_without_preload_in_a_fresh_process() -> None:
    source = Path(__file__).parents[3] / "src"
    program = f"""
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, {str(source)!r})
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.core.ports import TextModelRequest
from open_brain_legacy.production.providers import LocalProviderRuntimeConfig, ProviderComposition

class LocalTransport:
    def complete(self, **kwargs):
        raise AssertionError(kwargs)

class OpenAI:
    def __init__(self, **kwargs):
        self.responses = SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(output_text="fresh cloud result")
        )

assert "open_brain_legacy.providers.optional_cloud" not in sys.modules
service = ProviderComposition(
    config=LocalProviderRuntimeConfig(
        provider_name="cloud",
        cloud_enabled=True,
        local_endpoint="http://127.0.0.1:11434/api",
        local_model="synthetic-local",
        cloud_module="open_brain_legacy.providers.optional_cloud",
        cloud_model="synthetic-cloud",
    ),
    local_transport=LocalTransport(),
    resolve_cloud_secret=lambda: "synthetic-credential",
).build()
assert "open_brain_legacy.providers.optional_cloud" in sys.modules
sys.modules["openai"] = SimpleNamespace(OpenAI=OpenAI)
request = TextModelRequest.create(
    request_id="request.fresh-cloud",
    purpose="synthetic",
    prompt="safe prompt",
    timeout_seconds=1.0,
    max_output_bytes=64,
)
privacy = PrivacyDecision.create(
    tier=PrivacyTier.PERSONAL,
    reason=PrivacyReason.PERSONAL_CONFIRMED,
    policy_version="privacy-v1",
    authority=Authority(cloud=True, external_egress=False),
    confirmation_ref="confirmation.fresh-cloud",
)
result = service.complete(request, privacy=privacy)
payload = {{"error": result.error_code, "text": result.value.text if result.value else None}}
print(json.dumps(payload))
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"error": None, "text": "fresh cloud result"}


def test_stdlib_local_transport_is_static_bounded_and_closes() -> None:
    response = _Response(200, json.dumps({"response": "Synthetic result"}).encode())
    connection = _Connection(response)
    factory = _Factory(connection)
    transport = StdlibLocalModelTransport(connection_factory=factory)

    result = transport.complete(
        endpoint="http://127.0.0.1:11434/api/generate",
        model="synthetic-model",
        prompt="Synthetic prompt",
        timeout_seconds=2.0,
        max_output_bytes=64,
    )

    assert result == "Synthetic result"
    assert factory.calls == [("http", "127.0.0.1", 11434, 2.0)]
    method, target, body, headers = connection.requests[0]
    assert (method, target) == ("POST", "/api/generate")
    assert json.loads(body) == {
        "format": "json",
        "model": "synthetic-model",
        "options": {"temperature": 0},
        "prompt": "Synthetic prompt",
        "stream": False,
    }
    assert headers == {"Content-Type": "application/json"}
    assert response.reads == [16_449]
    assert connection.closed is True


@pytest.mark.parametrize(
    "response",
    [
        _Response(500, b"synthetic detail"),
        _Response(200, b"not-json"),
        _Response(200, json.dumps({"response": "x" * 65}).encode()),
        _Response(200, json.dumps({"unexpected": "value"}).encode()),
    ],
)
def test_stdlib_local_transport_maps_response_failures_without_residue(
    response: _Response,
) -> None:
    connection = _Connection(response)
    transport = StdlibLocalModelTransport(connection_factory=_Factory(connection))

    with pytest.raises(ProviderTransportError) as raised:
        transport.complete(
            endpoint="http://127.0.0.1:11434/api/generate",
            model="synthetic-model",
            prompt="Synthetic prompt",
            timeout_seconds=2.0,
            max_output_bytes=64,
        )

    assert str(raised.value) == "local provider transport failed"
    assert "synthetic detail" not in repr(raised.value)
    assert connection.closed is True


def test_provider_composition_selects_local_without_cloud_or_secret_access() -> None:
    response = _Response(200, json.dumps({"response": "Synthetic result"}).encode())
    factory = _Factory(_Connection(response))
    secret_calls = 0

    def resolve_secret() -> str | None:
        nonlocal secret_calls
        secret_calls += 1
        return "synthetic-secret"

    service = ProviderComposition(
        config=LocalProviderRuntimeConfig(
            provider_name="local",
            cloud_enabled=False,
            local_endpoint="http://127.0.0.1:11434/api/generate",
            local_model="synthetic-model",
            cloud_module="open_brain_legacy.providers.optional_cloud",
            cloud_model="synthetic-cloud-model",
        ),
        local_transport=StdlibLocalModelTransport(connection_factory=factory),
        resolve_cloud_secret=resolve_secret,
    ).build()

    result = service.complete(_request(), privacy=_privacy())

    assert result.value == TextModelResult(text="Synthetic result", provider_name="local")
    assert result.error_code is None
    assert secret_calls == 0


def test_production_provider_uses_owner_config_without_resolving_cloud_secret(
    tmp_path: Path,
) -> None:
    response = _Response(200, json.dumps({"response": "Synthetic result"}).encode())
    factory = _Factory(_Connection(response))
    file_reads = 0

    def read_secret(_path: Path) -> str:
        nonlocal file_reads
        file_reads += 1
        return "synthetic-secret"

    service = compose_production_provider(
        app_config=_app_config(tmp_path),
        config_path=_private_provider_config(
            tmp_path,
            endpoint="http://127.0.0.1:11434/api/generate",
        ),
        environment={},
        file_reader=read_secret,
        local_transport=StdlibLocalModelTransport(connection_factory=factory),
    )

    result = service.complete(_request(), privacy=_privacy())

    assert result.value == TextModelResult(text="Synthetic result", provider_name="local")
    assert result.error_code is None
    assert file_reads == 0


def test_production_provider_constructs_authorized_cloud_lazily(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_calls: list[dict[str, object]] = []
    constructor_calls: list[dict[str, object]] = []

    class Responses:
        def create(self, **kwargs: object) -> object:
            sdk_calls.append(kwargs)
            return SimpleNamespace(output_text="Synthetic cloud result")

    def constructor(**kwargs: object) -> object:
        constructor_calls.append(kwargs)
        return SimpleNamespace(responses=Responses())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=constructor))
    config = replace(
        _app_config(tmp_path),
        provider="cloud",
        cloud_enabled=True,
        egress_enabled=True,
        secret_refs=(
            NamedSecretRef(
                "provider_token",
                SecretRef.parse("env:OPEN_BRAIN_PROVIDER_TOKEN"),
            ),
        ),
    )
    service = compose_production_provider(
        app_config=config,
        config_path=_private_provider_config(
            tmp_path,
            endpoint="http://127.0.0.1:11434/api/generate",
        ),
        environment={"OPEN_BRAIN_PROVIDER_TOKEN": "synthetic-credential"},
        file_reader=lambda _path: "",
    )

    result = service.complete(_request(), privacy=_cloud_privacy())

    assert result.value == TextModelResult(
        text="Synthetic cloud result",
        provider_name="cloud",
    )
    assert constructor_calls == [
        {"api_key": "synthetic-credential", "max_retries": 0}
    ]
    assert sdk_calls == [
        {
            "input": "Synthetic prompt",
            "max_output_tokens": 64,
            "model": "synthetic-cloud-model",
            "store": False,
            "timeout": 2.0,
        }
    ]


def test_private_provider_config_is_owner_only_canonical_and_loopback_only(
    tmp_path: Path,
) -> None:
    path = _private_provider_config(
        tmp_path,
        endpoint="http://127.0.0.1:11434/api/generate",
    )
    assert load_private_provider_config(path).local_model == "synthetic-model"

    path.chmod(0o644)
    with pytest.raises(ProductionProviderConfigError):
        load_private_provider_config(path)

    remote = _private_provider_config(
        tmp_path,
        endpoint="https://example.test/api/generate",
    )
    with pytest.raises(ProductionProviderConfigError):
        load_private_provider_config(remote)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user@example.test/api",
        "http://example.test/api?query=1",
        "http://example.test/api",
        "ftp://example.test/api",
        "http://example.test",
    ],
)
def test_provider_runtime_config_rejects_ambient_or_unsafe_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError, match="invalid local provider endpoint"):
        LocalProviderRuntimeConfig(
            provider_name="local",
            cloud_enabled=False,
            local_endpoint=endpoint,
            local_model="synthetic-model",
            cloud_module="open_brain_legacy.providers.optional_cloud",
            cloud_model="synthetic-cloud-model",
        )
