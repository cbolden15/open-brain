from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.engine import TextPayload, acquire_daemon_authority, open_local_engine

from open_brain.integrations.phase1_ui import (
    BrowserSessionStore,
    Phase1UiHandler,
    Phase1UiRequest,
)
from open_brain.integrations.ports import PageDocument, PageReadRequest
from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.appliance_application import ApplianceApplication
from open_brain.services.appliance_auth import (
    APPLIANCE_SESSION_COOKIE,
    SECURE_APPLIANCE_SESSION_COOKIE,
    ApplianceBrowserSessionStore,
    derive_appliance_credential,
)
from open_brain.services.appliance_daemon import ApplianceDaemon
from open_brain.services.appliance_init import APPLIANCE_OWNER_CREDENTIAL, initialize_appliance
from open_brain.services.appliance_scheduler import (
    APPLIANCE_SCHEDULER_DIRECTORY,
    ApplianceJobResult,
    ApplianceScheduler,
)
from open_brain.services.appliance_status import read_appliance_status
from open_brain.services.runtime import appliance_http_configuration_from_environment

_PRIVATE_BIND_HOST = ".".join(("192", "168", "1", "10"))


class _SyntheticPageReader:
    def read(self, request: PageReadRequest) -> PageDocument | None:
        del request
        return None


def test_appliance_browser_sessions_are_purpose_bound_rotating_bounded_and_expiring() -> None:
    seed = "synthetic-owner-seed"
    browser = derive_appliance_credential(seed, purpose="browser-bootstrap")
    intake = derive_appliance_credential(seed, purpose="intake-bearer")
    now = datetime(2026, 9, 1, tzinfo=UTC)
    clock = {"now": now}
    store = ApplianceBrowserSessionStore(
        expected_bootstrap_credential=browser,
        now=lambda: clock["now"],
        session_ttl=timedelta(minutes=30),
        maximum_sessions=2,
    )

    first = store.create_session(browser)
    second = store.create_session(browser)

    assert browser != intake
    assert first.session_id != second.session_id
    assert first.csrf_token != second.csrf_token
    assert store.authenticate(
        cookie_header=f"{APPLIANCE_SESSION_COOKIE}={first.session_id}",
        csrf_token=first.csrf_token,
    )

    third = store.create_session(browser)

    assert not store.authenticate(
        cookie_header=f"{APPLIANCE_SESSION_COOKIE}={first.session_id}",
        csrf_token=first.csrf_token,
    )
    assert store.authenticate(
        cookie_header=f"{APPLIANCE_SESSION_COOKIE}={third.session_id}",
        csrf_token=third.csrf_token,
    )

    store.logout(cookie_header=f"{APPLIANCE_SESSION_COOKIE}={second.session_id}")
    assert not store.authenticate(
        cookie_header=f"{APPLIANCE_SESSION_COOKIE}={second.session_id}",
        csrf_token=second.csrf_token,
    )

    clock["now"] = now + timedelta(minutes=31)
    assert not store.authenticate(
        cookie_header=f"{APPLIANCE_SESSION_COOKIE}={third.session_id}",
        csrf_token=third.csrf_token,
    )


def test_appliance_browser_sessions_refuse_invalid_bootstrap_credentials() -> None:
    store = ApplianceBrowserSessionStore(expected_bootstrap_credential="bootstrap-token")

    with pytest.raises(ValueError, match="invalid browser bootstrap credential"):
        store.create_session("wrong-token")


def test_appliance_browser_cookie_headers_are_host_only_and_secure_only_for_https() -> None:
    session = ApplianceBrowserSessionStore(
        expected_bootstrap_credential="bootstrap-token"
    ).create_session("bootstrap-token")
    secure_session = ApplianceBrowserSessionStore(
        expected_bootstrap_credential="bootstrap-token",
        secure_cookie=True,
    ).create_session("bootstrap-token")

    loopback_cookie = ApplianceBrowserSessionStore(
        expected_bootstrap_credential="bootstrap-token"
    ).set_cookie_header(session)
    secure_cookie = ApplianceBrowserSessionStore(
        expected_bootstrap_credential="bootstrap-token",
        secure_cookie=True,
    ).set_cookie_header(secure_session)

    assert loopback_cookie.startswith(f"{APPLIANCE_SESSION_COOKIE}=")
    assert "__Host-" not in loopback_cookie
    assert "Domain=" not in loopback_cookie
    assert "HttpOnly" in loopback_cookie
    assert "SameSite=Strict" in loopback_cookie
    assert "Secure" not in loopback_cookie

    assert secure_cookie.startswith(f"{SECURE_APPLIANCE_SESSION_COOKIE}=")
    assert "Domain=" not in secure_cookie
    assert "HttpOnly" in secure_cookie
    assert "SameSite=Strict" in secure_cookie
    assert "Secure" in secure_cookie


