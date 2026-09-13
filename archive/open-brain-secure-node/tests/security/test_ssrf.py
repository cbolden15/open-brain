from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from sys import float_info

import pytest
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.core.ports import FetchRequest

from open_brain_legacy.capture.egress import (
    EgressFailure,
    EgressFailureCode,
    OutboundFetcher,
    PinnedRequest,
    TransportResponse,
)


@dataclass
class _NeverResolver:
    calls: int = 0

    def resolve(self, hostname: str) -> tuple[str, ...]:
        self.calls += 1
        raise AssertionError(f"unexpected DNS lookup for {hostname}")


@dataclass
class _NeverTransport:
    calls: int = 0

    def request(self, request: PinnedRequest) -> TransportResponse:
        self.calls += 1
        raise AssertionError("unexpected transport request")


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="test-v1",
        authority=Authority(cloud=False, external_egress=True),
    )


def _request(
    url: str,
    *,
    max_bytes: int = 1024,
    timeout_seconds: float = 10,
    allowed_cookie_domains: tuple[str, ...] = (),
) -> FetchRequest:
    return FetchRequest(
        request_id="request-1",
        url=url,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        max_redirects=3,
        allowed_cookie_domains=allowed_cookie_domains,
    )


def _ipv4(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


@pytest.mark.parametrize(
    "url",
    (
        "file:///tmp/input.html",
        "data:text/plain,blocked",
        "ftp://files.example/entry",
        "//good.example/path",
        "https://good.example/line\nbreak",
        "https://",
        "https://user@good.example/path",
        "https://" + _ipv4(127, 0, 0, 1) + "/path",
        "https://[::1]/path",
        "https://good.example:8080/path",
    ),
)
def test_e01_invalid_urls_fail_before_resolver_or_transport(url: str) -> None:
    resolver = _NeverResolver()
    transport = _NeverTransport()
    fetcher = OutboundFetcher(resolver=resolver, transport=transport)

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(_request(url), privacy=_privacy())

    assert caught.value.code is EgressFailureCode.INVALID_INPUT
    assert resolver.calls == 0
    assert transport.calls == 0


@dataclass
class _FakeResolver:
    answers: Mapping[str, tuple[str, ...]]
    calls: list[str] = field(default_factory=list)

    def resolve(self, hostname: str) -> tuple[str, ...]:
        self.calls.append(hostname)
        return self.answers[hostname]


@dataclass
class _ScriptedResolver:
    answers: list[tuple[str, ...]]
    calls: list[str] = field(default_factory=list)

    def resolve(self, hostname: str) -> tuple[str, ...]:
        self.calls.append(hostname)
        return self.answers.pop(0)


@dataclass
class _FakeStream:
    chunks: list[bytes]
    read_sizes: list[int] = field(default_factory=list)
    closed: bool = False

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
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
class _FakeTransport:
    responses: list[TransportResponse]
    requests: list[PinnedRequest] = field(default_factory=list)

    def request(self, request: PinnedRequest) -> TransportResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _response(
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
    chunks: list[bytes] | None = None,
) -> tuple[TransportResponse, _FakeStream]:
    stream = _FakeStream([] if chunks is None else chunks)
    return (
        TransportResponse(
            status=status,
            headers={"content-type": "text/html"} if headers is None else headers,
            stream=stream,
        ),
        stream,
    )


@pytest.mark.parametrize(
    "address",
    (
        _ipv4(127, 0, 0, 1),
        _ipv4(10, 0, 0, 1),
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "240.0.0.1",
        "192.0.2.1",
        "100.64.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::",
        "2001:db8::1",
        "::ffff:" + _ipv4(127, 0, 0, 1),
        "fd00:ec2::254",
    ),
)
def test_e02_blocked_address_classes_are_denied_before_connect(address: str) -> None:
    resolver = _FakeResolver({"blocked.example": (address,)})
    transport = _FakeTransport([])
    fetcher = OutboundFetcher(resolver=resolver, transport=transport)

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(_request("https://blocked.example/page"), privacy=_privacy())

    assert caught.value.code is EgressFailureCode.EGRESS_DENIED
    assert resolver.calls == ["blocked.example"]
    assert transport.requests == []


@pytest.mark.parametrize(
    "answers",
    (
        (),
        ("192.0.2.1",),
        ("8.8.8.8", "192.0.2.1"),
    ),
)
def test_e03_no_answer_blocked_answer_or_mixed_answer_denies_the_request(
    answers: tuple[str, ...],
) -> None:
    resolver = _FakeResolver({"mixed.example": answers})
    transport = _FakeTransport([])
    fetcher = OutboundFetcher(resolver=resolver, transport=transport)

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(_request("https://mixed.example/page"), privacy=_privacy())

    assert caught.value.code is EgressFailureCode.EGRESS_DENIED
    assert resolver.calls == ["mixed.example"]
    assert transport.requests == []


def test_e04_connection_uses_one_validated_pinned_address_without_rebinding() -> None:
    response, _ = _response(chunks=[b"ok"])
    resolver = _ScriptedResolver([("8.8.8.8",), ("192.0.2.1",)])
    transport = _FakeTransport([response])
    fetcher = OutboundFetcher(resolver=resolver, transport=transport)

    result = fetcher.fetch(_request("https://good.example/page"), privacy=_privacy())

    assert result.body == b"ok"
    assert resolver.calls == ["good.example"]
    assert len(transport.requests) == 1
    assert transport.requests[0].pinned_address == "8.8.8.8"
    assert transport.requests[0].hostname == "good.example"


@pytest.mark.parametrize(
    ("responses", "answers", "expected_requests"),
    (
        (
            [(302, {"location": "https://private.example/next"})],
            [("8.8.8.8",), ("192.0.2.1",)],
            1,
        ),
        (
            [(302, {"location": "file:///tmp/blocked"})],
            [("8.8.8.8",)],
            1,
        ),
        (
            [(302, {})],
            [("8.8.8.8",)],
            1,
        ),
        (
            [(302, {"location": "https://good.example/page"})],
            [("8.8.8.8",)],
            1,
        ),
        (
            [
                (302, {"location": "/one"}),
                (302, {"location": "/two"}),
                (302, {"location": "/three"}),
                (302, {"location": "/four"}),
            ],
            [("8.8.8.8",)] * 4,
            4,
        ),
    ),
)
def test_e05_unsafe_or_unbounded_redirects_fail_before_target_connect(
    responses: list[tuple[int, Mapping[str, str]]],
    answers: list[tuple[str, ...]],
    expected_requests: int,
) -> None:
    built = [_response(status=status, headers=headers) for status, headers in responses]
    resolver = _ScriptedResolver(answers)
    transport = _FakeTransport([response for response, _ in built])
    fetcher = OutboundFetcher(resolver=resolver, transport=transport)

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(_request("https://good.example/page"), privacy=_privacy())

    assert caught.value.code is EgressFailureCode.EGRESS_DENIED
    assert len(transport.requests) == expected_requests
    assert all(stream.closed for _, stream in built)


@pytest.mark.parametrize(
    ("hostname", "expected_cookie"),
    (
        ("good.example", "session=synthetic"),
        ("child.good.example", "session=synthetic"),
        ("notgood.example", None),
    ),
)
def test_e06_cookie_domain_matching_requires_an_exact_or_dot_boundary_host(
    hostname: str, expected_cookie: str | None
) -> None:
    response, _ = _response(chunks=[b"ok"])
    resolver = _FakeResolver({hostname: ("8.8.8.8",)})
    transport = _FakeTransport([response])
    fetcher = OutboundFetcher(
        resolver=resolver,
        transport=transport,
        cookies={"good.example": "session=synthetic"},
    )

    fetcher.fetch(
        _request(
            f"https://{hostname}/page",
            allowed_cookie_domains=("good.example",),
        ),
        privacy=_privacy(),
    )

    assert transport.requests[0].headers.get("cookie") == expected_cookie


def test_e06_redirect_discards_cookie_outside_the_configured_domain() -> None:
    first, _ = _response(status=302, headers={"location": "https://other.example/page"})
    second, _ = _response(chunks=[b"ok"])
    resolver = _ScriptedResolver([("8.8.8.8",), ("8.8.8.8",)])
    transport = _FakeTransport([first, second])
    fetcher = OutboundFetcher(
        resolver=resolver,
        transport=transport,
        cookies={"good.example": "session=synthetic"},
    )

    fetcher.fetch(
        _request(
            "https://good.example/page",
            allowed_cookie_domains=("good.example",),
        ),
        privacy=_privacy(),
    )

    assert transport.requests[0].headers.get("cookie") == "session=synthetic"
    assert "cookie" not in transport.requests[1].headers


@pytest.mark.parametrize(
    ("headers", "chunks", "expected_code"),
    (
        ({"content-type": "text/html"}, [b"abc", b"def"], EgressFailureCode.BODY_LIMIT),
        (
            {"content-type": "application/octet-stream"},
            [b"ignored"],
            EgressFailureCode.FETCH_FAILED,
        ),
    ),
)
def test_e07_response_media_and_stream_bounds_close_the_stream(
    headers: Mapping[str, str],
    chunks: list[bytes],
    expected_code: EgressFailureCode,
) -> None:
    response, stream = _response(headers=headers, chunks=chunks)
    resolver = _FakeResolver({"good.example": ("8.8.8.8",)})
    transport = _FakeTransport([response])
    fetcher = OutboundFetcher(resolver=resolver, transport=transport)

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(_request("https://good.example/page", max_bytes=5), privacy=_privacy())

    assert caught.value.code is expected_code
    assert stream.closed
    if expected_code is EgressFailureCode.BODY_LIMIT:
        assert max(stream.read_sizes) <= 6


@pytest.mark.parametrize("timeout_seconds", (float("nan"), float("inf"), float("-inf")))
def test_e07_nonfinite_request_timeout_fails_before_resolver_or_transport(
    timeout_seconds: float,
) -> None:
    resolver = _NeverResolver()
    transport = _NeverTransport()
    fetcher = OutboundFetcher(resolver=resolver, transport=transport)

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(
            _request("https://good.example/page", timeout_seconds=timeout_seconds),
            privacy=_privacy(),
        )

    assert caught.value.code is EgressFailureCode.INVALID_INPUT
    assert resolver.calls == 0
    assert transport.calls == 0


@pytest.mark.parametrize("timeout_seconds", (float("nan"), float("inf"), float("-inf")))
def test_nonfinite_configured_timeout_is_rejected(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="invalid egress configuration"):
        OutboundFetcher(
            resolver=_NeverResolver(),
            transport=_NeverTransport(),
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize("clock_value", (float("nan"), float("inf"), float("-inf")))
def test_e07_nonfinite_initial_clock_fails_before_resolver_or_transport(
    clock_value: float,
) -> None:
    resolver = _NeverResolver()
    transport = _NeverTransport()
    fetcher = OutboundFetcher(
        resolver=resolver,
        transport=transport,
        clock=lambda: clock_value,
    )

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(_request("https://good.example/page"), privacy=_privacy())

    assert caught.value.code is EgressFailureCode.FETCH_FAILED
    assert resolver.calls == 0
    assert transport.calls == 0


@pytest.mark.parametrize("clock_value", (float("nan"), float("inf"), float("-inf")))
def test_e07_nonfinite_remaining_clock_never_reaches_transport(clock_value: float) -> None:
    clock_values = iter((1.0, clock_value))
    resolver = _FakeResolver({"good.example": ("8.8.8.8",)})
    transport = _FakeTransport([])
    fetcher = OutboundFetcher(
        resolver=resolver,
        transport=transport,
        clock=lambda: next(clock_values),
    )

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(_request("https://good.example/page"), privacy=_privacy())

    assert caught.value.code is EgressFailureCode.FETCH_FAILED
    assert resolver.calls == ["good.example"]
    assert transport.requests == []


def test_e07_finite_values_that_overflow_deadline_fail_before_resolver() -> None:
    resolver = _NeverResolver()
    transport = _NeverTransport()
    fetcher = OutboundFetcher(
        resolver=resolver,
        transport=transport,
        timeout_seconds=float_info.max,
        clock=lambda: float_info.max,
    )

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(
            _request("https://good.example/page", timeout_seconds=float_info.max),
            privacy=_privacy(),
        )

    assert caught.value.code is EgressFailureCode.FETCH_FAILED
    assert resolver.calls == 0
    assert transport.calls == 0


def test_e07_finite_clock_values_that_overflow_remaining_never_reach_transport() -> None:
    clock_values = iter((float_info.max, -float_info.max))
    resolver = _FakeResolver({"good.example": ("8.8.8.8",)})
    transport = _FakeTransport([])
    fetcher = OutboundFetcher(
        resolver=resolver,
        transport=transport,
        clock=lambda: next(clock_values),
    )

    with pytest.raises(EgressFailure) as caught:
        fetcher.fetch(_request("https://good.example/page"), privacy=_privacy())

    assert caught.value.code is EgressFailureCode.FETCH_FAILED
    assert resolver.calls == ["good.example"]
    assert transport.requests == []
