from __future__ import annotations

import io
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier

from open_brain.local_data import select_local_root
from open_brain.services import harness_bridge
from open_brain.services.harness_bridge import HarnessBridgeConfig, HarnessBridgeError, run_harness
from open_brain.services.harness_cli import HarnessDeadlineExceeded, _deadline
from open_brain.services.local_bootstrap import open_local_brain
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_operations import current_brain_identity
from open_brain.services.session_consent import DurableProviderConsentStore


def setup(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    project = tmp_path / "project"
    project.mkdir()
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    root = tmp_path / "brain"
    with open_local_brain(
        select_local_root(data_dir=str(root), environment={"HOME": str(tmp_path)})
    ) as session:
        brain_id, issuer_epoch = current_brain_identity(session.tasks)
    store = DurableProviderConsentStore(
        authority / "consent.json",
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
    )
    for client in ("codex", "claude-code", "pi"):
        store.grant(
            provider_id=client,
            allowed_tiers=frozenset({PrivacyTier.WORK}),
            operation_id="grant-" + client,
            decided_at="2026-01-01T00:00:00Z",
        )
    policy = {
        "schema_version": "open-brain-harness-policy.v1",
        "enabled": True,
        "providers": {client: ["work"] for client in ("codex", "claude-code", "pi")},
        "sources": [
            {"id": "example", "project_root": str(project), "tier": "work", "local_only": False}
        ],
        "exclusions": [],
    }
    _write(authority / "policy.json", policy)
    config = {
        "schema_version": "open-brain-harness-bridge.v1",
        "brain_root": str(root),
        "brain_id": brain_id,
        "policy": str(authority / "policy.json"),
        "consent_state": str(authority / "consent.json"),
        "outbox": str(authority / "outbox"),
    }
    path = authority / "bridge.json"
    _write(path, config)
    return path, project, policy, config


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def event(project: Path) -> dict[str, object]:
    transcript = project / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "native-fixture", "cwd": str(project)}},
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "My preferred editor is Vim."}],
            },
        },
    ]
    transcript.write_text("".join(json.dumps(item) + "\n" for item in records))
    return {
        "session_id": "native-fixture",
        "cwd": str(project),
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
    }


def test_credential_refusal_latches_metadata_without_source_custody(tmp_path: Path) -> None:
    path, project, _, config = setup(tmp_path)
    payload = event(project)
    assert run_harness("enqueue", path, "codex", payload)["status"] == "queued"
    drained = run_harness("drain", path, "codex")
    assert cast(list[dict[str, object]], drained["results"])[0]["status"] == "delivered"
    transcript = Path(str(payload["transcript_path"]))
    with transcript.open("a") as stream:
        stream.write(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text",
            "text": "-----BEGIN PRIVATE KEY----- synthetic fixture"}],
        }}) + "\n")
    refused = run_harness("enqueue", path, "codex", payload)
    assert refused["error_code"] == "credential_signal"
    recalled = run_harness("recall", path, "codex", {"cwd": str(project), "query": "editor"})
    assert recalled["status"] == "refused"
    assert "Vim" not in json.dumps(recalled)
    retained = [p.read_bytes() for p in Path(config["outbox"]).rglob("*") if p.is_file()]
    assert all(b"BEGIN PRIVATE KEY" not in content for content in retained)


def test_enqueue_custody_drain_and_three_provider_reads(tmp_path: Path) -> None:
    path, project, _, _ = setup(tmp_path)
    queued = run_harness("enqueue", path, "codex", event(project))
    assert queued["status"] == "queued"
    assert "Vim" not in json.dumps(queued)
    assert run_harness("status", path, "codex")["counts"] == {
        "pending": 1,
        "delivered": 0,
        "refused": 0,
        "unavailable": 0,
    }
    drained = run_harness("drain", path, "codex")
    assert drained["status"] in {"ok", "delivered"}, drained
    for client in ("codex", "claude-code", "pi"):
        request = {"cwd": str(project), "project_path": str(project), "query": "preferred editor"}
        recalled = run_harness("recall", path, client, request)
        assert recalled["status"] == "ok", recalled
        rows = recalled["results"]
        assert isinstance(rows, list)
        assert rows[0]["text"] == "My preferred editor is Vim."
        citation = json.loads(rows[0]["citation"])
        assert citation["harness"] == "codex"
        assert citation["timestamp"] == "2026-01-01T00:00:00Z"


def test_enqueue_and_status_do_not_require_brain_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, project, _, config = setup(tmp_path)
    config["brain_root"] = str(tmp_path / "offline-brain")
    _write(path, config)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SDK startup forbidden")

    monkeypatch.setattr(harness_bridge, "open_local_brain", forbidden)
    assert run_harness("enqueue", path, "codex", event(project))["status"] == "queued"
    assert run_harness("status", path, "codex")["counts"] == {
        "pending": 1,
        "delivered": 0,
        "refused": 0,
        "unavailable": 0,
    }
    assert not (tmp_path / "offline-brain").exists()


def private_event(project: Path) -> dict[str, object]:
    payload = event(project)
    transcript = Path(str(payload["transcript_path"]))
    with transcript.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Confidential: keep this local."}
                        ],
                    },
                }
            )
            + "\n"
        )
    return payload


def test_pending_source_restriction_blocks_cloud_before_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, project, _, _ = setup(tmp_path)
    assert run_harness("enqueue", path, "codex", private_event(project))["status"] == "queued"

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("pending restriction must block SDK startup")

    monkeypatch.setattr(harness_bridge, "open_local_brain", forbidden)
    result = run_harness("recall", path, "codex", {"cwd": str(project), "query": "editor"})
    assert result["status"] == "refused"
    assert result["error_code"] == "pending_source_restriction"
    assert "results" not in result


def test_pending_source_restriction_during_read_blocks_content_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, project, _, _ = setup(tmp_path)
    assert run_harness("enqueue", path, "codex", event(project))["status"] == "queued"
    assert run_harness("drain", path, "codex")["status"] in {"ok", "delivered"}
    original = harness_bridge._workflow

    @contextmanager
    def concurrent_restriction(*args: Any, **kwargs: Any) -> Any:
        with original(*args, **kwargs) as workflow:
            yield workflow
            assert (
                run_harness("enqueue", path, "codex", private_event(project))["status"] == "queued"
            )

    monkeypatch.setattr(harness_bridge, "_workflow", concurrent_restriction)
    result = run_harness("recall", path, "codex", {"cwd": str(project), "query": "editor"})
    assert result["status"] == "refused"
    assert result["error_code"] == "pending_source_restriction"
    assert "results" not in result


def test_backend_startup_failure_remains_visible_in_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, project, _, _ = setup(tmp_path)
    assert run_harness("enqueue", path, "codex", event(project))["status"] == "queued"

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("private backend failure detail")

    monkeypatch.setattr(harness_bridge, "open_local_brain", unavailable)
    result = run_harness("drain", path, "codex")
    assert result == {"status": "unavailable", "error_code": "bridge_unavailable"}
    status = run_harness("status", path, "codex")
    counts = status["counts"]
    assert isinstance(counts, dict)
    assert counts["unavailable"] == 1
    assert status["error_count"] == 1
    assert "private backend failure detail" not in json.dumps(status)


def test_explicit_local_capture_restriction_survives_queue_and_delivery(tmp_path: Path) -> None:
    path, project, _, _ = setup(tmp_path)
    payload = {
        "session_id": "local-session",
        "project_path": str(project),
        "local_only": True,
        "provider": "local",
        "messages": [
            {
                "role": "user",
                "ordinal": 0,
                "source_id": "entry-local",
                "text": "My preferred editor is Vim.",
            }
        ],
    }
    assert run_harness("enqueue", path, "pi", payload, local_only_host=True)["status"] == "queued"
    before = run_harness("recall", path, "codex", {"cwd": str(project), "query": "editor"})
    assert before["error_code"] == "pending_source_restriction"
    assert run_harness("drain", path, "pi")["status"] in {"ok", "delivered"}
    after = run_harness("recall", path, "codex", {"cwd": str(project), "query": "editor"})
    assert after == {"status": "ok", "results": [], "omitted_count": 0}
    local = run_harness(
        "recall",
        path,
        "pi",
        {"project_path": str(project), "query": "editor", "local_only": True},
        local_only_host=True,
    )
    assert local["status"] == "ok"
    assert local["results"]
    # An unmarked replay cannot widen the accepted source restriction.
    payload["local_only"] = False
    assert run_harness("enqueue", path, "pi", payload)["status"] == "queued"
    assert run_harness("drain", path, "pi")["status"] in {"ok", "delivered"}
    assert run_harness("recall", path, "codex", {"cwd": str(project), "query": "editor"}) == after


def test_consent_revocation_during_shutdown_blocks_content_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, project, _, _ = setup(tmp_path)
    assert run_harness("enqueue", path, "codex", event(project))["status"] == "queued"
    assert run_harness("drain", path, "codex")["status"] in {"ok", "delivered"}
    original = harness_bridge._workflow

    @contextmanager
    def revoke_before_release(*args: Any, **kwargs: Any) -> Any:
        with original(*args, **kwargs) as workflow:
            yield workflow
            active = next(
                record
                for record in workflow.consent_snapshot().records
                if record.active and record.provider_id == "codex"
            )
            workflow._consent.revoke(
                consent_id=active.consent_id,
                operation_id="revoke-during-close",
                decided_at="2026-01-02T00:00:00Z",
            )

    monkeypatch.setattr(harness_bridge, "_workflow", revoke_before_release)
    result = run_harness("recall", path, "codex", {"cwd": str(project), "query": "editor"})
    assert result == {"status": "refused", "error_code": "authorization_changed"}


def test_exclusion_refuses_before_named_transcript_or_sdk_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, project, policy, config = setup(tmp_path)
    policy["exclusions"] = [str(project)]
    _write(Path(config["policy"]), policy)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("excluded source read forbidden")

    monkeypatch.setattr(harness_bridge, "capture_event", forbidden)
    result = run_harness("enqueue", path, "codex", {"cwd": str(project)})
    assert result == {"status": "refused", "error_code": "source_excluded"}
    assert not Path(config["outbox"]).exists()


def test_cloud_caller_cannot_claim_local_only_even_with_private_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, project, _, _ = setup(tmp_path)
    unknown = project.parent / "unknown"
    unknown.mkdir()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("private SDK read forbidden")

    monkeypatch.setattr(harness_bridge, "open_local_brain", forbidden)
    for client in ("codex", "claude-code", "pi"):
        result = run_harness(
            "recall",
            path,
            client,
            {
                "cwd": str(unknown),
                "project_path": str(unknown),
                "query": "violet",
                "local_only": True,
                "provider": "local",
            },
        )
        assert result["error_code"] == "local_only_host_required"
        assert "results" not in result


def test_wrong_destination_is_refused_before_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, project, _, config = setup(tmp_path)
    other = tmp_path / "other"
    with open_local_brain(
        select_local_root(data_dir=str(other), environment={"HOME": str(tmp_path)})
    ):
        pass
    config["brain_root"] = str(other)
    _write(path, config)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("wrong destination startup forbidden")

    monkeypatch.setattr(harness_bridge, "open_local_brain", forbidden)
    result = run_harness("recall", path, "codex", {"cwd": str(project), "query": "editor"})
    assert result == {"status": "refused", "error_code": "destination_mismatch"}


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "duplicate"])
def test_unsafe_or_ambiguous_configuration_is_rejected(tmp_path: Path, unsafe: str) -> None:
    path, _, _, _ = setup(tmp_path)
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "symlink":
        link = path.parent / "link.json"
        link.symlink_to(path)
        path = link
    else:
        path.write_text('{"schema_version":"one","schema_version":"two"}')
    with pytest.raises(HarnessBridgeError):
        HarnessBridgeConfig.load(path)


def test_cli_rejects_bad_input_without_private_parser_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"private-material":bad'))
    result = run_cli(
        ["harness", "enqueue", "--config", str(tmp_path / "missing.json"), "--client", "codex"]
    )
    assert result == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "refused", "error_code": "invalid_request"}
    assert "private-material" not in output


def test_deadline_interrupts_a_pending_foreground_operation() -> None:
    started = time.monotonic()
    with pytest.raises(HarnessDeadlineExceeded), _deadline(0.03):
        time.sleep(1)
    assert time.monotonic() - started < 0.5


def hermes_payload(project: Path, *, source: str = "synthetic-post") -> dict[str, object]:
    return {
        "project_path": str(project),
        "session_id": "synthetic-hermes-session",
        "messages": [{
            "role": "user", "text": "My preferred editor is Vim.", "ordinal": 11,
            "source_id": "mattermost:" + source + ":user",
            "timestamp": "2026-01-01T00:00:00Z",
        }],
    }


