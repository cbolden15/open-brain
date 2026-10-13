from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import DecisionOutcome, ProposalDraft
from open_brain_engine.engine.contracts import EngineTaskSet
from open_brain_engine.engine.source_lifecycle_contracts import (
    SourceInspection,
    SourceInspectRequest,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, SourceRouteRequest

from open_brain.services.launcher_policy import LauncherPolicyError
from open_brain.services.local_mcp import LocalMcpAdapter
from open_brain.services.mcp_protocol import serve_stdio_mcp
from open_brain.services.session_authority import TrustedSessionAuthority
from open_brain.services.session_consent import DurableProviderConsentStore
from open_brain.services.t03_adapters import T03AppAdapter
from open_brain_collector.lifecycle import CollectorController, EngineRevisionSink
from open_brain_collector.saved_markdown import SavedMarkdownCollectorRuntime
from packages.app.tests.integration.services.test_local_entrypoints import _subprocess_cli
from packages.app.tests.integration.services.test_local_mcp import (
    INITIALIZE,
    _call,
    _exchange,
    _session_policy_file,
    _start,
)
from packages.collector.tests.integration.test_saved_markdown import _adapter
from packages.collector.tests.integration.test_saved_markdown_publication import (
    _collector,
    _terminal_snapshot,
)


@dataclass
class _Case:
    tasks: EngineTaskSet
    selected: Path
    now: list[int]
    sink: EngineRevisionSink
    runtime: SavedMarkdownCollectorRuntime
    controller: CollectorController
    source: SourceInspection
    approval: dict[str, Any]
    page_id: str


def _owner() -> EffectiveAuthority:
    return EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)


def _case(tmp_path: Path) -> _Case:
    selected = tmp_path / "selected"
    selected.mkdir()
    for name in ("main", "control-a", "control-b"):
        (selected / f"{name}.md").write_text(
            f"---\nowner: synthetic\n---\n# Transport {name}\n"
            + f"Transport retained body {name} 漢字🙂\n" * 4
            + "\n# Why Saved\nprivate motivation\n",
            encoding="utf-8",
        )
    now = [100]
    tasks, sink, runtime, controller = _collector(tmp_path, selected, now)
    controller.enable(
        source_id="synthetic-publication",
        selection=_adapter(selected).selection,
        interval_seconds=60,
    )
    assert (
        controller.sync_due(
            source_id="synthetic-publication", runtime=runtime, capture_sink=sink
        ).captured_count
        == 3
    )
    assert tasks.sources is not None
    main_source = None
    main_approval = None
    request_path = tmp_path / "sharing-request.json"
    for _, envelope, receipt in _terminal_snapshot(tasks):
        captured = json.loads(cast(bytes, envelope))["submission"]["capture"]
        source_id = json.loads(cast(str, receipt))["source_receipt"]["source_id"]
        source = tasks.sources.inspect(
            SourceInspectRequest(source_id=source_id), authority=_owner()
        )
        request_path.write_text(
            json.dumps(
                {
                    "dto_version": 1,
                    "operation_id": "sharing.preview.transport." + source_id,
                    "source_id": source_id,
                    "expected_head": source.head_capture_id,
                    "expected_head_version": source.head_version,
                    "expected_lifecycle_version": source.lifecycle_version,
                    "expected_route_version": source.route_version,
                    "brain_id": source.destination_brain_id,
                    "issuer_epoch": source.issuer_epoch,
                    "provider_ids": ["anthropic", "openai"],
                }
            ),
            encoding="utf-8",
        )
        preview = _subprocess_cli(
            tasks.profile.root, "sharing", "preview", "--request-file", str(request_path)
        )
        assert "private motivation" not in cast(str, preview["text"])
        request_path.write_text(
            json.dumps(
                {
                    "dto_version": 1,
                    "operation_id": "sharing.approve.transport." + source_id,
                    "preview_id": preview["preview_id"],
                    "preview_sha256": preview["preview_sha256"],
                    "brain_id": source.destination_brain_id,
                    "issuer_epoch": source.issuer_epoch,
                    "destination_brain_id": source.destination_brain_id,
                    "expected_decision_version": 0,
                    "decision": "approve",
                }
            ),
            encoding="utf-8",
        )
        approval = _subprocess_cli(
            tasks.profile.root, "sharing", "approve", "--request-file", str(request_path)
        )
        assert (
            _subprocess_cli(
                tasks.profile.root, "sharing", "approve", "--request-file", str(request_path)
            )
            == approval
        )
        assert approval["copy_capture_id"] is not None
        if captured["title"] == "Transport main":
            main_source, main_approval = source, approval
    assert main_source is not None and main_approval is not None
    copy_id = cast(str, main_approval["copy_capture_id"])
    space = tasks.spaces.create_space("Synthetic Transport", delivery_id="transport.space")
    tasks.spaces.route(copy_id, space.space_id, delivery_id="transport.copy.route")
    page_id = None
    for index in range(2):
        proposal = tasks.review.propose(
            (copy_id,),
            (
                ProposalDraft(
                    "Transport page", f"Transport canonical version {index} 漢字🙂\n" * 20
                ),
            ),
            delivery_id=f"transport.proposal.{index}",
            target_page_id=page_id,
        )[0]
        decision = tasks.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id=f"transport.page.decision.{index}",
            expected_review_digest=proposal.review_digest,
        )
        page_id = decision.page_id
    assert page_id is not None
    return _Case(
        tasks, selected, now, sink, runtime, controller, main_source, main_approval, page_id
    )


