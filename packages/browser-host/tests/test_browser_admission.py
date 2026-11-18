from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import CaptureFault, TextPayload, open_local_engine
from open_brain_engine.engine.browser_pages import (
    BrowserOwnerSetup,
    BrowserPages,
)
from open_brain_engine.engine.contracts import CaptureSubmission, JournalEnvelope
from open_brain_engine.engine.local import BrainEngine

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import owner_authority_for_principal
from open_brain_browser_host import setup as host_setup
from open_brain_browser_host.operations import Request, dispatch
from open_brain_browser_host.policy import Policy, load
from open_brain_connectors.runtime.saved_item import identity_url, item_key

URL = "https://example.org/synthetic"
pytestmark = pytest.mark.integration


def _grant(home: Path, root: Path, *args: str) -> None:
    assert (
        host_setup.main(
            [
                "setup",
                "--extension-id",
                "a" * 32,
                "--data-dir",
                str(root),
                "--operation",
                "lookup",
                "--operation",
                "status",
                "--operation",
                "save",
                "--unassigned",
                "--tier",
                "personal",
                "--complete-domain",
                "--tier",
                "public",
                "--tier",
                "work",
                *args,
            ]
        )
        == 0
    )


def _save(policy: Policy, operation: str, url: str) -> dict[str, object]:
    return dispatch(policy, Request("save", operation, {"url": url, "reason": "synthetic reason"}))


def test_pending_limits_allow_independent_sources_and_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import browser_pages

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "brain"
    policies = []
    for index in range(3):
        installation = home / str(index)
        installation.mkdir(mode=0o700)
        monkeypatch.setenv("HOME", str(installation))
        _grant(installation, root, "--new-source", *(["--initialize-brain"] if index == 0 else []))
        policies.append(load())
    pending = str(uuid4())
    monkeypatch.setattr(browser_pages, "_MAX_PENDING_PER_BINDING", 1)
    monkeypatch.setattr(browser_pages, "_MAX_PENDING_INSTALLATION", 2)
    original = BrainEngine._fault

    def stop_after_custody(self: BrainEngine, point: object) -> None:
        if point == CaptureFault.AFTER_JOURNAL_COMMIT:
            raise InterruptedError

    with monkeypatch.context() as m:
        m.setattr(BrainEngine, "_fault", stop_after_custody)
        with pytest.raises(InterruptedError):
            _save(policies[0], pending, URL)
        with pytest.raises(ValueError, match="browser_pending_limit"):
            _save(policies[0], str(uuid4()), URL + "/new")
        with pytest.raises(InterruptedError):
            _save(policies[1], str(uuid4()), URL)
        with pytest.raises(ValueError, match="browser_pending_limit"):
            _save(policies[2], str(uuid4()), URL)
    assert BrainEngine._fault == original
    assert dispatch(policies[0], Request("status", pending, {})) == {"state": "pending"}
    assert _save(policies[0], pending, URL)["state"] == "accepted"
    assert _save(policies[2], str(uuid4()), URL)["state"] == "accepted"


def test_verified_source_scopes_protected_dedup_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "brain"
    _grant(home, root, "--new-source", "--initialize-brain")
    first = load()
    second_home = home / "second"
    second_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(second_home))
    _grant(second_home, root, "--new-source")
    second = load()
    profile = compile_single_user_local(root)
    from open_brain_engine.core.models import PrivacyTier

    tasks = open_local_engine(profile)
    tasks.capture.accept(
        TextPayload("Synthetic unrelated secret"),
        delivery_id="unrelated",
        privacy_tier=PrivacyTier.SECRET,
    )
    visible = _save(second, str(uuid4()), URL + "/visible")
    result = _save(first, str(uuid4()), URL)
    # Model retained effective-tier narrowing, leaving the accepted capture bytes intact.
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
        db.execute(
            "UPDATE source_revision_privacy SET effective_privacy_json="
            "json_set(effective_privacy_json,'$.tier','secret') WHERE capture_id=?",
            (result["capture_id"],),
        )
    for url in [URL, URL + "/absent"]:
        with pytest.raises(PermissionError, match="browser_operation_denied"):
            _save(first, str(uuid4()), url)
    assert _save(second, str(uuid4()), URL)["state"] == "accepted"
    # Earlier folder deliveries have no reverse source mapping at restore time.
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
        protected_delivery = db.execute(
            "SELECT delivery_id FROM captures WHERE capture_id=?", (result["capture_id"],)
        ).fetchone()[0]
        db.execute("DELETE FROM browser_operations WHERE delivery_id=?", (protected_delivery,))
        db.execute(
            "UPDATE saved_item_admissions SET producer='folder' WHERE delivery_id=?",
            (protected_delivery,),
        )
    with pytest.raises(PermissionError, match="browser_operation_denied"):
        _save(second, str(uuid4()), URL + "/another")
    assert (
        dispatch(second, Request("lookup", "", {"url": URL + "/visible"}))["capture_id"]
        == visible["capture_id"]
    )
    setup = BrowserOwnerSetup(
        profile,
        owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="setup"),
    )
    source = BrowserPages(profile).binding(first.token).source
    setup.register_legacy(source, item_key(identity_url(URL)[0]))
    assert _save(second, str(uuid4()), URL + "/another")["state"] == "accepted"