def hermes_setup(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any], dict[str, Any]]:
    path, work, policy, config = setup(tmp_path)
    personal = tmp_path / "personal"
    personal.mkdir()
    policy["sources"].append({
        "id": "personal", "project_root": str(personal), "tier": "personal", "local_only": False,
    })
    policy["providers"]["synthetic-provider"] = ["work", "personal"]
    _write(Path(config["policy"]), policy)
    config_value = HarnessBridgeConfig.load(path)
    with harness_bridge._workflow(config_value, local_only_host=False) as workflow:
        store = DurableProviderConsentStore(
            Path(config["consent_state"]), brain_id=config["brain_id"],
            issuer_epoch=current_brain_identity(workflow.tasks)[1],
        )
        store.grant(
            provider_id="synthetic-provider",
            allowed_tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
            operation_id="grant-synthetic-provider", decided_at="2026-01-01T00:00:00Z",
        )
    return path, work, personal, policy, config


def test_hermes_provider_bound_tier_narrowing_citation_and_source_read(tmp_path: Path) -> None:
    path, work, personal, _, _ = hermes_setup(tmp_path)
    for project, source in ((work, "work-post"), (personal, "personal-post")):
        assert run_harness("enqueue", path, "hermes", hermes_payload(project, source=source))[
            "status"
        ] == "queued"
    assert run_harness("drain", path, "hermes")["status"] == "delivered"
    captured: dict[PrivacyTier, str] = {}
    for project, tier, source in (
        (work, PrivacyTier.WORK, "work-post"),
        (personal, PrivacyTier.PERSONAL, "personal-post"),
    ):
        response = run_harness(
            "recall", path, "hermes", {"project_path": str(project), "query": "editor"},
            provider_id="synthetic-provider", read_tiers=frozenset({tier}),
        )
        assert response["status"] == "ok", response
        rows = cast(list[dict[str, Any]], response["results"])
        assert len(rows) == 1
        citation = json.loads(rows[0]["citation"])
        assert citation["harness"] == citation["client"] == "hermes"
        assert citation["source_id"] == "mattermost:" + source + ":user"
        assert citation["message_ordinal"] == 11
        captured[tier] = rows[0]["capture_id"]
    denied = run_harness(
        "source", path, "hermes", {
            "project_path": str(work), "capture_id": captured[PrivacyTier.PERSONAL],
        }, provider_id="synthetic-provider", read_tiers=frozenset({PrivacyTier.WORK}),
    )
    assert denied["status"] == "refused"
    assert "results" not in denied
    allowed = run_harness(
        "source", path, "hermes", {
            "project_path": str(work), "capture_id": captured[PrivacyTier.WORK],
        }, provider_id="synthetic-provider", read_tiers=frozenset({PrivacyTier.WORK}),
    )
    assert allowed["status"] == "ok"
    assert cast(list[dict[str, Any]], allowed["results"])[0]["source_context"]


def test_hermes_replay_keeps_original_locator_and_edits_fail_closed(tmp_path: Path) -> None:
    path, project, _, _ = setup(tmp_path)
    payload = hermes_payload(project)
    assert run_harness("enqueue", path, "hermes", payload)["status"] == "queued"
    first = run_harness("drain", path, "hermes")
    messages = cast(list[dict[str, Any]], payload["messages"])
    messages[0]["ordinal"] = 4
    assert run_harness("enqueue", path, "hermes", payload)["status"] == "queued"
    second = run_harness("drain", path, "hermes")
    rows = cast(list[dict[str, Any]], second["results"])
    assert rows[0]["acknowledgement"]["capture_ids"] == cast(
        list[dict[str, Any]], first["results"]
    )[0]["acknowledgement"]["capture_ids"]
    for edit in ("My preferred editor is Emacs.", "Thanks."):
        messages[0]["text"] = edit
        result = run_harness("enqueue", path, "hermes", payload)
        assert result["status"] == "refused"
        assert result["error_code"] == "lineage_conflict"