def _policy(
    case: _Case, root: Path, provider: str, *, grant: bool = True
) -> tuple[Path, DurableProviderConsentStore]:
    root.mkdir(mode=0o700)
    consent = DurableProviderConsentStore(
        root / "consent.json",
        brain_id=case.source.destination_brain_id,
        issuer_epoch=case.source.issuer_epoch,
    )
    if grant:
        consent.grant(
            provider_id=provider,
            allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
            operation_id="transport.grant",
            decided_at="2026-10-03T00:00:00Z",
            consent_id_factory=lambda: "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
    policy = _session_policy_file(
        case.tasks,
        root / "policy.json",
        read_tiers=["public"],
        capabilities=["search", "content-read", "history-read"],
        external=True,
        generation=1,
    )
    value = json.loads(policy.read_text(encoding="utf-8"))
    value["provider_id"] = provider
    policy.write_text(json.dumps(value), encoding="utf-8")
    policy.chmod(0o600)
    return policy, consent


def _flags(policy: Path) -> tuple[str, ...]:
    return (
        "--session-policy",
        str(policy),
        "--consent-state",
        str(policy.parent / "consent.json"),
        "--allow-search",
        "--allow-content-read",
        "--allow-history-read",
        "--allow-capture",
        "--allow-capture-submit",
        "--allow-inbox-read",
        "--allow-organize",
        "--allow-review-read",
        "--allow-review-propose",
        "--allow-review-decide",
        "--allow-workspace-read",
        "--allow-graph-refresh",
    )


@contextmanager
def _session(case: _Case, policy: Path) -> Iterator[subprocess.Popen[str]]:
    process = _start(case.tasks.profile.root, *_flags(policy))
    try:
        assert "result" in _exchange(process, INITIALIZE)
        listing = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in listing["result"]["tools"]} == {
            "brain_catalog",
            "brain_contract_describe",
            "brain_search_page",
            "brain_read",
            "brain_history_list",
            "brain_history_show",
        }
        yield process
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.wait(timeout=10)
        assert process.returncode == 0
        assert process.stderr is not None and process.stderr.read() == ""


def _rpc(process: subprocess.Popen[str], name: str, value: dict[str, object]) -> dict[str, Any]:
    return cast(dict[str, Any], _exchange(process, _call(name, value))["result"])


