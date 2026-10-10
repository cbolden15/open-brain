"""Repeatable synthetic harness checks; fixture evidence never proves native inference."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

SCHEMA = "open-brain-harness-acceptance.v1"
CLIENTS = ("codex", "claude-code", "pi")
ROOT = Path(__file__).resolve().parents[2]
MAX_JSON = 2 * 1024 * 1024
MAX_AGE_SECONDS = 24 * 60 * 60
INCOMPLETE = (
    "native_nine_pairs_and_scenarios",
    "native_clean_account_installed_packages",
    "backup_restore_cutover_rollback",
    "merged_installed_versions_and_required_gates",
    "independent_review_and_private_handoff",
)
_UNIT = "packages/app/tests/unit/"
_INTEGRATION = "packages/app/tests/integration/services/"
# These existing tests assert outcomes at public processing/serving boundaries.
# Parametrizations must all pass; skips and absent cases are failures.
SCENARIOS = (
    _UNIT + "test_harness_memory.py::test_early_user_statement_preserves_exact_source_and_locator",
    _UNIT + "test_harness_memory.py::"
    "test_statement_kinds_and_later_changed_decision_remain_source_grounded",
    _UNIT + "test_harness_memory.py::"
    "test_questions_commands_chatter_guesses_and_injected_context_are_not_memories",
    _UNIT
    + "test_harness_memory.py::test_requested_recipe_retains_complete_assistant_authored_material",
    _UNIT
    + "test_harness_events.py::test_native_source_identity_survives_inserted_metadata_and_rotation",
    _UNIT + "test_harness_events.py::"
    "test_trailing_partial_record_is_tolerated_but_middle_corruption_is_refused",
    _UNIT + "test_harness_outbox.py::test_receipt_fsync_failure_preserves_custody_and_retry",
    _UNIT + "test_harness_outbox.py::"
    "test_latest_failure_metadata_survives_restart_without_mutating_source",
    _UNIT
    + "test_harness_outbox.py::test_fork_origin_is_retained_metadata_and_unknown_parent_is_visible",
    _INTEGRATION + "test_harness_workflow.py::"
    "test_unknown_and_marked_records_are_denied_cloud_but_local_project_read_works",
    _INTEGRATION
    + "test_harness_workflow.py::test_paraphrased_tasks_keep_recipe_details_and_source_resolution",
    _INTEGRATION + "test_harness_workflow.py::"
    "test_explicit_changed_decision_withholds_previous_value_even_when_query_only_matches_old",
    _INTEGRATION
    + "test_harness_bridge.py::test_credential_refusal_latches_metadata_without_source_custody",
    _INTEGRATION
    + "test_harness_bridge.py::test_explicit_local_capture_restriction_survives_queue_and_delivery",
    _INTEGRATION
    + "test_harness_bridge.py::test_consent_revocation_during_shutdown_blocks_content_release",
    _INTEGRATION
    + "test_harness_bridge.py::test_exclusion_refuses_before_named_transcript_or_sdk_reads",
    _INTEGRATION
    + "test_harness_bridge.py::test_cloud_caller_cannot_claim_local_only_even_with_private_project",
    _INTEGRATION + "test_harness_bridge.py::test_wrong_destination_is_refused_before_bootstrap",
    _INTEGRATION + "test_harness_hook.py::test_harness_hook_fixture",
    _INTEGRATION + "test_harness_setup.py::test_clean_home_install_idempotence_and_uninstall",
    _INTEGRATION + "test_harness_setup.py::test_preserves_unrelated_hooks_and_settings",
    _INTEGRATION
    + "test_harness_setup.py::test_pi_entry_subprocess_fixture_optout_and_private_denial",
)


class AcceptanceError(ValueError):
    """Closed metadata code, safe to print without exception details."""


def _require(condition: object, code: str) -> None:
    if not condition:
        raise AcceptanceError(code)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp(value: object) -> datetime:
    _require(isinstance(value, str), "invalid_timestamp")
    try:
        result = datetime.fromisoformat(str(value))
    except ValueError:
        raise AcceptanceError("invalid_timestamp") from None
    _require(result.tzinfo is not None, "invalid_timestamp")
    return result


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _read(path: Path, *, maximum: int = MAX_JSON) -> bytes:
    _require(path.is_absolute(), "absolute_private_path_required")
    _require(path.resolve() == path, "unsafe_evidence_path")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and not info.st_mode & 0o077
            and info.st_size <= maximum,
            "unsafe_or_oversized_evidence",
        )
        raw = stream.read(maximum + 1)
    _require(len(raw) <= maximum, "unsafe_or_oversized_evidence")
    return raw


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            _read(path), object_pairs_hook=_pairs, parse_constant=lambda _value: _invalid_constant()
        )
    except ValueError as error:
        if isinstance(error, AcceptanceError):
            raise
        raise AcceptanceError("invalid_evidence_json") from None
    _require(isinstance(value, dict), "invalid_evidence_json")
    return cast(dict[str, Any], value)


def _invalid_constant() -> None:
    raise AcceptanceError("invalid_evidence_json")


def _write(path: Path, value: object) -> None:
    _require(path.is_absolute(), "absolute_private_path_required")
    _require(
        path.parent.resolve() == path.parent and not path.is_relative_to(ROOT),
        "unsafe_evidence_path",
    )
    raw = _canonical(value)
    _require(len(raw) <= MAX_JSON, "unsafe_or_oversized_evidence")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _source_binding() -> tuple[str, str]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    # Bind all repository inputs, including conftests, imported fixtures and lockfiles.
    # Git excludes generated environments/evidence while retaining uncommitted source.
    inventory = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=5,
    ).stdout
    _require(len(inventory) <= MAX_JSON, "source_inventory_oversized")
    binding = {}
    for name in sorted(set(inventory.decode().split("\0")) - {""}):
        path = ROOT / name
        _require(path.is_file() and not path.is_symlink(), "unsafe_source_input")
        binding[name] = sha256(path.read_bytes()).hexdigest()
    return commit, _digest(binding)


def _reference(directory: Path, name: str, value: object) -> dict[str, str]:
    path = directory / name
    _write(path, value)
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _evidence(reference: object, directory: Path) -> dict[str, Any]:
    _require(
        isinstance(reference, dict) and set(reference) == {"path", "sha256"},
        "invalid_evidence_reference",
    )
    reference = cast(dict[str, str], reference)
    path = Path(reference["path"])
    _require(path.parent == directory and not path.is_symlink(), "evidence_path_mismatch")
    raw = _read(path)
    _require(sha256(raw).hexdigest() == reference["sha256"], "evidence_digest_mismatch")
    return _load(path)


def _fixture_event(
    project: Path, client: str, session: str, text: str, timestamp: str
) -> dict[str, object]:
    messages = [("user", text), *(("user", "Thanks for the information.") for _ in range(5))]
    if client == "pi":
        return {
            "session_id": session,
            "header_session_id": session,
            "project_path": str(project),
            "session_timestamp": timestamp,
            "messages": [
                {
                    "role": role,
                    "text": body,
                    "ordinal": index,
                    "source_id": f"entry-{index}",
                    "timestamp": timestamp,
                }
                for index, (role, body) in enumerate(messages)
            ],
        }
    records: list[dict[str, object]] = []
    if client == "codex":
        records.append(
            {
                "type": "session_meta",
                "payload": {"id": session, "cwd": str(project), "timestamp": timestamp},
            }
        )
    for index, (role, body) in enumerate(messages):
        if client == "codex":
            records.append(
                {
                    "type": "response_item",
                    "timestamp": timestamp,
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": [{"type": "input_text", "text": body}],
                    },
                }
            )
        else:
            records.append(
                {
                    "type": role,
                    "sessionId": session,
                    "cwd": str(project),
                    "uuid": f"message-{index}",
                    "timestamp": timestamp,
                    "message": {"role": role, "content": body},
                }
            )
    transcript = project / (session + ".jsonl")
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records))
    transcript.chmod(0o600)
    return {
        "session_id": session,
        "cwd": str(project),
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
    }


def _fixture_setup(root: Path) -> tuple[Path, Path, str]:
    from open_brain_engine.core.models import PrivacyTier

    from open_brain.local_data import select_local_root
    from open_brain.services.local_bootstrap import open_local_brain
    from open_brain.services.local_operations import current_brain_identity
    from open_brain.services.session_consent import DurableProviderConsentStore

    project = root / "synthetic-project"
    project.mkdir()
    authority = root / "authority"
    authority.mkdir(mode=0o700)
    brain = root / "brain"
    with open_local_brain(
        select_local_root(data_dir=str(brain), environment={"HOME": str(root)})
    ) as session:
        brain_id, epoch = current_brain_identity(session.tasks)
    store = DurableProviderConsentStore(
        authority / "consent.json", brain_id=brain_id, issuer_epoch=epoch
    )
    for client in CLIENTS:
        store.grant(
            provider_id=client,
            allowed_tiers=frozenset({PrivacyTier.WORK}),
            operation_id="grant-" + client,
            decided_at=_now(),
        )
    _write(
        authority / "policy.json",
        {
            "schema_version": "open-brain-harness-policy.v1",
            "enabled": True,
            "providers": {client: ["work"] for client in CLIENTS},
            "sources": [
                {
                    "id": "synthetic",
                    "project_root": str(project),
                    "tier": "work",
                    "local_only": False,
                }
            ],
            "exclusions": [],
        },
    )
    config = authority / "bridge.json"
    _write(
        config,
        {
            "schema_version": "open-brain-harness-bridge.v1",
            "brain_id": brain_id,
            "brain_root": str(brain),
            "policy": str(authority / "policy.json"),
            "consent_state": str(authority / "consent.json"),
            "outbox": str(authority / "outbox"),
        },
    )
    return config, project, brain_id


def _junit_checks(raw: bytes, expected_cases: set[str] | None = None) -> set[str]:
    _require(len(raw) <= MAX_JSON and b"<!DOCTYPE" not in raw, "invalid_scenario_evidence")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise AcceptanceError("invalid_scenario_evidence") from None
    seen: set[str] = set()
    instances: set[str] = set()
    cases = list(root.iter("testcase"))
    _require(cases, "missing_scenarios")
    for case in cases:
        _require(
            not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")),
            "failed_or_skipped_scenario",
        )
        name = case.attrib.get("name", "").split("[")[0]
        module = case.attrib.get("classname", "").replace(".", "/") + ".py"
        instance = module + "::" + case.attrib.get("name", "")
        _require(instance not in instances, "duplicate_scenario")
        instances.add(instance)
        seen.add(module + "::" + name)
    _require(set(SCENARIOS) <= seen, "missing_scenarios")
    if expected_cases is not None:
        _require(instances == expected_cases, "missing_scenario_parametrizations")
    return seen


def _collect_scenarios() -> set[str]:
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", *SCENARIOS],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _require(result.returncode == 0, "scenario_collection_failed")
    cases = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("packages/app/tests/") and "::" in line
    }
    _require(
        cases and {case.split("[")[0] for case in cases} == set(SCENARIOS), "missing_scenarios"
    )
    return cases


def _run_scenarios(directory: Path) -> dict[str, object]:
    collected = _collect_scenarios()
    junit = directory / "scenarios.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=no",
        "--no-header",
        "-o",
        "addopts=",
        "--junitxml=" + str(junit),
        *SCENARIOS,
    ]
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, timeout=120)
    _require(junit.is_file(), "missing_scenarios")
    junit.chmod(0o600)
    _require(process.returncode == 0, "scenario_run_failed")
    _junit_checks(_read(junit), collected)
    return {
        "command": command,
        "returncode": process.returncode,
        "required_nodeids": list(SCENARIOS),
        "collected_nodeids": sorted(collected),
        "junit": {"path": str(junit), "sha256": sha256(junit.read_bytes()).hexdigest()},
    }


def run_fixture(*, report_path: Path, evidence_dir: Path) -> dict[str, object]:
    """Create a new disposable Brain, exercise public interfaces, retain private evidence."""
    from open_brain.services.harness_bridge import run_harness
    from open_brain.services.harness_mcp import HarnessMcpAdapter

    _require(
        report_path.is_absolute() and evidence_dir.is_absolute(), "absolute_private_path_required"
    )
    _require(not report_path.exists() and not evidence_dir.exists(), "evidence_already_exists")
    evidence_dir.mkdir(mode=0o700)
    initial_binding = _source_binding()
    started = _now()
    sources: list[dict[str, object]] = []
    pairs: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="open-brain-harness-fixture-") as temporary:
        config, project, brain_id = _fixture_setup(Path(temporary).resolve())
        for index, client in enumerate(CLIENTS):
            session_id = "fixture-source-" + uuid4().hex
            text = f"My preferred editor is SyntheticEditor{index}."
            timestamp = _now()
            event = _fixture_event(project, client, session_id, text, timestamp)
            before = time.monotonic()
            queued = run_harness("enqueue", config, client, event)
            enqueue_seconds = time.monotonic() - before
            _require(queued.get("status") == "queued" and enqueue_seconds <= 2, "capture_failed")
            request_id = str(queued["request_id"])
            outbox = config.parent / "outbox"
            request = _load(outbox / "requests" / (request_id + ".json"))
            run_harness("drain", config, client)
            receipt = _load(outbox / "receipts" / (request_id + ".json"))
            capture_ids = receipt["acknowledgement"]["capture_ids"]
            _require(len(capture_ids) == 1, "capture_failed")
            capture_id = capture_ids[0]
            sources.append(
                {
                    "client": client,
                    "session_id": session_id,
                    "request_id": request_id,
                    "capture_id": capture_id,
                    "brain_id": brain_id,
                    "introduced_at": timestamp,
                    "acknowledged_at": _now(),
                    "enqueue_seconds": enqueue_seconds,
                    "expected_text_sha256": sha256(text.encode()).hexdigest(),
                    "request": _reference(evidence_dir, client + "-request.json", request),
                    "receipt": _reference(evidence_dir, client + "-receipt.json", receipt),
                }
            )
            # A repeated Stop and a new workflow instance must preserve the stable identity.
            _require(
                run_harness("enqueue", config, client, event).get("request_id") == request_id,
                "replay_identity_failed",
            )
            _require(
                run_harness("status", config, client).get("complete") is True, "custody_incomplete"
            )
            for destination in CLIENTS:
                fresh_session = "fixture-destination-" + uuid4().hex
                query = "Which coding tool should I use for editing?"
                payload = {
                    "cwd": str(project),
                    "project_path": str(project),
                    "session_id": fresh_session,
                    "query": query,
                }
                before = time.monotonic()
                recall = run_harness("recall", config, destination, payload)
                recall_seconds = time.monotonic() - before
                adapter = HarnessMcpAdapter(config, destination, project)
                search = adapter.call_tool("brain_search_page", {"query": "preferred editor"})
                source = adapter.call_tool("brain_read_source", {"capture_id": capture_id})
                name = client + "-to-" + destination
                pairs.append(
                    {
                        "source": client,
                        "destination": destination,
                        "fresh_session_id": fresh_session,
                        "capture_id": capture_id,
                        "observed_at": _now(),
                        "recall_seconds": recall_seconds,
                        "query_sha256": sha256(query.encode()).hexdigest(),
                        "observation": _reference(
                            evidence_dir,
                            name + ".json",
                            {
                                "request": payload,
                                "recall": recall,
                                "search": search,
                                "source": source,
                            },
                        ),
                    }
                )
    scenarios = _run_scenarios(evidence_dir)
    commit, tree = _source_binding()
    _require((commit, tree) == initial_binding, "source_changed_during_run")
    report = {
        "schema_version": SCHEMA,
        "mode": "fixture",
        "evidence_kind": "public-interface-fixture",
        "source_commit": commit,
        "source_tree_sha256": tree,
        "fixture_runtime_sha256": sha256(Path(sys.executable).read_bytes()).hexdigest(),
        "started_at": started,
        "finished_at": _now(),
        "evidence_dir": str(evidence_dir),
        "sources": sources,
        "pairs": pairs,
        "scenarios": scenarios,
        "incomplete_required_criteria": list(INCOMPLETE),
        "goal_complete": False,
    }
    _write(report_path, report)
    return validate_fixture(report_path=report_path)


def validate_fixture(*, report_path: Path) -> dict[str, object]:
    try:
        return _validate_fixture(report_path=report_path)
    except AcceptanceError:
        raise
    except OSError, ValueError, KeyError, TypeError, AttributeError:
        raise AcceptanceError("invalid_or_missing_evidence") from None


def _validate_fixture(*, report_path: Path) -> dict[str, object]:
    from open_brain.services.harness_outbox import _decode_event, _validate_receipt

    report = _load(report_path)
    _require(
        report.get("schema_version") == SCHEMA
        and report.get("mode") == "fixture"
        and report.get("evidence_kind") == "public-interface-fixture",
        "mode_mismatch",
    )
    _require(
        report.get("goal_complete") is False
        and report.get("incomplete_required_criteria") == list(INCOMPLETE),
        "promoted_claim",
    )
    commit, tree = _source_binding()
    _require(
        report.get("source_commit") == commit and report.get("source_tree_sha256") == tree,
        "source_binding_mismatch",
    )
    _require(
        report.get("fixture_runtime_sha256")
        == sha256(Path(sys.executable).read_bytes()).hexdigest(),
        "runtime_binding_mismatch",
    )
    started, finished = _timestamp(report.get("started_at")), _timestamp(report.get("finished_at"))
    now = datetime.now(UTC)
    _require(
        started <= finished <= now
        and (now - finished).total_seconds() <= MAX_AGE_SECONDS
        and (finished - started).total_seconds() <= 600,
        "stale_or_invalid_run",
    )
    directory = Path(report["evidence_dir"])
    _require(
        directory.is_absolute()
        and not directory.is_symlink()
        and directory.stat().st_uid == os.geteuid()
        and not directory.stat().st_mode & 0o077,
        "unsafe_evidence_directory",
    )
    sources = report.get("sources")
    _require(isinstance(sources, list) and len(sources) == 3, "missing_sources")
    sources = cast(list[dict[str, Any]], sources)
    by_client: dict[str, Any] = {}
    sessions: set[str] = set()
    captures: set[str] = set()
    requests: set[str] = set()
    text_hashes: set[str] = set()
    for row in sources:
        client = row["client"]
        _require(client in CLIENTS and client not in by_client, "duplicate_source")
        _require(
            row["session_id"] not in sessions
            and row["capture_id"] not in captures
            and row["request_id"] not in requests
            and row["expected_text_sha256"] not in text_hashes,
            "duplicate_source_identity",
        )
        sessions.add(row["session_id"])
        captures.add(row["capture_id"])
        requests.add(row["request_id"])
        text_hashes.add(row["expected_text_sha256"])
        introduced, acknowledged = (
            _timestamp(row["introduced_at"]),
            _timestamp(row["acknowledged_at"]),
        )
        _require(
            started <= introduced <= acknowledged <= finished
            and (acknowledged - introduced).total_seconds() <= 120,
            "delivery_deadline_failed",
        )
        _require(
            type(row["enqueue_seconds"]) in {int, float} and 0 <= row["enqueue_seconds"] <= 2,
            "enqueue_deadline_failed",
        )
        request = _evidence(row["request"], directory)
        receipt = _evidence(row["receipt"], directory)
        _decode_event(request)
        _validate_receipt(receipt, row["request_id"], row["brain_id"])
        _require(
            _digest(request) == row["request_id"]
            and request.get("client") == client
            and request.get("session_id") == row["session_id"]
            and request.get("brain_id") == row["brain_id"],
            "request_binding_mismatch",
        )
        _require(
            receipt.get("status") == "delivered"
            and receipt.get("request_id") == row["request_id"]
            and receipt.get("brain_id") == row["brain_id"]
            and receipt.get("acknowledgement", {}).get("status") == "acknowledged"
            and receipt["acknowledgement"].get("capture_ids") == [row["capture_id"]]
            and receipt["acknowledgement"].get("delivery_ids"),
            "receipt_binding_mismatch",
        )
        _require(
            any(
                sha256(message["text"].encode()).hexdigest() == row["expected_text_sha256"]
                and message.get("role") == "user"
                and message.get("authored") is True
                for message in request.get("messages", [])
            ),
            "fact_binding_mismatch",
        )
        by_client[client] = row
    pairs = report.get("pairs")
    _require(isinstance(pairs, list) and len(pairs) == 9, "missing_pairs")
    pairs = cast(list[dict[str, Any]], pairs)
    seen: set[tuple[str, str]] = set()
    for pair in pairs:
        key = (pair["source"], pair["destination"])
        _require(
            key[0] in CLIENTS and key[1] in CLIENTS and key not in seen, "duplicate_or_invalid_pair"
        )
        seen.add(key)
        row = by_client[key[0]]
        _require(pair["fresh_session_id"] not in sessions, "destination_session_not_fresh")
        sessions.add(pair["fresh_session_id"])
        _require(pair["capture_id"] == row["capture_id"], "capture_binding_mismatch")
        _require(
            _timestamp(row["acknowledged_at"]) <= _timestamp(pair["observed_at"]) <= finished,
            "observation_timestamp_mismatch",
        )
        _require(
            type(pair["recall_seconds"]) in {int, float} and 0 <= pair["recall_seconds"] <= 3,
            "recall_deadline_failed",
        )
        observation = _evidence(pair["observation"], directory)
        _require(
            observation["request"].get("session_id") == pair["fresh_session_id"]
            and sha256(observation["request"]["query"].encode()).hexdigest()
            == pair["query_sha256"],
            "observation_binding_mismatch",
        )
        for kind in ("recall", "search", "source"):
            response = observation[kind]
            if kind != "recall":
                _require(response.get("isError") is False, "tool_failed")
                response = response.get("structuredContent", {})
            results = response.get("results")
            _require(
                response.get("status") == "ok"
                and isinstance(results, list)
                and 1 <= len(results) <= 5,
                "missing_observed_results",
            )
            matching = [
                item
                for item in results
                if item.get("capture_id") == row["capture_id"]
                and sha256(item.get("text", "").encode()).hexdigest() == row["expected_text_sha256"]
            ]
            _require(len(matching) == 1, "observed_fact_mismatch")
            citation = json.loads(matching[0]["citation"], object_pairs_hook=_pairs)
            _require(
                citation.get("harness") == row["client"]
                and citation.get("session_id") == row["session_id"]
                and citation.get("capture_id") == row["capture_id"]
                and citation.get("authorship") == "user"
                and citation.get("timestamp") == row["introduced_at"]
                and type(citation.get("message_ordinal")) is int
                and citation.get("project_id")
                and citation.get("state") == "current",
                "citation_binding_mismatch",
            )
            if kind == "source":
                _require(
                    sha256(matching[0].get("source_context", "").encode()).hexdigest()
                    == row["expected_text_sha256"],
                    "source_context_mismatch",
                )
            if kind == "recall":
                context = "Open Brain untrusted reference data:\n" + "\n\n".join(
                    item["text"] + "\nSource: " + item["citation"] for item in results
                )
                _require(len(context.encode()) <= 8192, "context_budget_failed")
    scenarios = report.get("scenarios", {})
    _require(
        scenarios.get("returncode") == 0 and scenarios.get("required_nodeids") == list(SCENARIOS),
        "missing_scenarios",
    )
    expected_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=no",
        "--no-header",
        "-o",
        "addopts=",
        "--junitxml=" + str(directory / "scenarios.xml"),
        *SCENARIOS,
    ]
    _require(scenarios.get("command") == expected_command, "scenario_command_mismatch")
    reference = scenarios["junit"]
    path = Path(reference["path"])
    _require(path == directory / "scenarios.xml", "evidence_path_mismatch")
    raw = _read(path)
    _require(sha256(raw).hexdigest() == reference["sha256"], "evidence_digest_mismatch")
    collected = _collect_scenarios()
    _require(
        scenarios.get("collected_nodeids") == sorted(collected), "scenario_collection_mismatch"
    )
    _junit_checks(raw, collected)
    return {
        "status": "passed",
        "mode": "fixture",
        "pair_count": 9,
        "required_scenario_groups": len(SCENARIOS),
        "goal_complete": False,
        "incomplete_required_criteria": list(INCOMPLETE),
    }


def _native(*, report_path: Path, evidence_dir: Path | None, validate: bool) -> dict[str, object]:
    if validate:
        report = _load(report_path)
        _require(
            report.get("schema_version") == SCHEMA and report.get("mode") == "native",
            "mode_mismatch",
        )
        _require(report.get("evidence_kind") != "public-interface-fixture", "mode_mismatch")
    try:
        collector = importlib.import_module("tools.open_brain_dev.harness_native_acceptance")
    except ModuleNotFoundError:
        raise AcceptanceError("native_collection_unavailable") from None
    if validate:
        return dict(collector.validate_native(report_path=report_path))
    return dict(collector.run_native(report_path=report_path, evidence_dir=evidence_dir))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("fixture", "native"))
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args(argv)
    try:
        _require(args.report.is_absolute(), "absolute_private_path_required")
        _require(args.validate or args.evidence_dir is not None, "evidence_directory_required")
        if not args.validate:
            _require(args.evidence_dir.is_absolute(), "absolute_private_path_required")
        if args.mode == "native":
            summary = _native(
                report_path=args.report, evidence_dir=args.evidence_dir, validate=args.validate
            )
        elif args.validate:
            summary = validate_fixture(report_path=args.report)
        else:
            summary = run_fixture(report_path=args.report, evidence_dir=args.evidence_dir)
        # A collector summary is an allowlisted metadata projection, never arbitrary stdout.
        _require(
            summary.get("status") == "passed" and summary.get("mode") == args.mode,
            "acceptance_incomplete",
        )
        print(
            json.dumps(
                {
                    key: summary[key]
                    for key in (
                        "status",
                        "mode",
                        "pair_count",
                        "required_scenario_groups",
                        "goal_complete",
                        "incomplete_required_criteria",
                    )
                    if key in summary
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, AcceptanceError) else "acceptance_unavailable"
        _require(re.fullmatch(r"[a-z_]{1,80}", code), "invalid_error_code")
        print(
            json.dumps(
                {"status": "failed", "mode": args.mode, "error_code": code, "goal_complete": False},
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
