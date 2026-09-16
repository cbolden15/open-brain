from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import Request

import pytest
from open_brain_engine.engine import PrivacyDecision

from open_brain.services.local_entrypoints import run_cli as brain_cli
from open_brain_collector.cli import main as collector_cli_main
from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorLease,
    CollectorLeaseError,
    CollectorRunPage,
    CollectorSourceRuntime,
    CollectorStateStore,
    CollectorStorageError,
    CredentialStatusProvider,
    MemoryCaptureSink,
)
from open_brain_collector.runner import DispatchingSourceRuntime
from open_brain_connectors.runtime.github import GitHubUserTokenStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


def test_second_collector_contender_is_rejected_until_owner_releases(tmp_path: Path) -> None:
    lease_path = tmp_path / "run" / "collector.lock"
    first = CollectorLease(lease_path)
    second = CollectorLease(lease_path)

    with first.acquire(owner="github.fixture"), pytest.raises(
        CollectorLeaseError,
        match="collector_already_owned",
    ), second.acquire(owner="github.fixture"):
        pass

    with second.acquire(owner="github.fixture"):
        assert json.loads(lease_path.read_text(encoding="utf-8"))["owner"] == "github.fixture"
    assert not lease_path.exists()


def test_stale_collector_lease_from_dead_pid_is_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_path = tmp_path / "run" / "collector.lock"
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text(
        json.dumps({"owner": "github.fixture", "pid": 424242, "schema_version": 1}) + "\n",
        encoding="utf-8",
    )

    def dead_pid(pid: int, signal: int) -> None:
        assert pid == 424242
        assert signal == 0
        raise ProcessLookupError

    monkeypatch.setattr("open_brain_collector.lifecycle.os.kill", dead_pid)

    with CollectorLease(lease_path).acquire(owner="github.fixture"):
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
    assert not lease_path.exists()


def test_live_collector_lease_is_not_stolen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_path = tmp_path / "run" / "collector.lock"
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text(
        json.dumps({"owner": "github.fixture", "pid": 424242, "schema_version": 1}) + "\n",
        encoding="utf-8",
    )

    def live_pid(pid: int, signal: int) -> None:
        assert pid == 424242
        assert signal == 0

    monkeypatch.setattr("open_brain_collector.lifecycle.os.kill", live_pid)

    with (
        pytest.raises(CollectorLeaseError, match="collector_already_owned"),
        CollectorLease(lease_path).acquire(owner="github.fixture"),
    ):
        pass
    assert lease_path.exists()


def test_locked_and_missing_credentials_record_actionable_failure_without_fetch(
    tmp_path: Path,
) -> None:
    clock = _Clock(100)
    controller = CollectorController(CollectorStateStore(tmp_path / "state.json"), clock=clock)
    selection = _selection()
    source = _Source(selection)
    sink = MemoryCaptureSink()
    controller.enable(source_id="github.fixture", selection=selection, interval_seconds=30)

    locked = controller.sync_due(
        source_id="github.fixture",
        runtime=source,
        capture_sink=sink,
        credential_status=_Credentials("locked"),
    )
    missing = controller.sync_due(
        source_id="github.fixture",
        runtime=source,
        capture_sink=sink,
        credential_status=_Credentials("missing"),
    )

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    last_run = state["sources"]["github.fixture"]["last_run"]
    assert locked.outcome == "failed"
    assert locked.failure_code == "credential_locked"
    assert missing.failure_code == "credential_missing"
    assert last_run["failure_code"] == "credential_missing"
    assert source.fetches == 0
    assert sink.delivery_ids == []
    assert "secret" not in json.dumps(state).lower()


def test_storage_failure_is_bounded_and_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(src: str, dst: str) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr("open_brain_collector.lifecycle.os.replace", fail_replace)
    store = CollectorStateStore(tmp_path / "state.json")
    with pytest.raises(CollectorStorageError, match="collector_storage_unavailable"):
        store.save({"schema_version": 1, "sources": {}})

    assert not list(tmp_path.glob(".state.json.*.tmp"))
    assert not (tmp_path / "state.json").exists()


def test_run_once_cli_reports_storage_failure_as_bounded_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_replace(src: str, dst: str) -> None:
        raise OSError("No space left on device")

    brain_root = tmp_path / "brain"
    state = tmp_path / "collector" / "state.json"
    lease = tmp_path / "collector" / "collector.lock"
    runtime = tmp_path / "collector" / "runtime.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text('{"records":[]}\n', encoding="utf-8")
    assert brain_cli(("init", "--data-dir", str(brain_root), "--json")) == 0
    capsys.readouterr()
    controller = CollectorController(CollectorStateStore(state), clock=_Clock(100))
    controller.enable(source_id="github.fixture", selection=_selection(), interval_seconds=30)
    monkeypatch.setattr("open_brain_collector.lifecycle.os.replace", fail_replace)

    exit_code = collector_cli_main(
        [
            "run-once",
            "--state",
            str(state),
            "--brain-root",
            str(brain_root),
            "--lease",
            str(lease),
            "--fixture-runtime-json",
            str(runtime),
        ],
    )

    captured = capsys.readouterr()
    assert exit_code == 78
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "failure_code": "storage_unavailable",
        "outcome": "failed",
        "results": [],
    }


