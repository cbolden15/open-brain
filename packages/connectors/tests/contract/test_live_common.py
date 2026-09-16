from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest

from open_brain_connectors.runtime import live_http
from open_brain_connectors.runtime.live_common import LiveSourceError, local_source_privacy
from open_brain_connectors.runtime.live_http import LiveHttpResponse, LiveHttpTransport
from open_brain_connectors.runtime.live_oauth import authorize_loopback
from open_brain_connectors.runtime.live_storage import OsCredentialStore, PrivateJsonStore


def test_private_store_roundtrip_permissions_and_nonfinite_state(tmp_path: Path) -> None:
    store = PrivateJsonStore(tmp_path / "state")
    store.write("test.json", {"schema_version": 1})
    assert store.read("test.json") == {"schema_version": 1}
    assert (store.root / "test.json").stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    with pytest.raises(LiveSourceError):
        store.write("test.json", {"bad": float("nan")})
    assert store.read("test.json") == {"schema_version": 1}
    store.delete("test.json")
    assert store.read("test.json") is None


def test_private_store_rejects_symlinks_hardlinks_and_public_files(tmp_path: Path) -> None:
    store = PrivateJsonStore(tmp_path / "state")
    outside = tmp_path / "outside.json"
    outside.write_text('{"untouched":true}')
    (store.root / "link.json").symlink_to(outside)
    operations: tuple[Callable[[], object], ...] = (
        lambda: store.read("link.json"),
        lambda: store.write("link.json", {}),
        lambda: store.delete("link.json"),
    )
    for operation in operations:
        with pytest.raises(LiveSourceError):
            operation()
    assert outside.read_text() == '{"untouched":true}'
    os.link(outside, store.root / "hard.json")
    with pytest.raises(LiveSourceError):
        store.read("hard.json")
    with pytest.raises(LiveSourceError):
        store.read("../outside.json")
    linked = tmp_path / "linked"
    linked.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(LiveSourceError):
        PrivateJsonStore(linked / "child")


def test_private_store_lock_serializes_distinct_instances(tmp_path: Path) -> None:
    first = PrivateJsonStore(tmp_path / "state")
    second = PrivateJsonStore(tmp_path / "state")
    with (
        first.lock("refresh"),
        pytest.raises(LiveSourceError, match="source_busy"),
        second.lock("refresh", timeout_seconds=0.02),
    ):
        pytest.fail("second writer entered")
    with second.lock("refresh", timeout_seconds=0):
        second.write("account.json", {"refreshed": True})


