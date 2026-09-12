# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Framework-neutral authenticated, read-only UI boundary."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address

from open_brain_legacy._compat.open_brain.capture.auth import BearerAuthenticator

from .ports import PageDocument, PageReader, PageReadRequest

_PRIVATE_IPV4_NETWORKS = (
    IPv4Network(".".join(("10", "0", "0", "0")) + "/8"),
    IPv4Network(".".join(("172", "16", "0", "0")) + "/12"),
    IPv4Network(".".join(("192", "168", "0", "0")) + "/16"),
)
_PRIVATE_IPV6_NETWORKS = (IPv6Network("fc00::/7"),)


@dataclass(frozen=True, slots=True)
class UiBindConfig:
    """Listener-neutral bind values with a loopback-only default."""

    host: str = "127.0.0.1"
    port: int = 8788
    allow_private_network: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.host, str)
            or not self.host
            or self.host != self.host.strip()
            or type(self.port) is not int
            or not 1 <= self.port <= 65_535
            or type(self.allow_private_network) is not bool
        ):
            raise ValueError("unsafe UI bind")
        try:
            address = ip_address(self.host)
        except ValueError:
            raise ValueError("unsafe UI bind") from None
        if address.is_loopback:
            return
        if not self.allow_private_network or not _is_private_address(address):
            raise ValueError("unsafe UI bind")


@dataclass(frozen=True, slots=True)
class UiRequest:
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class UiResponse:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...]


class UiHandler:
    """Authenticate before routing any read-only UI request."""

    def __init__(self, *, expected_bearer_token: str, page_reader: PageReader) -> None:
        self._authenticator = BearerAuthenticator(expected_bearer_token)
        self._page_reader = page_reader

    def handle(self, request: UiRequest) -> UiResponse:
        if not isinstance(request, UiRequest):
            return _text_response(400, "invalid_request")
        authorization = _authorization_values(request.headers)
        if authorization is None:
            return _text_response(400, "invalid_request")
        if not self._authenticator.authenticate(authorization):
            return _text_response(401, "unauthorized")
        if not isinstance(request.method, str) or not isinstance(request.path, str):
            return _text_response(400, "invalid_request")
        if request.method != "GET":
            return _text_response(405, "method_not_allowed", allow="GET")
        if request.path == "/health":
            return UiResponse(
                status=200,
                body=_json_bytes({"status": "ok"}),
                headers=(("Content-Type", "application/json"),),
            )
        if not request.path.startswith("/pages/"):
            return _text_response(404, "not_found")
        return page_response(self._page_reader, request.path.removeprefix("/pages/"))


def page_response(page_reader: PageReader, page_id: str) -> UiResponse:
    if not callable(getattr(page_reader, "read", None)):
        return _text_response(503, "service_unavailable")
    try:
        page_request = PageReadRequest(page_id=page_id)
    except ValueError:
        return _text_response(404, "not_found")
    try:
        page = page_reader.read(page_request)
    except Exception:
        return _text_response(503, "service_unavailable")
    if page is None:
        return _text_response(404, "not_found")
    try:
        validated_page = PageDocument(
            page_id=page.page_id,
            title=page.title,
            markdown=page.markdown,
            trust=page.trust,
        )
        if validated_page.page_id != page_request.page_id:
            raise ValueError("page identifier mismatch")
        body = _render_page(validated_page)
    except (AttributeError, TypeError, ValueError):
        return _text_response(503, "service_unavailable")
    return _html_response(body)


def _is_private_address(address: IPv4Address | IPv6Address) -> bool:
    if isinstance(address, IPv4Address):
        return any(address in network for network in _PRIVATE_IPV4_NETWORKS)
    return any(address in network for network in _PRIVATE_IPV6_NETWORKS)


def _authorization_values(headers: object) -> tuple[str, ...] | None:
    if not isinstance(headers, tuple):
        return None
    values: list[str] = []
    for header in headers:
        if (
            not isinstance(header, tuple)
            or len(header) != 2
            or not isinstance(header[0], str)
            or not isinstance(header[1], str)
            or not header[0].isascii()
        ):
            return None
        if header[0].lower() == "authorization":
            values.append(header[1])
    return tuple(values)


def _text_response(status: int, text: str, *, allow: str | None = None) -> UiResponse:
    headers = [("Content-Type", "text/plain; charset=utf-8")]
    if allow is not None:
        headers.append(("Allow", allow))
    return UiResponse(status=status, body=text.encode("utf-8"), headers=tuple(headers))


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+)$")
_LINK = re.compile(r"(?<!!)\[([^]\n]+)\]\([^\n)]*\)")


def _render_page(page: PageDocument) -> bytes:
    title = html.escape(page.title.text, quote=True)
    markdown = _render_markdown(page.markdown.text)
    document = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{title}</title></head><body><main>{markdown}</main></body></html>"
    )
    return document.encode("utf-8")


def _render_markdown(markdown: str) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []

    def finish_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{' '.join(paragraph)}</p>")
            paragraph.clear()

    for line in markdown.splitlines():
        if not line.strip():
            finish_paragraph()
            continue
        heading = _HEADING.fullmatch(line)
        if heading is not None:
            finish_paragraph()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_render_inline(heading.group(2))}</h{level}>")
            continue
        paragraph.append(_render_inline(line))
    finish_paragraph()
    return "".join(blocks)


def _render_inline(value: str) -> str:
    rendered: list[str] = []
    offset = 0
    for link in _LINK.finditer(value):
        rendered.append(html.escape(value[offset : link.start()], quote=True))
        rendered.append(html.escape(link.group(1), quote=True))
        offset = link.end()
    rendered.append(html.escape(value[offset:], quote=True))
    return "".join(rendered)


def _html_response(body: bytes) -> UiResponse:
    return UiResponse(
        status=200,
        body=body,
        headers=(
            ("Content-Type", "text/html; charset=utf-8"),
            (
                "Content-Security-Policy",
                "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            ),
            ("X-Content-Type-Options", "nosniff"),
        ),
    )
