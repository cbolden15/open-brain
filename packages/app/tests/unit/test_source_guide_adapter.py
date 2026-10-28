"""Optional collector adapter: trust, handshake, framing, deadlines and identity.

Protocol and failure cases use narrow synthetic process fixtures (small scripts
with a fixed interpreter); the final cases launch the installed workspace
collector to prove handshake parity and the read-only metadata path.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import PHASE1_STATE_SCHEMA_VERSION

from open_brain.services import source_guide_adapter as adapter
from open_brain.services.local_runtime_session import RUNTIME_SESSION_VERSION
from open_brain.services.source_guide_adapter import (
    CapturedRecord,
    GuideAdapterError,
    collector_handshake,
    list_accounts,
    read_guide_metadata,
    read_resource_inventory,
    recheck_collector_executable,
    resolve_captured_record,
    resolve_collector_executable,
)

CAPABILITIES = {
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
        "portable": 18,
    },
}
ACCOUNT = "account:" + "c" * 64
SOURCE_ID = "slack.0123456789abcdef01234567"


def _executable(tmp_path: Path, body: str, name: str = "collector") -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    path.write_text(f"#!{sys.executable} -I\nimport json, os, sys, time\n{body}\n")
    path.chmod(0o700)
    return path


def _literal(value: object) -> str:
    """Embed a JSON value in fixture source without relying on Python literal syntax."""
    return repr(json.dumps(value))


def _capabilities_script(value: object) -> str:
    return (
        "if sys.argv[1:] == ['--guide-capabilities-json']:\n"
        f"    print(json.dumps({json.dumps(value)}))\n"
        "    sys.exit(0)\n"
    )


def _assert_code(error: pytest.ExceptionInfo[GuideAdapterError], code: str) -> None:
    assert error.value.code == code


def test_executable_must_be_absolute_existing_and_owner_controlled(tmp_path: Path) -> None:
    with pytest.raises(GuideAdapterError) as error:
        resolve_collector_executable(Path("collector"))
    _assert_code(error, "invalid_request")
    with pytest.raises(GuideAdapterError) as error:
        resolve_collector_executable(tmp_path / "missing")
    _assert_code(error, "collector_unavailable")
    path = _executable(tmp_path, "pass")
    path.chmod(0o720)
    with pytest.raises(GuideAdapterError) as error:
        resolve_collector_executable(path)
    _assert_code(error, "unsafe_path")
    path.chmod(0o600)
    with pytest.raises(GuideAdapterError) as error:
        resolve_collector_executable(path)
    _assert_code(error, "unsafe_path")
    path.chmod(0o700)
    path.parent.chmod(0o770)
    with pytest.raises(GuideAdapterError) as error:
        resolve_collector_executable(path)
    _assert_code(error, "unsafe_path")


@pytest.mark.parametrize(
    ("mode", "uid", "trusted"),
    [
        (0o41777, 0, True),
        (0o40777, 0, False),
        (0o41777, None, False),
    ],
)
def test_ancestor_trust_admits_only_root_owned_sticky_shared_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int, uid: int | None, trusted: bool
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    owner = os.getuid() if uid is None else uid
    real_lstat = os.lstat

    def fake_lstat(item: object) -> os.stat_result:
        info = real_lstat(item)  # type: ignore[arg-type]
        if Path(cast(str, item)) != shared:
            return info
        fields = list(info)
        fields[0], fields[4] = mode, owner
        return os.stat_result(fields)

    directory = shared / "bin"
    directory.mkdir(mode=0o700)
    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", fake_lstat)
        if trusted:
            adapter._trusted_directory_chain(directory)
        else:
            with pytest.raises(GuideAdapterError) as error:
                adapter._trusted_directory_chain(directory)
            _assert_code(error, "unsafe_path")


def test_installer_symlink_records_canonical_target_and_detects_replacement(
    tmp_path: Path,
) -> None:
    target = _executable(tmp_path, "pass")
    links = tmp_path / "links"
    links.mkdir(mode=0o700)
    (links / "open-brain-collector").symlink_to(target)

    selected = resolve_collector_executable(links / "open-brain-collector")

    assert selected.path == target.resolve()
    assert len(selected.sha256) == 64
    recheck_collector_executable(selected)
    target.write_text(target.read_text() + "# changed\n")
    with pytest.raises(GuideAdapterError) as error:
        recheck_collector_executable(selected)
    _assert_code(error, "collector_changed")


def test_handshake_accepts_exact_capabilities_and_ignores_extra_operations(
    tmp_path: Path,
) -> None:
    extra = {**CAPABILITIES, "operations": [*cast(list[str], CAPABILITIES["operations"]), "x.y"]}
    selected = resolve_collector_executable(_executable(tmp_path, _capabilities_script(extra)))

    assert collector_handshake(selected) == frozenset(cast(list[str], CAPABILITIES["operations"]))


@pytest.mark.parametrize(
    "value",
    [
        {**CAPABILITIES, "package": "open-brain"},
        {**CAPABILITIES, "guide_protocols": [2]},
        {**CAPABILITIES, "metadata_protocols": [True]},
        {**CAPABILITIES, "lifetime_guard": "none"},
        {**CAPABILITIES, "compatibility": {"schema": 17, "runtime": 12, "portable": 12}},
        {**CAPABILITIES, "operations": ["sources.accounts"]},
        {key: item for key, item in CAPABILITIES.items() if key != "compatibility"},
        {**CAPABILITIES, "unexpected": True},
        [CAPABILITIES],
    ],
)
def test_handshake_refuses_wrong_package_protocol_or_coordinates(
    tmp_path: Path, value: object
) -> None:
    selected = resolve_collector_executable(_executable(tmp_path, _capabilities_script(value)))
    with pytest.raises(GuideAdapterError) as error:
        collector_handshake(selected)
    _assert_code(error, "collector_incompatible")


@pytest.mark.parametrize(
    "output",
    [
        "print(json.dumps(CAP)); print(json.dumps(CAP))",
        "print(json.dumps(CAP)[:-1])",
        "print(json.dumps(CAP).replace('\"schema_version\": 1', '\"schema_version\": NaN'))",
        "print(json.dumps(CAP)[:-1] + ', \"package\": \"open-brain-collector\"}')",
        "print('x' * 20000)",
        "sys.stdout.buffer.write(b'\\xff\\xfe'); sys.exit(0)",
        "sys.exit(3)",
    ],
)
def test_handshake_refuses_invalid_framing(tmp_path: Path, output: str) -> None:
    body = f"CAP = {json.dumps(CAPABILITIES)}\n{output}"
    selected = resolve_collector_executable(_executable(tmp_path, body))
    with pytest.raises(GuideAdapterError) as error:
        collector_handshake(selected)
    _assert_code(error, "collector_incompatible")


def test_stderr_flood_is_drained_bounded_and_refused(tmp_path: Path) -> None:
    body = (
        "sys.stderr.write('e' * 8_000_000); sys.stderr.flush()\n"
        f"print(json.dumps({json.dumps(CAPABILITIES)}))"
    )
    selected = resolve_collector_executable(_executable(tmp_path, body))
    started = time.monotonic()
    with pytest.raises(GuideAdapterError) as error:
        collector_handshake(selected)
    _assert_code(error, "collector_incompatible")
    assert time.monotonic() - started < 10


def test_deadline_terminates_the_child_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "_HANDSHAKE_DEADLINE", 0.5)
    marker = tmp_path / "survived"
    body = f"time.sleep(5)\nopen({str(marker)!r}, 'w').close()"
    selected = resolve_collector_executable(_executable(tmp_path, body))
    started = time.monotonic()
    with pytest.raises(GuideAdapterError) as error:
        collector_handshake(selected)
    _assert_code(error, "collector_incompatible")
    assert time.monotonic() - started < 4
    time.sleep(0.2)
    assert not marker.exists()


def test_child_environment_and_descriptors_are_minimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-provider-key")
    monkeypatch.setenv("PYTHONPATH", "/synthetic/path")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    leaked = os.open(tmp_path / "leak", os.O_RDWR | os.O_CREAT, 0o600)
    os.set_inheritable(leaked, True)
    try:
        body = (
            "fds = sorted(int(n) for n in os.listdir('/dev/fd'))\n"
            "print(json.dumps({'accounts': [], 'env': dict(os.environ), 'fds': fds,"
            " 'argv': sys.argv[1:], 'cwd': os.getcwd()}))"
        )
        path = _executable(tmp_path, body)
        selected = resolve_collector_executable(path)
        guard = os.open(tmp_path / "guard", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            output = adapter._invoke(
                selected,
                "sources.accounts",
                state=tmp_path / "state.json",
                brain=tmp_path / "brain",
                guard_fd=guard,
            )
        finally:
            os.close(guard)
    finally:
        os.close(leaked)
    environment = cast(dict[str, str], output["env"])
    assert set(environment) - {"__CF_USER_TEXT_ENCODING"} == {
        "HOME",
        "PATH",
        "LANG",
        "PYTHONNOUSERSITE",
    }
    assert environment["LANG"] == "C.UTF-8"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert leaked not in cast(list[int], output["fds"])
    assert output["cwd"] == "/"
    argv = cast(list[str], output["argv"])
    guard_index = argv.index("--guide-guard-fd") + 1
    assert int(argv[guard_index]) in cast(list[int], output["fds"])
    assert argv == [
        "sources",
        "--state",
        str(tmp_path / "state.json"),
        "--brain-root",
        str(tmp_path / "brain"),
        "--guide-guard-fd",
        argv[guard_index],
        "--foreground",
        "accounts",
        "--provider",
        "slack",
    ]


def test_only_allowlisted_operations_and_safe_arguments_reach_the_child(tmp_path: Path) -> None:
    selected = resolve_collector_executable(_executable(tmp_path, "print('{}')"))
    for operation in ("sources.connect", "sources.control", "request", "sources.status"):
        with pytest.raises(GuideAdapterError) as error:
            adapter._invoke(
                selected,
                operation,
                state=tmp_path / "s.json",
                brain=tmp_path / "b",
                guard_fd=0,
            )
        _assert_code(error, "invalid_request")
    with pytest.raises(GuideAdapterError) as error:
        read_resource_inventory(
            selected,
            state=tmp_path / "s.json",
            brain=tmp_path / "b",
            guard_fd=0,
            connection_id="account:x\n--provider",
            cursor=None,
        )
    _assert_code(error, "invalid_request")
    with pytest.raises(GuideAdapterError) as error:
        read_guide_metadata(
            selected, state=Path("relative"), brain=tmp_path / "b", source_id=SOURCE_ID
        )
    _assert_code(error, "invalid_request")


def _pages_script(pages: dict[str | None, dict[str, object]]) -> str:
    return (
        f"PAGES = json.loads({_literal({str(key): value for key, value in pages.items()})})\n"
        "args = sys.argv[1:]\n"
        "cursor = args[args.index('--cursor') + 1] if '--cursor' in args else 'None'\n"
        "print(json.dumps(PAGES[cursor]))"
    )


def _resource(index: int, name: str = "general") -> dict[str, str]:
    return {"resource_id": f"channel:C{index:04d}", "name": name, "resource_type": "channel"}


def test_resource_inventory_reads_at_most_five_returned_pages(tmp_path: Path) -> None:
    pages: dict[str | None, dict[str, object]] = {
        None: {"resources": [_resource(0, "same‮name")], "next_cursor": "c1"}
    }
    for index in range(1, 7):
        pages[f"c{index}"] = {
            "resources": [_resource(index, "same‮name")],
            "next_cursor": f"c{index + 1}",
        }
    selected = resolve_collector_executable(_executable(tmp_path, _pages_script(pages)))

    inventory = read_resource_inventory(
        selected,
        state=tmp_path / "s.json",
        brain=tmp_path / "b",
        guard_fd=0,
        connection_id=ACCOUNT,
        cursor=None,
    )

    assert inventory["kind"] == "resources"
    rows = cast(list[dict[str, str]], inventory["rows"])
    assert [row["resource_id"] for row in rows] == [f"channel:C{i:04d}" for i in range(5)]
    assert rows[0]["name"] == "same‮name"
    assert inventory["has_more"] is True
    assert inventory["next_cursor"] == "c5"


def test_complete_inventory_has_no_continuation(tmp_path: Path) -> None:
    pages: dict[str | None, dict[str, object]] = {
        None: {"resources": [_resource(0)], "next_cursor": None}
    }
    selected = resolve_collector_executable(_executable(tmp_path, _pages_script(pages)))
    inventory = read_resource_inventory(
        selected,
        state=tmp_path / "s.json",
        brain=tmp_path / "b",
        guard_fd=0,
        connection_id=ACCOUNT,
        cursor=None,
    )
    assert inventory == {
        "kind": "resources",
        "rows": [_resource(0)],
        "next_cursor": None,
        "has_more": False,
    }


@pytest.mark.parametrize(
    "pages",
    [
        {None: {"resources": [_resource(1)], "next_cursor": "c1"},
         "c1": {"resources": [_resource(1, "renamed")], "next_cursor": None}},
        {None: {"resources": [{**_resource(1), "resource_type": "mail_label"}],
                "next_cursor": None}},
        {None: {"resources": [{**_resource(1), "alias": "x"}], "next_cursor": None}},
        {None: {"resources": [_resource(1)], "next_cursor": None, "extra": 1}},
        {None: {"resources": [_resource(1)], "next_cursor": ""}},
        {None: {"outcome": "failed", "failure_code": "rate_limited"}},
    ],
)
def test_ambiguous_or_malformed_inventory_fails_closed(
    tmp_path: Path, pages: dict[str | None, dict[str, object]]
) -> None:
    selected = resolve_collector_executable(_executable(tmp_path, _pages_script(pages)))
    with pytest.raises(GuideAdapterError):
        read_resource_inventory(
            selected,
            state=tmp_path / "s.json",
            brain=tmp_path / "b",
            guard_fd=0,
            connection_id=ACCOUNT,
            cursor=None,
        )


def test_accounts_are_returned_rows_and_duplicate_ids_refuse(tmp_path: Path) -> None:
    account = {
        "provider": "slack",
        "connection_id": ACCOUNT,
        "display_name": "Same label",
        "scopes": ["channels:read"],
        "expires_at_epoch": None,
    }
    other = {**account, "connection_id": "account:" + "d" * 64}
    script = f"print(json.dumps({{'accounts': json.loads({_literal([account, other])})}}))"
    selected = resolve_collector_executable(_executable(tmp_path, script))
    result = list_accounts(selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0)
    assert result == {
        "kind": "accounts",
        "rows": [
            {"connection_id": ACCOUNT, "display_name": "Same label", "alias": None},
            {"connection_id": "account:" + "d" * 64, "display_name": "Same label", "alias": None},
        ],
        "has_more": False,
    }
    script = f"print(json.dumps({{'accounts': json.loads({_literal([account, account])})}}))"
    selected = resolve_collector_executable(_executable(tmp_path, script, "duplicate"))
    with pytest.raises(GuideAdapterError) as error:
        list_accounts(selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0)
    _assert_code(error, "operation_failed")


def _binding(**change: object) -> dict[str, object]:
    return {
        "receipt_id": "custody:" + "1" * 64,
        "source_id": SOURCE_ID,
        "generation": "a" * 64,
        "control_epoch": 0,
        "binding_verified": True,
        "current_generation": True,
        "supported_slack_observation": True,
        "outcome": "captured",
        "capture_id": "capture_synthetic",
        **change,
    }


class Record:
    def __init__(self, summary: dict[str, object], text: str = "Synthetic body") -> None:
        self.summary = summary
        self.text = text


def _summary(**change: object) -> dict[str, object]:
    return {
        "record_id": "capture_synthetic",
        "record_type": "source",
        "revision_id": "capture_synthetic",
        "source_id": "source_synthetic",
        **change,
    }


def test_captured_record_resolution_uses_the_exact_bound_capture() -> None:
    reads: list[str] = []

    def read(capture_id: str) -> Record:
        reads.append(capture_id)
        return Record(_summary())

    result = resolve_captured_record(
        receipt_id="custody:" + "1" * 64,
        retained_receipt_ids=("custody:" + "1" * 64,),
        selected_source_id=SOURCE_ID,
        bindings=[_binding()],
        read_record=read,
    )

    assert result == CapturedRecord(
        receipt_id="custody:" + "1" * 64,
        capture_id="capture_synthetic",
        source_id="source_synthetic",
        revision_id="capture_synthetic",
        body="Synthetic body",
        current=True,
    )
    assert reads == ["capture_synthetic"]


@pytest.mark.parametrize(
    ("retained", "bindings", "summary"),
    [
        ((), [_binding()], _summary()),
        (("custody:" + "1" * 64,), [], _summary()),
        (("custody:" + "1" * 64,), [_binding(), _binding()], _summary()),
        (("custody:" + "1" * 64,), [_binding(source_id="slack.other")], _summary()),
        (("custody:" + "1" * 64,), [_binding(supported_slack_observation=False)], _summary()),
        (("custody:" + "1" * 64,), [_binding(binding_verified=False)], _summary()),
        (("custody:" + "1" * 64,), [_binding(outcome="quarantined")], _summary()),
        (("custody:" + "1" * 64,), [_binding(capture_id=None)], _summary()),
        (("custody:" + "1" * 64,), [_binding()], _summary(revision_id="capture_newer")),
        (("custody:" + "1" * 64,), [_binding()], _summary(record_id="capture_other")),
        (("custody:" + "1" * 64,), [_binding()], _summary(record_type="canonical")),
    ],
)
def test_unbound_or_mismatched_receipts_stay_unresolved(
    retained: tuple[str, ...], bindings: list[dict[str, object]], summary: dict[str, object]
) -> None:
    with pytest.raises(GuideAdapterError) as error:
        resolve_captured_record(
            receipt_id="custody:" + "1" * 64,
            retained_receipt_ids=retained,
            selected_source_id=SOURCE_ID,
            bindings=bindings,
            read_record=lambda _capture: Record(summary),
        )
    _assert_code(error, "receipt_unresolved")


def test_refused_record_read_is_never_inferred() -> None:
    def refuse(_capture: str) -> Record:
        raise RuntimeError("not_found")

    with pytest.raises(GuideAdapterError) as error:
        resolve_captured_record(
            receipt_id="custody:" + "1" * 64,
            retained_receipt_ids=("custody:" + "1" * 64,),
            selected_source_id=SOURCE_ID,
            bindings=[_binding()],
            read_record=refuse,
        )
    _assert_code(error, "receipt_unresolved")


def _installed_collector() -> Path:
    path = Path(sys.executable).parent / "open-brain-collector"
    if not path.is_file():
        pytest.skip("workspace collector is not installed")
    return path


def test_installed_collector_handshake_matches_core_coordinates() -> None:
    selected = resolve_collector_executable(_installed_collector())
    assert collector_handshake(selected) == frozenset(cast(list[str], CAPABILITIES["operations"]))


def test_installed_collector_metadata_reads_absent_state_without_creating_it(
    tmp_path: Path,
) -> None:
    from open_brain.profile import compile_single_user_local

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    selected = resolve_collector_executable(_installed_collector())
    before = sorted(str(path) for path in tmp_path.rglob("*"))

    report = read_guide_metadata(
        selected, state=tmp_path / "collector" / "state.json", brain=brain, source_id=SOURCE_ID
    )

    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert cast(dict[str, object], report["snapshot"])["consistency"] == "uninitialized"
    assert cast(dict[str, object], report["source"])["source_id"] == SOURCE_ID


def test_invalid_health_shape_from_child_is_refused(tmp_path: Path) -> None:
    selected = resolve_collector_executable(
        _executable(tmp_path, "print(json.dumps({'schema_version': 1}))")
    )
    with pytest.raises(GuideAdapterError) as error:
        read_guide_metadata(
            selected, state=tmp_path / "s.json", brain=tmp_path / "b", source_id=SOURCE_ID
        )
    _assert_code(error, "operation_failed")


def test_timed_out_read_is_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "_METADATA_DEADLINE", 0.5)
    selected = resolve_collector_executable(_executable(tmp_path, "time.sleep(5)"))
    with pytest.raises(GuideAdapterError) as error:
        read_guide_metadata(
            selected, state=tmp_path / "s.json", brain=tmp_path / "b", source_id=SOURCE_ID
        )
    _assert_code(error, "operation_unknown")


def _valid_health() -> dict[str, object]:
    return {
        "schema_version": 1,
        "snapshot": {
            "consistency": "per_store",
            "global_consistency": "unknown",
            "retryable": False,
            "reason_codes": ["state_absent"],
            "evidence_tokens": [{"store": "source", "token": "f" * 64}],
        },
        "account": {
            "registration": "registered",
            "connection_id": ACCOUNT,
            "display_name": "Synthetic workspace",
            "credential_status": "unknown",
            "last_access_failure_code": None,
            "last_access_failure_epoch": None,
        },
        "source": {
            "source_id": SOURCE_ID,
            "generation": "a" * 64,
            "control_epoch": 0,
            "configured_status": "disabled",
            "activity": "manual_idle",
            "schedule_running": "unknown",
            "stored_next_run_epoch": None,
        },
        "pending_preview": {
            "status": "present",
            "preview_id": "preview:" + "b" * 64,
            "count": 1,
            "has_more": True,
            "coverage_codes": ["partial_page"],
        },
        "execution": {
            "outcome": "completed",
            "finished_epoch": 1_900_000_000,
            "captured_count": 1,
            "duplicate_count": 0,
            "quarantined_count": 0,
            "last_successful_completion_epoch": 1_900_000_000,
            "failure_code": None,
        },
        "coverage": {"status": "partial", "has_more": True, "codes": ["partial_page"]},
        "recovery": {
            "retry_deadline_epoch": None,
            "retry_deadline_proven": False,
            "unresolved_receipt_count": 0,
        },
        "custody": {
            "scope": "selected_source",
            "status": "available",
            "counts": {
                "pending": 0,
                "captured": 1,
                "duplicate": 0,
                "quarantined": 0,
                "history_only": 0,
            },
            "retained_items": 1,
        },
        "receipt_bindings": [_binding()],
        "processing_backlog": {
            "scope": "global",
            "status": "unavailable",
            "reason_code": "not_read_by_v1",
        },
        "release": {"status": "unavailable", "reason_code": "app_workflow_owned"},
    }


def test_valid_closed_health_is_accepted() -> None:
    report = _valid_health()
    assert adapter._validate_health(report) is report


def _set(path: tuple[str | int, ...], value: object) -> dict[str, object]:
    report = _valid_health()
    target: object = report
    for key in path[:-1]:
        target = cast(dict[str, object], target)[key] if isinstance(key, str) else cast(
            list[object], target
        )[key]
    if isinstance(target, dict):
        target[cast(str, path[-1])] = value
    else:
        cast(list[object], target)[cast(int, path[-1])] = value
    return report


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("coverage", "codes"), ["complete"]),
        (("pending_preview", "coverage_codes"), ["partial_page"] * 26),
        (("snapshot", "reason_codes"), ["healthy"]),
        (("snapshot", "reason_codes"), ["state_absent"] * 17),
        (("custody", "counts", "pending"), True),
        (("custody", "counts", "extra"), 0),
        (("execution", "captured_count"), -1),
        (("execution", "finished_epoch"), -5),
        (("execution", "finished_epoch"), True),
        (("execution", "failure_code"), "provider said no"),
        (("account", "display_name"), "x" * 513),
        (("account", "display_name"), "evil\u001b[2J"),
        (("account", "connection_id"), ""),
        (("source", "source_id"), "slack.\u0007"),
        (("source", "control_epoch"), 1.5),
        (("snapshot", "evidence_tokens"), [{"store": "source", "token": "f" * 64}] * 8),
        (("snapshot", "evidence_tokens", 0, "store"), "journal"),
        (("snapshot", "evidence_tokens", 0, "token"), "F" * 64),
        (("snapshot", "retryable"), 0),
        (("recovery", "unresolved_receipt_count"), False),
        (("receipt_bindings", 0, "capture_id"), "x" * 513),
        (("receipt_bindings", 0, "extra"), True),
        (("snapshot", "extra"), True),
        (("extra",), True),
        (("release", "reason_code"), "ready"),
    ],
)
def test_health_dto_violations_are_refused(path: tuple[str | int, ...], value: object) -> None:
    with pytest.raises(GuideAdapterError) as error:
        adapter._validate_health(_set(path, value))
    _assert_code(error, "operation_failed")


def _echo_script(result: object, *, code: int = 0) -> str:
    """Record argv and any arguments-file body, then print one canned result."""
    return (
        "args = sys.argv[1:]\n"
        "body = None\n"
        "if '--arguments-file' in args:\n"
        "    body = open(args[args.index('--arguments-file') + 1]).read()\n"
        "if '--selection-file' in args:\n"
        "    body = open(args[args.index('--selection-file') + 1]).read()\n"
        "open(os.path.join(os.path.dirname(sys.argv[0]), 'argv.json'), 'w').write("
        "json.dumps({'argv': args, 'body': body}))\n"
        f"print(json.dumps(json.loads({_literal(result)})))\n"
        f"sys.exit({code})"
    )


def _recorded(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads((path.parent / "argv.json").read_text()))


def _common_argv(tmp_path: Path, argv: list[str]) -> list[str]:
    index = argv.index("--guide-guard-fd") + 1
    assert argv[:index - 1] == [
        "sources", "--state", str(tmp_path / "s.json"), "--brain-root", str(tmp_path / "b")
    ]
    assert argv[index + 1] == "--foreground"
    return argv[index + 2:]


CONFIGURED = {
    "source_id": SOURCE_ID,
    "selection": {
        "connector_name": "slack",
        "connection_id": ACCOUNT,
        "resource_id": "channel:C0001",
        "resource_type": "channel",
    },
    "options": {"date_floor": "2025-10-01T00:00:00Z", "privacy_tier": "work"},
    "status": "disabled",
    "interval_seconds": 14400,
    "next_run_epoch": None,
    "pause_ack_epoch": None,
    "last_run": None,
}


def _arguments_file(tmp_path: Path, value: object) -> Path:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "configure.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, json.dumps(value).encode())
    os.close(fd)
    return path


def test_configure_passes_only_the_private_arguments_file_and_guard(tmp_path: Path) -> None:
    path = _executable(tmp_path, _echo_script(CONFIGURED))
    selected = resolve_collector_executable(path)
    request = {"selection": CONFIGURED["selection"], "options": CONFIGURED["options"]}
    arguments = _arguments_file(tmp_path, request)

    result = adapter.configure_source(
        selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0,
        arguments_file=arguments,
    )

    assert result == CONFIGURED
    recorded = _recorded(path)
    argv = cast(list[str], recorded["argv"])
    assert _common_argv(tmp_path, argv) == ["configure", "--arguments-file", str(arguments)]
    assert json.loads(cast(str, recorded["body"])) == request


@pytest.mark.parametrize(
    "result",
    [
        {**CONFIGURED, "extra": 1},
        {**CONFIGURED, "source_id": "slack.\n"},
        {**CONFIGURED, "status": "running"},
        {"source_id": SOURCE_ID},
    ],
)
def test_configure_result_after_success_exit_is_unknown_outcome(
    tmp_path: Path, result: dict[str, object]
) -> None:
    selected = resolve_collector_executable(_executable(tmp_path, _echo_script(result)))
    with pytest.raises(GuideAdapterError) as error:
        adapter.configure_source(
            selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0,
            arguments_file=_arguments_file(tmp_path, {}),
        )
    _assert_code(error, "operation_unknown")


def test_configure_requires_a_private_arguments_file(tmp_path: Path) -> None:
    selected = resolve_collector_executable(_executable(tmp_path, _echo_script(CONFIGURED)))
    loose = tmp_path / "loose.json"
    loose.write_text("{}")
    # Explicit: the process umask is global and may be 077 while another test runs.
    loose.chmod(0o644)
    with pytest.raises(GuideAdapterError) as error:
        adapter.configure_source(
            selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0,
            arguments_file=loose,
        )
    _assert_code(error, "invalid_request")
    assert not (selected.path.parent / "argv.json").exists()


@pytest.mark.parametrize(
    ("native", "code"),
    [
        ("source_selection_reset_required", "source_reset_required"),
        ("rate_limited", "retry_wait"),
        ("collector_custody_quota_exceeded", "capacity_reached"),
        ("source_stale_preview", "authority_refused"),
        ("source_privacy_unresolved", "authority_refused"),
        ("source_invalid_arguments", "invalid_request"),
        ("provider said no", "operation_failed"),
    ],
)
def test_native_refusals_map_to_closed_codes(tmp_path: Path, native: str, code: str) -> None:
    script = _echo_script({"outcome": "failed", "failure_code": native}, code=78)
    selected = resolve_collector_executable(_executable(tmp_path, script))
    with pytest.raises(GuideAdapterError) as error:
        adapter.preview_source(
            selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0,
            source_id=SOURCE_ID,
        )
    _assert_code(error, code)
    assert error.value.native == native


PREVIEW = {
    "source_id": SOURCE_ID,
    "preview_id": "preview:" + "a" * 64,
    "count": 1,
    "has_more": True,
    "notices": ["partial_page"],
    "records": [
        {"title": "t", "source_reference": "https://example.slack.com/x", "trust": "unverified"}
    ],
}


def test_preview_drops_source_references_and_keeps_counts(tmp_path: Path) -> None:
    path = _executable(tmp_path, _echo_script(PREVIEW))
    selected = resolve_collector_executable(path)
    result = adapter.preview_source(
        selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0, source_id=SOURCE_ID
    )
    assert result == {
        "source_id": SOURCE_ID,
        "preview_id": PREVIEW["preview_id"],
        "count": 1,
        "has_more": True,
        "notices": ["partial_page"],
    }
    assert "slack.com" not in json.dumps(result)
    argv = cast(list[str], _recorded(path)["argv"])
    assert _common_argv(tmp_path, argv) == ["preview", "--source-id", SOURCE_ID]


@pytest.mark.parametrize(
    "result",
    [
        {**PREVIEW, "source_id": "slack.other"},
        {**PREVIEW, "count": -1},
        {**PREVIEW, "has_more": 1},
        {**PREVIEW, "preview_id": "preview:\u001b"},
    ],
)
def test_preview_mismatch_is_unknown_outcome(tmp_path: Path, result: dict[str, object]) -> None:
    selected = resolve_collector_executable(_executable(tmp_path, _echo_script(result)))
    with pytest.raises(GuideAdapterError) as error:
        adapter.preview_source(
            selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0,
            source_id=SOURCE_ID,
        )
    _assert_code(error, "operation_unknown")


IMPORTED = {
    "source_id": SOURCE_ID,
    "preview_id": PREVIEW["preview_id"],
    "outcome": "completed",
    "captured_count": 1,
    "duplicate_count": 0,
    "quarantined_count": 0,
    "receipt_ids": ["custody:" + "1" * 64],
    "finished_epoch": 1_760_000_000,
    "has_more": False,
    "notices": [],
    "failure_code": None,
}


def test_import_returns_exact_receipts_for_the_exact_pair(tmp_path: Path) -> None:
    path = _executable(tmp_path, _echo_script(IMPORTED))
    selected = resolve_collector_executable(path)
    result = adapter.import_preview(
        selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0,
        source_id=SOURCE_ID, preview_id=cast(str, PREVIEW["preview_id"]),
    )
    assert result == IMPORTED
    argv = cast(list[str], _recorded(path)["argv"])
    assert _common_argv(tmp_path, argv) == [
        "import", "--source-id", SOURCE_ID, "--preview-id", PREVIEW["preview_id"]
    ]


@pytest.mark.parametrize(
    "result",
    [
        {**IMPORTED, "preview_id": "preview:" + "b" * 64},
        {**IMPORTED, "outcome": "failed"},
        {**IMPORTED, "receipt_ids": ["custody:x", "custody:x"]},
        {**IMPORTED, "receipt_ids": ["custody:" + str(index) for index in range(26)]},
        {**IMPORTED, "captured_count": True},
        {key: value for key, value in IMPORTED.items() if key != "receipt_ids"},
    ],
)
def test_import_mismatch_is_unknown_outcome(tmp_path: Path, result: dict[str, object]) -> None:
    selected = resolve_collector_executable(_executable(tmp_path, _echo_script(result)))
    with pytest.raises(GuideAdapterError) as error:
        adapter.import_preview(
            selected, state=tmp_path / "s.json", brain=tmp_path / "b", guard_fd=0,
            source_id=SOURCE_ID, preview_id=cast(str, PREVIEW["preview_id"]),
        )
    _assert_code(error, "operation_unknown")


def test_writes_require_the_lifetime_guard(tmp_path: Path) -> None:
    selected = resolve_collector_executable(_executable(tmp_path, _echo_script(PREVIEW)))
    with pytest.raises(GuideAdapterError) as error:
        adapter._invoke(
            selected, "sources.preview", state=tmp_path / "s.json", brain=tmp_path / "b",
            arguments={"source_id": SOURCE_ID},
        )
    _assert_code(error, "invalid_request")
    assert not (selected.path.parent / "argv.json").exists()


def test_metadata_selection_locator_uses_a_private_selection_file(tmp_path: Path) -> None:
    report = {"schema_version": 1}
    path = _executable(tmp_path, _echo_script(report))
    selected = resolve_collector_executable(path)
    selection = _arguments_file(tmp_path, CONFIGURED["selection"])
    with pytest.raises(GuideAdapterError):
        read_guide_metadata(
            selected, state=tmp_path / "s.json", brain=tmp_path / "b", selection_file=selection
        )
    argv = cast(list[str], _recorded(path)["argv"])
    assert argv == [
        "sources", "--state", str(tmp_path / "s.json"), "--brain-root", str(tmp_path / "b"),
        "--foreground", "guide-metadata", "--selection-file", str(selection),
    ]
    with pytest.raises(GuideAdapterError) as error:
        read_guide_metadata(selected, state=tmp_path / "s.json", brain=tmp_path / "b")
    _assert_code(error, "invalid_request")
    with pytest.raises(GuideAdapterError) as error:
        read_guide_metadata(
            selected, state=tmp_path / "s.json", brain=tmp_path / "b",
            source_id=SOURCE_ID, selection_file=selection,
        )
    _assert_code(error, "invalid_request")


def test_portable_coordinate_matches_catalog_and_collector() -> None:
    from open_brain.services import catalog

    source = Path(catalog.__file__).read_text()
    assert f'"portable_metadata": {adapter.PORTABLE_METADATA},' in source
    collector = pytest.importorskip("open_brain_collector.source_metadata")
    assert collector.GUIDE_CAPABILITIES["compatibility"] == {
        "schema": PHASE1_STATE_SCHEMA_VERSION,
        "runtime": RUNTIME_SESSION_VERSION,
        "portable": adapter.PORTABLE_METADATA,
    }
