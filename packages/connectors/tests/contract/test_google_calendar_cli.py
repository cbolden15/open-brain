from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request
from uuid import uuid4

import pytest
from open_brain_engine.engine import open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_connectors.runtime import google_calendar_cli
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.google_calendar_provider import GoogleCalendarClient
from open_brain_connectors.runtime.source_cli import run_cli
from open_brain_connectors.runtime.source_host import existing_source_profile

CONNECTION = "account:calendar-integration"
CALENDAR = "synthetic@example.invalid"
LINK = "https://www.google.com/calendar/event?eid=synthetic-event"


class _Response:
    def __init__(self, value: object, status: int = 200) -> None:
        self.body = json.dumps(value).encode()
        self.status = status

    def read(self, amount: int = -1) -> bytes:
        return self.body[:amount] if amount >= 0 else self.body

    def close(self) -> None:
        pass


class _Google:
    def __init__(self) -> None:
        self.pages: list[tuple[int, dict[str, object]]] = []
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: int) -> _Response:
        assert timeout <= 15
        self.requests.append(request)
        if "/calendarList/" in request.full_url:
            return _Response({
                "id": CALENDAR, "summary": "Integration calendar", "timeZone": "America/Chicago",
                "accessRole": "reader",
            })
        assert self.pages, "Import/status must not make a provider request"
        status, page = self.pages.pop(0)
        return _Response(page, status)

    def event(self, description: str, *, link: str = LINK) -> dict[str, object]:
        return {
            "id": "synthetic-instance", "status": "confirmed", "summary": "Planning",
            "description": description, "htmlLink": link,
            "start": {"dateTime": "2026-09-15T10:00:00-05:00"},
            "end": {"dateTime": "2026-09-15T11:00:00-05:00"},
            "attendees": [{"displayName": "Avery"}, {"email": "omit@example.invalid"}],
            "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc",
        }

    def queue(self, *items: dict[str, object], token: str) -> None:
        self.pages.append((200, {"items": list(items), "nextSyncToken": token}))


def _args(tmp_path: Path, verb: str, *extra: str, brain: Path | None = None) -> list[str]:
    args = [
        "google-calendar", verb, "--connection-id", CONNECTION, "--calendar-id", CALENDAR,
        "--range-start", "2026-09-15T00:00:00-05:00",
        "--range-end", "2026-09-17T00:00:00-05:00", "--timezone", "America/Chicago",
        "--state-dir", str(tmp_path / "state"), "--brain-root", str(brain or tmp_path / "brain"),
        *extra,
    ]
    if verb == "preview":
        args.extend(["--credential-dir", str(tmp_path / "credentials")])
    return args


def _provider(monkeypatch: pytest.MonkeyPatch) -> _Google:
    google = _Google()
    monkeypatch.setattr(google_calendar_cli, "_client", lambda parsed: GoogleCalendarClient(
        parsed.connection_id, lambda: "synthetic-access-token", transport=google,
    ))
    return google


@pytest.mark.parametrize("change", ["update", "cancel"])
def test_provider_refuses_unordered_change_preserving_retrieval_and_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    google = _provider(monkeypatch)
    brain = tmp_path / "brain"
    profile = compile_single_user_local(brain)

    def execute(verb: str, *extra: str) -> dict[str, object]:
        assert run_cli(_args(tmp_path, verb, *extra)) == 0
        result: dict[str, object] = json.loads(capsys.readouterr().out)
        rendered = json.dumps(result)
        for private in ("Originalneedle", "Revisedneedle", "omit@example.invalid",
                        "synthetic-access-token", "next-cursor"):
            assert private not in rendered
        return result

    google.queue(google.event("Originalneedle discussion"), token="next-cursor-1")
    first = execute("preview")
    assert first["record_count"] == 1
    assert not open_local_engine(profile).retrieval.search("Originalneedle")
    assert execute("status")["pending"] is True
    first_import = execute("import", "--preview-id", str(first["preview_id"]))
    assert first_import["checkpoint_committed"] is True
    assert len(google.requests) == 2
    results = open_local_engine(profile).retrieval.search("Originalneedle")
    assert len(results) == 1 and results[0].trust == "third_party"
    assert results[0].provenance.source_origin == "third_party"

    changed: dict[str, object] = (google.event("Revisedneedle discussion", link=LINK + "&ctz=UTC")
               if change == "update" else {"id": "synthetic-instance", "status": "cancelled"})
    google.queue(changed, token="next-cursor-2")
    second = execute("preview")
    request = parse_qs(urlsplit(google.requests[-1].full_url).query)
    assert request["syncToken"] == ["next-cursor-1"]
    assert "timeMin" not in request and "timeMax" not in request
    state_before = {p.name: p.read_bytes() for p in (tmp_path / "state").glob("*.json")}
    assert run_cli(_args(tmp_path, "import", "--preview-id", str(second["preview_id"]))) == 78
    error = json.loads(capsys.readouterr().out)
    assert "google_calendar_import_failed" in json.dumps(error)
    assert {p.name: p.read_bytes() for p in (tmp_path / "state").glob("*.json")} == state_before
    tasks = open_local_engine(profile)
    assert len(tasks.retrieval.search("Originalneedle")) == 1
    assert not tasks.retrieval.search("Revisedneedle")
    query = subprocess.run([
        sys.executable, "-I", "-c",
        "from pathlib import Path; from open_brain.profile import compile_single_user_local; "
        "from open_brain_engine.engine import open_local_engine; import sys; "
        "r=open_local_engine(compile_single_user_local(Path(sys.argv[1])))"
        ".retrieval.search('Originalneedle'); print(len(r),r[0].trust)", str(brain),
    ], capture_output=True, text=True, timeout=20, check=True)
    assert query.stdout.strip() == "1 third_party"

    assert execute("status")["pending"] is True
    reopened = open_local_engine(profile)
    exported = tmp_path / "export"
    reopened.portability.export(exported, export_id=f"export_{uuid4()}")
    history = b"\n".join(path.read_bytes() for path in exported.rglob("*") if path.is_file())
    assert b"Originalneedle" in history and b"Revisedneedle" not in history
    assert LINK.encode() in history and b"Source revision:" in history
    assert b"omit@example.invalid" not in history


