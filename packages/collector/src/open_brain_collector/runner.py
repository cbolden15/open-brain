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

from open_brain_collector.capture_health import CaptureHealthStore
from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorLease,
    CollectorLeaseError,
    CollectorRunPage,
    CollectorSourceRuntime,
    CollectorStateStore,
    CredentialStatusProvider,
    EngineCaptureSink,
    EngineRevisionSink,
)
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.github import GitHubSourceAdapter, GitHubUserTokenStore
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = [
    "CollectorProcessRunner",
    "DispatchingSourceRuntime",
    "FixtureSourceRuntime",
    "LocalSourceRuntime",
    "collector_capture_sink",
    "collector_revision_sink",
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
                results = self._sync_and_record(
                    controller=controller,
                    source_id=source_id,
                    force=force,
                )
        except CollectorLeaseError:
            return {"outcome": "failed", "failure_code": "collector_already_owned", "results": []}
        return {"outcome": "completed", "failure_code": None, "results": results}

    def run_until_caught_up(
        self,
        *,
        max_iterations: int = 500,
        max_seconds: int = 900,
    ) -> dict[str, object]:
        """Drain acknowledged continuations under one lease, retaining work at bounds."""
        if (
            type(max_iterations) is not int
            or not 1 <= max_iterations <= 10_000
            or type(max_seconds) is not int
            or not 1 <= max_seconds <= 86_400
        ):
            raise ConnectorContractError("invalid collector catch-up bounds")
        controller = CollectorController(
            CollectorStateStore(self.state_path), clock=self.clock, brain_root=self.brain_root
        )
        health = CaptureHealthStore(self.state_path.parent / "capture-health", clock=self.clock)
        latest: dict[str, dict[str, object]] = {}
        totals: dict[str, dict[str, int]] = {}
        usage_totals: dict[str, int] = {}
        unknown_usage: set[str] = set()
        usage_fields = ("input_tokens", "output_tokens", "cost_microusd")
        iterations = 0
        outcome, reason = "completed", "caught_up"

        def accumulate(results: list[dict[str, object]]) -> None:
            if not results:
                return
            receipt = health.record(results, persist=False)
            for index, result in enumerate(results):
                key = str(result.get("source_id", index))
                latest[key] = result
                counts = totals.setdefault(key, {"captured_count": 0, "duplicate_count": 0})
                counts["captured_count"] += cast(
                    int, result.get("captured_count", result.get("captures", 0))
                )
                counts["duplicate_count"] += cast(int, result.get("duplicate_count", 0))
            measured = cast(dict[str, int], receipt["metrics"])
            for field in usage_fields:
                for result in results:
                    usage = result.get("usage", {})
                    if (
                        result.get(field, usage.get(field) if isinstance(usage, dict) else None)
                        is None
                    ):
                        unknown_usage.add(field)
                        break
                if field in measured:
                    usage_totals[field] = usage_totals.get(field, 0) + measured[field]

        def summary() -> list[dict[str, object]]:
            return [
                {
                    **{
                        key: value
                        for key, value in result.items()
                        if key not in {*usage_fields, "usage"}
                    },
                    **totals[source],
                }
                for source, result in latest.items()
            ]

        def measured_usage() -> dict[str, int]:
            return {
                field: value for field, value in usage_totals.items() if field not in unknown_usage
            }

        try:
            with (
                _collector_stop_signal() as stop,
                CollectorLease(self.lease_path).acquire(owner=self.owner),
            ):
                deadline = time.monotonic() + max_seconds
                try:
                    while True:
                        results = self._sync_sources(
                            controller=controller,
                            source_id=None,
                            force=False,
                            process_developments=False,
                        )
                        iterations += 1
                        accumulate(results)
                        if any(result.get("budget_exhausted") is True for result in results):
                            outcome, reason = "deferred", "processing_budget"
                            break
                        due = [
                            epoch
                            for result in latest.values()
                            if (epoch := self._continuation_epoch(result)) is not None
                        ]
                        if not due:
                            if any(
                                result.get("outcome") in {"failed", "partial", "deferred"}
                                or result.get("held", 0)
                                or result.get("failed", 0)
                                for result in latest.values()
                            ):
                                outcome, reason = "deferred", "attention_required"
                            break
                        if iterations >= max_iterations:
                            outcome, reason = "deferred", "iteration_budget"
                            break
                        delay = max(0, min(due) - self.clock())
                        if time.monotonic() + delay >= deadline:
                            outcome, reason = "deferred", "deadline_budget"
                            break
                        while delay > 0 and not stop():
                            time.sleep(min(delay, 1))
                            delay = max(0, min(due) - self.clock())
                            if time.monotonic() >= deadline:
                                break
                        if stop() or time.monotonic() >= deadline:
                            outcome, reason = "deferred", "signal" if stop() else "deadline_budget"
                            break
                    if reason == "caught_up":
                        if stop() or time.monotonic() >= deadline:
                            outcome, reason = "deferred", "signal" if stop() else "deadline_budget"
                        else:
                            processing = self._process_live()
                            accumulate(processing)
                            if any(result.get("budget_exhausted") is True for result in processing):
                                outcome, reason = "deferred", "processing_budget"
                            elif any(
                                result.get("outcome") in {"failed", "partial", "deferred"}
                                for result in processing
                            ):
                                outcome, reason = "deferred", "attention_required"
                except Exception:
                    health.record(
                        summary(), failure_code="collector_run_failed", metrics=measured_usage()
                    )
                    raise
                health.record(
                    summary(), metrics=measured_usage(),
                    execution_outcome="deferred" if outcome == "deferred" else None,
                )
        except CollectorLeaseError:
            return {
                "outcome": "failed",
                "failure_code": "collector_already_owned",
                "iterations": iterations,
                "stop_reason": "owned",
                "results": [],
            }
        final = summary()
        return {
            "outcome": outcome,
            "failure_code": None,
            "iterations": iterations,
            "stop_reason": reason,
            "results": final,
            "captured_count": sum(cast(int, result["captured_count"]) for result in final),
            "duplicate_count": sum(cast(int, result["duplicate_count"]) for result in final),
            "usage": measured_usage(),
        }

    def _continuation_epoch(self, result: dict[str, object]) -> int | None:
        retry = result.get("retry_at")
        if type(retry) is int:
            return retry
        more = result.get("has_more") is True or result.get("next_cursor") is not None
        if more:
            due = result.get("next_run_epoch")
            return due if type(due) is int else self.clock() + 1
        if result.get("failure_code") in {
            "rate_limited",
            "provider_unavailable",
            "provider_failed",
        }:
            due = result.get("next_run_epoch")
            return due if type(due) is int else None
        return None

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
                            "results": self._sync_and_record(
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

    def _sync_and_record(
        self, *, controller: CollectorController, source_id: str | None, force: bool,
    ) -> list[dict[str, object]]:
        health = CaptureHealthStore(self.state_path.parent / "capture-health", clock=self.clock)
        try:
            results = self._sync_sources(controller=controller, source_id=source_id, force=force)
        except Exception:
            health.record([], failure_code="collector_run_failed")
            raise
        health.record(results)
        return results

    def _sync_sources(
        self,
        *,
        controller: CollectorController,
        source_id: str | None,
        force: bool,
        process_developments: bool = True,
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
            return (
                self._sync_live(process_developments=process_developments)
                if source_id is None
                else []
            )
        capture_sink = EngineCaptureSink(collector_capture_sink(self.brain_root))
        credential_status = (
            self.runtime if isinstance(self.runtime, CredentialStatusProvider) else None
        )
        results = [
            controller.sync_due(
                source_id=selected_id,
                runtime=self.runtime,
                capture_sink=(collector_revision_sink(
                    self.brain_root, self.state_path.parent / "revisions"
                ) if isinstance(sources[selected_id], dict) and
                    cast(dict[str, object], sources[selected_id]).get("connector_name")
                    == "saved_markdown" else capture_sink),
                credential_status=credential_status,
            ).to_dict()
            for selected_id in source_ids
        ]
        return results + (
            self._sync_live(process_developments=process_developments) if source_id is None else []
        )

    def _sync_live(self, *, process_developments: bool = True) -> list[dict[str, object]]:
        root = self.state_path.parent / "live"
        if not (root / "capture" / "brain.json").exists():
            return []
        from open_brain_collector.live_manager import LiveSourceManager

        manager = LiveSourceManager(root, self.brain_root, background=self.background)
        manager.capture._clock = self.clock
        return manager.sync_due(process_developments=process_developments)

    def _process_live(self) -> list[dict[str, object]]:
        root = self.state_path.parent / "live"
        if not (root / "capture" / "brain.json").exists():
            return []
        from open_brain_collector.live_manager import LiveSourceManager

        manager = LiveSourceManager(root, self.brain_root, background=self.background)
        if manager.workflow.configuration()["enabled"] is not True:
            return []
        return [{"source_id": "slack-development", **manager.workflow.process()}]


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


def collector_revision_sink(brain_root: Path, state_root: Path) -> EngineRevisionSink:
    """Inject per-item revision capabilities without exposing owner tasks."""
    capture = collector_capture_sink(brain_root)
    tasks = open_local_engine(_open_existing_collector_profile(brain_root))
    assert tasks.sources is not None
    return EngineRevisionSink(tasks.sources.public_revision_sink, capture,
                              store=PrivateJsonStore(state_root))


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
