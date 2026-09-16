"""GitHub D2 adapter values for selected repositories."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.connectors import (
    ConnectorCaptureSink,
    ConnectorContractError,
    ConnectorFailureCode,
    ConnectorOutcome,
    ConnectorRunReceipt,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import (
    D2_GITHUB_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "GitHubConnectionRef",
    "GitHubDeviceAuthSession",
    "GitHubUserTokenSession",
    "GitHubUserTokenStore",
    "GitHubRepositoryCheckpointStore",
    "GitHubIssueRecord",
    "GitHubPageStatus",
    "GitHubRepositoryListItem",
    "GitHubRepositoryListPage",
    "GitHubRepositoryCheckpoint",
    "GitHubRepositoryPage",
    "GitHubSourceAdapter",
]

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_ISOISH = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z")
_CONNECTION_ID = re.compile(
    r"account:[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
)
_CREDENTIAL_REF = re.compile(r"(?:keychain|session):[A-Za-z0-9][A-Za-z0-9._:/-]{0,180}")
_SECRET_SHAPED_REF = re.compile(r"(?:ghp_|github_pat_|ghu_|ghr_|sk_)", re.IGNORECASE)
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_DEVICE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,255}")
_USER_CODE = re.compile(r"[A-Z0-9-]{4,32}")
_TOKEN_REF = re.compile(r"session:github-user-token/[0-9a-f]{32}")
_GITHUB_USER_TOKEN = re.compile(r"ghu_[A-Za-z0-9_]+")
_GITHUB_REFRESH_TOKEN = re.compile(r"ghr_[A-Za-z0-9_]+")
_MAX_PREVIEW_RECORDS = 25
_MAX_REPOSITORIES = 50

GitHubContentType = Literal["comment", "issue", "pull_request"]


class GitHubPageStatus(StrEnum):
    """Host-observed result for one bounded GitHub repository page."""

    READY = "ready"
    RATE_LIMITED = "rate_limited"
    NEEDS_SIGN_IN = "needs_sign_in"


@dataclass(frozen=True, slots=True)
class GitHubConnectionRef:
    """Non-secret GitHub account reference owned by the host."""

    connection_id: str
    account_login: str
    credential_ref: str
    public_onboarding_proof: bool

    def __post_init__(self) -> None:
        if (
            type(self.connection_id) is not str
            or _CONNECTION_ID.fullmatch(self.connection_id) is None
            or type(self.account_login) is not str
            or _LOGIN.fullmatch(self.account_login) is None
            or type(self.credential_ref) is not str
            or _CREDENTIAL_REF.fullmatch(self.credential_ref) is None
            or _SECRET_SHAPED_REF.search(self.credential_ref) is not None
            or type(self.public_onboarding_proof) is not bool
        ):
            raise ConnectorContractError("invalid github connection")


@dataclass(frozen=True, slots=True)
class GitHubDeviceAuthSession:
    """Host-mediated public-device-flow session metadata without token material."""

    device_code_ref: str
    user_code: str
    verification_uri: str
    expires_in_seconds: int
    interval_seconds: int

    def __post_init__(self) -> None:
        if (
            type(self.device_code_ref) is not str
            or _DEVICE_CODE.fullmatch(self.device_code_ref) is None
            or type(self.user_code) is not str
            or _USER_CODE.fullmatch(self.user_code) is None
            or type(self.verification_uri) is not str
            or self.verification_uri != "https://github.com/login/device"
            or type(self.expires_in_seconds) is not int
            or not 60 <= self.expires_in_seconds <= 3_600
            or type(self.interval_seconds) is not int
            or not 1 <= self.interval_seconds <= 60
        ):
            raise ConnectorContractError("invalid github auth session")


@dataclass(frozen=True, slots=True)
class GitHubUserTokenSession:
    """Metadata for a stored GitHub App user token; token values remain private."""

    credential_ref: str
    account_login: str | None
    expires_at_epoch: int
    refresh_expires_at_epoch: int
    scope: str
    token_type: str

    def __post_init__(self) -> None:
        if (
            type(self.credential_ref) is not str
            or _TOKEN_REF.fullmatch(self.credential_ref) is None
            or (
                self.account_login is not None
                and (
                    type(self.account_login) is not str
                    or _LOGIN.fullmatch(self.account_login) is None
                )
            )
            or type(self.expires_at_epoch) is not int
            or self.expires_at_epoch <= 0
            or type(self.refresh_expires_at_epoch) is not int
            or self.refresh_expires_at_epoch <= 0
            or type(self.scope) is not str
            or self.scope != ""
            or type(self.token_type) is not str
            or self.token_type.lower() != "bearer"
        ):
            raise ConnectorContractError("invalid github token session")

    @property
    def is_expired(self) -> bool:
        return self.expires_at_epoch <= int(time.time())

    @property
    def can_refresh(self) -> bool:
        return self.refresh_expires_at_epoch > int(time.time())


class GitHubUserTokenStore:
    """Private local store for GitHub App user and refresh tokens."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid github token store")
        self._root = root

    def save_from_response(
        self,
        decoded: Mapping[str, object],
        *,
        account_login: str | None = None,
        now_epoch: int | None = None,
    ) -> GitHubUserTokenSession:
        if not isinstance(decoded, Mapping):
            raise ConnectorContractError("invalid github token session")
        now = int(time.time() if now_epoch is None else now_epoch)
        access_token = _required_token(
            decoded.get("access_token"),
            _GITHUB_USER_TOKEN,
            "invalid github token session",
        )
        refresh_token = _required_token(
            decoded.get("refresh_token"),
            _GITHUB_REFRESH_TOKEN,
            "invalid github token session",
        )
        expires_in = _positive_int(decoded.get("expires_in"), "invalid github token session")
        refresh_expires_in = _positive_int(
            decoded.get("refresh_token_expires_in"),
            "invalid github token session",
        )
        scope = _typed_str(decoded.get("scope"), "invalid github token session")
        token_type = _typed_str(decoded.get("token_type"), "invalid github token session")
        digest = SourceRecordKey(
            connector_name=D2_GITHUB_SOURCE,
            connection_id="account:github-app",
            resource_id="user-token",
            external_id=access_token,
            revision_id=refresh_token,
        ).revision_identity()[:32]
        session = GitHubUserTokenSession(
            credential_ref=f"session:github-user-token/{digest}",
            account_login=account_login,
            expires_at_epoch=now + expires_in,
            refresh_expires_at_epoch=now + refresh_expires_in,
            scope=scope,
            token_type=token_type,
        )
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(session.credential_ref)
        payload = json.dumps(
            {
                "access_token": access_token,
                "account_login": account_login,
                "app_type": "github_app",
                "expires_at_epoch": session.expires_at_epoch,
                "permission_model": "github_app_permissions",
                "refresh_expires_at_epoch": session.refresh_expires_at_epoch,
                "refresh_token": refresh_token,
                "schema_version": 1,
                "scope": scope,
                "token_type": token_type,
            },
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        with NamedTemporaryFile(
            "w",
            delete=False,
            dir=self._root,
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            handle.write(payload)
            temp_name = handle.name
        Path(temp_name).chmod(0o600)
        try:
            os.replace(temp_name, path)
        except OSError:
            Path(temp_name).unlink(missing_ok=True)
            raise
        path.chmod(0o600)
        return session

    def load_metadata(self, credential_ref: str) -> GitHubUserTokenSession:
        path = self._path(credential_ref)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid github token store") from error
        if not isinstance(decoded, Mapping):
            raise ConnectorContractError("invalid github token store")
        return GitHubUserTokenSession(
            credential_ref=credential_ref,
            account_login=_optional_str(decoded.get("account_login"), "invalid github token store"),
            expires_at_epoch=_typed_int(
                decoded.get("expires_at_epoch"),
                "invalid github token store",
            ),
            refresh_expires_at_epoch=_typed_int(
                decoded.get("refresh_expires_at_epoch"),
                "invalid github token store",
            ),
            scope=_typed_str(decoded.get("scope"), "invalid github token store"),
            token_type=_typed_str(decoded.get("token_type"), "invalid github token store"),
        )

    def metadata_exists(self, credential_ref: str) -> bool:
        return self._path(credential_ref).exists()

    def load_refresh_token(self, credential_ref: str) -> str:
        path = self._path(credential_ref)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid github token store") from error
        if not isinstance(decoded, Mapping):
            raise ConnectorContractError("invalid github token store")
        return _required_token(
            decoded.get("refresh_token"),
            _GITHUB_REFRESH_TOKEN,
            "invalid github token store",
        )

    def load_access_token(self, credential_ref: str) -> str:
        path = self._path(credential_ref)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid github token store") from error
        if not isinstance(decoded, Mapping):
            raise ConnectorContractError("invalid github token store")
        return _required_token(
            decoded.get("access_token"),
            _GITHUB_USER_TOKEN,
            "invalid github token store",
        )

    def revoke_locally(self, credential_ref: str) -> None:
        self._path(credential_ref).unlink(missing_ok=True)

    def _path(self, credential_ref: str) -> Path:
        if type(credential_ref) is not str or _TOKEN_REF.fullmatch(credential_ref) is None:
            raise ConnectorContractError("invalid github token session")
        return self._root / f"github-user-token-{credential_ref.rsplit('/', 1)[1]}.json"


@dataclass(frozen=True, slots=True)
class GitHubIssueRecord:
    """A bounded issue, pull request, or comment record from GitHub REST data."""

    content_type: GitHubContentType
    number: int
    external_id: str
    revision_id: str
    html_url: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class GitHubRepositoryListItem:
    """One host-discovered repository available for explicit selection."""

    owner: str
    name: str
    html_url: str
    selected: bool = False

    def __post_init__(self) -> None:
        try:
            _require_repository_name(self.owner, self.name)
        except ConnectorContractError as error:
            raise ConnectorContractError("invalid github repository list") from error
        if (
            type(self.html_url) is not str
            or self.html_url != f"https://github.com/{self.owner}/{self.name}"
            or type(self.selected) is not bool
        ):
            raise ConnectorContractError("invalid github repository list")

    def to_dict(self) -> dict[str, object]:
        return {
            "html_url": self.html_url,
            "name": self.name,
            "owner": self.owner,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class GitHubRepositoryListPage:
    """Bounded host-mediated repository listing page."""

    status: GitHubPageStatus
    repositories: tuple[GitHubRepositoryListItem, ...] = ()
    next_cursor: str | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        try:
            status = GitHubPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid github repository list") from error
        if status is GitHubPageStatus.READY:
            if (
                not isinstance(self.repositories, tuple)
                or len(self.repositories) > _MAX_REPOSITORIES
                or any(type(item) is not GitHubRepositoryListItem for item in self.repositories)
                or self.retry_after_seconds is not None
                or (
                    self.next_cursor is not None
                    and (
                        type(self.next_cursor) is not str
                        or _CURSOR.fullmatch(self.next_cursor) is None
                    )
                )
            ):
                raise ConnectorContractError("invalid github repository list")
        elif self.repositories or self.next_cursor is not None or (
            self.retry_after_seconds is not None
            and (
                type(self.retry_after_seconds) is not int
                or not 1 <= self.retry_after_seconds <= 86_400
            )
        ):
            raise ConnectorContractError("invalid github repository list")
        object.__setattr__(self, "status", status)

    def to_dict(self) -> dict[str, object]:
        return {
            "next_cursor": self.next_cursor,
            "repositories": [item.to_dict() for item in self.repositories],
            "retry_after_seconds": self.retry_after_seconds,
            "schema_version": 1,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class GitHubRepositoryCheckpoint:
    """Durable repository cursor committed after capture acknowledgements."""

    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.selection) is not SourceResourceSelection
            or self.selection.connector_name != D2_GITHUB_SOURCE
            or self.selection.resource_type != "repository"
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str
                    or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str or not value.startswith("connector.github.")
                for value in self.committed_delivery_ids
            )
            or len(self.committed_delivery_ids) != len(set(self.committed_delivery_ids))
            or not isinstance(self.committed_revision_identities, tuple)
            or any(
                type(value) is not str or _CURSOR.fullmatch(value) is None
                for value in self.committed_revision_identities
            )
            or (
                self.committed_revision_identities
                and len(self.committed_revision_identities) != len(self.committed_delivery_ids)
            )
        ):
            raise ConnectorContractError("invalid github checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> GitHubRepositoryCheckpoint:
        return cls(
            schema_version=1,
            selection=selection,
            next_cursor=None,
            committed_delivery_ids=(),
        )

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str] | None = None,
    ) -> GitHubRepositoryCheckpoint:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError("invalid github checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        if tuple(committed_delivery_ids) != expected:
            raise ConnectorContractError("invalid github checkpoint")
        revision_identities = tuple(
            () if committed_revision_identities is None else committed_revision_identities
        )
        if revision_identities and len(revision_identities) != len(expected):
            raise ConnectorContractError("invalid github checkpoint")
        existing_revisions = (
            dict(zip(self.committed_delivery_ids, self.committed_revision_identities, strict=True))
            if self.committed_revision_identities
            else {}
        )
        existing_delivery_ids = tuple(
            delivery_id
            for delivery_id in self.committed_delivery_ids
            if delivery_id not in set(expected)
        )
        retained_revisions = tuple(
            existing_revisions[delivery_id]
            for delivery_id in existing_delivery_ids
            if delivery_id in existing_revisions
        )
        return GitHubRepositoryCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=page.next_cursor,
            committed_delivery_ids=existing_delivery_ids + expected,
            committed_revision_identities=(
                retained_revisions + revision_identities if revision_identities else ()
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "committed_delivery_ids": list(self.committed_delivery_ids),
            "connector_name": self.selection.connector_name,
            "connection_id": self.selection.connection_id,
            "next_cursor": self.next_cursor,
            "resource_id": self.selection.resource_id,
            "resource_type": self.selection.resource_type,
            "schema_version": self.schema_version,
            "committed_revision_identities": list(self.committed_revision_identities),
        }

    @classmethod
    def from_dict(cls, value: object) -> GitHubRepositoryCheckpoint:
        legacy_fields = {
            "committed_delivery_ids",
            "connector_name",
            "connection_id",
            "next_cursor",
            "resource_id",
            "resource_type",
            "schema_version",
        }
        current_fields = legacy_fields | {"committed_revision_identities"}
        if not isinstance(value, dict) or set(value) not in {
            frozenset(legacy_fields),
            frozenset(current_fields),
        }:
            raise ConnectorContractError("invalid github checkpoint")
        committed = value["committed_delivery_ids"]
        committed_revisions = value.get("committed_revision_identities", [])
        if not isinstance(committed, list) or not isinstance(committed_revisions, list):
            raise ConnectorContractError("invalid github checkpoint")
        return cls(
            schema_version=_typed_int(value["schema_version"], "invalid github checkpoint"),
            selection=SourceResourceSelection(
                connector_name=_typed_str(value["connector_name"], "invalid github checkpoint"),
                connection_id=_typed_str(value["connection_id"], "invalid github checkpoint"),
                resource_id=_typed_str(value["resource_id"], "invalid github checkpoint"),
                resource_type=_typed_str(value["resource_type"], "invalid github checkpoint"),
            ),
            next_cursor=_optional_str(value["next_cursor"], "invalid github checkpoint"),
            committed_delivery_ids=tuple(
                _typed_str(item, "invalid github checkpoint") for item in committed
            ),
            committed_revision_identities=tuple(
                _typed_str(item, "invalid github checkpoint") for item in committed_revisions
            ),
        )


class GitHubRepositoryCheckpointStore:
    """Goal-owned JSON checkpoint persistence for selected GitHub repositories."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid github checkpoint store")
        self._root = root

    def load(self, selection: SourceResourceSelection) -> GitHubRepositoryCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return GitHubRepositoryCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid github checkpoint store") from error
        checkpoint = GitHubRepositoryCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid github checkpoint store")
        return checkpoint

    def save(self, checkpoint: GitHubRepositoryCheckpoint) -> Path:
        if type(checkpoint) is not GitHubRepositoryCheckpoint:
            raise ConnectorContractError("invalid github checkpoint store")
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(checkpoint.selection)
        payload = json.dumps(checkpoint.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        with NamedTemporaryFile(
            "w",
            delete=False,
            dir=self._root,
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            handle.write(payload)
            temp_name = handle.name
        try:
            os.replace(temp_name, path)
        except OSError:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return path

    def _path(self, selection: SourceResourceSelection) -> Path:
        if (
            type(selection) is not SourceResourceSelection
            or selection.connector_name != D2_GITHUB_SOURCE
            or selection.resource_type != "repository"
        ):
            raise ConnectorContractError("invalid github checkpoint store")
        digest = SourceRecordKey(
            connector_name=D2_GITHUB_SOURCE,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id="repository",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"github-repository-{digest}.json"


@dataclass(frozen=True, slots=True)
class GitHubRepositoryPage:
    """A bounded host-mediated page result with no credential or body exposure."""

    status: GitHubPageStatus
    preview: SourcePreviewPage | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        try:
            status = GitHubPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid github page") from error
        if status is GitHubPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage or self.retry_after_seconds is not None:
                raise ConnectorContractError("invalid github page")
        elif (
            self.preview is not None
            or (
                self.retry_after_seconds is not None
                and (
                    type(self.retry_after_seconds) is not int
                    or not 1 <= self.retry_after_seconds <= 86_400
                )
            )
        ):
            raise ConnectorContractError("invalid github page")
        object.__setattr__(self, "status", status)


class GitHubSourceAdapter:
    """Convert host-mediated GitHub REST responses into D2 source values."""

    def repository_selection(
        self, *, connection_id: str, owner: str, repository: str
    ) -> SourceResourceSelection:
        _require_repository_name(owner, repository)
        _require_connection_id(connection_id)
        return SourceResourceSelection(
            connector_name=D2_GITHUB_SOURCE,
            connection_id=connection_id,
            resource_id=f"repo:{owner}/{repository}",
            resource_type="repository",
        )

    def connection_ref(
        self,
        *,
        connection_id: str,
        account_login: str,
        credential_ref: str,
        public_onboarding_proof: bool,
    ) -> GitHubConnectionRef:
        return GitHubConnectionRef(
            connection_id=connection_id,
            account_login=account_login,
            credential_ref=credential_ref,
            public_onboarding_proof=public_onboarding_proof,
        )

    def device_auth_session(
        self,
        *,
        device_code_ref: str,
        user_code: str,
        verification_uri: str,
        expires_in_seconds: int,
        interval_seconds: int,
    ) -> GitHubDeviceAuthSession:
        return GitHubDeviceAuthSession(
            device_code_ref=device_code_ref,
            user_code=user_code,
            verification_uri=verification_uri,
            expires_in_seconds=expires_in_seconds,
            interval_seconds=interval_seconds,
        )

    def repository_list_page(
        self,
        values: Sequence[Mapping[str, object]],
        *,
        next_cursor: str | None = None,
        selected: bool = False,
    ) -> GitHubRepositoryListPage:
        if not isinstance(values, Sequence) or isinstance(values, str):
            raise ConnectorContractError("invalid github repository list")
        if len(values) > _MAX_REPOSITORIES:
            raise ConnectorContractError("invalid github repository list")
        repositories = tuple(
            self.repository_list_item_from_rest(value, selected=selected) for value in values
        )
        return GitHubRepositoryListPage(
            status=GitHubPageStatus.READY,
            repositories=repositories,
            next_cursor=next_cursor,
        )

    def repository_list_rate_limited_page(
        self, *, retry_after_seconds: int
    ) -> GitHubRepositoryListPage:
        return GitHubRepositoryListPage(
            status=GitHubPageStatus.RATE_LIMITED,
            retry_after_seconds=retry_after_seconds,
        )

    def repository_list_needs_sign_in_page(self) -> GitHubRepositoryListPage:
        return GitHubRepositoryListPage(status=GitHubPageStatus.NEEDS_SIGN_IN)

    def repository_list_item_from_rest(
        self, value: Mapping[str, object], *, selected: bool = False
    ) -> GitHubRepositoryListItem:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid github repository list")
        owner = value.get("owner")
        if not isinstance(owner, Mapping):
            raise ConnectorContractError("invalid github repository list")
        return GitHubRepositoryListItem(
            owner=_required_str(owner.get("login"), "invalid github repository list"),
            name=_required_str(value.get("name"), "invalid github repository list"),
            html_url=_required_str(value.get("html_url"), "invalid github repository list"),
            selected=selected,
        )

    def preview_repository(
        self,
        selection: SourceResourceSelection,
        records: Sequence[GitHubIssueRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        if (
            type(selection) is not SourceResourceSelection
            or selection.connector_name != D2_GITHUB_SOURCE
        ):
            raise ConnectorContractError("invalid github selection")
        if selection.resource_type != "repository":
            raise ConnectorContractError("invalid github selection")
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid github records")
        if len(records) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid github records")
        preview_records = tuple(
            SourcePreviewRecord.from_intake(
                self.intake(selection, record, privacy=privacy),
                content_type=record.content_type,
            )
            for record in records
        )
        return SourcePreviewPage(
            selection=selection,
            records=preview_records,
            next_cursor=next_cursor,
        )

    def repository_page_from_rest(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> GitHubRepositoryPage:
        if not isinstance(values, Sequence) or isinstance(values, str):
            raise ConnectorContractError("invalid github page")
        records = tuple(
            self.comment_from_rest(value)
            if _looks_like_comment(value)
            else self.issue_from_rest(value)
            for value in values
        )
        return GitHubRepositoryPage(
            status=GitHubPageStatus.READY,
            preview=self.preview_repository(
                selection,
                records,
                privacy=privacy,
                next_cursor=next_cursor,
            ),
        )

    def rate_limited_page(self, *, retry_after_seconds: int) -> GitHubRepositoryPage:
        return GitHubRepositoryPage(
            status=GitHubPageStatus.RATE_LIMITED,
            retry_after_seconds=retry_after_seconds,
        )

    def needs_sign_in_page(self) -> GitHubRepositoryPage:
        return GitHubRepositoryPage(status=GitHubPageStatus.NEEDS_SIGN_IN)

    def import_repository_page(
        self,
        checkpoint: GitHubRepositoryCheckpoint,
        page: GitHubRepositoryPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[GitHubRepositoryCheckpoint, ConnectorRunReceipt]:
        """Submit one acknowledged GitHub preview page and advance after exact receipts."""

        if type(checkpoint) is not GitHubRepositoryCheckpoint:
            raise ConnectorContractError("invalid github import")
        if type(page) is not GitHubRepositoryPage:
            raise ConnectorContractError("invalid github import")
        if not isinstance(intakes, Sequence) or isinstance(intakes, str):
            raise ConnectorContractError("invalid github import")
        if type(capture_sink) is not ConnectorCaptureSink:
            raise ConnectorContractError("invalid github import")
        if page.status is GitHubPageStatus.RATE_LIMITED:
            return (
                checkpoint,
                ConnectorRunReceipt.failed(
                    D2_GITHUB_SOURCE,
                    ConnectorFailureCode.RUNTIME_FAILED,
                ),
            )
        if page.status is GitHubPageStatus.NEEDS_SIGN_IN:
            return (
                checkpoint,
                ConnectorRunReceipt.failed(
                    D2_GITHUB_SOURCE,
                    ConnectorFailureCode.NOT_ALLOWED,
                ),
            )
        preview = page.preview
        if preview is None or preview.selection != checkpoint.selection:
            raise ConnectorContractError("invalid github import")
        selected = tuple(record for record in preview.records if record.selected)
        if not selected:
            return checkpoint, ConnectorRunReceipt.empty(
                D2_GITHUB_SOURCE,
                metadata_count=len(preview.records),
            )
        intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
        if (
            len(intake_by_delivery) != len(intakes)
            or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
            or any(
                intake.key.connector_name != preview.selection.connector_name
                or intake.key.connection_id != preview.selection.connection_id
                or intake.key.resource_id != preview.selection.resource_id
                or intake.source_reference != record.source_reference
                for record, intake in zip(selected, intakes, strict=True)
            )
        ):
            raise ConnectorContractError("invalid github import")
        committed_revisions = (
            dict(
                zip(
                    checkpoint.committed_delivery_ids,
                    checkpoint.committed_revision_identities,
                    strict=True,
                )
            )
            if checkpoint.committed_revision_identities
            else {}
        )
        committed_delivery_ids = set(checkpoint.committed_delivery_ids)
        if not committed_revisions:
            committed = tuple(
                record.delivery_id
                for record in selected
                if record.delivery_id in committed_delivery_ids
            )
            if committed and len(committed) != len(selected):
                raise ConnectorContractError("invalid github checkpoint")
            # Legacy checkpoints did not record revision identities. Re-submit the complete
            # page once so the engine can duplicate/replacement-check exact payloads and the
            # checkpoint can become revision-aware.
            changed_intakes = tuple(intakes)
        else:
            changed_intakes = tuple(
                intake
                for intake in intakes
                if committed_revisions.get(intake.key.delivery_id())
                != intake.key.revision_identity()
            )
        if not changed_intakes:
            return checkpoint, ConnectorRunReceipt.empty(
                D2_GITHUB_SOURCE,
                metadata_count=len(preview.records),
            )

        receipts = tuple(
            capture_sink.submit(
                intake.payload(),
                delivery_id=intake.key.delivery_id(),
                source_origin="third_party",
                source_reference=intake.source_reference,
                provenance=intake.provenance(),
                privacy=intake.privacy,
                intent="reference",
                title=intake.title,
            )
            for intake in changed_intakes
        )
        advanced = checkpoint.advance(
            SourcePreviewPage(
                selection=preview.selection,
                records=selected,
                next_cursor=preview.next_cursor,
            ),
            committed_delivery_ids=tuple(record.delivery_id for record in selected),
            committed_revision_identities=tuple(
                intake.key.revision_identity() for intake in intakes
            ),
        )
        return (
            advanced,
            ConnectorRunReceipt(
                connector_name=D2_GITHUB_SOURCE,
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=len(preview.records),
                fetched_count=len(selected),
                extracted_count=len(selected),
                submitted_count=len(receipts),
                stubbed_count=0,
                created_count=sum(1 for receipt in receipts if not receipt.duplicate),
                duplicate_count=sum(1 for receipt in receipts if receipt.duplicate),
                checkpoint_committed=True,
                metadata_count=len(preview.records),
            ),
        )

    def intake(
        self,
        selection: SourceResourceSelection,
        record: GitHubIssueRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not GitHubIssueRecord:
            raise ConnectorContractError("invalid github record")
        _require_record_url_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D2_GITHUB_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=record.external_id,
                revision_id=record.revision_id,
            ),
            url=record.html_url,
            title=record.title,
            text=record.body,
            privacy=privacy,
        )

    def issue_from_rest(self, value: Mapping[str, object]) -> GitHubIssueRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid github issue")
        number = _positive_int(value.get("number"), "invalid github issue")
        content_type: GitHubContentType = "pull_request" if "pull_request" in value else "issue"
        return GitHubIssueRecord(
            content_type=content_type,
            number=number,
            external_id=f"{'pull' if content_type == 'pull_request' else 'issue'}:{number}",
            revision_id="updated:" + _required_timestamp(value.get("updated_at")),
            html_url=_required_str(value.get("html_url"), "invalid github issue"),
            title=_required_str(value.get("title"), "invalid github issue"),
            body=_body_or_title(value.get("body"), value.get("title"), "invalid github issue"),
        )

    def comment_from_rest(self, value: Mapping[str, object]) -> GitHubIssueRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid github comment")
        comment_id = _positive_int(value.get("id"), "invalid github comment")
        issue_number = _issue_number_from_url(
            _required_str(value.get("issue_url"), "invalid github comment")
        )
        return GitHubIssueRecord(
            content_type="comment",
            number=issue_number,
            external_id=f"comment:{comment_id}",
            revision_id="updated:" + _required_timestamp(value.get("updated_at")),
            html_url=_required_str(value.get("html_url"), "invalid github comment"),
            title=f"Comment on issue {issue_number}",
            body=_body_or_title(
                value.get("body"),
                f"Comment on issue {issue_number}",
                "invalid github comment",
            ),
        )


def _require_repository_name(owner: str, repository: str) -> None:
    if (
        type(owner) is not str
        or _OWNER.fullmatch(owner) is None
        or type(repository) is not str
        or _REPO.fullmatch(repository) is None
    ):
        raise ConnectorContractError("invalid github repository")


def _require_connection_id(connection_id: str) -> None:
    if type(connection_id) is not str or _CONNECTION_ID.fullmatch(connection_id) is None:
        raise ConnectorContractError("invalid github connection")


def _positive_int(value: object, message: str) -> int:
    if type(value) is not int or value < 1:
        raise ConnectorContractError(message)
    return value


def _required_str(value: object, message: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ConnectorContractError(message)
    return value


def _required_token(value: object, token_pattern: re.Pattern[str], message: str) -> str:
    if type(value) is not str or token_pattern.fullmatch(value) is None:
        raise ConnectorContractError(message)
    return value


def _typed_str(value: object, message: str) -> str:
    if type(value) is not str:
        raise ConnectorContractError(message)
    return value


def _optional_str(value: object, message: str) -> str | None:
    if value is None:
        return None
    return _typed_str(value, message)


def _typed_int(value: object, message: str) -> int:
    if type(value) is not int:
        raise ConnectorContractError(message)
    return value


def _looks_like_comment(value: object) -> bool:
    return isinstance(value, Mapping) and "issue_url" in value and "id" in value


def _body_or_title(body: object, title: object, message: str) -> str:
    if body is None:
        return _required_str(title, message)
    if type(body) is not str or "\x00" in body:
        raise ConnectorContractError(message)
    return body.strip() or _required_str(title, message)


def _required_timestamp(value: object) -> str:
    timestamp = _required_str(value, "invalid github timestamp")
    if _ISOISH.fullmatch(timestamp) is None:
        raise ConnectorContractError("invalid github timestamp")
    return timestamp


def _issue_number_from_url(value: str) -> int:
    match = re.search(r"/issues/([1-9][0-9]*)$", value)
    if match is None:
        raise ConnectorContractError("invalid github comment")
    return int(match.group(1))


def _require_record_url_matches_selection(
    selection: SourceResourceSelection, record: GitHubIssueRecord
) -> None:
    owner, repository = _repository_parts(selection)
    base = f"https://github.com/{owner}/{repository}"
    if record.content_type == "pull_request":
        expected = f"{base}/pull/{record.number}"
    else:
        expected = f"{base}/issues/{record.number}"
    if record.content_type == "comment":
        comment_pattern = re.escape(expected) + r"#issuecomment-[1-9][0-9]*"
        if re.fullmatch(comment_pattern, record.html_url) is None:
            raise ConnectorContractError("invalid github record")
    elif record.html_url != expected:
        raise ConnectorContractError("invalid github record")


def _repository_parts(selection: SourceResourceSelection) -> tuple[str, str]:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != D2_GITHUB_SOURCE
        or selection.resource_type != "repository"
        or not selection.resource_id.startswith("repo:")
    ):
        raise ConnectorContractError("invalid github selection")
    parts = selection.resource_id.removeprefix("repo:").split("/", 1)
    if len(parts) != 2:
        raise ConnectorContractError("invalid github selection")
    owner, repository = parts
    _require_repository_name(owner, repository)
    return owner, repository