def test_phase1_ui_requires_browser_session_origin_and_csrf_for_browser_routes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root)).phase1
    browser = derive_appliance_credential("synthetic-owner-seed", purpose="browser-bootstrap")
    sessions = ApplianceBrowserSessionStore(expected_bootstrap_credential=browser)
    handler = Phase1UiHandler(
        tasks=tasks,
        browser_sessions=cast(BrowserSessionStore, sessions),
        allowed_origin="http://127.0.0.1:8788",
        page_reader=_SyntheticPageReader(),
        status_reader=lambda: read_appliance_status(root).to_dict(),
        history_reader=lambda _limit: {"runs": [], "status": "ok", "truncated": False},
    )
    login_body = canonical_json_bytes({"credential": browser})

    wrong_origin = handler.handle(
        Phase1UiRequest(
            "POST",
            "/auth/login",
            (("Origin", "http://localhost:8788"),),
            login_body,
        )
    )
    login = handler.handle(
        Phase1UiRequest(
            "POST",
            "/auth/login",
            (("Origin", "http://127.0.0.1:8788"),),
            login_body,
        )
    )
    login_payload = json.loads(login.body)
    cookie = dict(login.headers)["Set-Cookie"].split(";", 1)[0]

    unauthorized = handler.handle(Phase1UiRequest("GET", "/api/status", (), b""))
    wrong_csrf = handler.handle(
        Phase1UiRequest(
            "POST",
            "/api/captures/quick",
            (
                ("Cookie", cookie),
                ("Origin", "http://127.0.0.1:8788"),
                ("X-CSRF-Token", "wrong-token"),
            ),
            canonical_json_bytes(
                {"delivery_id": "browser.ui.capture", "text": "Should not be accepted"}
            ),
        )
    )
    accepted = handler.handle(
        Phase1UiRequest(
            "POST",
            "/api/captures/quick",
            (
                ("Cookie", cookie),
                ("Origin", "http://127.0.0.1:8788"),
                ("X-CSRF-Token", login_payload["csrf_token"]),
            ),
            canonical_json_bytes(
                {"delivery_id": "browser.ui.capture", "text": "Accepted browser capture"}
            ),
        )
    )
    bearer = handler.handle(
        Phase1UiRequest(
            "GET",
            "/api/status",
            (("Authorization", "Bearer not-browser-auth"),),
            b"",
        )
    )

    assert wrong_origin.status == 403
    assert login.status == 200
    assert dict(login.headers)["Set-Cookie"].startswith(f"{APPLIANCE_SESSION_COOKIE}=")
    assert unauthorized.status == 401
    assert wrong_csrf.status == 401
    assert accepted.status == 200
    assert bearer.status == 401
    assert tasks.inbox.list()[0].capture_id == json.loads(accepted.body)["capture_id"]


