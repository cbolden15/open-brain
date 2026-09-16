from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from open_brain_connectors.runtime.google_calendar_http import bounded_urlopen


class _Handler(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        self.seen.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
            return
        body = b'{"synthetic": "response"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for item in body:
                self.wfile.write(bytes([item]))
                self.wfile.flush()
                if self.path == "/slow":
                    time.sleep(0.1)
        except OSError:
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _server() -> Iterator[str]:
    _Handler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_http_reads_bounded_body_and_rejects_redirects() -> None:
    with _server() as origin:
        with bounded_urlopen(Request(origin + "/ok"), timeout=3, maximum=64) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "application/json"
            assert response.read() == b'{"synthetic": "response"}'
        with bounded_urlopen(Request(origin + "/large"), timeout=3, maximum=4) as response:
            # Caller detects truncation instead of silently accepting a partial response.
            assert len(response.read()) == 5
        with pytest.raises(HTTPError) as error:
            bounded_urlopen(Request(origin + "/redirect"), timeout=3, maximum=64)
        assert error.value.code == 302
        assert "/target" not in _Handler.seen


def test_drip_feed_has_total_wall_deadline_and_child_is_reaped() -> None:
    with _server() as origin:
        before = time.monotonic()
        with pytest.raises(TimeoutError, match="calendar request deadline exceeded"):
            bounded_urlopen(Request(origin + "/slow"), timeout=0.6, maximum=64)
        assert time.monotonic() - before < 1.5
        assert "/slow" in _Handler.seen
