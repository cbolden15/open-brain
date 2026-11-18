"""Native recovery pages end after one page of authorized, Chrome-usable intents."""

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.browser_pages import BrowserOwnerSetup, BrowserPages
from open_brain_engine.engine.browser_recovery import list_intents

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.t03_adapters import owner_authority_for_principal
from open_brain_browser_host.operations import Request, dispatch
from open_brain_browser_host.policy import Policy, binding_value


def test_first_page_bounded_and_legacy_ids_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    pages = BrowserPages(profile)
    setup = BrowserOwnerSetup(
        profile,
        owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="test"),
    )
    source = setup.create_source()
    token = setup.bind(
        source,
        tiers=["public", "work", "personal"],
        spaces=[None],
        operations=["lookup", "save", "status"],
        complete_domain=True,
    )
    binding = pages.binding(token)
    policy = Policy("a" * 32, profile.root, token, binding_value(binding))
    base_id = "80000000-0000-4000-8000-000000000000"
    url = "https://example.org/recovery"
    saved = dispatch(policy, Request("save", base_id, {"url": url, "reason": "synthetic"}))
    capture = saved["capture_id"]
    assert isinstance(capture, str)
    with engine._store.transaction() as db:
        row = list(db.execute("SELECT * FROM browser_operations").fetchone())
        for index in range(40):
            row[5] = f"81000000-0000-4000-8000-{index:012d}"
            db.execute("INSERT INTO browser_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
        row[5] = "00000000-0000-0000-0000-000000000000"
        db.execute("INSERT INTO browser_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
    checked = []
    original = pages._result_in_snapshot

    def counted(*args: Any) -> Any:
        checked.append(args[2])
        return original(*args)

    monkeypatch.setattr(pages, "_result_in_snapshot", counted)
    first = list_intents(pages, token, None, 2)
    assert first["intents"] == [
        {"id": base_id, "state": "accepted"},
        {"id": "81000000-0000-4000-8000-000000000000", "state": "accepted"},
    ]
    assert len(checked) <= 3
    assert list_intents(pages, token, str(uuid4()), 2) == {"intents": [], "next_after": None}
    annotation = "10000000-0000-4000-8000-000000000000"
    dispatch(
        policy,
        Request(
            "annotation.add",
            annotation,
            {
                "url": url,
                "capture_id": capture,
                "annotation_id": str(uuid4()),
                "quote": "river",
                "prefix": "",
                "suffix": "",
            },
        ),
    )
    revisit = "30000000-0000-4000-8000-000000000000"
    dispatch(
        policy,
        Request(
            "revisit.set",
            revisit,
            {"url": url, "capture_id": capture, "local_date": "2026-01-01", "zone": "UTC"},
        ),
    )
    space = engine.inbox.create_space("Read", delivery_id="read").space_id
    route = "20000000-0000-4000-8000-000000000000"
    dispatch(
        policy, Request("page.file", route, {"url": url, "capture_id": capture, "space_id": space})
    )
    second_route = str(uuid4())
    dispatch(
        policy,
        Request("page.file", second_route, {"url": url, "capture_id": capture, "space_id": space}),
    )
    cursor: str | None = "81000000-0000-4000-8000-000000000039"
    found: list[str] = []
    while cursor is not None:
        page = list_intents(pages, token, cursor, 1)
        found.extend(item["id"] for item in cast(list[dict[str, str]], page["intents"]))
        cursor = cast(str | None, page["next_after"])
    assert set(found) == {annotation, revisit, route, second_route} and len(found) == 4
    limited = setup.bind(
        source,
        tiers=["personal"],
        spaces=[None],
        operations=["status"],
        workflow_principal=binding.workflow_principal,
    )
    assert list_intents(pages, limited, base_id, 1) == {"intents": [], "next_after": None}
    assert list_intents(pages, limited, None, 1) == {"intents": [], "next_after": None}
    archive, restored = tmp_path / "archive", tmp_path / "restored"
    assert run_cli(("export", str(archive), "--verify", "--data-dir", str(profile.root))) == 0
    assert (
        run_cli(("restore", str(archive), str(restored), "--import-id", "import_" + str(uuid4())))
        == 0
    )
    profile = compile_single_user_local(restored)
    setup = BrowserOwnerSetup(
        profile,
        owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="restored"),
    )
    rebound = setup.bind(
        source,
        spaces=[space],
        tiers=["personal"],
        operations=["status"],
        workflow_principal=binding.workflow_principal,
    )
    restored_pages = BrowserPages(profile)
    for previous, expected in zip(found[1:-1], found[2:], strict=True):
        restored_page = list_intents(restored_pages, rebound, previous, 1)
        assert restored_page["intents"] == [{"id": expected, "state": "accepted"}]
        assert restored_page["next_after"] == (expected if expected != found[-1] else None)
