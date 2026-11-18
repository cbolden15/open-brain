"""Revisit dates belong to the owner's selected calendar zone."""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfoNotFoundError

import pytest
from open_brain_engine.engine import BrainEngine, browser_revisits
from open_brain_engine.engine.browser_pages import BrowserOwnerSetup
from open_brain_engine.engine.browser_revisits import (
    BrowserRevisits,
    date_is_due,
    due_for_authority,
    validate_calendar,
)

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.t03_adapters import owner_authority_for_principal
from open_brain_browser_host.operations import Request, dispatch
from open_brain_browser_host.policy import Policy, binding_value
from open_brain_connectors.runtime.saved_item import identity_url, item_key


def test_due_uses_stored_zone_across_dst_and_midnight() -> None:
    validate_calendar("2026-03-08", "America/Chicago", require_installed=True)
    assert not date_is_due("2026-03-08", "America/Chicago", datetime(2026, 3, 8, 5, 59, tzinfo=UTC))
    assert date_is_due("2026-03-08", "America/Chicago", datetime(2026, 3, 8, 6, 0, tzinfo=UTC))
    assert date_is_due("2026-11-01", "America/Chicago", datetime(2026, 11, 1, 7, 15, tzinfo=UTC))
    validate_calendar("2026-03-08", "Synthetic/Unavailable", require_installed=False)
    assert not date_is_due("2026-03-08", "Synthetic/Unavailable", datetime.now(UTC))
    with pytest.raises(ValueError):
        validate_calendar("2026-03-08", "Synthetic/Unavailable", require_installed=True)
    for value in ("../Etc/UTC", "/etc/passwd", "Etc//UTC", "Right Now"):
        with pytest.raises(ValueError):
            validate_calendar("2026-03-08", value, require_installed=False)


