"""Browser filing is an independently authorized and replayable route."""

from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine, CaptureFault
from open_brain_engine.engine.browser_filing import recover_filing_operation
from open_brain_engine.engine.browser_pages import BrowserOwnerSetup, BrowserPages

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.t03_adapters import owner_authority_for_principal
from open_brain_browser_host.operations import Request, dispatch
from open_brain_browser_host.policy import Policy, Refused, binding_value

URL = "https://example.org/synthetic-filing"


def test_scoped_filing_replay_and_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "brain"
    profile = compile_single_user_local(root)
    engine = BrainEngine.open(profile)
    owner = owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="test")
    setup = BrowserOwnerSetup(profile, owner)
    pages = BrowserPages(profile)
    source = setup.create_source()
    token = setup.bind(
        source,
        spaces=[None],
        tiers=["public", "work", "personal"],
        operations=["lookup", "save", "status"],
        complete_domain=True,
    )
    binding = pages.binding(token)
    original = Policy("a" * 32, root, token, binding_value(binding))
    visible = engine.inbox.create_space("A visible", delivery_id="visible").space_id
    other = engine.inbox.create_space("B other", delivery_id="other").space_id
    hidden = engine.inbox.create_space("C hidden", delivery_id="hidden").space_id

    def grant(operations: list[str]) -> Policy:
        token = setup.bind(
            binding.source,
            spaces=[None, visible, other],
            tiers=["personal"],
            operations=operations,
            workflow_principal=binding.workflow_principal,
        )
        return Policy(original.extension_id, root, token, binding_value(pages.binding(token)))

    writer = original
    partial = grant(["lookup", "save", "status"])
    reader = grant(["lookup", "status"])
    capture = dispatch(
        original, Request("save", str(uuid4()), {"url": URL, "reason": "synthetic"})
    )["capture_id"]
    assert isinstance(capture, str)
    args = {"url": URL, "capture_id": capture}
    listing = Request.parse(
        {
            "version": "browser.v1",
            "operation": "space.list",
            "args": {**args, "limit": 1, "offset": 0},
        }
    )
    assert dispatch(reader, listing) == {
        "spaces": [{"space_id": visible, "name": "A visible"}],
        "next_offset": 1,
        "current_space": None,
    }
    operation = str(uuid4())

    def file(target: str, op: str = operation) -> Request:
        return Request.parse(
            {
                "version": "browser.v1",
                "operation": "page.file",
                "operation_id": op,
                "args": {**args, "space_id": target},
            }
        )

    for gr, target in ((reader, visible), (partial, visible), (partial, hidden)):
        with pytest.raises((PermissionError, Refused)) as error:
            dispatch(gr, file(target))
        assert str(error.value) in {"operation_denied", "browser_operation_denied"}
    original_capture = next((root / "sources/captures").rglob("*.json")).read_bytes()

    def lose_ack(self: BrainEngine, point: object) -> None:
        if point == CaptureFault.AFTER_ROUTE_RESERVATION:
            raise RuntimeError("lost filing acknowledgment")

    with monkeypatch.context() as m:
        m.setattr(BrainEngine, "_fault", lose_ack)
        with pytest.raises(RuntimeError, match="lost filing acknowledgment"):
            dispatch(writer, file(visible))
    assert dispatch(writer, Request("status", operation, {})) == {"state": "pending"}
    saved = dispatch(writer, file(visible))
    assert saved == {"state": "accepted", "capture_id": capture, "space_id": visible}
    assert dispatch(writer, Request("status", operation, {})) == saved
    with pytest.raises(PermissionError, match="browser_operation_denied"):
        dispatch(partial, file(visible))
    engine.inbox.route(capture, other, delivery_id="owner.refile")
    assert dispatch(writer, file(visible)) == saved
    assert dispatch(writer, listing)["current_space"] == {"space_id": other, "name": "B other"}
    with pytest.raises(ValueError, match="browser_operation_conflict"):
        dispatch(writer, file(other))
    setup.revoke(writer.token)
    with pytest.raises(PermissionError):
        dispatch(writer, file(visible))
    assert next((root / "sources/captures").rglob("*.json")).read_bytes() == original_capture
    archive, restored = tmp_path / "archive", tmp_path / "restored"
    assert run_cli(("export", str(archive), "--verify", "--data-dir", str(root))) == 0
    assert (
        run_cli(("restore", str(archive), str(restored), "--import-id", "import_" + str(uuid4())))
        == 0
    )
    profile = compile_single_user_local(restored)
    setup = BrowserOwnerSetup(
        profile,
        owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="restored"),
    )
    pages = BrowserPages(profile)
    token = setup.bind(
        source,
        spaces=[None, visible, other],
        tiers=["public", "work", "personal"],
        complete_domain=True,
        operations=["lookup", "save", "status"],
        workflow_principal=binding.workflow_principal,
    )
    rebound = Policy(original.extension_id, restored, token, binding_value(pages.binding(token)))
    restored_engine = BrainEngine.open(profile, recover_on_open=False)
    with closing(restored_engine._store.connect()) as db:
        current_grant = pages._grant(db, token, "status")
        rows = db.execute(
            "SELECT r.*,c.delivery_id AS parent_delivery FROM route_operations r "
            "JOIN captures c USING(capture_id)"
        )
        recovered = [
            recover_filing_operation(current_grant, row, row["parent_delivery"]) for row in rows
        ]
        assert sorted(value for value in recovered if value) == [operation]
    assert dispatch(rebound, Request("status", operation, {})) == saved
    assert dispatch(rebound, file(visible)) == saved
    assert dispatch(rebound, listing)["current_space"] == {"space_id": other, "name": "B other"}
    assert dispatch(rebound, file(visible, str(uuid4()))) == saved
    assert dispatch(rebound, listing)["current_space"] == {"space_id": visible, "name": "A visible"}
