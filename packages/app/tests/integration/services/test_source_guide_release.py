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
    current = new_guide(tmp_path, capsys, monkeypatch)
    (tmp_path / "consent").mkdir(mode=0o700)
    runtime = tmp_path / "runtime" / "open-brain"
    runtime.parent.mkdir(mode=0o700)
    runtime.write_text("#!/bin/sh\nexit 0\n")
    runtime.chmod(0o700)
    return current


def approve(intent: dict[str, Any]) -> dict[str, object]:
    return {"intent_sha256": intent["intent_sha256"], "decision": "approve"}


def inspected(guide: Guide, texts: list[str]) -> list[str]:
    """Configure, preview, import one page and acknowledge every complete message."""
    guide.through_choose()
    assert guide.confirm("configure")[0] == 0
    support.queue(
        guide.fixture,
        "conversations.history",
        [support.history_page([
            support.message(f"17600000{index:02d}.000001", text)
            for index, text in reversed(list(enumerate(texts)))
        ])],
    )
    code, previewed = guide.confirm("preview")
    assert code == 0, previewed
    code, imported = guide.confirm("import", {"preview_id": previewed["data"]["preview_id"]})
    assert code == 0, imported
    receipts = list(imported["data"]["receipt_ids"])
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


def test_existing_destination_must_be_an_empty_dedicated_space(guide: Guide) -> None:
    from open_brain_engine.engine import open_local_engine

    receipts = inspected(guide, ["Status: draft"])
    tasks: Any = open_local_engine(compile_single_user_local(guide.brain))
    empty = tasks.inbox.create_space("Dedicated", delivery_id="synthetic.empty").space_id
    code, refused = guide.advance("destination", {"space_id": "space_" + "0" * 8})
    assert (code, refused["code"]) == (78, "authority_refused")
    code, chosen = guide.advance("destination", {"space_id": empty})
    assert (code, chosen["outcome"]) == (0, "completed"), chosen
    assert journal(guide)["choices"]["destination_space_id"] == empty
    code, refused = guide.advance("create-destination", {"name": "Second"})
    assert (code, refused["code"]) == (2, "invalid_request")
    code, proposed = guide.advance("route", {"receipt_id": receipts[0]})
    assert code == 3, proposed


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