def _denied(response: dict[str, Any], *codes: str) -> None:
    assert response["isError"] is True and "structuredContent" not in response
    assert response["content"] == [{"type": "text", "text": response["content"][0]["text"]}]
    assert response["content"][0]["text"] in codes


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("transition", ["head", "route", "withdraw", "revoke"])
def test_collector_trusted_stdio_cursors_close_after_source_transition(
    tmp_path: Path, provider: str, transition: str
) -> None:
    case = _case(tmp_path)
    policy, _consent = _policy(case, tmp_path / "authority", provider)
    with _session(case, policy) as process:
        search_args: dict[str, object] = {"dto_version": 1, "query": "Transport", "limit": 1}
        first = _rpc(process, "brain_search_page", search_args)["structuredContent"]
        assert first["results"] and first["next_cursor"] is not None
        search_tail = {**search_args, "cursor": first["next_cursor"]}
        assert _rpc(process, "brain_search_page", search_tail)["structuredContent"]["results"]
        history_args: dict[str, object] = {
            "dto_version": 1,
            "record_id": case.page_id,
            "limit": 1,
        }
        history = _rpc(process, "brain_history_list", history_args)["structuredContent"]
        assert history["next_cursor"] is not None and history["complete"] is False
        history_tail = {**history_args, "cursor": history["next_cursor"]}
        second = _rpc(process, "brain_history_list", history_tail)["structuredContent"]
        assert second["entries"] and second["complete"] is True
        read_args: dict[str, object] = {
            "dto_version": 1,
            "record_id": case.approval["copy_capture_id"],
            "expected_revision_id": case.approval["copy_capture_id"],
            "target_bytes": 16,
        }
        chunk = _rpc(process, "brain_read", read_args)["structuredContent"]
        assert chunk["next_cursor"] is not None and chunk["complete"] is False
        read_tail = {**read_args, "cursor": chunk["next_cursor"]}
        assert (
            _rpc(process, "brain_read", read_tail)["structuredContent"]["start_byte"]
            == chunk["end_byte"]
        )
        historical_args: dict[str, object] = {
            "dto_version": 1,
            "record_id": case.page_id,
            "expected_revision_id": second["entries"][0]["revision_id"],
            "target_bytes": 16,
        }
        old_chunk = _rpc(process, "brain_history_show", historical_args)["structuredContent"]
        assert old_chunk["next_cursor"] is not None and old_chunk["complete"] is False
        old_tail = {**historical_args, "cursor": old_chunk["next_cursor"]}
        assert (
            _rpc(process, "brain_history_show", old_tail)["structuredContent"]["start_byte"]
            == old_chunk["end_byte"]
        )
        _denied(
            _rpc(
                process,
                "brain_read",
                {
                    "dto_version": 1,
                    "record_id": case.source.head_capture_id,
                    "expected_revision_id": case.source.head_capture_id,
                },
            ),
            "not_found",
        )
        for name in (
            "brain_capture",
            "brain_export",
            "brain_source_withdraw",
            "brain_sharing_approve",
            "brain_review_approve",
            "brain_graph_refresh",
        ):
            _denied(_rpc(process, name, {}), "unknown tool")
        assert case.tasks.sources is not None
        request_path = tmp_path / "transition.json"
        if transition == "head":
            (case.selected / "main.md").write_text(
                "# Transport unapproved next version\nNew local-only body\n", encoding="utf-8"
            )
            case.now[0] = 160
            assert (
                case.controller.sync_due(
                    source_id="synthetic-publication", runtime=case.runtime, capture_sink=case.sink
                ).captured_count
                == 1
            )
        elif transition == "route":
            space = case.tasks.spaces.create_space("Other", delivery_id="transport.other-space")
            case.tasks.sources.route(
                SourceRouteRequest(
                    source_id=case.source.source_id,
                    expected_head=case.source.head_capture_id,
                    expected_route_version=case.source.route_version,
                    space_id=space.space_id,
                    operation_id="operation_" + str(uuid4()),
                ),
                authority=_owner(),
            )
        elif transition == "withdraw":
            request_path.write_text(
                json.dumps(
                    {
                        "dto_version": 1,
                        "operation_id": "withdraw.transport",
                        "source_id": case.source.source_id,
                        "expected_head": case.source.head_capture_id,
                        "expected_lifecycle_version": case.source.lifecycle_version,
                        "brain_id": case.source.destination_brain_id,
                        "issuer_epoch": case.source.issuer_epoch,
                        "reason_code": "owner_choice",
                        "absence_evidence_digest": None,
                    }
                ),
                encoding="utf-8",
            )
            _subprocess_cli(
                case.tasks.profile.root, "source", "withdraw", "--request-file", str(request_path)
            )
        else:
            request_path.write_text(
                json.dumps(
                    {
                        "dto_version": 1,
                        "operation_id": "sharing.revoke.transport",
                        "approval_id": case.approval["approval_id"],
                        "expected_approval_version": 1,
                        "brain_id": case.source.destination_brain_id,
                        "issuer_epoch": case.source.issuer_epoch,
                        "destination_brain_id": case.source.destination_brain_id,
                        "reason": "owner_choice",
                    }
                ),
                encoding="utf-8",
            )
            _subprocess_cli(
                case.tasks.profile.root, "sharing", "revoke", "--request-file", str(request_path)
            )
        _denied(_rpc(process, "brain_search_page", search_tail), "cursor_stale")
        for name, arguments in (
            ("brain_history_list", history_tail),
            ("brain_history_show", old_tail),
            ("brain_read", read_tail),
            ("brain_read", read_args),
        ):
            _denied(_rpc(process, name, arguments), "not_found", "cursor_stale")
    case.tasks.portability.rebuild_index()
    with _session(case, policy) as restarted:
        _denied(_rpc(restarted, "brain_read", read_args), "not_found")
        _denied(_rpc(restarted, "brain_history_list", history_args), "not_found")
        visible = _rpc(restarted, "brain_search_page", search_args)["structuredContent"]
        assert visible["results"] and visible["next_cursor"] is not None
    owner_history = _subprocess_cli(case.tasks.profile.root, "history", "list", case.page_id)
    assert len(cast(list[object], owner_history["entries"])) == 2


