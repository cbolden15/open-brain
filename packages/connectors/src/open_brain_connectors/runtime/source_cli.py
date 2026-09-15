"""JSON command surface for host-mediated source onboarding and preview."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import NoReturn, cast
from urllib import parse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.github import (
    GitHubDeviceAuthSession,
    GitHubRepositoryCheckpointStore,
    GitHubSourceAdapter,
    GitHubUserTokenStore,
)
from open_brain_connectors.runtime.source_registry import (
    SourceCatalog,
    github_source_descriptor,
    gitlab_source_descriptor,
    gmail_source_descriptor,
    google_drive_source_descriptor,
    jira_source_descriptor,
    microsoft_mail_source_descriptor,
    slack_source_descriptor,
)

__all__ = ["run_cli"]

_DEVICE_SESSION_REF = re.compile(r"session:github-device-flow/[0-9a-f]{32}")


class _UsageError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        raise _UsageError("invalid command")


def run_cli(argv: tuple[str, ...] | list[str] | None = None) -> int:
    """Run one bounded connector command and print metadata-only JSON."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        parsed = _parser().parse_args(arguments)
        if parsed.command is None:
            raise _UsageError("invalid command")
        payload = _run(parsed)
    except _UsageError:
        _write_json({"error": {"code": "invalid_arguments"}, "status": "failed"})
        return 2
    except ConnectorContractError as error:
        _write_json({"error": {"code": str(error)}, "status": "failed"})
        return 78
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        _write_json({"error": {"code": "source_file_unavailable"}, "status": "failed"})
        return 78
    _write_json(payload)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="open-brain-source",
        description="Optional Open Brain source connector metadata and preview commands.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser("catalog", help="List available source connector descriptors.")

    github = subparsers.add_parser("github", help="GitHub source onboarding and preview.")
    github_subparsers = github.add_subparsers(dest="github_command", required=True)

    start_auth = github_subparsers.add_parser("start-device-flow")
    start_auth.add_argument("--client-id-env", required=True)
    start_auth.add_argument("--session-dir", required=True)

    complete_auth = github_subparsers.add_parser("complete-device-flow")
    complete_auth.add_argument("--client-id-env", required=True)
    complete_auth.add_argument("--session-dir", required=True)
    complete_auth.add_argument("--device-code-ref", required=True)
    complete_auth.add_argument("--credential-dir", required=True)

    refresh_auth = github_subparsers.add_parser("refresh-token")
    refresh_auth.add_argument("--client-id-env", required=True)
    refresh_auth.add_argument("--credential-dir", required=True)
    refresh_auth.add_argument("--credential-ref", required=True)

    auth_status = github_subparsers.add_parser("auth-status")
    auth_status.add_argument("--credential-dir", required=True)
    auth_status.add_argument("--credential-ref", required=True)

    revoke_auth = github_subparsers.add_parser("mark-revoked")
    revoke_auth.add_argument("--credential-dir", required=True)
    revoke_auth.add_argument("--credential-ref", required=True)

    auth = github_subparsers.add_parser("auth-session")
    auth.add_argument("--device-code-ref", required=True)
    auth.add_argument("--user-code", required=True)
    auth.add_argument("--verification-uri", required=True)
    auth.add_argument("--expires-in-seconds", required=True, type=int)
    auth.add_argument("--interval-seconds", required=True, type=int)

    connection = github_subparsers.add_parser("connection-ref")
    connection.add_argument("--connection-id", required=True)
    connection.add_argument("--account-login", required=True)
    connection.add_argument("--credential-ref", required=True)

    selection = github_subparsers.add_parser("select-repository")
    _add_repository_args(selection)

    listing = github_subparsers.add_parser("list-repositories")
    listing.add_argument("--input", required=True)
    listing.add_argument("--next-cursor")

    live_installations = github_subparsers.add_parser("list-installations")
    live_installations.add_argument("--credential-dir", required=True)
    live_installations.add_argument("--credential-ref", required=True)
    live_installations.add_argument("--next-cursor")

    live_listing = github_subparsers.add_parser("list-selected-repositories")
    live_listing.add_argument("--credential-dir", required=True)
    live_listing.add_argument("--credential-ref", required=True)
    live_listing.add_argument("--installation-id", required=True, type=int)
    live_listing.add_argument("--next-cursor")

    preview = github_subparsers.add_parser("preview-repository")
    _add_repository_args(preview)
    preview.add_argument("--input", required=True)
    preview.add_argument("--next-cursor")

    checkpoint = github_subparsers.add_parser("checkpoint")
    _add_repository_args(checkpoint)
    checkpoint.add_argument("--checkpoint-dir", required=True)
    return parser


