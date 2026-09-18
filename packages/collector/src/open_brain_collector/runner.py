"""Bounded process runner for the optional unattended collector."""

from __future__ import annotations

import json
import signal
import stat
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from open_brain_engine.engine import (
    LocalEngineContext,
    PrivacyDecision,
    ProviderMode,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    open_local_engine,
)

from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorLease,
    CollectorLeaseError,
    CollectorRunPage,
    CollectorSourceRuntime,
    CollectorStateStore,
    CredentialStatusProvider,
    EngineCaptureSink,
)
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.github import GitHubSourceAdapter, GitHubUserTokenStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = [
    "CollectorProcessRunner",
    "DispatchingSourceRuntime",
    "FixtureSourceRuntime",
    "LocalSourceRuntime",
    "collector_capture_sink",
]


class CollectorProfileError(ValueError):
    """The collector cannot safely open the selected Brain root."""


@dataclass(frozen=True, slots=True)
class CollectorProcessRunner:
    """Run due sources from durable state under an exclusive process lease."""

    state_path: Path
    brain_root: Path
    lease_path: Path
    runtime: CollectorSourceRuntime
    owner: str = "open-brain-collector"
    clock: Callable[[], int] = lambda: int(time.time())
    control_enabled: bool = False
    background: bool = False

    @contextmanager
    def _control(self) -> Iterator[None]:
        if not self.control_enabled:
            yield
            return
        from open_brain_collector.control import control_server

        with control_server(self.state_path, self.brain_root, background=self.background):
            yield

    def run_once(
        self,
        *,
        source_id: str | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        if (
            not isinstance(self.state_path, Path)
            or not isinstance(self.brain_root, Path)
            or not isinstance(self.lease_path, Path)
            or not isinstance(self.runtime, CollectorSourceRuntime)
            or (source_id is not None and (type(source_id) is not str or not source_id))
            or type(force) is not bool
        ):
            raise ConnectorContractError("invalid collector runner")
        controller = CollectorController(
            CollectorStateStore(self.state_path), clock=self.clock, brain_root=self.brain_root
        )
        try:
            with CollectorLease(self.lease_path).acquire(owner=self.owner):
                results = self._sync_sources(
                    controller=controller,
                    source_id=source_id,
                    force=force,
                )
        except CollectorLeaseError:
            return {"outcome": "failed", "failure_code": "collector_already_owned", "results": []}
        return {"outcome": "completed", "failure_code": None, "results": results}

    def run_loop(
        self,
        *,
        interval_seconds: float,
        max_iterations: int | None = None,
    ) -> dict[str, object]:
        if interval_seconds <= 0 or interval_seconds > 3600:
            raise ConnectorContractError("invalid collector run interval")
        iterations = 0
        last: dict[str, object] = {"outcome": "empty", "failure_code": None, "results": []}
        with _collector_stop_signal() as stop:
            try:
                with CollectorLease(self.lease_path).acquire(owner=self.owner), self._control():
                    controller = CollectorController(
                        CollectorStateStore(self.state_path),
                        clock=self.clock,
                        brain_root=self.brain_root,
                    )
                    while max_iterations is None or iterations < max_iterations:
                        last = {
                            "outcome": "completed",
                            "failure_code": None,
                            "results": self._sync_sources(
                                controller=controller,
                                source_id=None,
                                force=False,
                            ),
                        }
                        iterations += 1
                        if stop():
                            return {
                                "iterations": iterations,
                                "last": last,
                                "outcome": "completed",
                                "stop_reason": "signal",
                            }
                        if max_iterations is not None and iterations >= max_iterations:
                            break
                        state_mtime = _path_mtime(self.state_path)
                        deadline = time.monotonic() + interval_seconds
                        while not stop():
                            if _path_mtime(self.state_path) != state_mtime:
                                break
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            time.sleep(min(remaining, 0.2))
                        if stop():
                            return {
                                "iterations": iterations,
                                "last": last,
                                "outcome": "completed",
                                "stop_reason": "signal",
                            }
            except CollectorLeaseError:
                return {
                    "iterations": iterations,
                    "last": {
                        "outcome": "failed",
                        "failure_code": "collector_already_owned",
                        "results": [],
                    },
                    "outcome": "failed",
                    "stop_reason": "owned",
                }
        return {
            "iterations": iterations,
            "last": last,
            "outcome": "completed",
            "stop_reason": "bounded",
        }

    def _sync_sources(
        self,
        *,
        controller: CollectorController,
        source_id: str | None,
        force: bool,
    ) -> list[dict[str, object]]:
        store = CollectorStateStore(self.state_path)
        state = store.load()
        sources = cast(dict[str, object], state["sources"])
        if source_id is not None and source_id not in sources:
            raise ConnectorContractError("unknown collector source")
        source_ids = [source_id] if source_id is not None else sorted(sources)
        if force:
            for selected_id in source_ids:
                entry = sources[selected_id]
                if isinstance(entry, dict) and entry.get("status") == "enabled":
                    entry["next_run_epoch"] = self.clock()
            store.save(state)
        if not source_ids:
            return self._sync_live() if source_id is None else []
        capture_sink = EngineCaptureSink(collector_capture_sink(self.brain_root))
        credential_status = (
            self.runtime if isinstance(self.runtime, CredentialStatusProvider) else None
        )
        results = [
            controller.sync_due(
                source_id=selected_id,
                runtime=self.runtime,
                capture_sink=capture_sink,
                credential_status=credential_status,
            ).to_dict()
            for selected_id in source_ids
        ]
        return results + (self._sync_live() if source_id is None else [])

    def _sync_live(self) -> list[dict[str, object]]:
        root = self.state_path.parent / "live"
        if not (root / "capture" / "brain.json").exists():
            return []
        from open_brain_collector.live_manager import LiveSourceManager

        manager = LiveSourceManager(root, self.brain_root, background=self.background)
        manager.capture._clock = self.clock
        return manager.capture.sync_due()


class FixtureSourceRuntime(CollectorSourceRuntime):
    """Explicit JSON fixture runtime for process/integration acceptance tests."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ConnectorContractError("invalid collector fixture runtime")
        self._path = path

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid collector fixture runtime") from error
        if not isinstance(payload, Mapping):
            raise ConnectorContractError("invalid collector fixture runtime")
        records = payload.get("records")
        next_cursor = payload.get("next_cursor")
        if not isinstance(records, list):
            raise ConnectorContractError("invalid collector fixture runtime")
        intakes = tuple(_intake_from_fixture(record, selection) for record in records)
        return CollectorRunPage(
            selection=selection,
            intakes=intakes,
            next_cursor=cast(str | None, next_cursor),
        )


class LocalSourceRuntime(CollectorSourceRuntime):
    """Durable local runtime pages staged by source setup flows."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid collector runtime")
        self._root = root

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        path = self._root / f"{_runtime_page_name(selection)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("collector_runtime_unavailable") from error
        if not isinstance(payload, Mapping):
            raise ConnectorContractError("collector_runtime_unavailable")
        if payload.get("connector_name") != selection.connector_name:
            raise ConnectorContractError("collector_runtime_unavailable")
        if payload.get("connection_id") != selection.connection_id:
            raise ConnectorContractError("collector_runtime_unavailable")
        if payload.get("resource_id") != selection.resource_id:
            raise ConnectorContractError("collector_runtime_unavailable")
        if payload.get("cursor") != cursor:
            raise ConnectorContractError("collector_runtime_unavailable")
        records = payload.get("records")
        next_cursor = payload.get("next_cursor")
        if not isinstance(records, list):
            raise ConnectorContractError("collector_runtime_unavailable")
        intakes = tuple(_intake_from_fixture(record, selection) for record in records)
        return CollectorRunPage(
            selection=selection,
            intakes=intakes,
            next_cursor=cast(str | None, next_cursor),
        )


