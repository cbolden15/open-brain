"""Optional Calendar HTTP with a process-enforced total request deadline."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import time
from email.message import Message
from http.client import HTTPResponse
from typing import BinaryIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

_MAX_REQUEST_BYTES = 131_072
_MAX_RESPONSE_BYTES = 1_048_576


class BoundedResponse:
    """A fully buffered response; no live network socket escapes the child."""

    def __init__(self, body: bytes, status: int, content_type: str) -> None:
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self._body = io.BytesIO(body)

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def close(self) -> None:
        self._body.close()

    def __enter__(self) -> BoundedResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def bounded_urlopen(request: Request, *, timeout: float, maximum: int) -> BoundedResponse:
    """Pass secrets through stdin, kill/reap on timeout, and return bounded bytes."""
    if not 0 < timeout <= 600 or not 1 <= maximum <= _MAX_RESPONSE_BYTES:
        raise ValueError("invalid bounded request")
    data = request.data
    if data is not None and not isinstance(data, bytes):
        raise URLError("streaming request body is unsupported")
    payload = json.dumps({
        "url": request.full_url, "method": request.get_method(),
        "headers": dict(request.header_items()),
        "body": base64.b64encode(data or b"").decode("ascii"),
        "maximum": maximum, "timeout": timeout,
    }).encode()
    if len(payload) > _MAX_REQUEST_BYTES:
        raise URLError("request exceeds limit")
    started = time.monotonic()
    child = subprocess.Popen(
        [sys.executable, "-I", "-m", "open_brain_connectors.runtime.google_calendar_http"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"},
        start_new_session=True,
    )
    try:
        output, _ = child.communicate(payload, timeout=max(0.001, timeout - (
            time.monotonic() - started)))
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate(timeout=2)
        raise TimeoutError("calendar request deadline exceeded") from None
    except BaseException:
        child.kill()
        child.communicate(timeout=2)
        raise
    if child.returncode != 0 or len(output) > 2 * (maximum + 1) + 4096:
        raise URLError("calendar transport unavailable")
    try:
        value = json.loads(output)
        if not isinstance(value, dict):
            raise ValueError
        if value.get("error") == "http":
            code = value.get("status")
            if type(code) is not int or not 100 <= code <= 599:
                raise ValueError
            raise HTTPError(request.full_url, code, "calendar request failed", Message(), None)
        if value.get("error"):
            raise URLError("calendar transport unavailable")
        body = base64.b64decode(value["body"], validate=True)
        content_type, status = value["content_type"], value["status"]
        if (len(body) > maximum + 1 or type(status) is not int or status != 200
                or type(content_type) is not str or len(content_type) > 256):
            raise ValueError
        return BoundedResponse(body, status, content_type)
    except (ValueError, TypeError, KeyError) as error:
        raise URLError("invalid calendar transport response") from error


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self, req: Request, fp: HTTPResponse, code: int, msg: str,
        headers: Message, newurl: str,
    ) -> None:
        return None


def _run_child(stdin: BinaryIO) -> dict[str, object]:
    raw = stdin.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        raise ValueError
    value = json.loads(raw)
    maximum, timeout = value["maximum"], value["timeout"]
    if (type(maximum) is not int or not 1 <= maximum <= _MAX_RESPONSE_BYTES
            or type(timeout) not in {float, int} or not 0 < timeout <= 600
            or value["method"] not in {"GET", "POST"}):
        raise ValueError
    data = base64.b64decode(value["body"], validate=True)
    request = Request(value["url"], data=data or None, headers=value["headers"],
                      method=value["method"])
    response = cast(HTTPResponse, build_opener(_RejectRedirects()).open(
        request, timeout=min(15, timeout)))
    with response:
        body = response.read(maximum + 1)
        content_type = response.headers.get_content_type()
        status = response.status
    if status != 200:
        return {"error": "http", "status": status}
    return {"body": base64.b64encode(body).decode("ascii"),
            "content_type": content_type[:256], "status": status}


def main() -> None:
    try:
        result = _run_child(sys.stdin.buffer)
    except HTTPError as error:
        result = {"error": "http", "status": error.code}
    except Exception:
        # Provider bodies, URLs, headers and credentials must never enter stderr.
        result = {"error": "unavailable"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
