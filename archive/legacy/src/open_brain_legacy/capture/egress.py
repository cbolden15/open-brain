from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from time import monotonic
from typing import NoReturn, Protocol
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.core.ports import FetchRequest, FetchResponse

_DEFAULT_MAX_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_REDIRECTS = 3
_DEFAULT_TIMEOUT_SECONDS = 10.0
_READ_CHUNK_BYTES = 64 * 1024
_ALLOWED_PORTS = frozenset({80, 443})
_DEFAULT_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html", "text/plain"})
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_METADATA_NETWORKS = (
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)
_DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)


class EgressFailureCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    PRIVACY_DENIED = "privacy_denied"
    EGRESS_DENIED = "egress_denied"
    FETCH_FAILED = "fetch_failed"
    BODY_LIMIT = "body_limit"


class EgressFailure(Exception):
    """A closed outbound-fetch failure with no URL, response, or transport detail."""

    def __init__(self, code: EgressFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class ResponseStream(Protocol):
    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PinnedRequest:
    scheme: str
    hostname: str
    port: int
    pinned_address: str
    target: str
    headers: Mapping[str, str]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    stream: ResponseStream


class Resolver(Protocol):
    def resolve(self, hostname: str) -> tuple[str, ...]: ...


class PinnedTransport(Protocol):
    def request(self, request: PinnedRequest) -> TransportResponse: ...


@dataclass(frozen=True, slots=True)
class _Hop:
    canonical_url: str
    scheme: str
    hostname: str
    port: int
    target: str


class OutboundFetcher:
    """Fail-closed, pinned-transport implementation of the core fetch port."""

    def __init__(
        self,
        *,
        resolver: Resolver,
        transport: PinnedTransport,
        cookies: Mapping[str, str] | None = None,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_redirects: int = _DEFAULT_MAX_REDIRECTS,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        allowed_media_types: frozenset[str] = _DEFAULT_MEDIA_TYPES,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes < 1
            or not isinstance(max_redirects, int)
            or isinstance(max_redirects, bool)
            or max_redirects < 0
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or not isinstance(allowed_media_types, frozenset)
            or not allowed_media_types
        ):
            raise ValueError("invalid egress configuration")
        normalized_media_types = frozenset(
            _normalize_media_type(value) for value in allowed_media_types
        )
        self._resolver = resolver
        self._transport = transport
        self._cookies = _normalize_cookies(cookies)
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._timeout_seconds = float(timeout_seconds)
        self._allowed_media_types = normalized_media_types
        self._clock = clock

    def fetch(self, request: FetchRequest, *, privacy: PrivacyDecision) -> FetchResponse:
        if not isinstance(privacy, PrivacyDecision) or not privacy.authority.external_egress:
            _closed(EgressFailureCode.PRIVACY_DENIED)
        max_bytes = _bounded_positive_integer(request.max_bytes, self._max_bytes)
        max_redirects = _bounded_nonnegative_integer(request.max_redirects, self._max_redirects)
        timeout_seconds = _bounded_timeout(request.timeout_seconds, self._timeout_seconds)
        try:
            now = self._clock()
        except Exception:
            _closed(EgressFailureCode.FETCH_FAILED)
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not isfinite(now):
            _closed(EgressFailureCode.FETCH_FAILED)
        deadline = now + timeout_seconds
        if not isfinite(deadline):
            _closed(EgressFailureCode.FETCH_FAILED)

        current_url = request.url
        redirects = 0
        seen_urls: set[str] = set()
        redirected = False
        while True:
            try:
                hop = _parse_hop(current_url)
            except EgressFailure:
                if redirected:
                    _closed(EgressFailureCode.EGRESS_DENIED)
                raise
            if hop.canonical_url in seen_urls:
                _closed(EgressFailureCode.EGRESS_DENIED)
            seen_urls.add(hop.canonical_url)
            address = self._resolve_public_address(hop.hostname)
            remaining = self._remaining_seconds(deadline)
            response = self._request_hop(hop, address, request.allowed_cookie_domains, remaining)
            if not _is_redirect(response.status):
                return self._read_response(response, hop.canonical_url, max_bytes)

            _close_stream(response.stream)
            location = _header(response.headers, "location")
            if not location or redirects >= max_redirects:
                _closed(EgressFailureCode.EGRESS_DENIED)
            redirects += 1
            redirected = True
            current_url = urljoin(hop.canonical_url, location)

    def _resolve_public_address(self, hostname: str) -> str:
        try:
            answers = self._resolver.resolve(hostname)
        except Exception:
            _closed(EgressFailureCode.EGRESS_DENIED)
        if not answers:
            _closed(EgressFailureCode.EGRESS_DENIED)
        parsed_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for answer in answers:
            if not isinstance(answer, str):
                _closed(EgressFailureCode.EGRESS_DENIED)
            try:
                address = ipaddress.ip_address(answer)
            except ValueError:
                _closed(EgressFailureCode.EGRESS_DENIED)
            if _denied_address(address):
                _closed(EgressFailureCode.EGRESS_DENIED)
            parsed_addresses.append(address)
        return str(parsed_addresses[0])

    def _request_hop(
        self,
        hop: _Hop,
        address: str,
        allowed_cookie_domains: tuple[str, ...],
        timeout_seconds: float,
    ) -> TransportResponse:
        headers: dict[str, str] = {
            "accept": ", ".join(sorted(self._allowed_media_types)),
            "host": hop.hostname,
            "user-agent": "open-brain/0.1",
        }
        cookie = _cookie_for_host(hop.hostname, self._cookies, allowed_cookie_domains)
        if cookie is not None:
            headers["cookie"] = cookie
        try:
            return self._transport.request(
                PinnedRequest(
                    scheme=hop.scheme,
                    hostname=hop.hostname,
                    port=hop.port,
                    pinned_address=address,
                    target=hop.target,
                    headers=headers,
                    timeout_seconds=timeout_seconds,
                )
            )
        except Exception:
            _closed(EgressFailureCode.FETCH_FAILED)

    def _read_response(
        self, response: TransportResponse, final_url: str, max_bytes: int
    ) -> FetchResponse:
        try:
            media_type = _response_media_type(response.headers)
            if media_type not in self._allowed_media_types:
                _closed(EgressFailureCode.FETCH_FAILED)
            body = bytearray()
            while True:
                remaining = max_bytes - len(body)
                chunk = response.stream.read(min(_READ_CHUNK_BYTES, remaining + 1))
                if not isinstance(chunk, bytes):
                    _closed(EgressFailureCode.FETCH_FAILED)
                if not chunk:
                    return FetchResponse(
                        final_url=final_url,
                        status=response.status,
                        media_type=media_type,
                        body=bytes(body),
                    )
                body.extend(chunk)
                if len(body) > max_bytes:
                    _closed(EgressFailureCode.BODY_LIMIT)
        except EgressFailure:
            raise
        except Exception:
            _closed(EgressFailureCode.FETCH_FAILED)
        finally:
            _close_stream(response.stream)

    def _remaining_seconds(self, deadline: float) -> float:
        try:
            now = self._clock()
        except Exception:
            _closed(EgressFailureCode.FETCH_FAILED)
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not isfinite(now):
            _closed(EgressFailureCode.FETCH_FAILED)
        remaining = deadline - now
        if not isfinite(remaining) or remaining <= 0:
            _closed(EgressFailureCode.FETCH_FAILED)
        return remaining


def _parse_hop(url: str) -> _Hop:
    if (
        not isinstance(url, str)
        or not url
        or any(unicodedata.category(character) == "Cc" for character in unquote(url))
    ):
        _closed(EgressFailureCode.INVALID_INPUT)
    try:
        parsed = urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        _closed(EgressFailureCode.INVALID_INPUT)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.fragment
        or "@" in unquote(parsed.netloc)
        or port not in {None, *_ALLOWED_PORTS}
    ):
        _closed(EgressFailureCode.INVALID_INPUT)
    hostname = _canonical_hostname(hostname)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        _closed(EgressFailureCode.INVALID_INPUT)
    scheme = parsed.scheme.lower()
    effective_port = port if port is not None else 443 if scheme == "https" else 80
    netloc = hostname if port is None else f"{hostname}:{port}"
    path = parsed.path or "/"
    target = path if not parsed.query else f"{path}?{parsed.query}"
    return _Hop(
        canonical_url=urlunsplit((scheme, netloc, path, parsed.query, "")),
        scheme=scheme,
        hostname=hostname,
        port=effective_port,
        target=target,
    )


