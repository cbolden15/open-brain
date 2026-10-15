"""W0 local Slack journey and explicit baseline refusals, without a live account."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import open_local_engine
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing_contracts import SharingError, SharingPreviewRequest

import open_brain.services.local_entrypoints as entrypoints
from open_brain.profile import compile_single_user_local
from open_brain.services.local_mcp import LocalMcpAdapter
from open_brain.services.local_operations import current_brain_identity
from open_brain.services.mcp_protocol import McpCallError
from open_brain.services.session_authority import TrustedSessionAuthority
from open_brain.services.session_consent import DurableProviderConsentStore
from open_brain.services.t03_adapters import T03AppAdapter, owner_authority
from open_brain_collector.live_manager import LiveSourceManager, PriorityRuntime
from open_brain_connectors.runtime.live_http import LiveHttpResponse, LiveHttpTransport
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.slack_auth import SLACK_USER_SCOPES, _credential_reference
from open_brain_connectors.runtime.slack_live import SlackSourceClient
from open_brain_connectors.runtime.source_registry import SourceResourceSelection
from packages.connectors.tests.contract.test_slack_auth import _Credentials

ACCOUNT = "account:" + "a" * 64
CONSENT_ID = "consent_" + "a" * 32
PROPOSAL = "Maybe invite-only onboarding should end; open signup might shorten the wait."
DECISION = "We kept invite-only onboarding because support can safely onboard ten teams weekly."
EDITED = "We kept invite-only onboarding because support can safely onboard twelve teams weekly."
CORRECTION = "Correction: open signup starts next month; invite-only onboarding ends then."


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


class _SlackHttp(LiveHttpTransport):
    """Only the HTTP boundary is synthetic; unexpected requests fail closed."""

    def __init__(self) -> None:
        self.responses: list[tuple[str, str, dict[str, object]]] = []
        self.calls: list[str] = []

    def scan(self, channel: str, messages: list[dict[str, object]]) -> None:
        for index, message in enumerate(messages):
            self.responses.append(
                (
                    channel,
                    "" if index == 0 else f"synthetic-page-{index}",
                    {
                        "ok": True,
                        "messages": [message],
                        "response_metadata": {
                            "next_cursor": f"synthetic-page-{index + 1}"
                            if index + 1 < len(messages) else "",
                        },
                    },
                )
            )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        max_bytes: int = 1_048_576,
        timeout_seconds: float = 30,
    ) -> LiveHttpResponse:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        assert method == "GET" and parsed.scheme == "https" and parsed.netloc == "slack.com"
        assert parsed.path == "/api/conversations.history"
        assert headers == {"authorization": "Bearer synthetic-access-token"}
        assert form is None and max_bytes == 1_048_576 and timeout_seconds == 30
        channel, cursor, payload = self.responses.pop(0)
        assert query["channel"] == [channel]
        assert query.get("cursor", [""]) == [cursor]
        assert query["limit"] == ["1"] and query["oldest"] == ["1759276800.000000"]
        self.calls.append(url)
        return LiveHttpResponse(200, {}, json.dumps(payload).encode())


def _runtime(root: Path, http: _SlackHttp, credentials: _Credentials) -> PriorityRuntime:
    account_root = root / "accounts/slack"
    reference = _credential_reference(ACCOUNT, account_root)
    credentials.set(reference, {"access_token": "synthetic-access-token"})
    PrivateJsonStore(account_root).write(
        "slack-account-" + "a" * 64 + ".json",
        {
            "schema_version": 1,
            "credential_ref": reference,
            "account": {
                "provider": "slack",
                "connection_id": ACCOUNT,
                "display_name": "Synthetic Slack",
                "expires_at_epoch": 4_000_000_000,
                "scopes": list(SLACK_USER_SCOPES),
            },
        },
    )
    return PriorityRuntime(root, http=http, credentials=credentials)


def _message(ts: str, text: str, *, edited: str | None = None) -> dict[str, object]:
    return {"ts": ts, "text": text, "user": "USYNTHETIC",
            **({"edited": {"ts": edited}} if edited else {})}


def test_slack_local_recall_continuity_and_external_release_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    brain = tmp_path / "brain"
    collector = tmp_path / "collector"
    tasks = open_local_engine(compile_single_user_local(brain))
    http, credentials = _SlackHttp(), _Credentials()
    runtime = _runtime(collector, http, credentials)
    assert type(runtime.client("slack")) is SlackSourceClient
    manager = LiveSourceManager(collector, brain, runtime=runtime)

    def cli(*arguments: str, expected_code: int = 0) -> dict[str, Any]:
        code = entrypoints.run_cli(
            (*arguments, "--data-dir", str(brain), "--json"),
            environment={"HOME": str(tmp_path)}, filesystem_type_probe=_filesystem,
        )
        output = capsys.readouterr()
        assert output.err == "" and code == expected_code, output.out
        return cast(dict[str, Any], json.loads(output.out))

    def configure(source: str, channel: str, tier: str) -> None:
        manager.dispatch(
            "sources.configure",
            {"source_id": source,
             "selection": asdict(SourceResourceSelection("slack", ACCOUNT,
                                                          f"channel:{channel}", "channel")),
             "options": {"privacy_tier": tier, "date_floor": "2025-10-01T00:00:00Z"}},
        )

    def scan(source: str, channel: str, messages: list[dict[str, object]]) -> dict[str, object]:
        http.scan(channel, messages)
        for index in range(len(messages)):
            preview = manager.dispatch("sources.preview", {"source_id": source})
            result = manager.dispatch(
                "sources.import", {"source_id": source, "preview_id": preview["preview_id"]}
            )
            assert result["outcome"] == "completed"
            assert result["has_more"] is (index + 1 < len(messages))
        assert not http.responses
        return result

    def find(needle: str) -> dict[str, Any]:
        results = cli("search-page", needle)["results"]
        assert len(results) == 1
        return cast(dict[str, Any], results[0])

    def read(record: dict[str, Any]) -> dict[str, Any]:
        response = cli("read", record["record_id"], "--expected-revision-id",
                       record["revision_id"])
        assert response["complete"] is True and response["next_cursor"] is None
        assert response["content"]["kind"] == "untrusted_text"
        assert response["record"]["record_id"] == record["record_id"]
        assert response["record"]["revision_id"] == record["revision_id"]
        assert "slack.com/archives" not in json.dumps(response)
        return response

    def route(record: dict[str, Any], space: str) -> dict[str, Any]:
        inspected = cli("source", "inspect", record["source_id"])
        cli("source", "route", record["source_id"], space,
            "--expected-head", inspected["head_capture_id"],
            "--expected-route-version", str(inspected["route_version"]),
            "--operation-id", "operation_" + str(uuid4()))
        return cli("source", "inspect", record["source_id"])

    work = cli("space", "create", "Onboarding Work", "--idempotency-key", "work")
    other = cli("space", "create", "Unrelated Work", "--idempotency-key", "other")
    work_id, other_id = work["space"]["space_id"], other["space"]["space_id"]
    configure("onboarding", "CONBOARDING", "work")
    proposal = _message("1760000000.000001", PROPOSAL)
    decision = _message("1760000001.000001", DECISION)
    imported = scan("onboarding", "CONBOARDING", [decision, proposal])
    proposed, decided = find("shorten"), find("ten")
    assert read(proposed)["content"]["text"] == "[protected] " + PROPOSAL
    assert read(decided)["content"]["text"] == "[protected] " + DECISION
    assert proposed["source_id"] != decided["source_id"]
    route(proposed, work_id)
    routed = route(decided, work_id)

    # Retry the acknowledged operation in a reconstructed real collector/runtime.
    manager = LiveSourceManager(
        collector, brain, runtime=PriorityRuntime(collector, http=http, credentials=credentials)
    )
    calls = len(http.calls)
    assert manager.dispatch("sources.import", {"source_id": "onboarding",
                                               "preview_id": imported["preview_id"]}) == imported
    assert len(http.calls) == calls
    duplicate = scan("onboarding", "CONBOARDING", [decision, proposal])
    assert duplicate["captured_count"] == 0 and duplicate["duplicate_count"] == 1
    assert find("ten")["revision_id"] == decided["revision_id"]

    edited = _message("1760000001.000001", EDITED, edited="1760000301.000001")
    scan("onboarding", "CONBOARDING", [edited, proposal])
    revised = find("twelve")
    assert revised["source_id"] == decided["source_id"]
    assert revised["revision_id"] != decided["revision_id"]
    assert revised["space_id"] == work_id
    assert cli("search-page", "ten")["results"] == []
    assert read(revised)["content"]["text"] == "[protected] " + EDITED
    edited_inspection = cli("source", "inspect", revised["source_id"])
    assert edited_inspection["head_version"] == routed["head_version"] + 1
    assert edited_inspection["route_version"] == routed["route_version"]

    correction = _message("1760000002.000001", CORRECTION)
    scan("onboarding", "CONBOARDING", [correction, edited, proposal])
    corrected = find("month")
    assert corrected["source_id"] not in {proposed["source_id"], revised["source_id"]}
    assert corrected["space_id"] is None
    assert read(corrected)["content"]["text"] == "[protected] " + CORRECTION
    assert cli("source", "inspect", revised["source_id"]) == edited_inspection

    configure("personal", "CPERSONAL", "personal")
    scan("personal", "CPERSONAL", [_message("1760000003.000001",
                                           "Personal onboarding private plans.")])
    personal = find("private")
    route(personal, work_id)
    configure("unrelated", "CUNRELATED", "work")
    scan("unrelated", "CUNRELATED", [_message("1760000004.000001",
                                             "Unrelated onboarding product plans.")])
    unrelated = find("product")
    route(unrelated, other_id)

    # Load the actual scoped launcher policy with actual durable synthetic consent.
    brain_id, issuer_epoch = current_brain_identity(tasks)
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    policy, consent_path = authority_root / "policy.json", authority_root / "consent.json"
    consent = DurableProviderConsentStore(consent_path, brain_id=brain_id,
                                         issuer_epoch=issuer_epoch)
    consent.grant(provider_id="synthetic-provider", allowed_tiers=frozenset({PrivacyTier.WORK}),
                  operation_id="grant.w0", decided_at="2026-10-14T12:00:00Z",
                  consent_id_factory=lambda: CONSENT_ID)
    policy.write_text(json.dumps({
        "policy_version": "launcher-policy.v1", "principal_id": "synthetic-provider-client",
        "session_id": "synthetic-w0", "capabilities": ["search", "content-read"],
        "space_ids": [work_id], "allowed_read_tiers": ["work"], "allowed_capture_tiers": [],
        "egress_mode": "external_provider", "provider_id": "synthetic-provider",
        "consent_id": CONSENT_ID, "authorization_generation": 1,
        "brain_id": brain_id, "issuer_epoch": issuer_epoch,
    }))
    authority_source = TrustedSessionAuthority(tasks, policy_path=policy,
                                              consent_state_path=consent_path)
    authority = authority_source.load()
    mcp = LocalMcpAdapter(authority=authority, negotiated=T03AppAdapter(tasks, authority),
                          revalidate_authority=lambda: authority_source.revalidate(authority))
    external = mcp.call_tool("brain_search_page", {"dto_version": 1, "query": "onboarding"})
    assert external["results"] == [] and external["complete"] is True
    for hidden in (proposed, revised, corrected, personal, unrelated):
        with pytest.raises(McpCallError, match="^not_found$"):
            mcp.call_tool("brain_read", {"dto_version": 1, "record_id": hidden["record_id"],
                                        "expected_revision_id": hidden["revision_id"]})

    # A fully current raw Slack head still cannot satisfy the saved-Markdown v1 proof.
    assert tasks.sharing is not None
    with pytest.raises(SharingError, match="^binding_mismatch$") as refusal:
        tasks.sharing.preview(SharingPreviewRequest(
            operation_id="sharing.w0.slack", source_id=revised["source_id"],
            expected_head=edited_inspection["head_capture_id"],
            expected_head_version=edited_inspection["head_version"],
            expected_lifecycle_version=edited_inspection["lifecycle_version"],
            expected_route_version=edited_inspection["route_version"],
            brain_id=brain_id, issuer_epoch=issuer_epoch, provider_ids=("synthetic-provider",),
        ), authority=owner_authority(tasks, session_id="w0-owner"))
    assert refusal.value.code == "binding_mismatch"
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM logical_sources").fetchone()[0] == 5
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 6
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 0
        for record in (proposed, revised, corrected, personal, unrelated):
            privacy = json.loads(connection.execute(
                "SELECT privacy_json FROM captures WHERE capture_id=?", (record["revision_id"],)
            ).fetchone()[0])
            assert privacy["tier"] == ("personal" if record is personal else "work")
            assert privacy["authority"]["external_egress"] is False


@pytest.mark.parametrize("missing_option", ["--session-policy", "--consent-state"])
def test_scoped_agent_setup_incomplete_policy_inputs_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], missing_option: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    arguments = (
        "agent", "setup", "--client", "codex", "--scope", "project",
        "--project-dir", str(project), "--allow-search", "--allow-content-read",
        "--runtime", str(Path(sys.executable).resolve()), "--data-dir", str(tmp_path / "brain"),
        "--json",
    )
    # Generic setup remains valid; incomplete scoped authority must refuse before writes.
    assert entrypoints.run_cli(arguments, environment={"HOME": str(tmp_path)},
                               filesystem_type_probe=_filesystem) == 0
    output = capsys.readouterr()
    assert output.err == "" and json.loads(output.out)["status"] == "preview"
    assert list(project.iterdir()) == []
    assert entrypoints.run_cli((*arguments, missing_option, str(tmp_path / "synthetic.json")),
                               environment={"HOME": str(tmp_path)},
                               filesystem_type_probe=_filesystem) == 2
    refused = capsys.readouterr()
    assert refused.err == ""
    assert json.loads(refused.out)["error"]["code"] == "invalid_arguments"
    assert list(project.iterdir()) == []
