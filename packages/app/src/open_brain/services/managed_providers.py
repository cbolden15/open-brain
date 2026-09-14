"""Bounded model-access adapters for managed graph inference."""

from __future__ import annotations

import http.client
import json
import re
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Protocol, cast

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import ManagedProvider

MAX_PROVIDER_INPUT_BYTES = 16 * 1024
MAX_PROVIDER_OUTPUT_BYTES = 16 * 1024
MAX_PROVIDER_TIMEOUT_SECONDS = 60
MAX_USAGE_TOKENS = (1 << 53) - 1
CLAUDE_CLIENT_VERSION = "2.1.265"

DEFAULT_MODELS = MappingProxyType(
    {
        ManagedProvider.OPENAI_API: "gpt-6-astra",
        ManagedProvider.ANTHROPIC_API: "claude-sonnet-4-6",
        ManagedProvider.CLAUDE_SUBSCRIPTION: "claude-sonnet-4-6",
        ManagedProvider.GEMINI_API: "gemini-2.5-flash",
    }
)

_MODEL = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_FAILURE_CODES = {
    "authentication_failed",
    "cancelled",
    "cleanup_failed",
    "content_rejected",
    "credential_unavailable",
    "output_invalid",
    "policy_unavailable",
    "provider_rejected",
    "provider_unavailable",
    "quota_exceeded",
    "setup_required",
    "timeout",
    "transport_failed",
}


class ManagedProviderFailure(RuntimeError):
    """A provider failure with one bounded public category."""

    def __init__(self, code: str) -> None:
        if code not in _FAILURE_CODES:
            raise ValueError("invalid managed provider failure")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProviderHttpRequest:
    host: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class ManagedGraphProviderResult:
    source: int
    source_quote: str
    target: int
    target_quote: str
    actual_model: str
    usage: Mapping[str, int]

    def __post_init__(self) -> None:
        if (
            type(self.source) is not int
            or type(self.target) is not int
            or self.source < 1
            or self.target < 1
            or self.source == self.target
            or not _valid_text(self.source_quote, maximum=4096)
            or not _valid_text(self.target_quote, maximum=4096)
            or not _valid_model(self.actual_model)
            or not isinstance(self.usage, Mapping)
        ):
            raise ValueError("invalid managed provider result")
        normalized: dict[str, int] = {}
        for key, value in self.usage.items():
            if (
                not isinstance(key, str)
                or not key
                or type(value) is not int
                or not 0 <= value <= MAX_USAGE_TOKENS
            ):
                raise ValueError("invalid managed provider usage")
            normalized[key] = value
        object.__setattr__(self, "usage", MappingProxyType(normalized))

    def suggestion(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_quote": self.source_quote,
            "target": self.target,
            "target_quote": self.target_quote,
        }


class ProviderTransport(Protocol):
    def __call__(
        self,
        request: ProviderHttpRequest,
        *,
        timeout_seconds: int,
        max_output_bytes: int,
        cancelled: Callable[[], bool],
    ) -> ProviderHttpResponse: ...