def _add_repository_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repository", required=True)


def _run(parsed: argparse.Namespace) -> dict[str, object]:
    if parsed.command == "catalog":
        catalog = SourceCatalog(
            (
                github_source_descriptor(),
                gitlab_source_descriptor(),
                gmail_source_descriptor(),
                google_drive_source_descriptor(),
                jira_source_descriptor(),
                microsoft_mail_source_descriptor(),
                slack_source_descriptor(),
            )
        )
        return {
            "connectors": [descriptor.to_dict() for descriptor in catalog.list()],
            "schema_version": 1,
            "status": "ok",
        }
    if parsed.command != "github":
        raise _UsageError("invalid command")
    adapter = GitHubSourceAdapter()
    github_command = cast(str, parsed.github_command)
    if github_command == "start-device-flow":
        session = _start_github_device_flow(
            client_id_env=cast(str, parsed.client_id_env),
            session_dir=Path(cast(str, parsed.session_dir)),
            adapter=adapter,
        )
        return {
            "device_code_ref": session.device_code_ref,
            "expires_in_seconds": session.expires_in_seconds,
            "interval_seconds": session.interval_seconds,
            "schema_version": 1,
            "status": "needs_user_verification",
            "user_code": session.user_code,
            "verification_uri": session.verification_uri,
        }
    if github_command == "auth-session":
        session = adapter.device_auth_session(
            device_code_ref=_session_device_code_ref(cast(str, parsed.device_code_ref)),
            user_code=cast(str, parsed.user_code),
            verification_uri=cast(str, parsed.verification_uri),
            expires_in_seconds=cast(int, parsed.expires_in_seconds),
            interval_seconds=cast(int, parsed.interval_seconds),
        )
        return {
            "device_code_ref": session.device_code_ref,
            "expires_in_seconds": session.expires_in_seconds,
            "interval_seconds": session.interval_seconds,
            "schema_version": 1,
            "status": "needs_user_verification",
            "user_code": session.user_code,
            "verification_uri": session.verification_uri,
        }
    if github_command == "complete-device-flow":
        token = _complete_github_device_flow(
            client_id_env=cast(str, parsed.client_id_env),
            session_dir=Path(cast(str, parsed.session_dir)),
            device_code_ref=cast(str, parsed.device_code_ref),
            credential_dir=Path(cast(str, parsed.credential_dir)),
        )
        return token
    if github_command == "refresh-token":
        token = _refresh_github_token(
            client_id_env=cast(str, parsed.client_id_env),
            credential_dir=Path(cast(str, parsed.credential_dir)),
            credential_ref=cast(str, parsed.credential_ref),
        )
        return token
    if github_command == "auth-status":
        token_store = GitHubUserTokenStore(Path(cast(str, parsed.credential_dir)))
        if not token_store.metadata_exists(cast(str, parsed.credential_ref)):
            return {
                "credential_ref": cast(str, parsed.credential_ref),
                "needs_reauthentication": True,
                "refresh_available": False,
                "schema_version": 1,
                "status": "needs_sign_in",
            }
        token_session = token_store.load_metadata(cast(str, parsed.credential_ref))
        needs_reauthentication = token_session.is_expired and not token_session.can_refresh
        status = (
            "needs_sign_in"
            if needs_reauthentication
            else "needs_refresh"
            if token_session.is_expired
            else "ok"
        )
        return {
            "account_login": token_session.account_login,
            "credential_ref": token_session.credential_ref,
            "expires_at_epoch": token_session.expires_at_epoch,
            "needs_reauthentication": needs_reauthentication,
            "refresh_available": token_session.can_refresh,
            "refresh_expires_at_epoch": token_session.refresh_expires_at_epoch,
            "schema_version": 1,
            "scope": token_session.scope,
            "status": status,
            "token_type": token_session.token_type,
        }
    if github_command == "mark-revoked":
        token_store = GitHubUserTokenStore(Path(cast(str, parsed.credential_dir)))
        token_store.revoke_locally(cast(str, parsed.credential_ref))
        return {
            "credential_ref": cast(str, parsed.credential_ref),
            "needs_reauthentication": True,
            "schema_version": 1,
            "status": "needs_sign_in",
        }
    if github_command == "connection-ref":
        connection = adapter.connection_ref(
            connection_id=cast(str, parsed.connection_id),
            account_login=cast(str, parsed.account_login),
            credential_ref=cast(str, parsed.credential_ref),
            public_onboarding_proof=False,
        )
        return {
            "account_login": connection.account_login,
            "connection_id": connection.connection_id,
            "credential_ref": connection.credential_ref,
            "onboarding_mode": "device_flow",
            "public_onboarding_available": True,
            "schema_version": 1,
            "status": "ok",
        }
    if github_command == "select-repository":
        return {
            **_selection(parsed),
            "schema_version": 1,
            "status": "selected",
        }
    if github_command == "list-repositories":
        values = _read_json_list(Path(cast(str, parsed.input)))
        repository_list = adapter.repository_list_page(values, next_cursor=parsed.next_cursor)
        return {**repository_list.to_dict(), "status": repository_list.status.value}
    if github_command == "list-installations":
        return _list_github_app_installations(
            credential_dir=Path(cast(str, parsed.credential_dir)),
            credential_ref=cast(str, parsed.credential_ref),
            next_cursor=parsed.next_cursor,
        )
    if github_command == "list-selected-repositories":
        return _list_selected_github_repositories(
            credential_dir=Path(cast(str, parsed.credential_dir)),
            credential_ref=cast(str, parsed.credential_ref),
            installation_id=cast(int, parsed.installation_id),
            next_cursor=parsed.next_cursor,
            adapter=adapter,
        )
    if github_command == "preview-repository":
        selection = adapter.repository_selection(
            connection_id=cast(str, parsed.connection_id),
            owner=cast(str, parsed.owner),
            repository=cast(str, parsed.repository),
        )
        values = _read_json_list(Path(cast(str, parsed.input)))
        repository_page = adapter.repository_page_from_rest(
            selection,
            values,
            privacy=_public_provider_privacy(),
            next_cursor=parsed.next_cursor,
        )
        if repository_page.preview is None:
            raise ConnectorContractError("invalid github page")
        return {**repository_page.preview.to_dict(), "status": repository_page.status.value}
    if github_command == "checkpoint":
        selection = adapter.repository_selection(
            connection_id=cast(str, parsed.connection_id),
            owner=cast(str, parsed.owner),
            repository=cast(str, parsed.repository),
        )
        checkpoint = GitHubRepositoryCheckpointStore(Path(cast(str, parsed.checkpoint_dir))).load(
            selection
        )
        return {**checkpoint.to_dict(), "status": "ok"}
    raise _UsageError("invalid command")