def test_dispatching_runtime_uses_github_adapter_and_credential_ref(
    tmp_path: Path,
) -> None:
    state = tmp_path / "collector" / "state.json"
    credential_dir = tmp_path / "credentials"
    credential = GitHubUserTokenStore(credential_dir).save_from_response(
        {
            "access_token": "ghu_fixtureAccessToken",
            "expires_in": 3600,
            "refresh_token": "ghr_fixtureRefreshToken",
            "refresh_token_expires_in": 7200,
            "scope": "",
            "token_type": "bearer",
        },
        account_login="cbolden15",
    )
    controller = CollectorController(CollectorStateStore(state), clock=_Clock(100))
    selection = _selection()
    controller.enable(
        source_id="github.fixture",
        selection=selection,
        credential_ref=credential.credential_ref,
        interval_seconds=30,
    )
    requested: list[str] = []

    def fake_http_get(request: Request) -> _Response:
        assert request.headers["Authorization"] == "Bearer ghu_fixtureAccessToken"
        requested.append(request.full_url)
        return _Response(
            json.dumps(
                [
                    {
                        "body": "Synthetic live-dispatch issue body.",
                        "html_url": "https://github.com/cbolden15/open-brain-fixture/issues/70",
                        "number": 70,
                        "title": "Live dispatch",
                        "updated_at": "2026-09-15T12:00:00Z",
                    }
                ]
            ).encode("utf-8")
        )

    runtime = DispatchingSourceRuntime(
        state_path=state,
        local_runtime_root=tmp_path / "collector" / "runtime",
        credential_dir=credential_dir,
        http_get=fake_http_get,
    )
    sink = MemoryCaptureSink()

    result = controller.sync_due(
        source_id="github.fixture",
        runtime=runtime,
        capture_sink=sink,
        credential_status=runtime,
    )

    assert result.captured_count == 1
    assert sink.delivery_ids[0].startswith("connector.github.")
    assert requested == [
        "https://api.github.com/repos/cbolden15/open-brain-fixture/issues?state=all&per_page=25&page=1"
    ]


def test_run_once_reports_missing_credential_ref_without_fetch_or_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brain_root = tmp_path / "brain"
    state = tmp_path / "collector" / "state.json"
    lease = tmp_path / "collector" / "collector.lock"
    assert brain_cli(("init", "--data-dir", str(brain_root), "--json")) == 0
    capsys.readouterr()
    controller = CollectorController(CollectorStateStore(state), clock=_Clock(100))
    controller.enable(source_id="github.fixture", selection=_selection(), interval_seconds=30)

    exit_code = collector_cli_main(
        [
            "run-once",
            "--state",
            str(state),
            "--brain-root",
            str(brain_root),
            "--lease",
            str(lease),
            "--credential-dir",
            str(tmp_path / "credentials"),
            "--force",
        ],
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert payload["results"][0]["failure_code"] == "credential_missing"
    assert "ghu_" not in captured.out


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Credentials(CredentialStatusProvider):
    def __init__(self, status: str) -> None:
        self._status = status

    def status_for(self, source_id: str) -> str:
        assert source_id == "github.fixture"
        return self._status


class _Source(CollectorSourceRuntime):
    def __init__(self, selection: SourceResourceSelection) -> None:
        self.selection = selection
        self.fetches = 0

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        self.fetches += 1
        return CollectorRunPage(selection=selection, intakes=(self._intake(),), next_cursor=None)

    def _intake(self) -> SourceRecordIntake:
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name="github",
                connection_id=self.selection.connection_id,
                resource_id=self.selection.resource_id,
                external_id="issue:70",
                revision_id="updated:2026-09-15T00:00:00Z",
            ),
            url="https://github.com/cbolden15/open-brain-fixture/issues/70",
            title="D3 failure fixture",
            text="Synthetic D3 failure fixture body.",
            privacy=_privacy(),
        )


def _selection() -> SourceResourceSelection:
    return SourceResourceSelection(
        connector_name="github",
        connection_id="account:cbolden15",
        resource_id="repo:cbolden15/open-brain-fixture",
        resource_type="repository",
    )


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": True},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "policy_public",
            "tier": "public",
        }
    )


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload
