from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
)
from open_brain_engine.core.policy import BoundaryErrorCode
from open_brain_engine.core.ports import TextModelRequest, TextModelResult
from open_brain_engine.providers.base import OptionalExtraUnavailable, ProviderFailure

from open_brain_legacy.providers.optional_cloud import OpenAICloudProvider, create_provider


def _request(
    *,
    maximum: int = 128,
    prompt: str = "Synthetic prompt",
) -> TextModelRequest:
    return TextModelRequest.create(
        request_id="request.cloud-001",
        purpose="synthetic",
        prompt=prompt,
        timeout_seconds=2.0,
        max_output_bytes=maximum,
    )


def _privacy(*, cloud: bool = True) -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=(
            PrivacyReason.PERSONAL_CONFIRMED
            if cloud
            else PrivacyReason.PERSONAL_LOCAL_ONLY
        ),
        policy_version="privacy-v1",
        authority=Authority(cloud=cloud, external_egress=False),
        confirmation_ref="confirmation.synthetic-001" if cloud else None,
    )


@dataclass
class _Response:
    output_text: object


@dataclass
class _Responses:
    result: object = field(default_factory=lambda: _Response("Synthetic result"))
    calls: list[dict[str, object]] = field(default_factory=list)

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass
class _Client:
    responses: _Responses


def test_openai_cloud_provider_uses_bounded_stateless_responses_call() -> None:
    responses = _Responses()
    provider = OpenAICloudProvider(model="synthetic-model", client=_Client(responses))

    result = provider.complete(_request(), privacy=_privacy())

    assert result == TextModelResult(text="Synthetic result", provider_name="cloud")
    assert responses.calls == [
        {
            "input": "Synthetic prompt",
            "max_output_tokens": 128,
            "model": "synthetic-model",
            "store": False,
            "timeout": 2.0,
        }
    ]


def test_openai_cloud_provider_rejects_missing_cloud_authority_before_client_call() -> None:
    responses = _Responses()
    provider = OpenAICloudProvider(model="synthetic-model", client=_Client(responses))

    with pytest.raises(ProviderFailure) as raised:
        provider.complete(_request(), privacy=_privacy(cloud=False))

    assert raised.value.code is BoundaryErrorCode.CLOUD_AUTHORITY_REQUIRED
    assert responses.calls == []


def test_openai_cloud_provider_rejects_credential_finding_before_client_call() -> None:
    responses = _Responses()
    provider = OpenAICloudProvider(model="synthetic-model", client=_Client(responses))

    with pytest.raises(ProviderFailure) as raised:
        provider.complete(
            _request(prompt="api" + "_key=" + "A" * 32),
            privacy=_privacy(),
        )

    assert raised.value.code is BoundaryErrorCode.PROVIDER_REJECTED
    assert responses.calls == []


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (TimeoutError("synthetic timeout detail"), BoundaryErrorCode.PROVIDER_TIMEOUT),
        (RuntimeError("synthetic response detail"), BoundaryErrorCode.PROVIDER_REJECTED),
        (_Response(None), BoundaryErrorCode.MALFORMED_RESPONSE),
        (_Response("x" * 129), BoundaryErrorCode.OUTPUT_LIMIT),
    ],
)
def test_openai_cloud_provider_maps_failures_without_response_residue(
    result: object,
    expected: BoundaryErrorCode,
) -> None:
    provider = OpenAICloudProvider(
        model="synthetic-model",
        client=_Client(_Responses(result=result)),
    )

    with pytest.raises(ProviderFailure) as raised:
        provider.complete(_request(), privacy=_privacy())

    assert raised.value.code is expected
    assert "synthetic" not in repr(raised.value)


def test_create_provider_maps_missing_optional_sdk_without_credential_residue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> object:
        raise ModuleNotFoundError("synthetic package detail")

    monkeypatch.setattr("importlib.import_module", missing)

    with pytest.raises(OptionalExtraUnavailable) as raised:
        create_provider("synthetic-credential", model="synthetic-model")

    assert "synthetic-credential" not in repr(raised.value)


def test_create_provider_constructs_sdk_lazily_with_bounded_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _Responses()
    observed: list[dict[str, object]] = []

    def constructor(**kwargs: object) -> _Client:
        observed.append(kwargs)
        return _Client(responses)

    monkeypatch.setattr(
        "importlib.import_module",
        lambda _name: SimpleNamespace(OpenAI=constructor),
    )

    provider = create_provider("synthetic-credential", model="synthetic-model")
    result = provider.complete(_request(), privacy=_privacy())

    assert result.provider_name == "cloud"
    assert observed == [{"api_key": "synthetic-credential", "max_retries": 0}]
    assert "synthetic-credential" not in repr(provider)