class StdlibHttpsTransport:
    """One fixed-host HTTPS exchange without proxy or ambient credential discovery."""

    def __call__(
        self,
        request: ProviderHttpRequest,
        *,
        timeout_seconds: int,
        max_output_bytes: int,
        cancelled: Callable[[], bool],
    ) -> ProviderHttpResponse:
        if cancelled():
            raise ManagedProviderFailure("cancelled")
        connection = http.client.HTTPSConnection(
            request.host,
            port=443,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            connection.request(
                "POST",
                request.path,
                body=request.body,
                headers=dict(request.headers),
            )
            response = connection.getresponse()
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    declared_bytes = int(declared)
                except ValueError:
                    raise ManagedProviderFailure("output_invalid") from None
                if not 0 <= declared_bytes <= max_output_bytes:
                    raise ManagedProviderFailure("output_invalid")
            body = response.read(max_output_bytes + 1)
            if len(body) > max_output_bytes:
                raise ManagedProviderFailure("output_invalid")
            if declared is not None and declared_bytes != len(body):
                raise ManagedProviderFailure("output_invalid")
            if cancelled():
                raise ManagedProviderFailure("cancelled")
            return ProviderHttpResponse(response.status, tuple(response.getheaders()), body)
        except TimeoutError:
            raise ManagedProviderFailure("timeout") from None
        except ManagedProviderFailure:
            raise
        except OSError:
            raise ManagedProviderFailure("transport_failed") from None
        finally:
            connection.close()


class DirectApiAdapter:
    """OpenAI, Anthropic, or Gemini API access with an explicit key source."""

    def __init__(
        self,
        provider: ManagedProvider,
        *,
        model: str,
        transport: ProviderTransport | None = None,
    ) -> None:
        if provider not in {
            ManagedProvider.OPENAI_API,
            ManagedProvider.ANTHROPIC_API,
            ManagedProvider.GEMINI_API,
        } or not _valid_model(model):
            raise ValueError("invalid direct provider adapter")
        self.provider = provider
        self.model = model
        self._transport = StdlibHttpsTransport() if transport is None else transport
        if not callable(self._transport):
            raise ValueError("invalid direct provider transport")

    @property
    def identity(self) -> str:
        public = {
            "host": _host(self.provider),
            "model": self.model,
            "provider": self.provider.value,
            "transport": "stdlib-https-v1",
        }
        digest = sha256(portable_canonical_json_bytes(public)).hexdigest()
        return f"{self.provider.value}:direct-v1:{digest}"

    def bind(
        self,
        resolve_credential: Callable[[], str | None],
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Callable[[str, int, int], ManagedGraphProviderResult]:
        if not callable(resolve_credential) or not callable(cancelled):
            raise ValueError("invalid direct provider binding")

        def invoke(
            prompt: str, max_output_bytes: int, timeout_seconds: int
        ) -> ManagedGraphProviderResult:
            _validate_call(prompt, max_output_bytes, timeout_seconds)
            if has_redaction_finding(prompt):
                raise ManagedProviderFailure("content_rejected")
            if cancelled():
                raise ManagedProviderFailure("cancelled")
            try:
                credential = resolve_credential()
            except Exception:
                raise ManagedProviderFailure("credential_unavailable") from None
            if not _valid_credential(credential):
                raise ManagedProviderFailure("credential_unavailable")
            request = _build_request(self.provider, self.model, prompt, cast(str, credential))
            try:
                response = self._transport(
                    request,
                    timeout_seconds=timeout_seconds,
                    max_output_bytes=max_output_bytes,
                    cancelled=cancelled,
                )
            except ManagedProviderFailure:
                raise
            except TimeoutError:
                raise ManagedProviderFailure("timeout") from None
            except Exception:
                raise ManagedProviderFailure("transport_failed") from None
            if cancelled():
                raise ManagedProviderFailure("cancelled")
            return _parse_response(self.provider, self.model, response, max_output_bytes)

        return invoke


@dataclass(frozen=True, slots=True)
class ClaudePolicyEvidence:
    authenticated: bool
    access_mode: str
    provider: str
    client_version: str
    model: str
    tool_names: tuple[str, ...]
    mcp_server_names: tuple[str, ...]
    ambient_context_bytes: int
    retention: str
    policy_complete: bool


@dataclass(frozen=True, slots=True)
class ClaudeCompletion:
    text: str
    actual_model: str
    usage: Mapping[str, int]


class ClaudeSupervisor(Protocol):
    def policy_evidence(self, *, model: str) -> ClaudePolicyEvidence: ...

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_output_bytes: int,
        timeout_seconds: int,
        cancelled: Callable[[], bool],
    ) -> ClaudeCompletion: ...


class ClaudeSubscriptionAdapter:
    """Client-owned Claude authentication behind complete pre-dispatch evidence."""

    def __init__(self, *, model: str = DEFAULT_MODELS[ManagedProvider.CLAUDE_SUBSCRIPTION]) -> None:
        if not _valid_model(model):
            raise ValueError("invalid Claude subscription model")
        self.provider = ManagedProvider.CLAUDE_SUBSCRIPTION
        self.model = model

    @property
    def identity(self) -> str:
        value = {
            "client_version": CLAUDE_CLIENT_VERSION,
            "model": self.model,
            "provider": self.provider.value,
            "transport": "official-client-supervisor-v1",
        }
        digest = sha256(portable_canonical_json_bytes(value)).hexdigest()
        return f"{self.provider.value}:supervisor-v1:{digest}"

    def bind(
        self,
        supervisor: ClaudeSupervisor,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Callable[[str, int, int], ManagedGraphProviderResult]:
        if not callable(getattr(supervisor, "policy_evidence", None)) or not callable(
            getattr(supervisor, "complete", None)
        ):
            raise ValueError("invalid Claude supervisor")

        def invoke(
            prompt: str, max_output_bytes: int, timeout_seconds: int
        ) -> ManagedGraphProviderResult:
            _validate_call(prompt, max_output_bytes, timeout_seconds)
            if has_redaction_finding(prompt):
                raise ManagedProviderFailure("content_rejected")
            if cancelled():
                raise ManagedProviderFailure("cancelled")
            try:
                evidence = supervisor.policy_evidence(model=self.model)
            except Exception:
                raise ManagedProviderFailure("policy_unavailable") from None
            if not isinstance(evidence, ClaudePolicyEvidence):
                raise ManagedProviderFailure("policy_unavailable")
            if not _valid_claude_evidence(evidence, self.model):
                raise ManagedProviderFailure(
                    "setup_required" if not evidence.authenticated else "policy_unavailable"
                )
            try:
                completion = supervisor.complete(
                    prompt,
                    model=self.model,
                    max_output_bytes=max_output_bytes,
                    timeout_seconds=timeout_seconds,
                    cancelled=cancelled,
                )
            except ManagedProviderFailure:
                raise
            except TimeoutError:
                raise ManagedProviderFailure("timeout") from None
            except Exception:
                raise ManagedProviderFailure("transport_failed") from None
            if not isinstance(completion, ClaudeCompletion):
                raise ManagedProviderFailure("output_invalid")
            if (
                completion.actual_model != self.model
                or not isinstance(completion.text, str)
                or len(completion.text.encode("utf-8")) > max_output_bytes
            ):
                raise ManagedProviderFailure("output_invalid")
            suggestion = _suggestion(_decode_json(completion.text), max_output_bytes)
            return ManagedGraphProviderResult(
                source=cast(int, suggestion["source"]),
                source_quote=cast(str, suggestion["source_quote"]),
                target=cast(int, suggestion["target"]),
                target_quote=cast(str, suggestion["target_quote"]),
                actual_model=completion.actual_model,
                usage=completion.usage,
            )

        return invoke


def _validate_call(prompt: str, max_output_bytes: int, timeout_seconds: int) -> None:
    if (
        not isinstance(prompt, str)
        or not prompt
        or len(prompt.encode("utf-8")) > MAX_PROVIDER_INPUT_BYTES
        or type(max_output_bytes) is not int
        or not 0 < max_output_bytes <= MAX_PROVIDER_OUTPUT_BYTES
        or type(timeout_seconds) is not int
        or not 0 < timeout_seconds <= MAX_PROVIDER_TIMEOUT_SECONDS
    ):
        raise ManagedProviderFailure("output_invalid")


def _valid_model(value: object) -> bool:
    return isinstance(value, str) and _MODEL.fullmatch(value) is not None


def _valid_text(value: object, *, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= maximum


def _valid_credential(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.encode("utf-8")) <= 4096
        and "\r" not in value
        and "\n" not in value
    )


def _host(provider: ManagedProvider) -> str:
    return {
        ManagedProvider.OPENAI_API: "api.openai.com",
        ManagedProvider.ANTHROPIC_API: "api.anthropic.com",
        ManagedProvider.GEMINI_API: "generativelanguage.googleapis.com",
    }[provider]


def _path(provider: ManagedProvider, model: str) -> str:
    if provider is ManagedProvider.OPENAI_API:
        return "/v1/responses"
    if provider is ManagedProvider.ANTHROPIC_API:
        return "/v1/messages"
    return f"/v1beta/models/{model}:generateContent"


def _headers(provider: ManagedProvider, credential: str) -> tuple[tuple[str, str], ...]:
    values = [
        ("Accept", "application/json"),
        ("Content-Type", "application/json"),
        ("User-Agent", "open-brain/0.1"),
    ]
    if provider is ManagedProvider.OPENAI_API:
        values.append(("Authorization", f"Bearer {credential}"))
    elif provider is ManagedProvider.ANTHROPIC_API:
        values.extend((("x-api-key", credential), ("anthropic-version", "2023-06-01")))
    else:
        values.append(("x-goog-api-key", credential))
    return tuple(values)


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["source", "source_quote", "target", "target_quote"],
        "properties": {
            "source": {"type": "integer", "minimum": 1, "maximum": 64},
            "source_quote": {"type": "string", "minLength": 1, "maxLength": 4096},
            "target": {"type": "integer", "minimum": 1, "maximum": 64},
            "target_quote": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
    }


def _build_request(
    provider: ManagedProvider, model: str, prompt: str, credential: str
) -> ProviderHttpRequest:
    schema = _schema()
    if provider is ManagedProvider.OPENAI_API:
        value: dict[str, object] = {
            "model": model,
            "input": prompt,
            "store": False,
            "max_output_tokens": 1024,
            "tools": [],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "managed_graph_suggestion",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
    elif provider is ManagedProvider.ANTHROPIC_API:
        value = {
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
    else:
        value = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "candidateCount": 1,
                "maxOutputTokens": 1024,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
    body = portable_canonical_json_bytes(value)
    if len(body) > MAX_PROVIDER_INPUT_BYTES:
        raise ManagedProviderFailure("output_invalid")
    return ProviderHttpRequest(
        _host(provider),
        _path(provider, model),
        _headers(provider, credential),
        body,
    )


def _parse_response(
    provider: ManagedProvider,
    model: str,
    response: ProviderHttpResponse,
    max_output_bytes: int,
) -> ManagedGraphProviderResult:
    if (
        type(response.status) is not int
        or not isinstance(response.headers, tuple)
        or not isinstance(response.body, bytes)
        or len(response.body) > max_output_bytes
    ):
        raise ManagedProviderFailure("output_invalid")
    if response.status != 200:
        raise ManagedProviderFailure(_http_failure(response.status))
    headers: dict[str, str] = {}
    for key, header_value in response.headers:
        normalized = key.casefold()
        if normalized in headers:
            raise ManagedProviderFailure("output_invalid")
        headers[normalized] = header_value
    if headers.get("content-type", "").split(";", 1)[0].strip().casefold() != "application/json":
        raise ManagedProviderFailure("output_invalid")
    if (
        headers.get("content-encoding", "identity").casefold() != "identity"
        or "set-cookie" in headers
    ):
        raise ManagedProviderFailure("output_invalid")
    payload = _decode_json(response.body)
    if not isinstance(payload, dict):
        raise ManagedProviderFailure("output_invalid")
    actual_model = payload.get(
        "modelVersion" if provider is ManagedProvider.GEMINI_API else "model"
    )
    if actual_model != model:
        raise ManagedProviderFailure("output_invalid")
    text, usage = _provider_payload(provider, payload)
    suggestion = _suggestion(_decode_json(text), max_output_bytes)
    return ManagedGraphProviderResult(
        source=cast(int, suggestion["source"]),
        source_quote=cast(str, suggestion["source_quote"]),
        target=cast(int, suggestion["target"]),
        target_quote=cast(str, suggestion["target_quote"]),
        actual_model=cast(str, actual_model),
        usage=usage,
    )


def _http_failure(status: int) -> str:
    if status in {401, 403}:
        return "authentication_failed"
    if status in {408, 504}:
        return "timeout"
    if status == 429:
        return "quota_exceeded"
    if 500 <= status <= 599:
        return "provider_unavailable"
    return "provider_rejected"


def _provider_payload(
    provider: ManagedProvider, value: Mapping[str, object]
) -> tuple[str, Mapping[str, int]]:
    if provider is ManagedProvider.OPENAI_API:
        if (
            value.get("object") != "response"
            or value.get("status") != "completed"
            or value.get("error") is not None
            or value.get("incomplete_details") is not None
        ):
            raise ManagedProviderFailure("output_invalid")
        parts = _one_message_part(value.get("output"), "message", "assistant", "output_text")
        usage = _usage(value.get("usage"), "input_tokens", "output_tokens", "total_tokens")
    elif provider is ManagedProvider.ANTHROPIC_API:
        if (
            value.get("type") != "message"
            or value.get("role") != "assistant"
            or value.get("stop_reason") != "end_turn"
            or value.get("stop_details") not in (None, {})
        ):
            raise ManagedProviderFailure("output_invalid")
        parts = _one_content_part(value.get("content"), part_type="text")
        usage = _usage(value.get("usage"), "input_tokens", "output_tokens", None)
    else:
        feedback = value.get("promptFeedback", {})
        candidates = value.get("candidates")
        if (
            not isinstance(feedback, dict)
            or feedback.get("blockReason")
            or not isinstance(candidates, list)
            or len(candidates) != 1
            or not isinstance(candidates[0], dict)
            or candidates[0].get("finishReason") != "STOP"
        ):
            raise ManagedProviderFailure("output_invalid")
        content = candidates[0].get("content")
        if not isinstance(content, dict) or content.get("role") != "model":
            raise ManagedProviderFailure("output_invalid")
        parts = _one_content_part(content.get("parts"), part_type=None)
        usage = _usage(
            value.get("usageMetadata"),
            "promptTokenCount",
            "candidatesTokenCount",
            "totalTokenCount",
        )
    text = parts.get("text")
    if not isinstance(text, str):
        raise ManagedProviderFailure("output_invalid")
    return text, usage


def _one_message_part(
    raw: object, item_type: str, role: str, part_type: str
) -> Mapping[str, object]:
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise ManagedProviderFailure("output_invalid")
    item = raw[0]
    if (
        item.get("type") != item_type
        or item.get("role") != role
        or item.get("status") != "completed"
    ):
        raise ManagedProviderFailure("output_invalid")
    return _one_content_part(item.get("content"), part_type=part_type)


def _one_content_part(raw: object, *, part_type: str | None) -> Mapping[str, object]:
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise ManagedProviderFailure("output_invalid")
    part = raw[0]
    if part_type is not None and part.get("type") != part_type:
        raise ManagedProviderFailure("output_invalid")
    if part_type is None and set(part) != {"text"}:
        raise ManagedProviderFailure("output_invalid")
    return part


def _usage(
    raw: object, input_key: str, output_key: str, total_key: str | None
) -> Mapping[str, int]:
    if not isinstance(raw, dict):
        raise ManagedProviderFailure("output_invalid")
    input_tokens = _usage_int(raw.get(input_key))
    output_tokens = _usage_int(raw.get(output_key))
    total_tokens = (
        input_tokens + output_tokens
        if total_key is None
        else _usage_int(raw.get(total_key))
    )
    if total_tokens < input_tokens + output_tokens:
        raise ManagedProviderFailure("output_invalid")
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    reasoning = raw.get("thoughtsTokenCount")
    if reasoning is not None:
        result["reasoning_tokens"] = _usage_int(reasoning)
    return result


def _usage_int(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_USAGE_TOKENS:
        raise ManagedProviderFailure("output_invalid")
    return value


def _decode_json(raw: bytes | str) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ManagedProviderFailure("output_invalid")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ManagedProviderFailure("output_invalid")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except ManagedProviderFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, RecursionError):
        raise ManagedProviderFailure("output_invalid") from None


def _suggestion(value: object, maximum: int) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "source",
        "source_quote",
        "target",
        "target_quote",
    }:
        raise ManagedProviderFailure("output_invalid")
    if len(portable_canonical_json_bytes(value)) > maximum:
        raise ManagedProviderFailure("output_invalid")
    source = value["source"]
    target = value["target"]
    if (
        type(source) is not int
        or type(target) is not int
        or not 1 <= source <= 64
        or not 1 <= target <= 64
        or source == target
        or not _valid_text(value["source_quote"], maximum=4096)
        or not _valid_text(value["target_quote"], maximum=4096)
    ):
        raise ManagedProviderFailure("output_invalid")
    return cast(dict[str, object], value)


def _valid_claude_evidence(evidence: object, model: str) -> bool:
    return (
        isinstance(evidence, ClaudePolicyEvidence)
        and evidence.authenticated
        and evidence.access_mode == "subscription"
        and evidence.provider == "anthropic"
        and evidence.client_version == CLAUDE_CLIENT_VERSION
        and evidence.model == model
        and evidence.tool_names == ()
        and evidence.mcp_server_names == ()
        and evidence.ambient_context_bytes == 0
        and evidence.retention == "ephemeral"
        and evidence.policy_complete
    )


__all__ = [
    "CLAUDE_CLIENT_VERSION",
    "DEFAULT_MODELS",
    "ClaudeCompletion",
    "ClaudePolicyEvidence",
    "ClaudeSubscriptionAdapter",
    "DirectApiAdapter",
    "ManagedGraphProviderResult",
    "ManagedProviderFailure",
    "ProviderHttpRequest",
    "ProviderHttpResponse",
    "StdlibHttpsTransport",
]