def test_collector_trusted_transport_preserves_utf8_and_hidden_cursor_stability(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    policy, _ = _policy(case, tmp_path / "authority", "openai")
    with _session(case, policy) as process:
        arguments: dict[str, object] = {"dto_version": 1, "query": "Transport", "limit": 1}
        first = _rpc(process, "brain_search_page", arguments)["structuredContent"]
        assert first["next_cursor"] is not None
        tail = {**arguments, "cursor": first["next_cursor"]}
        before = _rpc(process, "brain_search_page", tail)
        (case.selected / "hidden.md").write_text(
            "# Unapproved hidden source\nOwner-local-only body\n", encoding="utf-8"
        )
        case.now[0] = 160
        assert (
            case.controller.sync_due(
                source_id="synthetic-publication", runtime=case.runtime, capture_sink=case.sink
            ).captured_count
            == 1
        )
        after = _rpc(process, "brain_search_page", tail)
        assert {
            key: value for key, value in after["structuredContent"].items() if key != "next_cursor"
        } == {
            key: value for key, value in before["structuredContent"].items() if key != "next_cursor"
        }
        assert after["structuredContent"]["next_cursor"] is not None
        assert (
            _rpc(
                process,
                "brain_search_page",
                {
                    **arguments,
                    "cursor": after["structuredContent"]["next_cursor"],
                },
            )["structuredContent"]["results"]
            == _rpc(
                process,
                "brain_search_page",
                {
                    **arguments,
                    "cursor": before["structuredContent"]["next_cursor"],
                },
            )["structuredContent"]["results"]
        )
        read_arguments: dict[str, object] = {
            "dto_version": 1,
            "record_id": case.approval["copy_capture_id"],
            "expected_revision_id": case.approval["copy_capture_id"],
            "target_bytes": 16,
        }
        chunks: list[str] = []
        offset = 0
        while True:
            chunk = _rpc(process, "brain_read", read_arguments)["structuredContent"]
            assert chunk["start_byte"] == offset
            text = chunk["content"]["text"]
            chunks.append(text)
            offset += len(text.encode("utf-8"))
            assert chunk["end_byte"] == offset
            if chunk["complete"]:
                assert chunk["next_cursor"] is None
                break
            assert chunk["next_cursor"] is not None
            read_arguments = {**read_arguments, "cursor": chunk["next_cursor"]}
        body = "".join(chunks)
        assert body == "# Transport main\nTransport retained body main 漢字🙂\n" + (
            "Transport retained body main 漢字🙂\n" * 3
        )
        assert "private motivation" not in body and "owner: synthetic" not in body
    owner_read = _subprocess_cli(
        case.tasks.profile.root,
        "history",
        "show",
        cast(str, case.approval["copy_capture_id"]),
        "--expected-revision-id",
        cast(str, case.approval["copy_capture_id"]),
    )
    assert cast(dict[str, object], owner_read["content"])["text"] == body


@pytest.mark.parametrize("provider", ["gemini", "openai-alias"])
def test_collector_trusted_transport_denies_unapproved_named_provider(
    tmp_path: Path,
    provider: str,
) -> None:
    case = _case(tmp_path)
    policy, _ = _policy(case, tmp_path / "authority", provider)
    with _session(case, policy) as process:
        _denied(
            _rpc(
                process,
                "brain_read",
                {
                    "dto_version": 1,
                    "record_id": case.approval["copy_capture_id"],
                    "expected_revision_id": case.approval["copy_capture_id"],
                },
            ),
            "not_found",
        )
        assert (
            _rpc(
                process,
                "brain_search_page",
                {
                    "dto_version": 1,
                    "query": "Transport",
                },
            )["structuredContent"]["results"]
            == []
        )


def test_collector_trusted_transport_requires_current_durable_consent(tmp_path: Path) -> None:
    case = _case(tmp_path)
    missing_policy, _ = _policy(case, tmp_path / "missing-authority", "openai", grant=False)
    missing = _start(case.tasks.profile.root, *_flags(missing_policy))
    assert missing.wait(timeout=15) == 78
    assert missing.stderr is not None and missing.stderr.read() == "consent_unavailable\n"
    assert missing.stdout is not None and missing.stdout.read() == ""
    policy, consent = _policy(case, tmp_path / "authority", "openai")
    process = _start(case.tasks.profile.root, *_flags(policy))
    try:
        assert "result" in _exchange(process, INITIALIZE)
        assert _rpc(
            process,
            "brain_read",
            {
                "dto_version": 1,
                "record_id": case.approval["copy_capture_id"],
                "expected_revision_id": case.approval["copy_capture_id"],
            },
        )["structuredContent"]["content"]["text"]
        consent.revoke(
            consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            operation_id="transport.revoke",
            decided_at="2026-10-03T00:01:00Z",
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}) + "\n")
        process.stdin.flush()
        assert process.wait(timeout=15) == 78
        assert process.stderr is not None and process.stderr.read() == "stale_policy\n"
        assert process.stdout is not None and process.stdout.read() == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    restarted = _start(case.tasks.profile.root, *_flags(policy))
    assert restarted.wait(timeout=15) == 78
    assert restarted.stderr is not None and restarted.stderr.read() == "stale_policy\n"