def test_hermes_enqueue_offline_excludes_injected_content_and_screens_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, project, _, config = setup(tmp_path)
    config["brain_root"] = str(tmp_path / "offline-brain")
    _write(path, config)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Brain startup forbidden")

    monkeypatch.setattr(harness_bridge, "open_local_brain", forbidden)
    payload = hermes_payload(project)
    messages = cast(list[dict[str, Any]], payload["messages"])
    messages.append({"role": "user", "authored": False, "text": "synthetic-injected-context"})
    assert run_harness("enqueue", path, "hermes", payload)["status"] == "queued"
    messages[0]["text"] = "password=synthetic-example"
    refused = run_harness("enqueue", path, "hermes", payload)
    assert refused["error_code"] == "credential_signal"
    assert not (tmp_path / "offline-brain").exists()
    retained = [p.read_bytes() for p in Path(config["outbox"]).rglob("*") if p.is_file()]
    assert all(b"synthetic-injected-context" not in body for body in retained)
    assert all(b"password=synthetic-example" not in body for body in retained)


@pytest.mark.parametrize("restriction", ["unknown", "secret", "local", "marked", "spoof"])
def test_hermes_denies_restricted_capture_and_all_local_read_claims(
    tmp_path: Path, restriction: str,
) -> None:
    path, project, policy, config = setup(tmp_path)
    payload = hermes_payload(project)
    if restriction == "unknown":
        policy["sources"] = []
    elif restriction == "secret":
        policy["sources"][0]["tier"] = "secret"
    elif restriction == "local":
        policy["sources"][0]["local_only"] = True
    elif restriction == "marked":
        cast(list[dict[str, Any]], payload["messages"])[0]["text"] = (
            "Confidential:\nMy preferred editor is Vim."
        )
    else:
        payload["local_only"] = True
    _write(Path(config["policy"]), policy)
    refused = run_harness("enqueue", path, "hermes", payload, local_only_host=True)
    assert refused["status"] == "refused", refused
    assert refused["error_code"] == "source_denied"
    retained = [p.read_bytes() for p in Path(config["outbox"]).rglob("*") if p.is_file()]
    assert all(b"preferred editor" not in body for body in retained)
    read = run_harness(
        "recall", path, "hermes", {
            "project_path": str(project), "query": "editor", "local_only": True,
        }, local_only_host=True, provider_id="codex", read_tiers=frozenset({PrivacyTier.WORK}),
    )
    assert read["status"] == "refused"
    assert "results" not in read


def test_hermes_untrusted_payload_cannot_select_provider_or_tiers(tmp_path: Path) -> None:
    path, project, _, _ = setup(tmp_path)
    payload = {
        "project_path": str(project), "query": "editor", "provider_id": "codex",
        "read_tiers": ["work"],
    }
    assert run_harness("recall", path, "hermes", payload) == {
        "status": "refused", "error_code": "invalid_request",
    }
    for tiers in (
        frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}), frozenset({PrivacyTier.SECRET}),
        frozenset({"work"}), frozenset(),
    ):
        assert run_harness(
            "recall", path, "hermes", payload, provider_id="codex", read_tiers=cast(Any, tiers),
        )["status"] == "refused"


def test_hermes_narrowing_never_broadens_policy_or_durable_consent(tmp_path: Path) -> None:
    path, work, personal, policy, config = hermes_setup(tmp_path)
    assert run_harness("enqueue", path, "hermes", hermes_payload(personal))["status"] == "queued"
    assert run_harness("drain", path, "hermes")["status"] == "delivered"
    request = {"project_path": str(personal), "query": "editor"}
    denied = run_harness(
        "recall", path, "hermes", request,
        provider_id="codex", read_tiers=frozenset({PrivacyTier.PERSONAL}),
    )
    assert denied == {"status": "refused", "error_code": "provider_denied"}
    policy["providers"]["codex"] = ["work", "personal"]
    _write(Path(config["policy"]), policy)
    # Policy now grants personal, but the durable codex consent remains work-only.
    denied = run_harness(
        "recall", path, "hermes", request,
        provider_id="codex", read_tiers=frozenset({PrivacyTier.PERSONAL}),
    )
    assert denied == {"status": "refused", "error_code": "provider_denied"}
    mismatched_scope = run_harness(
        "recall", path, "hermes", {"project_path": str(work), "query": "editor"},
        provider_id="synthetic-provider", read_tiers=frozenset({PrivacyTier.PERSONAL}),
    )
    assert mismatched_scope == {"status": "refused", "error_code": "source_denied"}


