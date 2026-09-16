"""Fixed-origin provider requests with a process-enforced wall-clock deadline."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from http.client import HTTPResponse
from typing import BinaryIO, cast
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json

_ORIGINS = frozenset(
    {
        "gmail.googleapis.com",
        "www.googleapis.com",
        "oauth2.googleapis.com",
        "openidconnect.googleapis.com",
        "slack.com",
    }
)
_MAX_RESPONSE = 1_048_576


@dataclass(frozen=True, slots=True)
class LiveHttpResponse:
    status: int
    headers: dict[str, str] = field(repr=False)
    body: bytes = field(repr=False)

    def json(self) -> dict[str, object]:
        try:
            value = json.loads(self.body)
            if not isinstance(value, dict):
                raise ValueError
            return cast(dict[str, object], value)
        except ValueError, UnicodeError, RecursionError:
            raise LiveSourceError("source_invalid_response") from None

    def retry_after_seconds(self) -> int | None:
        value = self.headers.get("retry-after", "")
        if not value:
            return None
        try:
            seconds = (
                int(value)
                if value.isdigit()
                else int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
            )
            return min(86_400, max(1, seconds))
        except ValueError, TypeError, OverflowError:
            return None


def _validate_request(
    method: str, url: str, headers: dict[str, str], maximum: int, timeout: float
) -> None:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in _ORIGINS
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and len(url) <= 16_384
            and not any(ord(c) < 33 or ord(c) == 127 for c in url)
            and method in {"GET", "POST"}
            and type(maximum) is int
            and 1 <= maximum <= _MAX_RESPONSE
            and 0 < timeout <= 60
            and len(headers) <= 12
        )
        if not valid or any(
            type(k) is not str
            or type(v) is not str
            or k.lower()
            not in {"authorization", "content-type", "user-agent", "accept", "if-none-match"}
            or len(v) > 16_384
            or any(ord(c) < 32 for c in v)
            for k, v in headers.items()
        ):
            raise ValueError
    except ValueError, TypeError, AttributeError:
        raise LiveSourceError("source_invalid_request") from None


class LiveHttpTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        max_bytes: int = _MAX_RESPONSE,
        timeout_seconds: float = 30,
    ) -> LiveHttpResponse:
        request_headers = dict(headers or {})
        if form is not None:
            if method != "POST" or any(
                type(k) is not str or type(v) is not str for k, v in form.items()
            ):
                raise LiveSourceError("source_invalid_request")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        _validate_request(method, url, request_headers, max_bytes, timeout_seconds)
        payload = bounded_json(
            {
                "url": url,
                "method": method,
                "headers": request_headers,
                "form": form,
                "maximum": max_bytes,
                "timeout": timeout_seconds,
            }
        )
        try:
            child = subprocess.Popen(
                [sys.executable, "-I", "-m", "open_brain_connectors.runtime.live_http"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"},
                start_new_session=True,
            )
            try:
                raw, _ = child.communicate(payload, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=2)
                raise LiveSourceError("source_request_timeout") from None
            except BaseException:
                child.kill()
                child.communicate(timeout=2)
                raise
        except OSError:
            raise LiveSourceError("source_transport_unavailable") from None
        if child.returncode or len(raw) > 2 * max_bytes + 8192:
            raise LiveSourceError("source_transport_unavailable")
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("error"):
                raise ValueError
            body = base64.b64decode(result["body"], validate=True)
            status, response_headers = result["status"], result["headers"]
            if (
                type(status) is not int
                or not 100 <= status <= 599
                or len(body) > max_bytes
                or not isinstance(response_headers, dict)
                or any(
                    k not in {"content-type", "retry-after", "etag"}
                    or type(v) is not str
                    or len(v) > 512
                    for k, v in response_headers.items()
                )
            ):
                raise ValueError
            return LiveHttpResponse(status, response_headers, body)
        except ValueError, TypeError, KeyError:
            raise LiveSourceError("source_transport_unavailable") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: HTTPResponse,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


def _request_child(stdin: BinaryIO) -> dict[str, object]:
    raw = stdin.read(131_073)
    if len(raw) > 131_072:
        raise ValueError
    value = json.loads(raw)
    method, url, headers = value["method"], value["url"], value["headers"]
    maximum, timeout = value["maximum"], value["timeout"]
    _validate_request(method, url, headers, maximum, timeout)
    form = value["form"]
    request = Request(
        url,
        data=urlencode(form).encode() if form is not None else None,
        headers=headers,
        method=method,
    )
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=min(timeout, 15))
    except HTTPError as error:
        response = error
    with response:
        body = response.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError
        selected_headers = {
            name: str(response.headers[name])[:512]
            for name in ("content-type", "retry-after", "etag")
            if response.headers.get(name) is not None
        }
        return {
            "status": response.status,
            "headers": selected_headers,
            "body": base64.b64encode(body).decode("ascii"),
        }


def main() -> None:
    try:
        result = _request_child(sys.stdin.buffer)
    except Exception:
        result = {"error": "unavailable"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