def _selection(parsed: argparse.Namespace) -> dict[str, object]:
    selection = GitHubSourceAdapter().repository_selection(
        connection_id=cast(str, parsed.connection_id),
        owner=cast(str, parsed.owner),
        repository=cast(str, parsed.repository),
    )
    return {
        "connection_id": selection.connection_id,
        "connector_name": selection.connector_name,
        "resource_id": selection.resource_id,
        "resource_type": selection.resource_type,
    }


def _read_json_list(path: Path) -> Sequence[Mapping[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ConnectorContractError("invalid source input")
    return cast(Sequence[Mapping[str, object]], value)


def _start_github_device_flow(
    *,
    client_id_env: str,
    session_dir: Path,
    adapter: GitHubSourceAdapter,
) -> GitHubDeviceAuthSession:
    client_id = _github_client_id(client_id_env)
    payload = parse.urlencode({"client_id": client_id}).encode("ascii")
    response = urlopen(  # noqa: S310 - fixed GitHub endpoint, no caller URL.
        Request(
            "https://github.com/login/device/code",
            data=payload,
            headers={"Accept": "application/json"},
            method="POST",
        ),
        timeout=15,
    )
    with response:
        decoded = json.loads(response.read().decode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise ConnectorContractError("invalid github auth session")
    device_code = _required_auth_str(decoded.get("device_code"))
    digest = hashlib.sha256(device_code.encode("utf-8")).hexdigest()
    session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    session_path = session_dir / f"github-device-flow-{digest[:32]}.json"
    session_payload = json.dumps(
        {
            "app_type": "github_app",
            "device_code": device_code,
            "permission_model": "github_app_permissions",
            "schema_version": 1,
            "scope": "",
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    with NamedTemporaryFile(
        "w",
        delete=False,
        dir=session_dir,
        encoding="utf-8",
        prefix=f".{session_path.name}.",
        suffix=".tmp",
    ) as handle:
        handle.write(session_payload)
        temp_name = handle.name
    Path(temp_name).chmod(0o600)
    try:
        os.replace(temp_name, session_path)
    except OSError:
        Path(temp_name).unlink(missing_ok=True)
        raise
    session_path.chmod(0o600)
    return adapter.device_auth_session(
        device_code_ref=f"session:github-device-flow/{digest[:32]}",
        user_code=_required_auth_str(decoded.get("user_code")),
        verification_uri=_required_auth_str(decoded.get("verification_uri")),
        expires_in_seconds=_required_auth_int(decoded.get("expires_in")),
        interval_seconds=_required_auth_int(decoded.get("interval")),
    )


def _complete_github_device_flow(
    *,
    client_id_env: str,
    session_dir: Path,
    device_code_ref: str,
    credential_dir: Path,
) -> dict[str, object]:
    session_path = _device_session_path(session_dir, _session_device_code_ref(device_code_ref))
    try:
        decoded_session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConnectorContractError("invalid github auth session") from error
    if not isinstance(decoded_session, Mapping):
        raise ConnectorContractError("invalid github auth session")
    response = _request_github_token(
        client_id_env=client_id_env,
        parameters={
            "device_code": _required_auth_str(decoded_session.get("device_code")),
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    status = _github_auth_error_status(response)
    if status is not None:
        return status
    return _persist_github_token(response, credential_dir=credential_dir)


def _refresh_github_token(
    *,
    client_id_env: str,
    credential_dir: Path,
    credential_ref: str,
) -> dict[str, object]:
    token_store = GitHubUserTokenStore(credential_dir)
    refresh_token = token_store.load_refresh_token(credential_ref)
    response = _request_github_token(
        client_id_env=client_id_env,
        parameters={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    status = _github_auth_error_status(response)
    if status is not None:
        if status["status"] == "needs_sign_in":
            token_store.revoke_locally(credential_ref)
        return status
    token_store.revoke_locally(credential_ref)
    return _persist_github_token(response, credential_dir=credential_dir)


def _list_github_app_installations(
    *,
    credential_dir: Path,
    credential_ref: str,
    next_cursor: str | None,
) -> dict[str, object]:
    response = _request_github_api(
        credential_dir=credential_dir,
        credential_ref=credential_ref,
        path="/user/installations",
        query={"per_page": "50", **({"page": next_cursor} if next_cursor else {})},
    )
    status = _github_api_error_status(response)
    if status is not None:
        return status
    installations = response.get("installations")
    if not isinstance(installations, list):
        raise ConnectorContractError("invalid github installation list")
    safe_installations: list[dict[str, object]] = []
    for installation in installations:
        if not isinstance(installation, Mapping):
            raise ConnectorContractError("invalid github installation list")
        account = installation.get("account")
        if not isinstance(account, Mapping):
            raise ConnectorContractError("invalid github installation list")
        safe_installations.append(
            {
                "account_login": _required_auth_str(account.get("login")),
                "id": _required_auth_int(installation.get("id")),
                "permissions": _safe_github_permissions(installation.get("permissions")),
                "repository_selection": _required_auth_str(
                    installation.get("repository_selection")
                ),
            }
        )
    return {
        "installations": safe_installations,
        "next_cursor": response.get("_next_cursor"),
        "schema_version": 1,
        "status": "ready",
    }


def _list_selected_github_repositories(
    *,
    credential_dir: Path,
    credential_ref: str,
    installation_id: int,
    next_cursor: str | None,
    adapter: GitHubSourceAdapter,
) -> dict[str, object]:
    if installation_id <= 0:
        raise ConnectorContractError("invalid github installation")
    response = _request_github_api(
        credential_dir=credential_dir,
        credential_ref=credential_ref,
        path=f"/user/installations/{installation_id}/repositories",
        query={"per_page": "50", **({"page": next_cursor} if next_cursor else {})},
        not_found_status="not_allowed",
    )
    status = _github_api_error_status(response)
    if status is not None:
        return status
    repositories = response.get("repositories")
    if not isinstance(repositories, list):
        raise ConnectorContractError("invalid github repository list")
    repository_list = adapter.repository_list_page(
        cast(Sequence[Mapping[str, object]], repositories),
        next_cursor=_optional_next_cursor(response.get("_next_cursor")),
        selected=True,
    )
    return {**repository_list.to_dict(), "status": repository_list.status.value}


def _request_github_token(
    *,
    client_id_env: str,
    parameters: Mapping[str, str],
) -> Mapping[str, object]:
    client_id = _github_client_id(client_id_env)
    payload = parse.urlencode({"client_id": client_id, **parameters}).encode("ascii")
    response = urlopen(  # noqa: S310 - fixed GitHub endpoint, no caller URL.
        Request(
            "https://github.com/login/oauth/access_token",
            data=payload,
            headers={"Accept": "application/json"},
            method="POST",
        ),
        timeout=15,
    )
    with response:
        decoded = json.loads(response.read().decode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise ConnectorContractError("invalid github token session")
    return decoded


def _request_github_api(
    *,
    credential_dir: Path,
    credential_ref: str,
    path: str,
    query: Mapping[str, str],
    not_found_status: str = "needs_sign_in",
) -> Mapping[str, object]:
    access_token = GitHubUserTokenStore(credential_dir).load_access_token(credential_ref)
    if not path.startswith("/"):
        raise ConnectorContractError("invalid github api path")
    encoded_query = parse.urlencode(query)
    url = f"https://api.github.com{path}"
    if encoded_query:
        url = f"{url}?{encoded_query}"
    try:
        response = urlopen(  # noqa: S310 - fixed GitHub API host and validated path.
            Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                method="GET",
            ),
            timeout=15,
        )
        with response:
            decoded = json.loads(response.read().decode("utf-8"))
            link = response.headers.get("Link")
    except HTTPError as error:
        return _github_http_error(error, not_found_status=not_found_status)
    if not isinstance(decoded, Mapping):
        raise ConnectorContractError("invalid github api response")
    return {**decoded, "_next_cursor": _next_cursor_from_link_header(link)}


def _github_http_error(error: HTTPError, *, not_found_status: str) -> dict[str, object]:
    retry_after = error.headers.get("Retry-After")
    if error.code == 401:
        return {"needs_reauthentication": True, "schema_version": 1, "status": "needs_sign_in"}
    if error.code == 404:
        if not_found_status == "not_allowed":
            return {"schema_version": 1, "status": "not_allowed"}
        return {"needs_reauthentication": True, "schema_version": 1, "status": "needs_sign_in"}
    if error.code == 403 and retry_after:
        try:
            retry_after_seconds = int(retry_after)
        except ValueError:
            retry_after_seconds = 60
        return {
            "retry_after_seconds": max(1, min(retry_after_seconds, 86_400)),
            "schema_version": 1,
            "status": "rate_limited",
        }
    if error.code == 403:
        return {"schema_version": 1, "status": "not_allowed"}
    raise ConnectorContractError("invalid github api response") from error


def _next_cursor_from_link_header(value: str | None) -> str | None:
    if value is None:
        return None
    for part in value.split(","):
        url_part, *parameter_parts = part.split(";")
        if not any(parameter.strip() == 'rel="next"' for parameter in parameter_parts):
            continue
        url = url_part.strip()
        if not (url.startswith("<") and url.endswith(">")):
            raise ConnectorContractError("invalid github api response")
        parsed = parse.urlparse(url[1:-1])
        query = parse.parse_qs(parsed.query)
        pages = query.get("page")
        if pages is None or len(pages) != 1:
            raise ConnectorContractError("invalid github api response")
        return _optional_next_cursor(pages[0])
    return None


def _optional_next_cursor(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.isdecimal() or int(value) <= 0:
        raise ConnectorContractError("invalid github api response")
    return value


def _github_api_error_status(decoded: Mapping[str, object]) -> dict[str, object] | None:
    status = decoded.get("status")
    if status in {"needs_sign_in", "rate_limited", "not_allowed"}:
        return dict(decoded)
    return None


def _persist_github_token(
    decoded: Mapping[str, object],
    *,
    credential_dir: Path,
) -> dict[str, object]:
    session = GitHubUserTokenStore(credential_dir).save_from_response(decoded)
    return {
        "credential_ref": session.credential_ref,
        "expires_at_epoch": session.expires_at_epoch,
        "needs_reauthentication": False,
        "refresh_expires_at_epoch": session.refresh_expires_at_epoch,
        "schema_version": 1,
        "scope": session.scope,
        "status": "authenticated",
        "token_type": session.token_type,
    }


def _github_auth_error_status(decoded: Mapping[str, object]) -> dict[str, object] | None:
    error = decoded.get("error")
    if error is None:
        return None
    if error == "authorization_pending":
        return {
            "next_poll_seconds": None,
            "schema_version": 1,
            "status": "authorization_pending",
        }
    if error == "slow_down":
        return {
            "next_poll_seconds": 5,
            "schema_version": 1,
            "status": "slow_down",
        }
    if error in {"expired_token", "access_denied", "bad_refresh_token"}:
        return {
            "needs_reauthentication": True,
            "schema_version": 1,
            "status": "needs_sign_in",
        }
    raise ConnectorContractError("invalid github token session")


def _required_auth_str(value: object) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ConnectorContractError("invalid github auth session")
    return value


def _required_auth_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid github auth session")
    return value


def _safe_github_permissions(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid github installation list")
    safe: dict[str, str] = {}
    for key, permission in value.items():
        if (
            type(key) is not str
            or key not in {"issues", "metadata", "pull_requests"}
            or permission != "read"
        ):
            raise ConnectorContractError("invalid github installation list")
        safe[key] = cast(str, permission)
    return safe


def _session_device_code_ref(value: str) -> str:
    if type(value) is not str or _DEVICE_SESSION_REF.fullmatch(value) is None:
        raise ConnectorContractError("invalid github auth session")
    return value


def _device_session_path(session_dir: Path, device_code_ref: str) -> Path:
    return session_dir / f"github-device-flow-{device_code_ref.rsplit('/', 1)[1]}.json"


def _github_client_id(client_id_env: str) -> str:
    if (
        not client_id_env.startswith("OPEN_BRAIN_")
        or not client_id_env.endswith("CLIENT_ID")
        or client_id_env not in os.environ
    ):
        raise ConnectorContractError("github public app registration required")
    client_id = os.environ[client_id_env]
    if not isinstance(client_id, str) or not client_id.strip() or "\x00" in client_id:
        raise ConnectorContractError("github public app registration required")
    return client_id


def _public_provider_privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": True},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "policy_public",
            "tier": "public",
        }
    )


def _write_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
