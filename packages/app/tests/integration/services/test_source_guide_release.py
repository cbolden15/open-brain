"""Guided owner release and scoped setup through real owner authorities (P3).

Routing, provider consent, selection, sharing preview/approval, the scoped
policy helper and agent setup all run against the real engine and app
services; only the Slack HTTP boundary and the OS credential store are
synthetic. No client or model is launched.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine.local_schema import open_local_database_read_only

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from packages.app.tests.integration.services import source_guide_support as support
from packages.app.tests.integration.services.test_source_guide import Guide, new_guide


@pytest.fixture
def guide(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Guide:
    return new_release_guide(tmp_path, capsys, monkeypatch)


def new_release_guide(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Guide:
    """A capture guide plus a private consent directory and a never-launched runtime."""
    current = new_guide(tmp_path, capsys, monkeypatch)
    (tmp_path / "consent").mkdir(mode=0o700)
    runtime = tmp_path / "runtime" / "open-brain"
    runtime.parent.mkdir(mode=0o700)
    # A launch of the configured runtime would leave this marker; setup never launches it.
    runtime.write_text(f"#!/bin/sh\ntouch {tmp_path / 'launched'}\n")
    runtime.chmod(0o700)
    return current


def approve(intent: dict[str, Any]) -> dict[str, object]:
    return {"intent_sha256": intent["intent_sha256"], "decision": "approve"}


def inspected(guide: Guide, texts: list[str]) -> list[str]:
    """Configure, then preview and import one-message pages and acknowledge every message."""
    guide.through_choose()
    assert guide.confirm("configure")[0] == 0
    support.queue(
        guide.fixture,
        "conversations.history",
        [
            support.history_page(
                [support.message(f"17600000{index:02d}.000001", text)],
                f"page-{index + 1}" if index + 1 < len(texts) else "",
            )
            for index, text in enumerate(texts)
        ],
    )
    receipts: list[str] = []
    for _text in texts:
        code, previewed = guide.confirm("preview")
        assert code == 0, previewed
        code, imported = guide.confirm("import", {"preview_id": previewed["data"]["preview_id"]})
        assert code == 0, imported
        receipts += imported["data"]["receipt_ids"]
    for receipt_id in receipts:
        acknowledge(guide, receipt_id)
    return receipts


def acknowledge(guide: Guide, receipt_id: str) -> dict[str, Any]:
    code, inspection = guide.advance("inspect", {"receipt_id": receipt_id})
    assert code == 0, inspection
    code, done = guide.confirm(
        "acknowledge-inspection",
        {"receipt_id": receipt_id,
         "inspection_sha256": inspection["data"]["projection_sha256"]},
    )
    assert code == 0, done
    return dict(inspection["data"])


def journal(guide: Guide) -> dict[str, Any]:
    return dict(json.loads(guide.journal.read_bytes()))


def sources(guide: Guide) -> dict[str, dict[str, Any]]:
    profile = open_existing_single_user_local(guide.brain)
    with closing(open_local_database_read_only(profile)) as connection:
        return {
            row["source_id"]: dict(row)
            for row in connection.execute(
                "SELECT s.*,l.lifecycle_version FROM logical_sources s "
                "JOIN source_lifecycle_state l USING(source_id)"
            )
        }


def destination(guide: Guide, name: str = "Guide release") -> str:
    code, created = guide.confirm("create-destination", {"name": name})
    assert (code, created["outcome"]) == (0, "completed"), created
    return str(journal(guide)["choices"]["destination_space_id"])


def test_destination_and_route_each_need_inspection_and_their_own_confirmation(
    guide: Guide,
) -> None:
    receipts = inspected(guide, ["Decision: keep onboarding invite-only."])
    code, refused = guide.advance("route", {"receipt_id": receipts[0]})
    assert (code, refused["code"]) == (78, "inspection_required")
    assert refused["next_actions"] == ["destination", "create-destination"]

    code, proposed = guide.advance("create-destination", {"name": "Guide release"})
    assert (code, proposed["outcome"]) == (3, "needs_confirmation")
    request = json.loads(proposed["intent"]["canonical_request_utf8"])
    assert request["name"] == "Guide release"
    assert request["delivery_id"].startswith("guide.space.")
    assert journal(guide)["choices"]["destination_space_id"] is None
    code, created = guide.advance(
        "create-destination", {"name": "Guide release"}, confirmation=approve(proposed["intent"])
    )
    assert (code, created["outcome"]) == (0, "completed"), created
    space_id = journal(guide)["choices"]["destination_space_id"]
    assert space_id.startswith("space_")
    saved = journal(guide)["operations"][-1]
    assert saved["evidence"] == {"delivery_id": request["delivery_id"], "space_id": space_id}

    code, proposed = guide.advance("route", {"receipt_id": receipts[0]})
    assert (code, proposed["outcome"]) == (3, "needs_confirmation"), proposed
    route = json.loads(proposed["intent"]["canonical_request_utf8"])
    assert set(route) == {
        "source_id", "space_id", "expected_head", "expected_route_version", "operation_id"
    }
    assert route["space_id"] == space_id
    assert [item["kind"] for item in proposed["intent"]["expected_versions"]] == [
        "source", "route"
    ]
    before = sources(guide)[route["source_id"]]
    assert before["route_version"] == route["expected_route_version"]
    code, routed = guide.advance(
        "route", {"receipt_id": receipts[0]}, confirmation=approve(proposed["intent"])
    )
    assert (code, routed["outcome"]) == (0, "completed"), routed
    after = sources(guide)[route["source_id"]]
    assert after["space_id"] == space_id
    assert after["route_version"] == before["route_version"] + 1
    evidence = journal(guide)["operations"][-1]["evidence"]
    assert evidence["operation_id"] == route["operation_id"]
    assert evidence["route_version"] == after["route_version"]
    # Routing the same unchanged head again is refused rather than re-applied.
    code, again = guide.advance("route", {"receipt_id": receipts[0]})
    assert (code, again["code"]) == (2, "invalid_request")
    # A broad space that already holds records is refused as a destination.
    stored = guide.journal.read_bytes()
    assert b"invite-only" not in stored


def test_existing_spaces_are_refused_in_v1_and_create_destination_is_offered(
    guide: Guide,
) -> None:
    from open_brain_engine.engine import open_local_engine

    receipts = inspected(guide, ["Status: draft"])
    tasks: Any = open_local_engine(compile_single_user_local(guide.brain))
    empty = tasks.inbox.create_space("Dedicated", delivery_id="synthetic.empty").space_id
    before = guide.journal.read_bytes()
    for space_id in ("space_" + "0" * 8, empty):
        code, refused = guide.advance("destination", {"space_id": space_id})
        assert (code, refused["code"], refused["next_actions"]) == (
            78, "authority_refused", ["create-destination"]
        )
    assert guide.journal.read_bytes() == before
    destination(guide)
    code, proposed = guide.advance("route", {"receipt_id": receipts[0]})
    assert code == 3, proposed


@pytest.mark.parametrize("point", ["policy-preview", "policy-apply", "setup-apply"])
def test_foreign_record_in_destination_refuses_policy_and_setup(
    guide: Guide, point: str
) -> None:
    from open_brain_engine.engine import TextPayload, open_local_engine

    approved_one(guide)
    arguments: dict[str, object] = {}
    if point != "policy-preview":
        code, preview = guide.advance("policy-preview")
        assert code == 0, preview
        arguments = {"policy_sha256": preview["data"]["payload_sha256"]}
    if point == "setup-apply":
        guide.confirm("policy-apply", arguments)
        arguments = {"preview_id": setup_previewed(guide)["preview_id"]}
    # A separate owner action places a record outside this workflow into the space.
    tasks: Any = open_local_engine(compile_single_user_local(guide.brain))
    tasks.capture.accept(
        TextPayload("foreign synthetic record"), delivery_id="synthetic.foreign",
        space_id=journal(guide)["choices"]["destination_space_id"],
    )
    policy_path = guide.root / "policy" / "policy.json"
    policy_before = policy_path.read_bytes() if policy_path.exists() else None
    code, refused = guide.advance(point, arguments)
    assert (code, refused["outcome"], refused["code"]) == (78, "refused", "authority_refused")
    last = journal(guide)["operations"][-1]
    assert (last["action"], last["state"]) == (point, "refused")
    assert (policy_path.read_bytes() if policy_path.exists() else None) == policy_before
    assert not (guide.project / ".mcp.json").exists()


def test_unacknowledged_receipt_never_routes(guide: Guide) -> None:
    guide.through_choose()
    guide.confirm("configure")
    support.queue(guide.fixture, "conversations.history",
                  [support.history_page([support.message("1760000000.000001", "Unread")])])
    _code, previewed = guide.confirm("preview")
    _code, imported = guide.confirm("import", {"preview_id": previewed["data"]["preview_id"]})
    receipt = imported["data"]["receipt_ids"][0]
    destination(guide)
    code, refused = guide.advance("route", {"receipt_id": receipt})
    assert (code, refused["code"]) == (78, "inspection_required")
    guide.advance("inspect", {"receipt_id": receipt})
    code, refused = guide.advance("route", {"receipt_id": receipt})
    assert (code, refused["code"]) == (78, "inspection_required")
    assert stat.S_IMODE(os.lstat(guide.journal).st_mode) == 0o600


def routed(guide: Guide, texts: list[str]) -> list[str]:
    receipts = inspected(guide, texts)
    destination(guide)
    for receipt_id in receipts:
        code, result = guide.confirm("route", {"receipt_id": receipt_id})
        assert code == 0, result
    return receipts


def selected_and_consented(guide: Guide, texts: list[str]) -> tuple[list[str], str]:
    receipts = routed(guide, texts)
    code, selected = guide.confirm("select", {"member_receipt_ids": receipts})
    assert code == 0, selected
    code, consented = guide.confirm("consent", {"provider_id": "openai"})
    assert code == 0, consented
    return receipts, str(selected["data"]["selection_id"])


def released(guide: Guide, receipt_id: str) -> dict[str, Any]:
    code, preview = guide.confirm(
        "release-preview", {"receipt_id": receipt_id, "provider_ids": ["openai"]}
    )
    assert (code, preview["outcome"]) == (0, "completed"), preview
    return dict(preview["data"])


def test_consent_selection_and_per_message_release_need_separate_owner_checkpoints(
    guide: Guide,
) -> None:
    from open_brain.services.session_consent import DurableProviderConsentStore

    texts = ["Decision: keep onboarding invite-only.", "Rationale: support capacity."]
    receipts = routed(guide, texts)
    code, refused = guide.advance(
        "release-preview", {"receipt_id": receipts[0], "provider_ids": ["openai"]}
    )
    assert (code, refused["code"], refused["next_actions"]) == (
        78, "inspection_required", ["select"]
    )

    code, proposed = guide.advance("select", {"member_receipt_ids": receipts})
    assert code == 3, proposed
    request = json.loads(proposed["intent"]["canonical_request_utf8"])
    by_head = {row["head_capture_id"]: source for source, row in sources(guide).items()}
    member_sources = request["member_source_ids"]
    assert len(member_sources) == 2 and set(member_sources) <= set(by_head.values())
    assert [item["kind"] for item in proposed["intent"]["expected_versions"]] == [
        "source", "source"
    ]
    code, selected = guide.advance(
        "select", {"member_receipt_ids": receipts}, confirmation=approve(proposed["intent"])
    )
    assert code == 0, selected
    selection_id = selected["data"]["selection_id"]
    assert selection_id.startswith("selection_") and selected["data"]["count"] == 2
    # A selection is never silently replaced.
    code, again = guide.advance("select", {"member_receipt_ids": receipts[:1]})
    assert (code, again["code"]) == (2, "invalid_request")

    # No implicit consent: release needs the separate provider checkpoint first.
    code, refused = guide.advance(
        "release-preview", {"receipt_id": receipts[0], "provider_ids": ["openai"]}
    )
    assert (code, refused["next_actions"]) == (78, ["consent"])
    code, wrong = guide.advance("consent", {"provider_id": "anthropic"})
    assert (code, wrong["code"]) == (2, "invalid_request")
    code, proposed = guide.advance("consent", {"provider_id": "openai"})
    assert code == 3
    grant = json.loads(proposed["intent"]["canonical_request_utf8"])
    assert grant["allowed_tiers"] == ["work"] and grant["decided_at"].endswith("Z")
    assert not (guide.root / "consent" / "state.json").exists()
    code, consented = guide.advance(
        "consent", {"provider_id": "openai"}, confirmation=approve(proposed["intent"])
    )
    assert code == 0, consented
    binding = journal(guide)["binding"]
    state = DurableProviderConsentStore(
        guide.root / "consent" / "state.json",
        brain_id=binding["brain_id"], issuer_epoch=binding["issuer_epoch"],
    ).load()
    active = [record for record in state.records if record.active]
    assert [(item.provider_id, sorted(item.allowed_tiers)) for item in active] == [
        ("openai", ["work"])
    ]
    assert journal(guide)["operations"][-1]["evidence"]["consent_id"] == active[0].consent_id

    # No bulk or multi-provider release.
    code, bulk = guide.advance(
        "release-preview", {"receipt_id": receipts[0], "provider_ids": ["anthropic", "openai"]}
    )
    assert (code, bulk["code"]) == (2, "invalid_request")
    approved: list[str] = []
    for index, receipt_id in enumerate(receipts):
        preview = released(guide, receipt_id)
        assert preview["kind"] == "owner_preview" and preview["subject"] == "sharing"
        assert texts[index] in json.loads(preview["payload_utf8"])["text"]
        code, proposed = guide.advance("approve", {"preview_id": preview["preview_id"]})
        assert (code, proposed["outcome"]) == (3, "needs_confirmation")
        # The full external projection is shown with the approval intent itself.
        assert proposed["data"]["payload_utf8"] == preview["payload_utf8"]
        decision = json.loads(proposed["intent"]["canonical_request_utf8"])
        assert decision["decision"] == "approve" and decision["expected_decision_version"] == 0
        code, done = guide.advance(
            "approve", {"preview_id": preview["preview_id"]},
            confirmation=approve(proposed["intent"]),
        )
        assert (code, done["outcome"]) == (0, "completed"), done
        approved = done["data"]["approved_copy_ids"]
        assert len(approved) == index + 1 and done["data"]["count"] == index + 1
        assert done["data"]["selection_id"] == selection_id
        code, twice = guide.advance("approve", {"preview_id": preview["preview_id"]})
        assert (code, twice["code"]) == (2, "invalid_request")
    assert all(copy.startswith("capture_") for copy in approved)
    stored = guide.journal.read_bytes()
    assert all(text.encode() not in stored for text in texts)


def test_edited_message_needs_new_inspection_and_release_preview(guide: Guide) -> None:
    receipts, _selection = selected_and_consented(guide, ["Status: draft plan", "Owner: Sam"])
    # An extra, never-approved preview of the first message stays behind as the stale one.
    stale = released(guide, receipts[0])
    first: list[str] = []
    for receipt_id in receipts:
        code, done = guide.confirm("approve", {"preview_id": released(guide, receipt_id)[
            "preview_id"
        ]})
        assert code == 0, done
        first = done["data"]["approved_copy_ids"]
    assert done["data"]["count"] == 2
    # A full rescan: the first message was edited, the second is unchanged.
    support.queue(
        guide.fixture,
        "conversations.history",
        [
            support.history_page([{
                **support.message("1760000000.000001", "Status: final plan"),
                "edited": {"ts": "1760000500.000001"},
            }], "page-1"),
            support.history_page([support.message("1760000001.000001", "Owner: Sam")]),
        ],
    )
    new_receipts: list[str] = []
    for _page in range(2):
        _code, previewed = guide.confirm("preview")
        code, imported = guide.confirm("import", {"preview_id": previewed["data"]["preview_id"]})
        assert code == 0, imported
        new_receipts += imported["data"]["receipt_ids"]
    assert receipts[0] not in new_receipts
    edited = [
        receipt_id for receipt_id in new_receipts
        if "final plan" in str(guide.advance("inspect", {"receipt_id": receipt_id})[1]["data"])
    ]
    assert len(edited) == 1

    code, refused = guide.advance("approve", {"preview_id": stale["preview_id"]})
    assert (code, refused["code"]) == (78, "revision_conflict"), refused
    assert refused["next_actions"] == ["inspect", "release-preview"]
    code, refused = guide.advance(
        "release-preview", {"receipt_id": receipts[0], "provider_ids": ["openai"]}
    )
    assert code == 78 and refused["code"] in {"revision_conflict", "receipt_unresolved"}
    code, refused = guide.advance(
        "release-preview", {"receipt_id": edited[0], "provider_ids": ["openai"]}
    )
    assert (code, refused["code"]) == (78, "inspection_required")
    assert "Status: final plan" in acknowledge(guide, edited[0])["body"]
    # Repair only the edited member, inside the same selection.
    fresh = released(guide, edited[0])
    assert "Status: final plan" in json.loads(fresh["payload_utf8"])["text"]
    code, done = guide.confirm("approve", {"preview_id": fresh["preview_id"]})
    assert (code, done["data"]["count"]) == (0, 2), done
    copies = done["data"]["approved_copy_ids"]
    assert copies[:2] == first and len(copies) == 3
    # Only the edited member was re-approved; the other member's approval is untouched.
    approvals = [item for item in journal(guide)["operations"]
                 if item["action"] == "approve" and item["state"] == "completed"]
    assert len(approvals) == 3


def test_stale_selection_member_refuses_release(guide: Guide) -> None:
    from open_brain_engine.engine import open_local_engine
    from open_brain_engine.engine.t03_contracts import SourceRouteRequest

    from open_brain.services.t03_adapters import owner_authority

    receipts, _selection = selected_and_consented(guide, ["Decision: ship Friday"])
    tasks: Any = open_local_engine(compile_single_user_local(guide.brain))
    elsewhere = tasks.inbox.create_space("Elsewhere", delivery_id="synthetic.elsewhere").space_id
    (source_id, row), = sources(guide).items()
    tasks.sources.route(
        SourceRouteRequest(
            source_id=source_id, space_id=elsewhere, expected_head=row["head_capture_id"],
            expected_route_version=row["route_version"],
            operation_id="operation_00000000-0000-4000-8000-0000000000aa",
        ),
        authority=owner_authority(tasks, session_id="other-owner"),
    )
    code, proposed = guide.advance(
        "release-preview", {"receipt_id": receipts[0], "provider_ids": ["openai"]}
    )
    assert code == 3, proposed
    code, refused = guide.advance(
        "release-preview", {"receipt_id": receipts[0], "provider_ids": ["openai"]},
        confirmation=approve(proposed["intent"]),
    )
    assert refused["outcome"] == "refused" and code == 78, refused
    assert journal(guide)["operations"][-1]["state"] == "refused"
    assert not any(item["action"] == "approve" for item in journal(guide)["operations"])


def approved_one(guide: Guide) -> list[str]:
    receipts, _selection = selected_and_consented(guide, ["Decision: keep it invite-only."])
    preview = released(guide, receipts[0])
    code, done = guide.confirm("approve", {"preview_id": preview["preview_id"]})
    assert code == 0, done
    return list(done["data"]["approved_copy_ids"])


def policy_applied(guide: Guide) -> str:
    code, preview = guide.advance("policy-preview")
    assert (code, preview["outcome"]) == (0, "completed"), preview
    digest = preview["data"]["payload_sha256"]
    code, applied = guide.confirm("policy-apply", {"policy_sha256": digest})
    assert code == 0, applied
    return str(digest)


def setup_previewed(guide: Guide) -> dict[str, Any]:
    code, previewed = guide.confirm("setup-preview")
    assert (code, previewed["outcome"]) == (0, "completed"), previewed
    return dict(previewed["data"])


def test_json_journey_from_start_to_scoped_setup_without_client_launch(guide: Guide) -> None:
    """`start` through `setup-apply`, one `advance` per step; only Slack HTTP is synthetic."""
    import hashlib

    copies = approved_one(guide)
    code, refused = guide.advance("setup-preview")
    assert (code, refused["code"], refused["next_actions"]) == (
        78, "inspection_required", ["policy-apply"]
    )
    code, preview = guide.advance("policy-preview")
    assert (code, preview["outcome"]) == (0, "completed"), preview
    payload = preview["data"]
    assert payload["subject"] == "policy" and payload["preview_id"] is None
    document = json.loads(payload["payload_utf8"])
    space_id = journal(guide)["choices"]["destination_space_id"]
    workflow = hashlib.sha256(guide.workflow_id.encode()).hexdigest()
    assert document["principal_id"] == f"guide:{workflow}:client"
    assert document["session_id"] == f"guide:{workflow}:session"
    assert document["capabilities"] == ["content-read", "search"]
    assert document["space_ids"] == [space_id]
    assert (document["allowed_read_tiers"], document["allowed_capture_tiers"]) == (["work"], [])
    assert document["provider_id"] == "openai"
    policy_path = guide.root / "policy" / "policy.json"
    assert not policy_path.exists()
    # Policy bytes never enter the journal; only non-content inputs and the SHA do.
    assert b"principal_id" not in guide.journal.read_bytes()
    assert journal(guide)["operations"][-1]["evidence"] == {
        "policy_sha256": payload["payload_sha256"], "path": str(policy_path)
    }
    code, refused = guide.advance("policy-apply", {"policy_sha256": "0" * 64})
    assert (code, refused["code"]) == (78, "inspection_required")
    code, applied = guide.confirm("policy-apply", {"policy_sha256": payload["payload_sha256"]})
    assert (code, applied["data"]["policy_sha256"]) == (0, payload["payload_sha256"]), applied
    assert policy_path.read_text() == payload["payload_utf8"]
    assert stat.S_IMODE(os.lstat(policy_path).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(policy_path.parent).st_mode) == 0o700

    # An unrelated client setting must survive the owned-fragment apply.
    config = guide.project / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {"unrelated": {"command": "synthetic-tool"}}}))
    code, proposed = guide.advance("setup-preview")
    assert code == 3, proposed
    setup = json.loads(proposed["intent"]["canonical_request_utf8"])
    assert setup["session_policy"] == str(policy_path)
    assert setup["policy_sha256"] == payload["payload_sha256"]
    code, previewed = guide.advance(
        "setup-preview", confirmation=approve(proposed["intent"])
    )
    assert code == 0, previewed
    shown = json.loads(previewed["data"]["payload_utf8"])
    assert previewed["data"]["subject"] == "setup"
    assert shown["permissions"] == {
        "capture": False, "search": True, "content_read": True, "history_read": False
    }
    assert json.loads(config.read_text()) == {
        "mcpServers": {"unrelated": {"command": "synthetic-tool"}}
    }
    code, proposed = guide.advance("setup-apply", {"preview_id": previewed["data"]["preview_id"]})
    assert code == 3, proposed
    assert proposed["data"]["payload_utf8"] == previewed["data"]["payload_utf8"]
    code, done = guide.advance(
        "setup-apply", {"preview_id": previewed["data"]["preview_id"]},
        confirmation=approve(proposed["intent"]),
    )
    assert (code, done["outcome"]) == (0, "completed"), done
    assert done["data"]["approved_copy_ids"] == copies and done["data"]["count"] == 1
    servers = json.loads(config.read_text())["mcpServers"]
    assert servers["unrelated"] == {"command": "synthetic-tool"}
    assert set(servers) == {"unrelated", "open-brain"}
    arguments = servers["open-brain"]["args"]
    assert str(policy_path) in arguments and "--session-policy" in arguments
    assert journal(guide)["operations"][-1]["evidence"]["status"] == "configured"
    assert not (guide.root / "launched").exists()
    stored = guide.journal.read_bytes()
    assert b"synthetic-tool" not in stored and b"mcpServers" not in stored


def setup_applied(guide: Guide) -> dict[str, Any]:
    preview_id = setup_previewed(guide)["preview_id"]
    code, done = guide.confirm("setup-apply", {"preview_id": preview_id})
    assert (code, done["outcome"]) == (0, "completed"), done
    return done


def test_journal_completes_after_setup_apply_once_every_member_is_released(
    guide: Guide,
) -> None:
    approved_one(guide)
    policy_applied(guide)
    done = setup_applied(guide)
    assert done["data"]["count"] == 1
    assert journal(guide)["status"] == "completed"
    # A completed journal is terminal: no further action, including cancel.
    for action in ("policy-preview", "cancel"):
        code, refused = guide.advance(action)
        assert (code, refused["code"]) == (2, "invalid_request")
    assert journal(guide)["status"] == "completed"


def test_failed_completion_write_keeps_the_setup_result_and_is_retried_next_action(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain.services.source_guide_journal import JournalError
    from open_brain.services.source_guide_release import ReleaseFlow

    approved_one(guide)
    policy_applied(guide)
    preview_id = setup_previewed(guide)["preview_id"]
    original = ReleaseFlow._write

    def write(self: ReleaseFlow, document: Any, **keywords: Any) -> Any:
        if document["status"] == "completed":
            raise JournalError("journal_busy")
        return original(self, document, **keywords)

    with monkeypatch.context() as patch:
        patch.setattr(ReleaseFlow, "_write", write)
        code, done = guide.confirm("setup-apply", {"preview_id": preview_id})
    # The setup write succeeded; a failed completion-only write never reports refused.
    assert (code, done["outcome"], done["code"]) == (0, "completed", None), done
    assert done["data"]["count"] == 1
    state = journal(guide)
    assert state["status"] == "active"
    assert state["operations"][-1]["action"] == "setup-apply"
    assert state["operations"][-1]["state"] == "completed"
    # The next action re-evaluates completion, records it and refuses.
    code, refused = guide.advance("policy-preview")
    assert (code, refused["code"]) == (2, "invalid_request"), refused
    assert journal(guide)["status"] == "completed"


def test_partial_release_stays_active_for_repair_until_every_member_is_current(
    guide: Guide,
) -> None:
    receipts, _selection = selected_and_consented(guide, ["Decision: one.", "Owner: two."])
    code, done = guide.confirm("approve", {"preview_id": released(guide, receipts[0])[
        "preview_id"
    ]})
    assert code == 0, done
    policy_applied(guide)
    done = setup_applied(guide)
    assert done["data"]["count"] == 1
    assert journal(guide)["status"] == "active"
    code, done = guide.confirm("approve", {"preview_id": released(guide, receipts[1])[
        "preview_id"
    ]})
    assert (code, done["data"]["count"]) == (0, 2), done
    assert journal(guide)["status"] == "completed"


def test_revoked_consent_refuses_setup_apply_before_any_config_write(guide: Guide) -> None:
    from open_brain.services.session_consent import DurableProviderConsentStore

    approved_one(guide)
    policy_applied(guide)
    previewed = setup_previewed(guide)
    code, proposed = guide.advance("setup-apply", {"preview_id": previewed["preview_id"]})
    assert code == 3
    binding = journal(guide)["binding"]
    store = DurableProviderConsentStore(
        guide.root / "consent" / "state.json",
        brain_id=binding["brain_id"], issuer_epoch=binding["issuer_epoch"],
    )
    (record,) = [item for item in store.load().records if item.active]
    store.revoke(consent_id=record.consent_id, operation_id="revoke.synthetic",
                 decided_at="2026-10-22T13:00:00Z")
    code, refused = guide.advance(
        "setup-apply", {"preview_id": previewed["preview_id"]},
        confirmation=approve(proposed["intent"]),
    )
    assert (code, refused["outcome"]) == (78, "refused"), refused
    assert not (guide.project / ".mcp.json").exists()
    assert not (guide.project / "CLAUDE.md").exists()
    code, refused = guide.advance("policy-preview")
    assert (code, refused["code"]) == (78, "authority_refused")


def test_changed_policy_is_stale_and_never_repaired(guide: Guide) -> None:
    approved_one(guide)
    policy_applied(guide)
    policy_path = guide.root / "policy" / "policy.json"
    widened = json.loads(policy_path.read_text())
    widened["allowed_capture_tiers"] = ["work"]
    policy_path.write_text(json.dumps(widened))
    code, refused = guide.advance("setup-preview")
    assert (code, refused["code"], refused["next_actions"]) == (
        78, "revision_conflict", ["policy-preview"]
    )
    code, refused = guide.advance("policy-preview")
    assert (code, refused["code"]) == (78, "authority_refused")
    assert json.loads(policy_path.read_text()) == widened


def test_interactive_flow_from_start_to_scoped_setup_with_each_owner_checkpoint(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins
    import sys
    from typing import cast

    from packages.app.tests.integration.services.test_source_guide import DATE_FLOOR, HOSTILE

    support.queue(guide.fixture, "conversations.history",
                  [support.history_page([support.message("1760000000.000001", HOSTILE)])])
    support.queue(guide.fixture, "conversations.list",
                  [support.channel_page([("CGENERAL", "general")])] * 2)
    arguments = guide.preflight_arguments()
    answers = [
        "start",
        "claude-code", "project", str(guide.project),
        cast(str, arguments["consent_state"]), cast(str, arguments["policy_path"]),
        cast(str, arguments["runtime_path"]), "openai", "2.1.300",
        "1", "1", DATE_FLOOR, "",
        "approve", "approve", "approve", "approve",  # configure, preview, import, acknowledge
        "release", "Guide release", "approve",  # create-destination
        "1", "approve", "approve", "approve",  # route, select, consent
        "approve", "approve",  # release-preview, approve with the full payload shown
        "approve", "approve", "approve",  # policy-apply, setup-preview, setup-apply
    ]
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr(builtins, "input", ask)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    code, _, output = guide.run("slack", *guide.common())
    assert code == 0, output
    assert answers == []
    assert sum("Type approve" in prompt for prompt in prompts) == 13
    state = journal(guide)
    (approved,) = [item for item in state["operations"]
                   if item["action"] == "approve" and item["state"] == "completed"]
    copy_id = approved["evidence"]["copy_capture_id"]
    assert f"Approved copy IDs: {copy_id}" in output
    assert "Selection completeness: 1 of 1" in output
    assert "Reusable recall task:" in output
    assert "Slack coverage beyond the selected approved messages is unknown" in output
    assert "complete sharing payload" in output and "complete policy payload" in output
    assert "\u001b" not in output and "‮" not in output and "\u0007" not in output
    assert (guide.project / ".mcp.json").exists()
    assert not (guide.root / "launched").exists()
    assert state["status"] == "completed"
    # Resuming a completed journal asks nothing and makes no call.
    revision, asked = state["revision"], len(prompts)
    code, _, output = guide.run("slack", *guide.common())
    assert code == 0 and "workflow is completed" in output, output
    assert len(prompts) == asked and journal(guide)["revision"] == revision


def test_current_captured_approval_is_not_released_again(guide: Guide) -> None:
    copies = approved_one(guide)
    receipt_id = [
        json.loads(item["canonical_request_utf8"])["receipt_id"]
        for item in journal(guide)["operations"]
        if item["action"] == "acknowledge-inspection" and item["state"] == "completed"
    ][0]
    code, refused = guide.advance(
        "release-preview", {"receipt_id": receipt_id, "provider_ids": ["openai"]}
    )
    assert (code, refused["code"]) == (2, "invalid_request"), refused
    assert refused["data"]["approved_copy_ids"] == copies and refused["data"]["count"] == 1
    assert refused["next_actions"] == ["policy-preview"]


def test_resumed_interactive_repick_does_not_duplicate_an_approval(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins
    import sys

    approved_one(guide)
    policy_applied(guide)
    answers = ["1", "approve", "approve", "approve"]  # re-pick; policy reuse, setup x2
    monkeypatch.setattr(builtins, "input", lambda _prompt: answers.pop(0))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    code, _, output = guide.run("slack", *guide.common())
    assert code == 0, output
    assert answers == []
    approvals = [item for item in journal(guide)["operations"]
                 if item["action"] == "approve" and item["state"] == "completed"]
    assert len(approvals) == 1
    assert "already has a current approved copy" in output
    assert "Selection completeness: 1 of 1" in output



def test_work_egress_policy_never_exposes_the_slack_original(guide: Guide) -> None:
    """An owner `{work}` egress policy widens Work captures, never managed Slack originals."""
    from open_brain_engine.core.models import PrivacyTier
    from open_brain_engine.engine import open_local_engine
    from open_brain_engine.engine.t03_contracts import SearchPageRequest

    from open_brain.services.session_authority import TrustedSessionAuthority
    from open_brain.services.t03_adapters import owner_authority

    copies = approved_one(guide)
    policy_applied(guide)
    engine: Any = open_local_engine(compile_single_user_local(guide.brain))
    scoped = TrustedSessionAuthority(
        engine,
        policy_path=guide.root / "policy" / "policy.json",
        consent_state_path=guide.root / "consent" / "state.json",
    )

    def found(authority: Any = None) -> set[str]:
        page = engine.retrieval.search_page(
            SearchPageRequest(query="invite-only", limit=10),
            authority=authority or scoped.load(),
        )
        return {str(row["record_id"]) for row in page.to_wire()["results"]}

    # The owner sees the matching Slack original, so the scoped result below is meaningful.
    assert found(owner_authority(engine, session_id="owner-test")) > set(copies)
    assert found() == set(copies)
    engine.owner_egress_policy.set(
        frozenset({PrivacyTier.WORK}), operation_id="op-work",
        authority=owner_authority(engine, session_id="owner-test"),
    )
    assert found() == set(copies)
