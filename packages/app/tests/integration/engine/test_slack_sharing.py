"""W1a: real Slack intake, approval and scoped reads with synthetic transport only."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import open_local_engine
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingInspectRequest,
    SharingRevokeRequest,
    SlackSelectionCreateRequest,
    SlackSharingPreviewRequest,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, SourceRouteRequest

from open_brain.profile import compile_single_user_local
from open_brain.services.local_mcp import LocalMcpAdapter
from open_brain.services.t03_adapters import T03AppAdapter, owner_authority
from open_brain_collector.live_manager import LiveSourceManager
from open_brain_connectors.runtime.source_registry import SourceResourceSelection
from packages.app.tests.integration.services.test_slack_decision_recall import (
    ACCOUNT,
    DECISION,
    PROPOSAL,
    _message,
    _runtime,
    _SlackHttp,
)
from packages.connectors.tests.contract.test_slack_auth import _Credentials


class Journey:
    def __init__(self, root: Path) -> None:
        self.tasks: Any = open_local_engine(compile_single_user_local(root / "brain"))
        self.http = _SlackHttp()
        self.manager = LiveSourceManager(
            root / "collector",
            root / "brain",
            runtime=_runtime(root / "collector", self.http, _Credentials()),
        )
        self.manager.dispatch(
            "sources.configure",
            {
                "source_id": "onboarding",
                "selection": asdict(
                    SourceResourceSelection("slack", ACCOUNT, "channel:CONBOARDING", "channel")
                ),
                "options": {"privacy_tier": "work", "date_floor": "2025-10-01T00:00:00Z"},
            },
        )
        self.owner = owner_authority(self.tasks, session_id="w1a-owner")
        with open_local_database_read_only(self.tasks.profile) as connection:
            self.brain_id, self.epoch = connection.execute(
                "SELECT brain_id,issuer_epoch FROM brain_identity"
            ).fetchone()
        # Existing owner space API, no direct state writes.
        from open_brain.services.space_inbox import SpaceInboxService

        self.space = cast(
            dict[str, Any],
            SpaceInboxService(self.tasks.inbox).space_create(
                {"name": "Synthetic release", "idempotency_key": "w1a-work"}
            ),
        )["space"]["space_id"]

    def scan(self, messages: list[dict[str, object]]) -> None:
        self.http.scan("CONBOARDING", messages)
        for _ in messages:
            preview = self.manager.dispatch("sources.preview", {"source_id": "onboarding"})
            result = self.manager.dispatch(
                "sources.import", {"source_id": "onboarding", "preview_id": preview["preview_id"]}
            )
            assert result["outcome"] == "completed"

    def sources(self) -> list[dict[str, Any]]:
        with open_local_database_read_only(self.tasks.profile) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT s.*,l.lifecycle_version FROM logical_sources s "
                    "JOIN source_lifecycle_state l USING(source_id) WHERE source_id IN "
                    "(SELECT source_id FROM source_namespaces) ORDER BY source_id"
                )
            ]

    def route(self, source: dict[str, Any]) -> None:
        assert self.tasks.sources is not None
        self.tasks.sources.route(
            SourceRouteRequest(
                source_id=source["source_id"],
                space_id=self.space,
                expected_head=source["head_capture_id"],
                expected_route_version=source["route_version"],
                operation_id="operation_" + str(uuid4()),
            ),
            authority=self.owner,
        )

    def request(
        self, source: dict[str, Any], selection_id: str | None = None
    ) -> SlackSharingPreviewRequest:
        return SlackSharingPreviewRequest(
            operation_id="preview." + str(uuid4()),
            source_id=source["source_id"],
            expected_head=source["head_capture_id"],
            expected_head_version=source["head_version"],
            expected_route_version=source["route_version"],
            expected_lifecycle_version=source["lifecycle_version"],
            brain_id=self.brain_id,
            issuer_epoch=self.epoch,
            provider_ids=("synthetic-provider",),
            destination_space_id=self.space,
            selection_id=selection_id,
        )

    def approve(self, source: dict[str, Any], selection_id: str | None = None):  # type: ignore[no-untyped-def]
        sharing = self.tasks.sharing
        assert sharing is not None
        request = self.request(source, selection_id)
        preview = sharing.preview(request, authority=self.owner)
        decision = SharingDecisionRequest(
            operation_id="approve." + str(uuid4()),
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=self.brain_id,
            issuer_epoch=self.epoch,
            destination_brain_id=self.brain_id,
            expected_decision_version=0,
            decision="approve",
        )
        approved = sharing.decide(decision, authority=self.owner)
        assert approved.state == "captured"
        return request, preview, decision, approved

    def mcp(self, *, provider: str = "synthetic-provider") -> Any:
        authority = EffectiveAuthority(
            "synthetic-client",
            "w1a-session",
            frozenset({"search", "content-read", "history-read"}),
            frozenset({self.space}),
            allowed_read_tiers=frozenset({PrivacyTier.WORK}),
            egress_mode=EgressMode.EXTERNAL_PROVIDER,
            provider_id=provider,
            consent_id="consent_" + "a" * 32,
            authorization_generation=1,
            brain_id=self.brain_id,
            issuer_epoch=self.epoch,
        )
        return LocalMcpAdapter(authority=authority, negotiated=T03AppAdapter(self.tasks, authority))

    def read(self, copy: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.mcp().call_tool(
                "brain_read", {"dto_version": 1, "record_id": copy, "expected_revision_id": copy}
            ),
        )


def test_slack_work_copy_lifecycle_and_structured_selection(tmp_path: Path) -> None:
    journey = Journey(tmp_path)
    journey.scan([_message("1760000001.000001", DECISION), _message("1760000000.000001", PROPOSAL)])
    for source in journey.sources():
        journey.route(source)
    sources = journey.sources()
    sharing = journey.tasks.sharing
    assert sharing is not None
    selection_request = SlackSelectionCreateRequest(
        operation_id="selection.synthetic",
        brain_id=journey.brain_id,
        issuer_epoch=journey.epoch,
        destination_space_id=journey.space,
        member_source_ids=tuple(source["source_id"] for source in sources),
    )
    selection = sharing.create_selection(selection_request, authority=journey.owner)
    assert sharing.create_selection(selection_request, authority=journey.owner) == selection
    first = journey.approve(sources[0], selection["selection_id"])
    request, preview, decision, approved = first
    assert preview.dto_version == 2
    assert sharing.preview(request, authority=journey.owner) == preview
    assert sharing.decide(decision, authority=journey.owner) == approved
    assert (
        sharing.inspect(
            SharingInspectRequest(subject_id=approved.approval_id), authority=journey.owner
        ).preview
        == preview
    )
    assert approved.copy_capture_id is not None
    read = journey.read(approved.copy_capture_id)
    envelope = json.loads(read["content"]["text"])
    assert envelope["citation"]["permalink"].startswith("https://slack.com/archives/CONBOARDING/")
    assert envelope["citation"]["slack_coverage"] == "unknown"
    assert envelope["message_text"].startswith("[protected] ")
    assert read["selection"]["members"][1] == {"ordinal": 1, "status": "unavailable"}
    second = journey.approve(sources[1], selection["selection_id"])[3]
    assert all(
        member["status"] == "visible"
        for member in journey.read(approved.copy_capture_id)["selection"]["members"]
    )
    results = journey.mcp().call_tool(
        "brain_search_page", {"dto_version": 1, "query": "onboarding"}
    )["results"]
    assert {item["record_id"] for item in results} == {
        approved.copy_capture_id,
        second.copy_capture_id,
    }
    assert (
        journey.mcp(provider="different-provider").call_tool(
            "brain_search_page", {"dto_version": 1, "query": "onboarding"}
        )["results"]
        == []
    )
    sharing.revoke(
        SharingRevokeRequest(
            operation_id="revoke.synthetic",
            approval_id=second.approval_id,
            expected_approval_version=1,
            brain_id=journey.brain_id,
            issuer_epoch=journey.epoch,
            destination_brain_id=journey.brain_id,
            reason="owner_choice",
        ),
        authority=journey.owner,
    )
    assert journey.read(approved.copy_capture_id)["selection"]["members"][1] == {
        "ordinal": 1,
        "status": "unavailable",
    }
    with open_local_database_read_only(journey.tasks.profile) as connection:
        for source in sources:
            privacy = json.loads(
                connection.execute(
                    "SELECT privacy_json FROM captures WHERE capture_id=?",
                    (source["head_capture_id"],),
                ).fetchone()[0]
            )
            assert privacy["tier"] == "work" and privacy["authority"]["external_egress"] is False


def _single(tmp_path: Path) -> Journey:
    journey = Journey(tmp_path)
    journey.scan([_message("1760000001.000001", DECISION)])
    journey.route(journey.sources()[0])
    return journey


def _revoke(journey: Journey, approved: Any) -> None:
    assert journey.tasks.sharing is not None
    journey.tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="revoke." + str(uuid4()),
            approval_id=approved.approval_id,
            expected_approval_version=1,
            brain_id=journey.brain_id,
            issuer_epoch=journey.epoch,
            destination_brain_id=journey.brain_id,
            reason="owner_choice",
        ),
        authority=journey.owner,
    )


def _hidden(journey: Journey, copy: str) -> None:
    from open_brain.services.mcp_protocol import McpCallError

    assert (
        journey.mcp().call_tool("brain_search_page", {"dto_version": 1, "query": "onboarding"})[
            "results"
        ]
        == []
    )
    for tool, arguments in (
        ("brain_read", {"record_id": copy, "expected_revision_id": copy}),
        ("brain_history_list", {"record_id": copy}),
        ("brain_history_show", {"record_id": copy, "expected_revision_id": copy}),
    ):
        with pytest.raises(McpCallError, match="not_found"):
            journey.mcp().call_tool(tool, {"dto_version": 1, **arguments})


@pytest.mark.parametrize("transition", ["edit", "withdraw", "route", "revoke"])
def test_slack_stale_approval_hidden_from_search_read_and_history(
    tmp_path: Path, transition: str
) -> None:
    journey = _single(tmp_path)
    source = journey.sources()[0]
    approved = journey.approve(source)[3]
    assert approved.copy_capture_id is not None
    assert journey.read(approved.copy_capture_id)["complete"]
    if transition == "edit":
        journey.scan(
            [
                _message(
                    "1760000001.000001", "Changed onboarding rationale", edited="1760000301.000001"
                )
            ]
        )
    elif transition == "withdraw":
        from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest

        assert journey.tasks.sources is not None
        journey.tasks.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.synthetic",
                source_id=source["source_id"],
                expected_head=source["head_capture_id"],
                expected_lifecycle_version=source["lifecycle_version"],
                brain_id=journey.brain_id,
                issuer_epoch=journey.epoch,
                reason_code="owner_choice",
            ),
            authority=journey.owner,
        )
    elif transition == "route":
        journey.route(source)
    else:
        _revoke(journey, approved)
    _hidden(journey, approved.copy_capture_id)


@pytest.mark.parametrize(
    "failure", ["capture-before", "capture-after", "route-before", "route-after"]
)
def test_slack_lost_acknowledgment_and_restart_resume_one_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    journey = _single(tmp_path)
    sharing = journey.tasks.sharing
    assert sharing is not None
    preview = sharing.preview(journey.request(journey.sources()[0]), authority=journey.owner)
    decision = SharingDecisionRequest(
        operation_id="approve.retry",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=journey.brain_id,
        issuer_epoch=journey.epoch,
        destination_brain_id=journey.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    engine = sharing._engine
    target = engine.capture if failure.startswith("capture") else engine
    name = "submit" if failure.startswith("capture") else "_route_capture"
    original = getattr(target, name)

    def fail(*args: Any, **kwargs: Any) -> Any:
        if failure.endswith("after"):
            original(*args, **kwargs)
        raise RuntimeError("synthetic lost acknowledgment")

    with monkeypatch.context() as patch:
        patch.setattr(target, name, fail)
        receipt = sharing.decide(decision, authority=journey.owner)
        assert receipt.state == "pending"
        assert (
            journey.mcp().call_tool("brain_search_page", {"dto_version": 1, "query": "onboarding"})[
                "results"
            ]
            == []
        )
    restarted = open_local_engine(journey.tasks.profile)
    assert restarted.sharing is not None
    complete = restarted.sharing.decide(decision, authority=journey.owner)
    assert complete.state == "captured" and complete.copy_capture_id is not None
    assert restarted.sharing.decide(decision, authority=journey.owner) == complete
    with open_local_database_read_only(journey.tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM captures WHERE delivery_id LIKE 'sharing.copy.v2.%'"
            ).fetchone()[0]
            == 1
        )
    assert journey.read(complete.copy_capture_id)["complete"]


def test_selection_one_member_repair_reaction_duplicate_and_separate_correction(
    tmp_path: Path,
) -> None:
    journey = Journey(tmp_path)
    messages = [_message("1760000001.000001", DECISION), _message("1760000000.000001", PROPOSAL)]
    journey.scan(messages)
    for source in journey.sources():
        journey.route(source)
    sources = journey.sources()
    sharing = journey.tasks.sharing
    assert sharing is not None
    selection = sharing.create_selection(
        SlackSelectionCreateRequest(
            operation_id="selection.repair",
            brain_id=journey.brain_id,
            issuer_epoch=journey.epoch,
            destination_space_id=journey.space,
            member_source_ids=tuple(source["source_id"] for source in sources),
        ),
        authority=journey.owner,
    )
    copies = [journey.approve(source, selection["selection_id"])[3] for source in sources]
    reference = copies[0].copy_capture_id
    assert reference is not None
    before = journey.read(reference)
    journey.scan(
        [
            {**messages[0], "reactions": [{"name": "thumbsup", "count": 1}]},
            messages[1],
        ]
    )
    assert journey.read(reference) == before
    # Edit the second ordinal while leaving ordinal zero's approval untouched.
    with open_local_database_read_only(journey.tasks.profile) as connection:
        namespace = json.loads(
            connection.execute(
                "SELECT namespace_json FROM source_namespaces WHERE source_id=?",
                (sources[1]["source_id"],),
            ).fetchone()[0]
        )
    ts = namespace["external_id"].removeprefix("message:")
    changed = [
        _message(ts, "Revised onboarding rationale 漢字", edited="1760000501.000001")
        if item["ts"] == ts
        else item
        for item in messages
    ]
    journey.scan(changed)
    assert journey.read(reference)["selection"]["members"][1] == {
        "ordinal": 1,
        "status": "unavailable",
    }
    revised = next(
        source for source in journey.sources() if source["source_id"] == sources[1]["source_id"]
    )
    repaired = journey.approve(revised, selection["selection_id"])[3]
    assert all(
        member["status"] == "visible" for member in journey.read(reference)["selection"]["members"]
    )
    assert (
        journey.read(reference)["selection"]["members"][1]["record_id"] == repaired.copy_capture_id
    )
    journey.scan(
        [_message("1760000002.000001", "Correction: onboarding opens next month."), *changed]
    )
    assert len(journey.sources()) == 3
    assert all(
        member["status"] == "visible" for member in journey.read(reference)["selection"]["members"]
    )
    assert (
        journey.mcp().call_tool("brain_search_page", {"dto_version": 1, "query": "Correction"})[
            "results"
        ]
        == []
    )


@pytest.mark.parametrize("count", [0, 25])
def test_selection_size_and_duplicate_refusal(count: int) -> None:
    from open_brain_engine.engine.sharing_contracts import SharingError

    with pytest.raises(SharingError, match="invalid_arguments"):
        SlackSelectionCreateRequest(
            operation_id="selection.invalid",
            brain_id="brn_" + "a" * 26,
            issuer_epoch=1,
            destination_space_id="space_" + str(uuid4()),
            member_source_ids=tuple("source_" + str(uuid4()) for _ in range(count)),
        )
    with pytest.raises(SharingError, match="invalid_arguments"):
        SlackSelectionCreateRequest(
            operation_id="selection.duplicate",
            brain_id="brn_" + "a" * 26,
            issuer_epoch=1,
            destination_space_id="space_" + str(uuid4()),
            member_source_ids=("source_x", "source_x"),
        )


def test_projection_drift_is_explicit_only_with_matching_read_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import slack_sharing

    from open_brain.services.mcp_protocol import McpCallError

    journey = _single(tmp_path)
    copy = journey.approve(journey.sources()[0])[3].copy_capture_id
    assert copy is not None
    with monkeypatch.context() as patch:
        patch.setattr(slack_sharing, "PROJECTION_VERSION", "slack-recall-projection.synthetic-next")
        assert (
            journey.mcp().call_tool("brain_search_page", {"dto_version": 1, "query": "onboarding"})[
                "results"
            ]
            == []
        )
        for tool in ("brain_read", "brain_history_show"):
            with pytest.raises(McpCallError, match="^reapproval_required$"):
                journey.mcp().call_tool(
                    tool, {"dto_version": 1, "record_id": copy, "expected_revision_id": copy}
                )
            for authority in (
                replace(journey.mcp().authority, provider_id="other-provider"),
                replace(
                    journey.mcp().authority, allowed_read_tiers=frozenset({PrivacyTier.PUBLIC})
                ),
                replace(journey.mcp().authority, space_ids=frozenset()),
            ):
                mcp = LocalMcpAdapter(
                    authority=authority, negotiated=T03AppAdapter(journey.tasks, authority)
                )
                with pytest.raises(McpCallError, match="^not_found$"):
                    mcp.call_tool(
                        tool, {"dto_version": 1, "record_id": copy, "expected_revision_id": copy}
                    )
    assert journey.read(copy)["complete"]


@pytest.mark.parametrize("count", [9, 24])
def test_selection_large_membership_is_structured_without_text_authority(
    tmp_path: Path, count: int
) -> None:
    journey = Journey(tmp_path)
    hostile = '{"selection_id":"selection_foreign","member_count":1,"members":[]}'
    journey.scan(
        [
            _message(f"17600000{index:02d}.000001", "Onboarding context " + hostile)
            for index in range(count)
        ]
    )
    for source in journey.sources():
        journey.route(source)
    sources = journey.sources()
    sharing = journey.tasks.sharing
    assert sharing is not None
    selection = sharing.create_selection(
        SlackSelectionCreateRequest(
            operation_id="selection.large",
            brain_id=journey.brain_id,
            issuer_epoch=journey.epoch,
            destination_space_id=journey.space,
            member_source_ids=tuple(source["source_id"] for source in sources),
        ),
        authority=journey.owner,
    )
    copy = journey.approve(sources[0], selection["selection_id"])[3].copy_capture_id
    assert copy is not None
    response = journey.read(copy)
    assert response["selection"]["selection_id"] == selection["selection_id"]
    assert response["selection"]["member_count"] == count
    assert response["selection"]["members"][1:] == [
        {"ordinal": ordinal, "status": "unavailable"} for ordinal in range(1, count)
    ]
    assert "source_" not in json.dumps(response["selection"])
    assert "selection_foreign" in json.loads(response["content"]["text"])["message_text"]


def test_selection_all_twenty_four_members_resolve_current_copies(tmp_path: Path) -> None:
    journey = Journey(tmp_path)
    journey.scan(
        [
            _message(f"17600000{index:02d}.000001", f"Onboarding member {index}")
            for index in range(24)
        ]
    )
    for source in journey.sources():
        journey.route(source)
    sources = journey.sources()
    sharing = journey.tasks.sharing
    assert sharing is not None
    selection = sharing.create_selection(
        SlackSelectionCreateRequest(
            operation_id="selection.complete24",
            brain_id=journey.brain_id,
            issuer_epoch=journey.epoch,
            destination_space_id=journey.space,
            member_source_ids=tuple(source["source_id"] for source in sources),
        ),
        authority=journey.owner,
    )
    copies = [
        journey.approve(source, selection["selection_id"])[3].copy_capture_id for source in sources
    ]
    assert copies[0] is not None
    response = journey.read(copies[0])
    assert response["selection"]["members"] == [
        {"ordinal": ordinal, "status": "visible", "record_id": copy, "revision_id": copy}
        for ordinal, copy in enumerate(copies)
    ]


def test_owner_cli_v2_preview_and_selection_dispatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from open_brain.services.local_entrypoints import run_cli
    from packages.app.tests.integration.services.test_slack_decision_recall import _filesystem

    journey = _single(tmp_path)
    source = journey.sources()[0]
    selection = SlackSelectionCreateRequest(
        operation_id="selection.cli",
        brain_id=journey.brain_id,
        issuer_epoch=journey.epoch,
        destination_space_id=journey.space,
        member_source_ids=(source["source_id"],),
    )
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(selection.value()))
    assert (
        run_cli(
            (
                "sharing",
                "selection-create",
                "--request-file",
                str(request_file),
                "--data-dir",
                str(journey.tasks.profile.root),
                "--json",
            ),
            environment={"HOME": str(tmp_path)},
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    request = journey.request(source, created["selection_id"])
    request_file.write_text(json.dumps(request.value()))
    assert (
        run_cli(
            (
                "sharing",
                "preview",
                "--request-file",
                str(request_file),
                "--data-dir",
                str(journey.tasks.profile.root),
                "--json",
            ),
            environment={"HOME": str(tmp_path)},
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["dto_version"] == 2
    assert json.loads(preview["text"])["message_text"].startswith("[protected] ")


def test_cross_process_v2_revocation_is_observed_by_existing_reader(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    journey = _single(tmp_path)
    approved = journey.approve(journey.sources()[0])[3]
    assert approved.copy_capture_id is not None
    mcp = journey.mcp()
    arguments = {
        "dto_version": 1,
        "record_id": approved.copy_capture_id,
        "expected_revision_id": approved.copy_capture_id,
    }
    assert mcp.call_tool("brain_read", arguments)["complete"]
    request = SharingRevokeRequest(
        operation_id="revoke.cross-process",
        approval_id=approved.approval_id,
        expected_approval_version=1,
        brain_id=journey.brain_id,
        issuer_epoch=journey.epoch,
        destination_brain_id=journey.brain_id,
        reason="owner_choice",
    )
    request_file = tmp_path / "revocation.json"
    request_file.write_text(json.dumps(request.value()))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from open_brain.services.local_entrypoints import run_cli; "
            "sys.exit(run_cli(sys.argv[1:]))",
            "sharing",
            "revoke",
            "--request-file",
            str(request_file),
            "--data-dir",
            str(journey.tasks.profile.root),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["state"] == "revoked"
    from open_brain.services.mcp_protocol import McpCallError

    with pytest.raises(McpCallError, match="not_found"):
        mcp.call_tool("brain_read", arguments)