def test_current_grant_inside_admission_and_read_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "brain"
    _grant(home, root, "--initialize-brain", "--new-source")
    policy = load()
    profile = compile_single_user_local(root)
    setup = BrowserOwnerSetup(
        profile,
        owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="setup"),
    )
    pages = BrowserPages(profile)
    operation = str(uuid4())
    _save(policy, operation, URL)
    nominal = BrowserPages._grant

    def assert_snapshot(db: sqlite3.Connection, token: str, operation: str | None) -> sqlite3.Row:
        assert db.in_transaction
        return nominal(db, token, operation)

    monkeypatch.setattr(BrowserPages, "_grant", staticmethod(assert_snapshot))
    assert pages.status(policy.token, operation).state == "accepted"
    assert pages.lookup(policy.token, item_key(identity_url(URL)[0])) is not None
    nominal_prepare = BrainEngine._prepare_journal_submission

    def revoke_before_transaction(
        self: BrainEngine, submission: CaptureSubmission
    ) -> JournalEnvelope:
        result = nominal_prepare(self, submission)
        setup.revoke(policy.token)
        return result

    monkeypatch.setattr(BrainEngine, "_prepare_journal_submission", revoke_before_transaction)
    with pytest.raises(PermissionError, match="browser_unbound"):
        _save(policy, str(uuid4()), URL + "/revoked")
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM browser_operations").fetchone()[0] == 1


def test_restored_browser_receipt_needs_original_principal_source_and_current_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain.services.local_entrypoints import run_cli

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "brain"
    _grant(home, root, "--initialize-brain", "--new-source")
    policy = load()
    profile = compile_single_user_local(root)
    binding = BrowserPages(profile).binding(policy.token)
    op = str(uuid4())
    accepted = _save(policy, op, URL)
    archive = tmp_path / "portable"
    restored = tmp_path / "restored"
    assert run_cli(("export", str(archive), "--verify", "--data-dir", str(root))) == 0
    assert (
        run_cli(("restore", str(archive), str(restored), "--import-id", "import_" + str(uuid4())))
        == 0
    )
    pages = BrowserPages(compile_single_user_local(restored))
    with pytest.raises(PermissionError, match="browser_unbound"):
        pages.status(policy.token, op)
    owner = BrowserOwnerSetup(
        pages.profile,
        owner_authority_for_principal(
            principal_id=pages.profile.owner_actor_id, session_id="setup"
        ),
    )

    def rebind(tier: str, principal: str | None = None) -> Policy:
        from open_brain_browser_host.policy import binding_value

        token = owner.bind(
            binding.source,
            tiers=[tier],
            spaces=[None],
            operations=["save", "status"],
            workflow_principal=principal,
        )
        return Policy(policy.extension_id, restored, token, binding_value(pages.binding(token)))

    foreign = rebind("personal")
    narrowed = rebind("public", binding.workflow_principal)
    for denied in [foreign, narrowed]:
        assert pages.status(denied.token, op).state == "refused"
        with pytest.raises(PermissionError, match="browser_operation_denied"):
            _save(denied, op, URL)
    rebound = rebind("personal", binding.workflow_principal)
    assert _save(rebound, op, URL) == accepted
    assert pages.status(rebound.token, op).capture_id == accepted["capture_id"]
    with sqlite3.connect(restored / ".open-brain/state/phase1.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM browser_operations").fetchone()[0] == 1


def test_owner_requeue_retains_original_custody_tier_for_domain_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "brain"
    _grant(home, root, "--new-source", "--initialize-brain")
    policy = load()
    profile = compile_single_user_local(root)
    binding = BrowserPages(profile).binding(policy.token)
    operation = str(uuid4())

    def interrupt(self: BrainEngine, point: object) -> None:
        if point == CaptureFault.AFTER_JOURNAL_COMMIT:
            raise InterruptedError

    with monkeypatch.context() as m:
        m.setattr(BrainEngine, "_fault", interrupt)
        with pytest.raises(InterruptedError):
            _save(policy, operation, URL)
    delivery = binding.source.delivery_id(item_key(identity_url(URL)[0]))
    engine = BrainEngine.open(profile, recover_on_open=False)
    with engine._store.transaction() as db:
        engine.ingestion._append_event(db, delivery, "quarantined", 1, {"status": "invalid"})
    assert BrowserPages(profile).status(policy.token, operation).state == "refused"
    engine.ingestion.retry(delivery)
    assert BrowserPages(profile).status(policy.token, operation).state == "pending"
    assert _save(policy, str(uuid4()), URL + "/another")["state"] == "accepted"
    assert BrowserPages(profile).status(policy.token, operation).state == "accepted"


def test_saved_admission_preserves_general_delivery_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.capture import DeliveryConflict

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "brain"
    _grant(home, root, "--new-source", "--initialize-brain")
    policy = load()
    profile = compile_single_user_local(root)
    source = BrowserPages(profile).binding(policy.token).source
    delivery = source.delivery_id(item_key(identity_url(URL)[0]))
    open_local_engine(profile).capture.accept(
        TextPayload("Unrelated owner bytes"), delivery_id=delivery
    )
    with pytest.raises(DeliveryConflict):
        _save(policy, str(uuid4()), URL)
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM saved_item_admissions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM browser_operations").fetchone()[0] == 0