def _canonical_hostname(hostname: str) -> str:
    try:
        canonical = hostname.lower().removesuffix(".").encode("idna").decode("ascii")
    except UnicodeError:
        _closed(EgressFailureCode.INVALID_INPUT)
    if (
        not canonical
        or len(canonical) > 253
        or any(not _HOST_LABEL.fullmatch(label) for label in canonical.split("."))
    ):
        _closed(EgressFailureCode.INVALID_INPUT)
    return canonical


def _denied_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _denied_address(address.ipv4_mapped)
    return (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or any(
            address.version == network.version and address in network
            for network in _DOCUMENTATION_NETWORKS
        )
        or any(
            address.version == network.version and address in network
            for network in _METADATA_NETWORKS
        )
    )


def _cookie_for_host(
    hostname: str,
    cookies: tuple[tuple[str, str], ...],
    allowed_cookie_domains: tuple[str, ...],
) -> str | None:
    normalized_allowed = {_canonical_hostname(domain) for domain in allowed_cookie_domains}
    selected = [
        value
        for domain, value in cookies
        if domain in normalized_allowed and _domain_matches(hostname, domain)
    ]
    return "; ".join(selected) if selected else None


def _normalize_cookies(cookies: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if cookies is None:
        return ()
    normalized: list[tuple[str, str]] = []
    for domain, value in cookies.items():
        if not isinstance(domain, str) or not isinstance(value, str) or not value:
            raise ValueError("invalid egress configuration")
        normalized.append((_canonical_hostname(domain.removeprefix(".")), value))
    return tuple(normalized)


def _domain_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _response_media_type(headers: Mapping[str, str]) -> str:
    content_type = _header(headers, "content-type")
    if content_type is None:
        _closed(EgressFailureCode.FETCH_FAILED)
    return _normalize_media_type(content_type.split(";", maxsplit=1)[0])


def _normalize_media_type(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid egress configuration")
    normalized = value.strip().lower()
    if not normalized or "/" not in normalized:
        raise ValueError("invalid egress configuration")
    return normalized


def _header(headers: Mapping[str, str], expected_name: str) -> str | None:
    for name, value in headers.items():
        if isinstance(name, str) and name.lower() == expected_name and isinstance(value, str):
            return value
    return None


def _close_stream(stream: ResponseStream) -> None:
    with suppress(Exception):
        stream.close()


def _is_redirect(status: int) -> bool:
    return isinstance(status, int) and not isinstance(status, bool) and 300 <= status < 400


def _bounded_positive_integer(value: int, upper_bound: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _closed(EgressFailureCode.INVALID_INPUT)
    return min(value, upper_bound)


def _bounded_nonnegative_integer(value: int, upper_bound: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _closed(EgressFailureCode.INVALID_INPUT)
    return min(value, upper_bound)


def _bounded_timeout(value: float, upper_bound: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(value)
        or value <= 0
    ):
        _closed(EgressFailureCode.INVALID_INPUT)
    return min(float(value), upper_bound)


def _closed(code: EgressFailureCode) -> NoReturn:
    raise EgressFailure(code)