def test_phase1_ui_logout_invalidates_the_browser_session(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = open_local_engine(compile_single_user_local(root))
    space = engine.inbox.create_space("Pages", delivery_id="page-space")
    capture = engine.capture.accept(
        TextPayload("Page content for browser UI"),
        delivery_id="page-capture",
        space_id=space.space_id,
    )
    browser = derive_appliance_credential("synthetic-owner-seed", purpose="browser-bootstrap")
    sessions = ApplianceBrowserSessionStore(expected_bootstrap_credential=browser)
    handler = Phase1UiHandler(
        tasks=engine.phase1,
        browser_sessions=cast(BrowserSessionStore, sessions),
        allowed_origin="http://127.0.0.1:8788",
        page_reader=_SyntheticPageReader(),
        status_reader=lambda: read_appliance_status(root).to_dict(),
        history_reader=lambda _limit: {"runs": [], "status": "ok", "truncated": False},
    )
    login = handler.handle(
        Phase1UiRequest(
            "POST",
            "/auth/login",
            (("Origin", "http://127.0.0.1:8788"),),
            canonical_json_bytes({"credential": browser}),
        )
    )
    payload = json.loads(login.body)
    cookie = dict(login.headers)["Set-Cookie"].split(";", 1)[0]

    logout = handler.handle(
        Phase1UiRequest(
            "POST",
            "/auth/logout",
            (
                ("Cookie", cookie),
                ("Origin", "http://127.0.0.1:8788"),
                ("X-CSRF-Token", payload["csrf_token"]),
            ),
            b"",
        )
    )
    after = handler.handle(
        Phase1UiRequest(
            "GET",
            "/api/inbox",
            (("Cookie", cookie),),
            b"",
        )
    )

    assert capture.capture_id.startswith("capture_")
    assert logout.status == 200
    assert dict(logout.headers)["Set-Cookie"].endswith("SameSite=Strict")
    assert after.status == 401


def test_appliance_status_keeps_all_doctor_checks_and_sanitizes_scheduler_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    profile = compile_single_user_local(root)
    scheduler = ApplianceScheduler(
        profile,
        handlers={"engine-recover": lambda _context: ApplianceJobResult.completed()},
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    scheduler.run_due(now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
    state_path = root / APPLIANCE_SCHEDULER_DIRECTORY / "state.json"
    state = scheduler.read_state()
    jobs = cast(dict[str, dict[str, object]], state["jobs"])
    jobs["engine-recover"]["active_attempt"] = "credential=secret"
    jobs["engine-recover"]["last_status"] = "/private/runtime/canary"
    state_path.write_bytes(canonical_json_bytes(state))

    status = read_appliance_status(root).to_dict()
    doctor = cast(dict[str, object], status["doctor"])
    checks = cast(list[dict[str, str]], doctor["checks"])
    check_names = [check["check"] for check in checks]
    rendered = json.dumps(status, sort_keys=True)

    assert check_names == [
        "schema",
        "index",
        "scheduler",
        "daemon_authority",
        "locks",
        "backup",
        "export",
        "http_access",
        "last_successful_run",
    ]
    assert status["last_successful_run"] is not None
    assert cast(dict[str, object], status["scheduler"]) == {
        "active_count": 0,
        "due_count": 0,
        "jobs": [],
        "queue_age_seconds": 0,
        "state": "invalid",
    }
    assert "credential=secret" not in rendered
    assert "/private/runtime/canary" not in rendered


def test_appliance_daemon_uses_secure_cookie_for_external_https_origin(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root)
    profile = open_existing_single_user_local(root)
    browser = derive_appliance_credential(
        (root / APPLIANCE_OWNER_CREDENTIAL).read_text(encoding="utf-8").strip(),
        purpose="browser-bootstrap",
    )
    login_body = canonical_json_bytes({"credential": browser})
    configuration = appliance_http_configuration_from_environment(
        {
            "OPEN_BRAIN_UI_BIND": _PRIVATE_BIND_HOST,
            "OPEN_BRAIN_UI_PORT": "8788",
            "OPEN_BRAIN_UI_ALLOW_PRIVATE": "true",
            "OPEN_BRAIN_UI_EXTERNAL_TLS_TERMINATION": "true",
            "OPEN_BRAIN_UI_EXTERNAL_ORIGIN": "https://brain.example.test",
        }
    )

    daemon = ApplianceDaemon(root)
    with acquire_daemon_authority(profile) as authority:
        daemon._application = ApplianceApplication.open_mutating(root, authority=authority)
        daemon._stopping = False
        login = daemon._compose_http_service(configuration).dispatch(
            method="POST",
            path="/auth/login",
            headers=(
                ("Content-Length", str(len(login_body))),
                ("Content-Type", "application/json"),
                ("Origin", configuration.allowed_origin),
            ),
            body_reader=lambda _maximum_bytes, _timeout_seconds: login_body,
        )

    assert login.status == 200
    cookie = dict(login.headers)["Set-Cookie"]
    assert cookie.startswith(f"{SECURE_APPLIANCE_SESSION_COOKIE}=")
    assert "Secure" in cookie
    status = read_appliance_status(
        root,
        bind=configuration.bind,
        allowed_origin=configuration.allowed_origin,
        external_encryption_terminated=configuration.external_encryption_terminated,
    ).to_dict()
    http = cast(
        dict[str, object],
        cast(dict[str, object], status["configuration"])["http"],
    )
    assert http["allowed_origin"] == "https://brain.example.test"
