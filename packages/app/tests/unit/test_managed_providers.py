from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from open_brain_engine.engine import ManagedProvider

from open_brain.services.managed_providers import (
    CLAUDE_CLIENT_VERSION,
    ClaudeCompletion,
    ClaudePolicyEvidence,
    ClaudeSubscriptionAdapter,
    DirectApiAdapter,
    ManagedGraphProviderResult,
    ManagedProviderFailure,
    ProviderHttpRequest,
    ProviderHttpResponse,
)

PROMPT = "Connect the first selected source to the second selected source."
SUGGESTION = {
    "source": 1,
    "source_quote": "first selected source",
    "target": 2,
    "target_quote": "second selected source",
}


@dataclass
class RecordingTransport:
    response: ProviderHttpResponse

    def __post_init__(self) -> None:
        self.requests: list[ProviderHttpRequest] = []

    def __call__(
        self,
        request: ProviderHttpRequest,
        *,
        timeout_seconds: int,
        max_output_bytes: int,
        cancelled: Callable[[], bool],
    ) -> ProviderHttpResponse:
        assert timeout_seconds == 60
        assert max_output_bytes == 16 * 1024
        assert cancelled() is False
        self.requests.append(request)
        return self.response


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _response(provider: ManagedProvider, model: str) -> ProviderHttpResponse:
    text = json.dumps(SUGGESTION, sort_keys=True, separators=(",", ":"))
    body: dict[str, object]
    if provider is ManagedProvider.OPENAI_API:
        body = {
            "object": "response",
            "model": model,
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
        }
    elif provider is ManagedProvider.ANTHROPIC_API:
        body = {
            "type": "message",
            "model": model,
            "role": "assistant",
            "stop_reason": "end_turn",
            "stop_details": None,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 12, "output_tokens": 7},
        }
    else:
        body = {
            "modelVersion": model,
            "promptFeedback": {},
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"role": "model", "parts": [{"text": text}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 7,
                "thoughtsTokenCount": 3,
                "totalTokenCount": 22,
            },
        }
    return ProviderHttpResponse(
        200,
        (("Content-Type", "application/json; charset=utf-8"),),
        _json_bytes(body),
    )


@pytest.mark.parametrize(
    ("provider", "model", "host", "path", "credential_header"),
    (
        (
            ManagedProvider.OPENAI_API,
            "gpt-6-astra",
            "api.openai.com",
            "/v1/responses",
            ("Authorization", "Bearer provider-secret"),
        ),
        (
            ManagedProvider.ANTHROPIC_API,
            "claude-sonnet-4-6",
            "api.anthropic.com",
            "/v1/messages",
            ("x-api-key", "provider-secret"),
        ),
        (
            ManagedProvider.GEMINI_API,
            "gemini-2.5-flash",
            "generativelanguage.googleapis.com",
            "/v1beta/models/gemini-2.5-flash:generateContent",
            ("x-goog-api-key", "provider-secret"),
        ),
    ),
)
def test_direct_adapters_bind_provider_specific_request_and_preserve_attribution(
    provider: ManagedProvider,
    model: str,
    host: str,
    path: str,
    credential_header: tuple[str, str],
) -> None:
    transport = RecordingTransport(_response(provider, model))
    invoke = DirectApiAdapter(provider, model=model, transport=transport).bind(
        lambda: "provider-secret"
    )

    result = invoke(PROMPT, 16 * 1024, 60)

    assert result == ManagedGraphProviderResult(
        source=1,
        source_quote="first selected source",
        target=2,
        target_quote="second selected source",
        actual_model=model,
        usage={
            "input_tokens": 12,
            "output_tokens": 7,
            "total_tokens": 22 if provider is ManagedProvider.GEMINI_API else 19,
            **(
                {"reasoning_tokens": 3}
                if provider is ManagedProvider.GEMINI_API
                else {}
            ),
        },
    )
    request = transport.requests[0]
    assert request.host == host
    assert request.path == path
    assert credential_header in request.headers
    assert len(
        {
            "authorization",
            "x-api-key",
            "x-goog-api-key",
        }
        & {key.casefold() for key, _value in request.headers}
    ) == 1
    payload = json.loads(request.body)
    assert "tools" not in payload or payload["tools"] == []
    if provider is ManagedProvider.OPENAI_API:
        assert payload["store"] is False


def test_direct_adapter_rejects_sensitive_prompt_before_credential_or_transport() -> None:
    calls: list[str] = []
    transport = RecordingTransport(_response(ManagedProvider.OPENAI_API, "gpt-6-astra"))

    def credential() -> str:
        calls.append("credential")
        return "provider-secret"

    invoke = DirectApiAdapter(
        ManagedProvider.OPENAI_API,
        model="gpt-6-astra",
        transport=transport,
    ).bind(credential)

    with pytest.raises(ManagedProviderFailure, match="content_rejected") as caught:
        invoke("api_key=" + "A" * 32, 16 * 1024, 60)

    assert caught.value.code == "content_rejected"
    assert calls == []
    assert transport.requests == []