def test_hermes_foreground_capture_delivers_only_its_request_and_replays_receipt(
    tmp_path: Path,
) -> None:
    path, project, _, _ = setup(tmp_path)
    other = hermes_payload(project, source="unrelated-pending")
    assert run_harness("enqueue", path, "hermes", other)["status"] == "queued"
    payload = hermes_payload(project)
    first = run_harness("capture", path, "hermes", payload)
    assert first["status"] == "acknowledged", first
    assert first["capture_ids"]
    second = run_harness("capture", path, "hermes", payload)
    assert second["status"] == "acknowledged"
    assert second["capture_ids"] == first["capture_ids"]
    assert second["request_id"] == first["request_id"]
    assert run_harness("status", path, "hermes")["counts"] == {
        "pending": 1, "delivered": 1, "refused": 0, "unavailable": 0,
    }
    messages = cast(list[dict[str, Any]], payload["messages"])
    messages[0]["text"] = "My preferred editor is Emacs."
    edited = run_harness("capture", path, "hermes", payload)
    assert edited["error_code"] == "lineage_conflict"


def test_hermes_foreground_capture_missing_destination_does_not_bootstrap(tmp_path: Path) -> None:
    path, project, _, config = setup(tmp_path)
    missing = tmp_path / "offline-brain"
    config["brain_root"] = str(missing)
    _write(path, config)
    result = run_harness("capture", path, "hermes", hermes_payload(project))
    assert result == {"status": "unavailable", "error_code": "bridge_unavailable"}
    assert not missing.exists()
    counts = cast(dict[str, int], run_harness("status", path, "hermes")["counts"])
    assert counts["pending"] == 1
    assert run_harness("capture", path, "codex", event(project))["error_code"] == "invalid_request"


def test_hermes_cli_trusted_provider_flags_and_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    path, project, _, _ = setup(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hermes_payload(project))))
    assert run_cli([
        "harness", "capture", "--config", str(path), "--client", "hermes", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "acknowledged"
    request = {"project_path": str(project), "query": "editor"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    assert run_cli([
        "harness", "recall", "--config", str(path), "--client", "hermes", "--json",
        "--provider-id", "codex", "--read-tier", "work",
    ]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "ok"
    assert response["results"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        **request, "provider_id": "codex", "read_tiers": ["work"],
    })))
    assert run_cli([
        "harness", "recall", "--config", str(path), "--client", "hermes", "--json",
    ]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "invalid_request"


def test_hermes_later_confidential_edit_blocks_previous_source_release(tmp_path: Path) -> None:
    path, project, _, config = setup(tmp_path)
    payload = hermes_payload(project)
    assert run_harness("capture", path, "hermes", payload)["status"] == "acknowledged"
    query = {"project_path": str(project), "query": "editor"}
    arguments = {"provider_id": "codex", "read_tiers": frozenset({PrivacyTier.WORK})}
    assert run_harness("recall", path, "hermes", query, **cast(Any, arguments))["results"]
    cast(list[dict[str, Any]], payload["messages"])[0]["text"] = (
        "Confidential:\nMy preferred editor is Emacs."
    )
    assert run_harness("capture", path, "hermes", payload)["error_code"] == "source_denied"
    response = run_harness("recall", path, "hermes", query, **cast(Any, arguments))
    assert response["status"] == "refused"
    assert response["error_code"] == "pending_source_restriction"
    retained = [p.read_bytes() for p in Path(config["outbox"]).rglob("*") if p.is_file()]
    assert all(b"Confidential:" not in body and b"Emacs" not in body for body in retained)


def test_hermes_provider_revocation_withholds_previously_readable_memory(tmp_path: Path) -> None:
    path, project, _, config = setup(tmp_path)
    captured = run_harness("capture", path, "hermes", hermes_payload(project))
    assert captured["status"] == "acknowledged"
    request = {"project_path": str(project), "query": "editor"}
    before = run_harness(
        "recall", path, "hermes", request, provider_id="codex",
        read_tiers=frozenset({PrivacyTier.WORK}),
    )
    assert before["status"] == "ok" and before["results"]
    store = DurableProviderConsentStore(
        Path(config["consent_state"]), brain_id=config["brain_id"], issuer_epoch=1,
    )
    active = next(
        record for record in store.load().records
        if record.active and record.provider_id == "codex"
    )
    store.revoke(
        consent_id=active.consent_id, operation_id="synthetic-revoke-codex",
        decided_at="2026-01-02T00:00:00Z",
    )
    after = run_harness(
        "recall", path, "hermes", request, provider_id="codex",
        read_tiers=frozenset({PrivacyTier.WORK}),
    )
    assert after == {"status": "refused", "error_code": "consent_unavailable"}