class DispatchingSourceRuntime(CollectorSourceRuntime):
    """Host runtime that dispatches durable selections to connector adapters."""

    def __init__(
        self,
        *,
        state_path: Path,
        local_runtime_root: Path,
        credential_dir: Path | None = None,
        http_get: Callable[[Request], Any] | None = None,
    ) -> None:
        if not isinstance(state_path, Path) or not isinstance(local_runtime_root, Path):
            raise ConnectorContractError("invalid collector runtime")
        if credential_dir is not None and not isinstance(credential_dir, Path):
            raise ConnectorContractError("invalid collector runtime")
        self._state_path = state_path
        self._local = LocalSourceRuntime(local_runtime_root)
        self._local_root = local_runtime_root
        self._credential_dir = credential_dir
        self._http_get = urlopen if http_get is None else http_get

    def status_for(self, source_id: str) -> str:
        entry = self._source_entry(source_id)
        selection = _selection_from_entry(entry)
        cursor = cast(str | None, entry.get("next_cursor"))
        if _local_runtime_page_exists(self._local_root, selection, cursor):
            return "available"
        if selection.connector_name != "github":
            return "missing"
        credential_ref = entry.get("credential_ref")
        if self._credential_dir is None or type(credential_ref) is not str:
            return "missing"
        try:
            session = GitHubUserTokenStore(self._credential_dir).load_metadata(credential_ref)
        except ConnectorContractError:
            return "missing"
        if not session.is_expired:
            return "available"
        return "locked" if session.can_refresh else "missing"

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        if _local_runtime_page_exists(self._local_root, selection, cursor):
            return self._local.fetch_page(selection, cursor)
        if selection.connector_name == "github":
            return self._fetch_github_page(selection, cursor)
        raise ConnectorContractError("unsupported collector source runtime")

    def _fetch_github_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        if self._credential_dir is None:
            raise ConnectorContractError("credential_missing")
        credential_ref = self._credential_ref_for(selection)
        token_store = GitHubUserTokenStore(self._credential_dir)
        try:
            token = token_store.load_access_token(credential_ref)
        except ConnectorContractError as error:
            raise ConnectorContractError("credential_missing") from error
        owner, repository = _github_repository(selection)
        page_number = _github_page_number(cursor)
        request = Request(
            (
                f"https://api.github.com/repos/{owner}/{repository}/issues"
                f"?state=all&per_page=25&page={page_number}"
            ),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "open-brain-collector/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self._http_get(request) as response:
                values = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code in {401, 403, 404}:
                raise ConnectorContractError("credential_missing") from error
            raise ConnectorContractError("collector_runtime_unavailable") from error
        except (OSError, URLError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ConnectorContractError("collector_runtime_unavailable") from error
        if not isinstance(values, list) or any(not isinstance(item, Mapping) for item in values):
            raise ConnectorContractError("collector_runtime_unavailable")
        adapter = GitHubSourceAdapter()
        records = tuple(
            adapter.comment_from_rest(value)
            if _looks_like_github_comment(value)
            else adapter.issue_from_rest(value)
            for value in cast(list[Mapping[str, object]], values)
        )
        page = adapter.repository_page_from_rest(
            selection,
            cast(list[Mapping[str, object]], values),
            privacy=_public_provider_privacy(),
            next_cursor=str(page_number + 1) if len(values) == 25 else None,
        )
        if page.preview is None:
            raise ConnectorContractError("collector_runtime_unavailable")
        intakes = tuple(
            adapter.intake(selection, record, privacy=_public_provider_privacy())
            for record in records
        )
        return CollectorRunPage(
            selection=selection,
            intakes=intakes,
            next_cursor=page.preview.next_cursor,
        )

    def _credential_ref_for(self, selection: SourceResourceSelection) -> str:
        state = CollectorStateStore(self._state_path).load()
        for entry in cast(dict[str, object], state["sources"]).values():
            if not isinstance(entry, Mapping):
                continue
            if (
                _selection_from_entry(entry) == selection
                and type(entry.get("credential_ref")) is str
            ):
                return cast(str, entry["credential_ref"])
        raise ConnectorContractError("credential_missing")

    def _source_entry(self, source_id: str) -> Mapping[str, object]:
        state = CollectorStateStore(self._state_path).load()
        sources = cast(dict[str, object], state["sources"])
        entry = sources.get(source_id)
        if not isinstance(entry, Mapping):
            raise ConnectorContractError("unknown collector source")
        return entry


def collector_capture_sink(brain_root: Path) -> PublicJobCaptureSink:
    """Open the selected Brain root and return the collector's public capture sink."""

    try:
        profile = _open_existing_collector_profile(brain_root)
    except CollectorProfileError as error:
        raise ConnectorContractError("collector host runtime unavailable") from error
    tasks = open_local_engine(profile)
    actor = "actor_11111111-1111-4111-8111-111111111111"
    context = PublicJobCaptureContext.create(
        profile=tasks.profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_11111111-1111-4111-8111-111111111111",
            "role_id": "role_11111111-1111-4111-8111-111111111111",
            "tenant_id": tasks.profile.tenant_id,
        },
    )
    return tasks.capture.public_job_sink(context)


@contextmanager
def _collector_stop_signal() -> Iterator[Callable[[], bool]]:
    interrupted = False
    previous_handlers = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    def mark_stopped(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    try:
        signal.signal(signal.SIGINT, mark_stopped)
        signal.signal(signal.SIGTERM, mark_stopped)
    except ValueError:
        yield lambda: False
        return
    try:
        yield lambda: interrupted
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _open_existing_collector_profile(root: Path) -> LocalEngineContext:
    if not isinstance(root, Path):
        raise CollectorProfileError("Brain root must be a path")
    candidate = root.expanduser().absolute()
    if not candidate.exists() or candidate.is_symlink():
        raise CollectorProfileError("portable identity is missing")
    try:
        absolute_root = candidate.resolve(strict=True)
        metadata = absolute_root.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise CollectorProfileError("Brain root is unavailable")
        identity = _collector_identity_from_brain_toml((absolute_root / "brain.toml").read_bytes())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise CollectorProfileError("portable identity is unavailable") from error
    return LocalEngineContext(
        root=absolute_root,
        root_identity=(metadata.st_dev, metadata.st_ino),
        tenant_id=_collector_string(identity, "tenant_id"),
        owner_actor_id=_collector_string(identity, "owner_actor_id"),
        owner_role_claim=MappingProxyType(cast(dict[str, object], identity["owner_role_claim"])),
        provider_mode=ProviderMode.NONE,
        starter_spaces=(),
    )


def _path_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _collector_identity_from_brain_toml(payload: bytes) -> dict[str, object]:
    value = tomllib.loads(payload.decode("utf-8"))
    if not isinstance(value, dict) or value.get("layout_version") != 1:
        raise CollectorProfileError("portable identity is invalid")
    if value.get("profile") != "single-user-local":
        raise CollectorProfileError("portable identity is invalid")
    capabilities = value.get("owner_capabilities")
    if capabilities != ["canonical.publish", "capture.accept", "space.write"]:
        raise CollectorProfileError("portable identity is invalid")
    identity: dict[str, object] = {
        "tenant_id": value.get("tenant_id"),
        "owner_actor_id": value.get("owner_actor_id"),
        "owner_role_claim": {
            "actor_id": value.get("owner_actor_id"),
            "capabilities": tuple(capabilities),
            "role_claim_id": value.get("owner_role_claim_id"),
            "role_id": value.get("owner_role_id"),
            "tenant_id": value.get("tenant_id"),
        },
    }
    for key in ("tenant_id", "owner_actor_id"):
        _collector_string(identity, key)
    role_claim = cast(dict[str, object], identity["owner_role_claim"])
    for key in ("actor_id", "role_claim_id", "role_id", "tenant_id"):
        _collector_string(role_claim, key)
    return identity


def _collector_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if type(item) is not str or not item:
        raise CollectorProfileError("portable identity is invalid")
    return item


def _intake_from_fixture(
    value: object,
    selection: SourceResourceSelection,
) -> SourceRecordIntake:
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid collector fixture runtime")
    return SourceRecordIntake(
        key=SourceRecordKey(
            connector_name=selection.connector_name,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id=_fixture_str(value, "external_id"),
            revision_id=_fixture_str(value, "revision_id"),
        ),
        url=_fixture_str(value, "url"),
        title=_fixture_optional_str(value, "title"),
        text=_fixture_str(value, "text"),
        privacy=PrivacyDecision.from_dict(
            {
                "authority": {"cloud": False, "external_egress": True},
                "confirmation_ref": None,
                "policy_version": "privacy-v1",
                "reason": "policy_public",
                "tier": "public",
            }
        ),
    )


def _local_runtime_page_exists(
    root: Path,
    selection: SourceResourceSelection,
    cursor: str | None,
) -> bool:
    path = root / f"{_runtime_page_name(selection)}.json"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return False
    return isinstance(payload, Mapping) and payload.get("cursor") == cursor


def _selection_from_entry(entry: Mapping[str, object]) -> SourceResourceSelection:
    return SourceResourceSelection(
        connector_name=cast(str, entry["connector_name"]),
        connection_id=cast(str, entry["connection_id"]),
        resource_id=cast(str, entry["resource_id"]),
        resource_type=cast(str, entry["resource_type"]),
    )


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


def _github_repository(selection: SourceResourceSelection) -> tuple[str, str]:
    if selection.resource_type != "repository" or not selection.resource_id.startswith("repo:"):
        raise ConnectorContractError("invalid github selection")
    owner, separator, repository = selection.resource_id.removeprefix("repo:").partition("/")
    if not owner or separator != "/" or not repository:
        raise ConnectorContractError("invalid github selection")
    return owner, repository


def _github_page_number(cursor: str | None) -> int:
    if cursor is None:
        return 1
    try:
        value = int(cursor)
    except ValueError as error:
        raise ConnectorContractError("collector_runtime_unavailable") from error
    if value < 1 or value > 10_000:
        raise ConnectorContractError("collector_runtime_unavailable")
    return value


def _looks_like_github_comment(value: Mapping[str, object]) -> bool:
    return "issue_url" in value and "id" in value


def _fixture_str(value: Mapping[object, object], key: str) -> str:
    item = value.get(key)
    if type(item) is not str or not item:
        raise ConnectorContractError("invalid collector fixture runtime")
    return item


def _fixture_optional_str(value: Mapping[object, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if type(item) is not str or not item:
        raise ConnectorContractError("invalid collector fixture runtime")
    return item


def _runtime_page_name(selection: SourceResourceSelection) -> str:
    return SourceRecordKey(
        connector_name=selection.connector_name,
        connection_id=selection.connection_id,
        resource_id=selection.resource_id,
        external_id="collector-runtime",
        revision_id="v1",
    ).revision_identity()