def test_collector_trusted_transport_revocation_during_read_emits_no_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    policy, consent = _policy(case, tmp_path / "authority", "openai")
    trusted = TrustedSessionAuthority(
        case.tasks,
        policy_path=policy,
        consent_state_path=policy.parent / "consent.json",
    )
    authority = trusted.load()
    adapter = LocalMcpAdapter(
        authority=authority,
        negotiated=T03AppAdapter(tasks=case.tasks, authority=authority),
        revalidate_authority=lambda: trusted.revalidate(authority),
    )
    invoke = T03AppAdapter.invoke
    observed: list[str] = []

    def revoke_after_read(self: T03AppAdapter, *args: Any, **kwargs: Any) -> dict[str, object]:
        result = invoke(self, *args, **kwargs)
        assert "Transport retained body main" in json.dumps(result)
        observed.append("actual-approved-copy-read")
        consent.revoke(
            consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            operation_id="transport.inflight-revoke",
            decided_at="2026-10-03T00:01:00Z",
        )
        return result

    monkeypatch.setattr(T03AppAdapter, "invoke", revoke_after_read)
    incoming = b"".join(
        json.dumps(message).encode() + b"\n"
        for message in (
            INITIALIZE,
            _call(
                "brain_read",
                {
                    "dto_version": 1,
                    "record_id": case.approval["copy_capture_id"],
                    "expected_revision_id": case.approval["copy_capture_id"],
                },
            ),
        )
    )
    outgoing = io.BytesIO()
    with pytest.raises(LauncherPolicyError, match="^stale_policy$"):
        serve_stdio_mcp(adapter, input_stream=io.BytesIO(incoming), output_stream=outgoing)
    assert observed == ["actual-approved-copy-read"]
    assert b"Transport retained body main" not in outgoing.getvalue()
    assert b"private motivation" not in outgoing.getvalue()
