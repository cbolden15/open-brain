from __future__ import annotations

import json
import stat
from http.client import HTTPMessage
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import open_brain_connectors.runtime.source_cli as source_cli
from open_brain_connectors.runtime.source_cli import run_cli


def test_source_cli_catalog_exposes_registered_public_onboarding(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(("catalog",)) == 0
    payload = _json(capsys)

    assert payload["status"] == "ok"
    connectors = cast(list[dict[str, object]], payload["connectors"])
    assert connectors == [
        {
            "auth_mode": "device_flow",
            "connector_name": "github",
            "content_types": ["comment", "issue", "pull_request"],
            "display_name": "GitHub",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["repository"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "gitlab",
            "content_types": ["comment", "issue", "merge_request"],
            "display_name": "GitLab",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["project"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "gmail",
            "content_types": ["mail_message"],
            "display_name": "Gmail",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["mail_label"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "google_drive",
            "content_types": ["drive_file"],
            "display_name": "Google Drive",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["drive_file"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "jira",
            "content_types": ["comment", "issue"],
            "display_name": "Jira",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["project"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "microsoft_mail",
            "content_types": ["mail_message"],
            "display_name": "Microsoft 365 Mail",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["mail_folder"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "slack",
            "content_types": ["message", "thread_reply"],
            "display_name": "Slack",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["channel"],
            "schema_version": 1,
        },
    ]


def test_source_cli_requires_approved_github_public_client_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "start-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {
        "error": {"code": "github public app registration required"},
        "status": "failed",
    }


def test_source_cli_starts_github_public_device_flow_without_printing_device_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "device_code": "github-device-secret-material",
                    "expires_in": 900,
                    "interval": 5,
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                }
            ).encode("utf-8")

    observed: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: int) -> _Response:
        observed["request"] = request
        observed["timeout"] = timeout
        return _Response()

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "start-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "needs_user_verification"
    assert payload["device_code_ref"] != "github-device-secret-material"
    assert payload["verification_uri"] == "https://github.com/login/device"
    assert "github-device-secret-material" not in repr(payload)
    assert observed["timeout"] == 15
    request = cast(Request, observed["request"])
    assert request.full_url == "https://github.com/login/device/code"
    assert request.data == b"client_id=Iv1.fixturepublicclient"
    session_files = list((tmp_path / "sessions").glob("github-device-flow-*.json"))
    assert len(session_files) == 1
    assert stat.S_IMODE(session_files[0].stat().st_mode) == 0o600
    persisted = json.loads(session_files[0].read_text(encoding="utf-8"))
    assert persisted == {
        "app_type": "github_app",
        "device_code": "github-device-secret-material",
        "permission_model": "github_app_permissions",
        "schema_version": 1,
        "scope": "",
    }


def test_source_cli_completes_github_device_flow_without_printing_tokens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_device_session(
        tmp_path / "sessions",
        "0123456789abcdef0123456789abcdef",
        "github-device-secret-material",
    )

    observed: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: int) -> _TokenResponse:
        observed["request"] = request
        observed["timeout"] = timeout
        return _TokenResponse(
            {
                "access_token": "ghu_fixture_user_access_token",
                "expires_in": 28_800,
                "refresh_token": "ghr_fixture_refresh_token",
                "refresh_token_expires_in": 15_897_600,
                "scope": "",
                "token_type": "bearer",
            }
        )

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "complete-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
                "--device-code-ref",
                "session:github-device-flow/0123456789abcdef0123456789abcdef",
                "--credential-dir",
                str(tmp_path / "credentials"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "authenticated"
    assert str(payload["credential_ref"]).startswith("session:github-user-token/")
    assert payload["scope"] == ""
    assert "ghu_" not in repr(payload)
    assert "ghr_" not in repr(payload)
    request = cast(Request, observed["request"])
    assert request.full_url == "https://github.com/login/oauth/access_token"
    assert b"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in cast(
        bytes,
        request.data,
    )
    assert b"scope" not in cast(bytes, request.data)
    credential_files = list((tmp_path / "credentials").glob("github-user-token-*.json"))
    assert len(credential_files) == 1
    assert stat.S_IMODE(credential_files[0].stat().st_mode) == 0o600
    persisted = json.loads(credential_files[0].read_text(encoding="utf-8"))
    assert persisted["access_token"] == "ghu_fixture_user_access_token"
    assert persisted["refresh_token"] == "ghr_fixture_refresh_token"
    assert persisted["scope"] == ""


@pytest.mark.parametrize(
    ("error", "status"),
    (
        ("authorization_pending", "authorization_pending"),
        ("slow_down", "slow_down"),
        ("expired_token", "needs_sign_in"),
        ("access_denied", "needs_sign_in"),
    ),
)
def test_source_cli_device_flow_completion_models_polling_and_reauth_states(
    error: str,
    status: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_device_session(
        tmp_path / "sessions",
        "0123456789abcdef0123456789abcdef",
        "github-device-secret-material",
    )

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        assert timeout == 15
        return _TokenResponse({"error": error})

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "complete-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
                "--device-code-ref",
                "session:github-device-flow/0123456789abcdef0123456789abcdef",
                "--credential-dir",
                str(tmp_path / "credentials"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == status
    assert "github-device-secret-material" not in repr(payload)


def test_source_cli_refreshes_github_user_token_and_rotates_private_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_ref = _write_token_session(tmp_path / "credentials")

    def _urlopen(request: object, *, timeout: int) -> _TokenResponse:
        assert timeout == 15
        request_data = cast(bytes, cast(Request, request).data)
        assert b"grant_type=refresh_token" in request_data
        return _TokenResponse(
            {
                "access_token": "ghu_fixture_rotated_user_access_token",
                "expires_in": 28_800,
                "refresh_token": "ghr_fixture_rotated_refresh_token",
                "refresh_token_expires_in": 15_897_600,
                "scope": "",
                "token_type": "bearer",
            }
        )

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "refresh-token",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                first_ref,
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "authenticated"
    assert payload["credential_ref"] != first_ref
    assert not _token_session_path(tmp_path / "credentials", first_ref).exists()
    assert "ghu_" not in repr(payload)
    assert "ghr_" not in repr(payload)


def test_source_cli_bad_refresh_token_requires_sign_in_and_removes_local_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        assert timeout == 15
        return _TokenResponse({"error": "bad_refresh_token"})

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "refresh-token",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "needs_reauthentication": True,
        "schema_version": 1,
        "status": "needs_sign_in",
    }
    assert not _token_session_path(tmp_path / "credentials", credential_ref).exists()


def test_source_cli_auth_status_and_local_revocation_are_metadata_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")

    assert (
        run_cli(
            (
                "github",
                "auth-status",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    status_payload = _json(capsys)
    assert status_payload["status"] == "ok"
    assert status_payload["credential_ref"] == credential_ref
    assert status_payload["refresh_available"] is True
    assert "ghu_" not in repr(status_payload)

    assert (
        run_cli(
            (
                "github",
                "mark-revoked",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    revoke_payload = _json(capsys)

    assert revoke_payload["status"] == "needs_sign_in"
    assert not _token_session_path(tmp_path / "credentials", credential_ref).exists()

    assert (
        run_cli(
            (
                "github",
                "auth-status",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    revoked_status_payload = _json(capsys)
    assert revoked_status_payload == {
        "credential_ref": credential_ref,
        "needs_reauthentication": True,
        "refresh_available": False,
        "schema_version": 1,
        "status": "needs_sign_in",
    }


def test_source_cli_auth_status_reports_refresh_before_reauthentication(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    refreshable_ref = _write_token_session(
        tmp_path / "refreshable",
        expires_at_epoch=1,
        refresh_expires_at_epoch=5_000_000_000,
    )
    assert (
        run_cli(
            (
                "github",
                "auth-status",
                "--credential-dir",
                str(tmp_path / "refreshable"),
                "--credential-ref",
                refreshable_ref,
            )
        )
        == 0
    )
    refreshable = _json(capsys)
    assert refreshable["status"] == "needs_refresh"
    assert refreshable["needs_reauthentication"] is False
    assert refreshable["refresh_available"] is True

    expired_ref = _write_token_session(
        tmp_path / "expired",
        expires_at_epoch=1,
        refresh_expires_at_epoch=1,
    )
    assert (
        run_cli(
            (
                "github",
                "auth-status",
                "--credential-dir",
                str(tmp_path / "expired"),
                "--credential-ref",
                expired_ref,
            )
        )
        == 0
    )
    expired = _json(capsys)
    assert expired["status"] == "needs_sign_in"
    assert expired["needs_reauthentication"] is True
    assert expired["refresh_available"] is False


def test_source_cli_rejects_legacy_oauth_scope_for_github_app_device_flow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")

    assert (
        run_cli(
            (
                "github",
                "start-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
                "--scope",
                "public_repo",
            )
        )
        == 2
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid_arguments"}, "status": "failed"}
    assert "public_repo" not in repr(payload)


@pytest.mark.parametrize(
    "device_code_ref",
    (
        "ghp_plain_token_is_not_a_reference",
        "session:github-device-flow/ghp_plain_token_is_not_a_reference",
        "session:github-device-flow/github_pat_plain_token_is_not_a_reference",
        "session:github-device-flow/sk_live_plain_token_is_not_a_reference",
        "session:github-device-flow/raw-device-code",
    ),
)
def test_source_cli_auth_session_rejects_secret_shaped_device_refs(
    device_code_ref: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "auth-session",
                "--device-code-ref",
                device_code_ref,
                "--user-code",
                "ABCD-1234",
                "--verification-uri",
                "https://github.com/login/device",
                "--expires-in-seconds",
                "900",
                "--interval-seconds",
                "5",
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid github auth session"}, "status": "failed"}
    assert "ghp_" not in repr(payload)
    assert "github_pat_" not in repr(payload)


def test_source_cli_models_github_device_session_without_token_material(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "auth-session",
                "--device-code-ref",
                "session:github-device-flow/0123456789abcdef0123456789abcdef",
                "--user-code",
                "ABCD-1234",
                "--verification-uri",
                "https://github.com/login/device",
                "--expires-in-seconds",
                "900",
                "--interval-seconds",
                "5",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "needs_user_verification"
    assert payload["verification_uri"] == "https://github.com/login/device"
    assert "ghp_" not in repr(payload)


def test_source_cli_connection_ref_cannot_claim_live_onboarding_proof(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "connection-ref",
                "--connection-id",
                "account:fixture",
                "--account-login",
                "fixture",
                "--credential-ref",
                "keychain:open-brain/github/fixture",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "account_login": "fixture",
        "connection_id": "account:fixture",
        "credential_ref": "keychain:open-brain/github/fixture",
        "onboarding_mode": "device_flow",
        "public_onboarding_available": True,
        "schema_version": 1,
        "status": "ok",
    }
    assert "proof" not in repr(payload).lower()


@pytest.mark.parametrize(
    "credential_ref",
    (
        "session:ghu_plain_user_token_is_not_a_reference",
        "session:ghr_plain_refresh_token_is_not_a_reference",
        "session:github_pat_plain_token_is_not_a_reference",
        "keychain:open-brain/github/ghp_plain_token_is_not_a_reference",
    ),
)
def test_source_cli_connection_ref_rejects_secret_shaped_credential_refs(
    credential_ref: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "connection-ref",
                "--connection-id",
                "account:fixture",
                "--account-login",
                "fixture",
                "--credential-ref",
                credential_ref,
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid github connection"}, "status": "failed"}
    assert "ghu_" not in repr(payload)
    assert "github_pat_" not in repr(payload)


def test_source_cli_rejects_user_supplied_onboarding_proof_claim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "connection-ref",
                "--connection-id",
                "account:fixture",
                "--account-login",
                "fixture",
                "--credential-ref",
                "keychain:open-brain/github/fixture",
                "--public-onboarding-proof",
            )
        )
        == 2
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid_arguments"}, "status": "failed"}
    assert "fixture" not in repr(payload)


def test_source_cli_previews_github_repository_from_host_payload_without_bodies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "github-page.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "number": 42,
                    "html_url": "https://github.com/fixture/project/issues/42",
                    "title": "Fixture issue",
                    "body": "Synthetic issue body that must stay out of preview JSON.",
                    "updated_at": "2026-09-14T12:00:00Z",
                },
                {
                    "id": 987,
                    "issue_url": "https://api.github.com/repos/fixture/project/issues/42",
                    "html_url": (
                        "https://github.com/fixture/project/issues/42#issuecomment-987"
                    ),
                    "body": "Synthetic comment body that must stay out of preview JSON.",
                    "updated_at": "2026-09-14T12:05:00Z",
                },
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "github",
                "preview-repository",
                "--connection-id",
                "account:fixture",
                "--owner",
                "fixture",
                "--repository",
                "project",
                "--input",
                str(input_path),
                "--next-cursor",
                "cursor:page2",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "cursor:page2"
    assert payload["resource_id"] == "repo:fixture/project"
    content_types = [
        record["content_type"] for record in cast(list[dict[str, object]], payload["records"])
    ]
    assert content_types == [
        "issue",
        "comment",
    ]
    assert "must stay out" not in repr(payload)


def test_source_cli_lists_github_repositories_without_private_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "repositories.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "owner": {"login": "fixture"},
                    "name": "project",
                    "html_url": "https://github.com/fixture/project",
                    "private": True,
                    "description": "Private fixture description must stay local.",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "github",
                "list-repositories",
                "--input",
                str(input_path),
                "--next-cursor",
                "cursor:repositories2",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "cursor:repositories2"
    assert payload["repositories"] == [
        {
            "html_url": "https://github.com/fixture/project",
            "name": "project",
            "owner": "fixture",
            "selected": False,
        }
    ]
    assert "Private fixture description" not in repr(payload)


def test_source_cli_discovers_github_app_installations_with_private_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")
    observed: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: int) -> _TokenResponse:
        observed["request"] = request
        observed["timeout"] = timeout
        return _TokenResponse(
            {
                "installations": [
                    {
                        "account": {"login": "cbolden15"},
                        "id": 161812976,
                        "permissions": {
                            "issues": "read",
                            "metadata": "read",
                            "pull_requests": "read",
                        },
                        "repository_selection": "selected",
                    }
                ],
                "total_count": 1,
            },
            link_header=(
                '<https://api.github.com/user/installations?per_page=50&page=2>; rel="next"'
            ),
        )

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-installations",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "installations": [
            {
                "account_login": "cbolden15",
                "id": 161812976,
                "permissions": {
                    "issues": "read",
                    "metadata": "read",
                    "pull_requests": "read",
                },
                "repository_selection": "selected",
            }
        ],
        "next_cursor": "2",
        "schema_version": 1,
        "status": "ready",
    }
    request = cast(Request, observed["request"])
    assert request.full_url == "https://api.github.com/user/installations?per_page=50"
    assert request.get_header("Authorization") == "Bearer ghu_fixture_user_access_token"
    assert "ghu_" not in repr(payload)


def test_source_cli_rejects_broader_github_app_installation_permissions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        assert timeout == 15
        return _TokenResponse(
            {
                "installations": [
                    {
                        "account": {"login": "cbolden15"},
                        "id": 161812976,
                        "permissions": {
                            "issues": "write",
                            "metadata": "read",
                            "pull_requests": "read",
                        },
                        "repository_selection": "selected",
                    }
                ],
                "total_count": 1,
            }
        )

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-installations",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {
        "error": {"code": "invalid github installation list"},
        "status": "failed",
    }


def test_source_cli_discovers_selected_private_repositories_without_private_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")
    observed: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: int) -> _TokenResponse:
        observed["request"] = request
        observed["timeout"] = timeout
        return _TokenResponse(
            {
                "repositories": [
                    {
                        "description": "Synthetic private description must stay local.",
                        "html_url": "https://github.com/cbolden15/open-brain-fixture",
                        "name": "open-brain-fixture",
                        "owner": {"login": "cbolden15"},
                        "private": True,
                    }
                ],
                "total_count": 1,
            },
            link_header=(
                "<https://api.github.com/user/installations/161812976/repositories?"
                'per_page=50&page=2>; rel="next"'
            ),
        )

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-selected-repositories",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
                "--installation-id",
                "161812976",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "2"
    assert payload["repositories"] == [
        {
            "html_url": "https://github.com/cbolden15/open-brain-fixture",
            "name": "open-brain-fixture",
            "owner": "cbolden15",
            "selected": True,
        }
    ]
    request = cast(Request, observed["request"])
    assert request.full_url == (
        "https://api.github.com/user/installations/161812976/repositories?per_page=50"
    )
    assert "Synthetic private description" not in repr(payload)
    assert "ghu_" not in repr(payload)


def test_source_cli_live_discovery_maps_revocation_and_rate_limit_states(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")
    calls = 0

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        nonlocal calls
        calls += 1
        headers = HTTPMessage()
        if calls == 1:
            raise HTTPError("", 401, "Unauthorized", headers, None)
        headers.add_header("Retry-After", "30")
        raise HTTPError("", 403, "Forbidden", headers, None)

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-installations",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    revoked = _json(capsys)
    assert revoked == {
        "needs_reauthentication": True,
        "schema_version": 1,
        "status": "needs_sign_in",
    }

    assert (
        run_cli(
            (
                "github",
                "list-installations",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    limited = _json(capsys)
    assert limited == {
        "retry_after_seconds": 30,
        "schema_version": 1,
        "status": "rate_limited",
    }


def test_source_cli_selected_repository_404_reports_access_not_reauth(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        raise HTTPError("", 404, "Not Found", HTTPMessage(), None)

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-selected-repositories",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
                "--installation-id",
                "161812976",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {"schema_version": 1, "status": "not_allowed"}


def test_source_cli_reports_selected_repository_checkpoint_without_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "checkpoint",
                "--connection-id",
                "account:fixture",
                "--owner",
                "fixture",
                "--repository",
                "project",
                "--checkpoint-dir",
                str(tmp_path / "checkpoints"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ok"
    assert payload["resource_id"] == "repo:fixture/project"
    assert payload["committed_delivery_ids"] == []
    assert "Synthetic" not in repr(payload)


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return cast(dict[str, object], json.loads(capsys.readouterr().out))


class _TokenResponse:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        link_header: str | None = None,
    ) -> None:
        self._payload = payload
        self.headers = HTTPMessage()
        if link_header is not None:
            self.headers.add_header("Link", link_header)

    def __enter__(self) -> _TokenResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _write_device_session(session_dir: Path, digest: str, device_code: str) -> None:
    session_dir.mkdir(parents=True)
    path = session_dir / f"github-device-flow-{digest}.json"
    path.write_text(
        json.dumps(
            {
                "app_type": "github_app",
                "device_code": device_code,
                "permission_model": "github_app_permissions",
                "schema_version": 1,
                "scope": "",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_token_session(
    credential_dir: Path,
    *,
    expires_at_epoch: int = 4_000_000_000,
    refresh_expires_at_epoch: int = 5_000_000_000,
) -> str:
    credential_dir.mkdir(parents=True)
    digest = "0123456789abcdef0123456789abcdef"
    path = credential_dir / f"github-user-token-{digest}.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "ghu_fixture_user_access_token",
                "account_login": None,
                "app_type": "github_app",
                "expires_at_epoch": expires_at_epoch,
                "permission_model": "github_app_permissions",
                "refresh_expires_at_epoch": refresh_expires_at_epoch,
                "refresh_token": "ghr_fixture_refresh_token",
                "schema_version": 1,
                "scope": "",
                "token_type": "bearer",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return f"session:github-user-token/{digest}"


def _token_session_path(credential_dir: Path, credential_ref: str) -> Path:
    return credential_dir / f"github-user-token-{credential_ref.rsplit('/', 1)[1]}.json"