def test_expired_cursor_resync_refuses_unordered_tombstone_and_revocation_keeps_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    google = _provider(monkeypatch)
    profile = compile_single_user_local(tmp_path / "brain")
    google.queue(google.event("Removalneedle"), token="cursor-1")
    assert run_cli(_args(tmp_path, "preview")) == 0
    preview = json.loads(capsys.readouterr().out)
    assert run_cli(_args(tmp_path, "import", "--preview-id", preview["preview_id"])) == 0
    capsys.readouterr()
    before = {p.name: p.read_bytes() for p in (tmp_path / "state").glob("*.json")}
    google.pages.append((401, {"private_error": "do-not-print"}))
    assert run_cli(_args(tmp_path, "preview")) == 78
    error = capsys.readouterr().out
    assert "google_calendar_auth_required" in error and "do-not-print" not in error
    assert {p.name: p.read_bytes() for p in (tmp_path / "state").glob("*.json")} == before
    google.pages.append((410, {}))
    google.queue(token="cursor-2")
    assert run_cli(_args(tmp_path, "preview")) == 0
    resync = json.loads(capsys.readouterr().out)
    assert resync["full_sync"] is True and resync["record_count"] == 1
    before_apply = {p.name: p.read_bytes() for p in (tmp_path / "state").glob("*.json")}
    assert run_cli(_args(tmp_path, "import", "--preview-id", resync["preview_id"])) == 78
    assert "google_calendar_import_failed" in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in (tmp_path / "state").glob("*.json")} == before_apply
    tasks = open_local_engine(profile)
    assert len(tasks.retrieval.search("Removalneedle")) == 1
    assert not tasks.retrieval.search("unavailable")


def test_prepared_batch_cannot_be_applied_to_another_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    google = _provider(monkeypatch)
    compile_single_user_local(tmp_path / "brain")
    other = compile_single_user_local(tmp_path / "other")
    google.queue(google.event("Destinationneedle"), token="cursor-1")
    assert run_cli(_args(tmp_path, "preview")) == 0
    preview = json.loads(capsys.readouterr().out)
    assert run_cli(_args(tmp_path, "import", "--preview-id", preview["preview_id"],
                         brain=tmp_path / "other")) == 78
    assert "Destinationneedle" not in capsys.readouterr().out
    assert not open_local_engine(other).retrieval.search("Destinationneedle")


@pytest.mark.parametrize("case", ["missing", "symlink", "hardlink", "fifo", "oversize", "schema"])
def test_existing_brain_profile_rejects_unsafe_or_invalid_metadata(
    tmp_path: Path, case: str,
) -> None:
    brain = tmp_path / "brain"
    if case != "missing":
        brain.mkdir()
        config = brain / "brain.toml"
        target = tmp_path / "target"
        target.write_text("fixture")
        if case == "symlink":
            config.symlink_to(target)
        elif case == "hardlink":
            os.link(target, config)
        elif case == "fifo":
            os.mkfifo(config)
        else:
            config.write_text("a" * 16_385 if case == "oversize" else "layout_version = 999")
    with pytest.raises(ConnectorContractError, match="source_brain_unavailable"):
        existing_source_profile(brain)


def test_import_and_status_do_not_access_credentials_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    compile_single_user_local(tmp_path / "brain")

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("No credential access is allowed for local-only operations")

    monkeypatch.setattr(google_calendar_cli, "GoogleCalendarAuthStore", unexpected)
    assert run_cli(_args(tmp_path, "status")) == 0
    assert json.loads(capsys.readouterr().out)["configured"] is False
    assert run_cli(_args(tmp_path, "import", "--preview-id", "f" * 64)) == 78
    assert "google_calendar_preview_missing" in capsys.readouterr().out