def test_page_revisit_mutations_and_current_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    owner = owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="test")
    setup = BrowserOwnerSetup(profile, owner)
    source = setup.create_source()
    service = BrowserRevisits(profile)
    token = setup.bind(
        source,
        tiers=["public", "personal", "work"],
        spaces=[None],
        operations=["lookup", "save", "status"],
        complete_domain=True,
    )
    binding = service.binding(token)
    policy = Policy("a" * 32, profile.root, token, binding_value(binding))
    url = "https://pages.example.org/revisit"
    capture = dispatch(policy, Request("save", str(uuid4()), {"url": url, "reason": "synthetic"}))[
        "capture_id"
    ]
    assert isinstance(capture, str)
    key = item_key(identity_url(url)[0])
    operation = str(uuid4())

    def mutate(action: str, op: str, **calendar: str) -> dict[str, object]:
        return dispatch(
            policy,
            Request.parse(
                {
                    "version": "browser.v1",
                    "operation": "revisit." + action,
                    "args": {"url": url, "capture_id": capture, **calendar},
                    "operation_id": op,
                }
            ),
        )

    calendar = {"local_date": "2026-03-08", "zone": "America/Chicago"}
    saved = mutate("set", operation, **calendar)
    assert saved == {"state": "accepted", "capture_id": capture, "revisit_action": "set"}
    assert dispatch(policy, Request("status", operation, {})) == saved
    clock = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    due = service.due(token, now=clock, limit=1, offset=0)
    [entry] = cast(list[dict[str, str]], due["revisits"])
    assert entry["title"] and entry["url"] == url
    assert {k: v for k, v in entry.items() if k not in {"title", "url"}} == {
        "capture_id": capture,
        **calendar,
    }
    assert due["next_offset"] is None
    assert service.get(token, key, capture) == {"revisit": {"state": "scheduled", **calendar}}
    with monkeypatch.context() as missing:

        def unavailable(key: str) -> None:
            raise ZoneInfoNotFoundError(key)

        missing.setattr(browser_revisits, "ZoneInfo", unavailable)
        assert service.get(token, key, capture) == {
            "revisit": {"state": "zone_unavailable", **calendar}
        }
        assert service.due(token, now=clock, limit=1, offset=0)["revisits"] == []
        assert mutate("set", operation, **calendar) == saved
        with pytest.raises(ValueError, match="unavailable revisit zone"):
            mutate("set", str(uuid4()), **calendar)
    with pytest.raises(ValueError, match="browser_operation_conflict"):
        mutate("set", operation, local_date="2026-03-09", zone="America/Chicago")
    limited = setup.bind(
        source,
        tiers=["personal"],
        spaces=[None],
        operations=["lookup", "status"],
        workflow_principal=binding.workflow_principal,
    )
    with pytest.raises(PermissionError):
        service.mutate(limited, str(uuid4()), key, capture, "clear")
    narrowed_writer = setup.bind(
        source,
        tiers=["personal"],
        spaces=[None],
        operations=["lookup", "save", "status"],
        workflow_principal=binding.workflow_principal,
    )
    with pytest.raises(PermissionError, match="browser_operation_denied"):
        service.mutate(narrowed_writer, operation, key, capture, "set", **calendar)
    with pytest.raises(PermissionError, match="browser_operation_denied"):
        service.mutate(narrowed_writer, str(uuid4()), key, capture, "clear")
    with closing(engine._store.connect()) as db:
        assert due_for_authority(profile, db, owner, now=clock, limit=1) == [
            {"capture_id": capture, **calendar}
        ]
        assert (
            due_for_authority(profile, db, owner, now=clock, limit=1, space_ids=frozenset()) == []
        )
    hidden = engine.inbox.create_space("private", delivery_id="private").space_id
    engine.inbox.route(capture, hidden, delivery_id="route.hidden")
    assert service.due(limited, now=clock, limit=1, offset=0) == {
        "revisits": [],
        "next_offset": None,
    }
    assert service.status(limited, operation).state == "refused"
    with pytest.raises(PermissionError, match="browser_operation_denied"):
        service.get(limited, key, capture)
    assert service.due(token, now=clock, limit=1, offset=0) == due
    dismiss = str(uuid4())
    assert mutate("dismiss", dismiss)["revisit_action"] == "dismiss"
    assert mutate("set", operation, **calendar) == saved
    assert service.get(token, key, capture) == {"revisit": {"state": "dismissed", **calendar}}
    assert service.due(token, now=clock, limit=1, offset=0)["revisits"] == []
    assert mutate("clear", str(uuid4()))["revisit_action"] == "clear"
    assert service.get(token, key, capture) == {"revisit": None}
    changed = {"local_date": "2026-03-07", "zone": "Asia/Tokyo"}
    assert mutate("set", str(uuid4()), **changed) == saved
    assert mutate("dismiss", dismiss)["revisit_action"] == "dismiss"
    assert service.due(token, now=clock, limit=1, offset=0)["revisits"] == [
        {"capture_id": capture, **changed, "url": url, "title": url}
    ]
    archive, restored = tmp_path / "archive", tmp_path / "restored"
    assert run_cli(("export", str(archive), "--verify", "--data-dir", str(profile.root))) == 0
    assert (
        run_cli(("restore", str(archive), str(restored), "--import-id", "import_" + str(uuid4())))
        == 0
    )
    restored_profile = compile_single_user_local(restored)
    restored_setup = BrowserOwnerSetup(
        restored_profile,
        owner_authority_for_principal(
            principal_id=restored_profile.owner_actor_id, session_id="restored"
        ),
    )
    restored_token = restored_setup.bind(
        source,
        tiers=["public", "personal", "work"],
        spaces=[None],
        operations=["lookup", "save", "status"],
        complete_domain=True,
        workflow_principal=binding.workflow_principal,
    )
    recovered = BrowserRevisits(restored_profile)
    before = recovered.due(restored_token, now=clock, limit=1, offset=0)
    assert before == service.due(token, now=clock, limit=1, offset=0)
    assert recovered.mutate(restored_token, dismiss, key, capture, "dismiss").state == "accepted"
    assert recovered.due(restored_token, now=clock, limit=1, offset=0) == before
    setup.revoke(limited)
    with pytest.raises(PermissionError):
        service.get(limited, key, capture)
    preview = engine.lifecycle.preview_retraction(capture_id=capture, authority=owner)
    engine.lifecycle.commit_retraction(str(preview["preview_id"]), str(uuid4()), authority=owner)
    assert service.due(token, now=clock, limit=1, offset=0)["revisits"] == []
    with closing(engine._store.connect()) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM browser_revisits WHERE state='withdrawn' "
                "AND local_date IS NULL AND zone IS NULL"
            ).fetchone()[0]
            == 4
        )