@pytest.mark.parametrize(
    "mutation",
    (
        lambda response: ProviderHttpResponse(
            response.status, response.headers, response.body + b"{}"
        ),
        lambda response: ProviderHttpResponse(
            response.status,
            response.headers,
            response.body.replace(b'"gpt-6-astra"', b'"wrong-model"'),
        ),
        lambda response: ProviderHttpResponse(
            response.status, (("Content-Type", "text/plain"),), response.body
        ),
    ),
)
def test_direct_adapter_rejects_ambiguous_or_unattributed_output(
    mutation: Callable[[ProviderHttpResponse], ProviderHttpResponse],
) -> None:
    response = mutation(_response(ManagedProvider.OPENAI_API, "gpt-6-astra"))
    invoke = DirectApiAdapter(
        ManagedProvider.OPENAI_API,
        model="gpt-6-astra",
        transport=RecordingTransport(response),
    ).bind(lambda: "provider-secret")

    with pytest.raises(ManagedProviderFailure, match="output_invalid"):
        invoke(PROMPT, 16 * 1024, 60)


@pytest.mark.parametrize(
    ("status", "code"),
    (
        (401, "authentication_failed"),
        (429, "quota_exceeded"),
        (503, "provider_unavailable"),
        (504, "timeout"),
    ),
)
def test_direct_adapter_maps_http_failures_without_reading_provider_error_details(
    status: int, code: str
) -> None:
    response = ProviderHttpResponse(
        status,
        (("Content-Type", "application/json"),),
        _json_bytes({"sensitive_provider_detail": "must remain private"}),
    )
    invoke = DirectApiAdapter(
        ManagedProvider.OPENAI_API,
        model="gpt-6-astra",
        transport=RecordingTransport(response),
    ).bind(lambda: "provider-secret")

    with pytest.raises(ManagedProviderFailure, match=code) as caught:
        invoke(PROMPT, 16 * 1024, 60)

    assert caught.value.code == code


class FakeClaudeSupervisor:
    def __init__(
        self,
        evidence: object,
        completion: object,
    ) -> None:
        self.evidence = evidence
        self.completion = completion
        self.completion_calls = 0

    def policy_evidence(self, *, model: str) -> object:
        assert model == "claude-sonnet-4-6"
        return self.evidence

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_output_bytes: int,
        timeout_seconds: int,
        cancelled: Callable[[], bool],
    ) -> object:
        assert prompt == PROMPT
        assert model == "claude-sonnet-4-6"
        assert max_output_bytes == 16 * 1024
        assert timeout_seconds == 60
        assert cancelled() is False
        self.completion_calls += 1
        return self.completion


def _claude_evidence(**updates: object) -> ClaudePolicyEvidence:
    values: dict[str, object] = {
        "authenticated": True,
        "access_mode": "subscription",
        "provider": "anthropic",
        "client_version": CLAUDE_CLIENT_VERSION,
        "model": "claude-sonnet-4-6",
        "tool_names": (),
        "mcp_server_names": (),
        "ambient_context_bytes": 0,
        "retention": "ephemeral",
        "policy_complete": True,
    }
    values.update(updates)
    return ClaudePolicyEvidence(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "evidence",
    (
        object(),
        _claude_evidence(authenticated=False),
        _claude_evidence(tool_names=("Read",)),
        _claude_evidence(mcp_server_names=("workspace",)),
        _claude_evidence(ambient_context_bytes=1),
        _claude_evidence(retention="unknown"),
        _claude_evidence(policy_complete=False),
    ),
)
def test_claude_subscription_fails_closed_before_dispatch_without_complete_policy(
    evidence: object,
) -> None:
    supervisor = FakeClaudeSupervisor(evidence, object())
    invoke = ClaudeSubscriptionAdapter().bind(supervisor)  # type: ignore[arg-type]

    with pytest.raises(ManagedProviderFailure) as caught:
        invoke(PROMPT, 16 * 1024, 60)

    assert caught.value.code in {"policy_unavailable", "setup_required"}
    assert supervisor.completion_calls == 0


def test_claude_subscription_accepts_only_attributed_supervised_completion() -> None:
    completion = ClaudeCompletion(
        text=json.dumps(SUGGESTION),
        actual_model="claude-sonnet-4-6",
        usage={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
    )
    supervisor = FakeClaudeSupervisor(_claude_evidence(), completion)
    invoke = ClaudeSubscriptionAdapter().bind(supervisor)  # type: ignore[arg-type]

    result = invoke(PROMPT, 16 * 1024, 60)

    assert result.actual_model == "claude-sonnet-4-6"
    assert result.usage == {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12}
    assert result.suggestion() == SUGGESTION
    assert supervisor.completion_calls == 1
