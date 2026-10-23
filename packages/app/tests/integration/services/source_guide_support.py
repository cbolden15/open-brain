"""Real optional collector child for guide tests; only Slack HTTP and the OS keychain are synthetic.

`write_launcher` creates an owner-controlled executable that runs the installed
workspace collector CLI unchanged, after replacing the `PriorityRuntime`
transport with a file-scripted Slack HTTP boundary and an in-memory credential
store. Every request is appended to `calls.jsonl` so tests can assert exact
fetch counts across separate child processes. Unexpected requests fail closed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ACCOUNT = "account:" + "a" * 64
TOKEN = "synthetic-access-token"
SUPPORT_DIRECTORY = Path(__file__).resolve().parent


def write_launcher(directory: Path, fixture: Path) -> Path:
    directory.mkdir(mode=0o700, exist_ok=True)
    fixture.mkdir(mode=0o700, exist_ok=True)
    (fixture / "http.json").write_text(json.dumps({}))
    path = directory / "open-brain-collector"
    path.write_text(
        f"#!{sys.executable} -I\n"
        "import sys\n"
        f"sys.path.insert(0, {str(SUPPORT_DIRECTORY)!r})\n"
        "import source_guide_support\n"
        f"sys.exit(source_guide_support.collector_main({str(fixture)!r}, sys.argv[1:]))\n"
    )
    path.chmod(0o700)
    return path


def queue(fixture: Path, method: str, payloads: Sequence[dict[str, object]]) -> None:
    """Append scripted Slack API responses for one method (`conversations.list` / `.history`)."""
    state = json.loads((fixture / "http.json").read_text())
    state.setdefault(method, []).extend(payloads)
    (fixture / "http.json").write_text(json.dumps(state))


def calls(fixture: Path, method: str | None = None) -> list[dict[str, object]]:
    path = fixture / "calls.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if method is None or row["method"] == method]


def history_page(
    messages: Sequence[dict[str, object]], next_cursor: str = ""
) -> dict[str, object]:
    return {
        "ok": True,
        "messages": list(messages),
        "response_metadata": {"next_cursor": next_cursor},
    }


def channel_page(
    channels: Sequence[tuple[str, str]], next_cursor: str = ""
) -> dict[str, object]:
    return {
        "ok": True,
        "channels": [{"id": channel, "name": name} for channel, name in channels],
        "response_metadata": {"next_cursor": next_cursor},
    }


def message(ts: str, text: str) -> dict[str, object]:
    return {"ts": ts, "text": text, "user": "USYNTHETIC"}


def connect_account(collector_state: Path, display_name: str = "Synthetic Slack") -> None:
    """Register one synthetic Slack account exactly as the existing connect flow stores it."""
    from open_brain_connectors.runtime.live_storage import PrivateJsonStore
    from open_brain_connectors.runtime.slack_auth import SLACK_USER_SCOPES, _credential_reference

    account_root = collector_state.parent / "live" / "accounts" / "slack"
    PrivateJsonStore(account_root).write(
        "slack-account-" + ACCOUNT.removeprefix("account:") + ".json",
        {
            "schema_version": 1,
            "credential_ref": _credential_reference(ACCOUNT, account_root),
            "account": {
                "provider": "slack",
                "connection_id": ACCOUNT,
                "display_name": display_name,
                "expires_at_epoch": 4_000_000_000,
                "scopes": list(SLACK_USER_SCOPES),
            },
        },
    )


def collector_main(fixture: str, argv: list[str]) -> int:
    from open_brain_collector import live_manager
    from open_brain_connectors.runtime.live_http import LiveHttpResponse, LiveHttpTransport

    root = Path(fixture)

    class _Credentials:
        def get(self, reference: str) -> dict[str, object] | None:
            return {"access_token": TOKEN}

        def set(self, reference: str, value: dict[str, object]) -> None:
            raise AssertionError("guide tests never write credentials")

        def delete(self, reference: str) -> None:
            raise AssertionError("guide tests never delete credentials")

        def status(self, reference: str) -> str:
            return "available"

    class _SlackHttp(LiveHttpTransport):
        def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            form: dict[str, str] | None = None,
            max_bytes: int = 1_048_576,
            timeout_seconds: float = 30,
        ) -> LiveHttpResponse:
            parsed = urlsplit(url)
            assert method == "GET" and parsed.netloc == "slack.com" and form is None
            assert headers == {"authorization": f"Bearer {TOKEN}"}
            api = parsed.path.removeprefix("/api/")
            with (root / "calls.jsonl").open("a") as handle:
                handle.write(json.dumps({"method": api, "query": parse_qs(parsed.query)}) + "\n")
            if (root / "hold").exists():
                (root / "started").write_text(str(os.getpid()))
                deadline = time.monotonic() + 60
                while not (root / "release").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
            state = json.loads((root / "http.json").read_text())
            pending = state.get(api) or []
            if not pending:
                raise AssertionError("unexpected Slack request")
            payload = pending.pop(0)
            (root / "http.json").write_text(json.dumps(state))
            status = int(payload.pop("_status", 200))
            return LiveHttpResponse(status, {}, json.dumps(payload).encode())

    original = live_manager.PriorityRuntime.__init__

    def runtime(self: object, path: Path, **_ignored: object) -> None:
        original(self, path, http=_SlackHttp(), credentials=_Credentials())  # type: ignore[arg-type]

    live_manager.PriorityRuntime.__init__ = runtime  # type: ignore[method-assign,assignment]
    from open_brain_collector.cli import main

    return main(argv)
