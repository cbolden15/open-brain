"""W5 guide metadata: noncreating, non-bootstrapping and truthful selected-source health."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import socket
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import PHASE1_STATE_SCHEMA_VERSION

from open_brain.profile import compile_single_user_local
from open_brain.services.local_runtime_session import RUNTIME_SESSION_VERSION
from open_brain_collector import cli, source_metadata, sources_cli
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_collector.live_manager import LiveSourceManager, PriorityRuntime
from open_brain_collector.source_metadata import read_source_metadata
from open_brain_connectors.runtime import slack_auth
from open_brain_connectors.runtime.live_common import LiveBatch, local_source_privacy
from open_brain_connectors.runtime.live_http import LiveHttpTransport
from open_brain_connectors.runtime.live_storage import OsCredentialStore, PrivateJsonStore
from open_brain_connectors.runtime.slack_auth import _credential_reference
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

ACCOUNT = "account:" + "c" * 64
SELECTION = SourceResourceSelection("slack", ACCOUNT, "channel:CSYNTHETIC", "channel")
SOURCE_ID = LiveSourceManager._source_id(SELECTION)
HEALTH_KEYS = {
    "schema_version",
    "snapshot",
    "account",
    "source",
    "pending_preview",
    "execution",
    "coverage",
    "recovery",
    "custody",
    "receipt_bindings",
    "processing_backlog",
    "release",
}


def _intake(text: str = "Syntheticguidebody", *, ts: str = "100.001") -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey("slack", ACCOUNT, SELECTION.resource_id, ts, "r1"),
        "https://example.slack.com/archives/CSYNTHETIC/p" + ts.replace(".", ""),
        text,
        local_source_privacy(),
        title="Synthetic guide message",
    )


class Runtime:
    def __init__(self, *intakes: SourceRecordIntake, has_more: bool = False) -> None:
        self.batch = LiveBatch(intakes or (_intake(),), {"cursor": "1"}, has_more)

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        return self.batch

    def acknowledge(
        self, selection: SourceResourceSelection, checkpoint: dict[str, object]
    ) -> None:
        pass


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    return tmp_path / "live", brain


def _service(live: Path, brain: Path, runtime: Runtime) -> LiveCaptureService:
    return LiveCaptureService(live / "capture", brain, runtime=runtime)


def _account(live: Path, name: str = "Synthetic workspace") -> None:
    root = live / "accounts" / "slack"
    store = PrivateJsonStore(root)
    store.write(
        "slack-account-" + "c" * 64 + ".json",
        {
            "account": {
                "provider": "slack",
                "connection_id": ACCOUNT,
                "display_name": name,
                "scopes": ["channels:history", "channels:read", "groups:history", "groups:read"],
                "expires_at_epoch": None,
            },
            "credential_ref": _credential_reference(ACCOUNT, root),
            "schema_version": 1,
        },
    )


def _configured(tmp_path: Path, runtime: Runtime | None = None) -> tuple[Path, Path, Runtime]:
    live, brain = _layout(tmp_path)
    selected = runtime or Runtime()
    _service(live, brain, selected).configure(SOURCE_ID, SELECTION, {"privacy_tier": "work"})
    _account(live)
    return live, brain, selected


def _imported(tmp_path: Path, runtime: Runtime | None = None) -> tuple[Path, Path]:
    live, brain, selected = _configured(tmp_path, runtime)
    service = _service(live, brain, selected)
    preview = service.preview(SOURCE_ID)
    service.apply(SOURCE_ID, cast(str, preview["preview_id"]))
    return live, brain


def _snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    result: dict[str, tuple[object, ...]] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            path = Path(directory) / name
            info = path.lstat()
            digest = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file() and not path.is_symlink()
                else None
            )
            result[str(path.relative_to(root))] = (
                info.st_mode,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                digest,
            )
    return result


def _prohibit(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("prohibited during guide metadata")

    for owner, name in (
        (LiveSourceManager, "__init__"),
        (PriorityRuntime, "__init__"),
        (LiveCaptureService, "__init__"),
        (LiveCaptureService, "preview"),
        (PrivateJsonStore, "__init__"),
        (PrivateJsonStore, "lock"),
        (OsCredentialStore, "_run"),
        (LiveHttpTransport, "request"),
        (socket.socket, "connect"),
    ):
        monkeypatch.setattr(owner, name, refuse)
    for module, name in (
        (os, "mkdir"),
        (os, "makedirs"),
        (os, "chmod"),
        (os, "replace"),
        (os, "unlink"),
        (fcntl, "flock"),
        (sqlite3, "connect"),
    ):
        monkeypatch.setattr(module, name, refuse)


def _read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live: Path,
    brain: Path,
    *,
    source_id: str | None = None,
    selection: SourceResourceSelection | None = None,
) -> dict[str, object]:
    if selection is None and source_id is None:
        source_id = SOURCE_ID
    before = _snapshot(tmp_path)
    with monkeypatch.context() as patch:
        _prohibit(patch)
        report = read_source_metadata(
            live_root=live, brain_root=brain, source_id=source_id, selection=selection
        )
    assert _snapshot(tmp_path) == before
    assert set(report) == HEALTH_KEYS
    json.dumps(report, allow_nan=False)
    return report


def _section(report: dict[str, object], name: str) -> dict[str, object]:
    return cast(dict[str, object], report[name])


def test_absent_live_state_is_uninitialized_without_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain = _layout(tmp_path)

    report = _read(tmp_path, monkeypatch, live, brain)

    snapshot = _section(report, "snapshot")
    assert snapshot["consistency"] == "uninitialized"
    assert snapshot["global_consistency"] == "unknown"
    assert "state_absent" in cast(list[str], snapshot["reason_codes"])
    assert not live.exists()
    assert _section(report, "custody")["counts"] is None
    assert _section(report, "execution")["captured_count"] is None
    assert _section(report, "account")["registration"] == "unavailable"


def test_selection_locator_derives_existing_default_key_and_reports_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain = _layout(tmp_path)
    _service(live, brain, Runtime())
    _account(live)

    report = _read(tmp_path, monkeypatch, live, brain, selection=SELECTION)

    assert _section(report, "source")["source_id"] == SOURCE_ID
    assert _section(report, "source")["configured_status"] is None
    assert _section(report, "snapshot")["consistency"] == "uninitialized"
    assert _section(report, "account")["registration"] == "registered"


def test_exactly_one_locator_and_slack_selection_are_required(tmp_path: Path) -> None:
    live, brain = _layout(tmp_path)
    with pytest.raises(ValueError):
        read_source_metadata(live_root=live, brain_root=brain)
    with pytest.raises(ValueError):
        read_source_metadata(
            live_root=live, brain_root=brain, source_id=SOURCE_ID, selection=SELECTION
        )
    gmail = SourceResourceSelection("gmail", ACCOUNT, "label:INBOX", "mail_label")
    with pytest.raises(ValueError):
        read_source_metadata(live_root=live, brain_root=brain, selection=gmail)


def test_configured_manual_source_reports_unknown_execution_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain, _runtime = _configured(tmp_path)

    report = _read(tmp_path, monkeypatch, live, brain)

    assert _section(report, "snapshot")["consistency"] == "per_store"
    source = _section(report, "source")
    assert source["configured_status"] == "disabled"
    assert source["activity"] == "manual_idle"
    assert source["schedule_running"] == "unknown"
    assert source["control_epoch"] == 0
    assert _section(report, "pending_preview")["status"] == "absent"
    execution = _section(report, "execution")
    assert execution == {
        "outcome": "unknown",
        "finished_epoch": None,
        "captured_count": None,
        "duplicate_count": None,
        "quarantined_count": None,
        "last_successful_completion_epoch": None,
        "failure_code": None,
    }
    assert _section(report, "custody")["status"] == "unavailable"
    assert _section(report, "custody")["counts"] is None
    assert _section(report, "recovery") == {
        "retry_deadline_epoch": None,
        "retry_deadline_proven": False,
        "unresolved_receipt_count": None,
    }
    account = _section(report, "account")
    assert account["registration"] == "registered"
    assert account["display_name"] == "Synthetic workspace"
    assert account["credential_status"] == "unknown"
    assert "credential_ref" not in json.dumps(report)
    assert report["processing_backlog"] == {
        "scope": "global",
        "status": "unavailable",
        "reason_code": "not_read_by_v1",
    }
    assert report["release"] == {"status": "unavailable", "reason_code": "app_workflow_owned"}
    tokens = cast(list[dict[str, str]], _section(report, "snapshot")["evidence_tokens"])
    assert {item["store"] for item in tokens} == {"brain", "marker", "account", "source"}


def test_pending_preview_metadata_without_calling_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain, runtime = _configured(tmp_path, Runtime(has_more=True))
    preview = _service(live, brain, runtime).preview(SOURCE_ID)

    report = _read(tmp_path, monkeypatch, live, brain)

    pending = _section(report, "pending_preview")
    assert pending == {
        "status": "present",
        "preview_id": preview["preview_id"],
        "count": 1,
        "has_more": True,
        "coverage_codes": ["partial_page"],
    }
    assert _section(report, "source")["activity"] == "awaiting_inspection"
    assert "Syntheticguidebody" not in json.dumps(report)
    assert "example.slack.com" not in json.dumps(report)


def test_completed_partial_import_binds_supported_slack_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain = _imported(tmp_path, Runtime(has_more=True))

    report = _read(tmp_path, monkeypatch, live, brain)

    execution = _section(report, "execution")
    assert execution["outcome"] == "completed"
    assert execution["captured_count"] == 1
    assert execution["last_successful_completion_epoch"] == execution["finished_epoch"]
    coverage = _section(report, "coverage")
    assert coverage == {"status": "partial", "has_more": True, "codes": ["partial_page"]}
    custody = _section(report, "custody")
    assert custody["status"] == "available"
    assert cast(dict[str, int], custody["counts"])["captured"] == 1
    bindings = cast(list[dict[str, object]], report["receipt_bindings"])
    assert len(bindings) == 1
    binding = bindings[0]
    assert set(binding) == {
        "receipt_id",
        "source_id",
        "generation",
        "control_epoch",
        "binding_verified",
        "current_generation",
        "supported_slack_observation",
        "outcome",
        "capture_id",
    }
    assert binding["source_id"] == SOURCE_ID
    assert binding["binding_verified"] is True
    assert binding["current_generation"] is True
    assert binding["supported_slack_observation"] is True
    assert binding["outcome"] == "captured"
    assert cast(str, binding["capture_id"]).startswith("capture_")
    assert _section(report, "recovery")["unresolved_receipt_count"] == 0
    assert "Syntheticguidebody" not in json.dumps(report)


def test_other_source_receipts_are_never_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain = _imported(tmp_path)
    other = SourceResourceSelection("slack", ACCOUNT, "channel:COTHER", "channel")
    other_id = LiveSourceManager._source_id(other)
    service = _service(live, brain, Runtime())
    service.configure(other_id, other, {"privacy_tier": "work"})

    report = _read(tmp_path, monkeypatch, live, brain, source_id=other_id)

    assert report["receipt_bindings"] == []
    assert cast(dict[str, int], _section(report, "custody")["counts"])["captured"] == 0


def _rewrite_source(live: Path, change: dict[str, object]) -> None:
    capture = live / "capture"
    name = "source-" + hashlib.sha256(SOURCE_ID.encode()).hexdigest() + ".json"
    value = json.loads((capture / name).read_bytes())
    value.update(change)
    PrivateJsonStore(capture).write(name, value)


def test_failed_latest_run_hides_prior_success_and_retry_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain = _imported(tmp_path)
    _rewrite_source(
        live,
        {
            "status": "enabled",
            "next_run_epoch": 4_000_000_000,
            "last_run": {
                "outcome": "failed",
                "failure_code": "auth_required",
                "finished_epoch": 1_900_000_000,
            },
        },
    )

    report = _read(tmp_path, monkeypatch, live, brain)

    execution = _section(report, "execution")
    assert execution["outcome"] == "failed"
    assert execution["failure_code"] == "credentials_unavailable"
    assert execution["last_successful_completion_epoch"] is None
    assert execution["captured_count"] is None
    account = _section(report, "account")
    assert account["last_access_failure_code"] == "credentials_unavailable"
    assert account["last_access_failure_epoch"] == 1_900_000_000
    source = _section(report, "source")
    assert source["stored_next_run_epoch"] == 4_000_000_000
    assert source["activity"] == "unknown"
    assert _section(report, "recovery")["retry_deadline_epoch"] is None
    assert _section(report, "coverage") == {"status": "unknown", "has_more": None, "codes": []}


@pytest.mark.parametrize(
    ("native", "mapped", "access"),
    [
        ("not_allowed", "access_denied", True),
        ("credential_unavailable", "credentials_unavailable", True),
        ("rate_limited", "rate_limited", False),
        ("provider said <script>", "operation_failed", False),
    ],
)
def test_failure_codes_map_through_closed_enum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: str, mapped: str, access: bool
) -> None:
    live, brain, _runtime = _configured(tmp_path)
    _rewrite_source(
        live, {"last_run": {"outcome": "failed", "failure_code": native, "finished_epoch": 7}}
    )

    report = _read(tmp_path, monkeypatch, live, brain)

    assert _section(report, "execution")["failure_code"] == mapped
    assert _section(report, "account")["last_access_failure_code"] == (mapped if access else None)
    assert "<script>" not in json.dumps(report)


def test_legacy_and_forged_notices_never_become_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain = _imported(tmp_path)
    capture = live / "capture"
    name = "source-" + hashlib.sha256(SOURCE_ID.encode()).hexdigest() + ".json"
    last_run = json.loads((capture / name).read_bytes())["last_run"]
    legacy = {key: item for key, item in last_run.items() if key not in {"has_more", "notices"}}
    _rewrite_source(live, {"last_run": legacy})

    report = _read(tmp_path, monkeypatch, live, brain)
    assert _section(report, "coverage") == {
        "status": "unknown",
        "has_more": None,
        "codes": ["legacy_unknown"],
    }

    forged = {
        **last_run,
        "has_more": False,
        "notices": ["partial_thread_coverage", "forgedfullcoverage \u001b[2J", "message_deleted"],
    }
    _rewrite_source(live, {"last_run": forged})
    report = _read(tmp_path, monkeypatch, live, brain)
    assert _section(report, "coverage") == {
        "status": "partial",
        "has_more": False,
        "codes": ["thread_partial", "upstream_unknown"],
    }
    assert "forgedfullcoverage" not in json.dumps(report)


def test_disabled_source_with_stored_schedule_is_not_collecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain, _runtime = _configured(tmp_path)
    _rewrite_source(live, {"interval_seconds": 60})

    report = _read(tmp_path, monkeypatch, live, brain)

    source = _section(report, "source")
    assert source["activity"] == "manual_idle"
    assert source["schedule_running"] == "unknown"
    assert source["stored_next_run_epoch"] is None


def test_brain_binding_mismatch_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, _brain, _runtime = _configured(tmp_path)
    other = tmp_path / "other-brain"
    compile_single_user_local(other)

    report = _read(tmp_path, monkeypatch, live, other)

    snapshot = _section(report, "snapshot")
    assert snapshot["consistency"] == "unavailable"
    assert snapshot["reason_codes"] == ["binding_mismatch"]
    assert report["receipt_bindings"] == []
    assert _section(report, "source")["generation"] is None


def test_missing_brain_marker_is_uninitialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain, _runtime = _configured(tmp_path)
    (live / "capture" / "brain.json").unlink()

    report = _read(tmp_path, monkeypatch, live, brain)

    assert _section(report, "snapshot")["consistency"] == "uninitialized"
    assert "binding_absent" in cast(list[str], _section(report, "snapshot")["reason_codes"])


def _source_path(live: Path) -> Path:
    return live / "capture" / ("source-" + hashlib.sha256(SOURCE_ID.encode()).hexdigest() + ".json")


@pytest.mark.parametrize("case", ["mode", "hardlink", "symlink_dir", "fifo", "brain_mode"])
def test_unsafe_files_refuse_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    live, brain, _runtime = _configured(tmp_path)
    source = _source_path(live)
    if case == "mode":
        source.chmod(0o640)
    elif case == "hardlink":
        os.link(source, tmp_path / "alias.json")
    elif case == "symlink_dir":
        (live / "capture").rename(tmp_path / "moved")
        (live / "capture").symlink_to(tmp_path / "moved")
    elif case == "fifo":
        source.unlink()
        os.mkfifo(source, 0o600)
    else:
        brain.chmod(0o750)

    report = _read(tmp_path, monkeypatch, live, brain)

    snapshot = _section(report, "snapshot")
    assert snapshot["consistency"] == "unavailable"
    assert "unsafe_path" in cast(list[str], snapshot["reason_codes"])
    assert _section(report, "source")["configured_status"] is None
    if case == "mode":
        assert source.stat().st_mode & 0o777 == 0o640


def test_oversized_state_refuses_rather_than_truncates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain, _runtime = _configured(tmp_path)
    source = _source_path(live)
    with source.open("ab") as handle:
        handle.write(b" " * 4_194_305)

    report = _read(tmp_path, monkeypatch, live, brain)

    assert _section(report, "snapshot")["reason_codes"] == ["size_limit"]


def test_invalid_source_document_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain, _runtime = _configured(tmp_path)
    _rewrite_source(live, {"generation": "0" * 64})

    report = _read(tmp_path, monkeypatch, live, brain)

    assert _section(report, "snapshot")["consistency"] == "unavailable"
    assert _section(report, "snapshot")["reason_codes"] == ["invalid_state"]


def test_concurrent_replacement_yields_inconsistent_without_joins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain = _imported(tmp_path)
    original = source_metadata._verify_pass
    source = _source_path(live)

    def replace_then_verify(*args: object, **kwargs: object) -> object:
        raw = source.read_bytes()
        temporary = source.with_name("replacement.tmp")
        temporary.write_bytes(raw)
        temporary.chmod(0o600)
        temporary.rename(source)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_metadata, "_verify_pass", replace_then_verify)
    report = read_source_metadata(live_root=live, brain_root=brain, source_id=SOURCE_ID)

    snapshot = _section(report, "snapshot")
    assert snapshot["consistency"] == "inconsistent"
    assert snapshot["retryable"] is True
    assert snapshot["reason_codes"] == ["changed_during_read"]
    assert snapshot["evidence_tokens"] == []
    assert report["receipt_bindings"] == []
    assert _section(report, "custody")["counts"] is None


def test_interrupted_source_and_preview_pair_is_inconsistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain, runtime = _configured(tmp_path)
    preview = _service(live, brain, runtime).preview(SOURCE_ID)
    preview_path = live / "capture" / (cast(str, preview["preview_id"]).replace(":", "-") + ".json")
    value = json.loads(preview_path.read_bytes())
    value["before"] = "0" * 64
    PrivateJsonStore(live / "capture").write(preview_path.name, value)

    report = _read(tmp_path, monkeypatch, live, brain)

    assert _section(report, "pending_preview")["status"] == "inconsistent"
    assert _section(report, "pending_preview")["preview_id"] is None
    assert _section(report, "snapshot")["consistency"] == "inconsistent"
    assert "relation_mismatch" in cast(list[str], _section(report, "snapshot")["reason_codes"])


def _cli(argv: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = sources_cli.main(argv)
    return code, cast(dict[str, object], json.loads(output.getvalue()))


def test_cli_guide_metadata_dispatches_before_manager_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain = _imported(tmp_path)
    state = tmp_path / "state.json"
    before = _snapshot(tmp_path)
    with monkeypatch.context() as patch:
        _prohibit(patch)
        code, report = _cli(
            [
                "--state",
                str(state),
                "--brain-root",
                str(brain),
                "--foreground",
                "guide-metadata",
                "--source-id",
                SOURCE_ID,
            ]
        )
    assert _snapshot(tmp_path) == before
    assert code == 0
    assert set(report) == HEALTH_KEYS
    assert _section(report, "execution")["outcome"] == "completed"
    del live


def _selection_file(tmp_path: Path, value: object, mode: int = 0o600) -> Path:
    directory = tmp_path / "selection"
    directory.mkdir(mode=0o700)
    path = directory / "selection.json"
    path.write_text(json.dumps(value))
    path.chmod(mode)
    return path


def test_cli_selection_file_is_strict_and_owner_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain, _runtime = _configured(tmp_path)
    base = ["--state", str(tmp_path / "state.json"), "--brain-root", str(brain), "--foreground"]
    good = _selection_file(
        tmp_path,
        {
            "connector_name": "slack",
            "connection_id": ACCOUNT,
            "resource_id": SELECTION.resource_id,
            "resource_type": "channel",
        },
    )
    code, report = _cli([*base, "guide-metadata", "--selection-file", str(good)])
    assert code == 0
    assert _section(report, "source")["source_id"] == SOURCE_ID
    good.chmod(0o644)
    code, failure = _cli([*base, "guide-metadata", "--selection-file", str(good)])
    refused = {"outcome": "failed", "failure_code": "source_invalid_arguments"}
    assert (code, failure) == (78, refused)
    good.chmod(0o600)
    good.write_text('{"connector_name":"slack","connector_name":"slack"}')
    code, failure = _cli([*base, "guide-metadata", "--selection-file", str(good)])
    assert code == 78
    for argv in (
        [*base, "guide-metadata"],
        [*base, "guide-metadata", "--source-id", SOURCE_ID, "--selection-file", str(good)],
        [*base[:-1], "guide-metadata", "--source-id", SOURCE_ID],
        [*base, "guide-metadata", "--source-id", SOURCE_ID, "--guide-guard-fd", "0"],
    ):
        code, failure = _cli(argv)
        assert (code, failure) == (
            78,
            {"outcome": "failed", "failure_code": "source_invalid_arguments"},
        )
    del live


def test_cli_guide_metadata_refuses_abbreviations_and_equals_forms(tmp_path: Path) -> None:
    live, brain, _runtime = _configured(tmp_path)
    state = str(tmp_path / "state.json")
    other = str(tmp_path / "other" / "state.json")
    refused = (78, {"outcome": "failed", "failure_code": "source_invalid_arguments"})
    for argv in (
        ["--state", state, "--brain-root", str(brain), "--foreground", "guide-metadata",
         "--source", SOURCE_ID],
        ["--state", state, "--brain-root", str(brain), "--foreground", "guide-metadata",
         "--sel", str(tmp_path / "selection.json")],
        ["--state", state, "--state=" + other, "--brain-root", str(brain), "--foreground",
         "guide-metadata", "--source-id", SOURCE_ID],
        ["--state", state, "--brain-root", str(brain), "--brain-root=" + str(brain),
         "--foreground", "guide-metadata", "--source-id", SOURCE_ID],
        ["--state=" + state, "--brain-root", str(brain), "--foreground", "guide-metadata",
         "--source-id", SOURCE_ID],
    ):
        assert _cli(argv) == refused
    del live


def test_account_projection_never_reads_credential_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, brain, _runtime = _configured(tmp_path)

    def refuse(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("credential reference must not be derived or compared")

    assert not hasattr(source_metadata, "_credential_reference")
    monkeypatch.setattr(slack_auth, "_credential_reference", refuse)
    report = read_source_metadata(live_root=live, brain_root=brain, source_id=SOURCE_ID)

    assert _section(report, "account")["registration"] == "registered"


def test_cli_source_id_named_guide_metadata_keeps_legacy_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Manager:
        def __init__(self, *_args: object) -> None:
            pass

        def dispatch(self, operation: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((operation, arguments))
            return {"ok": True}

    monkeypatch.setattr(sources_cli, "LiveSourceManager", Manager)
    code, result = _cli(
        [
            "--state",
            str(tmp_path / "state.json"),
            "--brain-root",
            str(tmp_path / "brain"),
            "--foreground",
            "preview",
            "--source-id",
            "guide-metadata",
        ]
    )
    assert (code, result) == (0, {"ok": True})
    assert calls == [("sources.preview", {"source_id": "guide-metadata"})]


def test_guide_guard_fd_is_validated_and_never_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []

    class Manager:
        def __init__(self, *_args: object) -> None:
            pass

        def dispatch(self, operation: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append(arguments)
            return {"ok": True}

    monkeypatch.setattr(sources_cli, "LiveSourceManager", Manager)
    guard = tmp_path / "guard.lock"
    fd = os.open(guard, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        base = ["--state", str(tmp_path / "s.json"), "--brain-root", str(tmp_path / "b")]
        code, _ = _cli([*base, "--guide-guard-fd", str(fd), "--foreground", "custody-status"])
        assert code == 0
        assert calls == [{}]
        os.chmod(guard, 0o644)
        code, failure = _cli(
            [*base, "--guide-guard-fd", str(fd), "--foreground", "custody-status"]
        )
        assert (code, failure) == (
            78,
            {"outcome": "failed", "failure_code": "collector_guard_invalid"},
        )
        code, failure = _cli([*base, "--guide-guard-fd", "9999", "--foreground", "custody-status"])
        assert failure["failure_code"] == "collector_guard_invalid"
    finally:
        os.close(fd)
    assert len(calls) == 1


def test_guide_capabilities_are_inert_and_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()
    with monkeypatch.context() as patch:
        _prohibit(patch)
        patch.setattr(os, "open", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
        with redirect_stdout(output):
            code = cli.main(["--guide-capabilities-json"])
    assert code == 0
    assert json.loads(output.getvalue()) == {
        "schema_version": 1,
        "package": "open-brain-collector",
        "guide_protocols": [1],
        "metadata_protocols": [1],
        "lifetime_guard": "inherited-flock-v1",
        "operations": [
            "sources.accounts",
            "sources.resources",
            "sources.configure",
            "sources.preview",
            "sources.import",
            "sources.custody_inspect",
            "sources.guide_metadata",
        ],
        "compatibility": {
            "schema": PHASE1_STATE_SCHEMA_VERSION,
            "runtime": RUNTIME_SESSION_VERSION,
            "portable": 16,
        },
    }