def test_private_store_retries_raced_lock_creation_within_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PrivateJsonStore(tmp_path / "state")
    original_open = os.open
    attempts = 0

    def raced_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        nonlocal attempts
        if path == "refresh.lock":
            attempts += 1
            if attempts == 1:
                raise FileNotFoundError(errno.ENOENT, "raced first create", path)
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", raced_open)
    with store.lock("refresh", timeout_seconds=0.2):
        store.write("account.json", {"refreshed": True})
    assert attempts == 2
    assert store.read("account.json") == {"refreshed": True}
    attempts = 0
    with (
        pytest.raises(LiveSourceError, match="source_busy"),
        store.lock("refresh", timeout_seconds=0),
    ):
        pytest.fail("expired lock acquisition entered")
    assert attempts == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://slack.com/api/auth.test",
        "https://slack.com.evil.test/api/auth.test",
        "https://slack.com@evil.test/api/auth.test",
        "https://slack.com:444/api/auth.test",
        "https://127.0.0.1/",
        "https://slack.com/api/auth.test#fragment",
    ],
)
def test_http_origin_policy_rejects_before_launch(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid origin started a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(LiveSourceError, match="source_invalid_request"):
        LiveHttpTransport().request("GET", url)


def test_http_error_preserves_rate_limit_and_never_follows_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = Message()
    headers["Retry-After"] = "60"
    headers["Set-Cookie"] = "secret"

    class FakeOpener:
        def open(self, *args: object, **kwargs: object) -> None:
            raise HTTPError(
                "https://slack.com/api/conversations.history",
                429,
                "limited",
                headers,
                io.BytesIO(b'{"ok":false}'),
            )

    def opener(handler: object) -> FakeOpener:
        assert isinstance(handler, live_http._NoRedirect)
        return FakeOpener()

    monkeypatch.setattr(live_http, "build_opener", opener)
    raw = json.dumps(
        {
            "method": "GET",
            "url": "https://slack.com/api/conversations.history",
            "headers": {},
            "form": None,
            "maximum": 100,
            "timeout": 5,
        }
    ).encode()
    result = live_http._request_child(io.BytesIO(raw))
    assert result["status"] == 429
    assert result["headers"] == {"retry-after": "60"}
    assert LiveHttpResponse(429, {"retry-after": "60"}, b"{}").retry_after_seconds() == 60
    assert "secret" not in repr(LiveHttpResponse(200, {}, b"secret"))


def test_transport_total_deadline_reaps_child_without_secrets_in_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert "synthetic-private-token" not in " ".join(command)
        child = real_popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    started = time.monotonic()
    with pytest.raises(LiveSourceError, match="source_request_timeout"):
        LiveHttpTransport().request(
            "GET",
            "https://slack.com/api/auth.test",
            headers={"Authorization": "Bearer synthetic-private-token"},
            timeout_seconds=0.1,
        )
    assert time.monotonic() - started < 2
    assert len(children) == 1 and children[0].poll() is not None


def test_oauth_rejects_wrong_state_then_accepts_bound_callback() -> None:
    threads: list[threading.Thread] = []
    failures: list[Exception] = []
    challenge_seen: list[str] = []

    def build(redirect: str, state: str, challenge: str) -> str:
        challenge_seen.append(challenge)
        return "https://slack.com/oauth/v2/authorize?" + urlencode(
            {"redirect_uri": redirect, "state": state, "code_challenge": challenge}
        )

    def browser(url: str) -> bool:
        params = parse_qs(urlsplit(url).query)
        redirect = params["redirect_uri"][0]

        def callback() -> None:
            try:
                with pytest.raises(HTTPError) as failure:
                    urlopen(redirect + "?state=wrong&code=synthetic", timeout=2)
                assert failure.value.code == 400
                with urlopen(
                    redirect
                    + "?"
                    + urlencode({"state": params["state"][0], "code": "synthetic-code"}),
                    timeout=2,
                ) as r:
                    assert r.status == 200
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=callback)
        threads.append(thread)
        thread.start()
        return True

    code = authorize_loopback(
        build, timeout_seconds=5, browser_opener=browser, hostname="localhost"
    )
    for thread in threads:
        thread.join(timeout=2)
    assert not failures and code.code == "synthetic-code"
    assert code.redirect_uri.startswith("http://localhost:") and len(code.verifier) >= 43
    assert len(challenge_seen[0]) == 43
    assert "synthetic-code" not in repr(code) and code.verifier not in repr(code)


def test_credentials_only_use_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[list[str], bytes | None]] = []

    class Child:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            self.command = command

        def communicate(self, payload: bytes | None, timeout: float) -> tuple[bytes, None]:
            captured.append((self.command, payload))
            return b"", None

    monkeypatch.setattr(subprocess, "Popen", Child)
    store = OsCredentialStore()
    store._mac = True
    store._executable = "/usr/bin/security"
    store.set("gmail:synthetic", {"access_token": "synthetic-private-token"})
    assert captured[0][0][-1] == "open_brain_connectors.runtime.live_keychain"
    assert "synthetic-private-token" not in " ".join(captured[0][0])
    assert captured[0][1] is not None
    sent = json.loads(captured[0][1])
    assert sent["reference"] == "gmail:synthetic"
    assert json.loads(sent["payload"]) == {"access_token": "synthetic-private-token"}
    assert not local_source_privacy().authority.cloud
    assert not local_source_privacy().authority.external_egress
